# Cairn 作業候補（backlog）

最終更新: 2026-06-30

ROADMAP / phase2-design / architecture-audit / SECURITY / NOTES を横断して、
**今着手可能な作業候補**を一覧化したもの。実装着手前に都度更新する。

## 直近の到達点（2026-06-28 時点）

- Phase 1 & Phase 2 受入基準達成
  - keyword（FTS5 trigram）/ semantic（e5-small）/ hybrid（RRF, k₀=60）の 3 mode 検索
  - sqlite-vec primary + numpy fallback の VectorIndex 抽象（ADR-0001 Accepted）
  - UI に mode 切替・match_reason / matched_keywords / semantic_score バッジ
  - MCP `search_conversations(query, mode=...)` 拡張
- ChatGPT 実 export 取り込み済み（1226 会話 / 15323 messages / 添付 1446、blob 292 件）
- backend test **174 passed**

## 採用基準

- **規模**: 30 分（XS） / 1-2 時間（S） / 半日-1日（M） / 数日（L）
- **価値**: ユーザー実利・運用安定性・将来作業のブロック解除の観点
- **依存**: 他の候補に先行されるか

---

## A. Phase 1 系の残務

短期・独立。Phase 1 + 2 を仕上げて運用安定性を上げる。

| # | 候補 | 規模 | 依存 | 価値 |
|---|---|---|---|---|
| A1 | `admin backup --with-blobs` — `data/attachments/` を colocate | XS | — | blob store 後は backup が不完全。完全バックアップを 1 コマンドで |
| A2 | `import_runs.failed` の正しい計上 — 現状 parse 例外は warning 扱いで failed=0 | XS | — | メトリクス精度。実害なしだが将来のレポート用 |
| A3 | per-parser PARSER_VERSION — 現状 suite 単位 | S | — | 部分再 ingest（特定 parser だけ rev up）が可能になる |
| A4 | bearer token 認証 — SECURITY 残課題 | S/M | — | 共有環境を視野に入れたとき。今はローカルなので不要 |
| A5 | API/UI 経由の export — 現状 admin CLI のみ | S | — | ブラウザから download できると UX 向上 |
| A6 | httpx deprecation warning 解消 | XS | — | テスト出力の最後の警告を消す（pytest filter or httpx2） |
| A7 | `temp/` の整理 — 530MB の export が居座っている | XS | — | gitignored だが disk 圧迫 |
| A8 | backup ローテーション — `cairn.db.{backup,premigrate-*}` の累積管理 | XS | — | N 個保持 + 古い物自動削除 |

## B. Attachments 深耕

P1-J の続編。添付の **テキスト化** が実現すれば semantic 検索の到達範囲が画像・PDF まで広がる。

| # | 候補 | 規模 | 依存 | 価値 |
|---|---|---|---|---|
| B1 | 添付の OCR / PDF 抽出 → `attachments.extracted_text` を埋める | L | OCR バックエンド選定 | **画像・PDF が semantic 検索に乗る**。効果が大きい |
| B2 | 添付の HTTP download API `/api/attachments/{hash}` | XS | — | B3 の前提 |
| B3 | 添付 viewer UI — 会話画面でサムネイル / dl ボタン | S | B2 |会話の文脈に添付が戻ってくる |
| B4 | クロスソース dedupe レビュー | XS | — | claude_export と chatgpt で同じ画像が hash 一致するか検証 |
| B5 | `audio_asset_pointer` (sediment://) の metadata-only 記録 | XS | — | bytes 外でも「音声があった」記録だけ残す |

## C. Phase 2 拡張（オプショナル）

| # | 候補 | 規模 | 依存 | 価値 |
|---|---|---|---|---|
| C1 | P2-1d 外部 API provider — OpenAI / Voyage / Cohere | S | — | `CAIRN_EMBED_PROVIDER` で切替。SECURITY に外部送信明記 |
| C2 | MCP の hybrid デフォルト化 | XS | migration 周知 | mode 省略時の挙動を keyword → hybrid に。後方互換期間が必要 |
| C3 | `admin search-stats` — 利用ログ計測層 | S | — | 後で UX 判断するため。価値が出るのは数週運用後 |

## D. Phase 3（中長期、設計完了・実装未着手）

ROADMAP §6 知識抽出。**設計完了**（2026-06-30、未コミット）:
- [`docs/phase3-design.md`](phase3-design.md) — Phase 3 のスタートライン文書
  （データモデル / 抽出パイプライン / サブタスク分解 / リスク / 受入基準マッピング）
- [`docs/adr/0002-llm-provider.md`](adr/0002-llm-provider.md) — ollama primary +
  外部 API opt-in、`LLMProvider` 抽象（Proposed、smoke test 後に Accepted）

| # | 候補 | 規模 | 依存 | 価値 |
|---|---|---|---|---|
| D1 | ✅ 完了: phase3-design.md + ADR-0002 起草 | L | — | Phase 3 全体の前提（**完了**） |
| D2 | ADR-0002 を Accepted に上げる — ollama smoke test + Qwen2.5-32B 疎通確認 | XS | D1 | LLM provider の最終決定。実機で 20 tok/s + JSON schema が通ること |
| D3 | **P3-A**: `extraction_runs` schema + `LLMProvider` 抽象 + 検証層 + `admin llm-ping` | M | D2 | Phase 3 実装の最初の一歩。LLM を実際に動かさず基盤だけ作る |
| D4 | **P3-B**: rules-based entity 抽出（URL / GitHub repo） | M | D3 | LLM 不要、パイプライン全体を一度通せる |
| D5 | **P3-C**: segment summary（LLM 軽量タスク、Qwen2.5-14B or 32B） | L | D3, D4 | 初の LLM 本番投入。20 会話 sample で品質確認 |
| D6 | **P3-D**: assertion extraction（LLM 重量タスク、Qwen2.5-32B） | L | D5 | actor/kind/status + supporting_message_ids grounding。最大のヤマ |
| D7 | **P3-E**: Review UI（segment/assertion の確認・編集・無効化） | M | D6 | locked_by_user の動作確認の場 |
| D8 | **P3-F**: batch 再生成 + prompt versioning | S | D6 | prompt_version を上げて locked 行が保護されることを確認 |
| D9 | **P3-G**: MCP 拡張（search_segments / list_open_questions 等） | M | D6 | Phase 3 の成果物をエージェントから引ける |

## E. 観測・品質

| # | 候補 | 規模 | 依存 | 価値 |
|---|---|---|---|---|
| E1 | `/api/healthz` — DB / sqlite-vec / embedding model 起動状態 | XS | — | 運用時のサニティチェック |
| E2 | 検索 latency の simple metrics | S | — | mode 別の中央値、`admin stats` で表示 |
| E3 | CI 構築（GitHub Actions） — `pytest -q` + `audit-deps` + frontend build | S | — | 退化検出を自動化 |

---

## 推奨の組み合わせ

- **小物まとめて区切り**: A1 + A6 + A7 + A8（XS × 4、合計 1.5 時間程度）
- **攻める**: D2（ollama smoke test）→ D3（P3-A 基盤）— Phase 3 実装の最初の 1〜2 セッション
- **実用価値最大化**: B1 + B2 + B3 — 添付が検索 + 表示の両方に乗る一連の改善
- **守りを固める**: A4 + E1 + E3 — auth / health / CI で運用面の安心感

## 着手・更新の約束

- 着手前にこのファイルを読み、候補から選ぶ
- 完了したら該当行を削除（または「✅ 完了済み: commit XXX」に書き換え）
- 新しい候補に気付いたら追記
- 大きく事情が変わったら最終更新日を直して全体を見直す
