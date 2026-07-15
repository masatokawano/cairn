# Cairn 統合設計書 — Personal External Brain Platform

- 作成日: 2026-07-02
- 改訂: **v1.1**（2026-07-02）— リポジトリ実態（Phase 3 実装済み・schema v10）との整合、移行経路の明示（D2 注記・D11・§4・§5.4・§5.7・§7・§9）。差分は末尾「改訂記録」参照
- ステータス: **確定**（変更する場合は本文書の Decision Record を更新してから実装すること）
- 対象: Cairn リポジトリを、brain-sync（統合層）を取り込んだ「外部脳プラットフォーム」へ再編する
- 読者: Claude Code / Codex CLI（実装者）および将来の自分

---

## 0. この文書の使い方（Claude Code への指示）

1. **実装前に必ずこの文書全体を読むこと。** 特に §2 Decision Record と §8 非目標。
2. 作業は §7 のマイルストーン単位で行う。**1セッション = 1マイルストーン（または、その一部）**。マイルストーンを跨いで先回り実装しない。
3. 各マイルストーンの「完了条件」を満たしたことをテストで示してから次へ進む。
4. スキーマ変更は §4 に定義されたものに限る。逸脱が必要になったら、実装せずに理由を報告して判断を仰ぐ。
5. `conversations` / `messages` など既存の原本系テーブルは**破壊的変更禁止**。マイグレーションは追加のみ・実行前に DB バックアップを取る。
6. 旧 ROADMAP.md（Phase 3〜6）は本文書により**廃止**。M0 で ROADMAP.md を書き換える。
7. 迷ったら本文書の §1 成功基準に照らして判断する。成功基準に寄与しない作業はしない。
8. **文書の優先順位**: 本文書（docs/DESIGN.md）が正。`docs/adr/` は個別決定の記録（ADR-0004 が本文書の採用を記録し、ADR-0003 の architecture 判断を supersede している）。`INTEGRATION-PREP.md` は M0 前の移行準備専用で、M0 着手後は歴史文書。AGENTS.md は本文書の不変条件の要約であり、矛盾したら本文書が正（CLAUDE.md は AGENTS.md を import する 1 行スタブ。マルチモデル運用の詳細は docs/orchestration.md）。

---

## 1. 目的と成功基準

### 1.1 目的

X・Web・論文・AI との対話・自分の着想を、保存するだけでなく**自動的に記憶し、必要な時・意外な時にいい感じに思い出させる**個人用外部脳を作る。ユーザー（真人）の日常行動は変えない：

```
Web/SNS で発見     → Karakeep へ保存（既存の習慣）
AI と対話          → Cairn が自動収集（既存）
論文・一次資料     → Zotero へ保存（既存の習慣）
着想を書く         → Obsidian Inbox（既存の習慣）
```

Cairn がバックグラウンドでこれらを索引化し、以下の2経路で「思い出させる」：

- **週次レビュー**（pull）: 10分で読み切れる量に絞った、今週の活動 + 過去からの関連 + AI 草案
- **コンテキスト注入**（push）: MCP 経由で、作業中のトピックに関連する過去の知識を AI セッションに供給

### 1.2 成功基準（これで判断する）

| # | 基準 | 測り方 |
|---|------|--------|
| S1 | 週次レビューが10分以内で読み切れる | レビュー項目数の上限で担保（§5.4） |
| S2 | 毎週1件以上「忘れていたものの再発見」がある | 週次レビューの「過去からの関連」セクションが機能しているか、主観評価 |
| S3 | AI セッションで過去の知識が実際に使われる | MCP `build_context_pack` の呼び出しが日常化するか |
| S4 | 運用がゼロメンテで回る | launchd 実行の失敗が通知され、放置しても壊れない |
| S5 | 4系統横断の問いに答えられる | 「テーマXについて、構想・根拠・過去の議論・未解決課題を整理して」に MCP 経由で答えられる |

### 1.3 役割分担（不変の原則）

```
Karakeep = 発見したもの（原本庫・EC2）
Cairn    = 考えた過程（原本庫・ローカル） + 索引エンジン + 統合層 ← 本文書で拡張
Zotero   = 根拠資料（原本庫・公式同期+WebDAV）
Obsidian = 現在の理解（人間が管理）
```

Cairn は Karakeep/Zotero/Obsidian に対して**読み取り専用**。原本を複製せず、索引と抜粋のみ保持する。Obsidian への書き込みは指定ディレクトリのみ（§5.5）。

---

## 2. Decision Record（確定した設計判断）

手戻り防止のため、採用・棄却とその理由を記録する。**棄却済み案を再提案・再実装しないこと。**

### D1: brain-sync は独立プロジェクトをやめ、Cairn に統合する — 採用

