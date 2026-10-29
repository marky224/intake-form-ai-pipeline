# Evaluation methodology

The eval harness is the project's measurement instrument. It produces three numbers per batch — F1, cost-per-document, and latency p50/p99 — across a partitioned document corpus. F1 is the headline metric; the README's headline visual is the **by-stage ablation + escalation funnel** (`docs/assets/f1-by-stage.svg`), with the Tier-1 F1 series over alias batches (the self-improvement measurement, no longer charted since PR #79) and the full two-stage finding documented here in this methodology doc. The other two numbers are secondary signals showing that self-improvement reduces cost and latency in lockstep with accuracy gains.

This document covers how those numbers are computed, where the corpus comes from, how the partitions prevent train/test leakage, and the progressive alias-table mechanism that moves the Tier-1 F1 series.

## Metrics

### F1

F1 is computed at the field level across all extracted forms in the eval batch. For each form, every populated `ExtractedField` is compared to ground truth:

- **True positive:** populated field whose extracted value matches ground truth (exact match for structured fields like dates and SSN; normalized string comparison for free-text fields after lowercasing and whitespace collapse)
- **False positive:** populated field whose value does not match ground truth, *or* a populated field that should have been blank (extracted ghost values)
- **False negative:** ground-truth value present, extraction returned None or returned a wrong value

F1 = 2 × precision × recall / (precision + recall), micro-averaged across all field-level (TP, FP, FN) counts in the batch. Per-vertical and per-field-type F1 breakdowns are also computed for diagnostic use; the headline number on the README is the micro-averaged F1 across the full eval test partition.

Confidently-blank fields — `value=None` with `tier_used` set, indicating the cascade affirmatively determined no value was present — are scored against ground truth like any other empty field: if the form has a value for that field, the cascade's "absent" judgment was wrong and the field is a **false negative**; if the form has no value either, it is a true negative and stays out of precision/recall (counted separately for diagnostics). Until 2026-10-16 every confidently-blank field was excluded from precision/recall even when ground truth existed, which hid 8 misses on the 92-doc `test` split and overstated cascade-stage F1 by 0.003 (0.768 → 0.765).

### Cost per document

Cost per document is actual inference spend divided by document count, computed from per-call telemetry written to the eval-results store (SQLite in V1, Aurora in V2) during cascade execution. Provider rates are versioned in `src/evals/cost_table.json` so cost numbers remain reproducible against a frozen pricing snapshot even as upstream pricing changes.

In cached-fixture mode (the default), cost per document is computed from the cached fixture's recorded provider/tier metadata, not from live calls. Live mode (`EVAL_LIVE=true`) pulls real provider invoices via the cost-tagging system on each cascade run.

**V1 cost-per-doc reads $0 for every document** — V1's cascade has no cloud-provider surface, so there's no inference spend to divide. The harness still emits the column for schema continuity, but **F1 is the only primary signal in V1**. There is deliberately no latency-over-time or escalation-over-time companion chart: cached replay is ~1 ms/doc regardless of tier (replay overhead, not wall-clock), and a live run is non-deterministic, so neither can be a committed CI-drift-guarded artifact the way the F1 SVGs are. The operational reading of self-improvement is the Tier-1 *hit rate*, and that *is* the Tier-1 F1 series itself (see *Two-stage measurement* below) — not a separate artifact. V1's measured escalation rates still make the optional BAA-cloud enhancement's cost projection credible — the harness can model "what this batch would have cost under the BAA-cloud cascade" by multiplying V1 escalation rates against `src/evals/cost_table.json`, which is the strongest cost-engineering signal a complete-as-local portfolio can carry; that is a one-off modeled number, not a charted trend.

### Latency p50/p99

Latency is wall-clock time from cascade entry to last-tier response, recorded per document. p50 and p99 are computed across the eval batch. The eval harness also breaks latency down by tier — how long each document spent in each tier — to help diagnose where time accumulates when escalation rates climb.

## Cached fixtures vs live mode

The eval harness defaults to cached fixtures. Rather than a parallel fixture store, it reuses the cascade's existing per-tier replay cache at `src/tests/fixtures/eval-cache/<provider>/<image_sha256>.json` (already populated for every committed document across all tiers + the router). `src/evals/fixtures_manifest.json` is a thin manifest pinning the alias-seed version, the per-provider model identities, and the document id list to that cache — it deliberately carries no per-run timestamp (the replay fixtures were generated in Phase 4, not per eval run, and it is a committed file).

This default exists for two different reasons in V1 vs the optional cloud enhancement. In a BAA-cloud deployment, a single full eval batch through the live cascade would cost roughly $5–10 at current pricing (500–1000 docs at the ~$9.50/1K cascade rate; local-tier inference is essentially free, so live spend would be the cloud-tier portion only) — fine for explicit fixture-generation runs, ruinous if it accumulated accidentally across every CI run. In the complete V1 the spend is $0 across the board, but live mode still drives the Ollama models through full inference per document — slow wall-clock time (~117 s/doc for Tier 3's full-field second read, 161 s for a cold first call) compared to cached replay (~1 ms/doc). Cached mode reads fixtures from disk at full speed and produces deterministic F1 numbers; live mode produces slightly non-deterministic F1 because LLM responses have noise.

CI never runs in live mode (V1 or V2). Live runs require the `EVAL_LIVE=true` environment variable, set explicitly during local fixture-generation sessions. Phase 6 generates an initial fixture set against ~50 documents, sanity-checks F1, then expands to the full test partition. V1's "build budget" is wall-clock time rather than dollars — the small-testing-first discipline keeps fixture-generation runs to overnight rather than weekend-long. Were the optional BAA-cloud enhancement ever built, its build budget would return to the ~$30–50 envelope described in `docs/production-roadmap.md`.

## Partition discipline

`src/evals/manifest.json` partitions every document in the corpus into one of three sets:

- **train:** used for prompt engineering, threshold tuning during development, and Phase 9's QLoRA fine-tuning. Looking at these documents to inform extraction logic is allowed.
- **dev:** 98 healthcare (CMS-1500) documents, used for sanity-checking changes before running test. Looking at these documents during development is allowed but should be rare. (The router spot-check documents are a separate local run — see *Router spot-check* — not a dev-split slice: the manifest holds no business documents.)
- **test:** held out. The eval harness reports F1 on test only. Documents in test are never inspected during cascade development; threshold tuning never reads test.

The partition is stratified at the patient level only. The committed manifest is 584 of 584 healthcare documents (CMS-1500 rendered from Synthea; `train` 394 / `dev` 98 / `test` 92), all single-page renders, so there is no cross-vertical or document-complexity stratification to report. DocILE business documents are never in the manifest (CC-BY-NC-ND, local-only); the business-vertical numbers in this doc come from separate local runs.

## Leakage mitigations

Two leakage paths exist in the synthetic + DocILE corpus and are addressed explicitly.

**Synthea patient leakage.** A single Synthea-generated patient produces multiple documents (different visit dates, different facility templates). If the same patient appears in both train and test, an extractor that memorized the patient's idiosyncratic data could appear to generalize when it has not. Partitioning is therefore done at the *patient* level, not the document level: every document for a given Synthea patient lands in the same partition. This is enforced in the partition-generation script by hashing on patient ID, not document ID.

**DocILE annotation leakage.** DocILE provides its own train/val/test split. The committed `src/evals/manifest.json` holds no DocILE documents (see *Partition discipline*), so there is no business test partition to leak into; any local DocILE evaluation draws from DocILE's val + test, never its train, and DocILE train is never used as eval ground truth.

CI runs a partition-validation test that asserts no patient ID and no DocILE document ID appears in more than one of the three sets. The test runs in the standard CI suite on every PR; a partition-script edit that introduces leakage fails the build.

## Human-in-the-loop review queue

The review queue is the cascade's HITL surface — the *input* side of the self-improvement loop (the next section is what a human's correction then does). A field that is still below the 0.80 review gate after the Tier 3 second read is parked to the `review_queue` table (`src/cascade/store.py`) rather than silently accepted; the demo renders it under the "Human-in-the-loop review queue" heading with its per-tier error history.

**The queue is populated by design, not by failure.** A field parks when Tier 2 and Tier 3 read it differently, or only one of them finds a value: Tier 3 re-reads every field Tier 2 handled, and a mismatch gets confidence 0.5, below the 0.80 gate (see *By-stage ablation*). On the cached 92-doc `test` split every document still parks at least one field — in 90 of them Tier 2 reads a HIPAA acknowledgment that Tier 3 does not see — and the parked fields are the disagreements (after that acknowledgment, most often subscriber name, subscriber date of birth and first name). Until 2026-10-16 the queue was filled by a type heuristic that scored every date 0.5, so every form with a date parked whether or not the date was right. A non-empty queue is the intended operating point of a cascade built around human adjudication, not an error rate to drive to zero; the gate is deliberately *not* tuned to empty the queue.

**Measurement boundary (why this lives in the methodology doc).** Parking a field for review does **not** change its F1 contribution. F1 is computed over every populated `ExtractedField` against ground truth regardless of whether that field was also routed to `review_queue` — a wrong-but-parked value still counts as a false positive. This is the deliberate guard against the obvious gaming path: if "sent to review" excused a field from scoring, a cascade could inflate F1 simply by parking everything it was unsure of. The queue is operational triage layered on top of the metric, never a metric adjustment. Confidently-blank fields follow the same no-excuse rule (see *Metrics → F1*): a `value=None`-with-`tier_used` field is a false negative when the form has a value for it, and only a blank that is also blank on the form (a true negative) stays out of precision/recall. Neither parking nor a confident "absent" judgment removes a field from the F1 denominator when ground truth exists.

The coupling to self-improvement: the fields the queue surfaces are exactly the fields a reviewer corrects, and those corrections are what grow the alias overlay described next. The queue is the surface; the loop is the learning.

## The self-improving loop (component)

The self-improvement story has two halves: a **live runtime loop** (the component described here) and an **offline analogue** that measures it (the progressive alias-table sweep, next section). They share one mechanism — alias growth feeding the Tier 1 layout-to-fields post-processor and the Stage-1 router vocabulary — so the measured Tier-1 F1 series is a faithful proxy for the live loop, not a separate story.

The live loop runs in the demo's *reviewer correction feedback* surface. When a reviewer corrects a parked field, three things happen: (1) the correction is logged to the `corrections` table; (2) the on-form label the cascade missed is appended to a **live alias overlay** (`src/data/corrections_aliases.json`) — the frozen v1.0.0 seed and the committed F1 chart are never mutated, so the overlay is a pure runtime extension that the Tier 1 + router alias consumers union on top of the seed at load time, taking effect for the *next* extraction; (3) the document is re-embedded into the ColQwen retrieval corpus (demo-only: it feeds the nearest-neighbor panel, not extraction, and is a no-op on the cached path, since no `.npy` fixtures are committed). In the portfolio demo there are no live reviewers, so corrections are *seeded* from the schema-design alias work and replayed — the mechanism is identical, only the source of corrections differs (stated plainly, not dressed up as live reviewer data). Every demo action runs against a throwaway DB + overlay; persistent state is never mutated.

![The self-improving loop in the V1 demo — submit-a-correction form plus the seeded-reviewer replay over the parked 92-doc corpus (corrections logged → live alias overlay), cached and $0](assets/demo-self-improving-loop.png)

*This screenshot predates the 2026-10-16 numbers: it shows 184 corrections and 1 new alias; the current replay logs 200 and 4 (next paragraph).*

The seeded replay over the committed 92-doc corpus logs **200 corrections** and learns **4 new aliases** (184 and 1 before the 2026-10-16 Tier 3 second read changed which fields park; most missed phrasings are already recognized at seed v1.0.0 — the loop never fabricates a phantom alias, so "new aliases learned" is honestly low). The crucial honest point, carried through from *Two-stage measurement*: this loop's measurable V1 payoff is the Tier-1 hit-rate climb, **not** a falling escalation count (escalation is corpus-flat at ≈0.998 — see above) and **not** an end-to-end F1 gain (the cascade is alias-invariant at 0.853). The loop is real and the mechanism is production-shaped; what V1 honestly claims for it is exactly the Tier-1 curve and no more.

## Progressive alias-table partition (the Tier-1 F1 series)

The Tier-1 F1 series measures the cascade's self-improvement loop: as the alias table grows, Tier 1 extracts more fields correctly (0.242 → 0.340) — though, as *Two-stage measurement* records, escalation does not fall. (PR #79 removed the F1-over-time chart, its `just chart` recipe and its CI drift guard; `just eval` still measures and prints the series, and the README's headline visual is the by-stage ablation + escalation funnel.) It is still the most semantically loaded measurement in the repo.

