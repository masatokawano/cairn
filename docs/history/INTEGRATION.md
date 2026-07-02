# Cairn × brain-sync 統合手順書

- 作成日: 2026-07-02
- 対象: `masatokawano/cairn` (main) / `masatokawano/brain-sync` (main)
- 実施者: Claude Code / Codex CLI（原則 1 タスク = 1 セッション）
- 関連文書: `docs/adr/0003-brainsync-integration.md`（統合の意思決定記録）
- 配置先: cairn リポジトリのルートに置く。統合完了後は `docs/history/` へ移動してよい。

---

## 0. 統合方針の要約

brain-sync を cairn リポジトリへ `brainsync/` ディレクトリとして **git subtree で履歴ごと吸収**し、monorepo 化する。理由と代替案の検討は ADR-0003 に記録する。

責務分界は以下で固定する。以後のすべての実装はこの分界に従う。

| 領域 | Cairn（`backend/` + `frontend/`） | brainsync（`brainsync/`） |
|---|---|---|
| データ | 会話原本・派生データ（chunk / embedding / segment / assertion） | state ファイルのみ（`brainsync/state/`、gitignore） |
| 読み取り | 自身の SQLite | Cairn HTTP API / Karakeep API / Zotero API / Obsidian Vault |
| 書き込み | 自身の SQLite のみ | Obsidian の `90 Auto` と `40 Reviews/Weekly` のみ |
| 提供 | HTTP API（127.0.0.1）+ read-only MCP | CLI（launchd から実行）+ 将来の brain-mcp |
| 再浮上 | 候補算出・スコア・フィードバック保存（API） | 横断合成・digest 描画・Obsidian 出力・スケジュール |
| 連邦化 | URL/DOI 正規化・リンクテーブル・MCP 公開 | 各外部システムへの connector 実装 |

**不変条件（AGENTS.md にも記載する）:**

1. `backend/` は `brainsync/` を import しない。逆方向の依存のみ許す。
2. brainsync は `cairn.db` を直接開かない。Cairn へのアクセスは HTTP API のみ。
3. brainsync の書き込み先は Obsidian の `90 Auto` / `40 Reviews/Weekly` に限定。`10 Themes` / `20 Projects` / `50 Decisions` には書かない。
4. 外部由来テキスト（会話タイトル、ブックマークタイトル、タグ等）は信頼しない。markdown 出力時にエスケープし、シェル評価しない。

---

## 1. 統合後の構成

```
cairn/
├── backend/                  # 既存のまま（アーカイブ核）
├── frontend/                 # 既存のまま
├── brainsync/                # 旧 brain-sync（統合層）
│   ├── pyproject.toml        # 依存は標準ライブラリのみを維持
│   ├── brainsync/
│   │   ├── __init__.py
│   │   ├── config.py         # dotenv パーサ（bash source 廃止）
│   │   ├── secrets.py        # macOS Keychain ラッパー
│   │   ├── markdown.py       # エスケープ・レンダリング共通部
│   │   ├── state.py          # state/*.json の読み書き（schema version 付き）
│   │   ├── connectors/
│   │   │   ├── cairn_api.py
│   │   │   ├── karakeep.py
│   │   │   ├── zotero.py
│   │   │   └── obsidian.py
│   │   ├── render/           # state → markdown
│   │   ├── review/weekly.py  # state → 週次レビュー合成
│   │   └── cli.py            # python -m brainsync <subcommand>
│   ├── state/                # gitignore（JSON 中間状態）
│   └── tests/
│       └── fixtures/         # 各 API レスポンスのサンプル JSON + golden markdown
├── launchd/                  # plist テンプレート（cairn 本体 + brainsync 各ジョブ）
│   └── install-launchd.sh
├── docs/
│   ├── brainsync-design.md   # 旧 external-brain-brain-sync-design.md（改稿）
│   ├── adr/0003-brainsync-integration.md
│   └── history/              # CODEX_PROMPT_FOR_CLAUDE_CODE.md 等の歴史的文書
├── AGENTS.md / CLAUDE.md     # 統合版（同一内容）
├── NOTES.md                  # brainsync の癖を統合
├── ROADMAP.md                # Phase 5 / 6 を責務分界に沿って改訂
└── SECURITY.md               # brainsync 章を追加
```

---

## 2. タスク一覧

