> **廃止済み（2026-07-02, M0 で退避）**
>
> 本文書は Phase 1〜6 のフェーズ制ロードマップ（旧版）で、現行の開発指示ではない。
> Phase 1〜2 は完了、Phase 3 は実装済み（schema v10）だが凍結（DESIGN.md D2 注記）、Phase 3〜6 の**計画**は廃止された（DESIGN.md D1/D2/D5/D7）。
> 現在の正典は `docs/DESIGN.md` v1.1 とそのマイルストーン §7 M0〜M6。ADR-0004 が本移行を記録する。
> 本ファイルは歴史文書としてのみ保持する。編集しない。

---

# Cairn Development Roadmap

## 0. この文書の目的

この文書は、Codex CLI または Claude Code が Cairn を継続的に開発するための共通指示書である。

Cairn は、ChatGPT / Claude / Gemini のエクスポート、および Claude CLI / Codex CLI のローカルログを統合し、過去の AI 対話を検索・再利用できるローカルファーストの外部記憶基盤である。

単なる会話アーカイブにとどまらず、最終的には以下を実現する。

- 対話原本を安全かつ再現可能に保存する
- キーワード検索と意味検索を組み合わせる
- 会話を論点・主張・仮説・決定・未解決課題へ構造化する
- 会話間の関連、発展、矛盾、見解の更新を扱う
- 忘れていた有望な着想や未完了事項を能動的に再浮上させる
- Zotero、Obsidian、Web 記事アーカイブ等と連邦型に連携する

## 1. 作業開始時の必須手順

各セッションの冒頭で、必ず次を行うこと。

1. `pwd` と `git status --short --branch` を確認する。
2. `README.md`、`AGENTS.md`、`CLAUDE.md`、`NOTES.md`、`SECURITY.md` を読む。
3. 現在のスキーマ、テスト、API、MCP ツール、パーサー構成を調査する。
4. ユーザーの指示と本ロードマップのどの項目を実施するか明示する。
5. 変更前に関連テストを実行し、既存の基準状態を把握する。
6. 大きな変更では、実装前に短い設計案と変更対象ファイルを提示する。

既存コードを読まずに全面的な書き換えを行ってはならない。

## 2. 基本原則

### 2.1 ローカルファースト

- 会話本文、派生メタデータ、Embedding は、デフォルトではローカルに保存する。
- 外部 API への本文送信は明示的な opt-in とする。
- Web UI は原則 `127.0.0.1` に限定する。
- MCP は読み取り専用を標準とする。

### 2.2 原本と派生データの分離

- 取り込んだ会話原本を直接変更しない。
- 要約、タグ、セグメント、Embedding、関連付け等は派生テーブルに保存する。
- 派生データは削除・再生成可能にする。
- 派生データの生成モデル、プロンプト版、生成日時を記録する。

### 2.3 再構築可能性

- `cairn.db` を削除しても、原ログおよびエクスポートから再構築できる設計を維持する。
- スキーマ変更は明示的な migration とする。
- migration は冪等性、バックアップ、ロールバック可能性を考慮する。

### 2.4 後方互換性

- 既存のインポート、検索、Web UI、MCP ツールを壊さない。
- API や MCP の破壊的変更が必要な場合は、新旧を一定期間併存させる。

### 2.5 セキュリティ

- シークレット除去、localhost 制限、DB 権限、ZIP bomb 対策を維持・強化する。
- 元ログに残るシークレットのリスクを明記する。
- 外部コンテンツを LLM に処理させる場合、プロンプトインジェクションを信頼境界として扱う。
- 自動処理に削除、送信、公開、外部書き込み権限を与えない。

## 3. 現行アーキテクチャの前提

- Backend: Python 3.11+ / FastAPI
- Frontend: React + TypeScript + Vite
- Storage: SQLite
- Full-text search: FTS5 trigram + 2 文字以下等の LIKE fallback
- Parsers: `backend/app/parsers/`
- DB/search/import: `backend/app/db.py`
- CLI sync: `backend/app/cli_sync.py`
- API entry point: `backend/app/main.py`
- MCP server: `backend/app/mcp_server.py`
- Frontend: `frontend/`

