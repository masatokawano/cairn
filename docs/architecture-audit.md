# Architecture Audit — Phase 1 設計の起点

ROADMAP.md「Task 1: 現状監査と Phase 1 設計」の成果物。コードを変更する前の
現状把握と、Phase 1 を小タスクへ分解した実装計画を記録する。

- 監査日: 2026-06-22
- 対象コミット: `1c17738`（main）
- ベースライン: `backend/.venv/bin/python -m pytest tests/ -q` → **53 passed**
- frontend build: 未実行（このタスクはコード変更なし）

> 注: Codex の引き継ぎプロンプト（`CODEX_PROMPT_FOR_CLAUDE_CODE.md`）の優先項目
> 1〜5（localhost 防御 / import DoS / DB 0600 / CSRF / 依存固定）は
> コミット `9188721` で**実装済み**。`SECURITY.md` に ✅ 記載あり。本監査は
> ROADMAP の Phase 1（原本保全・基盤堅牢化）に焦点を当てる。

---

## 1. 現状アーキテクチャの事実確認

### 1.1 SQLite スキーマ（`backend/app/db.py`）

```
conversations(
  id INTEGER PRIMARY KEY,        -- rowid（DB ローカル、安定 ID ではない）
  source TEXT NOT NULL,
  source_id TEXT NOT NULL,       -- source 内の安定 ID（後述）
  title TEXT NOT NULL,
  created_at TEXT, updated_at TEXT,
  content_hash TEXT NOT NULL,    -- 差分判定キー
  meta TEXT NOT NULL DEFAULT '{}',
  UNIQUE(source, source_id)
)
messages(
  id INTEGER PRIMARY KEY,        -- rowid
  conversation_id INTEGER → conversations(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,          -- 会話内の順序
  role TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at TEXT
)
messages_fts                     -- FTS5 trigram, content='messages'（external content）
ingest_files(path PK, mtime, size)   -- CLI ログのポーリング差分検知
```

- インデックス: `idx_messages_conv(conversation_id)`, `idx_conversations_updated(updated_at)`。
- FTS 同期トリガ: `messages_ai`（INSERT）/ `messages_ad`（DELETE）/ `messages_au`（UPDATE OF text）。
- PRAGMA: `foreign_keys=ON`, `journal_mode=WAL`, `secure_delete=ON`。
- **派生テーブルは未導入**（segment / assertion / embedding / attachment いずれも無し）。

### 1.2 ID 生成と source_id の利用状況

| source | source_id の決定方法 | source 固有 ID | 安定性 |
|---|---|---|---|
| chatgpt | `conversation_id or id or f"index-{i}"` (`chatgpt.py:116`) | あり | ✅ ／ ⚠️ fallback の `index-{i}` は**順序依存で不安定** |
| claude_export | `uuid or f"index-{i}"` (`claude_export.py:72`) | あり | ✅ ／ ⚠️ 同上 |
| claude_cli | `session_id or path` (`claude_cli.py:106`) | あり（sessionId） | ✅ ／ パス fallback はディレクトリ移動で変わる |
| codex_cli | `session_id or path` (`codex_cli.py:109`) | あり | ✅ ／ 同上 |
| gemini | `f"{time}-{digest}"`、`digest=sha256(time|title_raw)[:16]` (`gemini.py:94`) | **なし**（内容由来で合成） | ✅ 内容が同じなら安定 |

- conversation の安定 ID = `(source, source_id)` の複合一意キー。
- ~~**message の安定 ID は DB に存在しない。**~~ → **P1-C で解決済み**:
  `messages.source_message_id` を追加し保存・返却。chatgpt (`msg.id`) / claude_cli /
  claude_export (`uuid`) はパース済み、codex / gemini は元データに安定 ID が無く None。

### 1.3 差分インポート（idempotency）

- 単位: 会話単位の `content_hash` 比較（`base.py:32` = `sha256` of `[(role,text,created_at)]`）。
- ロジック（`db.upsert_conversations`）:
  - `(source, source_id)` で既存検索 → hash 一致なら **skip**。
  - hash 不一致なら **messages を全削除して全再挿入**（message 単位マージはしない。シンプル優先 / NOTES.md）。
  - 該当なしなら **新規 insert**。
- redaction は**この choke point で content_hash 計算の前**に適用（再同期で平文が復活しない設計 / NOTES.md）。
- CLI 同期は `ingest_files` の `(mtime, size)` でファイル変更検知（`cli_sync._scan`）。
- 同時実行は `cli_sync.ingest_lock`（threading.Lock）で直列化。

### 1.4 添付ファイル / 画像

