# ADR 0001 — Vector storage for semantic search

- **Status**: Accepted
- **Date**: 2026-06-24 (Proposed) / 2026-06-24 (Accepted)
- **Deciders**: project owner (Masato)
- **Related**: ROADMAP §5.3 P2-2、[docs/phase2-design.md](../phase2-design.md)

ROADMAP §5.3 P2-2 が「候補を比較し ADR に記録すること」と明記している。
本 ADR は Cairn のベクトル保存先を確定するための比較表と推奨を記録する。

---

## 1. Context（背景と制約）

- Cairn は **ローカルファースト**の個人アーカイブ。SQLite 1 ファイル（`cairn.db`）に
  原本＋派生をまとめている。
- データ規模は**個人利用想定**: chunks は最大数万〜十数万程度を見込む（数 GB 級の
  巨大コーパスは想定外）。
- 既存検索は **FTS5 trigram**。これを置き換えず、semantic 検索を**独立した経路として
  追加**する（[phase2-design §1.4](../phase2-design.md)）。
- 配布: 開発者は macOS（Darwin、Apple Silicon を含む）。Python 仮想環境 + pip で完結
  させたい（Homebrew や OS パッケージへの依存は避ける）。
- バックアップ: 既存の `admin backup` は `cairn.db` を `backup` API で複製する。
  vector store もこの**単一ファイル backup で完結**することが望ましい。
- セキュリティ: vector store は本文の chunk text を保持するため、redact 適用済みの
  入力だけを書き、`secure_delete=ON` の SQLite 設定と整合させたい。
- 再構築可能性: `cairn.db` を消しても原ログから再生成できる前提（ROADMAP §2.3）。

### 1.1 Decision drivers（評価軸）

ROADMAP §5.3 P2-2 が挙げる 4 軸に、Cairn の現実から 3 軸を追加:

| 軸 | 重み | 備考 |
|---|---|---|
| 個人アーカイブ規模での性能 | ★★★ | 数万 vector × k=10 検索が体感即時（< 200 ms）であれば十分 |
| 配布容易性（pip / macOS arm64） | ★★★ | ビルド済み wheel が arm64 macOS にあるか |
| バックアップ容易性 | ★★★ | `cairn.db` 1 ファイル backup で済むか |
| テスト容易性 | ★★ | CI / pytest で起動できるか、軽量 fixture が組めるか |
| 依存追加の小ささ | ★★ | torch 等 GB 級の依存を引き込まないか |
| 既存 FTS5 との同居 | ★★ | 同一 SQLite ファイル内で衝突しないか |
| メンテナンス活発度 | ★ | 個人プロジェクトなのでメンテ放棄リスクは無視できない |

---

## 2. Options（候補）

### A. `sqlite-vec`

- alex garcia 製の SQLite 拡張。`sqlite-vss` の後継として活発に開発が継続。
- 純粋 C 実装、外部ランタイム依存なし。Python パッケージ `sqlite-vec` で macOS arm64
  含む wheel を配布。
- `vec0` virtual table で `vector BLOB(N)` 列を持つ表を作り、`MATCH` でブルートフォース
  KNN を提供。
- データは**同じ `cairn.db` 内の virtual table**に格納できる → 1 ファイル backup OK。
- API: `import sqlite_vec; sqlite_vec.load(conn)` で `LOAD EXTENSION` 相当。

### B. `sqlite-vss`

- 同じ作者の旧バージョン。Faiss を内部で利用するため、Apple Silicon の wheel が
  歴史的に不安定で、**作者本人が sqlite-vec への移行を案内**している。新規採用は非推奨。

### C. Python 側で BLOB 保存 + numpy cosine（拡張ロードなし）

- `embeddings.vector` を BLOB として持ち、検索時に**全件 SELECT → numpy で内積**。
- 拡張ロード不要。SQLite は素のまま。
- 数千 chunk 規模なら数十 ms で完了。**数万を超えると線形時間がボトルネック**になる
  可能性あり（dimension 384 × 50000 vector ≈ 75 MB のメモリ転送）。
- 利点: 依存 numpy のみ。デバッグ・テストが極めて容易。バックアップ自動的に 1 ファイル。

### D. 別ローカル vector store（Chroma / LanceDB / FAISS / Qdrant local）

- 独立した DB / プロセスを持つ。
- バックアップが**別ディレクトリ**になり、`admin backup` の前提（DB 1 ファイル）を壊す。
- 依存と起動の複雑さに対し、Cairn の規模では利点が乏しい。
- メンテナンス活発度は Chroma / LanceDB は高いが、Cairn のスコープには過剰。

---

## 3. 比較表

