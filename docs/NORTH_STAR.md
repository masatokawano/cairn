# Cairn North Star — Human Validation Platform

- Status: Proposal
- Date: 2026-07-11
- Scope: Long-term product and research direction
- Relationship to `docs/DESIGN.md`: This document states purpose and direction. `docs/DESIGN.md` remains the implementation authority.

## 1. The north star

Cairn exists to help a person preserve not merely what they have seen or thought, but **how their understanding changes under evidence**.

Its long-term destination is a **Human Validation Platform**: a personal, longitudinal system that makes observations, interventions, outcomes, interpretations, decisions, and evidence inspectable across years.

Cairn should help answer questions such as:

- What did I believe at the time, and why?
- What changed after an intervention or new evidence?
- Which conclusions remained stable, and which were revised?
- Which claims are supported by measurements, documents, or experiments?
- What remains uncertain or untested?
- What should be observed next to reduce uncertainty?

The goal is not to accumulate an ever-larger archive. The goal is to make knowledge **revisable, testable, and useful in action**.

## 2. Why this matters in the AI era

Generative AI makes plausible explanations and working artifacts abundant. Scarcity shifts away from production and toward validation.

The limiting questions become:

- Is this claim grounded?
- Is this result reproducible?
- Did the intervention actually work?
- Does the conclusion survive new evidence?
- Can another model or person inspect the reasoning trail?
- Are fact, inference, hypothesis, and decision clearly separated?

A system that only remembers content is insufficient. An AI-era external brain must also remember **provenance, change, uncertainty, and test outcomes**.

Cairn therefore evolves from an archive and retrieval engine into a platform for longitudinal validation.

## 3. The universal validation loop

Across health, research, software, security, learning, and decision-making, the same basic structure recurs:

```text
Observation
    ↓
Question or hypothesis
    ↓
Intervention, experiment, or decision
    ↓
Outcome
    ↓
Interpretation
    ↓
Validation against evidence
    ↓
Revision, continuation, or rejection
```

This loop is not assumed to be clean or causal. Cairn must preserve ambiguity:

- observations can be incomplete;
- interventions can overlap;
- outcomes can have multiple explanations;
- interpretations can conflict;
- evidence can be weak or stale;
- a conclusion can remain unresolved.

The platform should make these limitations visible rather than compressing them into false certainty.

## 4. Domains

The platform may eventually support several domains sharing the same validation primitives.

```text
Cairn
├── Knowledge     what was encountered and understood
├── Health        measurements, interventions, outcomes, clinical context
├── Research      questions, sources, hypotheses, analyses, findings
├── Software      requirements, changes, tests, incidents, regressions
├── Security      assets, controls, attacks, evidence, assurance results
├── Decisions     options, assumptions, choices, consequences
├── Learning      goals, practice, assessment, retained capability
└── Life          meaningful events and longitudinal personal context
```

These are not required to share one physical database. They share a conceptual validation model, provenance rules, and retrieval interface.

## 5. Why Health is the first proving ground

Health is the first major domain because it exposes nearly every hard problem early:

- long-term quantitative time series;
- heterogeneous source systems;
- interventions with uncertain causal effects;
- strict separation between measurement and interpretation;
- sensitive data requiring strong privacy boundaries;
- professional explanations alongside personal and AI analysis;
- the need to revisit earlier conclusions after later tests;
- practical outputs such as a visit brief or longitudinal trend review.

The Personal Health Observatory is therefore not a side feature. It is the first serious implementation of the Human Validation Platform model.

Its purpose is not automated diagnosis. Its purpose is to preserve what was measured, what changed, what was done, how the change was interpreted, and what evidence supports that interpretation.

## 6. Relationship to Validation Science

Validation Science asks how claims, systems, models, and interventions should be tested under real-world complexity, especially when AI accelerates both creation and error.

Cairn can serve as a personal-scale experimental substrate for this broader research direction.

The health domain validates questions such as:

- How should facts, derived metrics, and interpretations be separated?
- How can AI analyses remain inspectable after models change?
- How should contradictory interpretations coexist?
- What evidence is sufficient to change an accepted conclusion?
- How can longitudinal context be supplied without exposing unnecessary data?
- How can automated analysis remain useful without becoming an authority?

The software and security domains can later extend the same ideas toward Continuous Validation and Continuous Cyber Assurance.

