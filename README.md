# bunkyo_crawler

行政が公開しているWebページ・資料を**相手先サーバーに負荷をかけない方法で**定期収集し、
更新があった資料をAI（Anthropic Claude API）で解析して、発信用の文章と議会質疑の論点整理を
自動生成するシステムです。取得した生データとAI生成テキストは、ローカルのSQLiteデータベースで
紐付けて一元管理します。

> **AIの生成物はすべて「下書き」です。** 公開・提出の前に、必ず人が出典資料と照合して確認してください。
> 生成物はDB上で `review_status = unreviewed`（未レビュー）として保存されます。

---

## 1. システムの目的

| 目的 | 内容 |
|---|---|
| 情報収集の自動化 | 区の公式サイト等の更新を自動で検知し、更新分だけを取得・保存する |
| 相手先への配慮 | 待機時間・差分取得・robots.txt遵守・適切なUser-Agent・バックオフにより、行政サーバーへの負荷を最小化する |
| 発信・調査の効率化 | 1つの資料から、要約・SNS投稿・ブログ記事・質疑ポイントの6種類の下書きを一括生成する |
| 記録の一元管理 | 「いつ・どのURLから・何を取得し・どのプロンプト版で・何を生成したか」をDBで追跡できる |

### 生成される6種類のテキスト

| key | 成果物 | 主な制約（`config/prompts.yaml` で変更可） |
|---|---|---|
| `summary` | ① 全体要約 | 400〜800字、概要／主なポイント／日程・対象／出典 |
| `x_post` | ② X（旧Twitter）用ポスト | **X方式の加重文字数250以内を機械チェック**（URL付与分の余白を確保）、超過時は自動で書き直し |
| `facebook_post` | ③ Facebook用投稿 | 300〜600字、上限900字を機械チェック |
| `line_message` | ④ 公式LINE用配信メッセージ | 200〜400字、上限500字を機械チェック |
| `blog_article` | ⑤ 公式ブログ用記事 | Markdown、1500〜2500字、所感欄は【議員コメント：要記入】 |
| `council_questions` | ⑥ 議会・委員会向け質疑ポイント（論点整理） | 論点ごとに質問案・確認データ・想定答弁（要検証）・再質問 |

文書の種類によって、作る成果物の組を変えています（`config/prompts.yaml` の `profiles`）。

| 組（profile） | 対象 | 作る成果物 |
|---|---|---|
| `full` | 区のお知らせ・報道発表 | 6種類すべて |
| `council` | 会議録・委員会資料・教育委員会資料 | ①要約と⑥論点整理のみ。議員・理事者の氏名は発言者として残す。速報版は【速報版に基づく】と明記 |
| `none` | 保存だけしたいもの | AI生成なし |

---

## 1-2. 収集対象（初期設定）

| 頻度 | 名前 | 内容 | 種類 | 成果物 |
|---|---|---|---|---|
| 毎日 | `bunkyo_shinchaku_rss` | 文京区 新着情報（RSS） | feed | full |
| 毎日 | `bunkyo_chuumoku_rss` | 文京区 注目情報（RSS） | feed | full |
| 毎日 | `kugikai_shinchaku` | 文京区議会 新着情報 | index | council |
| 週1回 | `houdou` | 報道発表資料（令和8年以降） | index | full |
| 週1回 | `kugikai_iinkai_shiryo` | 区議会 委員会資料（令和8年以降） | index | council |
| 週1回 | `kyouikuiinkai` | 教育委員会 定例会等資料（令和8年以降） | index | council |

- 毎日の3つで新しい情報を拾い、週1回の3つは「拾い漏れがないかの念のための確認」です。
- 一度取得した記事は、次回以降は取りに行きません（RSSの日付が新しくなった場合を除く）。
- アドレスに含まれる年（`r08` = 令和8年）は「令和8年以降」に一致する書き方にしてあり、年が替わっても設定の変更は不要です。
- **未確認の点**：`houdou` と `kyouikuiinkai` の入口ページのアドレス、`kugikai_iinkai_shiryo` の入口ページから各委員会のページへリンクがあるかは、まだ実際のサイトで確認していません。初回実行時にエラーや取得0件になった場合は、`settings.yaml` のアドレスを修正してください。
- 会議録（速報版）の一覧ページが分かれば、`settings.yaml` の末尾にある例のとおり追加できます。

