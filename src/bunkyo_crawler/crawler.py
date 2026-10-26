"""収集処理の本体。robots.txt の遵守、条件付きGETによる差分取得、DB保存を行う。

差分判定は二段階:
    1. ETag / Last-Modified を If-None-Match / If-Modified-Since で送り、304 なら本文を受け取らない
    2. 200 でも本文のハッシュが前回と同一なら新しい版として保存しない
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
from .database import Database
from .extractor import extract
from .fetcher import DisallowedByRobots, FetchError, PoliteFetcher
from .robots import RobotsPolicy

logger = logging.getLogger(__name__)


@dataclass
class CrawlStats:
    checked: int = 0
    updated: int = 0
    not_modified: int = 0
    unchanged: int = 0
    disallowed: int = 0
    skipped: int = 0
    errors: int = 0
    new_document_ids: list[int] = field(default_factory=list)

    def summary(self) -> str:
        return (f"確認 {self.checked} 件 / 更新 {self.updated} / 304未変更 {self.not_modified} / "
                f"内容同一 {self.unchanged} / robots除外 {self.disallowed} / 対象外 {self.skipped} / "
                f"エラー {self.errors}")


def _charset(content_type_header: str) -> str | None:
    m = re.search(r"charset=([\w\-]+)", content_type_header or "", re.I)
    return m.group(1) if m else None


class Crawler:
    def __init__(self, config: CrawlerConfig, db: Database,
                 session: requests.Session | None = None, fetcher: PoliteFetcher | None = None):
        self.config = config
        self.db = db
        self.fetcher = fetcher or PoliteFetcher(config, session=session)
        self.robots = RobotsPolicy(self.fetcher, config.user_agent, config.robots_cache_ttl_sec)

    def fetch_page(self, url: str, target_name: str | None, stats: CrawlStats) -> list[str]:
        """1ページを処理し、HTMLから抽出したリンクを返す（未変更時は空）。"""
        stats.checked += 1
        page = self.db.get_or_create_page(url, target_name)
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
            return []
        except FetchError as e:
            stats.errors += 1
            logger.error("取得失敗: %s（%s）", url, e)
            self.db.mark_checked(page.id, e.status)
            self.db.log_fetch(url, "error", e.status, e.attempts, str(e))
            return []

        if result.status == 304:
            stats.not_modified += 1
            logger.info("更新なし（304）: %s", url)
            self.db.mark_checked(page.id, 304)
            self.db.update_validators(page.id, result.headers.get("etag"), result.headers.get("last-modified"))
            self.db.log_fetch(url, "not_modified", 304, result.attempts)
            return []

        if result.status != 200:
            stats.errors += 1
            logger.error("HTTP %d: %s", result.status, url)
            self.db.mark_checked(page.id, result.status)
            self.db.log_fetch(url, "error", result.status, result.attempts)
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
            return extracted.links

        note = "truncated" if result.truncated else None
        if result.final_url != url:
            note = f"{note + '; ' if note else ''}redirected to {result.final_url}"
        doc_id = self.db.insert_document(
            page_id=page.id, url=result.final_url, http_status=200, content_type=ctype,
            etag=etag, last_modified=last_modified, content_hash=content_hash,
            raw_content=result.content, title=extracted.title, extracted_text=extracted.text,
        )
        self.db.log_fetch(url, "updated", 200, result.attempts, note)
        stats.updated += 1
        stats.new_document_ids.append(doc_id)
        logger.info("更新を保存（document_id=%d）: %s", doc_id, url)
        return extracted.links

    def crawl_target(self, target: TargetConfig, stats: CrawlStats | None = None) -> CrawlStats:
        stats = stats or CrawlStats()
        patterns = [re.compile(p) for p in target.link_patterns]
        host = urlsplit(target.url).netloc
        queue: deque[tuple[str, int]] = deque([(target.url, 0)])
        seen = {target.url}
        processed = 0
        while queue and processed < max(1, target.max_pages):
            url, depth = queue.popleft()
            links = self.fetch_page(url, target.name, stats)
            processed += 1
            if not target.follow_links or depth >= target.max_depth:
                continue
            for link in links:
                if link in seen or urlsplit(link).netloc != host:
                    continue
                if patterns and not any(p.search(link) for p in patterns):
                    continue
                seen.add(link)
                queue.append((link, depth + 1))
        return stats

    def crawl(self, targets: list[TargetConfig]) -> CrawlStats:
        stats = CrawlStats()
        for target in targets:
            if not target.enabled:
                continue
            logger.info("収集開始: %s（%s）", target.name, target.url)
            self.crawl_target(target, stats)
        logger.info("収集完了: %s", stats.summary())
        return stats