- **未対応。** attachment テーブル・メタデータ・MIME/size/hash いずれも無し。
- パーサーはテキストブロックのみ採用し、非テキスト（画像・tool_result 等）は破棄。
- → P1-3 は完全に未着手。

### 1.5 DB 再構築経路

- `cairn.db` 削除 → 起動時 + 60 秒間隔で **CLI ログは自動再取り込み**。
- チャット系（ChatGPT/Claude/Gemini エクスポート）は**原本を Cairn が保持しないため、
  ユーザーが再ドロップする必要がある**（README に明記）。
- → 「原ログ + エクスポートから再構築できる」設計は CLI 分のみ自動。チャット分は手動依存。

### 1.6 backup / export / integrity / migration の現状

| 機能 | 状態 | 所在 |
|---|---|---|
| backup | △ 限定的 | `admin.py cmd_apply` が redact 前に `cairn.db.backup-{stamp}` を作るのみ。独立した `backup` コマンドは無し |
| export (JSONL) | ❌ 無し | — |
| export (Markdown) | ❌ 無し | — |
| integrity-check | ❌ 無し | redact-apply 内に `_verify_clean`（秘密パターン残存検査）はあるが汎用 integrity ではない |
| migration runner | △ **骨格あり** | `db.py:68-85`: `PRAGMA user_version`、`_SCHEMA_VERSION=1`、`_apply_migrations`、空の `_MIGRATIONS`。backup-before-migration とテストは未整備 |

- 注目: **migration の枠組み（P1-4）は既に最小実装が存在する。** 基底スキーマは
  `IF NOT EXISTS` で冪等、`_MIGRATIONS` に `(version, sql)` を append して
  `_SCHEMA_VERSION` を上げる方式。残るのは「backup 連携」「migration テスト」「実際の
  バージョン繰り上げ検証」。

### 1.7 テストカバレッジの空白

現状 53 件。内訳とギャップ:

- `test_parsers.py`(10): 各パーサー fixture + upload 検出 + ZIP。
- `test_db.py`(9): diff import / FTS 日本語 / LIKE fallback / AND / source filter /
  会話グルーピング / DB 側ページング / stats / file_state。
- `test_api.py`(13): Host/Origin 検証 / import サイズ・zip bomb / sync 409 / **DB 0600**。
- `test_mcp.py`(7), `test_redact.py`(10), `test_admin_migration.py`(4: redact 系のみ)。

**空白:**
- 再取り込み idempotency の専用回帰テスト（同入力 N 回 → skip、編集 → updated）は
  `test_insert_and_diff_import` で部分的だが、複数回・編集ケースの明示テストが薄い。
- migration バージョン繰り上げのテスト（`_MIGRATIONS` 実行で user_version が上がる）が無い。
- integrity-check（未実装機能）。
- attachment（未実装機能）。
- export JSONL/Markdown（未実装機能）。
- message 安定 ID の保存・往復テスト。

---

## 2. Phase 1 タスク分解

ROADMAP §16 の 8 分割に沿い、各タスクを「垂直スライス（schema → repo function →
API/CLI → test → docs）」で実装する。原則 **1 セッション 1 タスク**。

### P1-A. schema version + migration runner（骨格の完成）— ✅ 実装済み（2026-06-22）
- **現状**: 完了。`db.py` に新規/既存DB判定（conversations テーブル有無）、
  migration 前の自動 backup（`*.premigrate-v{from}-to-v{to}-{stamp}`、0600）、
  `tests/test_migrations.py`（4 件）を追加。詳細は NOTES.md「スキーマ migration」。
- **受入基準（達成）**: 新規DBは `_SCHEMA_VERSION` に直接スタンプし migration を走らせない /
  既存DBは `_MIGRATIONS` が順に適用され user_version が最新になる / migration 前に backup が
  作られる / 既存データが migration 後も検索・表示できる / backend test 57 passed。
- **リスク**: 低（既存スキーマは冪等、追加のみ）。
- **依存**: なし（最初に実施推奨。他タスクの schema 変更の前提）。

### P1-B. import run history — ✅ 実装済み（2026-06-22）
- **現状**: 完了。`import_runs` テーブル（source / input_name / started_at /
  completed_at / parser_version / inserted・updated・skipped・failed・conversations /
  warnings・warning_summary / content_hash / status・error）を `_SCHEMA` に追加し、
  migration v2（`_MIGRATION_2_IMPORT_RUNS`、`_SCHEMA_VERSION=2`）で既存DBにも適用
  （P1-A の migration runner の初実運用）。記録は `db.record_import_run()` を
  各 ingest 経路の呼び出し側（`/api/import` 成功・失敗、CLI sync のファイル単位）から行う。
  `upsert_conversations` 自体は純粋なまま（source/path は呼び出し側が保持するため）。
  parser version は `parsers/base.py: PARSER_VERSION`（suite 単位、将来 per-parser 化）。
  参照は `GET /api/import-runs` と `python -m app.admin import-runs`。
