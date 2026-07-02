# Personal External Brain / Brain Sync 設計書

作成日: 2026-06-29  
対象環境: macOS / iPhone / AWS EC2 / Karakeep / Cairn / Zotero / Obsidian

---

## 1. 目的

X、Facebook、ニュース記事、論文、GitHub、自分の着想、生成AIとの対話履歴などを、単に保存するだけでなく、後から横断的に検索・再発見・再利用できる個人用の外部脳を構築する。

目標は、巨大な資料倉庫ではなく、次の問いに答えられる仕組みである。

- 過去に何を見つけたか
- それについて何を考えたか
- どの資料を根拠として採用したか
- 現時点でどう理解しているか
- 何が未解決のままか
- 最近の情報と過去の思考がどうつながるか

---

## 2. 基本コンセプト

各システムの役割を明確に分ける。

```text
Karakeep = 発見したもの
Cairn    = 考えた過程
Zotero   = 根拠資料
Obsidian = 現在の理解
```

この4つを1つの巨大DBへ物理統合するのではなく、各システムを原本庫として残し、その上に `brain-sync` という薄い統合層を置く。

---

## 3. 全体アーキテクチャ

```text
Mac 2台 / iPhone 2台
        │
        ├─ Web / SNS / GitHub ─────────────→ Karakeep
        │
        ├─ 論文 / 公文書 / 判例 ──────────→ Zotero
        │
        ├─ ChatGPT / Claude / Codex ───────→ Cairn
        │
        └─ 自分の着想 / 仮説 / 構想 ──────→ Obsidian
                                               ▲
                                               │
                                      ~/workspace/brain-sync
                                               │
                    ┌──────────────────────────┼──────────────────────────┐
                    │                          │                          │
              Karakeep API               Cairn API                 Zotero API
                    │                          │                          │
                    └──────────────────────────┼──────────────────────────┘
                                               │
                                    Obsidian / External Brain
                                               │
                            自動一覧 / 週次レビュー / 再浮上
```

---

## 4. 現在の実環境

### 4.1 Karakeep

- AWS EC2上でセルフホスト
- 外部で発見したWeb資料の受信箱
- Mac / iPhoneから共有して保存
- API経由で `brain-sync` から取得
- `to-review` タグ付き項目をObsidianへ同期

### 4.2 Cairn

- ローカル開発パス:

```text
~/workspace/cairn
```

- API:

```text
http://127.0.0.1:8730
```

- 主なエンドポイント:

```text
GET /api/conversations
GET /api/conversations/{id}
GET /api/search
GET /api/stats
```

- 会話一覧は `updated_at DESC`
- 本文は一覧に含めず、必要時のみ個別取得
- SQLite DB:

```text
~/workspace/cairn/backend/data/cairn.db
```

ただし、`brain-sync` からはSQLiteを直接読まず、HTTP APIを使用する。

### 4.3 Zotero

- 書誌データ: Zotero公式同期
- 添付ファイル: AWS EC2上のセルフホストWebDAV
- Web APIから書誌データのみ取得
- WebDAVへ `brain-sync` から直接書き込まない
- PDF添付の同期はZotero自身に任せる

### 4.4 Obsidian

Vault:

```text
/Users/masato/Documents/Obsidian Vault
```

外部脳ディレクトリ:

```text
External Brain
```

構成:

```text
External Brain/
├── 00 Inbox/
│   ├── AI Drafts/
│   └── Ideas/
├── 10 Themes/
├── 20 Projects/
├── 30 Sources/
│   ├── Karakeep/
│   ├── Cairn/
│   └── Zotero/
├── 40 Reviews/
│   ├── Daily/
│   └── Weekly/
├── 50 Decisions/
└── 90 Auto/
```

原則:

- 自動生成物は `90 Auto` と `40 Reviews` に限定
- `10 Themes` と `20 Projects` は人間が管理
- AIがテーマノートやプロジェクトノートを勝手に上書きしない

### 4.5 Brain Sync

開発パス:

```text
~/workspace/brain-sync
```

役割:

- 各システムから情報を取得
- Obsidianへ安全に一覧化
- 週次レビューを生成
- 将来的に横断検索・関連付け・再浮上を担う

---

## 5. 日常運用

### 5.1 発見したとき