構成変更は可能だが、変更理由、移行方法、テストを伴うこと。

## 4. フェーズ別ロードマップ

---

# Phase 1 — 原本保全と基盤の堅牢化

## 4.1 目的

Cairn を長期利用できるアーカイブとして安定させる。新機能追加より先に、取り込み、識別、差分同期、添付、バックアップ、エクスポート、migration を堅牢化する。

## 4.2 実装項目

### P1-1 安定した識別子

- source 固有 ID がある場合は保存する。
- source 固有 ID がない場合、内容・時刻・source 等から安定した ID を生成する。
- conversation、message、attachment に安定 ID を持たせる。
- 同じデータの再取り込みで重複しないこと。
- 編集済み会話は差分更新できること。

### P1-2 インポート履歴

以下を記録する。

- import source
- input filename またはログパス
- started_at / completed_at
- parser version
- imported / updated / skipped / failed counts
- warning / error summary
- content hash

Web UI または管理 CLI から履歴を確認できること。

### P1-3 添付ファイルと画像

- 添付メタデータを conversation/message と関連付ける。
- 原ファイルの所在、MIME type、size、hash を保持する。
- 添付が存在しない場合も会話本文の取り込みを失敗させない。
- 将来の OCR、画像理解、PDF 抽出に備え、派生テキストを別管理できる構造にする。

### P1-4 migration

- スキーマ version table を導入する。
- migration runner を実装する。
- migration 前に DB backup を作成できるようにする。
- migration の単体・統合テストを作る。

### P1-5 エクスポート

少なくとも以下を実装する。

- JSONL: conversation/message/metadata を機械可読で出力
- Markdown: 人間が読めるスレッド単位の出力
- source / date range / conversation ID で絞り込み
- 原本と派生データを区別して出力

### P1-6 バックアップと整合性検査

管理 CLI に以下を追加する。

- `backup`
- `integrity-check`
- `export-jsonl`
- `export-markdown`

整合性検査では少なくとも以下を確認する。

- SQLite `PRAGMA integrity_check`
- orphan message / attachment
- FTS 件数整合
- stable ID の重複
- source record と conversation の参照整合

## 4.3 Phase 1 受入基準

- 同じ入力を複数回取り込んでも重複しない。
- 既存データを migration 後も検索・表示できる。
- DB をバックアップし、別 DB として復元できる。
- JSONL / Markdown にエクスポートし、会話本文・日時・source を確認できる。
- 破損行または未知フィールドを含む入力でも、可能な範囲を取り込み warning を残す。
- backend test が全件通る。
- UI 変更があれば frontend build が通る。

---

# Phase 2 — ハイブリッド検索

## 5.1 目的

正確な語句・固有名詞を探す全文検索と、表現が異なる関連対話を探す意味検索を併用する。

## 5.2 実装原則

- FTS5 を置き換えず維持する。
- Embedding provider は抽象化する。
- デフォルトはローカルモデルを優先する。
- 外部 Embedding API は opt-in とし、送信範囲を明示する。
- Embedding は再生成可能な派生データとする。
- model name、dimension、chunking version、created_at を保存する。

## 5.3 実装項目

### P2-1 chunking

- conversation 全体ではなく message または論理 chunk 単位で Embedding する。
- conversation ID、message range、character offsets を保持する。
- 長文の境界で意味が切れすぎないようにする。
- chunking algorithm を version 管理する。

### P2-2 ベクトル保存

初期実装では、運用の単純さを優先する。

候補を比較し ADR に記録すること。

- SQLite extension
- sqlite-vec
- Python 側での小規模 cosine similarity
- 別ローカル vector store

個人アーカイブ規模での性能、配布容易性、macOS 対応、バックアップ容易性を評価する。

### P2-3 ハイブリッドランキング