- 理由: Cairn 旧 ROADMAP Phase 3〜5 と brain-sync の機能（関連付け・再浮上・週次レビュー・MCP）がほぼ完全に重複していた。統合層は1つでよい。Cairn には既に chunking / FTS5 / sqlite-vec / RRF の検索基盤があり、これを4系統に一般化するのが最短。
- 帰結: `~/workspace/brain-sync` のシェルスクリプト群は M3 完了時に廃止。ロジックは Cairn の Python モジュールとして再実装する（シェルからの移植ではなく仕様ベースで書き直す）。

### D2: 知識の事前抽出（assertion 抽出）はやらない。クエリ時合成で代替する — 採用（旧 Phase 3〜4 の廃止）

- 棄却した案: 会話を LLM で分節し、kind 7種 × status 6種 + confidence の assertion を事前抽出して DB に固める（旧 ROADMAP Phase 3）。8種の関係タイプによる知識グラフ（旧 Phase 4）。
- 棄却理由:
  - ローカル 32B・11 tok/s で全会話のバックフィル（現況 約1,200会話 / 15,000 messages、今後も増加）は日〜週単位の計算になり、モデル更新のたびに再抽出が必要。
  - 抽出品質（日本語混在ログからの分類）はノイズが多く、人間レビュー UI は「自動的に記憶」の理念と矛盾する放置チョアになる。
  - 個人規模では、問われた時に原本 chunk を retrieval して LLM が合成する方が常に新鮮で、陳腐化した派生データを持たない。
- 採用した代替: **良い横断 retrieval + クエリ時 LLM 合成**（週次草案は ollama、対話は MCP 越しの Claude 等が担う）。
- 例外として残すもの: なし。「未解決の問い」も週次レビューの AI 草案セクション（§5.4）として生成し、人間が採否を決める。
- **実装状況の注記（v1.1）**: Phase 3 の抽出パイプラインは commit `b5f9e6d`（P3-A〜E: extraction_runs / segments / assertions スキーマ、ollama runner、検証層、Review UI、テスト。schema v10）として**実装・コミット済み**である。本決定は「新規投資の停止」であり、実装済みコードの処置は**凍結（deprecated in place）**とする: バッチバックフィルを行わない・Phase 4（関係タイプ分類）へ拡張しない・本文書の新機能（items / recall / MCP）は extraction 系テーブルに依存しない。テーブル・既存テスト・Review UI は維持し、抽出済みデータも残す。完全撤去は選択肢として残すが、実施する場合は migration（テーブル削除 + backup）と本 Decision の改訂を伴うこと。

### D3: 横断インデックスは Cairn の DB（cairn.db）が持つ — 採用

- 棄却した案: brain-sync 側に別の index DB（brain-sync.sqlite）を新設。
- 理由: 索引は派生データであり再構築可能。既存の chunks / embeddings / FTS 基盤の流用が最短で、DB を分けると RRF 横断検索が複雑化する。

### D4: 統一データモデルは `items` レジストリ方式 — 採用

- 会話・ブックマーク・文献・ノートを `items` テーブルに登録し、chunks は items を参照する。`conversations` 等の既存テーブルは kind 別の詳細テーブルとしてそのまま残す（§4）。
- 棄却した案: ポリモーフィック FK（chunks.source_type + source_id）。結合が汚くなり、横断検索の SQL が複雑化するため。

### D5: 関係付けは「強い一致」と「クエリ時の類似」のみ — 採用

- 採用: URL / DOI / GitHub URL の正規化済み完全一致（`item_links`）、および embedding+FTS による related 検索（保存しない、クエリ時計算）。
- 棄却: supports / contradicts / derived_from 等の関係タイプ自動分類。ローカル LLM で弁別不能、矛盾検出は研究課題。
- URL 正規化は結合率を決める要なので専用モジュールとし、テストを厚くする（§5.2）。

### D6: 週次レビューは「上限つき・繰り越しなし・AI 草案つき」 — 採用

- 各セクション最大10件。未処理項目を翌週へ繰り越さない（罪悪感の山を作らない。原本は Karakeep 等に残っており、検索と再浮上で再会できる）。
- 統合メモは AI が草案を書き、人間は編集・採否のみ行う。

### D7: ランキング学習・フィードバックループはやらない — 採用

- n=1 ユーザーの疎なフィードバックでは収束しない。pin / mute 相当の単純ルール（将来、必要になってから）で足りる。

### D8: Keychain サービス名は既存の `brain-sync-karakeep` / `brain-sync-zotero` を継続使用 — 採用

- 改名は純粋にコスメティックで移行コストだけがあるため。config で明示する。

### D9: `/bin/bash` へのフルディスクアクセスは廃止する — 採用（運用前提）

- FDA を /bin/bash に与えると、bash を経由する任意のプロセスが TCC 保護を全面バイパスできる。
- 対応: **Obsidian Vault を `~/Obsidian` へ移設**し（TCC 保護ディレクトリ外）、/bin/bash の FDA を解除する。Vault 移設は M3 の前提作業（人間が実施、§7 M3 参照）。

