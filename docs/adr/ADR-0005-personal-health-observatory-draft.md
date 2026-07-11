# ADR-0005: Add the Personal Health Observatory as an independent domain store

- Status: Draft
- Date: 2026-07-11
- Decision owner: Repository owner
- Related documents:
  - `docs/NORTH_STAR.md`
  - `docs/health/README.md`
  - `docs/health/DESIGN.md`
  - `docs/health/DATA_MODEL.md`
  - `docs/health/PRIVACY.md`
  - `docs/health/ACCEPTANCE.md`

## Context

Cairn currently integrates conversations, Karakeep, Zotero, and Obsidian around a local search and recall layer.
The proposed Personal Health Observatory introduces data with materially different characteristics:

- high-frequency and long-term numerical time series;
- heterogeneous sources such as laboratory sheets, Apple Health, home measurements, event logs, and medical documents;
- strict provenance requirements;
- a need to separate source fact, normalization, derived analysis, and interpretation;
- substantially higher privacy sensitivity;
- later comparison of interventions, outcomes, and revised interpretations.

The North Star proposes that Cairn evolve toward a Human Validation Platform. Health is the first proving domain, but the implementation should not force health records into storage abstractions designed for conversation search.

## Decision proposal

### 1. Use an independent health-domain store

Normalized health observations and domain records will be stored outside the production `cairn.db`, under a protected health data home.

Preferred analytical store: DuckDB, subject to dependency review during H0.

The raw source archive, health store, derived artifacts, reports, quarantine, and backups will live outside the Git worktree.

### 2. Keep Cairn as the integration and validation layer

The existing Cairn database may later index:

- approved factual reports;
- report metadata;
- human-readable summaries delivered through existing note pathways;
- bounded references required for retrieval.

It will not receive every high-frequency health sample as an `item` or `chunk`.

### 3. Preserve four layers

The domain will distinguish:

- **raw** — immutable source artifacts;
- **normalized** — structured observations with original values retained;
- **derived** — aggregates, comparisons, and figures that can be regenerated;
- **interpretation** — human, clinician, or AI explanations with explicit evidence and revision history.

### 4. Treat AI output as interpretation

AI analysis will not modify observations or source documents. It will be recorded with model ID, prompt version, data snapshot, evidence set, limitations, status, and supersession history.

### 5. Make model access opt-in and bounded

Health MCP tools, when introduced, will be disabled by default and will enforce limits on metric set, period, aggregation, row count, and source text disclosure.

### 6. Preserve existing write boundaries

The MVP will not expand Obsidian write locations beyond existing allowlists. Proposed health outputs will use:

- `90 Auto/Health/` for reproducible generated reports;
- `00 Inbox/AI Drafts/` for new interpretive drafts.

Any additional write target requires a separate decision and tests.

### 7. Delay generic framework extraction

The health domain may use validation concepts from `docs/NORTH_STAR.md`, but generic cross-domain schemas and APIs will not be extracted until the health implementation provides evidence about which concepts genuinely generalize.

## Consequences

### Positive

- protects the current Cairn search schema and performance;
- enables time-series and analytical queries with domain-appropriate storage;
- creates a clear privacy, backup, deletion, and access boundary;
- keeps source facts separate from derived and generated content;
- allows later model changes without rewriting original observations;
- gives the Human Validation Platform direction a concrete proving ground;
- permits rollback before deep integration.

### Negative

- introduces a second database and coordinated backup requirements;
- requires query orchestration across stores;
- may add a new dependency;
- requires separate migration and integrity tooling;
- makes MCP and report provenance more complex;
- does not provide FHIR compatibility automatically.

## Alternatives considered

### A. Add all health tables to `cairn.db`

Rejected for the initial implementation. It simplifies transactions but weakens privacy separation, enlarges the search database, requires root schema changes, and mixes high-frequency observations with knowledge items.

### B. Store health history only as Markdown notes

Rejected. Markdown is useful for reviewed understanding and reports, but is inadequate as the sole representation for idempotent import, units, reference ranges, high-frequency time series, and reproducible analysis.

### C. Embed Apple Health exports and medical documents directly into RAG

Rejected. Embedding is not a substitute for numerical computation, source verification, deduplication, or longitudinal provenance.

### D. Deploy a full FHIR server

Deferred. FHIR concepts can inform semantics and future export, but a server would add substantial complexity before the personal use cases and validation model are proven.

### E. Build a separate standalone health application with no Cairn integration

Rejected as the target architecture. A separate store is desirable, but retrieval, evidence, interpretation history, and longitudinal context should remain part of the Cairn platform.

## Required changes if accepted

1. Mark this ADR `Accepted`.
2. Add a concise health-domain decision to root `docs/DESIGN.md`.
3. Add health-data boundary rules to `AGENTS.md`.
4. Add repository audit and ignore rules for private health artifacts.
5. Implement H0 and H1 without changing production `cairn.db`.
6. Require independent review before adding MCP or expanding Obsidian writes.
7. Document backup and restore before routine real-data operation.

## Validation plan

The decision is considered supported when H0 and H1 demonstrate that:

- the health store remains isolated from `cairn.db`;
- source records are traceable and imports idempotent;
- no real values enter Git or logs;
- a factual report is reproducible from a fixed source snapshot;
- the same source can be re-imported without duplication;
- the module can be removed without damaging existing Cairn data.

## Rollback

Before Cairn/MCP integration, rollback consists of removing the health module and, with explicit confirmation, deleting the separate health data home.

Rollback must enumerate raw sources, store, derived artifacts, reports, quarantine, and backups. No production `cairn.db` migration should be required for H0 or H1.
