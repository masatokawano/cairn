# ADR 0002 — LLM provider for Phase 3 extraction

- **Status**: Accepted
- **Date**: 2026-06-30 (Proposed) / 2026-06-30 (Accepted)
- **Deciders**: project owner (Masato)
- **Related**: ROADMAP §6（Phase 3 知識抽出）、[docs/phase3-design.md](../phase3-design.md)

ROADMAP §6 は LLM を用いた抽出（segments / assertions）の実装方針を Phase 3 設計
判断に委ねている。本 ADR は Cairn が使う LLM ランタイムと provider 抽象を確定する。
ADR-0001（vector storage）と同じ構造で記述する。

---

## 1. Context（背景と制約）

- Cairn は **ローカルファースト**の個人アーカイブ（ROADMAP §2.1）。会話本文を
  外部に送らない構成がデフォルトであり続けること。
- Phase 3 で必要なタスクは大きく 2 種類:
  - **軽量**: 会話要約 / topic 候補（短い prompt、短い出力、~14B で十分）
  - **重量**: assertion 抽出（actor/kind/status 分類 + supporting_message_ids
    grounding + JSON schema 準拠、~32B が必要）
  - 詳細は phase3-design §1〜§4 を参照。
- 実行マシン: **Mac mini Apple M4 Pro / 64GB**（unified memory、~56GB 実効）。
- Cairn API サーバーは LaunchAgent で 24/7 常駐
  （`com.masato.cairn`、PID 30108、127.0.0.1:8730）。**LLM はこれと共存する**:
  常時メモリ占有を許さず、抽出バッチ中だけロード → idle で解放できること。
- バックアップ・配布: Cairn 全体が pip + git + LaunchAgent で完結している。LLM
  ランタイムも同様に **`pip install` か単一バイナリ**で導入したい（OS-wide
  パッケージマネージャや Docker 必須にしない）。
- 構造化出力: 抽出タスクは **JSON schema を強制**できる必要がある（phase3-design
  §4.2 検証層の前提）。自由テキストを後処理 parse する方式は脆く採用しない。
- 再現性: 同じ prompt + 同じ model + 同じ seed で同じ出力が出る（ことが望ましい）。
  完全な決定論は LLM では原理的に困難だが、temperature=0 + greedy で十分実用。
- セキュリティ: 取り込み済み会話本文は redact 済みだが、それでも**外部送信は
  オプトイン**で明示する（ROADMAP §2.1）。

### 1.1 Decision drivers（評価軸）

ROADMAP §2.1 / §6 / 既存 ADR-0001 の論理を踏襲しつつ、Phase 3 特有の軸を追加:

| 軸 | 重み | 備考 |
|---|---|---|
| ローカルで完結（外部送信なし） | ★★★ | デフォルト経路の必須条件 |
| Cairn 常駐との共存 | ★★★ | 常時メモリ占有不可。idle 解放できること |
| 構造化出力（JSON schema） | ★★★ | 検証層の前提。生成成功率が直接品質を決める |
| 配布容易性（pip / macOS arm64） | ★★ | `requirements.txt` 1 行 + α で揃うか |
| Qwen2.5-32B Q4 を 10+ tok/s で回せるか | ★★ | バッチコスト概算（phase3-design §4.4）の前提。実測 11 tok/s（M4 Pro 64GB、ollama 0.30.11） |
| Provider 切替容易性 | ★★ | ローカル ↔ 外部 を環境変数 1 つで切替できるか |
| メンテ活発度 | ★ | OSS の場合の話 |
| プロセス分離（OS レベル） | ★ | Cairn API のクラッシュと LLM のクラッシュを分離できるか |

---

## 2. Options（候補）

### A. `ollama` を別プロセスとして使い HTTP で呼ぶ

- 公式バイナリ（macOS / Linux / Windows）。内部は `llama.cpp` + 独自のモデル管理層。
- `http://127.0.0.1:11434` の REST API（`/api/chat`、`/api/generate`、`/api/embeddings`）。
- **JSON mode** あり: `format: "json"` 指定で JSON 強制、`format: <schema>`（2026 時点
  サポート開始）で schema 制約も可能。
- **auto-unload**: idle 数分（既定 5 分）でモデルを RAM から解放。`OLLAMA_KEEP_ALIVE`
  で調整可能。バッチ抽出時にだけメモリを使う運用が自然に成立。
- model 取得: `ollama pull qwen2.5:32b-instruct-q4_K_M` の 1 行。`~/.ollama/models/`
  にキャッシュ。
