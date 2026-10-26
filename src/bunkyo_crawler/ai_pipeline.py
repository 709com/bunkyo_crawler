"""取得したテキストから6種類の成果物をAIで生成し、DBに保存する。

- システムプロンプト・生成指示は config/prompts.yaml から読み込む（コード内に持たない）
- 出典資料のブロックにプロンプトキャッシュを指定し、6回の生成で入力を再利用する
- 文字数制約（X用の加重文字数など）を機械的に検査し、違反時は再生成を依頼する
- 生成物はすべて「未レビュー」で保存する（公開前に人が確認する運用を前提）
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .config import AIConfig, load_prompts
from .database import Database

logger = logging.getLogger(__name__)

# 安全分類器による拒否時に、サーバー側で推奨モデルへ切り替える機能（ベータ）
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIConfigError(Exception):
    pass


# ---------------------------------------------------------------- 文字数計算
def _is_light_char(ch: str) -> bool:
    """X の加重文字数で 1 と数える文字（ラテン文字・一般的な記号）か。"""
    cp = ord(ch)
    return (
        0x0000 <= cp <= 0x10FF
        or 0x2000 <= cp <= 0x200D
        or 0x2010 <= cp <= 0x201F
        or 0x2032 <= cp <= 0x2037
    )


def x_weighted_length(text: str) -> int:
    """X（旧Twitter）方式の加重文字数。全角=2、半角=1。URLは一律23として数える。"""
    text = unicodedata.normalize("NFC", text)
    total = 0
    for part in re.split(r"(https?://\S+)", text):
        if part.startswith(("http://", "https://")):
            total += 23
        else:
            total += sum(1 if _is_light_char(ch) else 2 for ch in part)
    return total


def check_constraints(text: str, constraints: dict[str, Any]) -> str | None:
    """制約違反があれば内容を説明する文字列を、なければ None を返す。"""
    if not text.strip():
        return "出力が空です。"
    limit = constraints.get("max_weighted_length")
    if limit is not None:
        n = x_weighted_length(text)
        if n > limit:
            return f"X方式の加重文字数が {n} で、上限 {limit}（全角換算 {limit // 2} 字）を超えています。"
    limit = constraints.get("max_chars")
    if limit is not None and len(text) > limit:
        return f"文字数が {len(text)} 字で、上限 {limit} 字を超えています。"
    return None


# ------------------------------------------------------------------ 生成結果
@dataclass
class GenerationResult:
    output_type: str
    label: str
    status: str                     # success / constraint_violation / refused / error / skipped
    content: str | None = None
    model: str | None = None
    error: str | None = None
    attempts: int = 0
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


class AIGenerator:
    def __init__(self, config: AIConfig, client: Any | None = None, prompts: dict[str, Any] | None = None):
        self.config = config
        self.prompts = prompts or load_prompts(config.prompts_file)
        self.version = str(self.prompts["version"])
        profile = self.prompts.get("profile") or {}
        self._fmt = {"author_role": profile.get("author_role", ""), "author_name": profile.get("author_name", "")}
        self.outputs: list[dict[str, Any]] = self.prompts["outputs"]
        self._client = client

    # ------------------------------------------------------------ クライアント
    @property
    def client(self) -> Any:
        if self._client is None:
            api_key = os.environ.get(self.config.api_key_env)
            if not api_key:
                raise AIConfigError(f"環境変数 {self.config.api_key_env} にAPIキーが設定されていません。")
            try:
                import anthropic
            except ImportError as e:
                raise AIConfigError("anthropic パッケージが未インストールです（pip install -r requirements.txt）。") from e
            self._client = anthropic.Anthropic(
                api_key=api_key, timeout=self.config.timeout_sec, max_retries=self.config.max_retries,
            )
        return self._client

    def output_keys(self) -> list[str]:
        return [o["key"] for o in self.outputs]

    def _render(self, template: str, **values: Any) -> str:
        return template.format(**{**self._fmt, **values})

    # ---------------------------------------------------------------- API呼出
    def _call(self, system: str, user_content: list[dict[str, Any]], max_tokens: int) -> Any:
        params: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
            "output_config": {"effort": self.config.effort},
        }
        if self.config.use_refusal_fallback:
            params["betas"] = [FALLBACK_BETA]
            params["extra_body"] = {"fallbacks": "default"}
            stream_ctx = self.client.beta.messages.stream(**params)
        else:
            stream_ctx = self.client.messages.stream(**params)
        # 長い出力でもHTTPタイムアウトしないようストリーミングで受け取る
        with stream_ctx as stream:
            return stream.get_final_message()

    @staticmethod
    def _text_of(message: Any) -> str:
        return "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", "") == "text").strip()

    # ------------------------------------------------------------------ 生成
    def generate_one(self, source_block: dict[str, Any], output: dict[str, Any]) -> GenerationResult:
        key, label = output["key"], output.get("label", output["key"])
        constraints = output.get("constraints") or {}
        max_attempts = int(constraints.get("max_attempts", 1))
        system = self._render(self.prompts["system_prompt"])
        instructions = self._render(output["instructions"])
        result = GenerationResult(output_type=key, label=label, status="error")

        draft: str | None = None
        problem: str | None = None
        for attempt in range(1, max_attempts + 1):
            result.attempts = attempt
            task_text = instructions
            if draft is not None and problem is not None:
                task_text += "\n\n" + self._render(self.prompts["revision_template"], draft=draft, problem=problem)
            message = self._call(system, [source_block, {"type": "text", "text": task_text}],
                                 int(output.get("max_tokens", 4000)))

            usage = message.usage
            result.model = message.model
            result.stop_reason = message.stop_reason
            result.input_tokens += (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
            result.cache_read_tokens += getattr(usage, "cache_read_input_tokens", 0) or 0
            result.output_tokens += usage.output_tokens or 0

            if message.stop_reason == "refusal":
                details = getattr(message, "stop_details", None)
                result.status = "refused"
                result.error = f"モデルが生成を辞退しました（category={getattr(details, 'category', None)}）"
                return result

            text = self._text_of(message)
            if message.stop_reason == "max_tokens":
                problem = "出力が途中で打ち切られました。より簡潔にまとめてください。"
            else:
                problem = check_constraints(text, constraints)
            if problem is None:
                result.status = "success"
                result.content = text
                return result
            logger.warning("[%s] 制約違反（%d/%d回目）: %s", key, attempt, max_attempts, problem)
            draft = text

        result.status = "constraint_violation"
        result.content = draft
        result.error = problem
        return result

    def generate_for_document(
        self,
        db: Database,
        document_id: int,
        output_keys: list[str] | None = None,
        force: bool = False,
    ) -> list[GenerationResult]:
        doc = db.get_document(document_id)
        if doc is None:
            raise ValueError(f"document_id={document_id} が見つかりません。")
        targets = [o for o in self.outputs if not output_keys or o["key"] in output_keys]
        text = doc["extracted_text"] or ""
        results: list[GenerationResult] = []

        skip_reason = None
        if len(text) < self.config.min_input_chars:
            skip_reason = f"本文が短すぎます（{len(text)} 字）。"
        elif len(text) > self.config.max_input_chars:
            skip_reason = (f"本文が上限（{self.config.max_input_chars} 字）を超えています（{len(text)} 字）。"
                           "分割または上限の見直しが必要です。")
        if skip_reason:
            logger.warning("document_id=%d: %s 生成をスキップします。", document_id, skip_reason)
            for o in targets:
                r = GenerationResult(o["key"], o.get("label", o["key"]), "skipped", error=skip_reason)
                db.save_generation(document_id, r.output_type, r.label, self.version, r.status, error=r.error, attempts=0)
                results.append(r)
            return results

        source_block = {
            "type": "text",
            "text": self._render(
                self.prompts["source_template"],
                title=doc["title"] or "（タイトルなし）", url=doc["url"], fetched_at=doc["fetched_at"], text=text,
            ),
            # 同じ出典資料を6回使うため、入力をキャッシュして費用と時間を抑える
            "cache_control": {"type": "ephemeral"},
        }

        for output in targets:
            if not force and db.has_successful_generation(document_id, output["key"], self.version):
                logger.info("document_id=%d [%s] は生成済みのためスキップ", document_id, output["key"])
                continue
            logger.info("document_id=%d [%s] を生成中…", document_id, output["key"])
            try:
                r = self.generate_one(source_block, output)
            except AIConfigError:
                raise
            except Exception as e:  # API障害等は記録して次の成果物へ
                logger.error("document_id=%d [%s] 生成失敗: %s", document_id, output["key"], e)
                r = GenerationResult(output["key"], output.get("label", output["key"]), "error",
                                     error=f"{type(e).__name__}: {e}")
            db.save_generation(
                document_id, r.output_type, r.label, self.version, r.status, content=r.content, model=r.model,
                error=r.error, attempts=r.attempts, stop_reason=r.stop_reason, input_tokens=r.input_tokens,
                output_tokens=r.output_tokens, cache_read_tokens=r.cache_read_tokens,
            )
            results.append(r)
        return results