| # | タスク | 規模 | 依存 | 挙動変更 |
|---|---|---|---|---|
| T0 | 準備と凍結 | XS | — | なし |
| T1 | git subtree によるリポジトリ統合 | S | T0 | なし |
| T2 | brainsync の Python パッケージ化と共通基盤 | M | T1 | なし（出力互換） |
| T3 | state 層の導入と週次レビュー再実装 | M | T2 | 内部のみ |
| T4 | Cairn API 契約強化と connector 移行 | M | T2 | API 追加のみ |
| T5 | launchd テンプレート化と稼働移行 | S | T2〜T4 | 運用切替 |
| T6 | ドキュメント統合とロードマップ改訂 | M | T1〜T5 | なし |

T2 以降は 1 タスクをさらに小さく割ってよいが、順序は守る。T3 と T4 は独立なので並行可（Claude Code と Codex に分担させる場合の切れ目として適切）。

---

## 3. 各タスク

### T0 — 準備と凍結

**作業:**

1. 両リポジトリで `git status` がクリーンであることを確認。
2. 両リポジトリの main に統合前タグを打つ:
   ```bash
   cd ~/workspace/cairn      && git tag pre-brainsync-merge && git push origin pre-brainsync-merge
   cd ~/workspace/brain-sync && git tag final-standalone    && git push origin final-standalone
   ```
3. Cairn のベースライン取得: `cd backend && .venv/bin/python -m pytest tests/ -q` の結果（passed 件数）を記録。
4. brain-sync のベースライン取得: `check_karakeep.sh` / `check_cairn.sh` / `check_zotero.sh` / `check_obsidian.sh` を手動実行し、成否を記録。
5. Obsidian の `90 Auto/` 配下 4 ファイルの現物をどこかへコピーしておく（T2 の出力互換確認に使う）。

**受入基準:** タグが両リポジトリに存在し、ベースライン記録が残っている。

### T1 — git subtree によるリポジトリ統合

**作業:**

```bash
cd ~/workspace/cairn
git remote add brainsync-origin https://github.com/masatokawano/brain-sync.git
git fetch brainsync-origin
git subtree add --prefix=brainsync brainsync-origin main
```

`git subtree` が使えない環境では fallback:

```bash
git merge --allow-unrelated-histories brainsync-origin/main
mkdir brainsync && git mv <brain-sync由来の各ファイル> brainsync/ && git commit
```

続けて同一セッション内で:

1. `brainsync/external-brain-brain-sync-design.md` を `docs/brainsync-design.md` へ `git mv`。
2. `CODEX_PROMPT_FOR_CLAUDE_CODE.md` を `docs/history/` へ `git mv`。
3. ルート `.gitignore` に `brainsync/config.env` と `brainsync/state/` を追加。

**注意:** この時点では旧 `~/workspace/brain-sync` の launchd 稼働をそのまま維持する（停止は T5）。統合先のスクリプトはまだ実行しない。

**受入基準:**

- `git log --follow brainsync/sync_cairn_recent.py` で旧履歴が辿れる。
- `cd backend && .venv/bin/python -m pytest tests/ -q` が T0 のベースラインと同数 pass。
- 旧リポジトリ側の launchd ジョブが引き続き正常動作している（翌時間の同期ログで確認）。

### T2 — brainsync の Python パッケージ化と共通基盤

**目的:** bash/Python 混在と 3 重コピペを解消し、テスト可能な形にする。**出力 markdown は現行と同等**を保つ（構造互換。生成日時等の可変部を除き diff が説明可能であること）。

**作業:**

1. §1 のパッケージ構成を作り、`pyproject.toml` を追加（依存: 標準ライブラリのみ。dev 依存に pytest）。`backend/.venv` へ `pip install -e brainsync/` する。
2. `config.py`: bash `source` + `env -0` 方式を廃止し、`KEY="value"` 形式のみを受け付ける素朴な dotenv パーサに置換。未知の行は警告、シェル構文は不許可。環境全体を混ぜない（ファイル内のキーのみ返す）。
3. `secrets.py`: `security find-generic-password` ラッパーを 1 箇所に集約。
4. `markdown.py`: `escape_inline()` を実装。改行除去に加え、行頭 `#`、`[`/`]`、バッククォート、`|` を無害化する。**全 connector のタイトル出力に適用**（設計書 §12.3 の実装化）。
5. `connectors/karakeep.py`: bash + jq 実装を Python へ移植。その際 **ページネーション対応**（`nextCursor` 追従、上限は設定値）を入れ、100 件超で to-review を取りこぼすバグを潰す。あわせて「タグなし新規項目の件数」を集計して返す。
6. `connectors/zotero.py`: `?since=<library version>` を使った増分取得へ変更（version は T3 の state に保存。T3 前は暫定で現行ロジック維持でも可）。
7. `cli.py`: `python -m brainsync sync-cairn | sync-karakeep | sync-zotero | sync-obsidian | weekly | check <target>` を提供。旧 `run_*.sh` / `check_*.sh` は CLI を呼ぶ薄いラッパーに置換するか削除。絶対パス `/Users/masato/...` のハードコードを排除（スクリプト自身の位置から解決）。
8. `tests/`: config パーサ、markdown エスケープ、各 connector のレスポンス解釈（`tests/fixtures/*.json` を用意）のユニットテストを追加。

