# Cairn 作業候補（backlog）

最終更新: 2026-07-10

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
| C8 | Obsidian Vault の複数端末同期方式を運用文書に明記（外部レビュー §4.2） | XS |

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
