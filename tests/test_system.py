"""ネットワーク・APIを使わずに主要機能を検証するテスト。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from bunkyo_crawler.ai_pipeline import AIGenerator, check_constraints, x_weighted_length  # noqa: E402
from bunkyo_crawler.config import AIConfig, CrawlerConfig, TargetConfig, load_prompts, load_settings  # noqa: E402
from bunkyo_crawler.crawler import Crawler  # noqa: E402
from bunkyo_crawler.database import Database  # noqa: E402
from bunkyo_crawler.fetcher import FetchError, PoliteFetcher, parse_retry_after  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
HOST = "https://example.lg.jp"


# ------------------------------------------------------------------ 疑似HTTP
class FakeResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict | None = None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def iter_content(self, chunk_size: int = 1024):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class FakeSession:
    """URLごとに応答（または例外）のリストを順に返す。"""

    def __init__(self, routes: dict[str, list]):
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, dict(headers or {})))
        queue = self.routes.get(url)
        if not queue:
            return FakeResponse(404)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def make_config(**overrides) -> CrawlerConfig:
    base = dict(user_agent="BunkyoCrawler/1.0 (+https://example.invalid)", request_interval_sec=5.0,
                interval_jitter_sec=0.0, max_retries=3, backoff_base_sec=2.0, backoff_max_sec=60.0)
    base.update(overrides)
    return CrawlerConfig(**base)


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def make_crawler(routes, **cfg):
    clock = FakeClock()
    config = make_config(**cfg)
    session = FakeSession(routes)
    fetcher = PoliteFetcher(config, session=session, sleep=clock.sleep, clock=clock.time, rng=lambda: 0.0)
    db = Database(":memory:")
    return Crawler(config, db, fetcher=fetcher), session, clock, db


HTML = "<html><head><title>お知らせ</title></head><body><nav>メニュー</nav><main><h1>保育園の募集</h1>" \
       "<p>令和8年度の入園申込を受け付けます。</p><a href='/a.html'>詳細</a></main></body></html>"


# ---------------------------------------------------------------- クローラー
class CrawlerTests(unittest.TestCase):
    def test_robots_disallow_is_respected(self):
        crawler, session, _, db = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(200, b"User-agent: *\nDisallow: /private/\n")],
            f"{HOST}/private/x.html": [FakeResponse(200, b"secret", {"Content-Type": "text/html"})],
        })
        stats = crawler.crawl([TargetConfig(name="t", url=f"{HOST}/private/x.html")])
        self.assertEqual(stats.disallowed, 1)
        self.assertNotIn(f"{HOST}/private/x.html", [c[0] for c in session.calls])

    def test_robots_forbidden_means_disallow_all(self):
        crawler, session, _, _ = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(403)],
            f"{HOST}/page.html": [FakeResponse(200, HTML.encode(), {"Content-Type": "text/html"})],
        })
        stats = crawler.crawl([TargetConfig(name="t", url=f"{HOST}/page.html")])
        self.assertEqual(stats.disallowed, 1)

    def test_robots_unreachable_is_error_and_page_not_fetched(self):
        crawler, session, _, db = make_crawler({
            f"{HOST}/robots.txt": [requests.ConnectionError("down")],
            f"{HOST}/page.html": [FakeResponse(200, HTML.encode(), {"Content-Type": "text/html"})],
        }, max_retries=1)
        stats = crawler.crawl([TargetConfig(name="t", url=f"{HOST}/page.html")])
        self.assertEqual(stats.errors, 1)
        self.assertNotIn(f"{HOST}/page.html", [c[0] for c in session.calls])

    def test_crawl_delay_raises_interval(self):
        crawler, _, clock, _ = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(200, b"User-agent: *\nCrawl-delay: 10\n")],
            f"{HOST}/page.html": [FakeResponse(200, HTML.encode(), {"Content-Type": "text/html"})],
        })
        crawler.crawl([TargetConfig(name="t", url=f"{HOST}/page.html")])
        self.assertTrue(any(abs(s - 10.0) < 1e-6 for s in clock.sleeps), clock.sleeps)

    def test_interval_between_requests(self):
        crawler, _, clock, _ = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(404)],
            f"{HOST}/page.html": [FakeResponse(200, HTML.encode(), {"Content-Type": "text/html"})],
        })
        crawler.crawl([TargetConfig(name="t", url=f"{HOST}/page.html")])
        self.assertEqual(clock.sleeps, [5.0])  # robots.txt取得後、本体取得前に5秒待機

    def test_conditional_get_and_diff(self):
        url = f"{HOST}/page.html"
        headers = {"Content-Type": "text/html; charset=utf-8", "ETag": '"v1"',
                   "Last-Modified": "Mon, 26 Oct 2026 00:00:00 GMT"}
        crawler, session, _, db = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(404)],
            url: [FakeResponse(200, HTML.encode(), headers), FakeResponse(304, b"", {"ETag": '"v1"'}),
                  FakeResponse(200, HTML.encode(), headers)],
        })
        target = TargetConfig(name="t", url=url)
        s1 = crawler.crawl([target])
        self.assertEqual(s1.updated, 1)
        s2 = crawler.crawl([target])
        self.assertEqual(s2.not_modified, 1)
        sent = [h for u, h in session.calls if u == url][1]
        self.assertEqual(sent.get("If-None-Match"), '"v1"')
        self.assertEqual(sent.get("If-Modified-Since"), "Mon, 26 Oct 2026 00:00:00 GMT")
        s3 = crawler.crawl([target])  # 200だが内容同一 → 保存しない
        self.assertEqual(s3.unchanged, 1)
        self.assertEqual(len(db.list_documents()), 1)
        doc = db.get_document(s1.new_document_ids[0])
        self.assertEqual(doc["title"], "お知らせ")
        self.assertIn("入園申込", doc["extracted_text"])
        self.assertNotIn("メニュー", doc["extracted_text"])
        self.assertEqual(bytes(doc["raw_content"]), HTML.encode())

    def test_exponential_backoff_then_success(self):
        url = f"{HOST}/page.html"
        crawler, _, clock, _ = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(404)],
            url: [FakeResponse(503), requests.ConnectionError("reset"),
                  FakeResponse(200, HTML.encode(), {"Content-Type": "text/html"})],
        })
        stats = crawler.crawl([TargetConfig(name="t", url=url)])
        self.assertEqual(stats.updated, 1)
        # 間隔5秒 → 503 → バックオフ2秒 → 接続失敗 → バックオフ4秒 → 成功
        self.assertIn(2.0, clock.sleeps)
        self.assertIn(4.0, clock.sleeps)

    def test_retry_gives_up(self):
        clock = FakeClock()
        fetcher = PoliteFetcher(make_config(max_retries=2), session=FakeSession({f"{HOST}/x": [FakeResponse(500)]}),
                                sleep=clock.sleep, clock=clock.time, rng=lambda: 0.0)
        with self.assertRaises(FetchError) as cm:
            fetcher.get(f"{HOST}/x")
        self.assertEqual(cm.exception.attempts, 3)

    def test_retry_after_header(self):
        self.assertEqual(parse_retry_after("120"), 120.0)
        self.assertIsNone(parse_retry_after("invalid date"))

    def test_user_agent_is_set(self):
        _, session, _, _ = make_crawler({})
        self.assertIn("BunkyoCrawler", session.headers["User-Agent"])


# -------------------------------------------------------------------- AI生成
class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, texts: dict[str, list[str]]):
        self.texts = texts
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **params):
        self.requests.append(params)
        task = params["messages"][0]["content"][1]["text"]
        key = next((k for k in self.texts if k in task), "default")
        queue = self.texts[key]
        text = queue.pop(0) if len(queue) > 1 else queue[0]
        msg = SimpleNamespace(
            model=params["model"], stop_reason="end_turn", stop_details=None,
            content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=20, cache_creation_input_tokens=0,
                                  cache_read_input_tokens=5),
        )
        return FakeStream(msg)


class AIPipelineTests(unittest.TestCase):
    def tearDown(self):
        self.db.close()

    def setUp(self):
        self.db = Database(":memory:")
        page = self.db.get_or_create_page(f"{HOST}/p.html")
        self.doc_id = self.db.insert_document(page.id, f"{HOST}/p.html", 200, "text/html", None, None, "h",
                                              b"<html/>", "お知らせ", "令和8年度の入園申込を受け付けます。" * 10)
        self.prompts = load_prompts(ROOT / "config" / "prompts.yaml")

    def test_prompts_file_defines_six_outputs(self):
        keys = [o["key"] for o in self.prompts["outputs"]]
        self.assertEqual(keys, ["summary", "x_post", "facebook_post", "line_message", "blog_article",
                                "council_questions"])

    def test_generates_all_outputs_and_links_to_document(self):
        client = FakeClient({"default": ["生成された本文です。"]})
        gen = AIGenerator(AIConfig(model="claude-opus-5-5"), client=client, prompts=self.prompts)
        results = gen.generate_for_document(self.db, self.doc_id)
        self.assertEqual(len(results), 6)
        self.assertTrue(all(r.status == "success" for r in results))
        rows = self.db.get_generations(self.doc_id)
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(r["review_status"] == "unreviewed" for r in rows))
        # 出典資料ブロックにキャッシュ指定、システムプロンプトは設定ファイル由来
        req = client.requests[0]
        self.assertEqual(req["messages"][0]["content"][0]["cache_control"], {"type": "ephemeral"})
        self.assertIn("文京区議会議員", req["system"])
        self.assertEqual(req["extra_body"], {"fallbacks": "default"})
        self.assertNotIn("temperature", req)
        # 2回目は生成済みのためAPIを呼ばない
        n = len(client.requests)
        gen.generate_for_document(self.db, self.doc_id)
        self.assertEqual(len(client.requests), n)

    def test_x_post_too_long_is_revised(self):
        long_text = "あ" * 200
        client = FakeClient({"X（旧Twitter）": [long_text, "短い投稿 #文京区"], "default": ["本文"]})
        gen = AIGenerator(AIConfig(model="claude-opus-5-5"), client=client, prompts=self.prompts)
        results = gen.generate_for_document(self.db, self.doc_id, ["x_post"])
        self.assertEqual(results[0].status, "success")
        self.assertEqual(results[0].attempts, 2)
        self.assertIn("<draft>", client.requests[-1]["messages"][0]["content"][1]["text"])

    def test_weighted_length(self):
        self.assertEqual(x_weighted_length("abc"), 3)
        self.assertEqual(x_weighted_length("文京区"), 6)
        self.assertEqual(x_weighted_length("見て https://example.com/very/long/path"), 2 * 2 + 1 + 23)
        self.assertIsNotNone(check_constraints("あ" * 130, {"max_weighted_length": 250}))
        self.assertIsNone(check_constraints("あ" * 125, {"max_weighted_length": 250}))


# ------------------------------------------------------------ RSS・一覧ページ
RSS2 = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>新着</title>
<item><title>報道発表A</title><link>https://example.lg.jp/news/a.html</link>
<pubDate>Mon, 26 Oct 2026 09:00:00 +0900</pubDate></item>
<item><title>外部</title><link>https://other.example.com/x.html</link></item>
</channel></rss>""".encode()