**受入基準:**

- `cd backend && .venv/bin/python -m pytest tests/ ../brainsync/tests -q` が全件 pass。
- `python -m brainsync sync-cairn` 等を手動実行し、T0 で保存した現行出力と比較して構造互換（見出し・項目・フィールドが一致、エスケープ強化による差分のみ）。
- `grep -r "/Users/masato" brainsync/` がゼロ件。

### T3 — state 層の導入と週次レビュー再実装

**目的:** markdown を系統間の契約にしない。awk 抽出を廃止する。

**作業:**

1. `state.py`: 各 connector の取得結果を `brainsync/state/<source>.json` に保存（`schema_version`、`fetched_at`、カーソル値 — `karakeep_next_cursor` / `cairn_last_updated_at` / `zotero_last_version` — を含む）。設計書「優先3」をここで消化する。
2. `render/`: state JSON → `90 Auto/*.md` のレンダラ。出力は T2 と同一構造。
3. `review/weekly.py`: `create_weekly_review.sh` を置換。**awk による markdown 再解析をやめ、state JSON から直接合成**する。既存週ファイルの上書き防止、`BRAIN_SYNC_WEEK` による週指定、frontmatter は現行仕様を維持。
4. golden file テスト: fixture JSON → 期待 markdown（`tests/fixtures/golden/`）の比較テストを、90 Auto 各ファイルと週次レビューについて追加。生成日時はテスト時に固定注入できるようにする。

**受入基準:**

- `python -m brainsync weekly` が state から週次レビューを生成し、既存週ファイルを上書きしない。
- golden テストを含め全テスト pass。
- `create_weekly_review.sh` の awk が消えている。

### T4 — Cairn API 契約強化と connector 移行

**目的:** brainsync が必要とする契約を Cairn API に明示し、クライアント側フィルタの脆さを解消する。

**作業（backend 側）:**

1. `GET /api/conversations` に `updated_after=<ISO8601>` クエリパラメータを追加（既存パラメータと併用可、後方互換）。テスト追加。
2. `GET /api/stats` にソース別の最終取り込み日時（`last_import_at` per source、`import_runs` から導出）を追加。既にあれば流用。
3. （任意・小）`GET /api/conversations` に `min_messages=` を追加。クライアント側フィルタでも実害はないため優先度低。

**作業（brainsync 側）:**

4. `connectors/cairn_api.py` を `updated_after` + state カーソルベースの増分取得へ移行。
5. **陳腐化警告**: `stats` のソース別 `last_import_at` を用い、chatgpt / claude / gemini の最終取り込みが 30 日を超えていたら週次レビュー冒頭に警告行を出す（手動エクスポート忘れ対策。閾値は設定可能に）。
6. タイトルのハードコード除外リスト（`excluded_title_prefixes` 等）を `config.env` または専用設定ファイルへ外出しする。除外ロジックの assertion 駆動化（decision / todo / question を含む会話のみ選別）は Phase 3 成果の安定後とし、ROADMAP 改訂（T6）に将来項目として記載する。

**受入基準:**

- `updated_after` の backend テストが pass し、既存 API テストが全件 pass。
- brainsync の cairn 同期が増分取得で動作し、出力が従来と同等。
- 意図的に古い `last_import_at` を fixture に与えたテストで、週次レビューに警告行が出る。

### T5 — launchd テンプレート化と稼働移行

**作業:**

1. `launchd/` に plist テンプレートを追加: `com.masato.brain-sync.{karakeep,cairn,zotero,weekly-review}.plist.template`（および既存の `com.masato.cairn` / `com.masato.ollama` もテンプレート化して同居させる）。`__REPO_DIR__` / `__PYTHON__` / `__LOG_DIR__` を変数化。
2. `launchd/install-launchd.sh`: テンプレートへパスを埋め込み `~/Library/LaunchAgents/` へ配置、`launchctl unload` → `load` を行うスクリプト。各ジョブは `python -m brainsync <subcommand>` を直接叩く。
3. 移行手順: 旧 `~/workspace/brain-sync` 側の 4 ジョブを `launchctl unload`。新リポジトリ側で install スクリプトを実行。1 時間後にログ（`~/Library/Logs/brain-sync/`）で全ジョブの成功を確認。
4. SECURITY.md 用のメモを残す: TCC（フルディスクアクセス）の付与先は現状 `/bin/bash`。launchd ジョブが `python -m brainsync` 直叩きになるため、付与先を Python バイナリ（venv の実体）へ**狭める**か、Vault を TCC 保護外パスへ移す選択肢を明記。判断は T6 の SECURITY 統合で確定する。