```text
Web / SNS / GitHub
        ↓
Karakeepへ保存
```

保存時に細かい分類はしない。

特に週次レビューへ出したいものだけ:

```text
to-review
```

を付ける。

将来用タグ:

```text
to-zotero
to-obsidian
processed
```

### 5.2 AIと対話したとき

ChatGPT、Claude、Codex、Claude Code等を通常どおり利用する。

会話履歴はCairnへ集約し、Obsidianには会話全文を複製しない。

### 5.3 論文や一次資料

引用可能な根拠資料はZoteroへ保存する。

### 5.4 自分の着想

Obsidianの:

```text
External Brain/00 Inbox/Ideas
```

へ保存する。

### 5.5 毎週

Obsidianの:

```text
External Brain/40 Reviews/Weekly/YYYY-Www.md
```

を確認する。

---

## 6. 設定ファイル

`~/workspace/brain-sync/config.env`

例:

```bash
KARAKEEP_URL="https://<Karakeep URL>"

CAIRN_URL="http://127.0.0.1:8730"

ZOTERO_USER_ID="<数値のUser ID>"
ZOTERO_API_URL="https://api.zotero.org"

OBSIDIAN_VAULT="/Users/masato/Documents/Obsidian Vault"
OBSIDIAN_EXTERNAL_BRAIN_DIR="External Brain"
```

`config.env` は `.gitignore` 対象。

---

## 7. 秘密情報の管理

APIキーはファイルへ保存せず、macOSキーチェーンを使用する。

### Karakeep

サービス名:

```text
brain-sync-karakeep
```

取得例:

```bash
security find-generic-password \
  -a "$USER" \
  -s "brain-sync-karakeep" \
  -w
```

### Zotero

サービス名:

```text
brain-sync-zotero
```

取得例:

```bash
security find-generic-password \
  -a "$USER" \
  -s "brain-sync-zotero" \
  -w
```

---

## 8. 実装済み・設計済みファイル

### 8.1 Karakeep

#### `check_karakeep.sh`

目的:

- API接続確認
- `to-review` タグ付き項目取得確認

#### `sync_karakeep_review.sh`

出力先:

```text
External Brain/90 Auto/karakeep-to-review.md
```

内容:

- タイトル
- URL
- Karakeep ID
- 保存日時
- タグ
- チェックボックス

#### `run_karakeep_sync.sh`

- `launchd` から安全に実行するラッパー
- 1時間ごとに実行

---

### 8.2 Cairn

#### `check_cairn.sh`

目的:

- Cairn API接続確認
- 直近会話3件取得
- 応答スキーマ確認

#### `sync_cairn_recent.py`

出力先:

```text
External Brain/90 Auto/cairn-recent.md
```

対象:

- 直近7日間
- 4メッセージ以上
- 定型的な自動レビュー会話を除外
- `New chat` 等の汎用タイトルを除外

除外例:

```text
Review this change for security vulnerabilities.
You are a security expert reviewing...
New chat
User Request: Help Needed
Untitled
```

出力内容:

- タイトル
- Source
- Cairn ID
- 更新日時
- メッセージ数
- Project directory
- 確認用チェックボックス

#### `run_cairn_sync.sh`

- Cairn APIが起動中か確認
- 停止中なら既存ファイルを壊さず正常終了
- 起動中なら `sync_cairn_recent.py` を実行

---

### 8.3 Zotero

#### `check_zotero.sh`

目的:

- Zotero Web API接続確認
- 最新3件取得
- APIキー権限確認

#### `sync_zotero_recent.py`

出力先:

```text
External Brain/90 Auto/zotero-recent.md
```

対象:

- 直近7日間に追加または更新された上位100件から抽出

出力内容:

- タイトル
- itemType
- Zotero Key
- 更新日時
- 著者
- DOI
- URL
- タグ
- 確認用チェックボックス

注意:

- 書誌データのみ取得
- AWS EC2上のWebDAV添付ファイルには触れない

#### `run_zotero_sync.sh`

- `sync_zotero_recent.py` を実行
- 1時間ごとに自動実行

---

### 8.4 Obsidian

#### `check_obsidian.sh`

目的:

- Vaultへの書き込み確認

テストファイル:

```text
External Brain/90 Auto/brain-sync-test.md
```