- keyword score
- semantic score
- recency はデフォルトでは弱い補助要素
- source filter / date range filter
- Reciprocal Rank Fusion 等の説明可能な統合方法

検索結果には、なぜヒットしたかを示す。

- matched keywords
- semantic match
- source
- date
- hit message range

### P2-4 検索 UI

- Keyword / Semantic / Hybrid の切替
- source / date / model filter
- 該当 message へのジャンプ
- 検索結果スニペット
- 関連度と一致理由の表示

### P2-5 MCP 拡張

既存 MCP 互換性を維持しつつ、以下を追加または拡張する。

- `search_conversations` に `mode=keyword|semantic|hybrid`
- message range または chunk ID を返す
- 検索理由を返す
- 件数上限と出力サイズ制限を維持する

## 5.4 Phase 2 受入基準

- 固有名詞は keyword 検索で正確に見つかる。
- 表現が異なる同一テーマは semantic/hybrid で見つかる。
- 検索結果から該当 message へ移動できる。
- Embedding を全削除して再生成できる。
- provider 未設定でも従来検索が完全に動作する。
- 外部 API を使わないローカル構成が成立する。

---

# Phase 3 — 知識抽出

## 6.1 目的

会話を「何について話したか」だけでなく、「何を主張し、何を決め、何が未解決か」という構造へ変換する。

## 6.2 データモデル

原文とは別に、少なくとも次の派生 entity を検討する。

### Segment

- segment_id
- conversation_id
- start_message_id
- end_message_id
- title
- summary
- topics
- generated_by
- prompt_version
- created_at

### Assertion

- assertion_id
- segment_id
- text
- actor: `user | assistant | shared`
- kind: `claim | hypothesis | conclusion | decision | rejected_idea | question | todo`
- status: `tentative | accepted | rejected | superseded | unresolved | completed`
- confidence
- supporting_message_ids

### Entity / Reference

- person
- organization
- project
- paper
- URL
- repository
- product
- place

### Artifact

- generated file
- proposed document
- code patch
- email draft
- research plan
- external URL

## 6.3 抽出ポリシー

- ユーザーの主張と AI の提案を必ず区別する。
- AI の発言をユーザーの確定見解として保存しない。
- 推測と確定事項を区別する。
- 根拠 message ID を必須とする。
- 原文に存在しない主張を生成しない。
- 抽出結果を UI で確認・修正・無効化できるようにする。
- 手動修正した項目を自動再生成で上書きしない。

## 6.4 実装段階

1. ルールベースで URL、repository、日付等を抽出する。
2. 会話要約と topic 候補を生成する。
3. segment 分割を生成する。
4. assertion / question / todo を抽出する。
5. 人間によるレビュー UI を追加する。
6. バッチ再生成と prompt version 管理を追加する。

## 6.5 Phase 3 受入基準

- 1 会話を複数 segment に分割できる。
- 各 assertion から原文 message に戻れる。
- user / assistant / shared を区別できる。
- rejected / unresolved / superseded を表現できる。
- 派生データを消して再生成できる。
- 手動修正が自動処理で失われない。

---

# Phase 4 — 会話間リンクと時間的知識

## 7.1 目的

個別会話を孤立したログとして扱わず、発展、関連、矛盾、更新の関係を扱う。

## 7.2 関係タイプ

- `related_to`
- `continues`
- `derived_from`
- `supports`
- `contradicts`
- `updates`
- `supersedes`
- `implements`

すべての自動生成リンクに score、生成根拠、生成器 version を保存する。

## 7.3 機能

- 各会話に関連会話を表示する。
- 同じテーマで結論が異なる会話を提示する。
- 古い見解から新しい見解への更新経路を示す。
- 関連候補は自動生成しても、自動確定しない。
- ユーザーがリンクを承認、却下、固定できるようにする。

## 7.4 Phase 4 受入基準

- 関連会話候補と根拠を表示できる。
- 矛盾候補を原文付きで比較できる。
- `supersedes` による見解の更新を追跡できる。
- 手動確定リンクは再計算で消えない。