**受入基準:**

- 全ジョブが新パスから 1 サイクル以上成功。
- 旧ディレクトリの launchd ジョブがすべて unload 済み。
- クリーンな別ユーザー/別マシンを仮定して、README の手順だけで launchd 構成を再現できる記述になっている。

### T6 — ドキュメント統合とロードマップ改訂

**作業:**

1. **ROADMAP.md 改訂**（最重要）:
   - Phase 5: 「候補算出・スコア・フィードバック保存 = Cairn（API `/api/resurfacing` 系として公開）」「digest 描画・Obsidian 出力・スケジュール = brainsync」に分割して書き直す。Cairn 自身は週次レビュー画面を持たない（デバッグ用最小 UI は可）。
   - Phase 6: connector 実装は brainsync に一本化。Cairn 側は URL/DOI 正規化・外部参照リンクテーブル・MCP 公開に限定。
   - 旧設計書の「優先3〜6」を該当 Phase へ吸収し、`docs/brainsync-design.md` からロードマップ性の記述を削る（設計書は思想・構成・運用の記述に純化。「現在の到達点」のような時点情報は `docs/backlog.md` へ）。
   - brain-mcp（`search_all` 等）は brainsync 側の将来項目として記載し、Cairn MCP の設計原則（read-only・データフェンシング・出力上限）を踏襲する旨を明記。
   - 将来項目: 週次レビュー選別の assertion 駆動化（T4-6 参照）。
2. **SECURITY.md**: brainsync 章を追加 — Keychain サービス名、config.env の非実行パース、markdown エスケープ、TCC 付与先の決定、Obsidian Vault が iCloud 同期される場合は会話タイトル・ID がクラウドへ出る旨の注意。
3. **NOTES.md**: brainsync 側の癖（Karakeep API のページネーション仕様、Zotero `since` の版管理、Takeout/export の罠は既存記述と統合）を追記。
4. **AGENTS.md / CLAUDE.md**: 統合版へ差し替え（本手順書と同時に配布されたドラフトを使用。両ファイルは同一内容を維持する）。
5. **旧リポジトリの後始末**: `masatokawano/brain-sync` の README を「cairn リポジトリの `brainsync/` へ統合済み（final-standalone タグ参照）」に差し替え、GitHub 上で Archive する。

**受入基準:**

- ROADMAP に Cairn / brainsync の両方の責務が現れ、Phase 5 / 6 の重複記述が消えている。
- 新規セッションの Claude Code / Codex が AGENTS.md → ROADMAP.md → NOTES.md の順で読んで、分界を破らずに次タスクへ着手できる（実際に 1 タスク流して検証する）。

---

## 4. ロールバック

- T1〜T4 の失敗: `git reset --hard pre-brainsync-merge`（push 済みなら revert）。旧 `~/workspace/brain-sync` は T5 まで生かしてあるため、運用は無停止。
- T5 の失敗: 新ジョブを unload し、旧ディレクトリのジョブを再 load。plist の旧ファイルは T5 完了まで削除しない。
- Obsidian 出力の破損: `90 Auto/` は機械上書き領域なので次サイクルで自己修復する。`40 Reviews/Weekly/` は上書き防止があるため、破損週ファイルは手動削除して再生成。

---

## 5. 統合後の開発運用（Claude Code / Codex 併用）

- 両エージェントは同一の AGENTS.md / CLAUDE.md / NOTES.md / ROADMAP.md を読む。エージェント別の指示ファイルを分岐させない。
- 役割の目安: 実装セッションと、別エージェントによるレビューセッション（セキュリティ・分界違反・テスト妥当性の検査）を交互に回す。従来 Codex がセキュリティレビューを担っていた運用（`docs/history/CODEX_PROMPT_FOR_CLAUDE_CODE.md` 参照)を、方向を固定せず双方向で継続する。
- 分界チェックをレビュー観点に常設する: (1) backend が brainsync を import していないか、(2) brainsync が cairn.db を直接開いていないか、(3) Obsidian 書き込み先が許可ディレクトリ内か、(4) 外部由来テキストが未エスケープで出力されていないか。
- コミット規約・1 タスク 1 セッション・作業前提案/作業後報告は既存 ROADMAP §11 をそのまま適用する。
