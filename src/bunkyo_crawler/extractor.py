"""取得した生データ（HTML / テキスト / PDF）から本文とリンクを抽出する。"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urldefrag, urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# 本文以外とみなして除去する要素
_NOISE_TAGS = ["script", "style", "noscript", "template", "iframe", "svg", "header", "footer", "nav", "form"]
# 本文領域の候補（上から順に探す）
_MAIN_SELECTORS = ["main", "[role=main]", "#main", "#contents", "#content", "article", ".main", ".contents"]


@dataclass
class Extracted:
    title: str | None
    text: str
    links: list[str] = field(default_factory=list)


def normalize_text(text: str) -> str:
    lines = [re.sub(r"[ \t　]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def extract_html(content: bytes, base_url: str) -> Extracted:
    soup = BeautifulSoup(content, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else None

    for tag in soup(_NOISE_TAGS):
        tag.decompose()
    root = None
    for selector in _MAIN_SELECTORS:
        root = soup.select_one(selector)
        if root is not None and root.get_text(strip=True):
            break
        root = None
    root = root or soup.body or soup

    # リンクは本文領域からだけ拾う（サイト共通のメニュー等を記事と取り違えないため）
    links: list[str] = []
    for a in root.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        absolute, _ = urldefrag(urljoin(base_url, href))
        if absolute.startswith(("http://", "https://")) and absolute not in links:
            links.append(absolute)
    return Extracted(title=title, text=normalize_text(root.get_text("\n")), links=links)


def extract_pdf(content: bytes) -> Extracted:
    try:
        from pypdf import PdfReader
    except ImportError:
        logger.warning("pypdf が未インストールのため PDF の本文を抽出できません。")
        return Extracted(title=None, text="")
    try:
        reader = PdfReader(io.BytesIO(content))
        title = None
        if reader.metadata and reader.metadata.title:
            title = str(reader.metadata.title)
        pages = [page.extract_text() or "" for page in reader.pages]
        return Extracted(title=title, text=normalize_text("\n\n".join(pages)))
    except Exception as e:  # 壊れたPDF等。生データは保存済みなので本文空で続行
        logger.warning("PDF の本文抽出に失敗しました: %s", e)
        return Extracted(title=None, text="")


def extract(content: bytes, content_type: str, base_url: str, encoding: str | None = None) -> Extracted:
    if content_type in ("text/html", "application/xhtml+xml"):
        return extract_html(content, base_url)
    if content_type == "application/pdf":
        return extract_pdf(content)
    text = content.decode(encoding or "utf-8", errors="replace")
    return Extracted(title=None, text=normalize_text(text))