### Two-stage measurement (Phase 6 finding — supersedes the naive model)

The pre-build mental model was "fewer recognized phrasings → more escalations → lower *end-to-end* F1; more phrasings → higher end-to-end F1." Phase 6 measurement on the committed corpus showed that model is wrong for a cascade with strong escalation tiers, and the honest result is documented here rather than the measurement being engineered to fit the original story.

F1 is measured at **two stages** per alias batch:

- **Tier-1 stage** — the pre-escalation form (Tier 1 extract → route → re-parse). This is the layer the alias table directly governs. Its F1 climbs as alias coverage grows, then asymptotes — the desired shape, and **this is the headline self-improvement series** (measured and printed by `just eval`; not charted since PR #79).
- **Cascade stage** — the end-to-end assembled form after escalation. This F1 is **invariant to alias coverage** (flat at 0.853, identical across all nine alias batches on the 92-doc test split): the Qwen Tier 2/3 tiers are alias-independent and recover whatever the alias layer missed. It is persisted as a *cascade-robustness* statistic, not the headline curve.

The defensible self-improvement claim is therefore: the alias loop measurably improves the Tier-1 hit rate (more on-form labels are recognized, so more fields are correctly populated pre-escalation), and end-to-end accuracy is robust to alias coverage because the cascade compensates. The "Tier-1 hit rate" *is* the Tier-1-stage F1 curve — it is not a separate measurement and there is no separate chart for it. In V2 that same hit-rate gain reads directly as fewer escalations and therefore lower cost-per-document; in V1 it does not, and the honest reason is recorded below. The naive "end-to-end F1 climbs" framing is *not* claimed. Measured on the 92-document patient-stratified `test` split (of the 584-document local corpus), the Tier-1 curve is 0.242 → 0.340 — modest and asymptoting by the second batch — and the cascade stays flat at 0.853. The shape is identical to the original 6-document slice: it is a property of alias coverage and cascade architecture, not of corpus size.

**Escalation rate does not visibly move in V1 (measured, recorded honestly).** A natural follow-up question is whether the alias gain shows up as a *falling escalation rate* — fewer scorable cells crossing the locked 0.85 Tier-1→2 gate as coverage grows. It does not. Measured at the gate across all nine batches on the 92-doc `test` split, the Tier-1→2 escalation rate is **flat at ≈0.998** — essentially every populated cell escalates regardless of alias coverage. The mechanism: alias growth raises Tier-1 *correctness* (the fields land the right value, lifting F1) but Tier 1's layout-parser confidence model keeps those same populated cells below 0.85 anyway. So the alias loop's measurable payoff in V1 is the Tier-1 F1/hit-rate climb, *not* a reduced escalation count — and no escalation-over-time chart is shipped, because a flat 0.998 line plotted as a trend would be exactly the sleight-of-hand visual *Why position-based ordering* rejects. The escalation-as-cost story is real but it is a V2 property (where cloud tiers make escalation expensive); in V1 it is a modeled projection against `src/evals/cost_table.json`, not a measured curve.

### By-stage ablation (Tier 3 as a second reader)

The two-stage measurement above measures Tier-1-stage F1 over alias batches. A separate, orthogonal question is *what each tier contributes* — answered by ablating the cascade tier-by-tier on the same 92-doc `test` split, cached and deterministic. The committed artifact is `docs/assets/f1-by-stage.svg` — two panels (the escalation funnel on top, F1-by-cumulative-stage below), drift-guarded by `src/tests/test_evals_by_stage.py`, regenerated via `just by-stage`.

Three cumulative-stage F1 points:

| Stage | F1 | Construction |
|---|---|---|
| Tier 1 | **0.340** | `harness._tier1_stage_form` (Tier 1 extract → route → re-parse) |
| Tier 1+2 | **0.791** | `process_document(providers=(t1,t2,t2))` — Tier-3 slot duplicated as Tier 2, so the second read is the 7B again and every field agrees with itself; a faithful "Tier 3 disabled" ceiling without touching the orchestrator |
| Tier 1+2+3 | **0.853** | the real `(t1,t2,t3)` cascade |

**Tier 3 is a second reader (since 2026-10-16), and the curve is now monotone: 0.340 → 0.791 → 0.853.** The 7B Tier 2 does the big lift. Tier 3 re-reads every field Tier 2 handled: when the two readings match, the field is accepted at confidence 0.95; when they differ, or only one finds a value, it gets 0.5 and parks for review. On a mismatch the cascade keeps Tier 2's value if it passes its field validator (`src/cascade/validators.py`: a date between 1900 and 2100, a 5- or 9-digit ZIP, a USPS state code, a 10-digit phone) or Tier 3 read nothing, and takes Tier 3's value otherwise, so Tier 3 never overwrites a validated date. Agreement is a real signal on this split: over the 972 scored cells Tier 3 re-read where both tiers produced a value, Tier 2's value is correct on 94.7% of the cells where the two values match exactly (739 of 780) and 38.5% where they differ (74 of 192). (Left out: 23 cells both tiers read as blank, 15 where only one did, and 2 cells Tier 1 settled before Tier 2.)

**What it replaced.** Until 2026-10-16 the curve was not monotone: 0.340 → 0.791 → 0.765, Tier 3 −0.026. The Tier 2/3 confidence was a field-type label — a string scored 1.0, a date 0.5 — so every date fell below the 0.80 gate and went to Tier 3, and the Q4_K_M 32B overwrote correct dates: 29 of the 31 cells Tier 3 changed went correct → wrong, all of them `date_of_birth` or `date_signed`. Escalation followed field type, not evidence.

**Disclosure — read the +0.062 with these caveats.**

- **Most of the gain is digit-suffixed synthetic names.** Synthea appends digits to the names it generates ("Ramona980"). Of the 107 scored first- and last-name cells the 32B turns from wrong to right, 74 are names the 7B read without their digit suffix — for 38 first names the digits went into `middle_name` (first name "Ramona", middle name "980"), and the other 36 (34 last names, 2 first names) just lost them — 23 are first and last names the 7B swapped, 6 are names the 7B left blank and 4 are other misreads. The 32B also turns 4 right names wrong (it drops the digits), so the net is +56 first-name and +47 last-name cells. Real intake forms don't carry digit-suffixed names, so this part of the gain says little about real documents.
- **Part of it is given back on street addresses.** The 32B drops the apartment suffix ("618 Beatty Mission Apt 0" → "618 Beaty Mission"): on `address_street` 38 cells go correct → wrong and 1 goes the other way. That loss is kept as measured — there is no per-field exception beyond the validator rule.
- **The rule was chosen after test-split outcomes had been seen.** Per-field Tier 2 / Tier 3 results on this `test` split were inspected before the rule was chosen, which *Partition discipline* says never happens. The rule is the simplest one that keeps Tier 3 from overwriting a validated date (agreement, the validator guard, the higher tier otherwise), and the kept street loss shows it was not tuned field by field, but these numbers are no longer an untouched held-out measurement. **So the rule was then checked on the 98-doc `dev` split, which nobody had inspected.** One cache-through run (one reading per document, no `EVAL_LIVE`) on 2026-10-18 took about 3.1 hours on the two GPUs, with the 32B partly on CPU. By-stage F1 on dev is 0.331 → 0.782 → 0.861 (test: 0.340 → 0.791 → 0.853): the Tier 3 rule's gain held on documents nobody inspected, +0.079 on dev against +0.062 on test. The dev funnel is Tier 1 2, Tier 1+2 860 and Tier 1+2+3 1051 of 1051 populated cells, with 98 missed and 27 true negatives; Tier 3's value was kept in 191 cells (186 free text, 5 Tier-2 blanks, 0 failed validators). It is one run, with no confidence interval. To repeat it from a checkout of this code: `uv run python -m evals by-stage --split dev --image-dir <render folder>`, where the render folder is the output of `just synthetic-data-render-500` (`--image-dir` can be dropped when that is `src/synthetic_data/output/render/` in the same checkout), with the models up and **without** `EVAL_LIVE`: the providers are cache-first, so each dev document is read live once on its first cache miss, the reading is saved, and the three by-stage points are scored from that same reading. `EVAL_LIVE=true` would skip the cache on every pass, so Tier 1 and Tier 2 would run three times per document and the three points would come from different readings. The run never rewrites the committed chart. It does leave untracked replay files under `src/tests/fixtures/eval-cache/`, which is a tracked folder, so keep them out of commits.

**Known limits.**

- **14 agree-wrong dates.** In 14 `date_of_birth` cells both models read the form's MM/DD date as DD/MM when the day is 12 or less (form `08/06/2008` → both output `2008-06-08`). The readings agree, so the field is accepted at 0.95 and nothing flags it. The ground truth is correct. A fix would be a prompt change ("dates on this form are MM/DD/YYYY"), which invalidates the replay cache.
- **92 never-attempted signatures.** No tier attempts the `signature` field — the Qwen tiers only prompt scalar fields, and Tier 1 never populates `SignatureCapture` — so all 92 cells are false negatives.
- **Live latency.** Tier 3 now reads every field Tier 2 handled instead of only the low-confidence ones. The cached full-field Tier 3 responses average ~117 s per document at Q4_K_M (161 s for the cold first call); the old narrowed call (2–3 fields) was shorter but was never timed on its own. The per-call wall-clock cap was raised from 180 s to 240 s to leave headroom over the cold call. The cap can only fail a call under `EVAL_LIVE=true`: without it, a provider saves a live reading to the replay cache before the cap is checked, so the retry after an over-cap call replays that same reading. V1's cost stays $0.

**Escalation funnel (top panel).** The same full `(t1,t2,t3)` run, counting each of the 1104 scorable cells (92 docs × 12 schema-mapped fields) by the tier whose value the cascade kept: Tier 1 **2**, Tier 2 **820**, Tier 3a **167**, and **115** empty cells. Cumulative cells resolved by tier ≤ N — **2 → 822 → 989** — rises to 100% of the 989 populated cells. Tier 3 reads every Tier-2 field, but its value is kept only where the readings differ, Tier 3 read something, and Tier 2's value doesn't pass a validator: 158 free-text cells (no validator exists for them), 8 cells Tier 2 left blank, and 1 cell whose Tier-2 value failed its validator. Tier 1's funnel share is a deliberately undressed 2 cells — consistent with its 0.340 F1, not inflated. The 115 empty cells are drawn as an explicit remainder above the 100% line, not hidden in the denominator, and split by ground truth: **92 missed** (the form has a value, the cascade produced none — the `signature` field, which no tier attempts) and **23 true negatives** (`reason_for_visit` absent on the form). Before the second read there were 100 missed cells; Tier 3 now fills 8 `first_name` cells Tier 2 left blank.

### Router spot-check (and a documented V1 limitation)

The Stage-1 router was spot-checked on the broad set: all **92/92** CMS-1500 `test`-split docs classify `healthcare`, **91/92 (98.9%)** on distinctive vocabulary alone at Stage 1 (the one fallthrough still resolves correctly via Stage 2), so `N = 1.0` holds with margin on the positive class.

The negative class is weak, and the repo says so. A 50-document local-only DocILE spot-check (business invoices — these *should* route `business`) scores **business_rate 0.46**: 27/50 are false-positively classified `healthcare`. This is a real V1 limitation, not noise. V1's Stage 1 is a deliberately **one-sided healthcare-distinctive-vocabulary gate** — there is no business-distinctive vocabulary and no business gate, so any document whose OCR trips enough healthcare-ish aliases to score ≥ `N` is classified `healthcare` immediately, never reaching the Stage-2 LLM. `N = 1.0` was only ever tuned on healthcare documents. The honest consequence: a business document that trips the healthcare gate is extracted against the wrong schema. A business-distinctive gate (and an `N` re-tuned against a cloud routing table) is exactly the kind of refinement the optional BAA-cloud enhancement would carry; the complete V1 ships the measured limitation rather than hiding it (DocILE derivatives are CC-BY-NC-ND, so this number is reported here but the spot-check itself stays local).

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

The natural batch count is around 8 given the current seed (the longest `aliases` lists have 8 entries). The series plateaus naturally as later batches add fewer marginal aliases, which is the desired shape — F1 should climb sharply early, then asymptote.

### Why position-based ordering

The `aliases` arrays in `src/build_alias_seed.py` are hand-curated in priority order. Position 0 is the canonical/authoritative phrasing for each field, typically the exact label from the source standard (CMS-1500, ACORD 125, USCIS I-9, IRS W-4, etc.). Subsequent positions are variants in rough decreasing real-world frequency — not a measured frequency, but a curator's judgment informed by inspection of dozens of real forms.

For hand-curated taxonomies, canonical priority and real-world frequency are heavily correlated. A field's canonical label is canonical *because* most forms use it. Position-based introduction is therefore a cheap, reproducible proxy for frequency-based introduction without requiring corpus-derived frequency counts.

Three alternatives were considered and rejected.

**Measured frequency from the rendered corpus** would be the most rigorous option but breaks down on synthetic data. Frequency in the Synthea + DocILE corpus is determined by the rendering templates and DocILE's source distribution, not by real-world prevalence on intake forms in the wild. Measuring template diversity and calling it "alias frequency" misrepresents what is being computed. If a CMS-1500 template uses "Patient Name" exclusively, then "Patient's Name" has frequency 0 in the corpus regardless of how common it is on real intake forms — the measurement reflects the template author's choices, not the field's actual prevalence.

**Source-standard priority** (introducing all aliases from records with a `source_standard` value first, then the rest) partitions records but does not graduate aliases. About 50% of seed records have a `source_standard` value, so this approach produces one big batch and one big "everything else" — a step function, not a curve, and the series loses its expressive power.

**Random with fixed seed** is reproducible but tells no story. A chart that climbs because of randomness is not a meaningful self-improvement narrative; it is a sleight-of-hand visual that would invite legitimate skepticism from anyone reading the code.

### Reproducibility

The partition is deterministic given a frozen seed. `alias_table_seed.json`'s top-level `version` field (currently `"1.0.0"`) is recorded in `src/evals/fixtures_manifest.json` and in `src/evals/manifest.json` (the partition-validation CI test asserts the two agree, so a seed bump that isn't propagated fails the build). Tier-1 F1 series runs against a given fixture set are directly comparable only to runs against the same seed version — comparing across seed versions requires re-running the sweep from scratch.

