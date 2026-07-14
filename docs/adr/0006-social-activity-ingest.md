# ADR-0006: Ingest self-authored and self-curated social activity (X / Facebook)

- Status: Draft（pre-ADR。批准前の設計提案。実装着手前にオーナー承認が必要）
- Date: 2026-07-14
- Decision owner: Repository owner
- Related documents:
  - `docs/DESIGN.md`（§8 非目標 / §5.1 connectors・parsers / D3 索引一元化）
  - `docs/NORTH_STAR.md`（§10 「なってはならないもの」= 監視基盤化しない）
  - `docs/HORIZONS.md`（1.1 予測台帳 / 1.2 belief diff）
  - `backend/app/parsers/`（既存 export parser 群）、`backend/app/core/urlnorm.py`

## Context

Cairn は現在 4 系統（会話 / Karakeep=発見 / Zotero=根拠 / Obsidian=理解）を
`items` レジストリに一元索引する。ここへ X / Facebook の「アクティビティ」を
加える提案。

ソーシャルは放置すると消費フィードの濁流であり、NORTH_STAR §10 の反-目標
「取得可能だからという理由だけで全部集める監視基盤」に最も近づく危険領域である。
したがって本 ADR の核心は「**何を取り込むか**」ではなく「**何を取り込まないか**」の
線引きにある。

オーナーの明示指定（2026-07-14）で取り込み対象は次に限定された:

| ソース | 取り込む | 除外 |
|---|---|---|
| Facebook | 自分の投稿 + 自分が書いたコメントのみ | 他人のコメント・交友・写真・広告・DM・フィード |
| X | 自分のポスト + 自分のリプライ + いいね（本文込み） + ブックマーク（本文込み） | フィード・フォロワー分析・DM |

## Decision proposal

### 1. 取得は公式エクスポートのみ（ライブ API・スクレイピングを採らない）

- X「データのアーカイブ」ZIP、Facebook「Download Your Information (DYI)」ZIP を
  parser で取り込む。**既存の chatgpt/claude/gemini export と同一パターン**
  （`app/parsers/` に `x_archive.py` / `facebook_dyi.py` を追加）。
- ライブ API は不採用: X API は有料・制限が厳しく、read-only connector 化は
  壊れやすく不変条件 1 の read-only 精神とも噛み合わない。スクレイピングは
  ToS 違反・脆弱で論外。
- connectors（ライブ read-only API 用）ではなく parsers（静的エクスポート用）が
  正しいスロット。

### 2. 取り込み対象は「自作」と「明示的キュレーション」の 2 類型に限る

フィード・タイムライン・ソーシャルグラフ・エンゲージメント指標・DM は**取り込まない**。
取り込むのは次の 2 つだけ:

- **自作コンテンツ**（自分の発言そのもの）
- **明示的キュレーション**（自分が like / bookmark という行為で選んだもの）

受動的に流れてきたもの（閲覧・おすすめ・他人の投稿一般）は一切保持しない。
この線引きを DESIGN.md §8 に一行追加して恒久化する。

### 3. 2 類型を既存の 2 kind へ写像する（新規表面積を最小化）

- **自作 → 新 `items.kind='social_post'`**。役割は「思考・発信」。
  「トピック Y について自分が X で何と言ったか」で絞れるよう kind を分ける。
  公開予測・belief diff（HORIZONS 1.1/1.2）の実データ源になり得る。
- **いいね/ブックマーク → 既存 `items.kind='bookmark'` を再利用**。
  「意図的に保存した他人のコンテンツを本文ごと保持」は **Karakeep が既に
  行っている posture** であり、新しいプライバシー姿勢を導入しない。
  meta に `{social_source:'x', action:'like'|'bookmark', author:'@handle'}`。
  `core/urlnorm.py` により、X ブックマークと同一 URL の Karakeep bookmark は
  既存の `item_links` で自動リンクし、重複排除がタダで付く。

### 4. 外部由来テキストは untrusted（不変条件 4 の徹底）

ソーシャル本文は敵対的テキスト（プロンプトインジェクション・詐欺 URL）の巣。
MCP／LLM へ渡す際は既存の `CAIRN_ARCHIVE_DATA` フェンスを必ず適用。
シェル評価しない、markdown 出力時はエスケープ。

### 5. エンゲージメント指標は非索引メタに留める

いいね数・RT 数・フォロワー数等は meta に保存してよいが、検索・ランキングに
使わない（§8「ランキング学習」非目標）。

## Consequences

### Positive

- 新規表面積が最小: `social_post` kind 1 個 + parser 2 本。既存の索引・
  検索・item_links・MCP フェンスをそのまま再利用。
- 自作ポストが検証ループ（公開予測の照合・過去の主張の想起）に接続する。
- urlnorm による Karakeep との自動 dedup/link。
- 取得が静的エクスポートなので、API 価格・仕様変更に影響されない。

### Negative

- `items.kind` の CHECK 制約変更（schema v12 → v13、追加のみ migration）。
- いいね/ブックマーク本文は第三者コンテンツをフル保持する（Karakeep と同型の
  リスクだが、対象が増える）。境界の明文化と fence が前提。
- エクスポートは手動取得（月1回以下）。ライブ更新はしない。

## Alternatives considered

- **ライブ API connector**（X API v2 等）: 却下。有料・脆弱・read-only 精神と
  不整合。
- **フィード/タイムライン全体の取り込み**: 却下。§8・NORTH_STAR §10 の監視基盤化
  非目標に真っ向から反する。
