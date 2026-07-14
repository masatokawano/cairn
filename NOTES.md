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
- **大口アカウントの export は複数ファイルに分割**される（2026-06-27 検証）:
  `conversations-000.json` 〜 `conversations-NNN.json`（実検証時は 13 shards =
  約 110MB の JSON、~530MB の zip に同梱）。`parsers/__init__.py` が
  `_CHATGPT_SHARD` 正規表現で shards を検出し、`_parse_chatgpt_shards` で順番に
  parse → 結合する。**小規模アカウントは従来通り `conversations.json` 単一**で、
  そちらの経路は変更なし。
- 実 export（1226 会話 / 15245 メッセージ）で warnings 0、source_id 重複 0、
  fallback 0（全 conversation に `conversation_id` あり）を確認。
- **zip の総サイズが `CAIRN_MAX_UPLOAD_MB` (既定 500MB) を超える場合**は環境変数で
  引き上げる必要あり。conversations-NNN.json だけ抜き出した小 zip を再 import する
  方法もあるが、実用上は環境変数のほうが楽。
- `chat.html` (~90MB の HTML レンダ) と `file-*.dat` (添付実体) は parser が読まない
  （ZIP 容量の大部分はこれ）。attachments 対応は別タスク（attached blob store）。

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
- **外部 items（karakeep/zotero、M3 で obsidian 追加予定）の redaction は
  `db.upsert_items` が唯一の choke point**。url_norm / doi も db 層が
  「redact 済み url」から導出する（コネクタは raw の url / doi を渡すだけ。
  URL query に混ざった API key が url_norm に平文で残る穴を防ぐ + 会話経路の
  「redact → 抽出 → 正規化」と順序が揃う。Codex M1 レビュー指摘）。
  content_hash は redaction 後の (title, url, url_norm, doi, meta) から計算
  （タイムスタンプは hash に含めない: dateModified だけの変更を skip にするため）。
  コネクタ側で redact / 正規化 / hash 計算をしないこと。
- **Karakeep v1 API には modified-since フィルタがない**。`sync karakeep` の増分は
  createdAt desc + early-stop で「新規のみ」検知。既存ブックマークの編集
  （タグ変更等）は `cairn sync karakeep --full` のスイープでしか拾えない
  （content_hash skip でスイープ自体は安い）。Zotero は `since=<library version>` +
  `Last-Modified-Version` ヘッダで真の増分が可能。
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

## 横断インデックス（M2 / migration v12）

- **chunks テーブル再構築（v12）は FK まわりに罠が 2 つ**:
  ① `DROP TABLE chunks` は FK ON だと embeddings（ON DELETE CASCADE）を
  全滅させる → migration は `PRAGMA foreign_keys=OFF` → BEGIN…COMMIT →
  `PRAGMA foreign_keys=ON` で挟む（PRAGMA はトランザクション内では**無言で
  no-op** になるので、必ず外側に置く）。
  ② `ALTER TABLE chunks RENAME TO x` は**他テーブルの FK 句を x に書き換える**
  （embeddings の FK が temp 名を指したまま迷子になる）。正しい向きは
  「新テーブルを別名で作る → コピー → DROP chunks → RENAME 新→chunks」。
  テストの旧形状シミュレーションも同じ罠を踏むため `tests/schema_shapes.py`
  の `downgrade_chunks_pre_v11` に集約した（呼び出しはトランザクション外で）。
- **chunks_fts は standalone FTS（external content にしない）**。索引対象が
  kind='item_text' の部分集合であり、external content の `('rebuild')` コマンドは
  content table 全体を再索引して message chunks まで取り込んでしまうため。
  部分再構築は `DELETE FROM chunks_fts` → 条件付き INSERT SELECT で行う
  （`cairn index rebuild` 実装参照）。
- main.py に Phase 3 の `_VALID_KINDS`（assertion 種別）が既にある。items.kind の
  検証セットは `_SEARCH_KINDS`。同名で足すと後勝ちで静かに壊れる。
