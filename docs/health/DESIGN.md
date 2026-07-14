# Personal Health Observatory — Design

- ステータス: Accepted（ADR-0005、2026-07-11 批准）
- 作成日: 2026-07-11
- 対象: Cairnへの健康領域追加
- 正典との関係: 健康ドメインの設計正典。root `docs/DESIGN.md` には D13 として登録済み。
  矛盾したら root DESIGN.md と ADR-0005 が優先。

## 1. 成功基準

| ID | 成功基準 | 測定方法 |
|---|---|---|
| H-S1 | 同一指標の長期推移を原本へ遡って確認できる | 観測値からsource_fileと原本ハッシュを取得 |
| H-S2 | 薬・サプリ・生活イベントを検査値と同一時間軸で比較できる | 期間比較クエリとグラフ |
| H-S3 | 再取り込みで重複しない | 同一原本を2回取り込み、行数不変 |
| H-S4 | AI要約の根拠を機械的に列挙できる | interpretation_evidenceの完全性 |
| H-S5 | 健康データが公開リポジトリ・通常ログへ漏れない | git/ログ漏えいテスト |
| H-S6 | Cairn本体が停止しても健康原本と分析ストアが壊れない | 独立バックアップと復元テスト |
| H-S7 | 日常運用が過度な手作業にならない | 月1回以下の手動importで更新可能 |

## 2. アーキテクチャ

```text
                    ┌─────────────────────────┐
                    │ Source systems          │
                    │                         │
                    │ Google Sheets / CSV     │
                    │ Apple Health export.zip │
                    │ Home measurements       │
                    │ Medical documents       │
                    │ Event YAML / UI          │
                    └────────────┬────────────┘
                                 │ read-only ingest
                                 ▼
┌──────────────────────────────────────────────────────────────┐
│ Health data home                                              │
│ ~/Library/Application Support/Cairn/health/                    │
│                                                               │
│ raw/        immutable source snapshots                        │
│ store/      health.duckdb                                     │
│ derived/    Parquet/aggregates/figures                         │
│ reports/    generated Markdown and visit briefs               │
│ quarantine/ rejected or ambiguous records                     │
└──────────────────────────────┬───────────────────────────────┘
                               │ bounded summaries/references
               ┌───────────────┴────────────────┐
               ▼                                ▼
┌─────────────────────────┐       ┌──────────────────────────────┐
│ Cairn                    │       │ Obsidian                     │
│ existing cairn.db        │       │ 90 Auto/Health              │
│ report metadata/index    │       │ 00 Inbox/AI Drafts          │
│ MCP context orchestration│       │ human-reviewed understanding│
└────────────┬────────────┘       └──────────────────────────────┘
             │
             ▼
       AI session / MCP
```

## 3. 設計判断

### H-D1: 健康時系列は別の分析ストアに置く

採用: `health.duckdb`をCairn本体の`cairn.db`と分離する。

理由:

- Apple Healthは高頻度サンプルを含み、全文検索中心のSQLite DBへ混在させる合理性がない。
- 期間集計、窓関数、Parquet入出力は分析系ストアに適する。
- 健康データのアクセス制御、バックアップ、破棄を独立させられる。
- Cairnの`items.kind` CHECK制約を変更せずに済み、既存原本テーブルを破壊しない。

Cairn側には生成レポートやソース記述を既存のnote経路で索引する。
観測サンプルそのものはCairnのchunksへ入れない。

### H-D2: raw / normalized / derived / interpretationを分離する

- raw: 受領した原本。immutable。
- normalized: 単位・時刻・項目コードを統一した観測値。
- derived: 日次集計、移動平均、期間差、グラフ。
- interpretation: 人間・医師・AIによる説明。

上流が変化しても、rawからnormalizedとderivedを再構築できるようにする。

### H-D3: 原値と正規化値を両方保持する

単位換算や項目名マッピングは誤り得るため、次を必ず残す
（列名は`DATA_MODEL.md` observations表に一致させる）。

- original_metric（原項目名）
- original_value（原値）
- original_unit（原単位）
- metric_id（canonical metric。不明ならNULL）
- value_num / value_text（正規化値）
- unit（正規単位）
- mapping_version

### H-D4: FHIR風の意味論を採用するが、FHIR完全実装はしない

Observation、Medication、DocumentReference、Provenanceに近い概念を採用する。
LOINC、UCUM、SNOMED等のコードは分かる場合に付けるが、コードがないデータを拒否しない。
将来のFHIR exportを可能にする設計余地を残す。

