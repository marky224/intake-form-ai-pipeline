# intake-form-ai-pipeline

> ✅ **V1 complete** — a self-improving intake-form extraction pipeline that runs end-to-end, locally, on consumer GPUs: measured on a 92-document held-out test split, $0/1K inference, no cloud. *Last updated 2026-05-21.*

## What it is

Forms come in as PDFs or page images — healthcare patient intake (CMS-1500), business documents (invoices, POs) — and validated, typed JSON comes out. A three-tier extraction cascade routes each field to the cheapest model that can handle it confidently and escalates only when confidence is low. Reviewer corrections feed back into an alias table and a ColQwen 2.5 retrieval corpus, so later extractions on similar documents resolve at Tier 1 more often.

V1 runs the entire cascade locally on two GPUs (RTX 4080 + RTX 4060 Ti, 32 GB combined) — no cloud, no deployed URL, $0/1K inference — and is the complete deliverable. The optional V2 enhancement exists for one reason: processing real PHI requires BAA-eligible providers, so V2 would swap the middle and top tiers for BAA-cloud services (Textract Queries, Bedrock) behind the *same* provider Protocol, wire the local tiers to a deployed endpoint, and stand up a public demo. The in-tree Terraform (`infra/terraform/`) and the architecture docs describe that target so it's a credible, scoped enhancement rather than hand-waving — but it is not built and not scheduled.

## The headline result

![Escalation funnel, and F1 by cumulative cascade stage](docs/assets/f1-by-stage.svg)

> **Measured on a 92-document held-out test split**, cached and deterministic ($0): the patient-level-stratified `test` partition of a 584-document local corpus (500 Synthea patients → CMS-1500, rendered 1:1; `train` 394 / `dev` 98 / `test` 92, zero patient leakage). Both panels are the same cascade run.

The interesting part of this project is not that a curve goes up. It's that measurement contradicted the pitch and the repo reports the contradiction.

| Cascade stage | End-to-end F1 | What the tier adds |
|---|---|---|
| Tier 1 — PaddleOCR-VL | 0.340 | layout parse + alias-table match |
| + Tier 2 — Qwen 2.5 VL 7B | **0.794** | the real lift |
| + Tier 3 — Qwen 2.5 VL 32B · Q4_K_M | 0.768 | **−0.026 — regresses** |

**The cascade is not monotone** (lower panel; numbers above). The Qwen 7B Tier 2 does the real lift; adding the Q4_K_M-quantized 32B Tier 3 *regresses* −0.026. Tier 3 only ever re-extracts the fields that escalated (confidence < 0.80), and the locked 0.5-confidence heuristic on coerced scalars forces every date field below that gate even when Tier 2 had it right — so the quantized 32B re-extracts those dates and overwrites correct values (29 of 31 changed fields go correct → wrong, nearly all dates). It ships as-is rather than engineered monotone; a better (unquantized, reasoning) local Tier-3b is the *measured* lever to fix it, not a framing change.

## How it works

```mermaid
flowchart TD
    DOC[Document<br/>PDF / page image] --> R1{Stage 1 router<br/>vocab keyword match<br/>local, ~80%}
    R1 -->|confident| SCHEMA[Pydantic schema<br/>Healthcare / Business]
    R1 -->|ambiguous ~20%| R2[Stage 2 fallback<br/>V1: Qwen 7B local<br/>V2: Bedrock Nova Lite]
    R2 --> SCHEMA
    SCHEMA --> T1[Tier 1 · PaddleOCR-VL<br/>RTX 4060 Ti · layout parser<br/>+ alias-table post-processor]
    T1 -->|field conf < 0.85| T2[Tier 2 · Qwen 2.5 VL 7B<br/>RTX 4080 · prompted VL]
    T1 -->|conf >= 0.85| OUT
    T2 -->|field conf < 0.80| T3[Tier 3 · Qwen 2.5 VL 32B<br/>combined VRAM · Q4_K_M]
    T2 -->|conf >= 0.80| OUT
    T3 --> OUT[Assembled form + per-field provenance]
    OUT --> GATE{form min conf<br/>>= 0.80 gate?}
    GATE -->|yes| APPROVE[Auto-approve]
    GATE -->|no| RQ[Review queue<br/>human-in-the-loop]
    RQ --> CORR[Reviewer correction]
    CORR -->|missed phrasing| OVL[Runtime alias overlay<br/>seed frozen v1.0.0]
    CORR -->|re-embed| COL[ColQwen 2.5 corpus]
    OVL -.unioned at load.-> T1
    OVL -.unioned at load.-> R1

    style T1 fill:#1f3a5f,color:#fff
    style T2 fill:#2a5a8a,color:#fff
    style T3 fill:#3a7ab5,color:#fff
    style RQ fill:#7a3a3a,color:#fff
```

