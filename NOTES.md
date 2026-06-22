# NOTES — フォーマットの癖・ハマりどころ

作業中に学んだことの記録。以降のセッションはまずここを読むこと。

## SQLite FTS5

- 日本語検索のためトークナイザは `trigram` を採用。unicode61はCJKを単語分割
  できないため不可。
- **trigramは3文字未満のクエリにマッチしない**。`db.search()` は全タームが
  3文字以上のときだけFTSを使い、それ以外は `LIKE '%q%'` にフォールバック
  （個人アーカイブ規模なら十分速い）。
- **`snippet()` / `bm25()` はウィンドウ関数と同居できない**
  （`unable to use function snippet in the requested context`）。
  FTSテーブルへのMATCHはサブクエリに閉じ込め、外側でJOIN・ウィンドウ関数を使う。
- external content table (`content='messages'`) を使う場合、削除は
  `INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', ...)`
  形式。トリガで自動化済み。
- **`SELECT COUNT(*) FROM messages_fts` は content table (messages) に proxy する**
  ため、FTS index の desync 検知には使えない（常に messages 件数と一致する）。
  実際の indexed 件数は shadow table **`messages_fts_docsize`** を数える
  （`db.integrity_check()` はこれで件数整合を見る）。
- FTS5 の構造検査は `INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')`。
  ただし INSERT 形のため sqlite3 が暗黙トランザクションを開く → 変更はないが
  後続の `PRAGMA foreign_keys` 等が効かなくなるので、実行後に `conn.rollback()`
  で閉じること（integrity_check 実装でハマった）。

## claude CLI ログ (`~/.claude/projects/<エスケープ済cwd>/<sessionId>.jsonl`)

- 1ファイル＝1セッション。`type` が `user`/`assistant` の行だけが会話。
  他に `mode`, `permission-mode`, `file-history-snapshot`, `attachment`,
  `summary` などのノイズ行が大量にある。
- `isSidechain: true` はサブエージェントのやり取り → 除外。
- `message.content` は文字列 or ブロック配列の2形態。ブロックは `text` のみ
  採用（`thinking` / `tool_use` / `tool_result` は除外）。
- userメッセージに混ざるノイズ: `<command-name>`, `<command-message>`,
  `<local-command-stdout>`, `<system-reminder>`, `Caveat: The messages below`
  で始まるものはスラッシュコマンドのラッパーや注入テキスト → 除外。
- ただし「The `/xxx` skill is loaded...」のようにラッパーなしで注入される
  テキストは区別できず残る（既知の限界、実害小）。
- `type: "summary"` 行の `summary` がセッションのタイトルに使える
  （ないファイルも多い。その場合は最初のuserメッセージから生成）。

## codex CLI ログ (`~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`)

- 1ファイル＝1セッション。`session_meta` 行に id / cwd / timestamp。
- 会話は `type: "response_item"` かつ `payload.type: "message"` の行。
  `payload.role` が `user`/`assistant` のみ採用（`developer` はシステム指示）。
- content ブロックは `input_text` / `output_text`。
- userメッセージのノイズ: `<permissions instructions>`, `<environment_context>`,
  `<user_instructions>`, `# AGENTS.md instructions` で始まるブロック → 除外。
- **IDE拡張経由の入力は `# Context from my IDE setup:` ラッパーに包まれ、
  実際の質問は `## My request for Codex:` の後にある** → アンラップして抽出。
- `~/.codex/` 直下には sqlite や history.jsonl など他ファイルが多数あるが、
  会話ログは `sessions/` 以下のみ。

## ChatGPT エクスポート (conversations.json)

- 会話はノードグラフ (`mapping`)。順序復元は `current_node` から `parent` を
  遡るのが正攻法（編集による分岐があるため全ノード走査だと重複する）。
  チェーンが壊れていたら create_time ソートにフォールバック。