- Python 側依存: **stdlib の `urllib` または `httpx` のみで十分**。専用 SDK は不要。
- プロセス分離: 別プロセスなので Cairn API がクラッシュしても LLM は生き、逆も同じ。

### B. `llama-cpp-python` を Cairn の Python プロセスに in-process でロード

- `llama.cpp` の Python binding。Cairn の `.venv` に `pip install llama-cpp-python`
  で入る。
- **GBNF grammar** で厳密な JSON schema 強制が可能（ollama JSON mode より制約強い）。
- in-process: モデルを Python プロセス内にロード。**Cairn API プロセスに 18GB+ が
  常駐**するか、抽出のたびにロード/解放（重い）するかの二択。
- auto-unload 機能なし。明示的に `del llm; gc.collect()` で解放、ただし fragment が残る。
- model 取得: GGUF ファイルを HuggingFace から手動 DL（`ollama pull` 相当の便利機能なし）。
- macOS arm64: Metal 対応 wheel あり、ビルドは必要に応じて。

### C. `mlx-lm` を使う（Apple MLX 上の LLM）

- Apple 公式の ML フレームワーク。M シリーズ Neural Engine + GPU を統合的に使い、
  llama.cpp より**速い**（同一モデルで 1.3〜1.5x）。
- in-process（Python 側で `from mlx_lm import generate`）。
- **構造化出力サポートが薄い**: 2026 時点、outlines-mlx などの外部ライブラリで
  対応するが ollama JSON mode ほど安定していない。
- macOS Apple Silicon **限定**。将来 Linux / 非 Apple マシンへ移したら使えない。
- model 配布: MLX 用に変換された GGUF/safetensors が必要。HF に増えてきているが
  ollama のレジストリほどキュレーションは進んでいない。

### D. 外部 API のみ（Anthropic / OpenAI）

- 高品質、構造化出力（Anthropic の `tool_use` / OpenAI の `response_format`）安定。
- **会話本文を外部に送る**（ローカルファースト原則と直接衝突）。
- 課金が発生。1813 conv × 平均 5000 input + 600 output tokens × $0.x/M tokens で
  数 $〜数十 $ のオーダー。個人利用としては痛くないが、原則違反のほうが致命的。
- ローカルの代替が不在の構成は Cairn のアイデンティティに反する。

### E. ハイブリッド: ローカル primary + 外部 opt-in（**実は A or B/C と D の組合せ**）

- デフォルトは A/B/C のいずれか、`CAIRN_LLM_PROVIDER=anthropic:claude-sonnet-4-6`
  のように環境変数で D に切替。
- 抽出単位で provider を変えられる（軽量 → ローカル、重量 → 外部、等）。
- これは「どれを採用するか」というより「**抽象を介して両対応する**」という
  実装方針の問題で、A〜D の選定とは直交する。

---

## 3. 比較表

| 軸 | A. ollama | B. llama-cpp-python | C. mlx-lm | D. 外部 API |
|---|---|---|---|---|
| ローカルで完結 | ◎ | ◎ | ◎ | ✗ |
| Cairn 常駐との共存 | ◎ 別プロセス + auto-unload | △ in-process / 常駐 or 都度ロード | △ in-process | ◎ |
| 構造化出力（JSON） | ◎ format=json / schema | ◎ GBNF（最も厳密） | △ outlines-mlx 等 | ◎ |
| 配布容易性 | ◎ 公式バイナリ + `ollama pull` | △ pip + GGUF 手動 DL | △ pip + 変換済み HF | ◎ API key のみ |
| Qwen2.5-32B Q4 @ M4 Pro | ○ ~11 tok/s (実測) | ○ ~11 tok/s | ◎ ~18 tok/s (推測) | ◎ ストリーミング |
| Provider 切替容易性 | ◎ HTTP shim だけ書けばよい | △ in-process は他 backend と同居しにくい | △ 同上 | ◎ |
| メンテ活発度 | ◎ 2026 時点で現役 | ◎ llama.cpp 本体は活発 | ○ Apple 主導、伸びている | ◎ |
| プロセス分離 | ◎ | ✗ | ✗ | ◎ |
| ロックイン | 低（OpenAI 互換 endpoint も持つ） | 中（pip 依存） | 高（Apple 限定） | プロバイダ依存 |

---

## 4. Decision