A two-stage router classifies the vertical: a deterministic local vocabulary match handles ~80% of documents with no network hop, and only the ambiguous remainder hits an LLM fallback (local Qwen 7B in V1, Bedrock Nova Lite in V2 — one provider swap, identical routing logic above it). The chosen Pydantic schema seeds the cascade.

An in-process Python orchestrator (no state machine in V1; V2 wraps it in Step Functions) runs the tiers. PaddleOCR-VL is a layout parser whose blocks run through an alias-table-driven layout-to-fields post-processor; fields below the 0.85 threshold escalate to a prompted Qwen 2.5 VL 7B, then to Qwen 2.5 VL 32B below 0.80. Cheap fields settle at Tier 1 in sub-second-per-page; only the fields Tier 1 can't resolve pay GPU time higher up. The assembled form's minimum confidence decides auto-approval versus the human review queue. A reviewer's correction writes back with full provenance, appends any missed label phrasing to a runtime alias overlay, and re-embeds the document into the ColQwen corpus — so the next similar document resolves earlier.

The whole system persists to one SQLite file (extracted fields, eval log, ColQwen multivectors), intentionally Aurora-compatible so the V2 migration is a row-copy rather than a redesign. `docs/architecture-deep-dive.md` covers the orchestrator, persistence model, and the optional enhancement's cloud edge.

## Human in the loop

The review queue is the cascade's HITL surface — where any populated field still below the 0.80 confidence gate after Tier 3 is parked rather than silently accepted. **It is populated by design.** The locked 0.5-confidence heuristic scores every *coerced* scalar (date / int / float / bool) at exactly 0.5 — below the gate — even when the value is extracted correctly, so every form with a date field reaches a human. A non-empty queue is the intended operating point of a cascade built around adjudication, not an error rate to drive to zero; the heuristic and gate are frozen (Phase 5/6) and deliberately not tuned to empty it.

**Park ≠ fix.** Parking a field for review does *not* change its F1 contribution: a wrong-but-parked value still counts as a false positive against ground truth. This is the deliberate guard against the obvious gaming path — if "sent to review" excused a field from scoring, the headline could be inflated by parking anything the cascade was unsure of. The queue is operational triage layered on top of the metric, never a metric adjustment.

Corrections submitted from the queue flow back through `src/rag/aliases.py`: any on-form label phrasing the cascade missed is appended to a runtime overlay (`src/data/corrections_aliases.json`, gitignored), unioned onto the frozen v1.0.0 seed at load time and taking effect for the *next* extraction. The committed seed is never mutated — the progressive-alias-partition sweep calls `rag.aliases.suppress_overlay()` so the published artifact can't silently drift from accumulated live corrections. `docs/eval-methodology.md` "## Human-in-the-loop review queue" has the full methodology framing.

![Per-field correction input panel from the local demo](docs/assets/demo-human-in-the-loop.png)

## What's worth a closer look

**The economics.** V1's cost is $0/1K — local inference on owned hardware, where latency is the meaningful metric, not dollars. The cost-routing argument is what the optional BAA-cloud enhancement would realize: BAA-cloud tiers in the middle and top would take the cascade to ~$9.50/1K, ~32× cheaper than putting every field through a single frontier model (~$300/1K at typical densities). V1 is not a prototype waiting on that number — it is complete, and it produces the measured escalation rates that make the projection credible rather than hypothetical.

**The integrity guardrails.** `alias_table_seed.json` is frozen at v1.0.0 because it's what the F1 chart plots from; live corrections accumulate in a gitignored overlay unioned onto the seed at load time, and the progressive-partition sweep explicitly *suppresses* that overlay so the published chart can never silently drift. The eval harness defaults to cached, deterministic, $0 fixtures with a CI drift-guard on the committed SVG; live provider runs are opt-in behind `EVAL_LIVE`. Phase 9's QLoRA experiment reports a `+0.0000` delta because the manifest leakage guard correctly yields zero non-leaky training pairs at committed scale — the honest result, not a hidden one.

**The consumer-hardware reality.** Tier 3's locked higher-precision plan (a Mungert Q8_0/Q6_K import) turned out infeasible on 31.2 GB of usable VRAM — Q8_0 spills, Q6_K hits an open llama.cpp M-RoPE assert. V1 ships the registry Q4_K_M build, the only configuration that runs, and documents the measured accuracy cost (≈0.77 on a 20-doc cross-vertical check) as a trade-off rather than burying it. `docs/local-development.md` has the full empirical decision.

