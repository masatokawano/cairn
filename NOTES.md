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
- **添付は content ブロックではなく message レベル**にある（2026-06-24, 実 export
  517 会話で検証）:
  - `attachments[]`: `{file_name, file_size, file_type, extracted_content}` 形式。
    `extracted_content` は Claude が事前に抽出済みのテキスト本文（数十KB〜数百KB）。
    実 export では 120 件、すべて human 側。
  - `files[]`: `{file_uuid, file_name}` 形式の **UUID 参照のみ**。バイナリ本体は
    export に含まれない（512 件、ほぼ human）。
- パーサーは attachments[] を `ParsedAttachment(source_ref=file_name,
  mime=file_typeから推定, size=file_size, hash=sha256(extracted_content),
  extracted_text=extracted_content)`、files[] を `ParsedAttachment(source_ref=file_uuid)`
  に変換。**bytes が手元に無いため hash は extracted_content のテキストハッシュ**
  （他ソースの bytes hash とは意味が異なる。テキストが変われば updated 検知できる）。
- text 空でも添付があれば message を残す（claude_cli と同方針）。実 export では 75
  メッセージ（添付/UUID のみのターン）がこれで救済される。
- content ブロック内には `image` / `document` は出現しなかった（claude_cli の base64
  document とは別仕様）。block type は `text` / `tool_use` / `tool_result` /
  `thinking` / `token_budget` のみで、text 以外は従来通り捨てる。

## Gemini (Takeout My Activity)

- **Takeoutで「Gemini」を選ぶとGems定義が出力される罠**（READMEに明記済み）。
  会話履歴は「マイ アクティビティ」→ Gemini Apps。
- デフォルト出力はHTML。JSONを選ばないと取り込めない（ZIP内にHTMLしか
  なければその旨のエラーメッセージを出すようにしてある）。
- スレッド構造なし。1レコード＝1会話として取り込む。
- **実 Takeout で検証（2026-06-24）。当初のパーサーは subtitles を assistant 応答に
  していたが、実データの subtitles は「添付ファイル N 件」「画像を N 枚生成しました」
  などのメタ文＋添付説明であり応答ではなかった**。応答本文は `safeHtmlItem[0].html`
  に HTML 形式で入っている（LaTeX は HTML 内テキストとして混ざる）。
  パーサーは `safeHtmlItem` を HTML→text 変換して assistant message にする。
  subtitles は **添付ファイル参照の抽出にのみ使う**。
- タイトルはロケール依存。実 Takeout で見たプレフィックス: `"送信したメッセージ: "`
  （新ロケール）、`"Prompted X"` / 「X と入力しました」（旧）。すべて除去対象。
- `"フィードバックを送信しました: "` で始まるレコードは user feedback ログで
  会話ではないので **除外**。
- **添付参照は 3 系統**:
  - `imageFile`: 単一ファイル名
  - `attachedFiles[]`: ファイル名のリスト（user upload + assistant 生成画像 が混在）
  - `subtitles[].url`: user upload を説明する `{name, url}` ブロック（url が
    ファイル名）
  - **assistant 生成画像は safeHtmlItem 内の `<img src="...">` で識別**。それ以外
    （imageFile + attachedFiles + subtitles{url}）から assistant 候補を除いた残りが
    user 添付。
- **ZIP 同梱画像の hash 化**: parse_upload に `_collect_zip_attachments` を追加し、
  gemini パーサーには `WANTS_ATTACHMENTS=True` フラグで「ZIP コンテキストが欲しい」
  と宣言させる。`parse(data, attachments={filename: bytes})` で受け取り、各
  添付の bytes を sha256 + size で記録（bytes 本体は保存しない）。他パーサーは
  この引数を受け取らない（無視）。
- **拡張子の癖**: subtitles の url が `.jpeg` で書かれているのに ZIP 内の実体は
  `.jpg` というケースがある（Takeout が拡張子を変換することがある）。
  `_make_attachment` は `.jpeg ↔ .jpg` swap でフォールバック検索する。
- **`looks_like` は header キーで判定**するため、Takeout ZIP の「マイ アクティビティ」
  以外の活動（Maps, Search 等）が同梱されていてもパーサー側で除外できるが、
  parse_upload は最初に見つけた `.json` を試すので、ファイル名優先順位を
  `conversations.json` > `MyActivity.json` > `Gemini` を含むパス > その他 にした
  （ロケール依存の `マイアクティビティ.json` を拾うため）。
- **既存 DB への影響**: 修正前後で Gemini 会話の messages 構造が変わるため
  content_hash は必ず変わる。再 sync で全 Gemini 会話が "updated" 扱いになる
  （修正前のデータがほぼ未完成だったので許容）。

## アーキテクチャのメモ

- 差分インポートは会話単位の content_hash 比較。一致→skip、不一致→
  メッセージ全削除して再挿入（メッセージ単位のマージはしない。シンプル優先）。
- CLI同期はファイルの (mtime, size) を `ingest_files` に記録して変更検知。
  watchdogではなくポーリング（60秒、`CAIRN_SYNC_INTERVAL`で変更可）。