### H-D5: 解釈はappend-onlyかつsupersede可能にする

AIや本人の解釈を上書きしない。新しい分析が古い分析を置換する場合、
`supersedes_id`で関係を記録する。採用状態は次のいずれか。

- draft
- accepted
- superseded
- rejected

### H-D6: MCP公開はオプトインかつbounded

健康ツールは既定で無効とする。明示設定時のみ公開し、返却件数・期間・項目数を制限する。
原本ファイルや全履歴を無条件でモデルへ送信しない。

### H-D7: Obsidian書き込み可能領域を拡大しない（ただしallowlistエントリ追加は必要）

MVPでは次だけを使う。

- `90 Auto/Health/` — current status、timeline、data quality、lab trends
- `00 Inbox/AI Drafts/` — 人間が採否を決める分析草案

`90 Auto/Health/`は既存の`90 Auto`ツリー内であり書き込み可能領域は広がらないが、
現行`obsidian_writer.py`はファイル名にパス区切りを許さないため、**実装時は第4カテゴリ
`"health": ("90 Auto/Health", overwrite=True)`のallowlist追加が必要**。同じ変更で
AGENTS.md不変条件2（「allowlist 3箇所」）を改訂し、既存カテゴリと同じパス検証・
シンボリックリンク拒否テストを`health`カテゴリにも適用する（H5で実施。H0/H1では
Obsidianへ書かない）。

新しい`40 Reviews/Health`のような`90 Auto`ツリー外の書き込み先を必要とする場合は、
別Decisionを先に行う。

## 4. コンポーネント

### 4.1 Importers

```text
backend/app/health/importers/
├── labs_csv.py
├── apple_health.py
├── events_yaml.py
└── documents.py
```

各importerは以下の共通処理を持つ。

1. 原本ハッシュ計算
2. source_files登録
3. streaming parse
4. canonical mapping
5. validation
6. transaction単位のupsert
7. import_runs監査記録
8. 曖昧データのquarantine

### 4.2 Metric catalog

項目定義と単位変換をコードから分離する。

```text
backend/app/health/catalog/
├── metrics.yml
├── apple_health_mappings.yml
├── lab_aliases.yml
└── units.yml
```

項目例:

```yaml
creatinine:
  label_ja: クレアチニン
  quantity_kind: concentration
  canonical_unit: mg/dL
  loinc: 2160-0
  aliases:
    - Cr
    - CRE
    - クレアチニン
```

実装時はコード値を検証し、誤ったコードを無理に付けない。

### 4.3 Analytics

- period summary
- baseline vs follow-up
- medication/event overlays
- missingness and data-quality report
- outlier candidate detection
- trend estimation
- rolling statistics

「異常判定」は検査機関の基準範囲を尊重し、独自の診断閾値を初期実装しない。

### 4.4 Reports

初期レポート:

- `current-status.md`
- `health-timeline.md`
- `lab-trends.md`
- `medication-response.md`
- `data-quality.md`
- `next-visit-brief.md`

レポートは生成時刻、対象期間、データスナップショットID、生成器、
参照観測IDを含む。

### 4.5 Cairn integration

`90 Auto/Health` の生成物は自己還流防止のため索引しない。人間が確認して通常の
索引対象ディレクトリ（`10 Themes` 等）へ昇格した summary だけを既存 Obsidian
connector が通常 note として索引する。健康ストアを`items`へ1サンプルずつ登録しない。

H7 MCP は通常 Cairn MCP から独立し、既定無効・read-only とする。実装ツール:

- `health_current_status(metrics, include_events=false)`
- `health_query_observations(metrics, since?, until?, max_rows?)`
- `health_compare_event(event_id, metrics, window_days?)`
- `health_data_quality(metrics)`
- `health_interpretation_history(statuses?)`
- `health_get_interpretation(interpretation_id)`
- `health_build_context_pack(metrics, ..., include_events=false, include_interpretations=false)`

metric は最大8、観測行は最大300、既定期間366日・最大3650日。free text は fence
と文字数上限を適用し、context pack は selection/projection/event の hash、source ID/
category、含めた interpretation の data snapshot ID を返す。

## 5. データフロー

### 5.1 Apple Health（H3 実装済み。実装正は `importers/apple_health.py`）

