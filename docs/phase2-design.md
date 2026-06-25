# Phase 2 Design — ハイブリッド検索（chunking + Embedding + Hybrid ranking）

ROADMAP §5「Phase 2 — ハイブリッド検索」の実装計画。Phase 1 で言うところの
`docs/architecture-audit.md` に相当する**スタートライン文書**であり、コードを
変更する前に「何を、どの順で、どこまでやるか」を確定する。

- 作成日: 2026-06-24
- 対象コミット: `03e3d2e`（main、Phase 1 完了 + claude_export/gemini 実検証済み）
- ベースライン: `backend/.venv/bin/python -m pytest tests/ -q` → **99 passed**
- frontend build: 未実行（このタスクはコード変更なし）

> 関連 ADR: [`docs/adr/0001-vector-storage.md`](adr/0001-vector-storage.md)
> （ベクトル保存先の選定。ROADMAP §5.3 P2-2 が ADR で決めることを明記）

---

## 1. 現状アーキテクチャの確認（Phase 2 の前提）

### 1.1 検索パス（既存）

- `db.search(q, ...)` がエントリポイント（`backend/app/db.py:400`）
- クエリ全タームが 3 文字以上 → **FTS5 trigram + bm25**。
  どれか 1 つでも < 3 文字 → **LIKE フォールバック**（個人アーカイブ規模では十分）。
- `snippet()` / `bm25()` は FTS サブクエリ内、`ROW_NUMBER() OVER (PARTITION BY c.id ORDER BY rank)`
  でウィンドウ関数、`LIMIT/OFFSET` を外側、と 3 層に分離。
- 結果は **会話単位**（1 会話につき best-hit 1 件 + matched message id）。
- API: `GET /api/search?q=...&source=...&limit=...&offset=...`
- MCP: `mcp__cairn__search_conversations(query)` — **mode 引数は無し**（keyword のみ）。

### 1.2 派生データの現状

- **embeddings / chunks / segments / assertions のテーブルは存在しない。**
- attachments は P1-H で導入済み（metadata + `extracted_text` 予約席あり、現状未使用）。
- `iter_export_conversations` は出力スキーマで `derived: {}` を予約済み（P1-F）。
  Phase 2 で `derived.chunks` / `derived.embeddings` を埋める想定。

### 1.3 スキーマ・migration の現状

- `_SCHEMA_VERSION = 4`。P1-H で `attachments` が追加された最後の変化。
- Phase 2 で新規 table を追加すると **v5 以降の migration が必要**。
  Phase 1 P1-A の枠組み（新規 DB は `_SCHEMA` に直接スタンプ、既存 DB は `_MIGRATIONS`
  を順に適用、migration 前に自動 backup）をそのまま使う。

### 1.4 Phase 2 で守るべき不変条件

- **FTS5 を置き換えない**。既存 keyword 検索は完全に動作し続ける（ROADMAP §5.2）。
- redaction choke point（`db.upsert_conversations` → content_hash 計算前の redact）を
  壊さない。新しく Embedding を計算する経路でも、入力テキストは redact 済みである必要がある。
- `cairn.db` 削除 → 原ログとエクスポートから再構築できる前提を維持する（ROADMAP §2.3）。
  embeddings は**派生データなので削除・再生成可能**であること。

---

## 2. Phase 2 の目的と実装原則

ROADMAP §5.1 / §5.2 を再掲し、Cairn の現実に即して具体化する。

### 2.1 目的

- 正確な語句・固有名詞 → **keyword 検索**（FTS5、現行）。
- 表現が違う関連対話 → **semantic 検索**（新規、embeddings ベース）。
- 既定値は**両方を組み合わせた hybrid**。各結果に「なぜヒットしたか」を返す。

### 2.2 実装原則

