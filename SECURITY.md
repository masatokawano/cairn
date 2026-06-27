# SECURITY

Cairn は個人の AI 会話ログを扱うローカル完結アプリです。会話本文には機密情報、
認証情報、業務情報、個人情報が含まれる可能性があるため、DB と HTTP API は
公開サービスと同じ前提で扱います。

## 想定する利用形態

- 通常起動は `127.0.0.1:8730` に限定する。
- LAN やインターネットへ公開しない。
- `backend/data/cairn.db`、`cairn.db-wal`、`cairn.db-shm` は機密データとして扱う。
- ChatGPT / Claude / Gemini のエクスポート ZIP は信頼できる入手元のものだけ取り込む。

## 優先度の高い改善

### 1. localhost バインドの強制または警告 — ✅ 実装済み

現状の API は認証なしで検索と会話本文取得ができる。README の起動例は
`127.0.0.1` だが、検証用途などで `--host 0.0.0.0` を使うと LAN 上の他端末から
会話アーカイブを読める可能性がある。

実装内容 (`app/main.py` の `local_only` middleware):

- `Host` ヘッダのホスト名が `127.0.0.1` / `localhost` / `::1` 以外なら全リクエスト 403。
  LAN からの直接アクセスも DNS rebinding も Host が一致しないため拒否される。
- 検証用途で他ホスト名が必要な場合は `CAIRN_ALLOW_HOSTS=host.docker.internal` の
  ように追加できる。設定時は起動ログに強い警告を出す。
- 共有利用が必要になったら bearer token などの認証を先に入れる（未実装・将来課題）。

### 2. アップロードサイズと ZIP 展開量の制限 — ✅ 実装済み

`/api/import` はアップロードファイル全体をメモリに読み、ZIP 内 JSON も全展開して
から parse する。巨大 JSON や zip bomb でメモリ・CPU を使い切る可能性がある。

実装内容:

- `Content-Length` の事前チェック + チャンク読み込みでの実サイズ上限
  （`CAIRN_MAX_UPLOAD_MB`、デフォルト500MB）→ 413。
- ZIP エントリ数上限（`CAIRN_MAX_ZIP_ENTRIES`、デフォルト10000）→ 413。
- ZIP 内 JSON はヘッダの `file_size` を信用せず **境界付き読み込み**
  （`CAIRN_MAX_JSON_MB`、デフォルト500MB）で展開量を制限（zip bomb 対策）→ 413。
- `conversations.json` / `MyActivity.json` を優先的に試し、それ以外の巨大 JSON は skip。
- フォーマット不明は 422、サイズ超過は 413 で、対処方法を含むメッセージを返す。

### 3. DB ファイル権限の固定 — ✅ 実装済み

会話本文は SQLite DB と WAL/SHM に平文で保存される。マルチユーザー環境やバックアップ
経由の漏えいを避けるため、DB ファイルは所有ユーザーだけが読める権限にする。

実装内容:

- `db.connect()` で DB 本体と既存の WAL/SHM に `0600` を適用する（ベストエフォート）。
  SQLite は WAL/SHM を DB 本体と同じ権限で作るため、以後作られる sidecar も `0600` になる。
- 暗号化が必要な運用では SQLCipher などを検討する（将来課題）。

### 4. mutation API の CSRF / localhost 防御 — ✅ 実装済み

ブラウザの Same-Origin Policy により別 origin からレスポンス本文は読みにくいが、
`POST /api/sync` や `POST /api/import` の blind request 自体は送れる。ローカル限定アプリ
でも、悪意ある Web ページから localhost API を叩かれる可能性を考慮する。

実装内容:

- GET 以外のメソッドで `Origin` ヘッダが存在し localhost 系でなければ 403
  （ブラウザは cross-origin POST に必ず Origin を付けるため blind POST を遮断。
  Origin を送らない curl 等の CLI クライアントは通す）。
- Host 検証は #1 の middleware が全リクエストに適用。
- sync / import は共通の ingest ロックで直列化。実行中の `POST /api/sync` は 409 を返す。
- rate limit は未実装（ローカル専用前提のため将来課題）。

### 5. 依存関係の固定と監査 — ✅ 実装済み

frontend は `package-lock.json` があり `npm audit` で既知脆弱性 0 件を確認できた。
backend は `requirements.txt` が下限指定だけなので、将来の install で異なる
バージョンが入る。

実装内容:

- `backend/requirements.lock`（`pip freeze` 出力）を追加。再現可能インストールは
  `pip install -r requirements.lock`、開発時の追加は `requirements.txt` を更新して
  lock を再生成する。
- 監査手順を下の「推奨確認コマンド」に追加（`uvx pip-audit`）。
  2026-06-12 時点で backend / frontend とも既知脆弱性 0 件。

## 現状の良い点

- SQL は parameterized query 中心で、検索語や source を直接 SQL 文字列へ埋め込んでいない。
- React で `dangerouslySetInnerHTML` を使っておらず、取り込みデータ由来の XSS は起きにくい。
- CLI ログ parser は壊れた行を warning として skip する方針で、1 ファイルの破損が全体を止めにくい。

## 推奨確認コマンド

```bash
cd frontend
npm audit

cd ../backend
.venv/bin/python -m pip check
.venv/bin/python -m app.admin audit-deps                     # 既知脆弱性監査（pip-audit ラッパー、CI で exit code を見れる）
ls -l data/                                                  # cairn.db* が 0600 (-rw-------) であること
```

## SQLite 拡張ロード（sqlite-vec, P2-1c）

- `db.connect()` が `sqlite_vec` ロードのために `enable_load_extension(True)` を一時的に
  立ち上げ、**ロード完了後すぐ `False` に戻す**。常時 True 放置はしない。
- 拡張ロードが OS / SQLite ビルドの事情で失敗する環境では、自動的に NumpyIndex
  フォールバックに切り替わり機能継続する（ADR-0001 §5.1）。
- `CAIRN_VECTOR_INDEX=numpy` 環境変数で明示的にフォールバックに固定可能（拡張に
  懸念があるが他機能は使いたい場合のエスケープハッチ）。

## 残課題（将来）

- bearer token 認証（共有利用が必要になった場合の前提条件）
- mutation API の rate limit
- SQLCipher 等による DB 暗号化
