# INTEGRATION-PREP — 移行準備手順（M0 前に一度だけ実施）

- 作成日: 2026-07-02
- 位置づけ: `docs/DESIGN.md`（v1.1）のマイルストーン M0 に入る**前**の準備作業。本文書は旧 INTEGRATION.md（T0〜T6）を置き換える（経緯は `docs/adr/0004-design-adoption.md`）
- 実施者: P0 / P1 は Claude Code / Codex（1 タスク = 1 セッション）。P2 の一部は人間の確認を要する
- 完了後: 本文書は `docs/history/` へ移動し、以後の作業は DESIGN.md の M0〜M6 に従う

---

## P0 — 凍結とベースライン

1. 両リポジトリで `git status` がクリーンであることを確認。
2. 統合前タグを打つ:
   ```bash
   cd ~/workspace/cairn      && git tag pre-brainsync-merge && git push origin pre-brainsync-merge
   cd ~/workspace/brain-sync && git tag final-standalone    && git push origin final-standalone
   ```
3. Cairn のベースライン: `cd backend && .venv/bin/python -m pytest tests/ -q` の passed 件数を記録。
4. brain-sync のベースライン: `check_karakeep.sh` / `check_cairn.sh` / `check_zotero.sh` / `check_obsidian.sh` を実行し成否を記録。
5. Obsidian `External Brain/90 Auto/` 配下 4 ファイルの現物をコピーして保管（M3 の出力互換確認に使う）。

**受入基準:** タグが両リポジトリに存在し、ベースライン記録が残っている。

## P1 — git subtree による履歴取り込み

```bash
cd ~/workspace/cairn
git remote add brainsync-origin https://github.com/masatokawano/brain-sync.git
git fetch brainsync-origin
git subtree add --prefix=legacy/brain-sync brainsync-origin main
```

`git subtree` が使えない場合の fallback:

```bash
git merge --allow-unrelated-histories brainsync-origin/main
mkdir -p legacy/brain-sync && git mv <brain-sync由来の各ファイル> legacy/brain-sync/ && git commit
```

**注意:**

- `legacy/brain-sync/` は**参照専用**。この配下のコードを修繕・拡張しない（機能は DESIGN.md M1〜M3 で仕様ベースに再実装する）。M3 完了時にディレクトリごと削除する（履歴は git に残る）。
- 旧 `~/workspace/brain-sync` の launchd 4 ジョブは**そのまま稼働継続**（停止は M3）。
- 旧設計書 `legacy/brain-sync/external-brain-brain-sync-design.md` はそのまま残す（歴史文書。現行の設計は docs/DESIGN.md）。

**受入基準:**

- `git log --follow legacy/brain-sync/sync_cairn_recent.py` で旧履歴が辿れる。
- backend テストが P0 のベースラインと同数 pass。
- 旧リポジトリ側 launchd の翌時間サイクルが正常（ログで確認）。

## P2 — 文書の差し替え

1. `docs/DESIGN.md`（v1.1）、`docs/adr/0004-design-adoption.md` をコミット。
2. `CODEX_PROMPT_FOR_CLAUDE_CODE.md` を `docs/history/` へ `git mv`。旧 INTEGRATION.md / ADR-0003 をコミット済みの場合は、それぞれ `docs/history/` へ移動（未コミットなら本文書と ADR-0004 のみで足りる）。
3. AGENTS.md / CLAUDE.md を統合版（DESIGN.md の不変条件を反映した v2）へ差し替え。両ファイルは同一内容。`diff AGENTS.md CLAUDE.md` が空であることを確認するチェックを CI またはコミット前フックに追加してよい（任意）。
4. **人間の確認事項**（Claude Code は確認のみ、実施は人間）:
   - Obsidian Vault の `~/Obsidian` への移設と `/bin/bash` の FDA 解除（DESIGN.md D9）は **M3 の前提作業**。今すぐでなくてよいが、M3 着手前に完了しておくこと。
   - `masatokawano/brain-sync` の GitHub Archive は **M3 完了後**（旧スクリプト削除と launchd 置換が済んでから）。README を「cairn の legacy/brain-sync へ統合済み（final-standalone タグ参照）」に差し替えてから Archive する。

**受入基準:**

- 新規セッションの Claude Code / Codex が AGENTS.md → docs/DESIGN.md の順で読み、次の作業が「M0」であると迷わず特定できる。
- ROADMAP.md はこの時点では旧内容のままでよい（書き換えは M0 の作業項目）。

---

## ロールバック

- P1 失敗時: `git reset --hard pre-brainsync-merge`（push 済みなら revert）。旧 brain-sync は無傷で稼働継続しているため運用影響なし。
- P2 以降の文書差し替えは通常の revert で戻せる。