| 原則 | Phase 2 での具体化 |
|---|---|
| FTS5 を置き換えない | `messages_fts` はそのまま。semantic 検索はそれと**独立した経路**として追加する。 |
| Embedding provider 抽象化 | `EmbeddingProvider` インターフェース。最低 1 つは**ローカルモデル**を実装。`provider`, `model`, `dimension` を `embeddings` 行に保存。 |
| ローカル優先・外部 opt-in | デフォルト provider はローカル。外部 API は環境変数で明示 opt-in、送信範囲（redact 後の chunk text）を README/SECURITY.md に明記。 |
| Embedding は派生で再生成可能 | `chunks` / `embeddings` テーブルを `messages` から再生成できる関数を持つ。`admin reindex` 相当のコマンドを用意。 |
| 後方互換 | `db.search()` のシグネチャは additive（`mode` 引数を kw-only で追加）。MCP も同様。Web UI は既定 mode 切替で keyword 維持。 |

---

## 3. データモデル提案

### 3.1 `chunks` テーブル（P2-1a）

```
chunks(
  id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  idx INTEGER NOT NULL,             -- chunk 内順序（1 message を複数 chunk に分割した場合）
  start_offset INTEGER NOT NULL,    -- 元 message.text 上の char offset（含む）
  end_offset INTEGER NOT NULL,      -- 元 message.text 上の char offset（含まない）
  text TEXT NOT NULL,               -- chunk 本文（redact 済み）
  kind TEXT NOT NULL DEFAULT 'message_text',  -- "message_text" | "attachment_text"
  chunking_version TEXT NOT NULL,   -- "v1-char1500-overlap200" など
  created_at TEXT NOT NULL
);
CREATE INDEX idx_chunks_msg ON chunks(message_id);
CREATE INDEX idx_chunks_conv ON chunks(conversation_id);
CREATE INDEX idx_chunks_version ON chunks(chunking_version);
```

- `chunking_version` を**列に持つ**ことで、algorithm 変更時に旧 chunk を残したまま
  新 chunk を生成 → 旧を削除する**段階的な再生成**が可能。
- `kind="attachment_text"` の chunk は `message_id` を介して該当メッセージに紐付け、
  attachments テーブルの `extracted_text` を chunking 対象に含める将来拡張用。
  Phase 2 の最初は `message_text` のみで開始。

### 3.2 `embeddings` テーブル（P2-1b）

```
embeddings(
  id INTEGER PRIMARY KEY,
  chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
  provider TEXT NOT NULL,           -- "local-sbert" | "openai" など
  model TEXT NOT NULL,              -- "intfloat/multilingual-e5-small" など
  dimension INTEGER NOT NULL,
  vector BLOB NOT NULL,             -- f32 little-endian, dimension 個
  created_at TEXT NOT NULL,
  UNIQUE(chunk_id, provider, model) -- 同一 chunk × provider×model で 1 つ
);
CREATE INDEX idx_embeddings_chunk ON embeddings(chunk_id);
CREATE INDEX idx_embeddings_provider_model ON embeddings(provider, model);
```

- 同一 chunk に対し**複数 provider/model を共存**できる（移行期や A/B 用）。
- vector 検索インデックスは ADR-0001 の決定に従う（sqlite-vec の virtual table を別に
  作るか、Python 側 cosine か）。

### 3.3 chunking algorithm v1（採用・実装済み）

> 実装: `backend/app/chunking.py`。提案値（MAX_CHARS=1500 / OVERLAP=200 / paragraph
> 境界優先）をそのまま採用し、`chunking_version = "v1-char1500-overlap200"`。
> 短いメッセージの結合は v1 では行わず、空文字・空白のみの message は chunk 0 件。


- 単位: **message が原則 1 chunk**。
- ただし `len(message.text) > MAX_CHARS`（提案: 1500）のときは分割する。
  - スライド窓: 窓幅 1500 文字、overlap 200 文字。
  - 段落境界（連続改行 2 つ以上）を best-effort で優先的にスライス位置に選ぶ。
  - 各 chunk に `start_offset` / `end_offset` を **元 message 上の char offset**で記録。
- 短いメッセージ（< 50 文字、提案）は前後の同 role 連続メッセージと**結合せず**、
  独立した chunk として保存する。Phase 2 のシンプル開始ライン。