When seed v2.0.0 ships (e.g., from Phase 8 correction feedback adding new aliases, or from a deliberate reordering of existing aliases in light of new evidence), the sweep can be re-run against the new seed for that version's self-improvement series. The v1.0.0 series is not backward-extended. The docs report whichever seed version produced the published numbers.

### Phase 8 implication (shipped)

The Phase 8 correction feedback loop introduces new aliases from reviewer corrections. As shipped, those aliases are **not** written into `alias_table_seed.json` — the seed is frozen at v1.0.0 (it is what the Tier-1 F1 series is measured from), and an in-place edit would silently alter the published numbers. Instead a reviewer correction appends the missed phrasing to a gitignored runtime overlay (`src/data/corrections_aliases.json`, same runtime-state convention as `src/data/v1.db`). The two alias consumers — the Tier 1 layout-to-fields post-processor and the Stage 1 router vocabulary — union the overlay on top of the seed at load time, so live corrections take effect immediately for subsequent extractions.

The progressive-partition sweep explicitly **suppresses** the overlay (`rag.aliases.suppress_overlay`, entered by `evals.alias_partition.active_alias_batch`): the Tier-1 F1 series is the *offline analogue* of the loop and must reflect the v1.0.0 seed alone, so runtime corrections never leak into it. The live loop and the offline analogue therefore share one mechanism (alias growth → Tier 1 + router) while the measured artifact stays honest — this is the "seeded vs. live, identical mechanism" claim made concrete in code.

