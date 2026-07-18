# Cairn 作業候補（backlog）

最終更新: 2026-07-11

**今なにをすべきかの正典は DESIGN.md §7（マイルストーン）**。本ファイルはマイルストーン外の
細かい作業候補の置き場。現況の詳細な棚卸し・残課題・拡張設計は
[`status-2026-07-06.md`](status-2026-07-06.md) を参照（本ファイルより詳しい）。
2026 下半期の実行計画（凍結解除ゲート → H5〜H8 → H10/H11 提案の順序・完了条件）は
[`plan-2026h2.md`](plan-2026h2.md)、長期の思弁的方向は [`HORIZONS.md`](HORIZONS.md)。

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
- 2026-07-13: **H5 レポート配信実装（オーナー指示 2026-07-12 により前倒し。凍結が保護する
  cairn.db 評価には非接触 — 90 Auto は索引対象外で還流なし）**。allowlist 第4カテゴリ
  `health`（90 Auto/Health、上書き可）+ AGENTS 不変条件2改訂 + パス検証テスト拡張。
  4レポート（current-status / timeline / lab-trends / data-quality）を `cairn health deliver`
  で手動配信（launchd 自動化はしない）。H5-P1 決定記録（既定除外のまま、opt-in なし。
  Syncthing 索引に載らないことを実機確認）。テスト 10 件追加で **613 passed**。
  events.yml テンプレートを保護 home に設置（ドライラン検証済み）
- 2026-07-13: **H6 AI 解釈と改訂履歴実装**。schema v4（interpretations /
  interpretation_evidence / data_snapshots）。AI draft はローカル ollama
  （qwen2.5、D10）で bounded evidence から生成、model/prompt/snapshot の
  provenance 必須、安全ゲートで診断・服薬変更の断定を保存前拒否、evidence を
  フェンスして指示と構造分離（PRIVACY §7）。accepted は人間 CLI + evidence 必須、
  append-only supersede。供養録（rejected/superseded 一覧）。AI Drafts 配信は
  opt-in（--deliver、同期される旨を警告）。テスト 12 件で **627 passed**。実機 store v4
- 2026-07-13: **H7 read-only Health MCP 実装**。通常 Cairn MCP と分離し既定無効、
  metric 必須 + 行/期間/free-text 上限、event/interpretation は別 opt-in。context pack は
  observation selection/projection・event の hash と source/snapshot provenance を返す。
  合成 store の実 STDIO initialize/list/call と独立 security review を完了。health 133 / backend
  全体 651 tests passed（既知 warning 1）。
- 2026-07-14: **ADR-0006 批准 + ソーシャル取り込み実装**（feature/adr-social-ingest）。
  schema v13（items.kind CHECK に `social_post`、v12 型再構築 migration・id 保存）、
  `parsers/x_archive.py`・`facebook_dyi.py`（公式エクスポートのみ、自作+キュレーション
  限定を regex で構造強制、DYI mojibake 復号、宛先文脈保持）、`cairn import x|facebook`
  （redaction 経由 → 索引 → item_links）、MCP/検索/UI に social_post。DESIGN.md §8
  非目標 10 追記。Codex 独立レビュー approve（SHOULD 2 件反映: FB 本人限定仮定の
  検知カウンタ、X 本文なしスキップ）。backend **691 passed**。
  残: 実データ import（FB は手元、X アーカイブ未入手）は merge + 本番 v13 適用後
