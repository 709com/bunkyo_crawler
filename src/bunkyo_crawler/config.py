"""設定ファイル（YAML）の読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SETTINGS_PATH = PROJECT_ROOT / "config" / "settings.yaml"
SETTINGS_ENV = "BUNKYO_CRAWLER_SETTINGS"


class ConfigError(Exception):
    """設定ファイルの内容に問題がある場合に送出する。"""


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"設定ファイルが見つかりません: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"設定ファイルの形式が不正です（辞書ではありません）: {path}")
    return data


@dataclass
class CrawlerConfig:
    user_agent: str
    request_interval_sec: float = 5.0
    interval_jitter_sec: float = 1.0
    timeout_sec: float = 30.0
    max_content_bytes: int = 10 * 1024 * 1024
    max_retries: int = 4
    backoff_base_sec: float = 2.0
    backoff_max_sec: float = 120.0
    retry_after_max_sec: float = 600.0
    retry_status_codes: tuple[int, ...] = (429, 500, 502, 503, 504)
    respect_robots_txt: bool = True
    robots_cache_ttl_sec: float = 86400.0
    allowed_content_types: tuple[str, ...] = (
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/pdf",
    )


@dataclass
class TargetConfig:
    name: str
    url: str
    description: str = ""
    enabled: bool = True
    follow_links: bool = False
    max_depth: int = 0
    max_pages: int = 1
    link_patterns: list[str] = field(default_factory=list)


@dataclass
class AIConfig:
    model: str
    effort: str = "medium"
    api_key_env: str = "ANTHROPIC_API_KEY"
    prompts_file: Path = PROJECT_ROOT / "config" / "prompts.yaml"
    timeout_sec: float = 600.0
    max_retries: int = 3
    use_refusal_fallback: bool = True
    max_input_chars: int = 150_000
    min_input_chars: int = 50
    provider: str = "anthropic"


@dataclass
class Settings:
    crawler: CrawlerConfig
    targets: list[TargetConfig]
    database_path: Path
    ai: AIConfig
    log_level: str = "INFO"
    log_file: Path | None = None


def load_settings(path: str | Path | None = None) -> Settings:
    settings_path = _resolve(path or os.environ.get(SETTINGS_ENV) or DEFAULT_SETTINGS_PATH)
    raw = _load_yaml(settings_path)

    c = dict(raw.get("crawler") or {})
    ua_template = c.pop("user_agent", "BunkyoCrawler/{version} (+{contact})")
    contact = c.pop("contact", "")
    if not contact:
        raise ConfigError("crawler.contact（User-Agentに記載する連絡先）を設定してください。")
    user_agent = ua_template.format(version=__version__, contact=contact)
    for key in ("retry_status_codes", "allowed_content_types"):
        if key in c:
            c[key] = tuple(c[key])
    crawler = CrawlerConfig(user_agent=user_agent, **c)
    if not crawler.respect_robots_txt:
        raise ConfigError("respect_robots_txt は true である必要があります。")
    if crawler.request_interval_sec < 1.0:
        raise ConfigError("request_interval_sec は 1.0 秒以上にしてください（相手先サーバーの負荷軽減）。")

    targets = [TargetConfig(**t) for t in (raw.get("targets") or [])]

    a = dict(raw.get("ai") or {})
    if "prompts_file" in a:
        a["prompts_file"] = _resolve(a["prompts_file"])
    if "model" not in a:
        raise ConfigError("ai.model を設定してください。")
    ai = AIConfig(**a)

    db = raw.get("database") or {}
    log = raw.get("logging") or {}
    return Settings(
        crawler=crawler,
        targets=targets,
        database_path=_resolve(db.get("path", "data/bunkyo_crawler.db")),
        ai=ai,
        log_level=str(log.get("level", "INFO")).upper(),
        log_file=_resolve(log["file"]) if log.get("file") else None,
    )


def load_prompts(path: str | Path) -> dict[str, Any]:
    """プロンプト設定を読み込み、必須項目を検証する。"""
    data = _load_yaml(_resolve(path))
    for key in ("version", "system_prompt", "source_template", "outputs"):
        if key not in data:
            raise ConfigError(f"プロンプト設定に {key} がありません。")
    keys = [o.get("key") for o in data["outputs"]]
    if len(keys) != len(set(keys)) or not all(keys):
        raise ConfigError("outputs の key が空または重複しています。")
    for o in data["outputs"]:
        if not o.get("instructions"):
            raise ConfigError(f"outputs[{o['key']}] に instructions がありません。")
    return data
