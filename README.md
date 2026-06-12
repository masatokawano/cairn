# Cairn

AI会話アーカイブ・横断検索アプリ。ChatGPT / Claude / Gemini のエクスポートと、
claude CLI / codex CLI のローカルログを1つのSQLiteに取り込み、ブラウザから
キーワードで横断検索してフルスレッドを閲覧できる。**すべてローカル完結**。

## セットアップ

要件: Python 3.11+ / Node.js 18+

```bash
# バックエンド
cd backend
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# フロントエンド（ビルドして FastAPI から配信）
cd ../frontend
npm install
npm run build
```

## 起動

```bash
cd backend
.venv/bin/uvicorn app.main:app --port 8730
```

ブラウザで http://127.0.0.1:8730 を開く。

- 起動時と60秒ごとに claude CLI / codex CLI のログを自動同期する
- DBは `backend/data/cairn.db`（消せば全データを再構築できる）

開発時（フロントエンドのホットリロードが欲しい場合）は別ターミナルで
`cd frontend && npm run dev` を実行し http://localhost:5173 を開く
（`/api` は8730へプロキシされる）。

## データの取り込み

### チャット系（手動エクスポート → 画面にドロップ）

ZIPのまま、または中のJSONファイルを、Cairnの画面にドラッグ＆ドロップする
（「エクスポートを取り込む」ボタンでも可）。再ドロップ時は差分のみ取り込む
（新規追加・変更された会話だけ反映、変更なしはスキップ）。

#### ChatGPT

1. ChatGPT → Settings → Data controls → Export data
2. メールで届くZIPをダウンロード
3. ZIPごとCairnにドロップ（中身は `conversations.json`）

#### Claude

1. claude.ai → Settings → Privacy → Export data
2. メールで届くZIPをダウンロード
3. ZIPごとCairnにドロップ（中身は `conversations.json`）

#### Gemini

1. [Google Takeout](https://takeout.google.com/) を開く
2. 「選択をすべて解除」してから **「マイ アクティビティ」(My Activity) のみ選択**
   - ⚠️ **罠**: 一覧にある「Gemini」を選ぶと出力されるのはGems（カスタムボット定義）
     であり、会話履歴ではない。会話履歴は「マイ アクティビティ」に入っている
3. 「マイ アクティビティ」の「すべてのアクティビティ データが含まれます」を押し、
   **「Gemini Apps」だけにチェック**を絞ると軽くなる
4. 「複数の形式」ボタンで形式を **JSON に変更**（デフォルトはHTMLで取り込めない）
5. エクスポートされたZIP（中身は `MyActivity.json`）をCairnにドロップ

注意: My Activityには会話のスレッド構造がなく、レコードは基本プロンプトのみ。
1レコード＝1会話として取り込まれる。

### CLI系（自動同期）

設定不要。以下のパスを起動時と60秒間隔で監視し、変更されたファイルだけ再取り込みする。

- claude CLI: `~/.claude/projects/**/*.jsonl`
- codex CLI: `~/.codex/sessions/**/*.jsonl`

「CLIログ同期」ボタンで即時同期もできる。

## 検索

- スペース区切りで複数キーワードのAND検索
- 日本語・英語とも部分一致（FTS5 trigram。2文字以下のクエリはLIKEフォールバック）
- ソースのチップ（ChatGPT / Claude / …）で絞り込み
- ヒットした会話をクリックするとフルスレッドを表示

## 設定（環境変数）

| 変数 | デフォルト | 説明 |
|---|---|---|
| `CAIRN_DB` | `backend/data/cairn.db` | SQLite DBパス |
| `CAIRN_CLAUDE_DIR` | `~/.claude/projects` | claude CLIログのルート |
| `CAIRN_CODEX_DIR` | `~/.codex/sessions` | codex CLIログのルート |
| `CAIRN_SYNC_INTERVAL` | `60` | CLI同期間隔（秒） |

## テスト

```bash
cd backend
.venv/bin/python -m pytest tests/ -v
```
