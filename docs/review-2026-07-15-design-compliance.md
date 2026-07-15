# Cairn 現行機能の設計適合レビュー

- レビュー日: 2026-07-15
- レビュアー: Codex
- 対象: `main` / `8d2a9c5`
- 正典: `docs/DESIGN.md`、健康領域は `docs/health/` と ADR-0005、ソーシャル領域は ADR-0006
- 作業種別: read-only レビュー（コード・文書の修正は未実施）
- **対応状況（2026-07-15 追記）**: R1〜R6 の全指摘を検証（すべて正確）のうえ PR #18 で反映済み。
  R2・R4 の設計判断はオーナー決定（R2=DESIGN を欠損補完へ改訂 / R4=body は Markdown 許可 +
  信頼境界明記）。backend 698 passed。詳細は各 `R#:` コミット参照。

## 1. Claude Code への引き継ぎ

この文書は、Cairn の現行機能が設計文書どおりかを確認したレビュー結果である。
修正に着手する場合は、最初に `AGENTS.md`、`docs/DESIGN.md`（特に §2、§8）、
`NOTES.md` を読み、対象を DESIGN.md §1 の S1〜S5 に結び付けること。

以下の指摘は修正許可そのものではない。修正前に、AGENTS.md の規定どおり
現状理解・実装対象・変更予定ファイル・schema/API 変更・リスク・テスト計画を提示し、
オーナーの承認を得ること。修正は 1 コミット 1 目的とし、Health の privacy 境界、
Obsidian allowlist、migration、本番 DB には特に注意すること。

推奨する対応順:

1. Health AI 草案のモデル設定と Markdown/provenance 境界
2. `cairn index rebuild` の契約整理と回帰テスト
3. 検索 UI の filter 網羅
4. Health CLI エラーの秘匿
5. 正典文書の追従

## 2. 総評

**条件付きで設計適合。blocker はないが、should 5 件と文書上の nit 1 件がある。**

主要なアーキテクチャは設計どおりである。Karakeep / Zotero / Obsidian connector は
read-only、Obsidian 書き込みは allowlist 4 カテゴリ、通常 MCP は read-only 4 ツール、
Health MCP は独立・既定無効である。schema v13 の本番 DB は SQLite integrity check が
`ok` で、orphan chunk は 0 件だった。

一方、テストがすべて通ることと設計適合は同義ではない。特に `index rebuild` は、
既存 chunk が壊れていても成功扱いのまま残すことを再現した。Health AI Drafts も、
設計が要求するモデル既定値・Markdown 不信頼境界・provenance 形式を厳密には満たさない。

## 3. 指摘

### R1 [should] 検索 UI の kind/source filter が現行データモデルを網羅しない

#### 設計根拠

- `docs/DESIGN.md:392-396`: M2 は `/api/search` と検索 UI の kind/source filter を要求する。
- ADR-0006 により `social_post`、source `x` / `facebook` が追加済み。
- M3 により source `obsidian`、kind `note` が追加済み。

#### 実装根拠

- `frontend/src/App.tsx:96-104` の `SOURCES` に `obsidian` / `x` / `facebook` がない。
- `frontend/src/App.tsx:106-113` の `KINDS` に `note` がない。
- filter UI は同ファイル `:457-496` で、この静的配列だけを描画する。

#### 影響

API と DB は対象 source/kind を検索できるが、UI からはそれらを直接 filter できない。
2026-07-15 の本番 DB には Obsidian note 3 件、Facebook social_post 46,299 件があり、
「データがまだ存在しないので非表示」というコードコメントも現状と一致しない。

#### 完了条件

- source filter に `obsidian` / `x` / `facebook` が表示される。
- kind filter に `note` が表示される。
- 各 filter の選択で `/api/search` へ正しい parameter が渡るテストを追加する。
- `npm run build` が成功する。

### R2 [should] `cairn index rebuild` が派生データを全再構築しない

#### 設計根拠

- `docs/DESIGN.md:333-338`: `cairn index rebuild` は「派生データ全再構築。原本から常に
  復元可能であることの担保」と定義されている。
- chunks / embeddings / FTS / vector index / item_links は再構築可能な派生データである。

#### 実装根拠

- `backend/app/cli.py:228-266` は `rechunk_messages(force=False)` と
  `rechunk_items(force=False)` を呼び、既存 chunk を skip する。
- embedding も `only_missing=True` のため、既存の誤った vector を再計算しない。
- `backend/tests/test_cli.py:193-204` は空 DB で成功することだけを確認しており、
  原本からの復元を検証していない。

#### 再現結果

一時 DB に会話を登録し、生成済み chunk の text を `CORRUPTED` に更新して
`cairn index rebuild` を実行した。

```text
exit_code: 0
chunks_messages: {messages: 0, chunks: 0, skipped: 1}
chunk_after: CORRUPTED
```

コマンドは成功したが、原本 message から chunk は復元されなかった。

#### 判断が必要な点

