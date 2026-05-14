# Evaluation methodology

The eval harness is the project's measurement instrument. It produces three numbers per batch — F1, cost-per-document, and latency p50/p99 — across a partitioned document corpus. F1 is the headline metric and produces the F1-over-time chart on the README and live demo. The other two are secondary signals showing that self-improvement reduces cost and latency in lockstep with accuracy gains.

This document covers how those numbers are computed, where the corpus comes from, how the partitions prevent train/test leakage, and the progressive alias-table mechanism that makes the F1-over-time chart move.

## Metrics

### F1

F1 is computed at the field level across all extracted forms in the eval batch. For each form, every populated `ExtractedField` is compared to ground truth:

- **True positive:** populated field whose extracted value matches ground truth (exact match for structured fields like dates and SSN; normalized string comparison for free-text fields after lowercasing and whitespace collapse)
- **False positive:** populated field whose value does not match ground truth, *or* a populated field that should have been blank (extracted ghost values)
- **False negative:** ground-truth value present, extraction returned None or returned a wrong value

F1 = 2 × precision × recall / (precision + recall), micro-averaged across all field-level (TP, FP, FN) counts in the batch. Per-vertical and per-field-type F1 breakdowns are also computed for diagnostic use; the headline number on the README is the micro-averaged F1 across the full eval test partition.

Confidently-blank fields — `value=None` with `tier_used` set, indicating the cascade affirmatively determined no value was present — are excluded from precision/recall. They are tracked separately for the reviewer UI but do not penalize F1.

### Cost per document

Cost per document is actual inference spend divided by document count, computed from per-call telemetry written to the eval-results store (SQLite in V1, Aurora in V2) during cascade execution. Provider rates are versioned in `evals/cost_table.json` so cost numbers remain reproducible against a frozen pricing snapshot even as upstream pricing changes.

In cached-fixture mode (the default), cost per document is computed from the cached fixture's recorded provider/tier metadata, not from live calls. Live mode (`EVAL_LIVE=true`) pulls real provider invoices via the cost-tagging system on each cascade run.

**V1 cost-per-doc reads $0 for every document** — V1's cascade has no cloud-provider surface, so there's no inference spend to divide. The harness still emits the column for schema continuity, but the V1 portfolio narrative leans on the latency-over-time chart (more Tier 1 hits = faster cascade) and F1-over-time as the primary signals. V1's measured escalation rates also feed the V2 cost projection — the harness can model "what this batch would have cost in V2" by multiplying V1 escalation rates against the V2 cost table, which is the strongest V1-only cost-engineering signal the portfolio carries.

### Latency p50/p99

Latency is wall-clock time from cascade entry to last-tier response, recorded per document. p50 and p99 are computed across the eval batch. The eval harness also breaks latency down by tier — how long each document spent in each tier — to help diagnose where time accumulates when escalation rates climb.

## Cached fixtures vs live mode

The eval harness defaults to cached fixtures. `evals/fixtures/` contains versioned per-tier responses for every document in the test partition. `evals/fixtures_manifest.json` records the version, the date the fixture was generated, and the model versions used for each provider call.

This default exists for two different reasons in V1 vs V2. In V2, a single full eval batch through the live cascade costs roughly $5–10 at current pricing (500–1000 docs at the ~$9.50/1K cascade rate; local-tier inference is essentially free, so the V2 live spend is the cloud-tier portion only) — fine for explicit Phase 6 fixture-generation runs, ruinous if it accumulates accidentally across every CI run. In V1 the spend is $0 across the board, but live mode still drives the Ollama models through full inference per document — slow wall-clock time (~30 s/doc for a deep Tier 3 escalation) compared to cached replay (~1 ms/doc). Cached mode reads fixtures from disk at full speed and produces deterministic F1 numbers; live mode produces slightly non-deterministic F1 because LLM responses have noise.

