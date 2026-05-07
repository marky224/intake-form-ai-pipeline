# Project: intake-form-ai-pipeline

## Project context

Owner: Mark Marquez. Targeting AI Infrastructure / ML Platform / AI Data Pipeline Engineer roles, $130K-$200K, remote-eligible US. Project is a portfolio piece designed to demonstrate production-grade AI infrastructure thinking, not coursework.

The project is a self-improving intake-form processing pipeline with three-tier extraction cascade, BAA-aware routing, real-time eval, and demonstrated F1-over-time improvement. Public-on-day-1 GitHub repo. Live demo at ai-intake.markandrewmarquez.com lands when Phase 7 ships.

Build budget: 50-70 hours over 6-8 weekends. Build started May 2, 2026.

## Hardware and environment

- AMD Ryzen 7 5800X3D, RTX 4080 (16 GB) + RTX 4060 Ti (16 GB), both connected to motherboard PCIe slots (NOT eGPU/OCuLink)
- Combined 32 GB VRAM, no NVLink, PCIe-only inter-GPU communication
- Local OS: Linux (Ubuntu, hostname openclaw-pc), bash for local commands
- AWS work: separate Windows machine, PowerShell for AWS CLI commands

When sharing CLI commands: bash for local Linux operations, PowerShell for AWS CLI from Windows. Never assume a unified shell.

## How to work with Mark

- Be direct. Skip preamble. If something is broken or wrong, say so.
- When sharing code or errors, debug directly without unnecessary context-setting.
- Push back when Mark is about to make a bad decision.
- Don't apologize unnecessarily or perform contrition. Acknowledge mistakes briefly and move on.
- Don't re-litigate decisions already locked below unless Mark explicitly asks to revisit them.
- Search the web before answering questions about current AWS pricing, model capabilities, or vendor pricing — these change frequently.
- When something is locked in this document, treat it as locked. Surface new information that might change a decision, but don't re-debate without prompting.
- Mark prefers detail and reasoning over brevity. When recommending an option, explain the tradeoffs and why other options were rejected.
- Mark wants to learn during the build, not just receive answers. Explain reasoning when it would be educational.
- When context grows large, start a fresh conversation. Project files and these instructions are the source of truth; don't try to preserve continuity across threads.

## Code style preferences

- Python 3.11+, type hints everywhere, Pydantic v2 for schemas
- uv for package management (faster than pip, modern, gaining adoption among Python infrastructure projects)
- Functional over class-heavy where reasonable (module-level functions preferred over methods when stateless and called from multiple contexts)
- Pydantic models use `ConfigDict(extra="forbid")` to catch typos at boundaries
- Black formatting, ruff linting, pytest for testing
- Terraform for IaC (with Bicep parallel for Azure branch — no Azure deployment)
- Pre-commit hooks (ruff + black + tests) configured from Phase 1
- GitHub Actions on every PR (Terraform validate + pytest + ruff + black)
- Branch protection on main (require passing CI before merge)

## Locked architectural decisions

### Project structure

- Single monorepo, public-on-day-1
- Project name: intake-form-ai-pipeline (matches local directory)
- Top-level justfile (`just demo`, `just eval`, `just deploy`, `just synthetic-data`, etc.)
- Subdomain: ai-intake.markandrewmarquez.com (Route 53 hosted zone exists at Z04568022MZ21HXK15I1D); URL is reserved and DNS configured from Phase 1, but the demo behind it lands in Phase 7
- GitHub repo: github.com/marky224/intake-form-ai-pipeline (public day 1, README skeleton from commit 1)

### Vertical structure (two verticals)

- **Healthcare:** Synthea-generated patients rendered onto CMS-1500-inspired templates via HTML+Playwright (local rendering, ~6 hrs build, $0 runtime). Synthetic data includes both typed and handwritten signature variants (~70/30 split) rendered programmatically via Google Fonts handwriting fonts + SVG ink-bleed filter, to give the cascade real diversity to handle.
- **Business Documents:** DocILE dataset (free academic license, register at docile.rossum.ai before Phase 4); use DocILE's 55-field taxonomy directly rather than designing a separate schema

