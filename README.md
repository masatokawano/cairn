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

## MCPサーバー（エージェントからアーカイブを参照する）

Cairn は読み取り専用のMCPサーバー（STDIO）を同梱しています。claude CLI / codex /
Claude Desktop に登録すると、エージェントが「過去に何を調べてどう結論したか」を
アーカイブから自力で検索できます。ツールは3つ、すべて読み取り専用です:

- `search_conversations` — キーワード横断検索（ソース・期間絞り込み、最大10件/回）
- `get_conversation` — 会話IDでフルスレッド取得（約8,000字ずつ、続きは `start_message` で）
- `list_recent_conversations` — 直近N日の会話一覧

Web UIサーバーの起動は不要です（MCPサーバーが直接DBを読みます）。

### claude CLI（検証済み）

```bash
claude mcp add cairn -s user -- \
  /path/to/cairn/backend/.venv/bin/python /path/to/cairn/backend/app/mcp_server.py
claude mcp list   # cairn: ✔ Connected と出ればOK
```

### codex CLI（手順のみ・未検証）

`~/.codex/config.toml` に追記:

```toml
[mcp_servers.cairn]
command = "/path/to/cairn/backend/.venv/bin/python"
args = ["/path/to/cairn/backend/app/mcp_server.py"]
```

### Claude Desktop（手順のみ・未検証）

Settings → Developer → Edit Config で `claude_desktop_config.json` に追記:

```json
{
  "mcpServers": {
    "cairn": {
      "command": "/path/to/cairn/backend/.venv/bin/python",
      "args": ["/path/to/cairn/backend/app/mcp_server.py"]
    }
  }
}
```

## シークレット除去

取り込み時にメッセージ本文へ自動でシークレット除去を適用します（OpenAI / Anthropic /
AWS / GitHub の各キー形式と PEM 秘密鍵ブロック → `[REDACTED:provider]` に置換）。

既存DBにまとめて適用する管理コマンド（MCPとは分離した管理用CLI）:

```bash
cd backend
.venv/bin/python -m app.admin redact-scan    # dry-run: provider別の検出件数を表示
.venv/bin/python -m app.admin redact-apply   # バックアップ作成→除去→FTS再構築→検証
.venv/bin/python -m app.admin force-resync   # 全CLIログの強制再取り込み
```

`redact-apply` が作るバックアップ（`cairn.db.backup-*`）には**平文が残る**ため、
確認後に削除してください。

⚠️ **残存リスク**: 除去対象は Cairn のDB・検索・表示・MCP出力のみです。
**元の claude CLI / codex CLI のログファイル（`~/.claude/projects/` /
`~/.codex/sessions/`）にはシークレットが平文のまま残ります。**
キーが漏れた場合はローテーションが唯一の確実な対処です。

## 設定（環境変数）

| 変数 | デフォルト | 説明 |
|---|---|---|
| `CAIRN_DB` | `backend/data/cairn.db` | SQLite DBパス |
| `CAIRN_CLAUDE_DIR` | `~/.claude/projects` | claude CLIログのルート |
| `CAIRN_CODEX_DIR` | `~/.codex/sessions` | codex CLIログのルート |
| `CAIRN_SYNC_INTERVAL` | `60` | CLI同期間隔（秒） |
| `CAIRN_ALLOW_HOSTS` | （空） | localhost以外に許可するHost名（カンマ区切り、検証用） |
| `CAIRN_MAX_UPLOAD_MB` | `500` | アップロード上限 |
| `CAIRN_MAX_JSON_MB` | `500` | ZIP内JSONの展開サイズ上限 |
| `CAIRN_MAX_ZIP_ENTRIES` | `10000` | ZIP内ファイル数上限 |

## セキュリティ

Cairn は会話本文を平文の SQLite DB に保存するローカルアプリです。通常起動は
`127.0.0.1` に限定し、LAN やインターネットへ公開しないでください。
Host/Origin が localhost 以外のリクエストはミドルウェアが拒否します。

- DBファイルは接続時に自動で `0600` になります。既存環境では
  `ls -l backend/data/` で `-rw-------` であることを確認してください
  （古い場合は `chmod 600 backend/data/cairn.db*`）
- 再現可能インストール: `pip install -r backend/requirements.lock`
- 脆弱性監査: `uvx pip-audit -r backend/requirements.lock --no-deps --disable-pip` / `npm audit`

詳細は [SECURITY.md](SECURITY.md) を参照してください。

## テスト

```bash
cd backend
.venv/bin/python -m pytest tests/ -v
```