---

## 2. ディレクトリ構成

```
bunkyo_crawler/
├── README.md                  # このファイル
├── pyproject.toml             # パッケージ定義（pip install -e . で導入可能）
├── requirements.txt           # 依存ライブラリ
├── .env.example               # 環境変数のひな形（APIキーは .env に記入、Git管理外）
├── .gitignore
├── config/
│   ├── settings.yaml          # クローラー・収集対象・DB・AIモデル等の動作設定
│   └── prompts.yaml           # AIのシステムプロンプトと6種類の生成指示（コード外で管理）
├── src/bunkyo_crawler/
│   ├── __init__.py
│   ├── __main__.py            # python -m bunkyo_crawler のエントリポイント
│   ├── cli.py                 # コマンドライン（crawl / generate / run / list / show / review）
│   ├── config.py              # YAML設定の読み込みと検証
│   ├── fetcher.py             # 負荷軽減型HTTP取得（待機・リトライ・バックオフ・サイズ上限）
│   ├── robots.py              # robots.txt の取得・解釈・遵守（Crawl-delay対応）
│   ├── feeds.py               # RSS 1.0 / 2.0 / Atom の読み取り
│   ├── crawler.py             # RSS・一覧ページ・単独ページの収集、差分取得とDB保存
│   ├── extractor.py           # HTML / テキスト / PDF からの本文抽出
│   ├── ai_pipeline.py         # AIによる6種類のテキスト生成と制約チェック
│   └── database.py            # SQLiteスキーマと保存処理
├── data/                      # SQLiteデータベースの保存先（中身はGit管理外）
├── logs/                      # ログ出力先（中身はGit管理外）
└── tests/
    └── test_system.py         # ネットワーク・API不要の単体テスト
```

---

## 3. セットアップ

### 3.1 前提

- Python 3.10 以上
- Anthropic API キー（AI生成を行う場合のみ）

### 3.2 インストール

```bash
git clone <このリポジトリのURL> bunkyo_crawler
cd bunkyo_crawler
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e .                   # または pip install -r requirements.txt
```

### 3.3 APIキーの設定（環境変数）

APIキーは**設定ファイルやコードには書かず、環境変数から読み込みます**（変数名は `settings.yaml` の `ai.api_key_env`、既定は `ANTHROPIC_API_KEY`）。

```bash
cp .env.example .env               # .env は .gitignore 済み
# .env を編集して ANTHROPIC_API_KEY=sk-ant-... を記入
set -a; source .env; set +a        # 現在のシェルに読み込む
```

### 3.4 動作設定（`config/settings.yaml`）

運用開始前に、最低限以下を確認・変更してください。

1. **`crawler.contact`** — User-Agent に記載する連絡先（運用者の連絡先ページURL等）。相手先の管理者が問い合わせできるようにするためのものです。
2. **`targets`** — 収集対象。初期設定は「1-2. 収集対象」のとおりです。未確認のアドレスがあるため、初回実行の結果を見て必要なら修正してください。
3. **`ai.model` / `ai.effort`** — 使用するモデルと推論の深さ（既定: `claude-opus-5-5` / `medium`）。

### 3.5 データベースの初期化

```bash
python -m bunkyo_crawler init-db
```

（他のコマンドを実行した際にも自動で作成されます）

---

## 4. 使い方

インストール後は `python -m bunkyo_crawler <コマンド>` または `bunkyo-crawler <コマンド>` で実行します。

