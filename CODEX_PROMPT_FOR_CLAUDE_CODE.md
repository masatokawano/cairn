# Codex Prompt for Claude Code

> **状態（2026-06-22 更新）: 項目 1〜5 は実装済み。** このプロンプトはセキュリティ
> 強化コミット `9188721` より前に書かれており、優先項目 1〜5（localhost 防御 /
> `/api/import` の DoS 対策 / SQLite DB の `0600` / mutation API の CSRF・同時実行
> ロック / 依存固定と監査）はすべて対応済みです。詳細は `SECURITY.md`（各項目に
> ✅ 実装済み）を参照。**項目 6（一般改善）のみ一部未実施**で、検索ページングの
> DB 寄せは完了、warning の UI 表示・fixture 追加・schema migration 等が残課題です。
> 以降は本プロンプトではなく `ROADMAP.md` の Phase 1 と `docs/architecture-audit.md`
> の P1-A〜H に沿って進めること。
>
> 以下は当時のオリジナル文面（履歴として保持）。

以下は、Codex が次回 Claude Code 起動時に渡すために作成したプロンプトです。

```text
このプロジェクト Cairn について、前回 Codex が改善レビューとセキュリティレビューを行い、`SECURITY.md` / `README.md` / `NOTES.md` に指摘事項をドキュメント化して commit/push 済みです。

まず `NOTES.md` と `SECURITY.md` を読んでください。そのうえで、以下の優先度で改善を進めてください。

1. 認証なし API の localhost 防御
   - 通常運用は `127.0.0.1` 限定。
   - `--host 0.0.0.0` 起動時に LAN へ会話本文が露出し得る。
   - `Host` / `Origin` 検証、起動時警告、必要なら bearer token を検討。

2. `/api/import` の DoS 対策
   - 現状はアップロード全体と ZIP 内 JSON をメモリに読む。
   - `Content-Length`、実読込サイズ、ZIP ファイル数、個別 JSON サイズ、総展開サイズの上限を入れる。
   - 巨大 JSON / zip bomb を 413 または 422 で安全に拒否する。

3. SQLite DB の機密性
   - `backend/data/cairn.db` / WAL / SHM は会話全文を平文で含む。
   - DB 作成時に `0600` を適用し、既存 DB の権限確認手順も README に追記する。
   - 必要なら SQLCipher は将来課題として残す。

4. mutation API の CSRF / blind POST 対策
   - `POST /api/sync` / `POST /api/import` は別 origin から blind request され得る。
   - localhost の `Origin` / `Referer` / `Host` 検証、CSRF token または API token を検討。
   - sync/import の同時実行ロックも入れる。

5. 依存関係の固定と監査
   - frontend は `npm audit` で既知脆弱性 0 件確認済み。
   - backend は `requirements.txt` が下限指定のみ。
   - Python lockfile と脆弱性監査ツール導入を検討。

6. 一般改善
   - 検索ページングを DB 側に寄せる。
   - import/sync warning を UI に表示する。
   - 実エクスポート由来の匿名化 fixture を増やす。
   - frontend の fetch HTTP エラー処理を共通化する。
   - SQLite schema migration を `PRAGMA user_version` などで入れる。

作業方針:
- まず `NOTES.md` の既知のパーサー/FTS5注意点を読む。
- 既存テストを壊さない。
- セキュリティ系は最小実装でもテストを追加する。
- UI 変更後は `cd frontend && npm run build` を実行する。
- backend テストは `cd backend && .venv/bin/python -m pytest tests/ -q`。
```