**Primary: A. `ollama` を別プロセスとして使い、`LLMProvider` 抽象越しに HTTP で呼ぶ。**
**Opt-in: D. 外部 API（Anthropic を第一実装候補）を同じ抽象の別実装として用意し、環境変数で切替可能にする。**
**不採用: B（in-process 常駐がメモリ要求面で Cairn 常駐と衝突）/ C（Apple 専属はロックインが強く、JSON 安定性も A 未満）。**

### 4.1 採用理由

- A は Cairn の 3 つの非交渉条件「ローカル動作」「Cairn 常駐との共存（auto-unload）」
  「構造化出力（JSON mode）」を**全て満たす唯一の選択肢**。pip 一行で入らない（公式
  バイナリ + `ollama serve`）のは譲歩点だが、`brew install ollama` または
  `curl -fsSL https://ollama.com/install.sh | sh` で導入できる範囲。
- B は GBNF による構造化出力で A よりも厳密だが、**in-process のメモリ要求が
  Cairn API プロセスに乗る**点が致命的。32B Q4 で 18GB、これが LaunchAgent で 24/7
  常駐すると Mac mini 64GB の運用余裕が消える。「都度ロード/解放」は実装可能だが、
  ollama の auto-unload が既に同じ機能を OS プロセス境界で提供している。
- C は速度面で最速だが、JSON schema 安定性が薄く、検証層のリトライ回数が増えると
  実質速度逆転する可能性がある。また Apple 専属ロックインは「将来 Linux サーバーへ
  移す」可能性を完全には否定したくない。
- D は品質最強だが、ローカルファースト原則と相容れない。**抽象の別実装として残し、
  ユーザーが明示的に opt-in したケース**でのみ使う。

### 4.2 抽象の輪郭

```python
# app/llm/__init__.py（提案）

class LLMProvider(ABC):
    name: str                              # "ollama" | "anthropic" | "fixture"
    model: str                             # "qwen2.5:32b-instruct-q4_K_M"

    @abstractmethod
    def complete_structured(
        self,
        prompt: str,
        *,
        schema: dict,                      # JSON schema for output
        system: str | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.0,
    ) -> dict:
        """JSON output validated against schema. Raises ValidationError on mismatch."""

    @abstractmethod
    def estimate_tokens(self, text: str) -> int: ...
```

実装案:

- `app/llm/ollama.py: OllamaProvider`
  - HTTP `POST http://127.0.0.1:11434/api/chat` + `format=<schema>`
  - 起動チェック: `GET /api/tags` で疎通確認、Qwen2.5-32B が pull 済みかも確認
- `app/llm/anthropic.py: AnthropicProvider`（**P3-A では実装しない、抽象だけ用意**）
  - opt-in 経路。`CAIRN_LLM_PROVIDER=anthropic:claude-sonnet-4-6` 等の env で起動
  - 実装は ROADMAP §6 が安定したあと、必要に応じて追加（P3 v1 では不要）
- `app/llm/rules.py: RulesProvider`
  - LLM を呼ばずに rule-based detector を `LLMProvider` の API に合わせて wrap
  - P3-B（rules-based entity 抽出）で使う
- `app/llm/fixture.py: FixtureProvider`
  - 決定論的テスト用。prompt のハッシュから schema 準拠の固定出力を返す
  - sentence-transformers 不要で全テストが動くようにする（P2-1b の FixtureProvider と同じ思想）

### 4.3 Provider 解決順（提案）

`_active_llm_provider()` の解決順（P2-2 の `_active_embedding_provider()` と同形）:

1. `CAIRN_LLM_PROVIDER=name:model` 環境変数（最優先）
2. 既定: `OllamaProvider("qwen2.5:32b-instruct-q4_K_M")`
3. ollama 疎通失敗時: 親切なエラー（`admin llm-ping` の案内付き）を投げる
   （**勝手に外部 API へフォールバックしない**。明示的 opt-in を尊重）

### 4.4 不採用の理由（補足）

- **B llama-cpp-python**: GBNF は魅力だが in-process メモリ要求が Cairn 常駐と
  衝突する。「都度ロード/解放」を自前実装するくらいなら ollama に任せたほうが運用が
  軽い。将来 GBNF 強制 JSON が必須になった場合は、ollama に GBNF が乗れば再評価。
- **C mlx-lm**: 速度最速だが JSON 安定性で劣る。「速いがリトライ回数が多い」より
  「やや遅いが一発で通る」のほうが検証層と相性が良い。Apple 専属ロックインも避けたい。
- **D 外部 API 単独**: ローカルファースト原則違反。opt-in の別実装としてのみ残す。

