"""相手先サーバーに配慮したHTTP取得処理。

- ホスト単位でリクエスト間隔を空ける（スリープ＋揺らぎ）
- 一時的なエラー（429/5xx/接続エラー）は指数関数的バックオフでリトライ
- Retry-After ヘッダーを尊重
- 取得サイズに上限を設ける
- リダイレクトは自前で辿り、遷移先ごとに robots.txt の可否を確認できるようにする
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import urljoin, urlsplit

import requests

from .config import CrawlerConfig

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)


class FetchError(Exception):
    def __init__(self, message: str, url: str, status: int | None = None, attempts: int = 0):
        super().__init__(message)
        self.url = url
        self.status = status
        self.attempts = attempts


class DisallowedByRobots(FetchError):
    pass


@dataclass
class FetchResult:
    url: str                      # 最初に要求したURL
    final_url: str                # リダイレクト後のURL
    status: int
    headers: dict[str, str]
    content: bytes = b""
    attempts: int = 1
    truncated: bool = False
    redirects: list[str] = field(default_factory=list)

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "").split(";")[0].strip().lower()


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After（秒数 または HTTP日付）を秒数に変換する。"""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (dt - now).total_seconds())


class PoliteFetcher:
    def __init__(
        self,
        config: CrawlerConfig,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ):
        self.config = config
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": config.user_agent,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,application/pdf;q=0.8,*/*;q=0.5",
            "Accept-Language": "ja,en;q=0.5",
        })
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._last_request: dict[str, float] = {}
        self._host_min_interval: dict[str, float] = {}

    # ------------------------------------------------------------ rate limit
    def set_host_interval(self, host: str, seconds: float) -> None:
        """robots.txt の Crawl-delay 等により、ホスト別の最小間隔を引き上げる。"""
        current = self._host_min_interval.get(host, 0.0)
        self._host_min_interval[host] = max(current, seconds)

    def _interval_for(self, host: str) -> float:
        return max(self.config.request_interval_sec, self._host_min_interval.get(host, 0.0))

    def _wait_for_slot(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is None:
            return
        wait = self._interval_for(host) + self._rng() * self.config.interval_jitter_sec
        remaining = wait - (self._clock() - last)
        if remaining > 0:
            logger.debug("待機 %.2f 秒（%s）", remaining, host)
            self._sleep(remaining)

    def _backoff_delay(self, attempt: int) -> float:
        base = self.config.backoff_base_sec * (2 ** attempt)
        jitter = self._rng() * self.config.backoff_base_sec
        return min(self.config.backoff_max_sec, base + jitter)

    # --------------------------------------------------------------- request
    def _request_once(self, url: str, headers: dict[str, str]) -> FetchResult:
        host = urlsplit(url).netloc
        self._wait_for_slot(host)
        try:
            resp = self.session.get(
                url, headers=headers, timeout=self.config.timeout_sec,
                allow_redirects=False, stream=True,
            )
        finally:
            self._last_request[host] = self._clock()
        try:
            chunks: list[bytes] = []
            size = 0
            truncated = False
            if resp.status_code == 200:
                for chunk in resp.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.config.max_content_bytes:
                        keep = len(chunk) - (size - self.config.max_content_bytes)
                        chunks.append(chunk[:keep])
                        truncated = True
                        break
                    chunks.append(chunk)
            return FetchResult(
                url=url, final_url=url, status=resp.status_code,
                headers={k.lower(): v for k, v in resp.headers.items()},
                content=b"".join(chunks), truncated=truncated,
            )
        finally:
            resp.close()

    def _get_with_retry(self, url: str, headers: dict[str, str]) -> FetchResult:
        attempts = 0
        last_status: int | None = None
        while True:
            attempts += 1
            try:
                result = self._request_once(url, headers)
            except (requests.ConnectionError, requests.Timeout) as e:
                last_status = None
                reason = f"{type(e).__name__}: {e}"
                retry_after = None
            else:
                if result.status not in self.config.retry_status_codes:
                    result.attempts = attempts
                    return result
                last_status = result.status
                reason = f"HTTP {result.status}"
                retry_after = parse_retry_after(result.headers.get("retry-after"))

            if attempts > self.config.max_retries:
                raise FetchError(f"リトライ上限に達しました（{reason}）", url, last_status, attempts)
            if retry_after is not None and retry_after > self.config.retry_after_max_sec:
                raise FetchError(
                    f"Retry-After が上限を超えたため中止（{retry_after:.0f}秒）", url, last_status, attempts
                )
            delay = self._backoff_delay(attempts - 1)
            if retry_after is not None:
                delay = max(delay, retry_after)
            logger.warning("%s の取得に失敗（%s）。%.1f 秒後に再試行（%d/%d）",
                           url, reason, delay, attempts, self.config.max_retries)
            self._sleep(delay)

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        is_allowed: Callable[[str], bool] | None = None,
    ) -> FetchResult:
        """URLを取得する。is_allowed が与えられた場合、リダイレクト先ごとに許可を確認する。"""
        headers = dict(headers or {})
        current = url
        redirects: list[str] = []
        total_attempts = 0
        for _ in range(MAX_REDIRECTS + 1):
            if is_allowed is not None and not is_allowed(current):
                raise DisallowedByRobots("robots.txt により取得が許可されていません", current, attempts=total_attempts)
            result = self._get_with_retry(current, headers)
            total_attempts += result.attempts
            if result.status in REDIRECT_CODES and "location" in result.headers:
                nxt = urljoin(current, result.headers["location"])
                if urlsplit(nxt).netloc != urlsplit(current).netloc:
                    # 別ホストへ移ると条件付きヘッダーの意味が変わるため外す
                    headers.pop("If-None-Match", None)
                    headers.pop("If-Modified-Since", None)
                redirects.append(nxt)
                current = nxt
                continue
            result.url = url
            result.final_url = current
            result.attempts = total_attempts
            result.redirects = redirects
            return result
        raise FetchError("リダイレクト回数が上限を超えました", url, attempts=total_attempts)