安全な修正方向は二つあるため、実装前に DESIGN.md との整合を決めること。

1. 設計どおり真の全再構築にする。既存 derived data を再生成し、失敗は非ゼロ終了にする。
2. 現行の「欠損補完」を正式仕様にする。この場合は先に DESIGN.md を改訂し、真の全再構築を
   別コマンドまたは明示 option として設計する。

既存実装を正として無断で DESIGN.md の意味を弱めてはならない。

#### 完了条件

- 選択した契約が DESIGN.md と CLI help/docstring で一致する。
- chunk text の破損、欠損 embedding、FTS desync、vector index orphan を fixture DB で作り、
  コマンド後に原本から復元される回帰テストがある。
- provider/model が必要な再構築に失敗した場合の exit code が仕様化され、テストされる。

### R3 [should] Health AI 解釈の既定モデルが D10 と異なる

#### 設計根拠

- `docs/DESIGN.md:119-122`（D10）: 草案・合成は qwen2.5:14b が既定で、32b は option。
- `docs/DESIGN.md:460-464`: ollama model 名は設定で一元管理する。

#### 実装根拠

- `backend/app/health/interpret.py:329-331` は `OllamaProvider()` を引数なしで生成する。
- `backend/app/llm/ollama.py:18-24` の既定値は
  `qwen2.5:32b-instruct-q4_K_M` である。
- Health 経路には `CAIRN_OLLAMA_MODEL` の参照がなく、CLI から14Bを選ぶ設定経路がない。

#### 影響

Health の AI 草案だけが週次レビュー / context pack と異なる既定モデルを使う。
32B が未導入の環境では、設計上の既定構成なのに Health 草案だけ失敗し得る。

#### 完了条件

- Health AI 草案の既定値が D10 の14Bになる。
- 32B を明示設定で選べる。
- 週次レビュー、context pack、Health の model 解決規則が一元化または同一契約になる。
- 既定値と環境変数 override のテストを追加する。

### R4 [should] Health AI Drafts の Markdown/provenance が共通不変条件に厳密には適合しない

#### 設計根拠

- `docs/DESIGN.md:348-359`: 外部由来 text は Markdown 出力時に escape し、LLM 生成物には
  `generated_by: cairn/<model>/<prompt_version>` を付ける。
- `docs/health/PRIVACY.md:120-131`: 原文と生成物を構造分離し、model / prompt version を記録する。
- AGENTS.md 不変条件4は全 LLM 生成物に同じ provenance 契約を要求する。

#### 実装根拠

- `backend/app/health/cli.py:417-428` は LLM 生成の title / body_markdown / limitations を
  そのまま Markdown へ連結する。
- 出力ラベルは `generated_by: {author_label} (prompt vN, ...)` であり、規定された
  `cairn/<model>/<prompt_version>` 形式ではない。
- `backend/tests/health/test_cli.py:101` は
  `assert "generated_by" not in ""` という placeholder で、ラベルを検証していない。
- 同テストは author label の部分文字列だけを確認し、prompt version の形式や hostile
  Markdown を検証していない。

#### 影響

health source の自由記述や prompt injection が LLM 出力へ反映された場合、AI Drafts 内で
意図しない Markdown link/embed/HTML 構造を生成できる。受け手も共通形式だけでは生成元と
prompt version を機械判定できない。

#### 完了条件

- AI Drafts に規定形式の provenance label が1つ必ず存在する。
- title と limitations を Markdown-safe にする。
- body を Markdown として許可するなら、その信頼境界を DESIGN/PRIVACY に明記し、危険な
  HTML/embed/link 等の扱いをテストする。全面 escape するなら週次レビューと同じ方針に揃える。
- hostile な title/body/limitations を使う回帰テストを追加する。
- `draft` category の new-only と path validation は維持する。

### R5 [should] Health event CLI が不正入力をエラーへ露出する

#### 設計根拠

- `docs/health/PRIVACY.md:76-96`: CLI/UI error は redact し、値・医療 free text・timestamp・
  absolute path を出さず、import ID でローカル調査できるようにする。

#### 実装根拠

- `backend/app/health/importers/events_yaml.py:72-85` は parse できない date の原文字列を
  `EventsError` に含める。
- `backend/app/health/cli.py:201-217` は「EventsError は entry id しか含まない」と仮定し、
  例外全文を stderr へ出す。この仮定は実装と一致しない。

#### 再現結果

```text
input: 2031-04-01 / medication-context
output: EventsError: unparseable date ... '2031-04-01 / medication-context'
```

import run の DB 記録と logger は redacted されているが、CLI 表示だけが漏れる。

#### 完了条件

- CLI には error code / exception type / import run ID のみを出す。
- 原入力は stderr、通常 log、`error_detail_redacted` のいずれにも出ない。
- malformed date、event label、notes、dose、source path を含む合成 fixture で漏えいテストを行う。

### R6 [nit] 正典文書の版数・allowlist・完了状態に追従漏れがある

#### 実態

