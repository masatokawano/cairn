# Personal Health Observatory — Roadmap

## Implementation strategy

The health domain is the first proving ground for the Human Validation Platform described in `docs/NORTH_STAR.md`.

The roadmap therefore prioritizes a complete, inspectable validation loop over broad source coverage.
Each milestone must deliver a coherent vertical slice, pass synthetic-data tests, and avoid premature abstraction into other domains.

## H0 — Ratify architecture and safety boundary

### Goal

Make the health domain safe to begin without changing the production Cairn database.

### Deliverables

- ADR-0005 decision
- relationship among North Star, root design, and health design
- protected health data home
- configuration loader
- independent schema/version mechanism
- synthetic fixture policy
- repository private-data audit design
- `cairn health init`
- `cairn health doctor`

### Explicit exclusions

- real health-data import
- root `cairn.db` migration
- MCP exposure
- Obsidian delivery
- Apple Health parsing

### Completion

All H0 criteria in `ACCEPTANCE.md`.

## H1 — Laboratory-data vertical slice

### Goal

Turn the existing longitudinal laboratory spreadsheet into an immutable-source, normalized, reproducible dataset.

### Deliverables

- protected raw CSV snapshot
- horizontal-to-long importer
- minimal metric catalog and aliases
- original and normalized values
- per-date reference ranges
- import audit trail
- deterministic fingerprinting and idempotency
- quarantine for ambiguous mappings
- factual laboratory summary report
- CLI status output

### Validation loop demonstrated

```text
Source measurement
→ normalized observation
→ derived trend
→ factual report
→ source traceability
```

### Completion

All H1 criteria in `ACCEPTANCE.md`, plus local comparison of representative records against the source sheet.

## H2 — Explicit event ledger

### Goal

Place interventions and context on the same timeline as laboratory observations.

### Initial event types

- medication start, stop, and dose change
- supplement start, stop, and dose change
- smoking cessation
- alcohol-use change
- exercise-program change
- acute illness
- procedure or imaging examination
- travel or other context likely to alter measurements

### Deliverables

- event schema
- YAML or minimal UI entry path
- uncertainty-aware date representation
- append-only correction/supersession
- event overlays on laboratory trends
- factual before/after period queries

### Validation loop demonstrated

```text
Observation
→ intervention
→ subsequent observation
→ bounded comparison
```

Causal inference is not claimed automatically.

### Completion

All H2 criteria in `ACCEPTANCE.md`.

## H3 — Apple Health selected import

### Goal

Add high-frequency personal measurements without turning Cairn into a raw sensor archive.

### Initial allowlist

- step count
- resting heart rate
- heart-rate variability
- body mass
- sleep analysis
- systolic and diastolic blood pressure
- exercise time

### Deliverables

- streaming XML parser
- allowlist enforcement
- deterministic deduplication
- source/device/time-zone retention
- daily and weekly aggregates
- data-quality report
- exclusion of workout routes and location-bearing records

### Completion

All H3 criteria in `ACCEPTANCE.md`.

## H4 — Medical documents and evidence linkage

### Goal

Connect observations and interpretations to their documentary provenance.

### Deliverables

- medical-document registry
- immutable hash and metadata
- extraction status: none/draft/verified
- broken-reference detection
- report evidence links
- optional manual verification workflow for extracted values

OCR-assisted ingestion may be explored only after provenance and verification states are implemented.

## H5 — Longitudinal reports

### Goal

Produce outputs that are useful in daily life and clinical conversations.

### Reports

- current status
- lifetime health timeline
- laboratory trends
- event-response comparison
- data quality and missingness
- next-visit brief
- unresolved questions

### Requirements

- factual and interpretive sections separated
- snapshot and template versions recorded
- source and observation IDs listed
- uncertainty and missingness visible
- writable area stays inside the existing `90 Auto` / `00 Inbox/AI Drafts` trees; the
  `90 Auto/Health` subdirectory requires adding a `health` category to the
  `obsidian_writer.py` allowlist (see H-D7) before delivery starts
- the vault-replication decision H5-P1 (`PRIVACY.md` §10) resolved before the
  first real-data report is written into the vault

## H6 — AI interpretation and revision trail

### Goal

Allow AI to analyze selected evidence without converting model output into authority.

### Deliverables

- interpretation records
- explicit evidence sets
- model and prompt provenance
- limitations
- accepted/rejected/superseded states
- comparison of current analysis with prior interpretations
- safety tests for diagnosis and medication-change language

### Validation loop demonstrated

```text
Question
→ bounded evidence set
→ interpretation
→ later evidence
→ support, weakening, contradiction, or unresolved status
→ revision
```

## H7 — Cairn and MCP integration

**Status: completed 2026-07-13 (independent security review completed).**

### Goal

Make health context available to AI sessions under explicit, minimum-disclosure controls.

### Implemented tools

- `health_current_status`
- `health_query_observations`
- `health_compare_event`
- `health_data_quality`
- `health_interpretation_history`
- `health_get_interpretation`
- `health_build_context_pack`

### Constraints

- opt-in
- conservative row and time limits
- aggregates preferred
- high-frequency samples not inserted one-by-one into Cairn search indexes
- original and generated content structurally separated

Implemented as a separate `cairn-health` STDIO server. It refuses startup
without explicit opt-in, opens the independent DuckDB store read-only, requires
metric selection, and keeps events/interpretations behind separate opt-ins.
High-frequency observations remain outside `cairn.db`; auto reports remain
excluded from Obsidian indexing, while human-promoted summaries use the normal
note path.

## H8 — Reliability and long-term operation

### Goal

Make the system trustworthy over years.

### Deliverables

- encrypted backup and restore
- migration policy
- catalog and parser versioning
- import inbox and failure notification
- health-store integrity checks
- deletion and retention procedures
- performance baseline
- monthly health review

## H9 — Evaluate the generic validation model

### Goal

After the health implementation is reliable, identify which primitives genuinely generalize to other domains.

### Evaluation questions

- Which objects are shared by Health, Research, Software, and Security?
- Which are merely similar in name but require different semantics?
- What should remain domain-local?
- Can evidence sets and revision histories share interfaces?
- How should validation status represent uncertainty without false precision?
- Which abstractions make reports better, and which only add complexity?

No generic validation framework should be extracted before this evidence exists.

## Immediate execution order

For the current short implementation window:

1. finalize and review the documentation PR;
2. ratify ADR-0005;
3. implement H0 completely;
4. implement H1 completely with synthetic tests;
5. perform local real-data verification;
6. stop and review before H2.

Relationship to the Cairn main line: the M6③ evaluation freeze
(`docs/backlog.md`, until late July 2026) protects the recall/related tuning
evaluation on `cairn.db`. H0/H1 touch neither `cairn.db` nor any existing
pipeline, and were explicitly exempted from the freeze by the repository owner
on 2026-07-11. Work that does touch Cairn integration (H5 Obsidian delivery,
H7 MCP) stays behind the freeze and its own reviews.

The first success is not “all health data imported.” It is one complete laboratory-data validation trail that can be trusted and reproduced.
