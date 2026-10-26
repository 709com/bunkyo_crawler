"""robots.txt の取得・解釈・遵守。

取得結果の扱い（安全側に倒した方針）:
    2xx           : 内容を解釈して従う
    401 / 403     : サイト全体を取得禁止として扱う
    その他の 4xx  : robots.txt が存在しないものとして全許可
    5xx・通信失敗 : 取得を行わず、エラー（RobotsUnavailable）として扱う。
                    失敗結果は短時間（FAILURE_TTL_SEC）だけ保持し、後で再確認する
Crawl-delay / Request-rate が指定されていれば、設定値より長い場合に採用する。
"""

from __future__ import annotations

import logging
import time
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from .fetcher import FetchError, PoliteFetcher

logger = logging.getLogger(__name__)

FAILURE_TTL_SEC = 600.0


class RobotsUnavailable(FetchError):
    """robots.txt を確認できないため取得を見送ったことを示す。"""


class RobotsPolicy:
    def __init__(
        self,
        fetcher: PoliteFetcher,
        user_agent: str,
        ttl_sec: float = 86400.0,
        clock: Callable[[], float] = time.time,
    ):
        self.fetcher = fetcher
        self.user_agent = user_agent
        self.ttl_sec = ttl_sec
        self._clock = clock
        self._cache: dict[str, tuple[float, RobotFileParser | RobotsUnavailable]] = {}

    @staticmethod
    def robots_url(url: str) -> str:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", ""))

    def _load(self, url: str) -> RobotFileParser | RobotsUnavailable:
        robots_url = self.robots_url(url)
        rp = RobotFileParser(robots_url)
        try:
            # robots.txt 自体の取得には robots チェックを適用しない
            result = self.fetcher.get(robots_url)
        except FetchError as e:
            logger.warning("robots.txt を取得できません（%s）。このホストへのアクセスを見送ります。", e)
            return RobotsUnavailable(f"robots.txt を確認できません: {e}", robots_url, e.status, e.attempts)

        if 200 <= result.status < 300:
            text = result.content.decode("utf-8", errors="replace")
            rp.parse(text.splitlines())
            rp.modified()
        elif result.status in (401, 403):
            logger.warning("robots.txt が HTTP %d。全体を取得禁止とします。", result.status)
            rp.disallow_all = True
        elif 400 <= result.status < 500:
            logger.info("robots.txt が存在しません（HTTP %d）。全許可として扱います。", result.status)
            rp.allow_all = True
        else:
            logger.warning("robots.txt が HTTP %d。このホストへのアクセスを見送ります。", result.status)
            return RobotsUnavailable(f"robots.txt が HTTP {result.status}", robots_url, result.status, result.attempts)

        host = urlsplit(url).netloc
        delay = self._delay_from(rp)
        if delay:
            logger.info("%s の Crawl-delay/Request-rate に従い、間隔を %.1f 秒以上とします。", host, delay)
            self.fetcher.set_host_interval(host, delay)
        return rp

    def _delay_from(self, rp: RobotFileParser) -> float | None:
        delay = rp.crawl_delay(self.user_agent)
        rate = rp.request_rate(self.user_agent)
        candidates = []
        if delay is not None:
            candidates.append(float(delay))
        if rate is not None and rate.requests:
            candidates.append(rate.seconds / rate.requests)
        return max(candidates) if candidates else None

    def _parser_for(self, url: str) -> RobotFileParser:
        key = self.robots_url(url)
        cached = self._cache.get(key)
        now = self._clock()
        if cached is not None:
            ttl = FAILURE_TTL_SEC if isinstance(cached[1], RobotsUnavailable) else self.ttl_sec
            if now - cached[0] > ttl:
                cached = None
        if cached is None:
            cached = (now, self._load(url))
            self._cache[key] = cached
        if isinstance(cached[1], RobotsUnavailable):
            raise cached[1]
        return cached[1]

    def can_fetch(self, url: str) -> bool:
        """許可なら True、禁止なら False。robots.txt を確認できない場合は RobotsUnavailable を送出。"""
        return self._parser_for(url).can_fetch(self.user_agent, url)