- **既知の限界（Codex M2 レビュー nit、意図的に未対応）**: chunks_fts の UPDATE
  トリガは `UPDATE OF text WHEN old.kind='item_text'` のみで、直接 SQL で
  `chunks.kind` を書き換えると部分索引性が崩れ得る。コード経路に kind の UPDATE は
  存在せず（rechunk は delete→insert）、修正には v13 migration（トリガ差し替え）か
  fresh/migrated の schema 乖離が必要なため見送り。kind を UPDATE する変更を
  入れる際はトリガも `UPDATE OF text, kind` + new.kind 条件へ差し替えること。

## Obsidian 書き込み（M3 / `deliver/obsidian_writer.py`）

- **`mkdir(parents=True)` は封じ込め検証の「前」に副作用を起こす**。allowlist の
  ベースディレクトリを作る前に、必ず「最深の既存祖先」を `resolve()` して Vault 内か
  検証すること（`_safe_mkdir_within`）。順序を逆にすると、中間コンポーネント
  （例: `External Brain`）が Vault 外への symlink で subdir が未作成のとき、書き込み
  自体は拒否されても **拒否前に symlink 先へディレクトリを作ってしまう**
  （Codex M3 レビュー blocker）。`resolve()` は経路上の全 symlink をたどるので、
  最深の既存祖先が Vault 外に解決されれば escape を mkdir 前に捕捉できる。
- 破損 symlink（存在しないターゲットへのリンク）が中間にある場合、Python の
  `Path.mkdir(parents=True, exist_ok=True)` は `is_dir()` が False になり
  FileExistsError で止まる → Vault 外に作らず fail-safe。ObsidianWriteError では
  ないが封じ込めは保たれる。

## auto_lists の markdown エスケープ（M3 / `deliver/auto_lists.py`）

- 改行の畳み込みだけでは **同一行の markdown 構文注入**（`[x](url)` / `![x](url)` /
  `` `code` `` / `**強調**`）を防げない（Codex M3 レビュー should）。外部由来テキストは
  位置別にエスケープする: 散文位置は `_esc`（メタ文字をバックスラッシュ escape）、
  code span 内は `_esc_code`（バッククォート置換。span 内は `\` が効かない）、
  URL は `_url`（`<autolink>` ラップ + `<`/`>` 除去 + 非 http(s) は行ごと省略）、
  wikilink は `_wikilink`（`[]|#` を含むパスはプレーンテキストに fallback）。

## 週次レビュー v2（M4 / `recall/` + `deliver/weekly_review.py`）

- **陳腐化警告（§5.4）の鮮度は import_runs だけでは測れない**。`/api/import`
  経由のアップロードは `import_runs.source='upload'` で記録され、パーサー由来の
  source 名（chatgpt/claude/gemini）では入らない。import_runs のみを見ると手動
  エクスポート系が常に「一度も取り込まれていません」になる。`stale_exports` は
  import_runs と **`conversations.updated_at` の新しい方**を採用する（警告の実質
  ＝「その source の最新知識の古さ」も後者が正しく測る）。
- **日本語タイトルは空白がないので、タイトル丸ごとの FTS フレーズはほぼ同一文
  にしか当たらない**。related() の keyword アームで再浮上を効かせるには、タイトル
  を内容語に分割してクエリ化する（`_content_terms`：区切り・句読点・主要助詞で
  分割）。分割語を related() の複数クエリ RRF に流すと OR + rank fusion になる。
- **ASCII の短い断片（`on` / `to` / `T1`）と英語ストップワードはノイズ**。LIKE で
  アーカイブの半分に当たり、理由行が「今週の『on』に関連」になる。`_content_terms`
  は ASCII は3文字以上かつ非ストップワードのみ採用（CJK は2文字で意味を持つので
  そのまま）。それでも会話タイトルが文の断片（「完了しました」等）だと理由行が
  やや雑になるが、これは実データのタイトル品質の問題で M6 のチューニング範囲。
