# AGENTS.md

Cairn — AI 会話アーカイブを核に、Karakeep（発見）/ Zotero（根拠）/ Obsidian（理解）を
横断索引する個人用外部脳プラットフォーム（FastAPI + React + SQLite FTS5 + sqlite-vec + ollama）。
すべてローカル完結。設計の正典は **docs/DESIGN.md**（v1.1）。

> **文書の関係**: 本ファイルが全エージェント共通の作業規約の正典。
> `CLAUDE.md` は本ファイルを import する 1 行スタブであり、直接編集しない。
> 本文書と docs/DESIGN.md が矛盾したら DESIGN.md が正。
> マルチモデル運用（役割分担・ハンドオフ様式・レビュー手順の詳細）は
> **docs/orchestration.md** — 引き継ぎ・レビュー・モデル選定時にのみ読めばよい。

**作業前に必ず読む:** docs/DESIGN.md（特に §2 Decision Record と §8 非目標）、NOTES.md。

## 構成

- `backend/app/parsers/` — AI 会話の取り込み（chatgpt / claude_export / gemini / claude_cli / codex_cli）
- `backend/app/db.py` — スキーマ・migration（現行 v12）・差分インポート・検索（FTS5 trigram + hybrid/RRF）
- `backend/app/connectors/` — Karakeep / Zotero / Obsidian の read-only クライアント（M1, M3）
- `backend/app/core/urlnorm.py` — URL/DOI 正規化（M1、テスト厚め）
- `backend/app/recall/` — related() / weekly digest（M4）
- `backend/app/deliver/` — obsidian_writer / weekly_review（M3, M4）
- `backend/app/mcp/` — 横断 MCP サーバ（M5）。旧 `app/mcp_server.py` は M5 で統合
- `backend/app/extraction/` — Phase 3 抽出パイプライン。**凍結中**（DESIGN.md D2 注記）
- `backend/app/admin.py` — 既存管理 CLI（redact / backup / integrity 等）。温存、M6 で統合検討
- `frontend/` — Vite + React + TypeScript
- `ops/launchd/` — plist テンプレート（M3 で `com.masato.cairn.*` 2 本に集約）

## 不変条件（違反する変更を提案・実装しない）

1. connectors は read-only。Karakeep / Zotero / 原本会話へ書き込まない。
2. Obsidian への書き込みは `deliver/obsidian_writer.py` の allowlist 4 箇所のみ
   （`90 Auto`=上書き可 / `40 Reviews/Weekly`=新規のみ / `00 Inbox/AI Drafts`=新規のみ /
   `90 Auto/Health`=上書き可・H5/ADR-0005。健康レポート専用、Vault 同期から既定除外
   = PRIVACY.md H5-P1）。パス検証でトラバーサルを拒否し、テストで強制する。
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
7. 旧 brain-sync は M3 で廃止済み（`legacy/brain-sync/` は削除、履歴は git に残存）。復活・再導入しない。
8. **不可逆操作は毎回明示の承認を得る**: migration の本番 DB への適用、ファイル/テーブル削除、
   launchd の load/unload、`git push`、タグ操作。承認済み計画の中に含まれていても、実行直前に
   対象を示して確認する。それ以外の可逆操作（コード編集・テスト実行・ローカル commit の準備）は
   承認済み計画の範囲内なら確認なしで進めてよい。
9. **健康データ境界（ADR-0005 / DESIGN.md D13）**: 実健康データ（検査値・Apple Health
   export・医療文書・実データ由来レポート）をリポジトリ・テストフィクスチャ・ログ・
   コミット・PR に入れない（テストは合成データのみ）。health ストアは `cairn.db` から
   独立させ、health data home は Git worktree 外に置く。health MCP は既定無効。
   Obsidian への健康レポート配信は allowlist（`90 Auto/Health` は H5 で追加）と
   docs/health/PRIVACY.md の複製決定 H5-P1 に従う。

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
- セッション冒頭で「このマイルストーンが DESIGN.md §1 の成功基準（S1〜S5）のどれに
  寄与するか」を 1 行で確認してから着手する（目的からの逸脱防止）。
- 実装前に: 現状理解・実装対象・変更予定ファイル・schema/API 変更・リスク・テスト計画を提示。
  **計画が承認されたら、その範囲内では選択肢の列挙や再確認で止まらず実装を進める**
  （不可逆操作は不変条件 8 に従い都度確認）。
