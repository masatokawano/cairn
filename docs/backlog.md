# Cairn 作業候補（backlog）

最終更新: 2026-07-11

**今なにをすべきかの正典は DESIGN.md §7（マイルストーン）**。本ファイルはマイルストーン外の
細かい作業候補の置き場。現況の詳細な棚卸し・残課題・拡張設計は
[`status-2026-07-06.md`](status-2026-07-06.md) を参照（本ファイルより詳しい）。

## 直近の到達点（2026-07-06 時点）

- DESIGN.md v1.1 の M0〜M6①② 完了（M0 items レジストリ → M1 コネクタ → M2 横断インデックス
  → M3 Obsidian/launchd → M4 週次レビュー v2 → M5 MCP → M6 失敗通知 + rebuild 実測）
- backend test **505 passed**、schema **v12**
- 実データ: conversations 1,875（+ karakeep 94 / zotero 95）、item_links 1,143、
  launchd 稼働・失敗ゼロ、週次レビュー W27/W28 生成済み、MCP 登録済み
- **残: M6③のみ** = 実運用評価（〜2026-07 下旬）+ 件数上限/related チューニング。
  この期間は機能追加をしない（status doc §4 R2 の評価手順に従う）
- 2026-07-10: 外部レビュー第1段階の修正6件を反映（weekly 対象週ルール / Karakeep full sweep
  自動昇格 + prune / MCP title フェンス + meta ホワイトリスト / 会話同期の flock 直列化 =
  D12 / README 実態化）。DESIGN.md は v1.2
- 2026-07-11: 健康ドメイン批准 + H0/H1 実装（ADR-0005 / DESIGN.md v1.3 D13 / 凍結例外）。
  `backend/app/health/`（独立 DuckDB ストア、labs CSV 縦切り、init/doctor/import/status/
  report labs）、テスト 38 件追加。cairn.db 非接触
- 2026-07-11: H2 イベント台帳実装。events YAML importer（append-only + supersedes 訂正、
  不確実日付は原文 + earliest/latest 区間で捏造なし）、overlay / before-after 窓の factual
  比較、`report event-response`、store schema v2（premigrate 自動バックアップ付き独立
  migration 初稼働）。テスト 19 件追加で **572 passed**
- 2026-07-12: H1 実データ検証（実検査シート 729観測、全 valid）+ 実フォーマット/カタログ拡張
  （PR #6）。H3 Apple Health 実装: streaming XML（allowlist 8型、位置情報除外、instant/
  interval、睡眠区間長）、`COPY FROM CSV` バルクロード（DuckDB per-row INSERT が実用外の
  ため。~5,700行/s・メモリ一定）、日次/週次/データ品質集計。catalog 2026.07-3（PR #7）
- 2026-07-12: H4 医療文書レジストリ実装。schema v3（documents）、不変スナップショット +
  抽出ライフサイクル（none→draft→verified、verified は明示操作のみ。OCR 自体は後続）、
  壊れた参照検出（`report broken-refs` + doctor の provenance_intact）、`import document` /
  `document attach-text|list`。テスト 15 件追加で **602 passed**。実機 store は v3 へ migrate 済み
  残: 実データ検証（実 iPhone export）→ H5 レポート配信（**凍結明け** + allowlist 第4カテゴリ + H5-P1）

## 採用基準

- **規模**: 30 分（XS） / 1-2 時間（S） / 半日-1日（M） / 数日（L）
- **価値**: ユーザー実利・運用安定性・将来作業のブロック解除の観点
- **依存**: 他の候補に先行されるか
- **凍結期間**: M6③ 完了までは、評価に必要な作業（データ穴埋め・後始末）以外に着手しない。
  例外（2026-07-11 オーナー決定）: health ドメインの設計批准と H0/H1 実装（ADR-0005）は
  `cairn.db`・既存パイプラインに一切触れないため凍結対象外。Cairn 統合に触れる H5 以降は
  凍結明け + 個別レビュー後

---

## A. 運用・残務（M6③ 期間中も可）