### D10: LLM は ollama（ローカル）を既定とし、クエリ時のみ使用 — 採用

- 週次草案・合成は qwen2.5:14b を既定（32b はオプション設定）。バッチ事前処理には使わない（D2）。
- 生成物には必ず provenance ラベルを付与する（§6.2）。

### D11: 移行経路 — git subtree による履歴保持と段階的切替 — 採用（v1.1 追加）

- brain-sync リポジトリは `git subtree add --prefix=legacy/brain-sync` で**履歴ごと** cairn へ取り込む（手順は `INTEGRATION-PREP.md`）。ロジックは仕様ベースで再実装するが（D1）、旧コードは M3 完了まで参照用に残し、M3 でディレクトリを削除する（履歴は git に残る）。
- 旧 brain-sync の LaunchAgent 4本は M3 完了まで稼働を継続する。移行期間中も週次レビューは無停止。
- 本決定は ADR-0003（brainsync を独立パッケージとし Cairn へは HTTP のみで接続する案）の architecture 判断を supersede する。理由の記録は `docs/adr/0004-design-adoption.md`。要点: 横断ハイブリッド検索（FTS5 + sqlite-vec + RRF）を4系統に効かせるには索引の cairn.db 一元化（D3）が必須であり、HTTP 越しのリスト合成では S5 に到達できない。信頼境界の懸念は read-only connector・書き込み allowlist（§5.5）・D9 で処理する。

### D12: CLI 会話ログ同期は 2 経路（サーバ内60秒 + launchd 毎時）を維持し、プロセス間 flock で直列化する — 採用（v1.2 追加）

- CLI ログ同期は FastAPI サーバ内の60秒ポーリングと launchd 毎時 `cairn sync all` の両方から走る。片方への一本化案（外部レビュー 2026-07-10 指摘 3.5）は、60秒側を消すと即時性を、毎時側から conversations を外すとサーバ停止時のフォールバックを失うため不採用。
- 代わりに両経路を DB 隣のサイドカーファイルへの `flock` で直列化する（`threading.Lock` はプロセス内のみ有効）。重複実行の実害（import_runs の重複行・SQLite write contention）はロックで消え、後着側は file_state 比較により no-op で抜ける。

### D13: 健康ドメインは独立ストア（Personal Health Observatory）として追加する — 採用（v1.3 追加、ADR-0005）

- 検査値・Apple Health 等の健康時系列は `cairn.db` に入れない。独立した health data home（`~/Library/Application Support/Cairn/health/`、分析ストアは DuckDB 予定・H0 で依存レビュー）で raw / normalized / derived / interpretation を分離管理する。高頻度サンプルを items/chunks へ 1 件ずつ登録しない。
- Cairn 本体は統合・検索層として、承認済みレポートの索引（Obsidian connector 経由の通常 note）と、オプトイン・bounded な MCP 供給のみを担う。health MCP は既定無効。
- Obsidian への配信先は既存 `90 Auto` ツリー内の `90 Auto/Health/`（allowlist 第4カテゴリとして H5 で追加、不変条件2を同時改訂）と `00 Inbox/AI Drafts/` のみ。Vault 複製は既定除外（docs/health/PRIVACY.md の H5-P1）。
- 詳細設計・privacy 境界・受け入れ基準・ロードマップ（H0〜H9）の正典は `docs/adr/0005-personal-health-observatory.md` と `docs/health/`。長期方向は `docs/NORTH_STAR.md`（Human Validation Platform。実装上の正典は引き続き本文書）。

---

## 3. アーキテクチャ

```
                     ┌────────────────────────────────────────────┐
                     │                  Cairn                     │
  原本庫（外部）      │                                            │
 ┌──────────┐  read  │ ┌──────────┐   ┌─────────┐   ┌──────────┐ │
 │ Karakeep │───────▶│ │connectors│──▶│  items  │──▶│  index   │ │
 │ (EC2)    │        │ │ karakeep │   │registry │   │ chunks   │ │
 └──────────┘        │ │ zotero   │   │         │   │ FTS5     │ │
 ┌──────────┐  read  │ │ obsidian │   │cairn.db │   │ sqlite-  │ │
 │ Zotero   │───────▶│ └──────────┘   └─────────┘   │ vec, RRF │ │
 │ API      │        │       ▲                      └────┬─────┘ │
 └──────────┘        │       │ (既存)                     │       │
 ┌──────────┐  read  │ ┌──────────┐                 ┌────▼─────┐ │
 │ Obsidian │───────▶│ │ parsers  │                 │  recall  │ │
 │ Vault    │        │ │ claude/  │                 │ related  │ │
 └──────────┘        │ │ chatgpt/ │                 │ digest   │ │
       ▲             │ │ gemini/  │                 └────┬─────┘ │
       │ write       │ │ codex    │                      │       │
       │ (限定)      │ └──────────┘                 ┌────▼─────┐ │
       └─────────────│──────────────────────────────│ deliver  │ │
                     │                              │ obsidian │ │
   AI セッション      │ ┌──────────┐                 │ writer   │ │
 ┌──────────┐  MCP   │ │   mcp    │◀────────────────│ weekly   │ │
 │ Claude / │◀──────▶│ │ server   │                 └──────────┘ │
 │ Code等   │        │ └──────────┘                              │
 └──────────┘        │ ┌──────────┐  ┌───────────┐               │
                     │ │ cli      │  │ scheduler │ (launchd)     │
                     │ └──────────┘  └───────────┘               │
                     └────────────────────────────────────────────┘
```