#### `sync_obsidian_context.py`

設計済み。未検証の場合は次回確認する。

目的:

- `10 Themes`
- `20 Projects`

の最近更新されたノート一覧を生成する。

出力先:

```text
External Brain/90 Auto/obsidian-context.md
```

対象期間:

```text
30日
```

内容:

- 最近更新されたテーマノート
- 最近更新されたプロジェクトノート
- Obsidian内部リンク
- 更新日時

---

### 8.5 週次レビュー

#### `create_weekly_review.sh`

出力先:

```text
External Brain/40 Reviews/Weekly/YYYY-Www.md
```

性質:

- 既存ファイルがある場合は上書きしない
- 週次レビューは人間が編集できる
- `90 Auto` の自動一覧からスナップショットを作る

統合対象:

```text
Karakeep
Cairn
Zotero
Obsidian
```

構成:

```text
今週の処理方針
Karakeep：発見したもの
Cairn：考えた過程
Zotero：根拠資料
Obsidian：現在の理解
今週の統合メモ
```

統合メモ欄:

```text
繰り返し現れたテーマ
新しく得た着想
根拠資料として残すもの
過去の考えから変化した点
未解決の問い
来週行うこと
```

テスト時は:

```bash
BRAIN_SYNC_WEEK=2099-W01 ./create_weekly_review.sh
```

のように週番号を上書きできる。

#### `run_weekly_review.sh`

処理順:

```text
Karakeep同期
Cairn同期
Zotero同期
Obsidian Context同期
週次レビュー作成
```

---

## 9. LaunchAgent

### Karakeep

Label:

```text
com.masato.brain-sync.karakeep
```

ファイル:

```text
~/Library/LaunchAgents/com.masato.brain-sync.karakeep.plist
```

頻度:

```text
1時間ごと
```

ログ:

```text
~/Library/Logs/brain-sync/karakeep-sync.log
~/Library/Logs/brain-sync/karakeep-sync-error.log
```

### Cairn

Label:

```text
com.masato.brain-sync.cairn
```

ファイル:

```text
~/Library/LaunchAgents/com.masato.brain-sync.cairn.plist
```

頻度:

```text
1時間ごと
```

ログ:

```text
~/Library/Logs/brain-sync/cairn-sync.log
~/Library/Logs/brain-sync/cairn-sync-error.log
```

### Zotero

Label:

```text
com.masato.brain-sync.zotero
```

ファイル:

```text
~/Library/LaunchAgents/com.masato.brain-sync.zotero.plist
```

頻度:

```text
1時間ごと
```

ログ:

```text
~/Library/Logs/brain-sync/zotero-sync.log
~/Library/Logs/brain-sync/zotero-sync-error.log
```

### 週次レビュー

Label:

```text
com.masato.brain-sync.weekly-review
```

ファイル:

```text
~/Library/LaunchAgents/com.masato.brain-sync.weekly-review.plist
```

実行:

```text
ログイン時
毎週日曜日 18:00
```

ログ:

```text
~/Library/Logs/brain-sync/weekly-review.log
~/Library/Logs/brain-sync/weekly-review-error.log
```

---

## 10. macOS権限

`launchd` から `Documents` 配下のObsidian Vaultへ書き込むため、macOSのTCC制約に対応する必要がある。

設定:

```text
システム設定
→ プライバシーとセキュリティ
→ フルディスクアクセス
→ /bin/bash
```

この設定により、LaunchAgent経由の書き込みが成功した。

エラー例:

```text
Operation not permitted
```

---

## 11. 生成されるObsidianファイル

```text
External Brain/
├── 40 Reviews/
│   └── Weekly/
│       └── YYYY-Www.md
└── 90 Auto/
    ├── brain-sync-test.md
    ├── karakeep-to-review.md
    ├── cairn-recent.md
    ├── zotero-recent.md
    └── obsidian-context.md
```

---

## 12. 安全設計

### 12.1 原本を壊さない

- Karakeep、Cairn、Zoteroは原本庫
- Obsidianへは索引・要約・レビュー候補のみ出力
- 会話全文や論文PDFを無条件に複製しない

### 12.2 自動生成物と人間編集を分離