- related() は会話ノイズタイトル（security-review boilerplate / New chat 等）も
  除外する。ただし検索行にはメッセージ数がないので `_is_review_candidate` の
  件数条件は使えず、**タイトル規則のみ**の `_is_noise_conversation` を別に持つ。
- `weekly_review.run()` は既存週があれば `status="exists"` で **何も書かず exit 0**
  （launchd のログイン時トリガが日曜の生成後に無音で再実行されるため）。
  obsidian_writer に read-only の `target_exists()` を追加（`_resolve_target` と違い
  ディレクトリを作らないが、封じ込め検証は同じ = Vault 外 base はエラー）。
- **草案は LLMProvider の structured JSON 契約を使う**（Phase 3 の `complete_structured`
  ＋ schema）。ollama 停止・モデル無し・非 JSON はすべて例外に集約され、run() が
  握って空草案＋失敗注記に縮退する（S4）。テストは `FixtureProvider(responses=[...])`
  で草案を注入、`fail_first=99` で失敗経路、実 `OllamaProvider` を死にポート
  （`CAIRN_OLLAMA_HOST=http://127.0.0.1:1`）に向けて到達不能を再現。
- 草案は外部テキスト由来なので **untrusted 扱い**：`_esc` で markdown エスケープし、
  provenance ラベル `<!-- generated_by: cairn/<model>/prompt_v1 -->` を必ず付す。
  LLM へ渡す資料は `<<<資料ここから>>>`/`<<<資料ここまで>>>` で囲み、各行は
  `_llm_line`（区切りマーカー除去 + 長さ制限）で洗う。プロンプト改訂時は
  `PROMPT_VERSION` を上げる。

## 横断 MCP サーバ（M5 / `app/mcp/` + `run_mcp.py`）

- **`import mcp` 衝突は起動スクリプトの「ディレクトリ」で決まる**。旧
  `app/mcp_server.py` はスクリプトパス起動で `sys.path[0]=backend/app/` になり、
  `app/mcp/` パッケージを作った瞬間 `import mcp`（SDK）がローカルへ解決されて壊れる
  （NOTES 末尾 M0 逸脱で予告済み）。**解決＝ランチャ `backend/run_mcp.py`**：スクリプト
  dir が `backend/`（`mcp` という名の子が無い）になるので `import mcp` は SDK に解決。
  登録は「パッケージ（`-m app.mcp`）ではなくランチャ絶対パス」を指す。pytest も cwd=
  `backend/` なので同様に SDK 解決（`app.mcp` は別ドット名で衝突しない）。**`app/mcp/` を
  作った以上、旧 `mcp_server.py` のスクリプトパス登録は次回 spawn で必ず import 失敗する**
  ため、パッケージ新設・旧ファイル削除・再登録はワンセット。
- **`build_context_pack` の seed は kind 別に検索する**。単一のブレンド検索
  （全 kind を1つの top-N で競わせる）だと、実データでは会話が上位を占めて
  Zotero/Karakeep が top-24 から押し出され、根拠バケットが 0 件になる（実 DB で確認）。
  `_VISION_KINDS` / `_EVIDENCE_KINDS` を `db.search(kinds=...)` で別々に引いて各バケットの
  代表性を保証する。過去の議論は seed と重なりやすいので related() を `k*3` で広めに取り、
  seed 済み item を除いた残りから bucket_k を取る。
- **未解決課題は抽出しない**（D2 非目標）。content バケットは source/kind による構造的
  グルーピングのみ（週次の 発見/思考/根拠/理解 と同じ発想）。「未解決」は `synthesize=True`
  時の LLM 合成が資料から起こす（§6.2 の合成部）。合成は既定 off で毎回 ollama を呼ばず、
  失敗時は `synthesized=null`＋`synthesis_note` へ縮退（content は無傷、S4）。
- テストで `MAX_BODY_CHARS` を差し替えるときは **`server.MAX_BODY_CHARS`** を monkeypatch
  する（`server` は `from . import MAX_BODY_CHARS` で自 module に束ねているため、パッケージ
  `app.mcp` 側の属性を差し替えても効かない）。

