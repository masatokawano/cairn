# ADR-0003: brain-sync の cairn リポジトリへの統合と責務分界

- Status: Proposed（T1 完了時に Accepted へ）
- Date: 2026-07-02
- 配置先: `docs/adr/0003-brainsync-integration.md`
- 関連: ROADMAP.md Phase 5 / Phase 6、`docs/brainsync-design.md`、`INTEGRATION.md`

## Context

Cairn（AI 会話アーカイブ・検索・知識抽出）と brain-sync（Karakeep / Cairn / Zotero / Obsidian を束ねる統合層）は別リポジトリで並行開発されてきた。両者のロードマップには構造的な重複がある。

- Cairn ROADMAP Phase 5（能動的再浮上: 週次 digest、フィードバック、再浮上スコア）と、brain-sync の週次レビューおよび設計書「優先6: AI による週次要約」。
- Cairn ROADMAP Phase 6（連邦型外部記憶: Obsidian export、Zotero 連携、federated MCP）と、brain-sync の存在意義そのものおよび「優先4: 関連付け」「優先5: 統合 MCP」。

また、両者の契約は暗黙である。brain-sync は Cairn の HTTP API と生成 markdown の非公式フォーマットに依存しており、Cairn 側の変更が brain-sync を静かに壊す。brain-sync にはテストがなく、launchd 設定・絶対パスなどの再構築性も cairn の水準に達していない。開発は単独開発者が Claude Code / Codex を併用して行うため、エージェントが読むドキュメント（AGENTS.md / NOTES.md / ROADMAP.md）が二系統あることのコンテキストコストも大きい。

## Decision

1. **monorepo 化する。** brain-sync を cairn リポジトリの `brainsync/` ディレクトリへ `git subtree add`（履歴保持）で統合する。旧リポジトリは final-standalone タグを残して Archive する。
2. **責務分界を固定する。**
   - Cairn（backend/frontend）= 会話原本の保全・検索・知識抽出・再浮上「候補算出」。提供形態は 127.0.0.1 の HTTP API と read-only MCP。自身の SQLite 以外へ書き込まない。
   - brainsync = 横断層。Cairn / Karakeep / Zotero / Obsidian から読み、Obsidian の `90 Auto` と `40 Reviews/Weekly` のみに書く。スケジューリング（launchd）、週次レビュー合成、将来の横断関連付けと brain-mcp を担う。
3. **依存方向は brainsync → Cairn の一方向のみ。** backend は brainsync を import しない。brainsync は cairn.db を直接開かず HTTP API のみ使用する。
4. **ROADMAP を改訂する。** Phase 5 は「候補算出・スコア・フィードバック保存 = Cairn（API）／digest 描画・Obsidian 出力 = brainsync」に分割。Phase 6 の connector 実装は brainsync に一本化し、Cairn は URL/DOI 正規化・リンクテーブル・MCP 公開に限定する。
5. **系統間の契約は markdown ではなく構造化データとする。** brainsync 内部は state JSON（schema version 付き）を中間形式とし、Cairn との契約は HTTP API（`updated_after` 等の増分取得パラメータを追加）とする。

## Consequences

### 良い影響

- 再浮上・連邦化の二重実装が構造的に不可能になる。
- API とその消費側を 1 コミットで変更でき、暗黙契約の破壊が CI（テスト）で検出可能になる。
- brainsync が cairn の pytest / ドキュメント規律（NOTES / SECURITY / ADR）に乗る。
- エージェントのセッション冒頭で読むべき文書が一系統になる。

### 受け入れるコスト・リスク

- リポジトリが「サーバアプリ + macOS 固有の自動化」の混成になる。→ ディレクトリ境界と AGENTS.md の不変条件で管理する。
- brainsync は Karakeep / Zotero の認証情報（Keychain 経由）と Obsidian への書き込み権限を持ち、cairn 本体より信頼境界が広い。→ SECURITY.md に brainsync 章を設け、書き込み先制限・エスケープ・TCC 付与先を明文化する。
- subtree 統合後の履歴はマージコミットを含み、`git log` がやや読みにくくなる。→ 許容する。

## Alternatives considered

- **別リポジトリのまま契約文書（API 仕様書）で結合する。** 契約の破壊を検出する仕組み（cross-repo CI）を個人環境で維持するコストが高く、暗黙契約の問題が実質的に残るため不採用。
- **brain-sync を主リポジトリとし cairn を connector 扱いにする。** コード量・テスト・基盤の 9 割超が cairn 側にあり、移動コストに見合わないため不採用。
- **brain-sync の機能を cairn 本体（backend）へ吸収する。** Cairn が Karakeep/Zotero 認証と Obsidian 書き込みを持つことになり、read-only MCP・localhost 限定という cairn の安全設計を毀損するため不採用。ROADMAP §9.1「Cairn を万能データベースにしない」とも矛盾する。