RSS1 = """<?xml version="1.0" encoding="Shift_JIS"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<item rdf:about="https://example.lg.jp/news/b.html"><title>注目B</title><dc:date>2026-10-27T10:00:00+09:00</dc:date></item>
</rdf:RDF>""".encode("shift_jis")

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>C</title>
<link rel="alternate" href="/news/c.html"/><updated>2026-10-27T01:00:00Z</updated></entry></feed>"""


def article(title: str, body: str, extra: str = "") -> bytes:
    return (f"<html><head><title>{title}</title></head><body><nav><a href='/menu.html'>menu</a></nav>"
            f"<main><p>{body}</p>{extra}</main></body></html>").encode()


class FeedTests(unittest.TestCase):
    def test_parse_rss2_rss1_atom(self):
        from bunkyo_crawler.feeds import parse_feed

        e2 = parse_feed(RSS2, f"{HOST}/shinchaku.xml")
        self.assertEqual(e2[0].url, f"{HOST}/news/a.html")
        self.assertEqual(e2[0].date, "2026-10-26T00:00:00+00:00")
        e1 = parse_feed(RSS1, f"{HOST}/chuumoku.xml")  # Shift_JIS も読める
        self.assertEqual((e1[0].url, e1[0].title), (f"{HOST}/news/b.html", "注目B"))
        ea = parse_feed(ATOM, f"{HOST}/atom.xml")
        self.assertEqual(ea[0].url, f"{HOST}/news/c.html")

    def test_feed_target_fetches_only_new_items(self):
        feed_url = f"{HOST}/shinchaku.xml"
        rss_v2 = RSS2.replace(b"</channel>", b"<item><link>https://example.lg.jp/news/d.html</link></item></channel>")
        crawler, session, _, db = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(404)],
            feed_url: [FakeResponse(200, RSS2, {"Content-Type": "application/rss+xml", "ETag": '"f1"'}),
                       FakeResponse(304, b"", {"ETag": '"f1"'}),
                       FakeResponse(200, rss_v2, {"Content-Type": "application/rss+xml", "ETag": '"f2"'})],
            f"{HOST}/news/a.html": [FakeResponse(200, article("A", "記事Aの本文"), {"Content-Type": "text/html"})],
            f"{HOST}/news/d.html": [FakeResponse(200, article("D", "記事Dの本文"), {"Content-Type": "text/html"})],
        })
        target = TargetConfig(name="rss", url=feed_url, type="feed", output_profile="full")
        s1 = crawler.crawl([target])
        self.assertEqual(s1.updated, 1)                      # 記事Aのみ（外部サイトのリンクは対象外）
        self.assertEqual(db.list_documents()[0]["output_profile"], "full")
        s2 = crawler.crawl([target])                         # RSSが304 → 保存済みリンクで判定
        self.assertEqual((s2.not_modified, s2.already_known, s2.updated), (1, 1, 0))
        s3 = crawler.crawl([target])                         # 記事Dが追加 → Dだけ取りに行く
        self.assertEqual((s3.updated, s3.already_known), (1, 1))
        self.assertEqual([u for u, _ in session.calls].count(f"{HOST}/news/a.html"), 1)
        self.assertNotIn("https://other.example.com/x.html", [u for u, _ in session.calls])

    def test_index_target_follows_year_lists_and_attachments(self):
        top = f"{HOST}/kekka/index.html"
        top_html = article("一覧", "委員会", "<a href='/kekka/r07/index.html'>令和7年</a>"
                                            "<a href='/kekka/r08/index.html'>令和8年</a>")
        r08_html = article("令和8年", "一覧", "<a href='/kugikai/p100.html'>文教委員会</a>"
                                             "<a href='/other/index.html'>他</a>")
        session_html = article("文教委員会(令和8年9月)", "資料一覧", "<a href='/documents/1/houkoku.pdf'>報告</a>"
                                                                  "<a href='/documents/1/memo.docx'>メモ</a>")
        crawler, session, _, db = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(404)],
            top: [FakeResponse(200, top_html, {"Content-Type": "text/html"})],
            f"{HOST}/kekka/r08/index.html": [FakeResponse(200, r08_html, {"Content-Type": "text/html"})],
            f"{HOST}/kugikai/p100.html": [FakeResponse(200, session_html, {"Content-Type": "text/html"})],
            f"{HOST}/documents/1/houkoku.pdf": [FakeResponse(200, b"%PDF-1.4 dummy", {"Content-Type": "application/pdf"})],
        })
        target = TargetConfig(
            name="iinkai", url=top, type="index", output_profile="council", max_depth=2,
            list_patterns=[r"/kekka/r(0[8-9]|[1-9][0-9])/index\.html$"],
            exclude_patterns=[r"/index\.html$"], attachment_patterns=[r"\.pdf$"],
        )
        stats = crawler.crawl([target])
        called = [u for u, _ in session.calls]
        self.assertNotIn(f"{HOST}/kekka/r07/index.html", called)   # 令和7年はたどらない
        self.assertNotIn(f"{HOST}/other/index.html", called)       # 除外
        self.assertNotIn(f"{HOST}/menu.html", called)              # 本文外のメニューは拾わない
        self.assertIn(f"{HOST}/documents/1/houkoku.pdf", called)   # 添付PDF
        self.assertNotIn(f"{HOST}/documents/1/memo.docx", called)
        self.assertEqual(stats.updated, 2)                          # 資料ページ＋PDF（一覧ページは保存しない）
        self.assertTrue(all(r["output_profile"] == "council" for r in db.list_documents()))

    def test_weekly_interval(self):
        crawler, session, _, db = make_crawler({
            f"{HOST}/robots.txt": [FakeResponse(404)],
            f"{HOST}/p.html": [FakeResponse(200, HTML.encode(), {"Content-Type": "text/html"})],
        })
        target = TargetConfig(name="weekly", url=f"{HOST}/p.html", interval_days=7)
        crawler.crawl([target])
        n = len(session.calls)
        stats = crawler.crawl([target])
        self.assertEqual(stats.targets_skipped, 1)
        self.assertEqual(len(session.calls), n)
        crawler.crawl([target], ignore_interval=True)
        self.assertGreater(len(session.calls), n)

    def test_budget_limit(self):
        links = "".join(f"<a href='/n/{i}.html'>{i}</a>" for i in range(5))
        routes = {f"{HOST}/robots.txt": [FakeResponse(404)],
                  f"{HOST}/list.html": [FakeResponse(200, article("一覧", "x", links), {"Content-Type": "text/html"})]}
        for i in range(5):
            routes[f"{HOST}/n/{i}.html"] = [FakeResponse(200, article(str(i), f"本文{i}"), {"Content-Type": "text/html"})]
        crawler, _, _, _ = make_crawler(routes)
        stats = crawler.crawl([TargetConfig(name="b", url=f"{HOST}/list.html", type="index", max_pages=3)])
        self.assertEqual(stats.updated, 2)                          # 一覧1回＋記事2件で上限
        self.assertEqual(stats.budget_exhausted, ["b"])

    def test_old_database_is_migrated(self):
        import sqlite3
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            conn = sqlite3.connect(path)
            conn.executescript("CREATE TABLE pages (id INTEGER PRIMARY KEY, url TEXT UNIQUE, target_name TEXT,"
                               " etag TEXT, last_modified TEXT, content_hash TEXT, last_status INTEGER,"
                               " last_checked_at TEXT, last_changed_at TEXT, created_at TEXT);")
            conn.close()
            db = Database(path)
            cols = {r["name"] for r in db.conn.execute("PRAGMA table_info(pages)")}
            db.close()
            self.assertIn("links_json", cols)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")
        self.prompts = load_prompts(ROOT / "config" / "prompts.yaml")

    def tearDown(self):
        self.db.close()

    def _doc(self, url: str, profile: str) -> int:
        page = self.db.get_or_create_page(url)
        return self.db.insert_document(page.id, url, 200, "application/pdf", None, None, url, b"%PDF",
                                       "文教委員会 会議録（速報版）", "委員長　ただいまから文教委員会を開会します。" * 10,
                                       output_profile=profile)

    def test_council_profile_makes_two_outputs_with_extra_rules(self):
        doc_id = self._doc(f"{HOST}/k.pdf", "council")
        client = FakeClient({"default": ["本文"]})
        gen = AIGenerator(AIConfig(model="claude-opus-5-5"), client=client, prompts=self.prompts)
        self.assertEqual(gen.pending_documents(self.db), [doc_id])
        results = gen.generate_for_document(self.db, doc_id)
        self.assertEqual([r.output_type for r in results], ["summary", "council_questions"])
        self.assertIn("速報版に基づく", client.requests[0]["system"])
        self.assertEqual(gen.pending_documents(self.db), [])

    def test_none_profile_stores_only(self):
        doc_id = self._doc(f"{HOST}/n.pdf", "none")
        client = FakeClient({"default": ["本文"]})
        gen = AIGenerator(AIConfig(model="claude-opus-5-5"), client=client, prompts=self.prompts)
        self.assertEqual(gen.generate_for_document(self.db, doc_id), [])
        self.assertEqual(client.requests, [])
        self.assertEqual(gen.pending_documents(self.db), [])


class ConfigTests(unittest.TestCase):
    def test_default_settings_load(self):
        s = load_settings(ROOT / "config" / "settings.yaml")
        self.assertTrue(s.crawler.respect_robots_txt)
        self.assertGreaterEqual(s.crawler.request_interval_sec, 1.0)
        self.assertIn("BunkyoCrawler/", s.crawler.user_agent)
        self.assertEqual(s.ai.api_key_env, "ANTHROPIC_API_KEY")
        names = [t.name for t in s.targets]
        self.assertIn("bunkyo_shinchaku_rss", names)
        self.assertIn("kugikai_shinchaku", names)
        prompts = load_prompts(s.ai.prompts_file)
        for t in s.targets:
            self.assertIn(t.output_profile, prompts["profiles"])


if __name__ == "__main__":
    unittest.main()