## Health MCP（H7 / `app/health/mcp_*` + `run_health_mcp.py`）

- DuckDB の read-only consumer は通常の `store.connect()` を使わない。`connect_readonly()`
  で migration を拒否し、MCP の snapshot は永続化せず content-addressed ID/hash を返す。
  永続 H6 snapshot は分析イベントごとの UUID のままにし、read-only identity と混同しない。
- minimum disclosure は行数 cap だけでは足りない。metric を必須にし、期間・free-text・evidence
  も別々に上限を持たせる。event/interpretation は既定除外し、解釈を含める場合は **全 evidence**
  が同じ context pack の明示 snapshot 内にあることを検証する（一部 observation の一致だけで
  採用すると、未選択 event/document/reference の内容が title 経由で漏れ得る）。
- provenance は「選択した原値行の hash」と「実際に返した正規化 projection の hash」を分ける。
  正規化値だけが変わった場合、前者は同じでも後者が変わるため、返却 payload を pin するには
  両方が必要。event は別 snapshot hash、各 fact は observation/source ID を保持する。

## 運用の通知・再構築（M6 / `app/ops.py` + launchd）

- **失敗通知は終了コード基準（stderr は見ない）**。エージェントの stderr は
  sentence-transformers の重みロードバー・HF Hub 警告で恒常的に賑やか、かつ append で
  古い行が残る（実ログで `weekly-error.log` に M4 以前の "not implemented" が残留を確認）。
  「stderr 非空＝失敗」だと誤検知だらけになる。`cairn sync all` は1ソース失敗で exit 1、
  `review weekly` は実エラー時のみ exit 1（週の既存・草案失敗は成功）なので、exit code が
  唯一の正しいシグナル。通知は `cli.main()` が `SystemExit` を捕捉し、`CAIRN_NOTIFY` 有効
  かつ非ゼロ時のみ `ops.notify_failure`（osascript 通知 + failures.log 追記）。通知は
  best-effort で **実 exit code を絶対に変えない**（osascript 不在・例外は握りつぶす）。
  plist は `CAIRN_NOTIFY=1` / `CAIRN_AGENT` / `CAIRN_LOG_DIR` を渡す。手動実行では鳴らない。
- **osascript のメッセージに外部由来テキストを入れるなら AppleScript 文字列リテラルを
  エスケープ**（`ops._as`: `\`→`\\`、`"`→`\"`）。エラーメッセージ経由の注入を防ぐ。
- **`sync all`（毎時）は新規 chunk を embedding まではしない**。keyword/FTS は即時だが
  semantic/hybrid はバックログのぶん遅れる。`cairn index rebuild` が穴埋め（rechunk は
  skip、embed は only_missing、FTS/vec0/item_links 再構築）。実測（本番 340MB / chunk
  約31.5k / Apple Silicon）: 差分なしで**約10秒**、埋め込みバックログ約2,900 chunk 込みで
  **約57秒**。定期 rebuild で semantic 索引が追いつく（M6 で観測。恒常自動 embed 化は
  設計変更＝要 Decision Record 改訂なので実装しない）。
- **plist に `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`** を入れると e5 が
  キャッシュ済み重みだけを使い、sync-error.log の HF 警告ノイズが消える（モデルは既に
  ローカルキャッシュ済みが前提）。

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

## sqlite-vec / vector index（P2-1c, 2026-06-25）