- 添付 `extracted_text` は v1 では扱わない（P2-1c 以降で `kind="attachment_text"`）。
- `chunking_version = "v1-char1500-overlap200"`。algorithm 変更時はこの文字列を変える。

> chunk サイズ 1500・overlap 200 は提案値。多言語埋め込みモデルの典型コンテキスト
> （512 トークン ≈ 1000-1500 文字）に収めつつ、文脈の連続性を最低限保つ妥協値。
> 実装時にローカルモデル選定と合わせて再調整する余地あり。

---

## 4. サブタスク分解（垂直スライス、1 セッション 1 タスク）

ROADMAP §5.3 の P2-1〜P2-5 を、垂直スライス（schema → repo function → API/CLI/MCP → test → docs）
で実装可能な小タスクに分解する。

### P2-1a. chunking スキーマ + v1 algorithm

- schema: `chunks` テーブル + migration v5
- repo: `db.rechunk_messages(message_ids=None, chunking_version=CURRENT_VERSION)` を追加
  - 既存 messages から chunks を生成（既存 chunks は same chunking_version なら skip）
  - `upsert_conversations` 内で新規/更新 message について自動 rechunk
- algorithm: `app/chunking.py` を新設、`chunk_text(text) -> list[Chunk]` を export
- CLI: `admin rechunk [--all] [--version-mismatched]`
- test: 短い msg・長い msg・改行混じり・char offset 復元の round-trip
- docs: NOTES.md / phase2-design.md 更新
- **受入**: 既存 99 tests pass + 新規。chunking_version=v1 で全 message 分の chunks が
  生成可能。chunks から元 message.text を offset で復元できる（overlap 部除く）。

### P2-1b. EmbeddingProvider 抽象 + ローカル provider 実装

- schema: `embeddings` テーブル + migration v6
- repo: `db.embed_chunks(chunk_ids=None, provider=...)` / `db.find_similar_chunks(vector, k, ...)`
- provider: `app/embedding/__init__.py` に `EmbeddingProvider` ABC、
  `app/embedding/local_sbert.py`（or 同等の軽量ローカルモデル）を 1 実装。
- 外部 API provider は P2-1b では実装しない（インターフェースだけ用意して P2-1d で追加）。
- CLI: `admin reindex [--provider X] [--model Y] [--all] [--missing]`
- test: 小さい vector を fixture provider で生成、find_similar が cosine 上位を返す
- **受入**: provider 1 つで全 chunks に embedding が付く、`cairn.db` 削除→再生成可能、
  既存テストを壊さない。

### P2-1c. vector index 実装（ADR-0001 に従う）

- ADR で決定した保存先（sqlite-vec / Python+numpy / その他）を実装。
- abstraction: `app/vector_index.py` に `VectorIndex` 抽象（add / search / rebuild）。
- バックエンド 1 つを実装し、必要なら fallback を用意。
- test: 1000 vector で k=10 検索の正答率と速度を簡易計測。
- **受入**: `db.find_similar_chunks` が ADR 採用バックエンドで動く。

### P2-1d. 外部 API provider（opt-in）

- OpenAI / Voyage / Cohere 等の 1 つを実装。`CAIRN_EMBED_PROVIDER` 環境変数で切替。
- README/SECURITY.md に「外部 API への chunk 送信」リスクを明記。
- **受入**: 環境変数で provider 切替可、未設定なら local provider のまま動作。
- このタスクはオプション（個人用途でローカル model で性能十分なら省略可）。

### P2-2. ハイブリッドランキング

- `db.search(q, mode="keyword"|"semantic"|"hybrid")` を kw-only で追加。
- semantic: クエリを provider で embed → vector index で k 件 → 会話単位に集約。
- hybrid: keyword 結果と semantic 結果の **RRF (Reciprocal Rank Fusion)** で統合。
- 返却に `matched_keywords` / `semantic_score` / `match_reason` を追加。
- API: `GET /api/search` に `mode` クエリパラメータ追加（デフォルトは hybrid または keyword）。
- **受入**: 3 mode が動作、既存呼び出しは破壊しない（mode 省略時は既定 mode）、
  各結果に「なぜヒットしたか」が含まれる。

