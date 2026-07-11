# Coding Agent Task — H0 and H1

## Mission

Implement the first trustworthy vertical slice of Cairn's Personal Health Observatory.

This task covers only:

- **H0** — safe, independent health-domain scaffold;
- **H1** — laboratory CSV importer and factual report.

The broader direction is defined in `docs/NORTH_STAR.md`, but this implementation must remain narrow. Do not build generic Human Validation Platform abstractions yet.

## Read before coding

1. `AGENTS.md`
2. root `docs/DESIGN.md`
3. `NOTES.md`
4. `docs/NORTH_STAR.md`
5. `docs/health/README.md`
6. `docs/health/DESIGN.md`
7. `docs/health/DATA_MODEL.md`
8. `docs/health/PRIVACY.md`
9. `docs/health/ACCEPTANCE.md`
10. `docs/health/ROADMAP.md`
11. `docs/adr/0005-personal-health-observatory.md`

If these documents conflict, stop before implementation and report the exact conflict with a proposed documentation change.

## Contribution to success criteria

This task directly contributes to:

- H-S1 source traceability;
- H-S3 idempotent import;
- H-S5 privacy boundary;
- H-S6 independent health-store integrity;
- H-S7 low-friction recurring import.

It establishes the factual observation layer required for later H-S2 and H-S4.

## Hard constraints

- Do not use real personal health data in repository files or tests.
- Do not modify the production `cairn.db` schema.
- Do not change the existing `items.kind` constraint.
- Do not add MCP health tools.
- Do not modify the Obsidian writer allowlist.
- Do not implement Apple Health parsing.
- Do not implement Google OAuth or direct Sheet synchronization.
- Do not implement AI medical analysis.
- Do not perform diagnosis, risk scoring, or medication recommendations.
- Do not write data inside the Git worktree.
- Do not log values, medical free text, provider names, or absolute source paths.
- Do not perform production migration, deletion, launchd changes, commit, push, or merge without explicit approval.
- Do not broaden the task through unrelated refactoring.

## Required planning report

Before changing code, report:

- current relevant architecture;
- exact files proposed for addition or modification;
- dependency decision;
- health-store schema for H0/H1;
- CLI/API changes;
- privacy risks and mitigations;
- test plan mapped to `ACCEPTANCE.md`;
- any documentation inconsistency.

After the plan is accepted, continue within that scope without repeatedly asking for reversible implementation decisions.

## Proposed package structure

Adapt naming to the current repository conventions when necessary, but keep the domain isolated.

```text
backend/app/health/
├── __init__.py
├── config.py
├── store.py
├── schema.py
├── catalog.py
├── cli.py
├── importers/
│   ├── __init__.py
│   └── labs_csv.py
└── reports/
    ├── __init__.py
    └── lab_summary.py

backend/tests/health/
├── fixtures/
│   └── synthetic_labs.csv
├── test_config.py
├── test_store.py
├── test_labs_csv.py
├── test_idempotency.py
├── test_privacy.py
└── test_lab_summary.py
```

## Required CLI

```bash
cairn health init
cairn health doctor
cairn health import labs-csv FILE
cairn health status
cairn health report labs
```

Commands must produce machine-readable status where practical and must not print sensitive values in routine logs.

## H0 implementation

### Health data home

Default:

```text
~/Library/Application Support/Cairn/health/
```

Subdirectories:

- `raw/`
- `store/`
- `derived/`
- `reports/`
- `quarantine/`
- `backups/`

Requirements:

- directory mode 0700;
- data files mode 0600;
- reject a location inside a Git worktree;
- reject traversal and unsafe symlink targets;
- allow `CAIRN_HEALTH_HOME` for tests and advanced configuration;
- use temporary directories in tests.

### Store decision

Evaluate DuckDB against the current dependency and packaging constraints.

Preferred: DuckDB because later milestones require analytical time-series queries and Parquet support.

Acceptable H1 fallback: SQLite behind a narrow repository/store interface, only when adding DuckDB would materially disrupt installation or packaging. If choosing the fallback:

- record the reason in the completion report;
- avoid SQLite-specific semantics leaking into importer and report code;
- do not claim DuckDB support.

### H0 schema

Implement only the H0/H1 subset:

- `schema_meta`
- `source_files`
- `import_runs`
- `metric_catalog`
- `metric_aliases`
- `observations`
- `quarantine_records`

Do not implement all future tables from `DATA_MODEL.md` in advance.

## H1 synthetic fixture

Create a completely synthetic laboratory CSV containing:

- three or more invented metrics;
- three dates;
- numeric values;
- a blank;
- a qualitative value such as `<5`;
- changed reference range;
- unknown metric;
- unknown unit.

Do not copy the user's real metric sequence, dates, values, facility names, medication names, or source filename.

## H1 import behavior

The importer must:

1. validate the source path;
2. calculate SHA-256;
3. copy an immutable source snapshot to `raw/`;
4. register `source_files` and `import_runs`;
5. parse the horizontal date-column format;
6. map aliases using a versioned catalog;
7. retain original metric, value, unit, and reference text;
8. store normalized fields separately;
9. preserve date-only precision;
10. create deterministic fingerprints;
11. make unchanged re-import idempotent;
12. quarantine unknown metrics rather than guessing;
13. preserve unknown-unit originals without false conversion;
14. retain a source row or cell reference;
15. use a transaction for normalized writes;
16. redact errors and logs.

Do not invent missing values, dates, units, or reference ranges.

## Factual laboratory report

Generate a deterministic Markdown report from a fixed data snapshot.

Required sections:

- generation metadata;
- source snapshot identifiers;
- measurement dates;
- imported metric and observation counts;
- latest factual values, dates, and sources;
- missingness summary;
- quarantine summary;
- provenance identifiers;
- limitations.

Prohibited content:

- diagnosis;
- medical interpretation;
- trend causality;
- medication recommendations;
- risk scores;
- statements that a value is safe or dangerous.

This report is a data-verification artifact, not a clinical report.

## Required tests

At minimum:

- safe default data-home creation;
- permission enforcement;
- rejection of worktree and traversal paths;
- schema initialization and version check;
- successful horizontal-to-long import;
- original/normalized field separation;
- qualitative value handling;
- changed reference range handling;
- unknown metric quarantine;
- unknown unit preservation;
- deterministic fingerprinting;
- unchanged re-import idempotency;
- transaction rollback on malformed input;
- no sensitive fixture values in captured logs;
- deterministic factual report;
- existing Cairn test-suite regression.

Map each test to the relevant `ACCEPTANCE.md` checkbox in code comments or the completion report.

## Completion report

Report only verified facts and include:

- changed files;
- dependency decision;
- implemented schema;
- implemented CLI commands;
- exact test commands and observed results;
- H0/H1 acceptance checklist;
- privacy and repository-audit results;
- limitations;
- decisions required before H2.

Do not state that implementation or tests are complete unless tool output proves it.