CI never runs in live mode (V1 or V2). Live runs require the `EVAL_LIVE=true` environment variable, set explicitly during local fixture-generation sessions. Phase 6 generates an initial fixture set against ~50 documents, sanity-checks F1, then expands to the full test partition. V1's "build budget" is wall-clock time rather than dollars — the small-testing-first discipline keeps fixture-generation runs to overnight rather than weekend-long. V2's build budget returns to the $30–50 envelope described in `docs/production-roadmap.md`.

## Partition discipline

`evals/manifest.json` partitions every document in the corpus into one of three sets:

- **train:** used for prompt engineering, threshold tuning during development, and Phase 9's QLoRA fine-tuning. Looking at these documents to inform extraction logic is allowed.
- **dev:** used for the Phase 5 router calibration spot-check (~50 hand-classified documents, roughly 25 healthcare + 25 business) and for sanity-checking changes before running test. Looking at these documents during development is allowed but should be rare.
- **test:** held out. The eval harness reports F1 on test only. Documents in test are never inspected during cascade development; threshold tuning never reads test.

The partition is stratified across verticals (healthcare and business documents both appear in all three splits at consistent ratios) and across document complexity (single-page forms, multi-page forms, rotation-corrected pages, and mostly-blank pages all appear proportionally in each split).

## Leakage mitigations

Two leakage paths exist in the synthetic + DocILE corpus and are addressed explicitly.

**Synthea patient leakage.** A single Synthea-generated patient produces multiple documents (different visit dates, different facility templates). If the same patient appears in both train and test, an extractor that memorized the patient's idiosyncratic data could appear to generalize when it has not. Partitioning is therefore done at the *patient* level, not the document level: every document for a given Synthea patient lands in the same partition. This is enforced in the partition-generation script by hashing on patient ID, not document ID.