- **受入基準（達成）**: upload/sync 後に run 行が残る（成功は ok、失敗は error+理由）/
  counts・warning_summary・content_hash が確認できる / API・admin CLI から履歴参照可 /
  backend test 63 passed。
- **リスク**: 低〜中（全 ingest 経路に記録を挿す。choke point は `upsert_conversations` だが
  source/path 情報は呼び出し側にあるため呼び出し側で記録）。
- **依存**: P1-A（migration 経由で既存DBに table 追加）。
- **残課題**: Web UI への履歴/ warning 表示は未実装（Codex 項目6・別タスク）。failed は
  会話単位の失敗カウント用に列を用意したが現状は parse 例外を status=error の run として
  記録（個別会話失敗は warning に含む）。

### P1-C. stable IDs + idempotency 回帰テスト — ✅ 実装済み（2026-06-22、安全コア）
- **現状**: 完了。`messages.source_message_id` 列を `_SCHEMA` に追加 + migration v3
  （`_MIGRATION_3_MSG_SOURCE_ID`、`_SCHEMA_VERSION=3`）。`upsert_conversations` で保存、
  `get_conversation` で返却。source_message_id は chatgpt（`msg.id`）/ claude_cli /
  claude_export（`uuid`）が既にパース済みのため**パーサー変更は不要**。codex / gemini は
  元データに安定 message ID が無く None（additive）。`tests/test_idempotency.py`（5件）追加。
- **fallback ID は変更せず**（ユーザー判断「安全コアのみ」）。理由: `index-{i}` は同一
  ファイル再取り込みでは既に冪等で受入基準を満たし、内容由来 ID への置換は既存 DB の
  破壊（redact 後の再計算不一致・タイムスタンプ衝突）リスクが実益を上回るため。
- **受入基準（達成）**: 同入力の複数回取り込みで重複ゼロ / 編集会話が in-place 更新
  （会話は1行、messages は全置換）/ message に安定 ID（あるソース）を保存・返却 /
  backend test 68 passed。
- **リスク**: 低（additive な列追加のみ。fallback は据え置き）。
- **依存**: P1-A。
- **残課題**: chatgpt/claude/gemini の実エクスポート未検証（NOTES.md）。codex の message ID は
  ローカルログに安定 ID が乏しく未対応。fallback の順序非依存化は将来課題（実益小）。

### P1-D. integrity-check（admin CLI）— ✅ 実装済み（2026-06-22）
- **現状**: 完了。`db.integrity_check()`（読み取り専用、`{ok, checks, problems}` を返す）
  + `python -m app.admin integrity-check`（問題ありで exit 2）。検査項目:
  ① `PRAGMA integrity_check` ② orphan messages ③ FTS 件数整合（`messages_fts_docsize` で
  実 indexed 件数を比較）＋ FTS5 `integrity-check` 構造検査 ④ stable ID 重複
  （`(source, source_id)`）⑤ blank source/source_id ⑥ orphan attachments（テーブルが
  存在する場合のみ＝P1-H 未実装なので現状スキップ）。`tests/test_integrity.py`（5件）。
- **受入基準（達成）**: 正常 DB で `ok=True`・problems 空 / orphan・FTS desync・blank source を
  故意に壊して検出 / admin の exit code（0/2）/ 読み取り専用（FTS integrity-check の暗黙
  トランザクションは rollback で閉じる）/ backend test 73 passed。
- **学び（NOTES.md）**: `COUNT(*) FROM messages_fts` は content table に proxy し desync を
  検知できない → `messages_fts_docsize` を使う。
- **リスク**: 低（読み取りのみ）。
- **依存**: なし（P1-C 後で stable ID 検査が有効）。

### P1-E. backup（admin CLI）— ✅ 実装済み（2026-06-22）
- **現状**: 完了。`db.backup(out_path=None)`（checkpoint(TRUNCATE) → copy → 0600、
  デフォルト `<db>.backup-<stamp>`）を追加し、`python -m app.admin backup [--out PATH]` で
  実行。既存 redact-apply の inline バックアップを `db.backup()` に共通化（cmd_apply 経由の
  既存テストも通過）。復元は backup を戻すか `CAIRN_DB` を向ける。`tests/test_backup.py`（3件）。
- **受入基準（達成）**: backup を別 DB として開いて検索・get_conversation・integrity_check が
  通る（原本を破壊しても backup は独立コピー）/ 0600 / backend test 76 passed。
- **リスク**: 低。
- **依存**: なし。