- `sqlite-vec` の vec0 virtual table はカラム制約に**癖**が多い。実装で踏んだもの:
  - **`INSERT OR REPLACE` 不可**。再 upsert は `DELETE` → `INSERT` の 2 ステップ。
  - **`k = ?` と `LIMIT` の同時指定が禁止**。エラー: `Only LIMIT or 'k =?' can be provided`。
  - **`rowid IN (?)` 単一要素は SQLite が `rowid = ?` に書換える**結果、vec0 が
    LIMIT 制約を検知できず `A LIMIT or 'k = ?' constraint is required` で失敗する。
    解決: `LIMIT` ではなく `k = ?` 制約を使う（書換えに耐える）。
  - **候補 chunk_id を `rowid IN (...)` に全部渡すと SQLite 変数上限で落ちる**
    （`too many SQL variables`）。`find_similar_chunks` は候補を SQL 変数として
    KNN に渡す設計なので、単一モデルの候補プールが上限（SQLite 3.32+ は 32766）を
    超えると semantic/hybrid が全滅する。NumpyIndex は 900 件バッチ済みだが
    `SQLiteVecIndex.search` は未バッチだった（2026-07-14、social_post 46k 取り込みで
    顕在化）。解決: `conn.getlimit(SQLITE_LIMIT_VARIABLE_NUMBER)` でバッチ幅を決め、
    各バッチの top-k をマージ（グローバル k-近傍は必ずどれかのバッチの top-k に入る
    ので、マージ後に再ソート→k で正確）。**新しい検索経路を足すときは候補数が
    上限を超え得るか必ず確認する。**
- `db.connect()` で `enable_load_extension(True)` → `sqlite_vec.load(conn)` を試行し、
  thread-local に成否を保存（`_sqlite_vec_loaded()`）。ロード後は `enable_load_extension(False)`
  に戻して攻撃面を最小化（SECURITY.md にも反映）。
- `CAIRN_VECTOR_INDEX=numpy` で明示的に NumpyIndex を強制可能（テスト用 / 拡張ロード
  が不安定な環境のエスケープハッチ、ADR-0001 §7.3）。
- **vec0 は外部 FK CASCADE を受けない**。chunks 削除で embeddings は CASCADE 削除されるが、
  vec0 行は残る（orphan）。integrity_check が `orphan_vector_index` で数を報告、
  `admin rebuild-vector-index` で再構築するとクリーンになる。
- **vec0 は単一次元のみ**保持。モデル切替（dim 変更）したら手動で
  `admin rebuild-vector-index` を実行する必要あり。自動切替はデータロス防止のためしない。
- find_similar_chunks は 3 段: (1) candidates SQL（provider/model/dim/source/date フィルタ）
  → (2) vector_index.search で KNN → (3) 上位 k を hydrate。
  sqlite-vec が空を返したら NumpyIndex フォールバック（dim 不一致や vec0 未populate ケース用）。

## ハイブリッド検索（P2-2, 2026-06-25）

- `db.search()` の既定 mode は **`keyword`** に据え置き。`semantic`/`hybrid` を既定に
  すると、`/api/search` の単純呼び出しでも embedding model のロードと推論が走るため、
  「既存挙動を変えない」「未設定環境で壊れない」を優先した。UI は P2-3 で明示的に
  `mode=hybrid` をデフォルトに切り替える。
- RRF は `score = Σ 1/(k₀ + rank_i + 1)` で k₀=60（Cormack et al. 2009）。**rank ベース**
  なので BM25 と cosine の絶対スケール差を気にせず統合できるのが採用の決め手。
- semantic は会話単位で best chunk に集約（同会話の複数 chunk マッチは hit_count に集約）。
  これにより keyword の `ROW_NUMBER OVER (PARTITION BY c.id ...) rn=1` と同じ結果形状を保つ。
- hybrid で同会話を両 path が拾ったときは **keyword の snippet を採用**（`[[…]]` 強調が
  UI に有用）、`semantic_score` は semantic 側から拾う。`match_reason="both"` でフラグ。
- provider 解決は `_active_embedding_provider()`: ① `CAIRN_EMBED_PROVIDER=name:model` 環境変数、
  ② embeddings テーブルの最多 (provider, model)、③ どちらも無ければ RuntimeError。
  単一モデル運用なら **設定不要で動く**のが狙い。
- API 側は `pattern="^(keyword|semantic|hybrid)$"` で 422 検証し、typo がサイレントに
  既定 mode へフォールバックしないようにした。