- `create_time` はUNIXエポック秒（float）。ISO文字列に変換して保存。
- `content.content_type` は `text` 以外に `code`, `multimodal_text`,
  `user_editable_context`（カスタム指示 → 除外）などがある。
- ※実エクスポートでの検証はまだ。実ファイルを入手したら要確認。

## Claude エクスポート (conversations.json)

- `chat_messages[].sender` は `human`/`assistant`。
- 本文は新形式 `content` ブロック配列を優先、なければ旧形式 `text`。
- ※実エクスポートでの検証はまだ。

## Gemini (Takeout My Activity)

- **Takeoutで「Gemini」を選ぶとGems定義が出力される罠**（READMEに明記済み）。
  会話履歴は「マイ アクティビティ」→ Gemini Apps。
- デフォルト出力はHTML。JSONを選ばないと取り込めない（ZIP内にHTMLしか
  なければその旨のエラーメッセージを出すようにしてある）。
- スレッド構造なし。1レコード＝1会話として取り込む。応答は原則含まれない
  （`subtitles` 等にあれば拾う）。
- タイトルはロケール依存（"Prompted X" / 「X と入力しました」）。
  プレフィックス除去はベストエフォート。
- ※実エクスポートでの検証はまだ。実ファイル入手後に要調整。

## アーキテクチャのメモ

- 差分インポートは会話単位の content_hash 比較。一致→skip、不一致→
  メッセージ全削除して再挿入（メッセージ単位のマージはしない。シンプル優先）。
- CLI同期はファイルの (mtime, size) を `ingest_files` に記録して変更検知。
  watchdogではなくポーリング（60秒、`CAIRN_SYNC_INTERVAL`で変更可）。
- uvicornはスレッドプールでハンドラを動かすため、SQLite接続は
  threading.local で1スレッド1接続。
- MCPブラウザ(Docker内)から動作確認するときは `host.docker.internal` を使い、
  サーバーを一時的に `--host 0.0.0.0` で起動する必要がある（通常は127.0.0.1）。

## スキーマ migration（P1-A / `db.py`）

- バージョンは `PRAGMA user_version`。2つの仕組みを併用する:
  - `_SCHEMA` … 常に最新形。冪等（IF NOT EXISTS）。**新規DBはこれで作り
    user_version を直接スタンプ。migration は走らせない**。
  - `_MIGRATIONS` … 既存DBを上げる `(version, sql)` の順序付きリスト
    （ALTER TABLE ADD COLUMN や backfill など IF NOT EXISTS で直せないもの）。
    ステップ追加時は `_SCHEMA_VERSION` も同時に上げる。
- **新規DBと既存DBの判定は `connect()` 内で「conversations テーブルが既に
  存在するか」で行う**。両者とも初期 user_version は 0 で区別できないため、
  テーブル有無で見る。これで新規DBに古い migration を誤適用しない。
- **migration 実行時は事前にDBを自動バックアップ**
  （`cairn.db.premigrate-v{from}-to-v{to}-{stamp}`、0600）。
  バックアップには平文が残るので、migration 確認後に削除すること（復旧手順）。
- 新しい migration を足すときは: ① `_SCHEMA` を最新形に更新（新規DB用）
  ② `_MIGRATIONS` に既存DB変換SQLを追加 ③ `_SCHEMA_VERSION` を +1
  ④ `tests/test_migrations.py` にダミー以外の回帰を追加。

## Phase 2a: MCPサーバー + シークレット除去（2026-06-12）

- MCP公式SDK（FastMCP）はSTDIOがデフォルト。`@mcp.tool()` はデコレート後も
  元関数をそのまま返すので、ツール関数は普通のPython関数としてユニットテストできる。
- claude CLIへの登録は `-m app.mcp_server` だとcwd依存になるため、**絶対パスの
  スクリプト起動**にし、mcp_server.py冒頭で `__package__` を見てsys.pathを
  ブートストラップする方式にした。
- `claude mcp add cairn -s user -- <python絶対パス> <スクリプト絶対パス>` で登録。
  ツール名は `mcp__cairn__search_conversations` の形になる。
