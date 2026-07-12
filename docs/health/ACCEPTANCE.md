# Personal Health Observatory — Acceptance Criteria

## 0. Test-data rule

Automated tests use synthetic data only. Real personal health data must never appear in fixtures, CI output, pull requests, issues, logs, or screenshots.

## H0 — Design and safety boundary

- [x] ADR-0005 has an explicit status.
- [x] The relationship among `docs/NORTH_STAR.md`, root `docs/DESIGN.md`, and the health design is documented.
- [x] The default health data home is outside the Git worktree.
- [x] Directories are mode 0700 and data files are mode 0600.
- [x] Worktree paths, traversal, and unsafe symlink targets are rejected.
- [x] Health MCP is absent or disabled by default.
- [x] No root `cairn.db` migration is required for H0/H1.
- [x] A repository audit detects likely private health artifacts.

H0 verified 2026-07-11: `backend/tests/health/` + live `cairn health init` /
`doctor` all-green on the production Mac.

## H1 — Laboratory CSV vertical slice

### Synthetic fixture

The fixture contains:

- at least three synthetic metrics;
- at least three synthetic dates;
- numeric and blank cells;
- a qualitative value such as `<5`;
- a reference-range change;
- an unknown metric;
- an unknown unit;
- no real metric history copied from the user.

### Acceptance

- [x] A horizontal date-column CSV is transformed to one observation per row.
- [x] Original metric, value, unit, and reference text are retained.
- [x] Canonical metric, normalized value, normalized unit, and mapping version are stored separately.
- [x] Date-only precision remains date-only.
- [x] Reference ranges are retained per observation date.
- [x] Re-importing an unchanged CSV does not increase observation count.
- [x] A changed cell affects only the corresponding record or version.
- [x] Unknown metric names are quarantined rather than guessed.
- [x] Unknown units preserve the original value without false normalization.
- [x] Every observation points to a source file and source-row reference.
- [x] A failed import rolls back normalized writes while preserving the raw source.
- [x] Logs contain counts and IDs, but no measurement values.
- [x] A deterministic Markdown summary can be generated from a fixed snapshot.

H1 verified 2026-07-11 with synthetic data (`backend/tests/health/`, 38
tests; suite total 553 passed). Local real-data verification (below) is a
separate, still-open human step.

## H2 — Intervention and event ledger

- [x] Medication, supplement, lifestyle, illness, travel, and procedure events validate against a schema.
- [x] Exact, date-only, month-only, and approximate times are represented without invented timestamps.
- [x] Dose, unit, frequency, source, and confidence are optional but structured.
- [x] Event histories are append-only and support correction through supersession.
- [x] Active event intervals can be overlaid on an observation timeline.
- [x] A missing or uncertain start date is visible as uncertainty.
- [x] Free-text event notes are not interpreted as medical facts automatically.

H2 verified 2026-07-11 with synthetic data (`tests/health/test_events.py`,
`test_event_response.py`, `test_migration.py`; suite total 572 passed).
Store schema v1→v2 migrates additively with an automatic premigrate backup.

## H3 — Apple Health export

### Synthetic fixture

The fixture contains synthetic records for:

- step count;
- resting heart rate;
- heart-rate variability;
- body mass;
- sleep analysis;
- systolic and diastolic blood pressure;
- exercise time;
- an allowlist-excluded type;
- a duplicate record;
- multiple sources, devices, and time zones.

### Acceptance

- [x] `export.xml` is parsed as a stream rather than loaded fully into memory.
- [x] Only allowlisted types enter normalized storage.
- [x] Ignored types are counted without logging values.
- [x] Deterministic fingerprints remove duplicates.
- [x] Source, device, time zone, start, and end times are preserved.
- [x] Instant and interval records remain distinct.
- [x] Re-importing the same export is idempotent.
- [x] Interrupted imports leave no partial normalized transaction.
- [x] Workout routes and location-bearing records are excluded initially.
- [x] Daily and weekly aggregates can be regenerated from normalized records.

H3 verified 2026-07-12 with synthetic data (`tests/health/test_apple_health.py`;
suite total 590 passed). Streaming confirmed memory-flat at ~73MB for 500k
records. Bulk load uses `COPY FROM CSV` via a temp file in the protected home
(DuckDB parameterized INSERT is ~700 rows/s; COPY brings the pipeline to
~5,700 rows/s, parse-bound). Real-data verification (an actual iPhone export)
is a still-open human step.