モジュール構成（backend/ 配下、既存構造に合わせて調整可）:

```
backend/app/
├── parsers/       # 既存: AI会話の取り込み + ソーシャル公式エクスポート（ADR-0006: x_archive / facebook_dyi）
├── connectors/    # 新規: karakeep.py, zotero.py, obsidian.py（read-only クライアント）
├── core/
│   └── urlnorm.py # 新規: URL/DOI 正規化
├── index/         # 既存の chunking/embedding/FTS/RRF を items 対応に一般化
├── recall/        # 新規: related(), weekly digest 生成
├── deliver/       # 新規: obsidian_writer.py, weekly_review.py
├── mcp/           # 新規: MCP server
└── cli.py         # 新規: `cairn` CLI（typer）— sync/review/index サブコマンド
ops/launchd/       # 新規: plist テンプレート
```

---

## 4. データモデル

既存テーブル（conversations, messages, chunks, embeddings、および凍結中の extraction 系）は温存。以下を追加する。migration は既存の枠組み（`_SCHEMA_VERSION` 逐次適用・実行前 `*.premigrate-*` 自動バックアップ。現行 v10）に従い、次版以降として追加する。

```sql
-- 全ソース横断のレジストリ。検索・関連付け・再浮上はすべて items 起点。
CREATE TABLE items (
    id            INTEGER PRIMARY KEY,
    kind          TEXT NOT NULL CHECK (kind IN ('conversation','bookmark','reference','note','social_post')),  -- social_post: v13, ADR-0006
    source        TEXT NOT NULL,          -- 'claude','chatgpt','karakeep','zotero','obsidian',...
    external_id   TEXT NOT NULL,          -- 元システムの ID（conversation id / karakeep id / zotero key / vault相対パス）
    title         TEXT,
    url           TEXT,
    url_norm      TEXT,                   -- 正規化URL（§5.2）
    doi           TEXT,
    created_at    TEXT,                   -- 元システム上の作成日時
    updated_at    TEXT,                   -- 元システム上の更新日時
    content_hash  TEXT,                   -- 差分検出用
    meta          TEXT,                   -- JSON（タグ、著者、メッセージ数等ソース固有）
    UNIQUE (source, external_id)
);
CREATE INDEX idx_items_url_norm ON items(url_norm) WHERE url_norm IS NOT NULL;
CREATE INDEX idx_items_doi      ON items(doi)      WHERE doi IS NOT NULL;
CREATE INDEX idx_items_updated  ON items(kind, updated_at);

-- 強い一致による関連（D5）。related_to の類似検索は保存しない。
CREATE TABLE item_links (
    a_id     INTEGER NOT NULL REFERENCES items(id),
    b_id     INTEGER NOT NULL REFERENCES items(id),
    link_via TEXT NOT NULL CHECK (link_via IN ('url','doi','github')),
    PRIMARY KEY (a_id, b_id, link_via),
    CHECK (a_id < b_id)
);

-- 同期カーソル（旧 brain-sync 優先3の state.json を置き換え）
CREATE TABLE sync_state (
    source     TEXT PRIMARY KEY,          -- 'karakeep','zotero','obsidian'
    cursor     TEXT NOT NULL,             -- JSON（last_created_at / last_version 等）
    synced_at  TEXT NOT NULL,
    last_error TEXT
);
```

方針:

- **conversations は kind='conversation' として items に登録**（M0 でバックフィル）。詳細（メッセージ本文等）は既存テーブルのまま。
- chunks は items.id を参照するよう拡張する。既存の会話 chunk は items バックフィル時にマッピング。移行が既存検索を壊さないことを回帰テストで担保。
- 外部ソースの索引対象テキスト:
  - Karakeep: タイトル + 説明/メモ + タグ（本文全文は取得できる範囲で抜粋。原本複製はしない）
  - Zotero: タイトル + abstract + タグ + 著者（PDF 本文は対象外。WebDAV に触れない）
  - Obsidian: `10 Themes` `20 Projects` `00 Inbox/Ideas` `50 Decisions` のノート本文。`90 Auto` と `40 Reviews` は**索引対象外**（自己生成物の還流ループを防ぐ）