- 2026-07-13: **H8 バックアップ/復元/整合性/保持/削除実装**。`app/health/ops.py`:
  tar.gz スナップショット（store+raw+reports+manifest、temp+atomic rename、live store は
  read-only）、restore は counts+hash 検証、verify、rotate（--keep N）、delete-derived
  （再生成可能物のみ）、purge（全 dir 列挙 + 明示確認フラグ必須）。PRIVACY §10 に暗号化
  宛先・保持ポリシー明記。launchd 自動化と実破壊操作は不変条件8で手動。backend 664 passed
  残: 実運用（実データで解釈生成）→ H9 汎用 validation 評価（凍結明け・実運用データ後）

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
| A0 | ✅ 完了（2026-07-12）: Gemini Takeout +6 会話、Claude export +14/更新177、ChatGPT は 6/26 版取り込み済みを確認（1,226 全 skip）。Obsidian 3 ノートは launchd 同期済み。`index rebuild` 実行。S5 の 4 系統すべてに実データあり | XS | — | 済 |
| A1 | ✅ 完了（2026-07-17）: `backup --with-blobs` で `<backup>.attachments/` に兄弟複製（DB とペアで移動・復元） | XS | — | 済 |
| A2 | ✅ 完了（2026-07-17）: failed = パースできなかった入力単位の数（全体例外=1、失敗シャード数）。寛容な per-entry skip は warnings のまま（回帰テストで固定） | XS | — | 済 |
| A3 | ✅ 完了（2026-07-18）: 各会話パーサが `PARSER_VERSION` を宣言、import_runs には `<parser>/<version>` 形式で記録（upload は source='upload' のためパーサ識別を兼ねる）。`force-resync --source claude_cli\|codex_cli` で片側のみの部分再 ingest。x/facebook は import_runs 非記録の一回性 CLI のため対象外 | S | — | 済 |
| A4 | bearer token 認証 — SECURITY 残課題。**見送り（2026-07-17 オーナー決定）**: ローカル運用では不要のまま。共有利用が要件化したら SECURITY.md 残課題（§残課題）として再提案 | S/M | — | 見送り |
| A5 | API/UI 経由の export — 現状 admin CLI のみ | S | — | UX 向上 |
| A6 | ✅ 完了（2026-07-17）: `backend/pytest.ini` の filterwarnings（message 一致）で抑制。httpx2 依存追加は不採用 — 次の意図的な starlette 更新時に再検討 | XS | — | 済 |
| A7 | 一部完了（2026-07-18）: premigrate 5本 + 旧バックアップ6本を削除（`prune_backups(keep=1)` 使用、約3.7GB解放）。**温存（オーナー決定）**: `temp/`（547MB、取り込み元 zip）・旧 Vault 複製・旧 brain-sync 実体 | XS | **残りも不可逆＝個別承認** | disk と平文残存の解消（status doc R7 の表） |
| A8 | ✅ 完了（2026-07-18）: `backup --keep N` — 自動命名バックアップの最新 N 個を残して削除（`.attachments` 兄弟ごと）。`--out` 指定のバックアップは対象外 | XS | — | 済 |

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

- **D-social（2026-07-14 批准。2026-07-16 FB・X とも取り込み完了）**: X / Facebook の
  **自作 + 明示的キュレーションのみ**の取り込み。ADR-0006（Accepted）、§8 非目標 10。
  実装 PR #15（schema v13 / `parsers/x_archive`・`facebook_dyi` / `cairn import x|facebook`）
  + PR #16（検索スケーリング修正）+ PR #20（X ブックマーク対象外確定）+ PR #21
  （item_links 欠落・巨大クリーク化修正）すべて merge 済み。
  - **完了（2026-07-14）**: 本番 `cairn.db` に FB DYI 実データ取り込み済み
    （social_post 46,299 = post 36,467 + comment 9,832、Karakeep と item_links 56 件、
    本人限定・他人コンテンツ混入 0 を実データで検証）。v13 は launchd sync 経由で
    本番適用済み・整合性検証済み。
  - **完了（2026-07-16）**: X アーカイブ（7GB、tweets 123,223・likes 82,482）取り込み。
    X ブックマークは公式アーカイブに dataType 自体が存在せず対象外で確定（ADR Open
    question 1 解消・実データで検証）。取り込み後の検証で2件のバグを発見・修正:
    ① `_post_record()` の埋め込みリンクが `items.url` に反映されず item_links から
    不可視だった（facebook_dyi と同じパターンに修正）。② 上記修正を適用したところ
    旧 Twitter 時代の自動投稿ボイラープレート（paper.li/Ustream/fllwrs.com 等、同一
    URL を70〜595件で共有）が `rebuild_item_links()` の総当たりペア生成で組み合わせ
    爆発（item_links 1,898→504,078、99.5%が X-X 自己リンクのノイズ、`linked_items()`
    に LIMIT がなく `build_context_pack` へ実害）。`_LINK_GROUP_CAP=30` で修正
    （同種の欠陥は claude_cli 側にも既存: 修正前で最大594件のfanoutを確認、修正で26件）。
  - **残**: メディア（写真）は非取り込みのまま（ADR 決定、変更なし）

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
