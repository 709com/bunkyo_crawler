"""収集処理の本体。robots.txt の遵守、条件付きGETによる差分取得、DB保存を行う。

収集対象の種類（settings.yaml の targets[].type）:
    page  : 指定ページそのものを文書として取得する
    feed  : RSS/Atom を読み、載っている記事のうち新しいもの（または更新日時が進んだもの）を取得する
    index : 一覧ページを読み、本文中のリンク先のうち、まだ取得していないものを取得する

差分判定:
    1. ETag / Last-Modified を If-None-Match / If-Modified-Since で送り、304 なら本文を受け取らない
       （一覧ページ・RSSが 304 のときは、前回保存したリンク一覧を使う）
    2. 200 でも本文のハッシュが前回と同一なら新しい版として保存しない
    3. 記事は一度取得したら、RSSの日付が新しくならない限り取りに行かない
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import requests

from .config import CrawlerConfig, TargetConfig
from .database import Database, PageState
from .extractor import extract
from .feeds import FeedError, parse_feed
from .fetcher import DisallowedByRobots, FetchError, FetchResult, PoliteFetcher
from .robots import RobotsPolicy

logger = logging.getLogger(__name__)

_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/rdf+xml",
               "application/xml", "text/xml")


@dataclass
class CrawlStats:
    checked: int = 0
    updated: int = 0
    not_modified: int = 0
    unchanged: int = 0
    already_known: int = 0
    disallowed: int = 0
    skipped: int = 0
    errors: int = 0
    targets_skipped: int = 0
    budget_exhausted: list[str] = field(default_factory=list)
    new_document_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        text = (f"アクセス {self.checked} 件 / 新規・更新 {self.updated} / 変更なし(304) {self.not_modified} / "
                f"内容同一 {self.unchanged} / 取得済みのため省略 {self.already_known} / "
                f"robots除外 {self.disallowed} / 対象外 {self.skipped} / エラー {self.errors}")
        if self.targets_skipped:
            text += f" / 実行間隔待ちの対象 {self.targets_skipped}"
        if self.budget_exhausted:
            text += f"\n※ リクエスト上限に達し、残りを次回に回した対象: {', '.join(self.budget_exhausted)}"
        return text


def _charset(content_type_header: str) -> str | None:
    m = re.search(r"charset=([\w\-]+)", content_type_header or "", re.I)
    return m.group(1) if m else None


def _matches(patterns: list[re.Pattern], url: str) -> bool:
    return any(p.search(url) for p in patterns)


class Crawler:
    def __init__(self, config: CrawlerConfig, db: Database,
                 session: requests.Session | None = None, fetcher: PoliteFetcher | None = None):
        self.config = config
        self.db = db
        self.fetcher = fetcher or PoliteFetcher(config, session=session)
        self.robots = RobotsPolicy(self.fetcher, config.user_agent, config.robots_cache_ttl_sec)

    # ------------------------------------------------------------ 共通の取得
    def _conditional_get(self, url: str, page: PageState, stats: CrawlStats) -> FetchResult | None:
        """条件付きGETを行う。失敗時は記録して None を返す。"""
        stats.checked += 1
        headers: dict[str, str] = {}
        if page.etag:
            headers["If-None-Match"] = page.etag
        if page.last_modified:
            headers["If-Modified-Since"] = page.last_modified
        try:
            result = self.fetcher.get(url, headers=headers, is_allowed=self.robots.can_fetch)
        except DisallowedByRobots as e:
            stats.disallowed += 1
            logger.info("robots.txt により除外: %s", e.url)
            self.db.log_fetch(url, "disallowed", note=f"blocked at {e.url}")
            return None
        except FetchError as e:
            stats.errors += 1
            logger.error("取得失敗: %s（%s）", url, e)
            self.db.mark_checked(page.id, e.status)
            self.db.log_fetch(url, "error", e.status, e.attempts, str(e))
            return None
        if result.status not in (200, 304):
            stats.errors += 1
            logger.error("HTTP %d: %s", result.status, url)
            self.db.mark_checked(page.id, result.status)
            self.db.log_fetch(url, "error", result.status, result.attempts)
            return None
        return result

    # ---------------------------------------------------------- 一覧・RSS
    def fetch_listing(self, url: str, target: TargetConfig, as_feed: bool,
                      stats: CrawlStats) -> list[tuple[str, str | None]] | None:
        """一覧ページまたはRSSを読み、(リンク, 日付) の一覧を返す。文書としては保存しない。"""
        page = self.db.get_or_create_page(url, target.name)
        result = self._conditional_get(url, page, stats)
        if result is None:
            return None
        if result.status == 304:
            stats.not_modified += 1
            logger.info("一覧に変更なし（304）: %s", url)
            self.db.mark_checked(page.id, 304)
            self.db.log_fetch(url, "not_modified", 304, result.attempts)
            return [tuple(x) for x in self.db.get_listing_links(page.id)]

        content_hash = hashlib.sha256(result.content).hexdigest()
        if content_hash == page.content_hash:
            stats.unchanged += 1
            links = [tuple(x) for x in self.db.get_listing_links(page.id)]
        else:
            links = self._parse_listing(result, as_feed)
            if links is None:
                stats.errors += 1
                self.db.mark_checked(page.id, result.status)
                self.db.log_fetch(url, "error", result.status, result.attempts, "一覧の解析に失敗")
                return None
        self.db.save_listing(page.id, 200, result.headers.get("etag"), result.headers.get("last-modified"),
                             content_hash, [list(x) for x in links])
        self.db.log_fetch(url, "listing", 200, result.attempts, f"{len(links)} links")
        return links

    def _parse_listing(self, result: FetchResult, as_feed: bool) -> list[tuple[str, str | None]] | None:
        if as_feed or result.content_type in _FEED_TYPES:
            try:
                return [(e.url, e.date) for e in parse_feed(result.content, result.final_url)]
            except FeedError as e:
                logger.error("RSSを読めません: %s（%s）", result.final_url, e)
                return None
        extracted = extract(result.content, result.content_type, result.final_url,
                            _charset(result.headers.get("content-type", "")))
        return [(link, None) for link in extracted.links]

    # -------------------------------------------------------------- 記事
    def fetch_document(self, url: str, target: TargetConfig, stats: CrawlStats) -> list[str]:
        """1ページを文書として処理し、本文中のリンクを返す（未変更時は空）。"""
        page = self.db.get_or_create_page(url, target.name)
        result = self._conditional_get(url, page, stats)
        if result is None:
            return []

        if result.status == 304:
            stats.not_modified += 1
            logger.info("更新なし（304）: %s", url)
            self.db.mark_checked(page.id, 304)
            self.db.update_validators(page.id, result.headers.get("etag"), result.headers.get("last-modified"))
            self.db.log_fetch(url, "not_modified", 304, result.attempts)
            return []

        ctype = result.content_type
        if ctype not in self.config.allowed_content_types:
            stats.skipped += 1
            logger.info("対象外のContent-Type（%s）: %s", ctype, url)
            self.db.mark_checked(page.id, 200)
            self.db.log_fetch(url, "skipped", 200, result.attempts, f"content-type={ctype}")
            return []

        content_hash = hashlib.sha256(result.content).hexdigest()
        etag = result.headers.get("etag")
        last_modified = result.headers.get("last-modified")
        extracted = extract(result.content, ctype, result.final_url, _charset(result.headers.get("content-type", "")))

        if content_hash == page.content_hash:
            stats.unchanged += 1
            logger.info("内容同一のため保存省略: %s", url)
            self.db.mark_checked(page.id, 200)
            self.db.update_validators(page.id, etag, last_modified)
            self.db.log_fetch(url, "unchanged", 200, result.attempts)
            return []

        note = "truncated" if result.truncated else None
        if result.final_url != url:
            note = f"{note + '; ' if note else ''}redirected to {result.final_url}"
        doc_id = self.db.insert_document(
            page_id=page.id, url=result.final_url, http_status=200, content_type=ctype,
            etag=etag, last_modified=last_modified, content_hash=content_hash,
            raw_content=result.content, title=extracted.title, extracted_text=extracted.text,
            output_profile=target.output_profile,
        )
        self.db.log_fetch(url, "updated", 200, result.attempts, note)
        stats.updated += 1
        stats.new_document_ids.append(doc_id)
        logger.info("保存（document_id=%d）: %s", doc_id, url)
        return extracted.links

    def _item_needed(self, url: str, date: str | None) -> bool:
        """記事を取りに行く必要があるか。

        未取得のもの、前回エラーだったもの、RSSの日付が前回確認より新しいものだけを取りに行く。
        （対象外のファイル形式だったものも「確認済み」として扱い、繰り返し取りに行かない）
        """
        page = self.db.find_page(url)
        if page is None or page.last_checked_at is None:
            return True
        if not self.db.has_document(url) and page.last_status not in (200, 304):
            return True
        return bool(date and date > page.last_checked_at)

    # -------------------------------------------------------------- 対象
    def crawl_target(self, target: TargetConfig, stats: CrawlStats | None = None) -> CrawlStats:
        stats = stats or CrawlStats()
        if target.type == "page":
            self.fetch_document(target.url, target, stats)
            return stats

        list_pats = [re.compile(p) for p in target.list_patterns]
        item_pats = [re.compile(p) for p in target.item_patterns]
        excl_pats = [re.compile(p) for p in target.exclude_patterns]
        att_pats = [re.compile(p) for p in target.attachment_patterns]
        hosts = set(target.allowed_hosts) or {urlsplit(target.url).netloc}

        # (url, 深さ, 種類, 日付)。種類: listing / item / attachment
        queue: deque[tuple[str, int, str, str | None]] = deque([(target.url, 0, "listing", None)])
        seen = {target.url}
        budget = max(1, target.max_pages)

        while queue:
            url, depth, kind, date = queue.popleft()
            if kind != "listing" and not self._item_needed(url, date):
                stats.already_known += 1
                continue
            if budget <= 0:
                stats.budget_exhausted.append(target.name)
                logger.warning("[%s] リクエスト上限（%d）に達しました。残り %d 件は次回に回します。",
                               target.name, target.max_pages, len(queue) + 1)
                break
            budget -= 1

            if kind == "listing":
                entries = self.fetch_listing(url, target, as_feed=(target.type == "feed" and depth == 0),
                                             stats=stats)
                for link, link_date in entries or []:
                    if link in seen or urlsplit(link).netloc not in hosts:
                        continue
                    seen.add(link)
                    if _matches(list_pats, link):
                        if depth < target.max_depth:
                            queue.append((link, depth + 1, "listing", None))
                        continue
                    if _matches(excl_pats, link) or (item_pats and not _matches(item_pats, link)):
                        continue
                    queue.append((link, depth, "item", link_date))
            else:
                links = self.fetch_document(url, target, stats)
                if kind == "item" and att_pats:
                    for link in links:
                        if link in seen or urlsplit(link).netloc not in hosts:
                            continue
                        if _matches(att_pats, link) and not _matches(excl_pats, link):
                            seen.add(link)
                            queue.append((link, depth, "attachment", None))
        return stats

    def crawl(self, targets: list[TargetConfig], ignore_interval: bool = False) -> CrawlStats:
        stats = CrawlStats()
        for target in targets:
            if not target.enabled:
                continue
            if not ignore_interval and not self.db.target_due(target.name, target.interval_days):
                stats.targets_skipped += 1
                logger.info("実行間隔（%s日）に達していないため省略: %s", target.interval_days, target.name)
                continue
            logger.info("収集開始: %s（%s）", target.name, target.url)
            self.crawl_target(target, stats)
            self.db.mark_target_run(target.name)
        logger.info("収集完了: %s", stats.summary())
        return stats