- `docs/DESIGN.md:4` の header は v1.1 だが、同文書 `:468` 以降に v1.2 / v1.3 の改訂記録がある。
- `docs/DESIGN.md:197` は「現行 v10」のままだが、現行 schema は v13。
- `docs/DESIGN.md:310-317` は Obsidian allowlist を3箇所とするが、D13 / ADR-0005 /
  AGENTS.md / 実装は `90 Auto/Health` を含む4箇所である。
- `docs/health/ACCEPTANCE.md:141` の AI Drafts 配信は未チェックだが、H6 で実装・テスト済み。
- `ROADMAP.md:3` も正典を v1.1 と記載している。

#### 影響

DESIGN.md が正典であるため、将来のエージェントが正しい実装を「設計違反」と判断したり、
逆に古い schema/allowlist 前提で変更したりする可能性がある。

#### 完了条件

- DESIGN.md header、schema version、allowlist、改訂記録の表記が一致する。
- ROADMAP.md と Health ACCEPTANCE の状態が実装・テスト実態に追従する。
- D13 の決定内容は変えず、文書内の参照だけを整合させる。

## 4. 適合を確認した事項

- connector は Karakeep / Zotero へ GET のみを行い、Obsidian connector は読み取りのみ。
- API key は Keychain service `brain-sync-karakeep` / `brain-sync-zotero` を使用する。
- Obsidian writer は `auto` / `weekly` / `draft` / `health` の4カテゴリだけを許可する。
- traversal、symlink target、symlinked ancestor、new-only 上書き拒否のテストがある。
- Obsidian connector は `90 Auto`、`40 Reviews`、`90 Auto/Health` を索引しない。
- weekly review は各 section 最大10件、14日以内を related から除外し、既存週を上書きしない。
- weekly AI draft は失敗時に縮退し、provenance label と Markdown escape がある。
- 通常 MCP は `search_all` / `get_item` / `build_context_pack` /
  `get_recent_activity` の read-only 4 ツールで、raw content と synthesized を分離する。
- Health store は `cairn.db` と分離され、Health MCP は別 process・既定無効・read-only。
- social parser は公式 export の自作 + 明示的 curate 対象だけを構造的に読む。
- extraction pipeline は残存するが凍結され、現行 recall / MCP / items pipeline は依存しない。

## 5. 実行した検証

### Backend

```text
cd backend && .venv/bin/python -m pytest tests/ -q
692 passed, 1 warning in 22.60s
```

warning は Starlette TestClient の既知 deprecation warning 1件。

### Frontend

```text
cd frontend && npm run build
TypeScript build + Vite production build: success
```

### 本番 DB の read-only 外形確認

`backend/data/cairn.db` を SQLite URI `mode=ro` + `PRAGMA query_only=ON` で確認した。
application 経由の `db.connect()` は migration を起こし得るため使用していない。

```text
PRAGMA user_version: 13
PRAGMA integrity_check: ok
orphan chunks: 0
item_links: 1,890
indexed generated notes under 90 Auto / 40 Reviews: 0
```

item 内訳:

```text
bookmark / karakeep: 124
conversation / chatgpt: 1,227
conversation / claude: 509
conversation / claude_cli: 164
conversation / codex_cli: 30
conversation / gemini: 6
note / obsidian: 3
reference / zotero: 102
social_post / facebook: 46,299
```

同一 transaction snapshot での embedding 状態:

```text
chunks: 79,591
embeddings: 78,788
missing embeddings: 803
  message_text / claude_cli: 706
  message_text / codex_cli: 97
```

最新 CLI 会話の約1.0%が keyword/FTS には載るが semantic/hybrid 用 embedding を持たない。
これは既知の M6③運用 gap であり、S4/S5 の最終評価時に明示して扱う必要がある。

## 6. S1〜S5 の現状

| 成功基準 | 現状 |
|---|---|
| S1: 週次レビュー10分以内 | section cap は実装済み。実読了時間の評価は M6③ |
| S2: 毎週1件以上の再発見 | related / 理由行は実装済み。主観評価は M6③ |
| S3: MCPの日常利用 | 登録・機能は実装済み。日常化の評価はリポジトリだけでは判定不能 |
| S4: ゼロメンテ運用 | 通知・縮退は実装済み。手動 export と803件の embedding backlogが残る |
| S5: 4系統横断回答 | 4系統の実データと context pack は存在。検索精度と日常利用は M6③評価中 |

したがって、S1〜S5を測る機構は存在するが、M6③の実運用評価が終わるまでは
「成功基準をすべて達成済み」とは主張しないこと。

## 7. スコープ外として修正しなかった事項

- backend の Starlette/httpx deprecation warning
- attachment blob を含まない既存 backup
- `import_runs.failed` 集計精度
- cleanup 対象（temp、premigrate backup、旧 Vault、旧 brain-sync 実体）
- M6③の ranking/related tuning
- X archive 未入手による未取り込み

これらは既存 backlog または運用課題であり、本レビューは修正許可を与えない。