### Extraction cascade

Three tiers (with Tier 3 splitting into 3a/3b internally):

- **Tier 1 (always-on):** PaddleOCR-VL — local on RTX 4060 Ti for dev/eval, Novita for production demo
- **Tier 2 (cloud only):** AWS Textract Regular Queries (`AnalyzeDocument` API with `QUERIES` feature, $15/1K pages, zero-shot, no adapter training required)
- **Tier 3a:** Qwen 2.5 VL 32B
  - Local (dev/eval): Q8_0 quantization imported from `Mungert/Qwen2.5-VL-32B-Instruct-GGUF` via custom Ollama Modelfile (Ollama registry only ships Q4_K_M, Q8_0, FP16). Model size 36.3 GB on 32 GB combined VRAM, ~4 GB CPU spill expected (validation deferred to Phase 4 dual-quant sanity test). Q4_K_M (~20.1 GB, validated May 5, 2026 — loads with ~15 GB on each card, 7% CPU spill) retained as fast-iteration fallback. Vision capability assumed working pending Phase 4 verification.
  - Production demo: Together AI Qwen 2.5 VL 72B
  - **Quantization choice and contingency tree** (decided pre-Phase-4 to avoid side-quest debugging):
    - Default: Q8_0 from Mungert HuggingFace via Modelfile import. Vision encoder stays at FP16 in GGUF (only LLM weights are quantized), so quantization concerns are about LLM reasoning over vision features, not feature extraction itself.
    - Phase 4 dual-quant sanity test: run 20-doc validation against both Q8_0 and Q6_K (~28.7 GB, no CPU spill). If F1 gap is within ±0.02, demote to Q6_K (saves the spill, no accuracy cost). If F1 gap is larger, keep Q8_0 locked. Decision made once at Phase 4 start, not revisited.
    - F1 ≥ 0.80 absolute on whichever quant wins the sanity test: ship as locked, local Tier 3a stays.
    - F1 0.65–0.80: document the gap publicly, ship anyway. Portfolio story becomes "consumer hardware runs this with measurable trade-off."
    - F1 < 0.65, or hallucinations on simple forms: drop to InternVL3.5-8B local for Tier 3a dev iteration (fits 16 GB on the 4080 alone). Lower F1 ceiling but local cost stays $0.
    - InternVL3.5-8B also broken: Tier 3a becomes cloud-only (Together AI Qwen 72B for everything including dev iteration). Kills the local-first claim for Tier 3a; cascade still works. Document as known limitation in production-roadmap.md.
