# ADR-0004: DESIGN.md（外部脳プラットフォーム設計）の採用と ADR-0003 architecture 判断の supersede

- Status: Proposed（M0 完了時に Accepted へ）
- Date: 2026-07-02
- 配置先: `docs/adr/0004-design-adoption.md`
- Supersedes: ADR-0003 の architecture 判断（決定 2〜5）。ADR-0003 の決定 1（monorepo 化・subtree 履歴保持）は有効なまま
- 関連: `docs/DESIGN.md`（v1.1）、`INTEGRATION-PREP.md`

## Context

ADR-0003 は brain-sync を独立パッケージ `brainsync/` として cairn リポジトリに併置し、Cairn へは HTTP API のみで接続する構成を採用し、「brain-sync の機能を cairn 本体（backend）へ吸収する」案を信頼境界の理由で棄却した。

その後の設計検討（DESIGN.md、Decision Record D1〜D10）で問題が再定式化された。目標は「旧 brain-sync のスクリプト群の置き場所」ではなく、「4系統（会話・ブックマーク・文献・ノート）を横断するハイブリッド検索と再浮上を成立させること」（成功基準 S5）である。この再定式化のもとでは:

1. 横断ハイブリッド検索（FTS5 + sqlite-vec + RRF）は索引が cairn.db に一元化されていることを要求する（DESIGN.md D3）。別プロセスが HTTP 越しに一覧を合成する構成では、意味検索・RRF・関連度理由の提示が Cairn 内の会話にしか効かず、S5 に構造的に到達できない。
2. ADR-0003 が根拠とした信頼境界の懸念は、パッケージ分離では実質的に軽減されない。同一マシン・同一ユーザーで動く以上、分離は組織的境界であってセキュリティ境界ではない。実効的な防御は read-only connector、Obsidian 書き込みの allowlist とパス検証（DESIGN.md §5.5）、Keychain 限定の秘密管理、FDA の廃止と Vault 移設（D9）、provenance 分離（§6.2）であり、これらは backend 内実装でも同等に強制できる。
3. さらに、その後 別セッションで確定した DESIGN.md は旧 ROADMAP Phase 3〜6 の計画を廃止し（D2: 事前抽出をやめクエリ時合成に一本化）、ADR-0003 が前提とした「Cairn = 候補算出 API、brainsync = 合成・出力」という Phase 5 の分業自体が消滅した。

## Decision

1. `docs/DESIGN.md`（v1.1）を統合後アーキテクチャの正とする。統合層（connectors / index 一般化 / recall / deliver / mcp / cli）は `backend/app/` 配下に実装する。独立パッケージ `brainsync/` は作らない。
2. ADR-0003 の決定 2〜5（責務分界・一方向依存・HTTP 限定・markdown 非契約化の各詳細）を supersede する。ただしその精神は DESIGN.md に引き継がれている: connector は read-only、Obsidian 書き込みは allowlist 限定、外部由来テキストの不信頼、状態の構造化（state.json ではなく `sync_state` テーブル）。
3. ADR-0003 の決定 1（brain-sync を cairn リポジトリへ subtree で履歴ごと統合し、旧リポジトリを Archive する）は維持する。取り込み先は `legacy/brain-sync/` とし、M3 完了時にディレクトリを削除する（DESIGN.md D11）。
4. 旧 INTEGRATION.md（T0〜T6）は廃止し、`INTEGRATION-PREP.md`（P0〜P2: 凍結・subtree・文書差し替え）に縮退する。以後の実装順序は DESIGN.md のマイルストーン M0〜M6 が正。
5. Phase 3 実装済みコード（extraction 系、schema v10）の処置は DESIGN.md D2 注記の「凍結（deprecated in place）」に従う。

## Consequences

### 良い影響

- 4系統横断のハイブリッド検索・再浮上・context pack が単一の索引基盤（chunks / embeddings / FTS / RRF）の一般化として実装でき、二重の検索スタックを持たない。
- 「Cairn API を拡張してから brainsync を追従させる」二段の契約管理が消え、マイルストーンが垂直に切れる。
- 週次レビューの選別が、タイトルのヒューリスティクスではなく横断 retrieval（recall/related）に基づく設計へ最初から向かう。

### 受け入れるコスト・リスク

- Cairn 本体が Karakeep / Zotero の認証情報（Keychain 経由・read-only）と Obsidian への限定書き込みを持つ。緩和策は DESIGN.md §5.5 / §6 に規定し、allowlist のパス検証はテストで強制する。
- 旧 INTEGRATION.md の T2〜T4（旧スクリプトの Python 化・state JSON 層・API 増分パラメータ）に相当する作業は行わない。旧スクリプトは M3 まで現状のまま並走させ、修繕せず置換する。既知の欠陥（エスケープ不足・ページネーション欠落）は並走期間中残存するが、ローカル個人運用での実害は限定的と判断する。
- ADR の連続性が一段複雑になる（0003 の一部が 0004 で上書き）。本文書と DESIGN.md §0-8 の優先順位規定で吸収する。

## Alternatives considered

- **ADR-0003 の構成を維持し、DESIGN.md の内容（items / urlnorm / related / weekly v2）を brainsync 側へ移植する。** 横断索引を brainsync が別 DB で持つことになり（DESIGN.md D3 の棄却案と同型）、embedding / FTS / RRF 基盤の二重化と RRF 横断の複雑化を招くため不採用。
- **DESIGN.md を参考情報としてのみ扱い、旧 INTEGRATION.md T0〜T6 を完走してから再検討する。** T2〜T4 の成果物（旧スクリプトの Python 化・state JSON・HTTP 増分契約）が M0〜M3 でほぼ全て捨てられることが既に判明しているため、完走は無駄。不採用。
