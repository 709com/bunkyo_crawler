"""RSS / Atom（更新情報の配信ファイル）の読み取り。

RSS 1.0（RDF）・RSS 2.0・Atom のいずれにも対応し、各記事のリンクと日付を取り出す。
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urldefrag, urljoin

logger = logging.getLogger(__name__)

JST = timezone(timedelta(hours=9))
_DATE_TAGS = ("pubDate", "date", "updated", "published", "modified", "issued")


@dataclass
class FeedEntry:
    url: str
    title: str | None = None
    date: str | None = None        # ISO 8601（UTC）。不明なら None


class FeedError(Exception):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_date(text: str | None) -> str | None:
    """RFC 822（RSS 2.0）と ISO 8601（RSS 1.0 / Atom）の日付を UTC の ISO 形式にそろえる。"""
    if not text:
        return None
    text = text.strip()
    dt: datetime | None = None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=JST)  # 時差の書かれていない日付は日本時間とみなす
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _parse_xml(content: bytes) -> ET.Element:
    try:
        return ET.fromstring(content)
    except (ET.ParseError, ValueError) as first:
        # expat が対応していない文字コード（Shift_JIS 等）の場合は、宣言を読んで変換し直す
        m = re.match(rb'<\?xml[^>]*encoding=["\']([\w\-]+)["\']', content)
        if not m:
            raise FeedError(f"RSSを解析できません: {first}") from first
        try:
            text = content.decode(m.group(1).decode("ascii"))
        except (LookupError, UnicodeDecodeError) as e:
            raise FeedError(f"RSSの文字コードを変換できません: {e}") from e
        text = re.sub(r"^<\?xml[^>]*\?>", "", text.lstrip())
        try:
            return ET.fromstring(text)
        except ET.ParseError as e:
            raise FeedError(f"RSSを解析できません: {e}") from e


def parse_feed(content: bytes, base_url: str) -> list[FeedEntry]:
    root = _parse_xml(content)
    entries: list[FeedEntry] = []
    seen: set[str] = set()
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        link = title = date = None
        for child in el:
            name = _local(child.tag)
            if name == "link" and link is None:
                href = child.get("href")
                rel = child.get("rel", "alternate")
                if href and rel == "alternate":
                    link = href
                elif child.text and child.text.strip():
                    link = child.text.strip()
            elif name == "title":
                title = (child.text or "").strip() or None
            elif name in _DATE_TAGS and date is None:
                date = parse_date(child.text)
        if link is None:
            # RSS 1.0 では item の rdf:about 属性がリンクになっている
            link = next((v for k, v in el.attrib.items() if _local(k) == "about"), None)
        if not link:
            continue
        url, _ = urldefrag(urljoin(base_url, link))
        if url in seen:
            continue
        seen.add(url)
        entries.append(FeedEntry(url=url, title=title, date=date))
    return entries