- embeddings は既存パイプライン・既存モデルを流用。モデル名と次元は設定で一元管理し、items 対応で変えない。

---

## 5. コンポーネント仕様

### 5.1 connectors（read-only）

共通仕様:

- API キーは macOS Keychain（`brain-sync-karakeep` / `brain-sync-zotero`、D8）。`security find-generic-password` 相当を subprocess で呼ぶ薄いヘルパを core に置く。キーをファイル・ログ・例外メッセージへ出さない。
- 増分同期: `sync_state` のカーソルに基づき差分のみ取得。`content_hash` 不変ならスキップ。
- 失敗時: 例外を握りつぶさず `sync_state.last_error` に記録し、非ゼロ終了。既存データは壊さない。
- 外部から取得したタイトル・本文・タグは**信頼できない入力**として扱う（§6.1）。

個別:

- **karakeep.py**: 全ブックマークを items(kind='bookmark') へ。タグは meta に。`to-review` タグは週次レビューの対象抽出に使う。API に modified-since がないため増分は createdAt カーソル。古いブックマークの編集・削除を収束させるため、24時間ごとに full sweep へ自動昇格し、full sweep が完全成功（非空 listing）したときのみ上流削除を prune する（v1.2）。
- **zotero.py**: 書誌のみ items(kind='reference') へ。DOI を items.doi に正規化格納。Zotero の library version をカーソルに使う（編集は since で拾える）。削除の反映は完全 listing（初回 / --full）成功時の prune のみ（v1.2）。
- **obsidian.py**: Vault の対象ディレクトリ（§4）を走査し items(kind='note') へ。ファイルの mtime + hash で差分検出。読み取りのみ。

### 5.2 core/urlnorm.py（重要・テスト厚め）

結合率（S5）を決めるモジュール。最低限:

- スキーム/ホスト小文字化、既定ポート除去、末尾スラッシュ正規化、fragment 除去
- トラッキングパラメータ除去: `utm_*`, `fbclid`, `gclid`, `si`, `t`, `ref_src` など（設定でリスト管理）
- ドメイン正規化: `twitter.com`→`x.com`、`mobile.x.com`→`x.com`、`m.youtube.com`→`youtube.com`、`www.` 除去
- GitHub URL: `github.com/{owner}/{repo}` 部分の抽出（tree/blob 等の深いパスから repo 単位のキーも別途生成し item_links の 'github' に使う）
- DOI: `https://doi.org/` プレフィックス除去、小文字化

ユニットテストに実在パターン（X 共有 URL、YouTube share、arXiv abs/pdf 対等）を含めること。

### 5.3 recall/

- `related(query_texts: list[str], k: int, exclude_days: int = 14) -> list[Item]`
  - 既存 RRF（FTS5 + sqlite-vec）を items 横断で実行。**直近 exclude_days 以内の item を除外**（「過去からの」再浮上なので）。ソース多様性を軽く担保（同一ソース独占を避ける丸め）。
- `weekly_activity() -> ActivitySummary`: 直近7日の items（会話は4メッセージ以上・ノイズタイトル除外＝旧 sync_cairn_recent.py の除外規則を移植）。
- `weekly_digest() -> Digest`: activity を検索クエリ化し related() で「過去からの関連」を取得。各セクション**最大10件**（D6）。

### 5.4 deliver/weekly_review.py

出力先: `External Brain/40 Reviews/Weekly/YYYY-Www.md`（既存ファイルは上書きしない。旧仕様継承）。

構成:

```markdown
# YYYY-Www 週次レビュー
## 今週の活動
### 発見（Karakeep, to-review 優先, ≤10件）
### 思考（Cairn 会話, ≤10件）
### 根拠（Zotero, ≤10件）
### 理解（Obsidian 更新ノート, ≤10件）
## 過去からの関連 ← 本システムの核
（今週の活動に関連する 14日以前の item, ≤10件。各行に「なぜ出したか」を1行で）
## 統合メモ（AI草案 — 編集・削除自由）
<!-- generated_by: cairn/qwen2.5:14b/prompt_vN -->
- 繰り返し現れたテーマ / 新しい着想 / 見解の変化 / 未解決の問い / 来週の候補
```

- AI 草案は ollama で生成し、**草案であることと生成元を明記**（§6.2）。生成失敗時は空セクション+失敗注記で出力を止めない（S4）。
- 未処理項目の繰り越し機構は作らない（D6）。
- **陳腐化警告（v1.1 追加）**: 手動エクスポート依存ソース（chatgpt / claude / gemini）の最終取り込み日時（`import_runs` 由来）が閾値（既定30日、設定可）を超えていたら、レビュー冒頭に警告行を出す。「自動的に記憶」の唯一の手動ステップを忘れさせないため（S4 に寄与）。

