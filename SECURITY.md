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

### 1. localhost バインドの強制または警告

現状の API は認証なしで検索と会話本文取得ができる。README の起動例は
`127.0.0.1` だが、検証用途などで `--host 0.0.0.0` を使うと LAN 上の他端末から
会話アーカイブを読める可能性がある。

推奨対応:

- デフォルト起動を `127.0.0.1` に限定する運用を明記する。
- `Host` / `Origin` が localhost 以外なら拒否する middleware を追加する。
- `0.0.0.0` で起動した場合は起動時ログに強い警告を出す。
- 共有利用が必要になったら bearer token などの認証を先に入れる。

### 2. アップロードサイズと ZIP 展開量の制限

`/api/import` はアップロードファイル全体をメモリに読み、ZIP 内 JSON も全展開して
から parse する。巨大 JSON や zip bomb でメモリ・CPU を使い切る可能性がある。

推奨対応:

- `Content-Length` と実読込サイズの上限を設定する。
- ZIP 内のファイル数、個別 JSON サイズ、総展開サイズを検査してから読む。
- `conversations.json` / `MyActivity.json` など候補ファイル以外を優先的に無視する。
- 失敗時は 422/413 で、ユーザーが再エクスポートや分割を判断できるメッセージを返す。

### 3. DB ファイル権限の固定

会話本文は SQLite DB と WAL/SHM に平文で保存される。マルチユーザー環境やバックアップ
経由の漏えいを避けるため、DB ファイルは所有ユーザーだけが読める権限にする。

推奨対応:

- DB 作成時に `0600` を適用する。
- 既存の `cairn.db` / `cairn.db-wal` / `cairn.db-shm` の権限を確認し、必要に応じて
  `chmod 600 backend/data/cairn.db*` を実行する。
- 暗号化が必要な運用では SQLCipher などを検討する。

### 4. mutation API の CSRF / localhost 防御

ブラウザの Same-Origin Policy により別 origin からレスポンス本文は読みにくいが、
`POST /api/sync` や `POST /api/import` の blind request 自体は送れる。ローカル限定アプリ
でも、悪意ある Web ページから localhost API を叩かれる可能性を考慮する。

推奨対応:

- `Origin` / `Referer` / `Host` を検証し、localhost 以外を拒否する。
- mutation API に CSRF token か API token を要求する。
- `sync` は長時間 I/O を伴うため、同時実行ロックと rate limit を入れる。

### 5. 依存関係の固定と監査

frontend は `package-lock.json` があり `npm audit` で既知脆弱性 0 件を確認できた。
backend は `requirements.txt` が下限指定だけなので、将来の install で異なる
バージョンが入る。

推奨対応:

- Python 依存を lockfile で固定する。
- CI またはローカル確認手順に `npm audit` と Python 側の脆弱性監査を追加する。
- `requirements.txt` は直接実行用、lockfile は再現可能インストール用として役割を分ける。

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
```

Python 側の既知脆弱性監査は、監査ツールを導入してから追加する。