---

## 5. Consequences

### 5.1 採用すると…

- セットアップ手順に「ollama インストール + `ollama pull qwen2.5:32b-instruct-q4_K_M`」を追加
  （README / SECURITY.md 更新）。
- `requirements.txt` に新規依存は不要（`httpx` は既にある）。
- `app/llm/` パッケージを新規追加: `__init__.py` (ABC) / `ollama.py` / `rules.py` /
  `fixture.py`（+ 将来の `anthropic.py`）。
- `app/extraction/validate.py`（検証層）と組み合わせて使う。
- `admin llm-ping` サブコマンドを追加（ollama 疎通 + model 存在チェック）。
- README に「ollama が動いていないと Phase 3 抽出は実行できない」ことを明記。
- `extraction_runs.provider` / `extraction_runs.model` 列に
  `"ollama" / "qwen2.5:32b-instruct-q4_K_M"` 等を保存。

### 5.2 後悔したくないので…

- ADR Accepted の前に、**実機で**以下の smoke test を通す:
  - `ollama pull qwen2.5:32b-instruct-q4_K_M` が完了する（~18GB）
  - `OllamaProvider.complete_structured()` が JSON schema 制約を満たす出力を返す
  - ✅ 11 tok/s（32B Q4_K_M）/ 24 tok/s（14B Q4_K_M）を実測（2026-06-30）
  - auto-unload（5 分 idle）で RSS が解放される（`OLLAMA_KEEP_ALIVE` 既定）
- `LLMProvider` 抽象を最初から用意し、決定が**実装の片側に張り付かない**ように
  する（ADR-0001 と同じ方針）。
- prompt は外部ファイル（`app/extraction/prompts/*.txt`）に分離し、コード変更なしで
  prompt を差し替えられるようにする（phase3-design §4.3）。

### 5.3 採用しないと…

- ローカル代替が無い構成（D 単独）はローカルファースト原則違反 → Cairn の存在意義に反する。
- B / C を採用すると Cairn API プロセスのメモリプロファイルが読みにくくなり、
  LaunchAgent 運用が脆くなる（OOM kill されると Cairn 全体が落ちる）。

---

## 6. References

- ROADMAP §2.1（ローカルファースト）/ §6（Phase 3 知識抽出）
- phase3-design.md §2 / §4（抽出パイプライン）/ §7.1（プロンプトインジェクション）
- ADR-0001（vector storage 選定）— 同じ「ローカル primary + 外部 opt-in + 抽象越し」
  の判断構造をここでも踏襲
- ollama 公式: `ollama.com`（model registry / JSON mode / KEEP_ALIVE 仕様）
- llama-cpp-python（B 不採用の参考）
- mlx-lm（C 不採用の参考）

---

## 7. Open questions（Proposed 時点で未確定）

以下は ADR Accepted までに smoke test と判断で確定する:

1. **Qwen2.5-32B Q4_K_M を採用するか、Qwen2.5-14B Q4 から始めるか**
   - ✅ **実測（2026-06-30, M4 Pro 64GB, ollama 0.30.11)**:
     - 32B Q4_K_M: **11 tok/s**（想定 ~25 tok/s より遅い。メモリ帯域律速と推測）
     - 14B Q4_K_M: **24 tok/s**（実用速度）
   - JSON mode（format=schema）は両モデルで正常動作を確認。
   - **結論**: P3-C（segment summary）は **14B**、P3-D（assertion）は **32B** の使い分けを採用。
     segment は量が多く（~1800 conv）速度優先で 14B が現実解。assertion は品質優先で 32B。
     バッチコスト概算は phase3-design §4.4 で更新済み。

2. **ollama auto-unload の挙動が Cairn API の DB ロックと干渉しないか**
   - ollama は別プロセスで DB に触らないので問題ないはず。要確認。

3. **外部 API 実装の優先度**
   - P3-A〜D ではインターフェースだけ用意し、実装は Phase 3 v1 では行わない。
   - ユーザーが明示的に「品質再処理を Claude でやりたい」と希望した時点で実装。

4. **prompt の言語**
   - phase3-design §10 で open のまま。実機 AB で決める。

5. **Qwen3 が出たら？**
   - 2026 時点で Qwen2.5 が最新の安定。Qwen3 がリリースされ品質改善が顕著なら
     model 名差し替えのみで対応（`extraction_runs.model` が変わるため再生成の判断は
     prompt_version と同じ流儀で）。