## 添付バイナリの blob store（P1-J, 2026-06-27）

- 添付の bytes 本体は `data/attachments/{hash[:2]}/{hash}` に sharded で保存。
  hash をファイル名そのものに使っているので**同一バイト列は自然に重複排除**される。
  schema 列での pointer 管理は不要（drift しがちなので避けた）。
- 書き込みは `{target}.tmp` への書き出し → `os.replace` で atomic。途中で死んでも
  半端な blob が完成済みファイルを名乗ることがない。
- `ParsedAttachment.data: bytes | None` を新設。bytes を持っているパーサー
  （現状 gemini Takeout）はここに乗せ、`db.upsert_conversations` が `attachments.store()`
  を呼んで永続化する。**メタデータのみのソース（Claude の UUID 参照等）は `data=None`** の
  ままで blob は作らない。
- パーミッションは DB と同じ 0600（読み出しは現所有者のみ）。
- `db.integrity_check()` に `attachment_blobs_missing`（attachments 行は hash を持つが
  ファイル無し）と `attachment_blobs_orphan`（ファイル有りだが誰も参照していない）を追加。
  どちらも problems には積まない（前者はメタデータ専用ソースで正常、後者は GC 余地）。
- **`admin backup` は現状 cairn.db のみコピー**で attachments/ 配下は対象外。完全バックアップ
  には `cp -R data/attachments/` も別途必要。blob 込みの backup は別タスク扱い
  （blob は GB 級になり得るため、毎回コピーは望ましくない場合がある）。
- chatgpt 実 export の `file-*.dat`（アップロード添付バイナリ）取り込みは未対応。
  `content.parts[].asset_pointer = "file-service://file-XXX"` を辿って `file-XXX.dat` を
  解決する経路が必要で、別コミットで実装する（添付モデルが UUID 形式と混在しているため
  慎重に）。

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

## M0 モジュール骨格の意図的逸脱（DESIGN.md §3 との差分）

DESIGN.md §3 は `backend/app/` 直下に `parsers/ connectors/ core/ index/ recall/
deliver/ mcp/ cli.py` の骨格を挙げているが、M0 では以下 2 箇所を意図的にずらして
いる。§3 冒頭の「既存構造に合わせて調整可」に沿った判断で、非目標への逸脱では
ないが、レビュー時に「§3 と食い違っている」と誤検知されないよう記録する。

- **`app/index/` は作らない**: 既存の `chunking.py` / `embedding/` /
  `vector_index.py` が該当する。DESIGN.md §3 も「chunking/embedding/FTS/RRF を
  items 対応に一般化」と書いており、新パッケージ導入ではなく既存モジュールの
  拡張が本旨。M1〜M2 で items 対応を進める際もこれらのモジュールに手を入れる。
- **`app/mcp/` を M0 では作らない（M5 まで延期）**: `mcp_server.py` は 26 行目で
  `from mcp.server.fastmcp import FastMCP` として MCP SDK を絶対 import する。
  README（`claude mcp add`）が推奨する登録方法は**スクリプトパス直接実行**
  （`python .../backend/app/mcp_server.py`）で、この起動時 Python はスクリプトの
  ディレクトリ（= `backend/app/`）を `sys.path[0]` に積む。`backend/app/mcp/`
  ディレクトリを新設した瞬間、`import mcp` が SDK ではなくローカルパッケージへ
  解決されて MCP サーバが起動不能になる。稼働中のユーザー MCP を壊す代償は
  §3 との名前一致より重い。M5（MCP サーバ統合）で `mcp_server.py` を廃止し
  `python -m app.<新名>` 形式へ移す際に、起動方式ごと解決する。
  **→ M5 で解決済み**: `app/mcp/` を新設し、起動はランチャ `backend/run_mcp.py`
  （スクリプト dir = `backend/` なので `import mcp` は SDK 解決）に移行、
  `mcp_server.py` は廃止。詳細は上の「横断 MCP サーバ（M5）」節。
