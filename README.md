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

### macOS LaunchAgent（常駐・自動起動）

ログイン時に自動起動し、クラッシュ時に自動再起動する設定が
`~/Library/LaunchAgents/com.masato.cairn.plist` に登録済み。

```bash
# 状態確認
launchctl list | grep com.masato.cairn   # PIDが表示されれば稼働中

# 手動操作
launchctl stop  com.masato.cairn
launchctl start com.masato.cairn

# plist を編集した後の再読み込み
launchctl unload ~/Library/LaunchAgents/com.masato.cairn.plist
launchctl load   ~/Library/LaunchAgents/com.masato.cairn.plist
```

ログは `~/Library/Logs/cairn/server.log` / `server-error.log` に出力される。

### ollama（Phase 3 知識抽出用 LLM）

Phase 3 の segment 要約 / assertion 抽出に `ollama` を使用する。
`~/Library/LaunchAgents/com.masato.ollama.plist` で常駐起動済み（`127.0.0.1:11434`）。
モデルは idle 5 分で自動アンロードされるため常時メモリ占有はしない。

```bash
# 状態確認
launchctl list | grep com.masato.ollama

# 使用モデル（初回のみ pull が必要）
ollama pull qwen2.5:14b-instruct-q4_K_M   # segment summary 用（~8GB、24 tok/s）
ollama pull qwen2.5:32b-instruct-q4_K_M   # assertion 抽出用（~18GB、11 tok/s）
```

ログは `~/Library/Logs/cairn/ollama.log` / `ollama-error.log` に出力される。

### 手動起動（開発時）

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

Cairn は読み取り専用の横断MCPサーバー（STDIO）を同梱しています。claude CLI / codex /
Claude Desktop に登録すると、エージェントが AI 会話・Karakeep・Zotero・Obsidian を
横断して「過去に何を調べてどう結論したか」「あるテーマの構想・根拠・議論」を
自力で参照できます。ツールは4つ、すべて読み取り専用です（DESIGN.md §5.6）:

- `search_all` — 4系統横断のキーワード/意味/ハイブリッド検索（kind・ソース・期間
  絞り込み、最大10件/回）。各結果に kind/source/url/provenance
- `get_item` — `(source, external_id)` で1件を取得（会話はフルスレッドを約8,000字ずつ、
  続きは `start_message` で。外部 item は索引メタ+原本URL+本文）
- `build_context_pack` — テーマ横断パック。構想（会話）/ 根拠（Zotero・Karakeep・
  強い一致リンク）/ 過去の議論（再浮上）を provenance 付きで構成。`synthesize=true` で
  ローカル ollama による未解決課題込みの草案（`generated_by:` ラベル付き。既定は付けない）
- `get_recent_activity` — 直近N日の横断アクティビティ要約（新セッションの立ち上げ用）

応答テキストは untrusted データとして区切り（`<<<CAIRN_ARCHIVE_DATA …>>>`）で
囲まれます。`build_context_pack` は原文引用（`content`）と Cairn 生成の合成
（`synthesized`）を構造的に分離します（DESIGN.md §6.2）。

Web UIサーバーの起動は不要です（MCPサーバーが直接DBを読みます）。登録には
**パッケージではなくランチャ `backend/run_mcp.py` の絶対パス**を指定してください
（`import mcp` を SDK に解決させるため。詳細は run_mcp.py の docstring）。

### claude CLI

```bash
claude mcp add cairn -s user -- \
  /path/to/cairn/backend/.venv/bin/python /path/to/cairn/backend/run_mcp.py
claude mcp list   # cairn: ✔ Connected と出ればOK
```

> 旧バージョン（`app/mcp_server.py`、3ツール）を登録済みの場合は
> `claude mcp remove cairn` してから上記で登録し直してください。

### codex CLI（手順のみ・未検証）

`~/.codex/config.toml` に追記:

