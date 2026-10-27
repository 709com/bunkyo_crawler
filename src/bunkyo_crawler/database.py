"""SQLiteによる生データとAI生成テキストの一元管理。

テーブル構成:
    pages        : 監視対象URLごとの状態（ETag / Last-Modified / 直近ハッシュ / 一覧ページのリンク）
    documents    : 取得した生データ（更新があった版ごとに1行）。生成する成果物の組を output_profile で持つ
    generations  : documents に紐付くAI生成テキスト（成果物種別 × プロンプト版ごと）
    fetch_log    : すべてのHTTPアクセスの記録（304や robots による除外も含む）
    target_runs  : 収集対象ごとの最終実行日時（週1回などの間隔制御に使う）
"""

from __future__ import annotations

import sqlite3
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT    NOT NULL UNIQUE,
    target_name      TEXT,
    etag             TEXT,
    last_modified    TEXT,
    content_hash     TEXT,
    last_status      INTEGER,
    last_checked_at  TEXT,
    last_changed_at  TEXT,
    links_json       TEXT,                      -- 一覧ページ・RSSから抽出したリンク（304時に再利用）
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    page_id          INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    url              TEXT    NOT NULL,
    fetched_at       TEXT    NOT NULL,
    http_status      INTEGER NOT NULL,
    content_type     TEXT,
    etag             TEXT,
    last_modified    TEXT,
    content_hash     TEXT    NOT NULL,
    raw_content      BLOB    NOT NULL,
    title            TEXT,
    extracted_text   TEXT,
    output_profile   TEXT    NOT NULL DEFAULT 'full'
);
CREATE INDEX IF NOT EXISTS idx_documents_page ON documents(page_id, fetched_at);

CREATE TABLE IF NOT EXISTS generations (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id      INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    output_type      TEXT    NOT NULL,
    output_label     TEXT,
    prompt_version   TEXT    NOT NULL,
    model            TEXT,
    status           TEXT    NOT NULL,          -- success / constraint_violation / refused / error / skipped
    review_status    TEXT    NOT NULL DEFAULT 'unreviewed',  -- unreviewed / approved / rejected
    content          TEXT,
    error            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 1,
    stop_reason      TEXT,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cache_read_tokens INTEGER,
    created_at       TEXT    NOT NULL,
    UNIQUE (document_id, output_type, prompt_version)
);
CREATE INDEX IF NOT EXISTS idx_generations_doc ON generations(document_id);