| 軸 | A. sqlite-vec | B. sqlite-vss | C. Python+numpy | D. 別 store |
|---|---|---|---|---|
| 個人規模性能（〜数万） | ◎ ブルートフォースで十分 | ○ Faiss ANN | ○ 〜数千で快適、数万で要計測 | ◎ ANN ありで高速 |
| 個人規模性能（〜十数万） | ○ | ◎ | △ メモリ・時間ともに線形悪化 | ◎ |
| pip / arm64 macOS | ◎ wheel あり | △ wheel が不安定 | ◎ numpy のみ | △ ものによる |
| 単一ファイル backup | ◎ `cairn.db` 内 virtual table | ◎ 同上 | ◎ BLOB 列のみ | ✗ 別ディレクトリ |
| テスト容易性 | ○ 拡張ロードは fixture で 1 行 | △ | ◎ | △ 別プロセス起動が必要 |
| 依存追加 | ◎ C 拡張のみ、純粋 | △ Faiss | ◎ numpy のみ | ✗ 重い |
| FTS5 同居 | ◎ 同一 DB | ◎ | ◎ | n/a |
| メンテ活発度 | ◎ 現役 | ✗ deprecated 案内 | ◎ 自前 | ◎ |

---

## 4. Decision

**Primary: A. sqlite-vec を採用し、`VectorIndex` 抽象越しに使う。**
**Fallback: C. Python + numpy 実装を抽象の別実装として用意し、拡張ロード失敗時に切替可能にする。**

承認: 2026-06-24（プロジェクトオーナー）。実装は Phase 2-1c で行う。

### 4.1 採用理由

- Cairn の 3 つの非交渉条件「1 ファイル backup」「pip 配布」「FTS5 と同居」を**全て満たす
  唯一の選択肢**。
- 個人規模の十数万 chunk まで brute-force KNN で性能十分。
- 拡張ロードが失敗する環境（古い SQLite ビルド、フラグ無効）でも C の Python+numpy
  実装で**動作継続できる**抽象を入れる。

### 4.2 抽象の輪郭

```python
# app/vector_index.py（提案）
class VectorIndex:
    def add(self, chunk_id: int, vector: list[float]) -> None: ...
    def remove(self, chunk_id: int) -> None: ...
    def search(self, vector: list[float], k: int) -> list[tuple[int, float]]: ...  # (chunk_id, score)
    def rebuild(self) -> None: ...
```

- 実装 1: `SQLiteVecIndex`（`sqlite-vec` の `vec0` virtual table を `cairn.db` 内に作成）
- 実装 2: `NumpyIndex`（既存 `embeddings.vector` BLOB を全件メモリに展開して cosine）
- `db.connect()` で `sqlite_vec.load(conn)` を試み、失敗時は環境変数で
  `VectorIndex` の選択を上書き可能（`CAIRN_VECTOR_INDEX=numpy`）。

### 4.3 不採用の理由（補足）

- **B sqlite-vss**: 作者本人が新規採用を推奨していない（A の後継）。
- **D 別 store**: 「1 ファイル backup」を壊す。個人規模で性能利点も享受しにくい。

---

## 5. Consequences

### 5.1 採用すると…

- Phase 2-1c で `app/vector_index.py` + `app/vector_index_sqlite_vec.py` + `app/vector_index_numpy.py`
  を追加する。
- `requirements.txt` / `pyproject.toml` に `sqlite-vec` を追加。
- `db.connect()` で拡張ロード処理を追加（既存の `PRAGMA` 設定の隣）。
- 拡張ロードが OS や SQLite のビルドオプション次第で**できない環境がある**ことを
  README / SECURITY.md に明記し、その場合は numpy 実装で動くことを補足する。

### 5.2 後悔したくないので…

- ADR Accepted の前に、**実環境（このマシンの macOS arm64 + Python 3.13）で**
  `import sqlite_vec; sqlite_vec.load(conn)` が通ることを **smoke test**で確認する。
- `VectorIndex` 抽象を最初から用意し、決定が**実装の片側に張り付かない**ようにする。

---

## 6. References

- ROADMAP §5.3 P2-2（Cairn 内部資料）
- `sqlite-vec` GitHub（alex garcia）
- `sqlite-vss` の deprecation 案内（同上）
- 旧版（sqlite-vss）に関する Cairn 内議論は無し（本 ADR が初）

---

## 7. Resolved questions（Accepted 時点での確定事項）

1. **採用候補**: A primary + C fallback で決定（2026-06-24）。
2. **依存追加**: `sqlite-vec` の追加は許可。numpy は OK（既存方針通り）。
3. **拡張ロード失敗時の挙動**: **自動フォールバック**を採用する。
   `db.connect()` が `sqlite_vec.load(conn)` を try し、失敗した場合は
   `VectorIndex` を numpy 実装に切り替えてログに警告を残す。環境変数
   `CAIRN_VECTOR_INDEX=numpy` で明示的に numpy 強制も可能とする
   （テスト用 + 拡張ロード環境問題時のエスケープハッチ）。
4. **将来の data 量**: 現時点では個人規模（数万 chunk 想定）で十分。
   sqlite-vec の brute-force KNN がボトルネックになったら別 ADR で ANN への
   upgrade パスを検討する（P2-1c の `VectorIndex` 抽象が後付け差し替えを許容）。