| コマンド | 内容 |
|---|---|
| `crawl` | 有効な対象を収集（週1回の対象は、前回から7日経っているときだけ） |
| `crawl --ignore-interval` | 週1回の対象も含め、すべてを今すぐ収集 |
| `crawl --target NAME` | `settings.yaml` の特定の対象だけを今すぐ収集 |
| `crawl --url URL` | 単発でURLを指定して収集 |
| `generate` | 成果物が揃っていない文書すべてについてAI生成（文書の種類に応じて6種類または2種類） |
| `generate --document-id N` | 特定の文書についてAI生成 |
| `generate --document-id N --types x_post line_message` | 種類を絞って生成 |
| `generate --document-id N --force` | 生成済みでも作り直す |
| `run` | 収集 → 新たに保存された文書だけAI生成（定期実行向け）。1回のAI生成は `ai.max_documents_per_run`（既定30件）まで |
| `run --no-ai` | 収集のみ（`crawl` と同じ） |
| `list` | 保存済み文書の一覧（Markdown表） |
| `show --document-id N` | 文書と生成物をMarkdownで表示 |
| `show --document-id N --output exports/doc1.md --with-text` | 原文も含めてファイルに書き出し |
| `review --generation-id N --status approved` | 生成物のレビュー状態を更新（`unreviewed` / `approved` / `rejected`） |

共通オプション: `--settings PATH`（別の設定ファイル）、`-v`（詳細ログ）。

### 典型的な流れ

```bash
python -m bunkyo_crawler run                       # 収集と生成
python -m bunkyo_crawler list                      # 一覧で document_id を確認
python -m bunkyo_crawler show --document-id 3 --output exports/doc3.md
# → exports/doc3.md を開き、出典と照合して確認・修正
python -m bunkyo_crawler review --generation-id 15 --status approved
```

### 初回実行について

初回は令和8年分の報道発表・委員会資料などをまとめて取得するため、件数が多くなります。
1回のアクセス数は対象ごとの `max_pages` で、AI生成は `max_documents_per_run` で上限を設けており、
残りは次回以降に少しずつ処理されます。費用を確認しながら進めたい場合は、初回は
`run --no-ai` で収集だけ行い、`list` で件数を確認してから `generate` を実行してください。

### 定期実行の例（cron）

`run` を1日1回実行すれば、毎日の対象と週1回の対象が自動で振り分けられます。
アクセス頻度は相手先への配慮から**1日1〜数回程度**を推奨します。

```cron
# 毎日 7:10 に実行（APIキーは .env から読み込み）
10 7 * * * cd /path/to/bunkyo_crawler && set -a && . ./.env && set +a && .venv/bin/python -m bunkyo_crawler run >> logs/cron.log 2>&1
```

---

## 5. 負荷軽減の仕組み（クローラー仕様）

| 要件 | 実装 |
|---|---|
| リクエスト間の待機 | 同一ホストへのリクエストは `request_interval_sec`（既定5秒、1秒未満は設定エラー）＋ランダム揺らぎ（`interval_jitter_sec`）以上の間隔を空ける。robots.txt の取得もこの間隔の対象 |
| 更新差分のみ取得 | 前回の `ETag` / `Last-Modified` を `If-None-Match` / `If-Modified-Since` として送信し、**304なら本文を受信しない**。さらに200でも本文のSHA-256が前回と同じなら新しい版を保存しない |
| robots.txt の遵守 | ホストごとに robots.txt を取得・解釈し、禁止パスは取得しない。リダイレクト先も都度確認。`Crawl-delay` / `Request-rate` が設定より長ければそちらに従う。robots.txt は24時間キャッシュ |
| robots.txt 取得不能時 | 401/403 → 全体禁止、その他4xx → 存在しないものとして許可、5xx・通信失敗 → **そのホストへのアクセスを見送り**エラーとして記録（10分後に再確認） |
| User-Agent | `BunkyoCrawler/<版> (+<連絡先>; ...)` を全リクエストに付与。連絡先未設定は設定エラー |
| 指数関数的バックオフ | 429・5xx・接続エラー・タイムアウト時に `backoff_base_sec × 2^n`＋揺らぎ（上限 `backoff_max_sec`）待って再試行。`Retry-After` ヘッダーがあればそれ以上待つ。上限を超える `Retry-After` やリトライ回数超過で中止 |
| RSS・一覧ページ | RSS（更新情報の配信ファイル）や一覧ページは文書として保存せず、リンク集めにだけ使う。304のときは前回保存したリンク一覧を使う。記事は**未取得のものだけ**取りに行く |
| 実行間隔 | 対象ごとの `interval_days` で、週1回などの頻度を制御 |
| その他 | 取得サイズ上限（既定10MB）、対象Content-Typeの限定、1回のアクセス数上限（`max_pages`）、同一ドメインのみ、本文領域内のリンクのみ（サイト共通メニューは無視） |