## H4 — Documents and provenance

- [x] Source documents are copied or referenced without modification.
- [x] SHA-256, size, acquisition time, and document type are recorded.
- [x] Extracted text has `none`, `draft`, or `verified` status.
- [x] OCR output is never silently treated as verified source text.
- [x] Broken source-file references are detectable.
- [x] Generated reports enumerate their data snapshot and evidence IDs.
- [x] An observation can be traced to a source file and location.

H4 verified 2026-07-12 with synthetic fixtures (`tests/health/test_documents.py`,
`test_migration.py`; suite total 602 passed). Store schema v2→v3 (documents)
migrates additively with a premigrate backup. OCR itself is deferred: this
milestone establishes the immutable snapshot + extraction lifecycle
(none→draft→verified, verified only by explicit human action) and
broken-reference detection (`cairn health report broken-refs`, also a
`doctor` check). Extracted text is attached separately via
`cairn health document attach-text`.

## H5 — Reports and longitudinal comparison

- [ ] A current-status report shows latest value, date, source, and data-quality caveats.
- [ ] A timeline report combines observations and explicit events.
- [ ] Baseline and follow-up periods are defined in the output.
- [ ] Missingness and observation frequency are reported.
- [ ] A single value is not described as a persistent trend by the template.
- [ ] Reports distinguish source facts, derived calculations, and interpretations.
- [ ] The same snapshot and template version produce deterministic factual sections.
- [ ] Auto-generated reports stay within `90 Auto/Health`.
- [ ] The new `health` writer category (`90 Auto/Health`, overwrite allowed) is enforced by the same allowlist and path-validation tests as the existing categories, and `AGENTS.md` invariant 2 is revised in the same change.
- [ ] The vault-replication decision (H5-P1 in `PRIVACY.md` §10) is made and applied before the first real-data report is delivered; until then `90 Auto/Health` is excluded from every vault sync mechanism.
- [ ] Interpretive drafts use `00 Inbox/AI Drafts` and are created as new files only.

## H6 — AI interpretation and validation trail

- [ ] Interpretations are separate from observations, events, and documents.
- [ ] AI interpretations record model ID, prompt version, creation time, and data snapshot.
- [ ] Each consequential interpretation has an explicit evidence set.
- [ ] An interpretation with no evidence cannot become `accepted`.
- [ ] New analysis does not overwrite old analysis; it uses `supersedes_id` or an equivalent revision relation.
- [ ] Limitations and uncertainty are stored with the interpretation.
- [ ] Safety tests reject outputs that present diagnosis or medication changes as autonomous decisions.
- [ ] Model context is bounded by metric, time range, aggregation, row count, and source type.
- [ ] Embedded instructions in imported content do not override the analysis task.
- [ ] Original source text and model synthesis remain structurally separate.

## H7 — Cairn integration

- [ ] High-frequency observations are not inserted one-by-one into Cairn `items` or `chunks`.
- [ ] Cairn indexes report metadata and approved human-readable summaries only.
- [ ] Health-domain retrieval can be disabled independently.
- [ ] MCP tools are opt-in and have conservative default limits.
- [ ] A context pack identifies all included data snapshots and source categories.
- [ ] Failure of the health store does not corrupt `cairn.db`.
- [ ] Failure of Cairn search does not corrupt the health store.

## H8 — Backup, restore, and deletion

- [ ] Raw sources, database, catalog versions, and report metadata can be snapshotted consistently.
- [ ] Restore into an empty test environment reproduces record counts and hashes.
- [ ] Derived data can be deleted and regenerated.
- [ ] Backup failure does not destroy a successful import.
- [ ] Retention and encrypted destination requirements are documented.
- [ ] Destructive deletion lists raw, store, derived, reports, quarantine, and backups.
- [ ] Destructive deletion requires explicit confirmation.

## Local real-data verification

Only after all relevant synthetic tests pass:

1. Export the laboratory sheet to a local protected file.
2. Import once and verify date count, metric count, and latest date.
3. Compare at least three representative observations with the original source.
4. Re-import and verify no duplicate observations are created.
5. Generate a local factual report and inspect provenance links.
6. Confirm no real value appears in repository files or logs.
7. Run `git status --ignored` and the repository health-data audit.
8. Import an Apple Health export only after H2 privacy tests pass.