```toml
[mcp_servers.cairn]
command = "/path/to/cairn/backend/.venv/bin/python"
args = ["/path/to/cairn/backend/run_mcp.py"]
```

### Claude Desktop（手順のみ・未検証）

Settings → Developer → Edit Config で `claude_desktop_config.json` に追記:

```json
{
  "mcpServers": {
    "cairn": {
      "command": "/path/to/cairn/backend/.venv/bin/python",
      "args": ["/path/to/cairn/backend/run_mcp.py"]
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
.venv/bin/python -m app.admin import-runs    # 取り込み履歴（件数・warning・成否）を表示
.venv/bin/python -m app.admin integrity-check # DB整合性検査（読み取り専用、問題ありで exit 2）
.venv/bin/python -m app.admin backup         # DBの一貫したコピーを作成（0600）
```

バックアップは `<db>.backup-<日時>`（`--out PATH` で変更可）に作られ、平文を含むため
`0600` に制限されます。復元はそのファイルを戻すか `CAIRN_DB` をそれに向けます。

## 取り込み履歴

取り込み（アップロード／CLI同期）は1入力ごとに `import_runs` テーブルへ記録されます
（source / 入力名 / 日時 / parser version / inserted・updated・skipped 件数 /
warning 概要 / content hash / 成否）。

- 管理CLI: `python -m app.admin import-runs [--limit N] [--source upload|claude_cli|codex_cli]`
- API: `GET /api/import-runs?limit=&offset=&source=`

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

## `cairn` CLI（M0 骨格）

`backend/bin/cairn` は typer 製の CLI ラッパーで、統合層のサブコマンドを 1 本にまとめる
入口。M0 では骨格のみで、`sync conversations`（既存 CLI ログ同期）だけが実装済み。
残り（`sync karakeep|zotero|obsidian|all`、`review weekly`、`index rebuild`）は
DESIGN.md §7 の M1〜M4 で順次実装され、それまでは実装先マイルストーンを添えて
exit 1 で失敗する。

```bash
# PATH に通すには（推奨）:
ln -s /abs/path/to/cairn/backend/bin/cairn ~/bin/cairn
cairn --help

# 直接叩く場合:
/path/to/cairn/backend/bin/cairn sync conversations
```

editable install（`pip install -e .` で `cairn` を PATH に登録）は M6 で検討する。

## M0 スキーマ適用手順（実 DB を v10 → v11 に上げる）

M0 で `_SCHEMA_VERSION` が 10 → 11 になり、`items` / `item_links` / `sync_state` の
追加と、既存 conversations の items バックフィルが走る。運用中の `cairn.db` に
適用する手順:

1. **稼働中サービスを止める**（旧コードが新スキーマの DB を開くのは additive なので
   安全だが、順序として停止 → migrate が確実）:

   ```bash
   launchctl stop com.masato.cairn
   ```

2. **実 DB のコピーへドライラン**（migration 自体が自動 backup を取るが、
   その前に安全側のコピーを作る）:

   ```bash
   cd backend
   .venv/bin/python -m app.admin backup   # cairn.db.backup-<日時>
   CAIRN_DB=data/cairn.db.backup-XXXXXX .venv/bin/python -m app.admin integrity-check
   # → v11 migration がコピー上で成功し、items 件数 == conversations 件数、
   #   chunks_missing_item_id=0、ok:true を確認
   ```

3. **本 DB へ適用**（`connect()` が v11 migration を走らせ、premigrate バックアップも
   自動で作る）:

   ```bash
   .venv/bin/python -m app.admin integrity-check
   # 期待: ok:true / conversations_missing_item=0 / chunks_missing_item_id=0
   ```

4. **サービス再起動**:

   ```bash
   launchctl start com.masato.cairn
   ```

5. **クリーンアップ**: `data/cairn.db.premigrate-v10-to-v11-*` と手動 backup は
   会話本文を平文で含む。動作確認後に削除する。

## テスト

```bash
cd backend
.venv/bin/python -m pytest tests/ -v
```
