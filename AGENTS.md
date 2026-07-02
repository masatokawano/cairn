# AGENTS.md

Cairn — AI 会話アーカイブ（FastAPI + React + SQLite FTS5 + sqlite-vec + ollama 抽出）と、
その上の統合層 brainsync（Karakeep / Cairn / Zotero / Obsidian を束ね、Obsidian へ
週次レビューと自動一覧を出力）の monorepo。すべてローカル完結。

> この内容は CLAUDE.md と同一に保つこと。片方だけ更新してはならない。

**作業前に必ず `NOTES.md` を読むこと。** ログ形式の癖・FTS5 のハマりどころ・
外部 API（Karakeep / Zotero）の癖が記録してある。学んだことは NOTES.md に追記する。

## 構成

- `backend/app/parsers/` — ソース別パーサー（chatgpt / claude_export / gemini / claude_cli / codex_cli）
- `backend/app/db.py` — スキーマ・migration・差分インポート・検索（FTS5 trigram + LIKE フォールバック + hybrid/RRF）
- `backend/app/extraction/` — Phase 3 知識抽出（segment / assertion、ollama 経由）
- `backend/app/mcp_server.py` — read-only MCP（STDIO）
- `backend/app/main.py` — FastAPI（127.0.0.1:8730、frontend/dist も配信）
- `frontend/` — Vite + React + TypeScript
- `brainsync/` — 統合層。connector（cairn_api / karakeep / zotero / obsidian）→ state JSON →
  markdown レンダリング → Obsidian 出力。CLI は `python -m brainsync <subcommand>`
- `launchd/` — plist テンプレートと install スクリプト
- `docs/` — 設計文書・ADR・backlog。`docs/brainsync-design.md` が統合層の思想と構成

## 責務分界（不変条件 — 違反する変更を提案・実装しない）

1. `backend/` は `brainsync/` を import しない。依存方向は brainsync → Cairn の一方向のみ。
2. brainsync は `cairn.db` を直接開かない。Cairn へのアクセスは HTTP API のみ。
3. brainsync の書き込み先は Obsidian Vault の `External Brain/90 Auto` と
   `External Brain/40 Reviews/Weekly` のみ。`10 Themes` / `20 Projects` / `50 Decisions`
   には書き込まない。`40 Reviews/Weekly` の既存ファイルは上書きしない。
4. 外部由来テキスト（会話タイトル、ブックマークタイトル、タグ、Web 本文）は信頼しない:
   markdown 出力時は `brainsync.markdown.escape_inline()` を通す。シェルとして評価しない。
   LLM に処理させる場合はプロンプトインジェクションを信頼境界として扱う。
5. Cairn の MCP・API は read-only を標準とし、自動処理に削除・送信・公開・外部書き込み権限を与えない。
6. シークレットは macOS Keychain（`brainsync.secrets` 経由）。config.env・コード・ログに書かない。

分界の変更が必要な場合は、実装ではなく ADR の起草を提案すること
（`docs/adr/0003-brainsync-integration.md` が現行の決定）。

## コマンド

```bash
# テスト（backend + brainsync を両方回す）
cd backend && .venv/bin/python -m pytest tests/ ../brainsync/tests -q

# Cairn 起動（開発時。常駐は launchd）
cd backend && .venv/bin/uvicorn app.main:app --port 8730

# フロントエンド再ビルド（UI 変更後に必要）
cd frontend && npm run build

# brainsync（launchd が定期実行するのと同じ入口）
.venv/bin/python -m brainsync check cairn      # 接続確認
.venv/bin/python -m brainsync sync-cairn       # 個別同期
.venv/bin/python -m brainsync weekly           # 週次レビュー生成（既存週は保護）
```

## 方針

- パーサーは実データで調整する前提。フォーマット差異に寛容に（壊れた行は warning にして skip）。
- 原本と派生データを分離する。派生（chunk / embedding / segment / assertion / state JSON /
  生成 markdown）は削除・再生成可能に保つ。
- brainsync の系統間契約は markdown ではなく state JSON と Cairn HTTP API。
  markdown 出力の構造を変える場合は golden file テストを更新する。
- スキーマ変更は migration + テスト + 事前バックアップ。破壊的 API 変更は新旧併存期間を設ける。

## 開発ロードマップと作業手順

中長期の設計方針・実装順序・受入基準は `ROADMAP.md`。作業候補は `docs/backlog.md`。

機能追加・設計変更の前に必ず読む: 1. ROADMAP.md 2. NOTES.md 3. SECURITY.md 4. README.md
（brainsync を触る場合は加えて docs/brainsync-design.md）

- ロードマップ全体を一度に実装しない。明確な受入基準を持つ 1 タスクに限定する。
- 実装前に: 現状理解・実装対象・変更予定ファイル・schema/API 変更・リスク・テスト計画を提示する。
- 実装後に: 変更ファイル・実行テストと結果・未解決事項・次の推奨タスクを報告する。
- 1 コミット 1 目的。自動 commit / push はユーザーが明示的に求めた場合のみ。

## Claude Code / Codex 併用時のレビュー観点

別エージェント（または別セッション）が変更をレビューする際は、通常の観点に加えて必ず確認する:

- 責務分界（上記 1〜6）への違反がないか
- 外部由来テキストが未エスケープで markdown / シェル / プロンプトに流れていないか
- 派生データの再生成可能性・migration の後方互換が保たれているか
- テストが受入基準を実際に検証しているか（形式的な pass でないか）