```text
90 Auto
  機械が上書きしてよい

40 Reviews
  一度作成したら上書きしない

10 Themes / 20 Projects
  人間が管理
```

### 12.3 外部入力を信頼しない

Webページや会話タイトルは信頼できない外部入力として扱う。

- シェルコマンドとして評価しない
- HTMLをそのまま実行しない
- Markdown出力時に安全に扱う
- 将来LLM処理を加える場合も、外部本文からのプロンプトインジェクションを前提にする

### 12.4 書き込み権限を最小化

- Karakeep APIキーは必要最小限
- Zoteroは当初Read Only
- Cairnは読み取りAPI
- Obsidianの自動書き込み先を限定

---

## 13. 現在の到達点

完了済み:

- Obsidianディレクトリ作成
- Karakeep API接続
- Karakeep APIキーのKeychain保存
- Karakeep `to-review` 一覧のObsidian出力
- Karakeepの1時間ごとの自動同期
- Cairn API接続
- Cairn直近会話一覧のObsidian出力
- Cairnノイズ除外
- Cairnの1時間ごとの自動同期
- Zotero API接続
- Zotero直近資料一覧のObsidian出力
- Zoteroの1時間ごとの自動同期
- Karakeep / Cairn / Zoteroの週次統合
- 週次レビューの自動作成
- 既存週次レビューの上書き防止
- macOS TCC対応

設計済み・要確認:

- `sync_obsidian_context.py`
- Obsidian Contextの週次レビュー統合
- 週次レビュー生成時の4系統完全同期

---

## 14. 次の実装候補

### 優先1: Obsidian Contextの動作確認

- `10 Themes`
- `20 Projects`

へテストノートを1件ずつ作成し、`obsidian-context.md` に出ることを確認する。

### 優先2: LaunchAgent設定をリポジトリへテンプレート化

端末固有の絶対パスを変数化し、再構築可能にする。

候補:

```text
launchd/
├── com.masato.brain-sync.karakeep.plist.template
├── com.masato.brain-sync.cairn.plist.template
├── com.masato.brain-sync.zotero.plist.template
└── com.masato.brain-sync.weekly-review.plist.template
```

### 優先3: 状態管理

現状は直近7日または最新100件ベース。

将来的には:

```text
state.json
または
brain-sync.sqlite
```

へ最終同期カーソルを保存する。

例:

```text
karakeep_last_created_at
cairn_last_updated_at
zotero_last_version
last_weekly_review
```

### 優先4: 関連付け

以下の強い一致から始める。

```text
同一URL
同一DOI
同一GitHub URL
Cairn会話内にKarakeep URL
Cairn会話内にZotero DOI
```

### 優先5: 統合MCP

将来的な `brain-mcp`:

```text
search_all
search_karakeep
search_cairn
search_zotero
search_obsidian
get_related_items
get_project_context
get_open_questions
```

### 優先6: AIによる週次要約

週次レビューに対して:

```text
繰り返し現れたテーマ
新しい仮説
見解の変化
未解決課題
次の調査対象
```

をAIが草案として生成する。

ただし出力先は:

```text
00 Inbox/AI Drafts
```

とし、テーマノートを直接更新しない。

---

## 15. 最終的な利用イメージ

ユーザーは、通常どおり保存・対話・読書を行う。

```text
Karakeepへ保存
Cairnへ会話蓄積
Zoteroへ論文保存
Obsidianへ着想を書く
```

`brain-sync` が自動的に:

```text
各システムの最近の変化を取得
        ↓
Obsidianの90 Autoへ一覧化
        ↓
毎週40 Reviewsへスナップショット
        ↓
人間が確認・選択・統合
```

最終的にはAIに次のように依頼できる状態を目指す。

```text
「継続的システム保証」について、
Karakeep、Cairn、Zotero、Obsidianを横断し、
現在の構想、根拠資料、過去の議論、
未解決課題、次に行うべきことを整理して。
```

---

## 16. 設計原則の要約

```text
Karakeep = 発見したもの
Cairn    = 考えた過程
Zotero   = 根拠資料
Obsidian = 現在の理解
brain-sync = それらをつなぎ、再浮上させる統合層
```

この構成により、情報を保存するだけでなく、過去の関心・思考・判断・根拠を時間を越えて継続的に利用できる外部脳を形成する。