- **いいね/ブックマークを参照のみ（本文非保存）で保持**: 検討したが、オーナー
  指定により本文フル保存を採用（Karakeep と同 posture のため posture 上の新規性
  なし）。
- **新ドメインストア化**（health のように cairn.db から分離）: 却下。ソーシャル
  投稿は全文検索・横断索引の対象そのものであり、health（高頻度数値時系列）と
  違って cairn.db 一元索引（D3）に載せるのが適切。

## Open questions（批准前に確定）

1. **X ブックマークの取得可否**: 公式アーカイブにブックマークが含まれない
   可能性がある。含まれなければブックマークだけ有料 API 依存となり方針外。
   **「アーカイブにあれば取り込む、無ければ対象外」**とし、実 export を 1 つ
   確認して確定する（いいねは `like.js` の `fullText` で確実に取得可能）。
   **未検証**: 手元にあるのは Facebook DYI のみで X アーカイブは未入手。
2. **削除・編集の扱い**: 原本（items）は破壊せず、再エクスポートは既存の
   差分インポート。ソーシャルで消した過去発言が Cairn に残ることの是非は
   本人決定（既定: 残す。原本不変の原則に従う）。
3. ~~**FB「自分のコメント」の抽出単位**~~ → **解消**（下記「実 export 検証」参照）。
   `comments_and_reactions/comments.json` は DYI が既に本人のコメントのみに
   絞っており（distinct author = 1）、追加のフィルタ不要。
4. **FB コメントの宛先（誰の投稿へのコメントか）を残す**（オーナー決定
   2026-07-14）: 各コメントの `title` は「◯◯さんの投稿／写真にコメント
   しました」「◯◯さんのコメントに返信しました」形式で、コメント先の
   投稿者名を含む。これは**本人の行為の文脈（宛先）であり自作以外の本文
   取り込みではない**（他人の投稿本文は comments.json に存在せず取り込まない）。
   よって parser は**本人のコメント本文 + timestamp に加え、復号した `title`
   を宛先文脈として保持する**（誰の投稿へのコメントだったかが残る）。
   宛先投稿者を構造化フィールドに分離するかは実装時裁量（title 文字列の
   保持で最低要件は満たす）。

## 実 export 検証（2026-07-14、Facebook DYI）

実 Facebook DYI ZIP（1 件）の**構造のみ**を確認（本文はリポジトリ／テストに
持ち込まない。不変条件 9 の精神）。判明した実装事実:

- **自作コメント**: `comments_and_reactions/comments.json` →
  `comments_v2[]`、各 `data[].comment.{timestamp, comment, author}` + `title`。
  10,024 レコード中 9,832 が本文あり（残りはスタンプ／写真のみ → skip）。
  author は全件同一（本人）。
- **自作投稿**: `posts/your_posts__check_ins__photos_and_videos_{1..5}.json` →
  レコード配列 `{timestamp, data[].post, attachments[].data[].external_context.url,
  title}`。1 ファイル 10,000 件中 9,504 が本文あり、6,621 が外部リンク付き
  → **X と同様、urlnorm/item_links で Karakeep と自動 dedup できる**。
- **FB いいね／リアクション**（`likes_and_reactions*.json`、140 ファイル）は
  **オーナー指定の FB スコープ外**（FB は投稿＋自作コメントのみ）。parser は
  読み込まない。
- **文字化け復号が必須**（新規実装要件）: DYI のテキストは「UTF-8 バイト列を
  latin-1 として再解釈」した mojibake。parser で `s.encode('latin-1').decode('utf-8')`
  による復元が必要（実データ 2,998/3,000 タイトルで復元を確認）。
- **メディア**（`media/**/*.jpg` 等のバイナリ）は取り込まない。投稿本文と
  コメント本文（テキスト知識）のみが対象で、写真は参照もしない（表面積最小）。

## Required changes if accepted

1. 本 ADR を `Accepted` に。
2. DESIGN.md §8 に「ソーシャルは自作＋明示的キュレーションのみ。フィード・
   ソーシャルグラフ・エンゲージメント指標・DM は非目標」を追加。
3. DESIGN.md §5.1（parsers）に `x_archive` / `facebook_dyi` を追記、
   `items.kind` に `social_post` を追加する migration（v13、追加のみ）。
4. `app/parsers/x_archive.py` / `facebook_dyi.py` 実装（自作 → social_post、
   いいね/ブックマーク → bookmark + social meta）。`facebook_dyi.py` は
   mojibake 復号（latin-1→utf-8）と本文なしレコード skip を必須とし、
   FB コメントは本文＋timestamp＋宛先文脈（復号 title＝誰の投稿へのコメント
   だったか）を保持する。
5. MCP フェンス・エスケープの適用確認、実 export での取得可否検証（open q.1）。
6. backlog D から本項目を除去し、到達点へ記録。

## Validation plan

批准後、実装は次で完了とみなす:

- 実 X アーカイブ 1 件・実 FB DYI 1 件を取り込み、自作は `social_post`、
  いいね/ブックマークは `bookmark(social meta)` として索引される。
- フィード・DM・他人のコメントが 1 件も入っていないことをテストで強制。
- 同一エクスポートの再取り込みで行数不変（差分インポート冪等）。
- 同一 URL の Karakeep bookmark と X bookmark が `item_links` でリンクする。
- MCP でソーシャル本文がフェンス済みで返る。
- 実データはリポジトリ・テストに入れない（テストは合成アーカイブのみ）。
