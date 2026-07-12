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

### 2.6 events（H2 実装済み、schema v2）

不確実な日付は「原文 + earliest/latest の区間」で表現し、タイムスタンプを
捏造しない（`2031-03` → earliest=3/1, latest=3/31, precision=month。
`~` 接頭辞 = approximate。start 欠損 = precision=unknown, status=uncertain）。
行は append-only: 訂正は `supersedes_id` を付けた新しい行で行い、
「現在のイベント」= 他の行から supersede されていない行。

| column | type | meaning |
|---|---|---|
| id | TEXT PK | 記入者が付ける安定ID（YAML の `id`） |
| kind | TEXT | medication_start / medication_stop / dose_change / supplement_start / supplement_stop / smoking_stop / alcohol_change / exercise_change / illness / procedure / travel / context_change |
| label | TEXT NULL | 表示名 |
| start_raw / end_raw | TEXT NULL | 原文のまま（`2031-03`・`~2031-05-01` 等） |
| start_earliest / start_latest | DATE NULL | 開始の区間下限/上限 |
| end_earliest / end_latest | DATE NULL | 終了の区間下限/上限 |
| time_precision | TEXT | date / month / approximate / unknown |
| status | TEXT | active / completed / uncertain |
| dose_value | DOUBLE NULL | 用量 |
| dose_unit | TEXT NULL | mg/day等 |
| route | TEXT NULL | oral等 |
| frequency | TEXT NULL | daily / every_other_day等 |
| source_type | TEXT | self_report / clinician / document |
| source_file_id | UUID NULL | 取り込み元 ledger ファイル |
| confidence | TEXT | confirmed / estimated / uncertain |
| notes | TEXT NULL | 自由記述。**レポートへ出力せず、事実として解釈しない** |
| supersedes_id | TEXT NULL | append-only 訂正チェーン |
| entry_hash | TEXT | 冪等判定用の内容ハッシュ（同一 id + 内容変更は拒否） |
| imported_at | TIMESTAMPTZ | 取り込み日時 |
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

### 2.11 quarantine_records（H1 実装済み）

不明な項目名など、推測で正規化しないデータの保留台帳（DESIGN.md health §6）。
値そのものは保護ストア内にのみ保存され、ログには件数だけが出る。

| column | type | meaning |
|---|---|---|
| id | UUID | 保留ID |
| source_file_id | UUID | 原本 |
| import_run_id | UUID | 発生した import 実行 |
| reason_code | TEXT | unknown_metric / parse_error |
| original_metric | TEXT NULL | 原項目名 |
| original_unit | TEXT NULL | 原単位 |
| source_row_ref | TEXT NULL | セル位置 |
| payload_json | JSON | 原セル値（保護ストア内のみ） |
| created_at | TIMESTAMPTZ | 記録日時 |
| status | TEXT | pending / resolved |

同一原本の同一セルは再取り込みで重複保留しない（冪等）。
別ハッシュの原本（月次 export の更新版等）では、新しい原本への provenance
付きで改めて保留される。解決はエイリアスカタログの拡張 + mapping_version 更新で行う。

### 2.12 Apple Health の格納（H3、observations を共用）

高頻度データも `observations` に入れる（H-D1 により cairn.db からは独立）。
Apple Health 固有の扱い:

- マッピングは `metric_catalog.healthkit_identifier`（allowlist 8型）。
  それ以外の型は取り込まず件数のみ計上。`Workout`/`WorkoutRoute` は完全除外
- `original_metric` = HealthKit 型識別子、`source_name` = sourceName、
  `device_name` = device 属性、`observed_start`/`observed_end` にタイムゾーン付き
  時刻、`observed_date` = 開始日。`time_precision` = instant（start==end）/ interval
- 睡眠（カテゴリ型）は区間長を分に換算して `value_num`、カテゴリ値を
  `original_value`（例: HKCategoryValueSleepAnalysisAsleepCore）
- `fingerprint` = 型/source/開始/終了/原値/単位。Apple の複数ソース重複を吸収し冪等

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

H1 の入力契約（`importers/labs_csv.py` docstring が実装正）:

- 先頭列は `項目`（または `metric`/`item`）、任意で `単位`・`基準値` 列、
  残りの列はすべて日付（`YYYY-MM-DD` / `YYYY/MM/DD`）。
- **基準範囲の変更**は、同じ項目を新しい `基準値` の別行として追加し、
  適用される日付列にだけ値を書くことで表現する。各観測は自分の行の
  基準範囲を保持するため、範囲は検査日単位で残る（グローバル上書きなし）。
- 空セルは観測なし（何も捏造しない）。数値でないセル（`<5` 等）は
  value_text として保持。未知の単位は原値のみ保持し正規化値を持たない
  （quality_status=provisional）。

## 5. バージョニング

- schema version
- parser version
- metric catalog version
- mapping version
- report template version
- prompt version

を独立管理する。モデル変更だけで過去の観測値を再取り込みしない。