| # | 候補 | 規模 | 依存 | 価値 |
|---|---|---|---|---|
| A0 | **データ穴埋め**: Gemini Takeout 取り込み + claude/chatgpt export 更新 + `cairn index rebuild` 1 回 | XS(人間) | — | **最優先**。S5 の材料が揃う（status doc R1） |
| A1 | `admin backup --with-blobs` — `data/attachments/` を colocate | XS | — | blob store 後は backup が不完全 |
| A2 | `import_runs.failed` の正しい計上 — parse 例外が warning 扱いで failed=0 | XS | — | メトリクス精度 |
| A3 | per-parser PARSER_VERSION — 現状 suite 単位 | S | — | 部分再 ingest が可能になる |
| A4 | bearer token 認証 — SECURITY 残課題 | S/M | — | ローカル運用では不要のまま |
| A5 | API/UI 経由の export — 現状 admin CLI のみ | S | — | UX 向上 |
| A6 | httpx deprecation warning 解消 | XS | — | テスト出力の警告を消す |
| A7 | `temp/`（547MB）・premigrate/backup 累積・旧 Vault 複製・旧 brain-sync 実体の削除 | XS | **全て不可逆＝個別承認** | disk と平文残存の解消（status doc R7 の表） |
| A8 | backup ローテーション — N 個保持 + 自動削除 | XS | — | A7 とセット |

## B. M6③ のチューニング対象（評価データが揃ってから）

| # | 候補 | 規模 | 依存 | 価値 |
|---|---|---|---|---|
| B1 | build_context_pack evidence バケットの相対スコア閾値（空バケット許容） | S | 評価 | S5 精度（status doc R4） |
| B2 | related() semantic アームの距離閾値・ノイズタイトル規則拡充 | S | 評価 | S2 精度（status doc R5） |
| B3 | 件数上限の調整（各節 ≤10 の見直し） | XS | 評価 | S1（§7 M6 の規定範囲） |

## C. M6③ 完了後の候補（Decision Record 改訂不要）

status doc §5.1 の詳細参照。

| # | 候補 | 規模 |
|---|---|---|
| C1 | 定期 reindex エージェント（**§5.7 の 1 行改訂が先**） | S |
| C2 | `/api/healthz` | XS |
| C3 | 検索レイテンシ簡易メトリクス | S |
| C4 | CI（pytest + 依存監査 + frontend build） | S |
| C5 | 添付 download API + viewer UI | S |
| C6 | `admin` CLI の `cairn admin` への統合（§5.7 M6 検討事項） | M |
| C7 | 複数 Mac の claude/codex ログ集約（端末 ID 付き staging 方式。外部レビュー §4.1） | M |
| C8 | ✅ 決定（2026-07-11）: Vault 同期は **Syncthing**（Mac 間双方向）+ 受信専用レプリカ（iPhone）。iCloud Drive は不採用 — vault は D9 により TCC 保護ディレクトリ外（`~/Obsidian`）に置く前提で、iCloud に移すと launchd の headless ジョブが TCC 権限プロンプトを出せず D9 の意図（`/bin/bash` の FDA 廃止）に逆戻りする上、オンデマンドダウンロードで未実体化ファイルを索引する恐れがあるため。Cairn 側のコード・設定は無変更（`CAIRN_OBSIDIAN_VAULT` は `~/Obsidian` のまま） | XS |

## D. Decision Record / DESIGN.md 改訂が必要な拡張

status doc §5.2 参照（pin/mute、会話添付 OCR、Karakeep 本文抜粋拡充、MCP ツール追加、
外部 embedding provider、週次レビュー書式改訂）。**実装ではなく改訂提案から始めること。**

## E. 旧 Phase 3 系（凍結・着手禁止）

旧 backlog にあった Phase 3 抽出パイプライン系の候補（P3-A〜G のバックフィル・拡張）は
**DESIGN.md D2 により凍結**（deprecated in place）。再提案しない。実装済みコード・テスト・
Review UI は温存するが、新規投資・依存・バッチバックフィルを行わない。
必要になったら Decision Record の改訂提案から。

---

## 着手・更新の約束

- 着手前にこのファイルと DESIGN.md §7 / §8 を読み、候補から選ぶ
- 完了したら該当行を削除（または「✅ 完了済み: commit XXX」に書き換え）
- 新しい候補に気付いたら追記。大きく事情が変わったら最終更新日を直して全体を見直す
- **マイルストーン完了時は ROADMAP.md の状態表も更新する**（2026-07-06 の追従漏れの教訓）
