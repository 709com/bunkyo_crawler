"""コマンドラインインターフェース。

    python -m bunkyo_crawler init-db
    python -m bunkyo_crawler crawl [--target NAME] [--url URL]
    python -m bunkyo_crawler generate [--document-id N | --pending] [--types summary x_post ...] [--force]
    python -m bunkyo_crawler run [--no-ai]
    python -m bunkyo_crawler list
    python -m bunkyo_crawler show --document-id N [--output FILE]
    python -m bunkyo_crawler review --generation-id N --status approved
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import ConfigError, Settings, TargetConfig, load_settings
from .database import Database

logger = logging.getLogger("bunkyo_crawler")


def setup_logging(settings: Settings, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, settings.log_level, logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if settings.log_file:
        settings.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))
    logging.basicConfig(level=level, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)


def _select_targets(settings: Settings, name: str | None, url: str | None) -> list[TargetConfig]:
    if url:
        return [TargetConfig(name="adhoc", url=url, type="page")]
    if name:
        found = [t for t in settings.targets if t.name == name]
        if not found:
            raise ConfigError(f"対象 {name} が settings.yaml にありません。")
        return [TargetConfig(**{**found[0].__dict__, "enabled": True})]
    return settings.targets


def cmd_init_db(settings: Settings, db: Database, args: argparse.Namespace) -> int:
    print(f"データベースを初期化しました: {settings.database_path}")
    return 0


def cmd_crawl(settings: Settings, db: Database, args: argparse.Namespace) -> list[int]:
    from .crawler import Crawler

    crawler = Crawler(settings.crawler, db)
    # 対象を個別に指定した場合は、実行間隔（週1回など）に関係なく実行する
    ignore_interval = bool(args.ignore_interval or args.target or args.url)
    stats = crawler.crawl(_select_targets(settings, args.target, args.url), ignore_interval=ignore_interval)
    print(stats.summary())
    if stats.new_document_ids:
        print("新規保存した document_id:", ", ".join(map(str, stats.new_document_ids)))
    return stats.new_document_ids


def _generate(settings: Settings, db: Database, document_ids: list[int], types: list[str] | None, force: bool) -> int:
    from .ai_pipeline import AIConfigError, AIGenerator

    try:
        generator = AIGenerator(settings.ai)
        generator.client  # APIキー・SDKの有無を生成開始前に確認する
        if types:
            unknown = set(types) - set(generator.output_keys())
            if unknown:
                print(f"未定義の成果物種別です: {', '.join(sorted(unknown))}", file=sys.stderr)
                return 2
        failures = 0
        for doc_id in document_ids:
            for r in generator.generate_for_document(db, doc_id, types, force=force):
                mark = "OK " if r.status == "success" else "NG "
                print(f"{mark} document_id={doc_id} {r.label}: {r.status}"
                      + (f"（{r.error}）" if r.error else ""))
                failures += r.status != "success"
        return 1 if failures else 0
    except AIConfigError as e:
        print(f"AI生成を実行できません: {e}", file=sys.stderr)
        return 2


def cmd_generate(settings: Settings, db: Database, args: argparse.Namespace) -> int:
    if args.document_id:
        ids = [args.document_id]
    else:
        from .ai_pipeline import AIGenerator

        gen = AIGenerator(settings.ai)  # クライアントは遅延生成のため、ここではAPIを呼ばない
        ids = gen.pending_documents(db, args.types)
    if not ids:
        print("生成対象の文書はありません。")
        return 0
    return _generate(settings, db, ids, args.types, args.force)


def cmd_run(settings: Settings, db: Database, args: argparse.Namespace) -> int:
    new_ids = cmd_crawl(settings, db, args)
    if args.no_ai or not new_ids:
        return 0
    limit = settings.ai.max_documents_per_run
    if len(new_ids) > limit:
        print(f"新しい文書が {len(new_ids)} 件あります。費用の歯止めのため、今回AI生成するのは {limit} 件までです。"
              "残りは次回、または `generate` で生成されます。")
        new_ids = new_ids[:limit]
    return _generate(settings, db, new_ids, None, False)


def cmd_list(settings: Settings, db: Database, args: argparse.Namespace) -> int:
    rows = db.list_documents(args.limit)
    if not rows:
        print("保存済みの文書はありません。")
        return 0
    print("| id | 取得日時(UTC) | 種類 | 生成済 | 文字数 | タイトル | URL |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['id']} | {r['fetched_at']} | {r['output_profile']} | {r['generated']} | {r['text_chars'] or 0} | "
              f"{(r['title'] or '').replace('|', '／')} | {r['url']} |")
    return 0


def cmd_show(settings: Settings, db: Database, args: argparse.Namespace) -> int:
    doc = db.get_document(args.document_id)
    if doc is None:
        print(f"document_id={args.document_id} が見つかりません。", file=sys.stderr)
        return 1
    lines = [
        f"# {doc['title'] or '（タイトルなし）'}",
        "",
        f"- document_id: {doc['id']}",
        f"- URL: {doc['url']}",
        f"- 取得日時(UTC): {doc['fetched_at']}",
        "",
        "> AI生成の下書きです。公開・提出前に必ず内容を確認してください。",
        "",
    ]
    for g in db.get_generations(doc["id"]):
        lines += [
            f"## {g['output_label'] or g['output_type']}",
            "",
            f"<!-- generation_id={g['id']} status={g['status']} review={g['review_status']} "
            f"prompt_version={g['prompt_version']} model={g['model']} -->",
            "",
            g["content"] or f"（{g['status']}: {g['error'] or ''}）",
            "",
        ]
    if args.with_text:
        lines += ["## 抽出テキスト（原文）", "", doc["extracted_text"] or "", ""]
    text = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"書き出しました: {args.output}")
    else:
        print(text)
    return 0


def cmd_review(settings: Settings, db: Database, args: argparse.Namespace) -> int:
    if not db.set_review_status(args.generation_id, args.status):
        print(f"generation_id={args.generation_id} が見つかりません。", file=sys.stderr)
        return 1
    print(f"generation_id={args.generation_id} を {args.status} に更新しました。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bunkyo_crawler", description="行政公開情報の収集とAI解析")
    p.add_argument("--settings", help="設定ファイルのパス（既定: config/settings.yaml）")
    p.add_argument("-v", "--verbose", action="store_true", help="詳細ログを出力")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="データベースを初期化").set_defaults(func=cmd_init_db)

    for name, func, help_ in (("crawl", cmd_crawl, "収集のみ実行"), ("run", cmd_run, "収集→新規分をAI生成")):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--target", help="settings.yaml の targets[].name を指定")
        sp.add_argument("--url", help="単発でURLを指定して取得")
        sp.add_argument("--ignore-interval", action="store_true",
                        help="実行間隔（週1回など）を無視して、有効な対象をすべて実行")
        if name == "run":
            sp.add_argument("--no-ai", action="store_true", help="AI生成を行わない")
        sp.set_defaults(func=func)

    sp = sub.add_parser("generate", help="保存済み文書からAI生成")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--document-id", type=int, help="対象の document_id")
    g.add_argument("--pending", action="store_true", help="未生成の文書すべて（既定）")
    sp.add_argument("--types", nargs="+", help="生成する成果物の key（既定: 全6種）")
    sp.add_argument("--force", action="store_true", help="生成済みでも再生成")
    sp.set_defaults(func=cmd_generate)

    sp = sub.add_parser("list", help="保存済み文書の一覧")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("show", help="文書と生成物をMarkdownで表示・書き出し")
    sp.add_argument("--document-id", type=int, required=True)
    sp.add_argument("--output", help="書き出し先ファイル")
    sp.add_argument("--with-text", action="store_true", help="抽出した原文も含める")
    sp.set_defaults(func=cmd_show)

    sp = sub.add_parser("review", help="生成物のレビュー状態を更新")
    sp.add_argument("--generation-id", type=int, required=True)
    sp.add_argument("--status", choices=["unreviewed", "approved", "rejected"], required=True)
    sp.set_defaults(func=cmd_review)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(args.settings)
    except ConfigError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        return 2
    setup_logging(settings, args.verbose)
    db = Database(settings.database_path)
    try:
        result = args.func(settings, db, args)
        return 0 if isinstance(result, list) else int(result)
    except ConfigError as e:
        print(f"設定エラー: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("中断しました。", file=sys.stderr)
        return 130
    finally:
        db.close()
