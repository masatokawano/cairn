# AGENTS.md

Cairn — AI 会話アーカイブを核に、Karakeep（発見）/ Zotero（根拠）/ Obsidian（理解）を
横断索引する個人用外部脳プラットフォーム（FastAPI + React + SQLite FTS5 + sqlite-vec + ollama）。
すべてローカル完結。設計の正典は **docs/DESIGN.md**（v1.1）。

> この内容は CLAUDE.md と同一に保つこと。片方だけ更新してはならない。
> 本文書と docs/DESIGN.md が矛盾したら DESIGN.md が正。

**作業前に必ず読む:** docs/DESIGN.md（特に §2 Decision Record と §8 非目標）、NOTES.md。
学んだことは NOTES.md に追記する。

## 構成

- `backend/app/parsers/` — AI 会話の取り込み（chatgpt / claude_export / gemini / claude_cli / codex_cli）
- `backend/app/db.py` — スキーマ・migration（現行 v10）・差分インポート・検索（FTS5 trigram + hybrid/RRF）
- `backend/app/connectors/` — Karakeep / Zotero / Obsidian の read-only クライアント（M1, M3）
- `backend/app/core/urlnorm.py` — URL/DOI 正規化（M1、テスト厚め）
- `backend/app/recall/` — related() / weekly digest（M4）
- `backend/app/deliver/` — obsidian_writer / weekly_review（M3, M4）
- `backend/app/mcp/` — 横断 MCP サーバ（M5）。旧 `app/mcp_server.py` は M5 で統合
- `backend/app/extraction/` — Phase 3 抽出パイプライン。**凍結中**（DESIGN.md D2 注記）
- `backend/app/admin.py` — 既存管理 CLI（redact / backup / integrity 等）。温存、M6 で統合検討
- `frontend/` — Vite + React + TypeScript
- `legacy/brain-sync/` — 旧 brain-sync（参照専用・修繕禁止・M3 で削除）
- `ops/launchd/` — plist テンプレート（M3 で `com.masato.cairn.*` 2 本に集約）

## 不変条件（違反する変更を提案・実装しない）

1. connectors は read-only。Karakeep / Zotero / 原本会話へ書き込まない。
2. Obsidian への書き込みは `deliver/obsidian_writer.py` の allowlist 3 箇所のみ
   （`90 Auto`=上書き可 / `40 Reviews/Weekly`=新規のみ / `00 Inbox/AI Drafts`=新規のみ）。
   パス検証でトラバーサルを拒否し、テストで強制する。
3. `conversations` / `messages` 等の原本系テーブルは破壊的変更禁止。migration は追加のみ・
   実行前バックアップ必須。派生データ（items / chunks / embeddings / 索引）は常に再構築可能に保つ。
4. 外部由来テキスト（タイトル・本文・タグ）は信頼しない: シェル評価しない、markdown 出力時は
   エスケープ、LLM へ渡す際は区切りとガード指示を付す。LLM 生成物には provenance ラベル
   （`generated_by: cairn/<model>/<prompt_version>`）を必ず付与し、MCP 応答では原文と合成を
   構造的に分離する（DESIGN.md §6.2）。
5. 秘密情報は macOS Keychain のみ（`brain-sync-karakeep` / `brain-sync-zotero`）。
   config・ログ・例外メッセージにキーを出さない。
6. **DESIGN.md §8 の非目標を再提案・再実装しない**（assertion 事前抽出の再開、関係タイプ自動分類、
   ランキング学習、レビュー繰り越し、原本への書き込み、原本全文の Obsidian 複製 等）。
   必要になったら実装ではなく Decision Record の改訂を提案する。
7. `legacy/brain-sync/` は参照専用。修繕・拡張しない。

## コマンド

```bash
# テスト
cd backend && .venv/bin/python -m pytest tests/ -q

# Cairn 起動（開発時。常駐は launchd）
cd backend && .venv/bin/uvicorn app.main:app --port 8730

# フロントエンド再ビルド（UI 変更後に必要）
cd frontend && npm run build

# 統合層 CLI（M0 以降）
cairn sync [karakeep|zotero|obsidian|conversations|all]
cairn review weekly [--week 2099-W01]
cairn index rebuild

# 既存管理 CLI（温存）
cd backend && .venv/bin/python -m app.admin <subcommand>
```

## 作業手順

- 作業は DESIGN.md §7 のマイルストーン単位（M0〜M6）。1 セッション = 1 マイルストーン
  またはその一部。先回り実装をしない。完了条件はテストで示す。
- 実装前に: 現状理解・実装対象・変更予定ファイル・schema/API 変更・リスク・テスト計画を提示。
- 実装後に: 変更ファイル・実行テストと結果・未解決事項・次の推奨タスクを報告。
- コミットはマイルストーン接頭辞（`M1:` 等）。1 コミット 1 目的。
  自動 commit / push はユーザーが明示的に求めた場合のみ。
- 文書と実装が食い違ったら、実装ではなくまず文書（DESIGN.md）を直す提案をする。

## Claude Code / Codex 併用時のレビュー観点

別エージェント（または別セッション）によるレビューでは、通常の観点に加えて必ず確認する:

- 不変条件 1〜7 への違反がないか（特に書き込み allowlist と read-only 制約）
- 外部由来テキストが未エスケープで markdown / シェル / プロンプトに流れていないか
- provenance ラベルの付与漏れがないか
- migration の追加のみ原則・バックアップ・再構築可能性が保たれているか
- DESIGN.md §8 非目標への逸脱（善意の先回り実装を含む）がないか
- テストが完了条件を実際に検証しているか（形式的な pass でないか）