- **Tier 3b (cloud only):** Bedrock Claude Sonnet 4.6 — hardware can't fit Tier-3b-capable open-weights models
- **Router:** Two-stage classifier. Stage 1 vocabulary keyword match locally; Stage 2 Bedrock Nova Lite fallback for ambiguous documents. Total cost ~$0.05 per 1,000 documents. Locked specifics:
  - **Vocabulary inclusion rule.** Built at runtime from `alias_table_seed.json`. An alias qualifies as healthcare-distinctive vocabulary if it appears in `vertical="healthcare"` records AND does not appear in `vertical="base"`, `"insurance"`, or `"hr"` records. Examples that qualify: "MRN", "Patient ID", "HIPAA Acknowledgment", "Allergies", "Subscriber ID". Examples that don't (cross-vertical): "First Name", "Date of Birth", "Phone".
  - **Match strategy.** Substring match per line of OCR text, case-insensitive (normalize to uppercase before matching). Weighted by alias specificity (inverse frequency across the seed file): a healthcare-distinctive term that appears once in the seed weighs more than one that appears five times.
  - **Stage 1 classification threshold.** Healthcare classification when accumulated weighted-match score ≥ N. Starting value 1.0 (one strong distinctive term is enough). Phase 5 spot-checks N against ~50 hand-classified documents (~25 healthcare, ~25 business): hand-label each doc, run Stage 1 vocabulary classifier against same set, compute confusion matrix manually. Adjust N if obviously broken (e.g., misclassifies >20% of healthcare). Phase 6 eval harness does the precision tuning once F1 measurement infrastructure exists.
  - **Generation timing.** Runtime, not build-time. Vocabulary is built once at router startup (~50ms), then reused across all documents in that Lambda warm cycle. No cache file to invalidate; schema changes propagate automatically.
  - **Schema-version coupling.** CI tests validate that representative healthcare-distinctive terms (MRN, HIPAA Acknowledgment) make the vocabulary list and representative shared terms (First Name, Phone) do not. When `alias_table_seed.json` regenerates, these tests catch any unintentional vocabulary drift.
  - **Stage 2 (Bedrock Nova Lite) trigger.** When Stage 1 score < N. Roughly 20% of documents fall through. Nova Lite is BAA-eligible, so PHI exposure is acceptable in this stage. Cost ~$0.05/1K documents at 20% fallback rate.
  - **PHI containment guarantee.** Stage 1 runs locally in the routing Lambda; documents never leave AWS until Stage 1 makes its determination. Healthcare documents that match Stage 1 never reach Bedrock for routing — the vocabulary classifier alone routes them. This is the BAA boundary's first line.

Confidence thresholds for cascade escalation: per-field at 0.85 / 0.80 / 0.75 (Tier 1→2/3a, Tier 2→3a, Tier 3a→3b). Starting values; tuned via Phase 6 eval sweeps.

Tier failure handling: hybrid retry-then-escalate. 4xx fails immediately. 5xx/timeout retries 3x with exponential backoff (1s/2s/4s with jitter) then escalates. 429 respects Retry-After header. Schema validation failure retries once with stricter prompt then escalates. Tier 3b exhaustion fails to review queue with full error history.

### Local model coexistence

Mark maintains separate Gemma 4 31B workflow on the same hardware (general-purpose agent, 256K context, accepts CPU spill for quality). Coexistence pattern:

- `OLLAMA_KEEP_ALIVE=10m` (Mark previously had "forever," now changed)
- Models load on demand, unload after idle
- Cascade orchestrator handles its own model loading/unloading
- Dispatcher pattern: when cascade needs to run, Gemma unloads and Qwen loads (~60-90 second swap accepted)
- Per-request keep-alive override in cascade code (`keep_alive: "1h"`) pins models during eval batches
- Tier-batched eval pattern: process all Tier 1 docs first, unload, load Tier 3a, process escalations

### Schema design (locked)