CREATE TABLE IF NOT EXISTS target_runs (
    target_name      TEXT    PRIMARY KEY,
    last_run_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_log (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    url              TEXT    NOT NULL,
    requested_at     TEXT    NOT NULL,
    result           TEXT    NOT NULL,          -- updated / not_modified / unchanged / disallowed / error / skipped
    http_status      INTEGER,
    attempts         INTEGER,
    note             TEXT
);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class PageState:
    id: int
    url: str
    etag: str | None
    last_modified: str | None
    content_hash: str | None
    last_checked_at: str | None = None
    last_status: int | None = None


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if str(path) != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """古い版で作ったDBに、後から追加した列を足す。"""
        added = {
            "pages": [("links_json", "TEXT")],
            "documents": [("output_profile", "TEXT NOT NULL DEFAULT 'full'")],
        }
        for table, columns in added.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # ------------------------------------------------------------------ pages
    def get_or_create_page(self, url: str, target_name: str | None = None) -> PageState:
        row = self.conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        if row is None:
            with self.transaction() as c:
                cur = c.execute(
                    "INSERT INTO pages (url, target_name, created_at) VALUES (?, ?, ?)",
                    (url, target_name, utcnow()),
                )
            return PageState(cur.lastrowid, url, None, None, None)
        return PageState(row["id"], row["url"], row["etag"], row["last_modified"], row["content_hash"],
                         row["last_checked_at"], row["last_status"])

    def find_page(self, url: str) -> PageState | None:
        row = self.conn.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        return PageState(row["id"], row["url"], row["etag"], row["last_modified"], row["content_hash"],
                         row["last_checked_at"], row["last_status"])

    def has_document(self, url: str) -> bool:
        """このURLを記事として取得済み（文書が1件以上ある）か。"""
        row = self.conn.execute(
            "SELECT 1 FROM documents d JOIN pages p ON p.id = d.page_id WHERE p.url = ? LIMIT 1", (url,)
        ).fetchone()
        return row is not None

    def save_listing(self, page_id: int, status: int, etag: str | None, last_modified: str | None,
                     content_hash: str, links: list) -> None:
        """一覧ページ・RSSの状態とリンクを保存する（文書としては保存しない）。"""
        with self.transaction() as c:
            c.execute(
                """UPDATE pages SET etag = ?, last_modified = ?, content_hash = ?, last_status = ?,
                       last_checked_at = ?, links_json = ? WHERE id = ?""",
                (etag, last_modified, content_hash, status, utcnow(), json.dumps(links, ensure_ascii=False),
                 page_id),
            )

    def get_listing_links(self, page_id: int) -> list:
        row = self.conn.execute("SELECT links_json FROM pages WHERE id = ?", (page_id,)).fetchone()
        return json.loads(row["links_json"]) if row and row["links_json"] else []

    # ------------------------------------------------------------ target_runs
    def target_due(self, target_name: str, interval_days: float) -> bool:
        if interval_days <= 0:
            return True
        row = self.conn.execute("SELECT last_run_at FROM target_runs WHERE target_name = ?",
                                (target_name,)).fetchone()
        if row is None:
            return True
        last = datetime.fromisoformat(row["last_run_at"])
        return datetime.now(timezone.utc) - last >= timedelta(days=interval_days)

    def mark_target_run(self, target_name: str) -> None:
        with self.transaction() as c:
            c.execute(
                """INSERT INTO target_runs (target_name, last_run_at) VALUES (?, ?)
                   ON CONFLICT(target_name) DO UPDATE SET last_run_at = excluded.last_run_at""",
                (target_name, utcnow()),
            )

    def mark_checked(self, page_id: int, status: int | None) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE pages SET last_status = ?, last_checked_at = ? WHERE id = ?",
                (status, utcnow(), page_id),
            )

    def update_validators(self, page_id: int, etag: str | None, last_modified: str | None) -> None:
        with self.transaction() as c:
            c.execute(
                "UPDATE pages SET etag = COALESCE(?, etag), last_modified = COALESCE(?, last_modified) WHERE id = ?",
                (etag, last_modified, page_id),
            )

    # -------------------------------------------------------------- documents
    def insert_document(
        self,
        page_id: int,
        url: str,
        http_status: int,
        content_type: str | None,
        etag: str | None,
        last_modified: str | None,
        content_hash: str,
        raw_content: bytes,
        title: str | None,
        extracted_text: str | None,
        output_profile: str = "full",
    ) -> int:
        now = utcnow()
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO documents (page_id, url, fetched_at, http_status, content_type, etag,
                       last_modified, content_hash, raw_content, title, extracted_text, output_profile)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (page_id, url, now, http_status, content_type, etag, last_modified,
                 content_hash, raw_content, title, extracted_text, output_profile),
            )
            c.execute(
                """UPDATE pages SET etag = ?, last_modified = ?, content_hash = ?, last_status = ?,
                       last_checked_at = ?, last_changed_at = ? WHERE id = ?""",
                (etag, last_modified, content_hash, http_status, now, now, page_id),
            )
        return int(cur.lastrowid)

    def get_document(self, document_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()

    def list_documents(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT d.id, d.url, d.title, d.fetched_at, d.content_type, d.output_profile,
                      LENGTH(d.extracted_text) AS text_chars,
                      (SELECT COUNT(*) FROM generations g
                        WHERE g.document_id = d.id AND g.status = 'success') AS generated
                 FROM documents d ORDER BY d.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def document_profiles(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT id, output_profile FROM documents ORDER BY id").fetchall()

    def successful_output_types(self, document_id: int, prompt_version: str) -> set[str]:
        rows = self.conn.execute(
            """SELECT output_type FROM generations
                WHERE document_id = ? AND prompt_version = ? AND status = 'success'""",
            (document_id, prompt_version),
        ).fetchall()
        return {r["output_type"] for r in rows}

    # ------------------------------------------------------------ generations
    def has_successful_generation(self, document_id: int, output_type: str, prompt_version: str) -> bool:
        row = self.conn.execute(
            """SELECT 1 FROM generations WHERE document_id = ? AND output_type = ?
                   AND prompt_version = ? AND status = 'success'""",
            (document_id, output_type, prompt_version),
        ).fetchone()
        return row is not None

    def save_generation(
        self,
        document_id: int,
        output_type: str,
        output_label: str,
        prompt_version: str,
        status: str,
        content: str | None = None,
        model: str | None = None,
        error: str | None = None,
        attempts: int = 1,
        stop_reason: str | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cache_read_tokens: int | None = None,
    ) -> int:
        """同一（文書・種別・プロンプト版）の既存行は置き換える。レビュー状態は未レビューに戻る。"""
        with self.transaction() as c:
            c.execute(
                "DELETE FROM generations WHERE document_id = ? AND output_type = ? AND prompt_version = ?",
                (document_id, output_type, prompt_version),
            )
            cur = c.execute(
                """INSERT INTO generations (document_id, output_type, output_label, prompt_version, model,
                       status, content, error, attempts, stop_reason, input_tokens, output_tokens,
                       cache_read_tokens, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (document_id, output_type, output_label, prompt_version, model, status, content, error,
                 attempts, stop_reason, input_tokens, output_tokens, cache_read_tokens, utcnow()),
            )
        return int(cur.lastrowid)

    def get_generations(self, document_id: int, prompt_version: str | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM generations WHERE document_id = ?"
        params: list = [document_id]
        if prompt_version:
            sql += " AND prompt_version = ?"
            params.append(prompt_version)
        return self.conn.execute(sql + " ORDER BY prompt_version DESC, id", params).fetchall()

    def set_review_status(self, generation_id: int, review_status: str) -> bool:
        if review_status not in ("unreviewed", "approved", "rejected"):
            raise ValueError(f"不正なレビュー状態です: {review_status}")
        with self.transaction() as c:
            cur = c.execute("UPDATE generations SET review_status = ? WHERE id = ?", (review_status, generation_id))
        return cur.rowcount > 0

    # -------------------------------------------------------------- fetch_log
    def log_fetch(self, url: str, result: str, http_status: int | None = None,
                  attempts: int | None = None, note: str | None = None) -> None:
        with self.transaction() as c:
            c.execute(
                """INSERT INTO fetch_log (url, requested_at, result, http_status, attempts, note)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (url, utcnow(), result, http_status, attempts, note),
            )