### P1-F. export JSONL（admin CLI / API）
- **やること**: conversation/message/metadata を機械可読 JSONL 出力。
  source / date range / conversation ID で絞り込み。原本と派生データを区別
  （現状派生は無いので将来拡張に備えた構造）。
- **受入基準**: 出力から会話本文・日時・source を再構成できる / フィルタが効く。
- **リスク**: 低〜中（大量データのストリーミング出力に注意。limit/段階出力）。
- **依存**: なし。

### P1-G. export Markdown（admin CLI / API）
- **やること**: スレッド単位の人間可読 Markdown 出力。フィルタは P1-F と共通化。
- **受入基準**: 1 会話が読みやすい Markdown になり、source/日時が確認できる。
- **リスク**: 低。
- **依存**: P1-F（フィルタ・取得層を共有）。

### P1-H. attachment metadata
- **やること**: `attachments` テーブル（conversation_id/message_id, source_path,
  mime, size, hash, extracted_text 別管理用フィールド）。パーサーが添付メタを拾えるものは
  関連付け、無くても本文取り込みは失敗させない。
- **受入基準**: 添付メタが会話/メッセージに紐づく / 添付欠損でも会話は取り込める /
  将来の OCR/PDF 抽出用に派生テキストを別管理できる構造。
- **リスク**: 中（各パーサーの添付表現が source ごとに異なる。CLI ログは
  添付参照が乏しく、まず schema + チャット系から）。
- **依存**: P1-A。実データ調査が前提（NOTES.md の方針通り）。

---

## 3. 推奨実装順序

```
P1-A (migration 土台)
  └─ P1-C (stable IDs + idempotency)   ← 安定 ID は後続の検査/export の前提
  └─ P1-B (import history)
P1-E (backup)      ┐ 独立、いつでも可
P1-D (integrity)   ┘ P1-C 後が望ましい
P1-F (export jsonl) → P1-G (export markdown)
P1-H (attachment)  ← 実データ調査を伴うため後半
```

最初の 1 タスクとしては **P1-A**（migration 土台の完成）を推奨。理由:
- 既に骨格があり差分が小さい＝低リスクで「完了の定義」を満たしやすい。
- 以降のすべての schema 変更（B/C/H）の安全な前提になる。

---

## 4. 横断的リスク / 注意点

- **fallback source_id の変更（P1-C）は破壊的になりうる**: 既存 DB の `index-{i}` /
  `path` 由来 source_id を変えると再取り込みで重複する。migration で旧→新 ID の
  付け替え、または旧データ削除＋再構築の方針を明示すること。
- **redaction choke point を壊さない**: `upsert_conversations` は全 ingest の単一
  チョークポイントかつ content_hash 前に redact する。ここに列追加・履歴記録を入れる際は
  この不変条件（redact → hash → 比較）を維持する。
- **FTS 外部コンテンツの整合**: messages を直接 UPDATE/DELETE する処理を足す場合、
  トリガ（ai/ad/au）の前提を確認。バルク変更後は `rebuild` を検討（admin.py 参照）。
- **後方互換**: 既存 import/search/Web UI/MCP を壊さない。API/MCP の破壊的変更は新旧併存。
- **テストの DB 分離**: `TestClient(app, base_url="http://127.0.0.1")` でないと Host 検証で
  403 になる（NOTES.md）。`CAIRN_DB` を tmp に向けて隔離する既存パターンを踏襲。
- **`on_event("startup")` の DeprecationWarning**: FastAPI が lifespan 移行を促している
  （テストは通るが将来対応。Phase 1 のスコープ外、別途）。

---

## 5. Phase 1 受入基準（ROADMAP §4.3 再掲・現状対応）

| 基準 | 現状 | 達成タスク |
|---|---|---|
| 同入力の複数取り込みで重複しない | ✅ P1-C 実装済み（test_idempotency） | P1-C |
| message に安定 ID を持たせる | ✅ P1-C（source_message_id、あるソース） | P1-C |
| migration 後も既存データを検索・表示できる | ✅ P1-A 実装済み（test_migrations） | P1-A |
| import 履歴（counts/warning/source）を確認できる | ✅ P1-B 実装済み（import_runs） | P1-B |
| backup → 別 DB として復元できる | ✅ P1-E 実装済み（test_backup） | P1-E |
| JSONL/Markdown export | ❌ | P1-F / P1-G |
| 破損行・未知フィールドでも可能範囲を取り込み warning | ✅ | （実装済み・維持） |
| backend test 全通過 | ✅ 53 passed | （各タスクで維持） |
| UI 変更あれば frontend build 通過 | n/a（本タスクは UI 変更なし） | — |
