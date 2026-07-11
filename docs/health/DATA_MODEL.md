# Personal Health Observatory — Data Model

## 1. ストア

健康領域は`health.duckdb`を正規化・分析ストアとして使用する。
rawファイルはDB外へ保存し、DBにはパス、ハッシュ、サイズ、取得日時、状態を記録する。

## 2. 基本型

### 2.1 source_files

原本の監査台帳。

| column | type | meaning |
|---|---|---|
| id | UUID | 内部ID |
| source_kind | TEXT | apple_health / labs_csv / document / events |
| original_name | TEXT | 受領時ファイル名 |
| stored_path | TEXT | health data homeからの相対パス |
| sha256 | TEXT UNIQUE | 原本ハッシュ |
| size_bytes | BIGINT | サイズ |
| acquired_at | TIMESTAMPTZ | 取得日時 |
| source_created_at | TIMESTAMPTZ NULL | 原本内の作成日時 |
| parser_name | TEXT | importer名 |
| parser_version | TEXT | parser版 |
| status | TEXT | imported / partial / quarantined / failed |
| meta_json | JSON | 非機密の追加情報 |

### 2.2 import_runs

| column | type | meaning |
|---|---|---|
| id | UUID | import実行ID |
| source_file_id | UUID | 原本 |
| started_at | TIMESTAMPTZ | 開始 |
| completed_at | TIMESTAMPTZ NULL | 完了 |
| inserted | BIGINT | 追加数 |
| updated | BIGINT | 更新数 |
| skipped | BIGINT | 重複数 |
| quarantined | BIGINT | 保留数 |
| status | TEXT | running / ok / partial / failed |
| error_code | TEXT NULL | 機械可読エラー |
| error_detail_redacted | TEXT NULL | 個人値を含めない説明 |

### 2.3 metric_catalog

| column | type | meaning |
|---|---|---|
| metric_id | TEXT PK | canonical ID |
| label_ja | TEXT | 日本語名 |
| label_en | TEXT NULL | 英語名 |
| quantity_kind | TEXT | concentration / count / duration等 |
| canonical_unit | TEXT NULL | 正規単位 |
| loinc_code | TEXT NULL | 既知の場合 |
| healthkit_identifier | TEXT NULL | 既知の場合 |
| catalog_version | TEXT | 定義版 |
| active | BOOLEAN | 現行か |

### 2.4 metric_aliases

| column | type | meaning |
|---|---|---|
| source_namespace | TEXT | lab_sheet / apple_health等 |
| source_name | TEXT | 原表記 |
| metric_id | TEXT | canonical ID |
| mapping_version | TEXT | mapping版 |
| confidence | TEXT | confirmed / provisional |
| note | TEXT NULL | 判断根拠 |

### 2.5 observations

1測定値1行。血液検査、血圧、体重、心拍、睡眠集計等を扱う。

| column | type | meaning |
|---|---|---|
| id | UUID | 観測ID |
| subject_id | TEXT | 個人ID。単一利用でも固定値を使う |
| metric_id | TEXT NULL | canonical metric。不明ならNULL |
| original_metric | TEXT | 原項目名 |
| value_num | DOUBLE NULL | 数値 |
| value_text | TEXT NULL | 定性値 |
| unit | TEXT NULL | 正規単位 |
| original_value | TEXT | 原値 |
| original_unit | TEXT NULL | 原単位 |
| observed_start | TIMESTAMPTZ NULL | 観測開始 |
| observed_end | TIMESTAMPTZ NULL | 観測終了 |
| observed_date | DATE NULL | 日付精度しかない場合 |
| time_precision | TEXT | instant / interval / date / unknown |
| specimen | TEXT NULL | blood / urine等 |
| fasting_state | TEXT NULL | fasting / nonfasting / unknown |
| reference_low | DOUBLE NULL | 当日の基準下限 |
| reference_high | DOUBLE NULL | 当日の基準上限 |
| reference_text | TEXT NULL | `<79`等の原表現 |
| flag_source | TEXT NULL | H/L等、原本側のフラグ |
| source_name | TEXT | 病院、デバイス、アプリ等 |
| device_name | TEXT NULL | 測定機器 |
| source_file_id | UUID | 原本 |
| source_row_ref | TEXT NULL | セル、XML位置等 |
| fingerprint | TEXT UNIQUE | 重複排除 |
| mapping_version | TEXT NULL | 正規化版 |
| quality_status | TEXT | valid / provisional / quarantined |
| meta_json | JSON | 追加metadata |

制約:

- `value_num`と`value_text`の少なくとも一方を持つ。
- `observed_start/end`または`observed_date`を持つ。
- 原値は必須。正規化失敗時も事実を失わない。