**HIPAA as a deployment posture.** The V2 provider surface is BAA-eligible by design, so `HIPAA_MODE` is a startup-time assertion plus raised audit verbosity — not a parallel codebase or a provider-routing fork. V1's flag is a no-op (synthetic data only, no cloud routing surface). `docs/hipaa-architecture.md` covers the V2 BAA boundary.

## Running it

```bash
git clone https://github.com/marky224/intake-form-ai-pipeline
cd intake-form-ai-pipeline
just install        # uv sync + pre-commit
just test           # 1077 tests (1058 fast + 19 slow)
just lint           # ruff + ruff-format + black

just demo           # Streamlit on :8501 — real 3-tier cascade over the
                    # 92-doc test split via cached replay. $0, no GPU.
```

![V1 local demo — by-stage ablation + escalation-funnel headline over the cached 92-doc cascade](docs/assets/demo-screenshot.png)

The demo surfaces, per document: the rendered form, routed vertical and final tier, per-tier escalations, the per-field value/confidence/tier table, the populated review queue, and — as the headline analytics — the **by-stage ablation + escalation funnel** (the honest non-monotone F1 `0.340 → 0.794 → 0.768` shown beside the monotone *cells-resolved* coverage rising to 100%, both from the same cached run), with the supporting two-stage F1 numbers below it. For live on-GPU inference, `ollama pull qwen2.5vl:7b qwen2.5vl:32b`, install PaddleOCR-VL per `docs/local-development.md`, then `EVAL_LIVE=true just demo`. No cloud calls, no AWS credentials, either way. `just eval` / `by-stage` run the harness and regenerate the CI-drift-guarded SVG; full task list in the `justfile`.

## Project structure

```
intake-form-ai-pipeline/
├── src/                     # installable editable package (uv sync)
│   ├── intake_schemas.py    # Pydantic v2 schemas (canonical artifact)
│   ├── build_alias_seed.py  # regenerates alias_table_seed.json
│   ├── _paths.py            # repo_root() / src_root() — single path resolver
│   ├── cascade/             # provider Protocol, tier1/2/3, orchestrator, router, store
│   │   └── providers/       # tier1_paddleocr_local, tier2_qwen_7b_local, tier3_qwen_32b_local
│   ├── evals/               # F1/latency metrics, manifest, progressive alias partition, by-stage chart
│   ├── rag/                 # ColQwen 2.5 retrieval + correction feedback loop
│   ├── finetune/            # QLoRA text post-corrector (Phase 9 experiment)
│   ├── demo/                # Streamlit: data.py (testable core) + app.py (view)
│   ├── synthetic_data/      # synthea/, render/ (Playwright CMS-1500), docile/
│   ├── tests/               # 1077 tests + fixtures/ (eval-cache, eval-validation, synthea, docile)
│   └── data/                # SQLite v1.db + ColQwen .npy cache (gitignored runtime)
├── alias_table_seed.json    # 465 aliases / 86 records, frozen v1.0.0 (canonical, repo root)
├── scripts/                 # dev tooling (regen fixtures, dual-quant sanity)
├── infra/                   # terraform/ (optional-enhancement target; bootstrap live) + bicep/ (no-deploy parallel)
└── docs/                    # architecture-deep-dive, hipaa-architecture, eval-methodology,
                             #   production-roadmap, local-development
```

Python 3.11+, Pydantic v2, `uv`, pytest, ruff + black, pre-commit from Phase 1. GitHub Actions runs four required checks on every PR — Lint, Test, Secret scan (gitleaks), IaC scan (checkov against the in-tree Terraform).

## Further reading

- **`docs/architecture-deep-dive.md`** — the shipped V1 orchestrator + persistence; the optional enhancement's cloud edge, five-tier routing, Step Functions layout, sequence diagrams
- **`docs/eval-methodology.md`** — F1 computation, partition/leakage discipline, progressive alias partition, the two-stage finding, the by-stage ablation, Phase 8/9 deviations
- **`docs/hipaa-architecture.md`** — *why the optional cloud enhancement exists*: the BAA boundary, three-layer enforcement, the real-PHI swap path
- **`docs/production-roadmap.md`** — the one optional future enhancement (BAA-cloud for real PHI) + considered-not-done items (Qwen3-VL mixed-precision, Spanish, vLLM scale-up, Bedrock adapter import)
- **`docs/local-development.md`** — GPU/Ollama setup, multi-GPU split, the Tier 3 Q4_K_M trade-off, Synthea + DocILE workflows
- **`RATIONALE.md`** — schema design rationale (DataClass enum, ExtractedField wrapper, SignatureCapture, BoundingBox, confidence aggregation)