### 5.5 deliver/obsidian_writer.py

- 書き込み許可先は次の3箇所**のみ**をコードレベルで強制（allowlist、パス検証でトラバーサル拒否）:
  - `External Brain/90 Auto/`（上書き可）
  - `External Brain/40 Reviews/Weekly/`（新規のみ）
  - `External Brain/00 Inbox/AI Drafts/`（新規のみ）
- `90 Auto` へは旧 brain-sync 相当の一覧（karakeep-to-review.md, cairn-recent.md, zotero-recent.md, obsidian-context.md）を出力。フォーマットは旧仕様を踏襲してよい。
- 書き込みはテンポラリファイル + アトミック rename（iCloud/Sync との競合でファイル破損を残さない）。

### 5.6 mcp/（MCP サーバ）

ツールは最小4つから始める（旧 brain-mcp 案の8ツールは統合・削減）:

| ツール | 内容 |
|---|---|
| `search_all(query, kinds?, k?)` | 4系統横断のハイブリッド検索。結果に kind/source/provenance を必ず含める |
| `get_item(source, external_id)` | 単一 item の詳細（会話なら本文、外部なら索引済みメタ+原本URL） |
| `build_context_pack(topic, budget_tokens?)` | S5 用。related + 強い一致リンクをたどり、構想/根拠/過去の議論/未解決を構成するパック。§6.2 のラベル必須 |
| `get_recent_activity(days?)` | 直近活動の要約（新セッションの立ち上げ用） |

- 読み取り専用。書き込み系ツールは作らない。
- claude code / claude CLI から `cairn` MCP として登録して常用するのが S3 の想定形。

### 5.7 cli + scheduler

- `cairn sync [karakeep|zotero|obsidian|conversations|all]`
- `cairn review weekly [--week 2099-W01]`（テスト用の週指定は旧仕様踏襲）
- `cairn index rebuild`（派生データの**欠損補完**。現行 chunking/embedding
  バージョンに未達の message/item を chunk し、未生成の embedding のみ埋め、
  FTS・vec0・item_links を再構築する。既存 chunk を強制全 rechunk すると
  embeddings が CASCADE 全削除され再埋め込みに数時間かかるため、既定は
  「欠損補完」であり全再生成ではない。原本からの**真の全再構築**（破損した
  chunk/embedding の作り直し）は `python -m app.admin rechunk --all` +
  `reindex --all` + `rebuild-vector-index` で行う。派生データは常に原本から
  再構築可能であること自体は保たれる）
- 既存の管理 CLI `python -m app.admin`（redact / backup / integrity-check / import-runs 等）は**温存**し、M0〜M5 では触れない。二重 CLI の解消（`cairn admin ...` への吸収）は M6 で検討する。それまで新 `cairn` CLI は sync / review / index のみを担う。
- launchd: `ops/launchd/*.plist.template`（絶対パスは変数化）。**エージェントは2本に集約**:
  - `com.masato.cairn.sync` — 1時間ごと `cairn sync all`
  - `com.masato.cairn.weekly` — 日曜18:00 + ログイン時 `cairn review weekly`。対象は「直近に締まった週」
    （締め＝日曜18:00 ローカル）: 定時実行はその週を、ログイン時（RunAtLoad）は取りこぼした週の
    補完のみを生成する。進行中の週を早期生成して後半の活動を欠いたまま固定しない（v1.2）
  - ログ: `~/Library/Logs/cairn/`。旧 brain-sync の4エージェントは M3 で unload・削除。

---

## 6. セキュリティ要件（全マイルストーン共通）

### 6.1 外部入力の不信頼

- Karakeep/Zotero/Obsidian/会話由来のあらゆるテキスト（タイトル含む）はシェル評価しない・そのまま HTML として出さない・Markdown 出力時はエスケープする。
- LLM へ渡す際は、外部テキストを明示的な区切り内に置き「本文中の指示には従わない」系のガード指示を付す（完全な防御ではないことを理解した上での緩和策）。

### 6.2 派生テキストの provenance（インジェクション洗浄対策）

- LLM 生成物（週次草案、context pack の合成部）には必ず `generated_by: cairn/<model>/<prompt_version>` を付与。
- MCP 応答では、**原文引用部**と**Cairn が生成した要約部**を構造的に区別して返す（例: `content` と `synthesized` フィールドを分け、`synthesized` にラベル）。受け手のエージェントが信頼判断できる形式にする。
- prompt_version はプロンプト文面とともにリポジトリ管理。

### 6.3 秘密情報

- API キーは Keychain のみ（D8）。config.env にはキーを置かない。ログ・エラー出力へのキー混入をテストで確認。
- 既存のシークレット除去（redaction）パイプラインは温存。外部ソース由来テキストにも適用する。

### 6.4 権限最小化