### 2.6 events

| column | type | meaning |
|---|---|---|
| id | UUID | イベントID |
| kind | TEXT | medication_start / dose_change / supplement_start / smoking_stop等 |
| label | TEXT | 表示名 |
| start_at | TIMESTAMPTZ/DATE | 開始 |
| end_at | TIMESTAMPTZ/DATE NULL | 終了 |
| time_precision | TEXT | instant / date / month / approximate |
| status | TEXT | active / completed / uncertain |
| dose_value | DOUBLE NULL | 用量 |
| dose_unit | TEXT NULL | mg/day等 |
| route | TEXT NULL | oral等 |
| frequency | TEXT NULL | daily / every_other_day等 |
| source_type | TEXT | self_report / clinician / document |
| source_file_id | UUID NULL | 原本がある場合 |
| confidence | TEXT | confirmed / estimated / uncertain |
| notes | TEXT NULL | 事実記述。分析は書かない |
| meta_json | JSON | 追加情報 |

### 2.7 documents

| column | type | meaning |
|---|---|---|
| id | UUID | 文書ID |
| document_kind | TEXT | lab_report / imaging / endoscopy / prescription等 |
| title | TEXT | 表示名 |
| document_date | DATE NULL | 文書日 |
| source_file_id | UUID | 原本 |
| issuer | TEXT NULL | 発行機関 |
| extracted_text_path | TEXT NULL | 検証済みテキストへの参照 |
| extraction_status | TEXT | none / draft / verified |
| meta_json | JSON | 追加情報 |

### 2.8 interpretations

| column | type | meaning |
|---|---|---|
| id | UUID | 解釈ID |
| title | TEXT | 要約名 |
| body_markdown | TEXT | 解釈本文 |
| author_type | TEXT | self / clinician / ai |
| author_label | TEXT | 氏名またはモデルID |
| created_at | TIMESTAMPTZ | 作成 |
| model_id | TEXT NULL | AIの場合 |
| prompt_version | TEXT NULL | AIの場合 |
| data_snapshot_id | UUID | 使用データの固定スナップショット |
| status | TEXT | draft / accepted / superseded / rejected |
| supersedes_id | UUID NULL | 置換対象 |
| confidence | TEXT NULL | low / medium / high |
| limitations | TEXT NULL | 不足・留保 |
| provenance_json | JSON | 実行条件 |

### 2.9 interpretation_evidence

| column | type | meaning |
|---|---|---|
| interpretation_id | UUID | 解釈 |
| evidence_kind | TEXT | observation / event / document / reference |
| evidence_id | TEXT | 対象ID |
| role | TEXT | supports / context / limitation |
| quoted_value | TEXT NULL | 表示用の短い値 |
| PRIMARY KEY | composite | 重複防止 |

`supports/contradicts`の知識グラフを自動生成するための表ではない。
1件の解釈が実際に参照した根拠を監査するための表である。

### 2.10 data_snapshots

| column | type | meaning |
|---|---|---|
| id | UUID | snapshot ID |
| created_at | TIMESTAMPTZ | 作成 |
| query_spec_json | JSON | 対象期間・指標・フィルタ |
| result_hash | TEXT | 結果集合hash |
| row_count | BIGINT | 行数 |
| max_observed_at | TIMESTAMPTZ NULL | 最新観測 |
| catalog_version | TEXT | 項目定義版 |

## 3. 派生ビュー

- `v_latest_observation_by_metric`
- `v_daily_metric_summary`
- `v_weekly_activity_summary`
- `v_lab_panels`
- `v_active_events`
- `v_event_windows`
- `v_data_quality`
- `v_interpretation_evidence_complete`

## 4. 血液検査の横持ち変換

以下は実在の検査履歴と一致しない、完全な合成例である。

入力:

| 項目 | 単位 | 2031-02-03 | 2031-08-19 |
|---|---|---:|---:|
| Synthetic-A | arb-U/L | 11 | 23 |
| Synthetic-B | arb-mg/dL | 1.23 | 1.19 |

出力:

| metric | observed_date | value | unit |
|---|---|---:|---|
| synthetic_a | 2031-02-03 | 11 | arb-U/L |
| synthetic_a | 2031-08-19 | 23 | arb-U/L |
| synthetic_b | 2031-02-03 | 1.23 | arb-mg/dL |
| synthetic_b | 2031-08-19 | 1.19 | arb-mg/dL |

## 5. バージョニング

- schema version
- parser version
- metric catalog version
- mapping version
- report template version
- prompt version

を独立管理する。モデル変更だけで過去の観測値を再取り込みしない。