### P2-3. 検索 UI

- `frontend/src/App.tsx` に mode 切替（segmented control / tabs）。
- 検索結果に matched_keywords / semantic_score バッジを表示。
- source / date filter は既存があれば再利用、無ければ追加。
- **受入**: keyword / semantic / hybrid を UI から切替できる、結果スニペットと
  ヒット理由が見える、`npm run build` が通る。

### P2-4. MCP 拡張

- `mcp__cairn__search_conversations(query, mode="hybrid")` のように mode 追加。
- chunk_id range か message id range を返す。
- 件数上限と出力サイズ制限は既存方針を維持。
- **受入**: claude CLI 等から mode 引数を使え、既存呼び出し（mode 省略）は keyword
  互換で動く（または既定値を hybrid に統一する場合は migration 期間を設けて告知）。

---

## 5. 推奨実装順序

```
P2-1a (chunking schema + v1)
  └─ P2-1b (embedding schema + local provider)
      └─ P2-1c (vector index, ADR-0001 の決定に従う)
          └─ P2-2 (hybrid ranking)
              └─ P2-3 (UI)
              └─ P2-4 (MCP 拡張)
P2-1d (external API provider) は P2-1c 後の任意項目
```

- **最初の 1 タスクは P2-1a**（chunking schema + v1 algorithm）。理由:
  - スキーマ追加と algorithm だけで完結し、外部依存ゼロ（重い ML 依存を後回しにできる）。
  - Phase 2 の後続全ての前提（chunk 単位）が確定する。
  - `cairn.db` 削除 → 再構築の不変条件を P2-1a 段階で確認できる。

- **ADR-0001（vector storage）は accepted 済み**（2026-06-24、sqlite-vec primary +
  Python+numpy fallback、`VectorIndex` 抽象越し）。P2-1c はこの決定で実装する。

---

## 6. 横断的リスク / 注意点

- **DB サイズ**: embeddings vector の dimension × chunk 数 × 4 byte で膨張する。
  例: 1500-msg × 平均 1.2 chunks/msg × 384 dim × 4 byte ≈ **2.6 MB**。個人規模では問題なし。
  数万 msg 規模で sqlite-vec ANN なしの brute-force cosine がどこまで現実的か、
  P2-1c でベンチマークする。
- **依存追加の重さ**: `sentence-transformers` は torch 経由で 1GB 級の依存を引き込む。
  ONNX 版 + onnxruntime、または mlx / llama.cpp embed といった軽量経路を P2-1b で検討。
- **redaction との合流点**: chunk text は `message.text` をそのままスライスするので、
  既に redact 済み。新規 chunking 時に「再 redact」は不要だが、過去 chunks が古い redact
  ルールで作られているケース（ルール変更時）に再生成が必要 → `admin rechunk --all` で対応。
- **後方互換**: `db.search()` の戻り値 dict にキー追加（`matched_keywords` 等）するのは
  additive で OK。**既存キーの意味を変えない**。MCP の戻り値も同様に拡張。
- **migration テスト**: v5 / v6 で `_MIGRATIONS` を追加するたびに `test_migrations.py` を
  拡充（既存 v1→v4 と同形）。
- **CI 時間**: ローカル model を test fixture で fake する（ハッシュベースの dummy embedder）。
  実 model のロードはテストでは行わない。

---

## 7. Phase 2 受入基準（ROADMAP §5.4 再掲・タスクへのマッピング）

| 基準 | 達成タスク |
|---|---|
| 固有名詞は keyword 検索で正確に見つかる | 既存（維持） |
| 表現が違う同一テーマは semantic/hybrid で見つかる | P2-1b / P2-1c / P2-2 |
| 検索結果から該当 message へ移動できる | 既存（維持） |
| Embedding を全削除して再生成できる | P2-1b（`admin reindex --all`） |
| provider 未設定でも従来検索が完全に動作する | P2-2（mode 省略時の挙動） |
| 外部 API を使わないローカル構成が成立する | P2-1b（local provider が default） |
