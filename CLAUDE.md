# CLAUDE.md

Cairn — AI会話アーカイブ・横断検索アプリ（ローカル完結、FastAPI + React + SQLite FTS5）。

**作業前に必ず `NOTES.md` を読むこと。** 各ログ形式の癖・FTS5のハマりどころが記録してある。

## 構成

- `backend/app/parsers/` — ソース別パーサー（chatgpt / claude_export / gemini / claude_cli / codex_cli）
- `backend/app/db.py` — スキーマ・差分インポート・検索（FTS5 trigram + LIKEフォールバック）
- `backend/app/cli_sync.py` — CLIログのポーリング同期
- `backend/app/main.py` — FastAPI（ポート8730、frontend/distも配信）
- `frontend/` — Vite + React + TypeScript（単一画面、`src/App.tsx`）

## コマンド

```bash
# テスト
cd backend && .venv/bin/python -m pytest tests/ -q

# 起動
cd backend && .venv/bin/uvicorn app.main:app --port 8730

# フロントエンド再ビルド（UI変更後に必要）
cd frontend && npm run build
```

## 方針

- パーサーは実データで調整する前提。フォーマット差異に寛容に（壊れた行はwarningにしてskip）
- chatgpt / claude / gemini パーサーはまだ実エクスポートで未検証（NOTES.md参照）
- 学んだことは NOTES.md に追記する

## 開発ロードマップ

中長期の設計方針、実装順序、受入基準は ROADMAP.md に記載している。

機能追加または設計変更を行う前に、必ず以下を読むこと。

1. ROADMAP.md
2. NOTES.md
3. SECURITY.md
4. README.md

ロードマップ全体を一度に実装してはならない。原則として、明確な受入基準を持つ一つのタスクに限定して作業する。

最初に着手する場合は、ROADMAP.md の「Task 1: 現状監査と Phase 1 設計」を実施し、コードの大規模変更より先に docs/architecture-audit.md を作成すること。