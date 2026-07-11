# Personal Health Observatory — Design

- ステータス: Proposal
- 作成日: 2026-07-11
- 対象: Cairnへの健康領域追加
- 正典との関係: `docs/DESIGN.md`を変更する前の設計提案。採用にはADR-0005の承認が必要。

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

単位換算や項目名マッピングは誤り得るため、次を必ず残す。

- original_metric
- original_value
- original_unit
- canonical_metric
- normalized_value
- normalized_unit
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

### H-D7: Obsidian書き込みは既存allowlistを越えない

MVPでは次だけを使う。

- `90 Auto/Health/` — current status、timeline、data quality、lab trends
- `00 Inbox/AI Drafts/` — 人間が採否を決める分析草案

新しい`40 Reviews/Health`を必要とする場合は、別Decisionとallowlist変更を先に行う。

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

H4以降で、生成済みMarkdownをObsidian connector経由で通常のnoteとして索引する。
健康ストアを`items`へ1サンプルずつ登録しない。

H5のMCP候補:

- `health_get_current_status`
- `health_get_timeline`
- `health_query_observations`
- `health_compare_periods`
- `health_get_event_response`
- `health_build_visit_brief`

## 5. データフロー

### 5.1 Apple Health

1. iPhone Healthアプリからexport.zipを生成
2. 指定inboxへ配置
3. ZIPを展開せずstreamingで`export.xml`を読む
4. 対象型だけを取り込む
5. deterministic fingerprintで重複排除
6. 日次・週次の派生集計を更新
7. 元ZIPをrawへ移し、hashとimport結果を保存

将来の自動同期は別フェーズとし、MVPでiOSアプリを作らない。

### 5.2 血液検査

H1では既存スプレッドシートから手動exportしたCSVを正式入力とする。
シートAPIによるread-only同期はH2で追加する。

横持ち形式（日付が列）を、1観測1行の縦持ち形式へ変換する。
基準範囲は検査日ごとに保持し、グローバルな基準値で上書きしない。

### 5.3 Event ledger

服薬・サプリ・生活変化は明示的イベントとして記録する。

```yaml
- id: event-example-001
  kind: medication_start
  occurred_at: 2026-01-01
  label: Example medication
  dose:
    value: 10
    unit: mg/day
  source: self_report
  confidence: confirmed
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
cairn health init
cairn health import labs-csv FILE
cairn health import apple-export FILE
cairn health import events FILE
cairn health import document FILE
cairn health status
cairn health report current
cairn health report visit-brief --since 2025-01-01
cairn health doctor
```

H1では`init`、`import labs-csv`、`status`だけを実装する。
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
