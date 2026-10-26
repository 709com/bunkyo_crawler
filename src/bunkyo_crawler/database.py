"""SQLiteによる生データとAI生成テキストの一元管理。

テーブル構成:
    pages        : 監視対象URLごとの状態（ETag / Last-Modified / 直近ハッシュ）
    documents    : 取得した生データ（更新があった版ごとに1行）
    generations  : documents に紐付くAI生成テキスト（成果物種別 × プロンプト版ごと）
    fetch_log    : すべてのHTTPアクセスの記録（304や robots による除外も含む）
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
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
    extracted_text   TEXT
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
        self.conn.commit()

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
        return PageState(row["id"], row["url"], row["etag"], row["last_modified"], row["content_hash"])

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
    ) -> int:
        now = utcnow()
        with self.transaction() as c:
            cur = c.execute(
                """INSERT INTO documents (page_id, url, fetched_at, http_status, content_type, etag,
                       last_modified, content_hash, raw_content, title, extracted_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (page_id, url, now, http_status, content_type, etag, last_modified,
                 content_hash, raw_content, title, extracted_text),
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
            """SELECT d.id, d.url, d.title, d.fetched_at, d.content_type,
                      LENGTH(d.extracted_text) AS text_chars,
                      (SELECT COUNT(*) FROM generations g
                        WHERE g.document_id = d.id AND g.status = 'success') AS generated
                 FROM documents d ORDER BY d.id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

    def documents_missing_generations(self, output_keys: list[str], prompt_version: str) -> list[int]:
        """指定プロンプト版で、成功済みの生成物が揃っていない文書IDを返す。"""
        placeholders = ",".join("?" for _ in output_keys)
        rows = self.conn.execute(
            f"""SELECT d.id FROM documents d
                 WHERE (SELECT COUNT(*) FROM generations g
                         WHERE g.document_id = d.id AND g.prompt_version = ?
                           AND g.status = 'success' AND g.output_type IN ({placeholders})) < ?
                 ORDER BY d.id""",
            (prompt_version, *output_keys, len(output_keys)),
        ).fetchall()
        return [r["id"] for r in rows]

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