## 7. Product principles

### 7.1 Preserve originals

Original conversations, documents, measurements, exports, and source references must remain recoverable. Derived indexes and summaries must be rebuildable.

### 7.2 Separate fact from interpretation

A measured value, a source quotation, an inferred relationship, a hypothesis, and a decision are different object types. The system must not collapse them into a single narrative record.

### 7.3 Make provenance first-class

Every important conclusion should identify the observations, documents, events, and analyses on which it depends.

### 7.4 Preserve revision history

New interpretations supersede or challenge old ones; they do not silently overwrite them. The platform should make intellectual change visible.

### 7.5 Prefer retrieval-time synthesis over premature certainty

The system should avoid freezing large amounts of speculative structure in advance. Retrieve relevant originals and structured observations, then synthesize for the current question with explicit provenance.

### 7.6 Use domain-appropriate stores

Conversation search, high-frequency health time series, documents, and analytical outputs need not share one storage engine. Cairn is the integration and validation layer, not a demand that all data become one table.

### 7.7 Local-first and minimum disclosure

Private longitudinal data should remain local by default. AI context should be bounded to the minimum data necessary for the specific question.

### 7.8 Human authority over consequential conclusions

AI can summarize, compare, propose hypotheses, and expose inconsistencies. It must not silently convert analysis into diagnosis, treatment, policy, or irreversible action.

### 7.9 Useful outputs over ontological perfection

The platform should produce concrete value: a weekly review, a research context pack, a health timeline, a visit brief, a change-impact report. It should not delay usefulness while attempting to model all human knowledge.

## 8. Core validation objects

The exact schema may differ by domain, but Cairn should converge on a small set of conceptual objects:

- **Observation** — a recorded fact, measurement, event, or source statement.
- **Question** — an uncertainty that motivates retrieval or analysis.
- **Hypothesis** — a provisional explanation or prediction.
- **Intervention** — an action intended to change or test something.
- **Outcome** — what occurred after an intervention, experiment, or passage of time.
- **Interpretation** — a human, professional, or AI explanation of observations.
- **Evidence set** — the explicit records used to support, contextualize, or limit an interpretation.
- **Decision** — a chosen action with assumptions and expected consequences.
- **Validation result** — the extent to which later evidence supports, weakens, contradicts, or leaves unresolved a claim or intervention.
- **Revision** — a traceable change to an earlier interpretation, hypothesis, or decision.

These are conceptual contracts, not a mandate for one universal database schema.

## 9. What Cairn should become

In mature form, Cairn should be able to construct a bounded, provenance-rich answer to a question such as:

> Show what I observed, what I believed, what I changed, what happened afterward, what evidence supports the current interpretation, and what remains uncertain.

The answer may cross conversations, notes, measurements, documents, source literature, code changes, and prior analyses. It should distinguish originals from generated synthesis and make every consequential statement inspectable.

## 10. What Cairn must not become

- a system that claims to know the person better than the person;
- an automated medical, legal, financial, or moral authority;
- a surveillance system that captures everything merely because it can;
- a knowledge graph filled with unreviewed inferred relationships;
- a cloud dependency that makes private memory inaccessible or exposed;
- an anxiety engine that treats every deviation as a warning;
- an archive so comprehensive that it becomes unusable;
- a model-specific artifact that loses meaning when the current AI is replaced.

## 11. Near-term direction

The next proving sequence is:

1. Ratify the Personal Health Observatory privacy and data boundaries.
2. Build a complete blood-test vertical slice from immutable source to normalized observations and reproducible report.
3. Add explicit medication, supplement, and lifestyle events.
4. Import selected Apple Health types using allowlists and deterministic deduplication.
5. Produce provenance-rich timelines and visit briefs.
6. Add opt-in, bounded MCP access for AI analysis.
7. Evaluate the resulting system as an implementation of the validation loop.

Only after the health vertical slice is reliable should the generic validation abstractions be extracted for other domains.

## 12. Ten-year test

Cairn is on course if, ten years from now, it can answer important questions without pretending that the past was clearer than it was.

It should reveal:

- the original records;
- the understanding held at each point in time;
- the interventions and decisions made;
- the evidence that later emerged;
- the revisions that followed;
- the uncertainties that still remain.

That is the north star: **not perfect memory, but inspectable learning over time**.