**DocILE annotation leakage.** DocILE provides its own train/val/test split. The project's `evals/manifest.json` test partition for the business-documents vertical is a strict subset of DocILE's val + test (never DocILE's train). DocILE train is used for prompt examples and few-shot retrieval corpus seeding only — never as eval ground truth.

CI runs a partition-validation test that asserts no patient ID and no DocILE document ID appears in more than one of the three sets. The test runs in the standard CI suite on every PR; a partition-script edit that introduces leakage fails the build.

## Progressive alias-table partition for F1-over-time

The F1-over-time chart is the README's headline visual and the most semantically loaded artifact in the repo. It demonstrates the cascade's self-improvement loop: as the alias table grows, fewer fields require escalation, so F1 climbs and cost falls in tandem.

In production with real reviewers, the loop runs continuously — corrections write back to the alias table, and subsequent batches benefit. In this portfolio demo there are no live reviewers, so the corrections feeding the loop are seeded from the schema-design alias work rather than reviewer-generated. The mechanism is identical; only the source of corrections differs. This is documented honestly in the README rather than dressed up as live reviewer data.

### Partition mechanism

`alias_table_seed.json` (~465 aliases across 86 records as of seed v1.0.0) is partitioned into batches by per-record alias position. Batch N includes positions 0 through N–1 of every record's `aliases` array.

Concrete batch math against the current seed:

| Batch | Positions included | Approx alias count | % of total |
|------:|:------------------|-------------------:|-----------:|
| 1     | 0                 | ~86                | ~18%       |
| 2     | 0–1               | ~170               | ~37%       |
| 3     | 0–2               | ~250               | ~54%       |
| 4     | 0–3               | ~330               | ~71%       |
| 5     | 0–4               | ~395               | ~85%       |
| 6     | 0–5               | ~440               | ~95%       |
| 7     | 0–6               | ~460               | ~99%       |
| 8     | all               | 465                | 100%       |

Records with fewer aliases than the current batch index contribute their full list and no further. The exact alias counts above will drift as the seed evolves; the partition definition — "Batch N includes positions 0 through N–1 of every record's `aliases` array" — is the stable contract.

The natural batch count is around 8 given the current seed (the longest `aliases` lists have 8 entries). The chart plateaus naturally as later batches add fewer marginal aliases, which is the desired shape — F1 should climb sharply early, then asymptote.

### Why position-based ordering

The `aliases` arrays in `build_alias_seed.py` are hand-curated in priority order. Position 0 is the canonical/authoritative phrasing for each field, typically the exact label from the source standard (CMS-1500, ACORD 125, USCIS I-9, IRS W-4, etc.). Subsequent positions are variants in rough decreasing real-world frequency — not a measured frequency, but a curator's judgment informed by inspection of dozens of real forms.

For hand-curated taxonomies, canonical priority and real-world frequency are heavily correlated. A field's canonical label is canonical *because* most forms use it. Position-based introduction is therefore a cheap, reproducible proxy for frequency-based introduction without requiring corpus-derived frequency counts.

Three alternatives were considered and rejected.

**Measured frequency from the rendered corpus** would be the most rigorous option but breaks down on synthetic data. Frequency in the Synthea + DocILE corpus is determined by the rendering templates and DocILE's source distribution, not by real-world prevalence on intake forms in the wild. Measuring template diversity and calling it "alias frequency" misrepresents what is being computed. If a CMS-1500 template uses "Patient Name" exclusively, then "Patient's Name" has frequency 0 in the corpus regardless of how common it is on real intake forms — the measurement reflects the template author's choices, not the field's actual prevalence.

**Source-standard priority** (introducing all aliases from records with a `source_standard` value first, then the rest) partitions records but does not graduate aliases. About 50% of seed records have a `source_standard` value, so this approach produces one big batch and one big "everything else" — a step function, not a curve, and the chart loses its expressive power.

**Random with fixed seed** is reproducible but tells no story. A chart that climbs because of randomness is not a meaningful self-improvement narrative; it is a sleight-of-hand visual that would invite legitimate skepticism from anyone reading the code.

### Reproducibility

The partition is deterministic given a frozen seed. `alias_table_seed.json`'s top-level `version` field (currently `"1.0.0"`) is recorded in every entry of `evals/fixtures_manifest.json`. F1-over-time runs against a given fixture set are directly comparable only to runs against the same seed version — comparing across seed versions requires regenerating the chart from scratch.

When seed v2.0.0 ships (e.g., from Phase 8 correction feedback adding new aliases, or from a deliberate reordering of existing aliases in light of new evidence), the F1-over-time chart can be regenerated against the new seed for that version's self-improvement curve. The v1.0.0 chart is not backward-extended. The README documents whichever version produced the live chart.

### Phase 8 implication

The correction feedback loop in Phase 8 will introduce new aliases from reviewer corrections. These aliases are appended to the *end* of the relevant record's `aliases` list per the convention documented in `build_alias_seed.py`. They represent the latest-discovered variants and naturally belong in the highest-numbered batches. This preserves the historical batch shape: aliases that existed in seed v1.0.0 stay in their original positions, and the new ones extend the curve at the high end.

If a Phase 8 correction reveals that a specific variant is significantly more common than originally believed and should be promoted to an earlier position, that is a v2.0.0 seed bump rather than an in-place edit to v1.0.0. The version bump is the explicit signal to regenerate the F1-over-time chart from scratch.

## Operational notes

The eval harness is invoked via `just eval` (cached fixtures) or `just eval-live` (live cloud calls, opt-in only). Both commands write results to `evals/results/` keyed on a UTC timestamp + git short SHA + seed version. Historical results are gitignored — they regenerate deterministically from the fixture set and seed.

The static SVG F1-over-time chart that lands on the wake-page during Aurora cold-start (and on the README until Phase 6 produces real data) is generated by a separate `just chart` command that reads from `evals/results/` and emits to `docs/assets/f1-over-time.svg`. The chart is regenerated as a manual step at Phase 7 and again at Phase 10 polish; intermediate phases use the placeholder SVG.