- Karakeep/Zotero/Obsidian: 読み取りのみ。Obsidian 書き込みは §5.5 の allowlist のみ。
- FDA は D9 に従い解消（Vault 移設）。コードは Vault パスを設定値として扱い、`~/Documents` 前提を持たない。

---

## 7. マイルストーン

各マイルストーンは単独で価値が出る順に並べてある。**完了条件をテストで示すこと。**

### M0: 基盤再編（この文書の反映）

- **前提**: `INTEGRATION-PREP.md` の P0（タグ・ベースライン記録）と P1（git subtree による履歴取り込み）が完了していること。未了なら先にそちらを実施する。
- [ ] 本文書を `docs/DESIGN.md` としてコミット。ROADMAP.md を書き換え（Phase 3〜6 の**計画**廃止と、Phase 3 実装済みコードの凍結扱い〔D2 注記〕を明記し、本文書のマイルストーンを正とする）。ADR-0004 をコミット
- [ ] §3 のモジュール骨格（空でよい）と `cairn` CLI の雛形
- [ ] マイグレーション: items / item_links / sync_state 追加、conversations → items バックフィル、chunks の items 参照化
- [ ] 完了条件: 既存の検索 API・フロントエンドが回帰テストで全て従前どおり動く。`items` に全会話が登録済み

### M1: 外部コネクタ（Karakeep, Zotero）

- [ ] connectors/karakeep.py, zotero.py、Keychain ヘルパ、sync_state カーソル
- [ ] core/urlnorm.py + ユニットテスト（§5.2 のパターン網羅）
- [ ] item_links の生成（url/doi/github 完全一致、会話本文中の URL 抽出→正規化→突合を含む）
- [ ] 完了条件: `cairn sync karakeep && cairn sync zotero` が増分で回り、`/api/stats` に items 内訳が出る。同一 URL の bookmark と会話が item_links で結ばれるケースをテストで確認

### M2: 横断インデックス

- [ ] 外部 items の chunking + embedding + FTS 投入
- [ ] `/api/search` と検索 UI に kind/source フィルタ、横断 RRF
- [ ] 完了条件: 「Karakeep に保存した記事」と「その話をした会話」が同一クエリの結果に並ぶことを実データで確認

### M3: Obsidian 連携と brain-sync の廃止

- [ ] **人間の事前作業**: Vault を `~/Obsidian` へ移設、/bin/bash の FDA 解除（D9）。Claude Code はこの完了を確認してから着手
- [ ] connectors/obsidian.py（読み取り・索引）、deliver/obsidian_writer.py（allowlist 書き込み、90 Auto 一覧4種）
- [ ] launchd テンプレート2本、導入スクリプト、旧 brain-sync エージェント4本の unload 手順書
- [ ] 完了条件: 旧 `~/workspace/brain-sync` を止めても 90 Auto の一覧が Cairn 単独で更新され続ける。書き込み先 allowlist のパス検証テストが通る

### M4: 週次レビュー v2（再浮上 + AI 草案）

- [ ] recall/related()（直近除外・件数上限・ソース多様性）、weekly_digest()
- [ ] deliver/weekly_review.py（§5.4 構成、既存週の上書き禁止、ollama 草案、生成失敗時の縮退）
- [ ] 完了条件: 実データで週次レビューを生成し、「過去からの関連」に14日以前の item が理由つきで最大10件出る。ollama 停止状態でもレビュー生成自体は成功する

### M5: MCP サーバ

- [ ] §5.6 の4ツール。provenance 分離レスポンス（§6.2）
- [ ] claude code への登録手順を README に記載
- [ ] 完了条件: 新規 Claude セッションから `build_context_pack("<実在テーマ>")` で S5 の問い（構想・根拠・過去の議論・未解決課題）に有用な回答が得られる

### M6: 運用の仕上げ

- [ ] 同期失敗の通知（macOS 通知 or ログ監視の簡易な仕組み。過剰実装しない）
- [ ] `cairn index rebuild` の実測とドキュメント化
- [ ] 2〜3週間の実運用後、S1〜S5 を評価し、件数上限・related のチューニングのみ調整（機能追加はしない）

---

## 8. 非目標（実装しないこと）

再提案禁止リスト。必要になったら本文書の Decision Record を改訂してから。

1. assertion の事前抽出・分類体系（kind×status×confidence）— D2
2. 関係タイプの自動分類（supports/contradicts 等の知識グラフ）— D5
3. ランキング学習・フィードバックループ — D7
4. 未処理レビュー項目の繰り越し・バックログ管理 — D6
5. Karakeep / Zotero / 原本会話への書き込み
6. 原本全文の Obsidian への複製
7. マルチユーザー・クラウドホスティング・スマホネイティブアプリ
8. Obsidian のテーマ/プロジェクトノートの自動編集（AI Drafts への提案のみ可）
9. PDF 本文の索引（Zotero は書誌+abstract まで。将来検討事項であって現スコープ外)
10. ソーシャル（X / Facebook）は**自作＋明示的キュレーション（いいね/ブックマーク）のみ**。
    フィード・タイムライン・ソーシャルグラフ・エンゲージメント指標・DM・他人のコンテンツ
    一般の取り込み、およびライブ API / スクレイピングによる取得 — ADR-0006