A genuine reseed — promoting a variant to an earlier position, or folding accumulated overlay corrections into the canonical seed — remains a deliberate **v2.0.0 seed bump**, not an in-place edit to v1.0.0, and the version bump is still the explicit signal to re-run the sweep from scratch (per the `src/build_alias_seed.py` position convention).

### Phase 8 storage + scope deviations (recorded honestly)

Two deliberate deviations from the originally-locked Phase 8 design, documented rather than silently followed:

1. **Embedding storage.** The locked design named `sqlite-vec`. ColQwen 2.5 is a *late-interaction multivector* retriever (one matrix of per-patch token vectors per document, scored by ColBERT MaxSim); `sqlite-vec`'s `vec0` is single-vector KNN and cannot express late interaction. Honoring the literal lock would force a lossy mean-pool that defeats the point of choosing a late-interaction model, while adding a dependency that buys nothing at the V1 corpus scale (6–500 documents) where the exact NumPy MaxSim scan is microseconds. V1 instead packs the multivector into the **reserved `embeddings.vector` BLOB column** (Phase 5 reserved it for exactly this — so Phase 8 is reads/writes, **no schema migration**) and ranks with brute-force MaxSim. V2's pgvector path is unaffected.

2. **Retrieval scope.** The README describes the production mechanism in which the retrieved corrected document is injected as a few-shot example into the Tier 2 + Tier 3 prompts. V1 builds and *surfaces* the late-interaction retrieval (the demo shows each parked document's nearest corrected neighbors) but does **not** wire that injection into the cascade providers: changing the providers' prompts would invalidate the committed replay-cache fixtures and the frozen two-stage F1 artifact, which Phase 8 must leave untouched. The alias-overlay half of the loop is the part that genuinely affects V1 extraction; the few-shot-injection half is V2's deployed pipeline.

### Phase 9 — QLoRA experiment measurement (honest at portfolio scale)

Phase 9 fine-tunes a text post-corrector (Qwen2.5-7B QLoRA — not the plan's nonexistent "Llama 3.1 13B"; not a VL Tier-2 swap, since the `corrections` corpus is text-only) and measures it through the **existing** `score_form` over the **same** `test` partition the cascade-stage F1 uses — the post-correction number is therefore directly comparable to the Phase 6 cascade-stage F1, by construction (the metric is reused, not re-implemented; `finetune.evaluate` reproduces the 0.853 cascade-stage baseline as a regression check).

The training data is split-partitioned by `src/evals/manifest.json`: pairs are built **only** from `train` documents *that carry `seeded_correction` corrections*. The 584-document corpus now populates `train` (394 docs), but the **corrections corpus itself is the deferred local piece** — no reviewer/correction data is committed (the committed eval set is the 92-doc `test` split only), so the experiment still honestly produces **0 non-leaky training pairs** and the eval reports an **identity baseline (delta 0.000)**. This is the leakage guard working as designed, not a gap: training a corrector on the eval documents would be the dishonest version of this experiment. The reproducible pipeline + harness are the V1 deliverable; the real F1 delta requires a GPU-box `FINETUNE_LIVE` run over the deferred local corrections corpus (the separate Phase 9 live-train follow-up). A leakage-safe synthetic format-normalization set (`source: synthetic_format_kind`, no test-document values) lets the QLoRA pipeline run end-to-end on the GPU box and keeps the unit tests meaningful; it is explicitly *pipeline smoke*, never reported as correction signal. Consistent with Phase 6/7/8, this seeded-vs-live distinction is documented plainly rather than dressed up.

## Operational notes

The eval harness is invoked via `just eval` (cached fixtures) or `just eval-live` (live cloud calls, opt-in only). Both commands write results to `src/evals/results/` keyed on a UTC timestamp + git short SHA + seed version. Historical results are gitignored — they regenerate deterministically from the fixture set and seed.

The committed by-stage SVG (`docs/assets/f1-by-stage.svg`) is generated by `just by-stage`, which reads from a fresh cached measurement and emits the F1-by-cumulative-tier panel plus the escalation funnel. A CI drift guard (`test_evals_by_stage.py`) fails the build if the committed SVG diverges from a fresh re-render, so it cannot silently go stale; the chart is deterministic from the fixture set + seed.