- uvicornはスレッドプールでハンドラを動かすため、SQLite接続は
  threading.local で1スレッド1接続。
- MCPブラウザ(Docker内)から動作確認するときは `host.docker.internal` を使い、
  サーバーを一時的に `--host 0.0.0.0` で起動する必要がある（通常は127.0.0.1）。

## 添付 (P1-H / 各 source 実調査メモ)

- **claude_cli**: `type:"attachment"` という**行は罠**。中身は tool/hook メタデータ
  （allowedTools、MCP、skillCount、commands、stdout/stderr 等）でファイル添付ではない。
  → 実際の添付は user/assistant の `message.content` ブロック内に
  `{type:"document", source:{type:"base64", media_type, data}}` として現れる。
  実データで `application/pdf` を確認（base64 数十〜百KB）。`image` ブロックも
  同じ source 形（type:"base64" or "url"）の前提でパース。
- **codex_cli**: content は `input_text` / `output_text` のみ。添付なし。
- **claude_export**: 実 export で検証（2026-06-24）。添付は content ブロックでなく
  message レベルの `attachments[]` / `files[]` に置かれる。`extracted_content` は
  `extracted_text` として保存し、hash は extracted_content のテキストハッシュ
  （bytes が無いため bytes hash と意味が異なる）。詳細は「Claude エクスポート」節参照。
- **gemini**: 実 Takeout で検証（2026-06-24）。添付は `imageFile` / `attachedFiles[]` /
  `subtitles[].url` の 3 系統。assistant 生成画像は `safeHtmlItem` 内の `<img src>` で
  識別。ZIP 同梱の画像 bytes は parse_upload 経由で hash + size を取得（bytes 本体は
  保存しない）。詳細は「Gemini」節参照。
- **chatgpt**: 現状 fixture には添付ブロックなし。実エクスポートでの挙動は未検証。
- **バイナリ本体は保存しない**方針（attachments は metadata only: source_ref / mime /
  size / hash / extracted_text）。redact-apply の責務を text に限定し、ストレージ爆発も回避。
  ハッシュは base64 デコード後の生バイトの sha256。
- **`content_hash` の後方互換**: attachments を空のとき content_hash は P1-H 以前と同じ
  JSON 形を維持（`[role, text, created_at]`）、添付があるときだけ 4 項目目に `[hash...]`
  を append。これで「P1-H 以前に取り込んだ会話が再 sync で一斉に updated 扱いになる」
  事故を回避（test_idempotency / test_attachments で固定）。
- **FK CASCADE**: attachments は `(conversation_id, message_id)` 双方に ON DELETE
  CASCADE。upsert_conversations は更新時に messages を全削除→再挿入するため、
  attachments も自動で消えて再投入される（orphan 0 を integrity_check で確認）。

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

## chunking（P2-1a, 2026-06-25）

- `chunks` は messages からの**派生データ**。`cairn.db` の chunks 行は削除しても
  `admin rechunk --all` で復元できる前提で実装している。
- `start_offset` / `end_offset` は元 message.text 上の char offset。**隣接 chunk は
  OVERLAP 文字ぶん重なる**ため、全 chunk を offset で連結すると overlap 領域が二重に
  出る。原文復元は「いずれか 1 chunk の text スライス」を使うこと。
- `chunking_version` を**列に持つ**ことで algorithm 変更時に旧版を残したまま新版を
  生成できる。`rechunk_messages(force=False)` は同一バージョンの再生成を skip し、
  `force=True` は同一バージョン分のみを delete → 再 insert する（他バージョンは温存）。
- `upsert_conversations` は message を再 insert（新 id）した後に自動 chunk するため、
  通常運用では rechunk を呼ぶ必要はない。**rechunk が必要になるのは①既存 DB に chunks
  テーブルが migration v5 で追加された直後、② chunking_version を上げたとき**。

## embeddings（P2-1b, 2026-06-25）

- `embeddings` は chunks からの**派生データ**。`vector` は f32 little-endian の BLOB、
  `dimension` を**列に持つ**ので read 時に provider を呼ばずに width が分かる。
- `UNIQUE(chunk_id, provider, model)` で同一 chunk に複数 provider/model が共存可能
  （A/B、移行期、外部 provider opt-in 用）。`only_missing=True` の embed_chunks は
  この index でチェック、`only_missing=False` は `INSERT OR REPLACE` で in-place 更新。
- e5 系モデルは **`"passage: ..."` / `"query: ..."` プレフィックスが必須**。
  `LocalSbertProvider` 側で自動付与するので呼び出し側はモデル依存を意識しない。
- `sentence-transformers` は **遅延 import**：モジュール import 時には触らず、
  最初の `embed_passages` / `embed_query` で `SentenceTransformer(...)` を初期化。
  テストは決定論的な `FixtureProvider`（SHA-256 → 8 次元正規化ベクトル）を使い、
  CI/開発環境に sentence-transformers が無くてもグリーン。
- P2-1b の `find_similar_chunks` は **pure Python cosine の全件走査**。
  個人アーカイブ規模なら数万 chunks まで秒オーダー、P2-1c で sqlite-vec ／ numpy
  fallback に差し替える前提（ADR-0001）。

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