- 除去はdb.upsert_conversations（全取り込み経路の単一チョークポイント）で
  **content_hash計算の前に**適用する。これで「元ログ再パース→取り込み時除去→
  同一ハッシュ→skip」となり、再同期で平文が復活しない。
- タイトルは60字で切り詰められるためキーが途中で切れてフルパターンに一致しない。
  タイトル専用に閾値を短くしたパターン（redact_title）を併用。
- FTS5の更新整合: `AFTER UPDATE OF text` トリガを追加。migrationでは念のため
  `INSERT INTO messages_fts(messages_fts) VALUES('rebuild')` も実行。
- 残留ゼロ化の手順: UPDATE → rebuild → `PRAGMA wal_checkpoint(TRUNCATE)` →
  `VACUUM` → もう一度checkpoint。これでDB本体・WAL・SHMの生バイトから
  平文が消えることをgrepで確認済み（secure_delete=ONも常時設定）。
- pip-auditと同じ理由でuvxの内部venv作成は死ぬ環境がある（SIGABRT）。
- **実Claudeエクスポートで判明**: クラウド版Claudeの会話にもAPIキーが混入していた
  （dry-runで11箇所/2会話を検出: codex_cli 1 + claude 1）。除去は全ソース必須。
- 元のCLIログファイル（~/.claude/projects, ~/.codex/sessions）の平文は対象外
  （READMEに残存リスクとして明記）。漏えいキーのローテーションが唯一確実な対処。

## セキュリティ強化の実装メモ（2026-06-12）

- Host/Origin検証はmiddlewareで実装。**TestClientのデフォルトHostは `testserver`**
  なので、`TestClient(app, base_url="http://127.0.0.1")` にしないと全テストが403になる。
- `TestClient` を `with` なしで使うと startup イベント（バックグラウンド同期スレッド）
  が走らない。APIテストではこれが好都合。
- SQLiteのWAL/SHMはDB本体と同じ権限で作られるため、DB本体をchmod 0600すれば
  以後のsidecarも0600になる。既存sidecarだけ明示的にchmod。
- zip bombはZipInfo.file_size（ヘッダ申告値）を信用せず、`zf.open().read(limit+1)`
  の境界付き読み込みで実展開量を制限する。
- `uvx pip-audit` はデフォルトで内部venvを作ろうとしてこの環境ではSIGABRTで死ぬ。
  **`--no-deps --disable-pip` を付けると完全固定のlockfileに対してvenvなしで監査できる**。
- 検索のDB側ページングは `ROW_NUMBER() OVER (PARTITION BY c.id ORDER BY rank)` で
  会話ごとのベストヒットだけ残して LIMIT/OFFSET。snippet()はFTSサブクエリ内、
  ウィンドウ関数は中間層、と3層に分ける。
- 同時実行制御: ingest_lock（threading.Lock）を cli_sync に置き、バックグラウンド同期・
  /api/sync・/api/import で共有。/api/sync は非ブロッキング取得で実行中なら409。

## セキュリティレビューのメモ

- Cairn は認証なし API なので、`--host 0.0.0.0` 起動は会話本文をLANに露出し得る。
  通常運用では必ず `127.0.0.1` に閉じる。必要なら Host/Origin 検証か bearer token を入れる。
- `/api/import` はアップロード全体とZIP内JSONをメモリに読むため、巨大ファイル/zip bomb
  対策として Content-Length、ZIP総展開サイズ、候補ファイルサイズの上限が必要。
- SQLite DB/WAL/SHM は会話全文を平文で含む。作成時・既存ファイルとも `0600` に寄せる。
- React 側は `dangerouslySetInnerHTML` を使っておらず、本文表示は JSX のテキストノードなので
  取り込みデータ由来XSSのリスクは低い。
- frontend は `npm audit` で既知脆弱性0件を確認済み。backend は `pip check` はOKだが、
  Python脆弱性監査ツールは未導入。
