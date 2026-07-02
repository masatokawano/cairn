# ROADMAP

現行の開発計画とマイルストーンの正典は **`docs/DESIGN.md`（v1.1）§7** に置く。本文書はその要約と索引。

## いま何をやっているか

DESIGN.md §7 のマイルストーン M0〜M6 を順に実施している。1 セッション = 1 マイルストーン（またはその一部）。
先回り実装をしない。完了条件はテストで示す。

| ID | 内容 | 状態 |
|----|------|------|
| M0 | 基盤再編（items レジストリ、モジュール骨格、cairn CLI 雛形） | 進行中 |
| M1 | 外部コネクタ（Karakeep, Zotero）+ URL 正規化 + item_links | 未着手 |
| M2 | 横断インデックス（外部 items の chunking + embedding + 検索 UI 拡張） | 未着手 |
| M3 | Obsidian 連携と旧 brain-sync の廃止（launchd 2 本に集約） | 未着手 |
| M4 | 週次レビュー v2（再浮上 + AI 草案） | 未着手 |
| M5 | MCP サーバ（4 系統横断の read-only ツール） | 未着手 |
| M6 | 運用の仕上げ | 未着手 |

各マイルストーンの詳細（背景・完了条件・非目標）は DESIGN.md §7 と §8 を参照。

## Phase ロードマップ（廃止）

旧 Phase 1〜6 ロードマップは Phase 1〜2 の完了・Phase 3 の実装凍結・Phase 3〜6 計画の廃止をもって役目を終えた。旧全文は `docs/history/ROADMAP-phases.md` に退避してある。

- **Phase 1（原本保全・migration・export・backup）**: 完了。実装は温存（DESIGN.md §9）。
- **Phase 2（ハイブリッド検索）**: 完了。実装は温存し M2 で items 対応に一般化する。
- **Phase 3（知識抽出）**: 実装済み（extraction 系テーブル・runner・Review UI、schema v10）だが**凍結（deprecated in place）**。バッチバックフィルや Phase 4 への拡張は行わない（DESIGN.md D2 注記）。
- **Phase 4（会話間リンクと関係タイプ）**: 計画廃止（DESIGN.md D5）。強い一致（URL/DOI/GitHub 完全一致 = `item_links`）とクエリ時類似のみに限定。
- **Phase 5（能動的再浮上）**: 計画廃止（旧設計）。DESIGN.md D6/D7 に基づく「上限つき・繰り越しなし・AI 草案つき」の週次レビュー（M4）に再設計。
- **Phase 6（連邦型外部記憶）**: 計画廃止（旧設計）。DESIGN.md D3/D4 に基づく items レジストリ方式の横断索引（M1〜M3）に再設計。

## 開発規約

作業手順・不変条件・レビュー観点は `AGENTS.md`（= `CLAUDE.md`、同一内容維持）と DESIGN.md §10 を参照。要点:

- 実装前に現状理解・変更予定・schema/API 変更・リスク・テスト計画を提示。
- コミットはマイルストーン接頭辞（`M1:` 等）。1 コミット 1 目的。
- 文書と実装が食い違ったら、実装ではなく**まず文書（DESIGN.md）を直す提案**をする。
- DESIGN.md §8 の非目標を再提案・再実装しない。