`respect_robots_txt` を `false` にすると設定エラーになり、無効化できない設計です。

---

## 6. AI生成パイプライン

- **プロンプトは `config/prompts.yaml` で一元管理**しています（Pythonコードにはプロンプト文を持ちません）。
  - `system_prompt`：共通の役割と厳守事項（出典にない事実を補わない、不明点は【要確認】、議員の意見を創作しない、公職選挙法への配慮 等）
  - `source_template`：出典資料をAIに渡す書式
  - `revision_template`：文字数超過時の書き直し依頼文
  - `outputs`：6種類それぞれの生成指示・出力上限・制約
- 文言を変えたら **`version` を更新**してください。生成物は「文書 × 種類 × プロンプト版」単位で保存されるため、新旧の比較ができます。
- 指示文中で `{` `}` を文字として使う場合は `{{` `}}` と書いてください（置換記号と区別するため）。
- 同じ出典資料を6回使うため、資料部分に**プロンプトキャッシュ**を指定し、費用と時間を抑えています。
- 長文出力でも通信が切れないよう、ストリーミングで受信しています。
- `ai.use_refusal_fallback: true` の場合、モデルの安全判定で生成が辞退されたときにAPI側で推奨の代替モデルへ自動で切り替えます（ベータ機能）。それでも辞退された場合は `status = refused` として記録します。
- 本文が `min_input_chars` 未満・`max_input_chars` 超の文書は生成せず `skipped` として理由を記録します（長文を黙って切り詰めることはしません）。

---

## 7. データベース（SQLite）

保存先は既定で `data/bunkyo_crawler.db` です。

```
pages ─┬─< documents ─┬─< generations
       │              │
       │  (URLごとの状態)   (取得した版ごとの生データ)   (版ごと・種類ごとのAI生成テキスト)
fetch_log（全アクセスの記録：updated / listing / not_modified / unchanged / disallowed / error / skipped）
target_runs（収集対象ごとの最終実行日時。週1回などの判定に使う）
```

| テーブル | 主な列 |
|---|---|
| `pages` | `url`, `etag`, `last_modified`, `content_hash`, `last_checked_at`, `last_changed_at`, `links_json`（一覧ページのリンク） |
| `documents` | `page_id`, `url`, `fetched_at`, `content_type`, `raw_content`（生データBLOB）, `title`, `extracted_text`, `output_profile` |
| `generations` | `document_id`, `output_type`, `prompt_version`, `model`, `status`, `review_status`, `content`, `error`, トークン使用量 |
| `fetch_log` | `url`, `requested_at`, `result`, `http_status`, `attempts`, `note` |

SQLで直接参照する例:

```bash
sqlite3 data/bunkyo_crawler.db \
  "SELECT d.id, d.title, g.output_label, g.status, g.review_status
     FROM generations g JOIN documents d ON d.id = g.document_id
    ORDER BY d.id DESC, g.id;"
```

---

## 8. テスト

ネットワークやAPIを使わずに、robots.txt遵守・待機・差分取得・バックオフ・RSS（RSS 1.0/2.0/Atom、Shift_JIS含む）・一覧ページの巡回・週1回の間隔制御・成果物の組・文字数チェックを検証します。

```bash
python -m unittest discover -s tests -v
```

---

## 9. 運用上の注意

- **公開前の確認**：生成物は下書きです。事実関係・数値・日付を必ず出典と照合し、【要確認】【議員コメント：要記入】を解消してから使用してください。
- **選挙関連**：選挙期間中のSNS・ブログ・LINE配信は公職選挙法の規制を受けます。プロンプトでも配慮していますが、最終判断は必ず人が行ってください。
- **秘密情報**：APIキーは環境変数（`.env`）で管理し、コミットしないでください。`data/` と `logs/` の中身もGit管理外です。
- **アクセス頻度**：待機時間を短くしたり、実行頻度を過度に上げたりしないでください。相手先から問い合わせがあった場合に備え、`crawler.contact` は必ず実在の連絡先にしてください。
- **利用規約**：収集対象サイトの利用規約・著作権表示を確認し、転載範囲に注意してください。