1. iPhone Healthアプリからexport.zipを生成
2. `cairn health import apple-export FILE`（.zip / .xml いずれも可）
3. ZIPを展開せずstreamingで`export.xml`を読む（`iterparse` + root.clear で
   数百MBでもメモリ一定）。allowlist 8型（歩数/安静時心拍/HRV/体重/睡眠/
   収縮期・拡張期血圧/運動時間、healthkit_identifier でマッピング）のみ取り込み、
   他の型は件数のみ計上して破棄。`Workout`/`WorkoutRoute`（位置情報）は完全にスキップ
4. deterministic fingerprint（型/source/開始/終了/原値/単位）で重複排除。再取り込み冪等
5. instant/interval を区別し、睡眠は区間長（分）を派生。sourceName/device/タイムゾーンを保持
6. 日次・週次集計は正規化行から再生成（`analytics.daily_summary`/`weekly_summary`）
7. 元ファイルをrawへ不変スナップショット、hashとimport結果を保存

**性能**: 高頻度データのため DuckDB のパラメータ化 INSERT（~700行/s）では実用に耐えず、
保護 home 内の一時 CSV 経由で `COPY FROM CSV`（native bulk）を使う（PRIVACY §3 が
保護 home 内の一時ファイルを許可）。パース律速で実測 ~5,700行/s、メモリは一定。

将来の自動同期は別フェーズとし、MVPでiOSアプリを作らない。

### 5.2 血液検査

H1では既存スプレッドシートから手動exportしたCSVを正式入力とする。
シートAPIによるread-only同期は後続フェーズ（H8の運用改善以降）で検討する。
月1回以下の手動exportでH-S7は満たせるため、MVPの依存を増やさない。

横持ち形式（日付が列）を、1観測1行の縦持ち形式へ変換する。
基準範囲は検査日ごとに保持し、グローバルな基準値で上書きしない。

### 5.3 Event ledger

服薬・サプリ・生活変化は明示的イベントとして記録する（H2 実装済み。
実装正は `importers/events_yaml.py` docstring、格納形は DATA_MODEL §2.6）。

```yaml
- id: event-example-001
  kind: medication_start
  start: 2026-01-01          # '2026-01'（月精度）や '~2026-01-01'（approximate）も可
  end: null                  # 任意
  label: Example medication
  dose:
    value: 10
    unit: mg/day
  source: self_report
  confidence: confirmed
  # 訂正は編集ではなく、新しいエントリ + supersedes: <旧id> で行う（append-only）
```

公開リポジトリには合成例だけを置き、実イベントファイルはhealth data homeへ置く。

## 6. エラー処理

- 不明な項目名: quarantineし、推測で既存項目へ結合しない
- 不明な単位: 原値だけ保存し、normalized_valueをNULLにする
- 時刻欠損: date precisionを保持し、正午等を捏造しない
- 同一指標の複数ソース: sourceとdeviceを保持し、勝手に平均しない
- 基準範囲の不整合: 検査機関・検査日単位で保存
- Apple Healthの重複: source、device、期間、値、metadataのfingerprintで排除
- 中断したimport: transaction rollbackし、rawは残す
- AI生成失敗: 観測データ処理を失敗扱いにしない

## 7. CLI案

```bash
cairn health init                                   # H0
cairn health doctor                                 # H0（provenance_intact 含む）
cairn health import labs-csv FILE                    # H1
cairn health import events FILE                      # H2
cairn health import apple-export FILE                # H3（.zip/.xml）
cairn health import document FILE --kind lab_report  # H4
cairn health document attach-text ID TEXTFILE [--verified]  # H4
cairn health document list                           # H4
cairn health status
cairn health report labs                             # H1
cairn health report event-response ID [--days N]     # H2
cairn health report data-quality                     # H3
cairn health report broken-refs                      # H4
cairn health report current           # 後続（H5）
cairn health report visit-brief --since 2025-01-01   # 後続（H5）
```

H0では`init`と`doctor`を、H1では`import labs-csv`、`status`、`report labs`
（factual lab summary。ACCEPTANCE H1の決定的Markdownレポート）を実装する。
残りのコマンドは各後続フェーズで追加する。
既存CLIへ統合する前に、healthモジュール単体でテスト可能にする。

## 8. 非目標

`README.md`の非目標に加え、MVPでは以下を行わない。

- リアルタイムHealthKit同期
- Apple Watchからの直接取得
- 医療画像の自動診断
- OCR結果の無検証自動確定
- 薬物相互作用判定
- 疾病リスクスコアの無断計算
- 公開クラウドへの自動アップロード
- 健康データを使ったランキング学習