---

# Phase 5 — 能動的再浮上

## 8.1 目的

ユーザーが覚えているものを検索するだけでなく、忘れていた有望な着想や未完了事項を適切な頻度で再提示する。

## 8.2 再浮上タイプ

- 過去の同日・同時期の会話
- 最近のテーマと関係する過去の会話
- 未解決 question
- 未完了 todo
- 繰り返し現れる問題意識
- 最近の見解と矛盾する過去の見解
- 長期間放置された有望な仮説
- 複数テーマを橋渡しする会話

## 8.3 再浮上スコア

候補要素:

- relevance to recent activity
- novelty
- unresolved importance
- age / dormancy
- user feedback
- recurrence across conversations
- explicit importance marker

単純な最新順やランダム提示にしない。

## 8.4 フィードバック

ユーザーは各提示に対して以下を記録できる。

- useful
- not useful
- already resolved
- remind later
- pin
- mute topic

フィードバックはランキング改善に利用するが、原本には影響させない。

## 8.5 出力

初期実装はアプリ内の週次レビュー画面とする。

将来候補:

- Markdown digest
- local notification
- email は明示 opt-in
- MCP tool: `get_resurfacing_digest`

## 8.6 Phase 5 受入基準

- 週次 digest をローカルで生成できる。
- 各項目から元会話・元 message へ戻れる。
- 未解決事項と関連会話を区別できる。
- ユーザーフィードバックを保存できる。
- mute / resolved を再提示しない。

---

# Phase 6 — 連邦型外部記憶

## 9.1 目的

Cairn を万能データベースにせず、他の専門的な知識庫と連携する。

想定対象:

- Zotero: 論文、書誌、PDF、一次資料
- Obsidian: 整理済みノート、自分の現在の理解
- Reader / Web archive: 記事、SNS、動画、ニュースレター
- Git repositories: 実装と issue

## 9.2 原則

- 物理的統合より、識別子とリンクを優先する。
- 原本の authoritative source を明示する。
- Cairn に複製する場合も provenance を保持する。
- 連携先が利用不能でも Cairn の基本機能は動作する。
- 各 connector は独立した adapter として実装する。

## 9.3 最初の連携候補

1. Obsidian への Markdown export
2. Zotero item URI / citekey の保存
3. MCP 経由での federated query
4. URL 正規化による Cairn 会話と外部資料の関連付け

## 9.4 Phase 6 受入基準

- Cairn の conversation/segment から Zotero item または Obsidian note へリンクできる。
- 外部 source と Cairn source の出所を混同しない。
- connector 無効時にも既存機能が壊れない。

## 10. 優先順位

原則として以下の順で進める。

1. Phase 1: 原本保全、migration、export、backup
2. Phase 2: ハイブリッド検索
3. Phase 3: segment と assertion の最小実装
4. Phase 4: 関連会話と時間的更新
5. Phase 5: 週次再浮上
6. Phase 6: 外部知識庫連携

ただし各 Phase は巨大な一括変更にせず、垂直スライスで実装する。

例:

- schema → repository function → API → UI → test → documentation

## 11. 開発プロセス

### 11.1 1 タスクの大きさ

1 回の作業では、明確な受入基準を持つ 1 機能または 1 改善に限定する。

避けること:

- 複数 Phase の同時全面実装
- 無関係なリファクタリング
- テストのないスキーマ変更
- UI と backend の契約を暗黙に変更すること

### 11.2 作業前の提案

実装前に次を示す。

- 現状理解
- 実装対象
- 変更予定ファイル
- schema/API 変更
- リスク
- テスト計画

### 11.3 作業後の報告

- 実装内容
- 変更ファイル
- 実行したテストと結果
- 未解決事項
- migration / backup 手順
- 次の推奨タスク

### 11.4 コミット

- 1 コミット 1 目的を基本とする。
- commit message は変更理由が分かるものにする。
- 自動 commit / push はユーザーが明示的に求めた場合だけ行う。
- 既存の未コミット変更を勝手に破棄、stash、上書きしない。

