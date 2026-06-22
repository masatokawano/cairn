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
- **message の安定 ID は DB に存在しない。** `ParsedMessage.source_message_id` は
  claude_cli (`uuid`) / claude_export (`uuid`) で**パースされているが messages テーブルに
  保存されていない**（`db.upsert_conversations` が INSERT する列に含まれない）。
  → P1-1 の「message に安定 ID を持たせる」が未達。

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

### P1-B. import run history
- **やること**: `import_runs` テーブル（source, input_path/filename, started_at,
  completed_at, parser_version, inserted/updated/skipped/failed, warning_summary,
  content_hash）を追加。`upsert_conversations` 経路と CLI sync / `/api/import` で記録。
  parser に version 定数を持たせる。API or admin CLI で履歴参照。
- **受入基準**: import/sync 後に 1 run 行が残る / counts と warning が確認できる /
  既存テスト回帰なし。
- **リスク**: 低〜中（全 ingest 経路に記録を挿す。choke point は `upsert_conversations` だが
  source/path 情報は呼び出し側にあるため引数追加が必要）。
- **依存**: P1-A（schema 追加なので migration 経由が望ましい）。

### P1-C. stable IDs + idempotency 回帰テスト
- **やること**: (1) `messages` に `source_message_id` 列を追加し
  `upsert_conversations` で保存。(2) chatgpt/claude_export の `index-{i}` fallback を
  内容由来ハッシュ（gemini 方式）に置換し順序非依存にする。(3) 「同入力複数回 → 全 skip」
  「1 メッセージ編集 → updated かつ重複なし」の回帰テストを追加。
- **受入基準**: 同じ入力を複数回取り込んでも重複ゼロ / 編集会話が差分更新される /
  message に安定 ID が入る。
- **リスク**: 中（fallback ID 変更は既存 DB の source_id を変える＝再取り込みで重複の懸念。
  既存 `index-{i}` データの移行方針を migration で決めること）。
- **依存**: P1-A。

### P1-D. integrity-check（admin CLI）
- **やること**: `app.admin integrity-check` を追加。`PRAGMA integrity_check` /
  orphan message・attachment / FTS 件数整合（`messages` 件数 vs FTS）/ stable ID 重複 /
  source_id ↔ conversation 参照整合 を検査して JSON 報告。
- **受入基準**: 正常 DB で問題ゼロ / 故意に壊した DB で各問題を検出 / 読み取り専用。
- **リスク**: 低（読み取りのみ）。
- **依存**: なし（P1-C 後だと stable ID 検査が意味を持つ）。

### P1-E. backup（admin CLI）
- **やること**: `app.admin backup [--out PATH]`。checkpoint(TRUNCATE) 後に
  整合性のある単一ファイルをコピー（既存 redact-apply のロジックを共通化）。
- **受入基準**: backup を別 DB として開いて検索・表示できる。
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
| 同入力の複数取り込みで重複しない | △ 基本動作するが回帰テスト薄い | P1-C |
| migration 後も既存データを検索・表示できる | △ 骨格のみ・テスト無し | P1-A |
| backup → 別 DB として復元できる | ❌ | P1-E |
| JSONL/Markdown export | ❌ | P1-F / P1-G |
| 破損行・未知フィールドでも可能範囲を取り込み warning | ✅ | （実装済み・維持） |
| backend test 全通過 | ✅ 53 passed | （各タスクで維持） |
| UI 変更あれば frontend build 通過 | n/a（本タスクは UI 変更なし） | — |