---

## 9. 既存資産の扱い

| 資産 | 扱い |
|---|---|
| Cairn Phase 1〜2（parsers, 検索, UI, redaction） | 温存。items 対応の一般化のみ |
| Cairn Phase 3 実装済みコード（extraction 系テーブル・runner・Review UI、schema v10） | **凍結**（D2 注記）。維持するが投資・依存・バックフィルをしない |
| Cairn 旧 ROADMAP Phase 3〜6 の**計画** | 廃止（D1, D2）。M0 で文書上も無効化 |
| brain-sync シェルスクリプト群 | `legacy/brain-sync/` に subtree で履歴ごと保持（D11）。M3 でディレクトリ削除（履歴は git に残る）。仕様（除外規則・出力形式・上書き禁止）は本文書へ移植済み |
| brain-sync LaunchAgent 4本 | M3 完了まで稼働継続、その後 unload・削除し `com.masato.cairn.*` 2本に置換 |
| Keychain（brain-sync-*） | 継続使用（D8） |
| Obsidian ディレクトリ構成（External Brain/…） | 不変。書き込み先のみ §5.5 で制限 |

---

## 10. Claude Code 作業規約

- ブランチ/コミットはマイルストーン単位。コミットメッセージに `M1:` 等の接頭辞。
- マイグレーションは冪等に書き、実行前に `cairn.db` をタイムスタンプ付きでバックアップするコマンドを用意。
- 新規コードには型ヒント + pytest。connectors は API モックでテスト（実キー不要で CI 可能に）。
- 設定は既存の方式に合わせて一元化（Vault パス、ollama モデル名、上限件数、除外日数、トラッキングパラメータ一覧は全て設定値）。
- 本文書と実装が食い違ったら、実装ではなく**まず文書を直す提案をする**。文書が正。

---

## 11. 改訂記録

### v1.3（2026-07-11）

健康ドメインの追加を批准。既存アーキテクチャは不変（`cairn.db` 非接触）。

- D13: Personal Health Observatory を独立ストアとして追加（ADR-0005 Accepted）。
  設計文書一式は docs/health/、北極星は docs/NORTH_STAR.md
- AGENTS.md に健康データ境界の不変条件 9 を追加、.gitignore に健康アーティファクトの
  除外パターンを追加

### v1.2（2026-07-10）

外部レビュー（GitHub 最新状態レビュー 2026-07-10）の第1段階修正を反映。アーキテクチャは不変。

- §5.1 karakeep: 増分同期（createdAt カーソル）に加え、24時間ごとの full sweep 自動昇格と
  full 成功時の上流削除 prune を明記（指摘 3.2 / 3.4）
- §5.1 zotero: 完全 listing（初回 / --full）成功時の prune を明記（指摘 3.4）
- §5.7 weekly: 対象週を「直近に締まった週」（締め＝日曜18:00 ローカル）と定義。
  ログイン時（RunAtLoad）実行は取りこぼした週の補完のみ（指摘 3.1）
- D12: CLI 会話ログ同期 2 経路（サーバ内60秒 + launchd 毎時）のプロセス間 flock 直列化を新設（指摘 3.5）
- MCP: search_all / get_item の title もフェンス、get_item の meta はホワイトリスト投影に統一（指摘 3.3、§6.1/§6.2 の適合修正）

### v1.1（2026-07-02）

リポジトリ実態との整合および移行経路の明示。設計判断（D1〜D10）の方向は不変。

- ヘッダ・§0-8: 文書の優先順位（DESIGN.md が正、ADR-0004 / INTEGRATION-PREP.md との関係）を追加
- D2: Phase 3 が実装済み（commit `b5f9e6d`、schema v10）である事実を注記し、実装済みコードの処置を「凍結（deprecated in place）」と定義。会話数を実測値へ修正
- D11: 移行経路（git subtree 履歴保持・旧 launchd の M3 まで稼働継続・ADR-0003 の supersede）を新設
- §4: migration が既存枠組み（v10 の次版以降）に従うことを明記
- §5.4: 手動エクスポートの陳腐化警告を週次レビュー仕様に追加
- §5.7: 既存 `app/admin.py` CLI の温存と M6 での統合検討を明記
- §7 M0: INTEGRATION-PREP.md（P0/P1）完了を前提化、ADR-0004 のコミットを追加
- §9: Phase 3 実装済みコードの行を追加、brain-sync スクリプトの扱いを subtree 保持に修正