- 実装後に: 変更ファイル・実行テストと結果・未解決事項・次の推奨タスクを報告。
- マイルストーン相当の節目の完了報告と同じセッションで、ROADMAP.md の状態表・
  docs/backlog.md・関連 memory を実態に合わせて更新する（文書追従、orchestration.md G2③）。
- コミットはマイルストーン接頭辞（`M1:` 等）。1 コミット 1 目的。
  自動 commit / push はユーザーが明示的に求めた場合のみ。
- 文書と実装が食い違ったら、実装ではなくまず文書（DESIGN.md）を直す提案をする。
- 同じ失敗が 2 回続いたら、3 回目の試行ではなく状況を報告して判断を仰ぐ。

## スコープと報告の規律（全モデル共通。高能力モデルほど厳守）

- **タスクが求める以上のことをしない**: 依頼範囲外の抽象化・エラーハンドリング・
  リファクタリング・依存パッケージ追加・「ついで」の改善をしない。気づいた問題は
  実装せず報告に載せる（報告 = 修正許可ではない）。
- **進捗・完了の主張はツール結果で裏付けられたものだけ**: テストは pass の出力を
  確認してから「通った」と書く。未実行・未確認のことを完了として報告しない。
- **コンテキスト残量や所要時間を理由に、途中終了・セッション分割・要約を自分から
  提案しない**。マイルストーンの完了条件を優先する。
- **宣言だけで止まらない**: 「次に〜します」と書いたら、そのターンで実際にツールを
  呼ぶ。承認待ちが必要なのは不変条件 8 の不可逆操作のみ。
- 作業途中の思考メモは簡潔でよいが、**最終報告はこのセッションを見ていない読者が
  読んで分かる完全な文**で書く。
- サブエージェントへの委任は read-only の調査・テスト実行・ログ解析に限る。
  DB・migration・deliver 系への書き込みを伴う変更は主エージェントが自ら行う。

## NOTES.md への記録規律

NOTES.md は次セッションのための永続メモリ。学んだことは追記するが、規律を守る:

- 記録するのは「次のセッションが同じ穴に落ちないための教訓」のみ。作業ログ・進捗は書かない。
- 1 教訓 = 1 項目。追記前に既存項目との重複を確認する。
- 過去の項目が誤りと判明したら、矛盾する項目を追記するのではなく**該当項目を修正**する。
- フォーマットは既存の見出し構成（ソース別・技術別）に合わせる。
- `.memory-audit/`（記憶監査ループの成果物置き場）は consolidation・索引・メモリ採掘・NOTES への転記の対象外とする。

## モデル / エージェントの役割（要約）

| 役者 | 既定の用途 |
|---|---|
| 実装既定モデル（現在: Claude Opus 4.8） | マイルストーン級実装・長い自律実行・セキュリティ関連コード（redaction / allowlist / MCP ガード）。**orchestration.md §2 の実行規律 G1〜G6 を必ず適用** |
| 実装既定モデル + 最高 effort / 実装前 Plan・Review | migration 設計のような不可逆で難度の高い設計（実装前に独立レビューを 1 本挟む） |
| Claude Sonnet 5 (high) | backlog の XS/S タスク、文書更新、フロント再ビルド等の定型作業 |
| Codex CLI | 実装とは別系統による独立レビュー（下記観点）。セカンドオピニオン |

選定基準・**実行規律（旧既定モデル Fable 5 の作業スタイルを、どのモデルでも再現できる
ゲート G1〜G6 に手順化したもの）**・ハンドオフ様式・レビュー往復の上限は
docs/orchestration.md を参照。モデル世代交代時は表の「現在: ◯◯」のみ差し替える。

## 別エージェント / 別セッションによるレビュー観点

レビューでは、通常の観点に加えて必ず確認する:

- 不変条件 1〜8 への違反がないか（特に書き込み allowlist と read-only 制約）
- 外部由来テキストが未エスケープで markdown / シェル / プロンプトに流れていないか
- provenance ラベルの付与漏れがないか
- migration の追加のみ原則・バックアップ・再構築可能性が保たれているか
- DESIGN.md §8 非目標への逸脱（善意の先回り実装を含む）がないか
- テストが完了条件を実際に検証しているか（形式的な pass でないか）
- **実装者の完了報告がツール結果と一致しているか**（報告された pass 件数・変更ファイルを再確認）
