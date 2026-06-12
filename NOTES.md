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