## 12. テスト方針

最低限:

- parser fixture tests
- DB migration tests
- import idempotency tests
- search regression tests
- API tests
- MCP tool tests
- security regression tests

実データを fixture にする場合:

- 個人情報とシークレットを除去する。
- 必要最小限に縮小する。
- 元の私的ログを repository に commit しない。

基本コマンド:

```bash
cd backend
.venv/bin/python -m pytest tests/ -q

cd ../frontend
npm run build
```

追加推奨:

```bash
cd backend
.venv/bin/python -m ruff check .
.venv/bin/python -m mypy app

cd ../frontend
npm run lint
```

存在しない lint/typecheck を勝手に前提にせず、導入する場合は設定と理由を説明する。

## 13. パフォーマンス方針

- 個人アーカイブとして、数万〜数十万 conversation/message を想定する。
- import は差分処理を維持する。
- 大量 migration は transaction、進捗、キャンセル、再開を考慮する。
- 検索 API は limit を必須とし、巨大本文を無制限に返さない。
- N+1 query を避ける。
- index 追加は query plan を確認する。

## 14. UI 方針

- 機能を増やしても「検索して原文へ戻る」導線を最優先する。
- AI の要約や抽出結果を原文と視覚的に区別する。
- 自動生成結果には生成日時、生成器、根拠 message を表示できるようにする。
- 破壊的操作には確認を求める。
- 派生データの再生成と原本削除を混同させない。

## 15. MCP 方針

- 読み取り専用を標準とする。
- 出力件数と文字数に上限を設ける。
- tool description に検索範囲と制約を明記する。
- agent が AI の過去発言をユーザーの見解と誤認しないよう、speaker と assertion actor を返す。
- 新 tool は既存 tool と責務が重複しないようにする。

将来候補:

- `search_segments`
- `search_assertions`
- `list_open_questions`
- `list_pending_todos`
- `get_related_conversations`
- `get_resurfacing_digest`
- `build_context_pack`

## 16. 最初に着手する推奨タスク

最初の実装タスクは Phase 1 の設計調査とする。

### Task 1: 現状監査と Phase 1 設計

コードを変更する前に、以下を調査して `docs/architecture-audit.md` に記録する。

- 現在の SQLite schema
- conversation/message の ID 生成方法
- import の差分判定方法
- parser ごとの source ID 利用状況
- 添付ファイルの扱い
- DB 再構築経路
- backup/export の現状
- migration の有無
- test coverage の主要な空白

そのうえで、Phase 1 を以下の小タスクに分解する。

1. schema version と migration runner
2. import run history
3. stable IDs と idempotency regression tests
4. integrity-check
5. backup
6. JSONL export
7. Markdown export
8. attachment metadata

Task 1 では、監査文書と issue 相当の実装計画だけを作り、全面実装を開始しない。

## 17. Codex / Claude Code に最初に渡す指示

以下をそのまま使用できる。

> このリポジトリの `ROADMAP.md`、`AGENTS.md`、`CLAUDE.md`、`NOTES.md`、`SECURITY.md`、`README.md` を読んでください。まず `ROADMAP.md` の「Task 1: 現状監査と Phase 1 設計」だけを実施してください。既存コードとテストを調査し、`docs/architecture-audit.md` を作成してください。この段階では大規模な実装やスキーマ変更を行わないでください。監査結果、具体的な実装分割、各タスクの受入基準、リスク、推奨順序を記載し、関連テストを実行して現在の基準状態も記録してください。既存の未コミット変更は変更・破棄しないでください。

## 18. 完了の定義

各タスクは、次を満たしたときに完了とする。

- 受入基準を満たす。
- 既存機能の回帰がない。
- テストが追加・更新され、通過する。
- README / NOTES / SECURITY / API docs の必要箇所が更新される。
- migration と復旧手順が必要なら文書化される。
- 自動生成データと原本の境界が維持される。
- セキュリティおよびプライバシー上の影響が説明される。