Pydantic v2 schemas with these constructs (defined in `intake_schemas.py` within this Project's files):

- `ExtractedField[T]` generic wrapper carries: value, confidence, tier_used, escalation_history, raw_text, bounding_box
- `BoundingBox` model: frozen, page_number 1-indexed, x1/y1/x2/y2 floats
- `PageMetadata` model: per-page tracking with page_status enum ("extracted", "skipped_blank", "failed", "manual_only"), rotation_corrected, page_image_uri, page_confidence, tier_used_for_page
- `DataClass` enum: PUBLIC, PII, PHI, PCI (replaces is_pii/is_phi booleans)
- `Sensitivity` retained as orthogonal axis (low/medium/high) — data_class says WHAT, sensitivity says HOW
- `is_baa_required(meta)` helper returns True for PHI and PCI
- `compute_form_confidence(form, recurse=True)` module-level function returns dict with min/mean/field_count/blank_count/unattempted_count
- "Populated" definition for confidence calculation: `value is not None`. Confidently-blank fields (value=None, tier_used set) excluded from confidence calc, counted separately for reviewer UI
- `SignatureCapture` sub-model: `signature: ExtractedField[SignatureCapture]` on `IntakeFormBase` (replaces previous `signature_present: ExtractedField[bool]`). Sub-model fields:
  - `present: bool`
  - `appears_handwritten: Optional[bool]` (None when cascade can't determine)
  - `appears_typed: Optional[bool]` (None when cascade can't determine)
  - Both can be True simultaneously for genuinely ambiguous cases (e.g., typed name in script font that resembles handwriting); routing layer treats both-True as a review-queue trigger.

The vertical-specific Insurance and HR schema classes remain in code as future-extensibility examples per the locked architectural decision (DocILE replaces both as the active business-documents vertical).

The SignatureCapture refactor was applied May 5, 2026 against `intake_schemas.py`, `test_intake_schemas.py`, `alias_table_seed.json`, `build_alias_seed.py`, and `RATIONALE.md`. All 40 tests pass; ruff/black clean. Phase 4 cascade work proceeds against this updated schema; Tier 1/3a prompt templates will extract the handwritten/typed signals into the SignatureCapture sub-model.

### HIPAA / BAA routing

Single config flag (`HIPAA_MODE` env var) switches routing rules globally:

- Default (off): full cost-optimized cascade for all documents (synthetic data, no real PHI)
- HIPAA mode (on): PHI/PCI fields route to BAA-eligible providers only (Bedrock + Textract). Non-BAA providers (Novita, Together AI, Deepinfra) blocked for these fields.
- `is_baa_required(meta)` returns True for PHI and PCI; PII routing is HIPAA-mode-dependent and handled in routing layer
- `hipaa-architecture.md` documents the production swap point and the BAA-eligible service mapping

### Infrastructure

- AWS-primary, Azure parallel branch (Bicep, no deployment)
- Single Aurora Serverless v2 cluster with three schemas: `demo`, `eval`, `staging`
- Aurora min 0 ACU, auto-pause after 5 min, ~$5-10/month total
- pgvector for RAG embeddings (over Bedrock KB)
- Wake-on-request landing page (Aurora cold start 30-90s)
- DynamoDB single-item lock prevents race conditions on simultaneous wakes (~$0/month)
- ColQwen 2.5 (NOT ColPali) for retrieval embeddings
- GitHub Actions OIDC federation, no long-lived AWS keys
- Cost circuit breaker: AWS Budgets ($5 daily threshold) + per-IP rate limit Lambda + Cost Anomaly Detection (free, anomaly alerts via SNS)
- Bot blocking: robots.txt + CloudFront UA blocking + per-IP rate limit; AWS WAF Bot Control deferred until needed
- Realistic monthly cost: $10-15

### Eval harness

- Cached fixtures default, opt-in live mode via `EVAL_LIVE=true` flag
- Saves $125-275 in build budget vs naive approach
- evals/fixtures/ directory with versioned per-tier responses per fixture document
- evals/fixtures_manifest.json records version, generated_at, model versions used
- Three metrics tracked, all computable locally with $0 cloud cost:
  - F1 (headline metric, plotted over batches)
  - Cost-per-document (running average, demonstrates self-improvement reduces cost)
  - Latency p50/p99 (same dynamic — better cache hits = faster cascade)
- Healthcare F1 chart is the headline (controlled experiment with synthetic data)
- Business documents F1 is secondary "real-world validation"
- evals/manifest.json explicitly partitions documents into train/dev/test
- Synthea patients partitioned at patient level (not document level) to prevent train/test leakage
- **Self-improvement mechanism for F1-over-time chart: progressive alias-table introduction.** The full `alias_table_seed.json` (~465 aliases across 86 records) is partitioned into batches by per-record alias position. Batch N includes positions 0 through N–1 of every record's `aliases` array — Batch 1 starts with the canonical phrasing only (~86 aliases, ~18% of total), Batch 2 adds the first variant per field (~37%), and so on. Records with fewer aliases than the current batch index contribute their full list and no further. The natural batch count is ~8 given the current seed (max ~8 aliases per record). Eval runs at each batch's alias set; F1 plotted over batches. The chart measures real cascade behavior as the alias table grows: fewer recognized phrasings → more escalations → lower F1; more recognized phrasings → more Tier 1 hits → higher F1. Mechanism is identical to production reviewer-correction loop; corrections are seeded from the schema-design alias work in canonical-priority order rather than reviewer-generated, which is documented honestly rather than presented as live reviewer data. See `docs/eval-methodology.md` for the partition strategy.

### Demo UX

- Wake-on-request landing page
- Landing page shows during 30-90s Aurora wake: project pitch (1 paragraph), F1-over-time static SVG chart, "View source" + "Read architecture" links, live progress indicator
- Once Aurora wakes, automatic redirect (not button-prompted) to React review UI
- Three pre-loaded demo documents per session: one healthcare (Synthea, low-confidence requiring review), one business document (DocILE invoice, high-confidence auto-extracted), one healthcare (fully-escalated through all tiers)
- Session reset on each visit via cookie/localStorage UUID; reset_demo Lambda truncates demo tables keyed by session_id
- Bulk batch correction UX with auto-save to localStorage as the primary throughput-driven workflow
- Single-document review available as secondary view (drill into a document from the batch view) for cases where field-level context across the form matters
- "Submit corrections" explicit save button after batch review
- Persistent header with live cost telemetry: "This session has cost $0.0X in inference" (real number from database)
- Recruiter who has 30 seconds gets value from landing page alone; 5 minutes clicks demo; 30 minutes reads supplementary docs

### Documentation strategy

- Comprehensive README is the entry point (~3,000-4,000 words)
- README includes inline architectural decisions as flowing prose (not formal ADRs)
- Skeleton README created at start of Phase 1 (project name, brief description, "in development" banner) — incrementally updated through build
- 5 supplementary docs for depth, linked from README:
  - architecture-deep-dive.md (detailed diagrams, sequence flows, data model, four-tier 3a/3b distinction)
  - hipaa-architecture.md (BAA boundary, healthcare routing rules, synthetic-to-PHI swap)
  - eval-methodology.md (F1 computation, fixture strategy, partition discipline, leakage mitigations, progressive alias-table partition)
  - production-roadmap.md (what changes at production scale, deferred questions, per-phase build budget breakdown, Qwen3-VL-32B v2 candidate)
  - local-development.md (GPU setup, Ollama configuration, Q8_0 import workflow, Synthea workflow)

### Demo flow on multi-page forms

- Form-level canonical schema with per-page metadata
- Confidence aggregated as min across populated fields
- Empty/blank pages don't drag down form-level confidence
- Page status drives routing: blank/failed pages don't escalate looking for nonexistent fields

## Phase plan

Phase budget: 50-70 hours total, ~5-7 hours per phase.

1. **Pre-build:** DocILE registration, Ollama validation (model loading already done May 5), AWS hello-world cold-start test, Novita and Together AI API access validation, single-GPU and multi-GPU smoke tests
2. **Terraform foundation:** VPC, S3, single Aurora 3 schemas, IAM roles, OIDC for GitHub Actions, CI/CD setup, robots.txt + CloudFront UA blocking + per-IP rate limit Lambda, Budgets + Cost Anomaly Detection, README skeleton committed
3. **Synthea + rendering pipeline + DocILE ingestion:** Synthea Docker setup, HTML+Playwright templates for CMS-1500-inspired forms (including programmatic typed/handwritten signature rendering via Google Fonts + SVG filter, ~70/30 split), render 500 healthcare docs to S3, ingest DocILE annotated set
4. **Schema implementation + cascade providers:** BusinessDocumentForm schema using DocILE taxonomy (the SignatureCapture refactor on `IntakeFormBase` has already been applied), Phase 4 dual-quant Q8_0 vs Q6_K sanity test for Tier 3a local, then all six provider implementations (tier1_paddleocr_local.py, tier1_paddleocr_novita.py, tier2_textract.py, tier3a_qwen_local.py, tier3a_qwen_together.py, tier3b_claude_bedrock.py), EXTRACTION_MODE env var routing. **Validate each provider against ~10 documents before expanding to full fixture set** (build discipline; see Cost model section).
5. **Step Functions orchestration + DataClass enforcement + HIPAA mode + tier failure handling:** state machine with Retry/Catch per state, two-stage router with Phase 5 hand-classified spot-check on threshold N, HIPAA_MODE flag implementation
6. **Eval harness:** cached fixtures + live mode, F1/cost/latency metrics, multi-metric dashboard, manifest discipline, partition validation, progressive alias-table partition implementation. **Initial fixture generation against ~50 documents, sanity-check F1, then expand to full eval test partition** (build discipline).
7. **Review UI + reset_demo Lambda + wake-on-request landing page:** React with bulk batch correction (primary) + single-document review (secondary drill-down view) + localStorage auto-save, session UUID, F1 chart static SVG generation, demo URL goes live
8. **RAG layer:** ColQwen 2.5 retrieval, alias table population from seed, correction feedback loop, pgvector embedding pipeline
9. **QLoRA fine-tuning experiment:** Llama 3.1 13B fine-tuning on accumulated corrections (uses combined VRAM during fine-tuning runs only)
10. **Polish:** README rewrite, supplementary docs written, demo verification, no TODOs in main, end-to-end demo dry runs

### Definition of done

- Phases 1-9: standard (tests pass, ruff/black clean, working artifact, README updated incrementally as features land)
- Phase 10: strict (full README rewrite for production quality, supplementary docs written, demo verification, no TODOs in main)

## Cost model

**Build budget (one-time, Phase 4-7 dev iteration, with cached fixtures + small-testing-first discipline + Textract Regular Queries):** ~$65-110 total inference cost. Per-phase breakdown documented in `docs/production-roadmap.md` if needed for planning.

**Ongoing run cost (Phase 7+):** $10-15/month deployed demo + $0 local development.

**Per-1,000-document inference cost (deployed demo):**
- Standard cascade: ~$24/1K (PaddleOCR Novita Tier 1 + Textract Regular Queries Tier 2 at ~30% escalation + Together AI Qwen 72B Tier 3a at ~10% escalation + Bedrock Sonnet Tier 3b at ~3% escalation + Bedrock Nova Lite router fallback)
- HIPAA-mode cascade: ~$150/1K (Textract replaces Novita; Bedrock Sonnet replaces Together AI Qwen; same router)
- ~6.25x cost multiplier for healthcare-mode documents
- Escalation rates above are industry-prior estimates for cascade extraction on form-like documents; Phase 6 eval harness will measure actual rates on this corpus and update the cost figures accordingly.

The 13× cost ratio over single-model Bedrock Sonnet is the cascade's engineering payoff — single-model at Sonnet pricing would cost ~$300-350/1K at typical field densities.

### Build discipline (small-testing-first)

To prevent the build budget from ballooning if initial provider implementations have bugs that burn calls without producing useful fixtures:

- **Phase 4 provider validation**: Each new provider gets validated against **~10 documents** before expanding to the full fixture set. Catches structural bugs before they burn budget across hundreds of fixture-generation calls.
- **Phase 4 dual-quant sanity test**: 20-doc validation pitting Q8_0 against Q6_K for Tier 3a local before locking the quant choice. ~30-45 minutes of additional Phase 4 time.
- **Phase 6 initial fixture generation**: Run against **~50 documents first**, F1 numbers reviewed for sanity, then expand to the full eval test partition (~500-1000 docs).
- **Cached fixtures default everywhere**: live calls require explicit `EVAL_LIVE=true` env var or equivalent provider-implementation flag. Default behavior is replay-from-cache.
- **CI/CD never makes live provider calls**: GitHub Actions runs use cached fixtures only. Live calls are local-dev or explicit Phase 6 fixture-generation runs.

These practices collectively keep the build budget at the $65-110 range. Without them, naive iteration on the same architecture would cost $400-700.

## Multi-thread coordination

Single-thread workflow with documentation-as-source-of-truth. When context grows large, start a fresh conversation; project files and these instructions are the canonical state. See "Files to reference" below.

## Open questions deferred

These were considered and explicitly deferred. Don't re-debate; revisit only if Mark asks or if the relevant phase begins. Long-form treatment with full architectural reasoning lives in `docs/production-roadmap.md`:

- LossEvent inner-field PHI flagging (third-party PII handling item)
- Spanish-language alias table extension (Texas healthcare consideration; mechanical extension when needed)
- Bedrock model import for QLoRA-trained adapters vs document as future work (depends on Phase 9 outcomes)
- Snowflake destination in production roadmap (matches Mark's existing experience)
- WAF Bot Control upgrade trigger condition (sustained abuse not caught by per-IP rate limit)
- vLLM as production scale-up path
- Tier 2 local model upgrade — fine-tune InternVL3.5-8B on Synthea+DocILE annotations to replace Textract Tier 2. ~20 hours work, saves ~$4.50/1K deployed-demo cost. Revisit only if hiring-target signal calls for ML-engineering-depth over AWS-service-consumption.
- Cost circuit breaker upgrade — if deployed-demo monthly cost sustained above $25, revisit AWS WAF Bot Control + tighter Budget thresholds + consider migrating away from Aurora Serverless v2 to RDS Proxy + always-on minimum-size instance for predictable billing. Defer until cost telemetry indicates the problem.
- Self-hosted demo deployment vs cloud — cloud deployment is locked. Revisit only if monthly costs become unmanageable; the reliability and portfolio-signal calculus would have to genuinely flip before this is worth re-evaluating.
- Synthetic-handwriting authenticity upgrade — if Phase 6 F1 measurement shows the cascade can't generalize from font-rendered handwriting to real handwriting samples, upgrade path is (a) public handwriting datasets like IAM, or (b) a small handwriting GAN. Don't pre-engineer.
- Tier 3a v2 candidate: Qwen3-VL-32B (released October 2025) ships with mixed-precision GGUF support (different bit widths for language vs vision components). Directly addresses the MBQ paper's finding that VLM language tokens are ~10× more sensitive to quantization than vision tokens. Locked architecture is Qwen 2.5 VL; revisit if Phase 4 dual-quant testing reveals Q8_0 limitations or if community Qwen3-VL GGUF builds mature.

## Files to reference

The Project files area contains canonical artifacts and is the source of truth for any locked decision:

- intake_schemas.py (Pydantic v2 schemas with all locked design, including SignatureCapture sub-model)
- test_intake_schemas.py (40 tests, all passing, including aliases[0] sort-stability check)
- build_alias_seed.py (regenerator for alias_table_seed.json; documents alias position priority convention)
- alias_table_seed.json (86 records, 465 aliases)
- RATIONALE.md (schema design rationale, including SignatureCapture sub-model decisions in Section 12 and signature-rendering parameters in Section 1)

The public README and supplementary docs (`docs/architecture-deep-dive.md`, `docs/hipaa-architecture.md`, `docs/eval-methodology.md`, `docs/production-roadmap.md`, `docs/local-development.md`) live in the GitHub repo, not the Project files area. The README is the canonical entry point for architectural questions; this instructions document holds the locked decisions that drive those docs.

If Mark asks about a locked decision and the answer might have shifted, search this document first, then reference Project files, then search the web for current vendor pricing or model capabilities. Don't ask Mark to re-explain context that exists in these files.

## Defaults for unfamiliar territory

When a question arises that isn't covered above:

- Default to opinionated, cost-conscious recommendations matching this document's existing posture
- Push back if Mark seems to be making a decision that conflicts with the locked architecture
- Surface tradeoffs explicitly when recommending an option
- Search the web for current vendor pricing, model capabilities, or AWS service pricing before quoting numbers
- If a decision needs to be made and it's small (formatting, naming convention, file layout), make it and move on
- If a decision needs to be made and it's architectural, surface the tradeoffs and ask Mark to confirm
