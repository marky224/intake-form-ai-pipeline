# Production roadmap

This document collects items deliberately deferred from the build — decisions that were considered, reasoned about, and parked because the answer depends on factors the build can't yet measure (real traffic, real corrections, real cost telemetry). It is the canonical home for "considered, not done, will revisit when X" items.

**V2 is the largest deferred item.** As of 2026-05-14 the project pivoted to a local-first V1 build (3-tier all-local cascade, no AWS, no deployed demo URL). V2 is the planned cloud rebuild that re-introduces BAA-eligible AWS tiers (Textract Queries + Bedrock Sonnet + Bedrock Nova Lite), stands up the deployed demo at `ai-intake.markandrewmarquez.com` via Lambda + Cloudflare Tunnel, and migrates V1's SQLite to Aurora Serverless v2. V2 entry is a separate decision from V1's build cadence — it lands when V1 is end-to-end coherent (Phases 4-V1 through 7-V1 done) and the portfolio narrative justifies adding the deployed-demo signal. The Hybrid cascade architecture (locked 2026-05-12) is the V2 target; in-tree Terraform at `infra/terraform/` describes the V2 infrastructure verbatim.

This file is the long-form treatment of V2-deferred items and other "considered, not done" decisions. The locked architectural decisions in the project's main instructions document keep the brief "deferred" framing with don't-re-debate signals; this file is where the actual reasoning lives.

## Tier 3 model upgrade candidate: Qwen3-VL-32B

Qwen3-VL-32B was released October 2025, with both Instruct and Thinking variants. The relevant new capability is mixed-precision GGUF support — different bit widths for language vs vision components within a single quantized model. This directly addresses the finding from the MBQ paper (arxiv 2412.19509) that VLM language tokens are roughly 10× more sensitive to quantization than vision tokens. In a Qwen 2.5 VL 32B GGUF, the LLM and vision encoder are quantized at the same bit width, so aggressive LLM quantization drags down VLM accuracy more than necessary; mixed precision lets you quantize the LLM aggressively while keeping the vision encoder at full precision.

The locked architecture is Qwen 2.5 VL 32B (V1's Tier 3; V2's Tier 3a) per the main instructions document. Qwen3-VL-32B is a model-upgrade candidate, not the locked default. Revisit conditions:

1. **Realized (2026-05-17).** Phase 4 found that on 31.2 GB of consumer VRAM the higher-precision Qwen 2.5 VL 32B GGUFs are unusable — Q8_0 spills impractically and the Mungert Q6_K import hits an open llama.cpp M-RoPE `seq_add` assert (#19915) — so V1 Tier 3 ships the **registry Q4_K_M** build: aggressive *uniform* LLM+vision quantization, exactly the case the MBQ finding says costs the most accuracy. A stable mixed-precision Qwen3-VL-32B GGUF (aggressive LLM, full-precision vision) is the natural V2 upgrade to recover that accuracy at similar VRAM. Gated on condition 2.
2. Qwen3-VL community GGUF repositories mature enough to provide stable, well-documented mixed-precision builds (likely 6+ months post-release).
3. Phase 9 QLoRA fine-tuning experiments suggest mixed-precision training would yield meaningful gains.

Estimated cost of switching once revisit conditions are met: ~3–5 hours (download, Modelfile import, sanity test, prompt template adjustments for any API differences). Low-risk migration.

## V2 Tier 2 local model upgrade (was: replace Textract; now: replace Qwen 7B in V1 carrying over to V2)

Pre-V1-pivot framing: replace AWS Textract Regular Queries with a fine-tuned local InternVL3.5-8B on Synthea + DocILE annotations. Estimated work: ~20 hours. Estimated savings: ~$4.50/1K deployed-demo cost (Textract Tier 2 escalation rate × $15/1K page price).

Post-V1-pivot framing: V1's Tier 2 is already a local model (Qwen 2.5 VL 7B). The upgrade question shifts to "fine-tune InternVL3.5-8B on V1's accumulated corrections + DocILE annotations to replace Qwen 7B at the Tier 2-local slot." Same ~20 hour estimate; V1 savings are wall-clock (smaller model = faster Tier 2 escalations) rather than dollars. V2 keeps the Tier 2-local + Tier 2-cloud split — InternVL3.5-8B fine-tuned would still sit at Tier 2-local with Textract Queries above it, so V2's BAA-boundary story is unchanged.

Defer until the project's emphasis shifts toward ML-engineering depth over infrastructure orchestration. The current cascade architecture deliberately uses pre-trained Qwen 2.5 VL at Tier 2-local (V1) and Textract Regular Queries at Tier 2-cloud (V2) to demonstrate AWS service integration and BAA boundary discipline; replacing either with a fine-tuned model shifts the project narrative toward "built a custom model" and away from "wired the right managed services."

Revisit only if requirements explicitly call for model-building over infrastructure orchestration.

## Spanish-language alias table extension

Texas healthcare consideration. Most CMS-1500 forms in Texas are issued bilingually (English and Spanish) and reviewer-correction loops in production would surface Spanish field-label variants quickly. The alias table is structured to support a `language` axis cleanly — adding `("first_name", "base", "es"): ["Nombre", "Primer Nombre", ...]` and a corresponding `language` column on the `field_aliases` Postgres table is mechanical extension work.

Defer because the build's eval corpus is English-only (Synthea generates English forms; DocILE is primarily English). Adding Spanish aliases without Spanish documents to extract from would create dead-letter alias entries with no F1 contribution.

Revisit when:
- A bilingual document corpus is available for eval
- Or production deployment hits Texas healthcare traffic and reviewer corrections start flagging unrecognized Spanish labels

Estimated work to extend: ~10 hours (~5 hours adding Spanish aliases for existing fields, ~5 hours extending the router to detect document language and choose vocabulary set).

## Bedrock model import for QLoRA-trained adapters

Phase 9 of the portfolio build is a QLoRA fine-tuning experiment over accumulated corrections, run on the local GPU. Two corrections to the original plan, made at Phase 9 entry: (1) the base model is **Qwen2.5-7B-Instruct**, not "Llama 3.1 13B" — Llama 3.1 ships only 8B/70B/405B (there is no 13B), and Qwen2.5 keeps model-family coherence with the Qwen 2.5 VL cascade tiers; 4-bit QLoRA on a 7B fits a single 15.6 GB GPU for the short single-field sequences, so combined VRAM is not the constraint at this size. (2) The fine-tuned model is a **text post-corrector** applied *after* the cascade (the `corrections` corpus is text — `field, original, corrected` — with no image, so a text LLM cannot replace a *vision* Tier 2/3); it is scored through the existing harness metric and never alters the frozen cascade, replay-cache fixtures, or the two-stage F1 artifact. The resulting adapter is a portfolio artifact demonstrating the full feedback loop, not a productized model.

The committed corpus is 6 CMS-1500, all `test` split — the manifest is the leakage guard, so the experiment honestly yields **0 non-leaky training pairs** at V1 committed scale and the eval reports an **identity baseline (delta 0.000)**. That is the correct, publishable result: the reproducible pipeline + harness ship, and a real F1 delta requires the deferred local 500-doc corpus populating `train` (a `FINETUNE_LIVE` run on the GPU box). A "QLoRA on a small portfolio-scale correction corpus does not move the needle" finding is itself a credible result, not a failure.

Open question: in a production deployment, would the trained adapter be imported into Bedrock via Custom Model Import, or kept as a local-only inference artifact? Custom Model Import has cost implications (storage + per-token pricing on imported models) that may or may not justify the operational simplicity over self-hosted inference.

Defer until Phase 9 outcomes are measured (V2-gated). The committed identity-baseline result already indicates the gains are marginal at portfolio scale — consistent with the original "likely marginal, given the small correction corpus a portfolio demo can accumulate" expectation — so Bedrock import stays documented-as-future-work and is not pursued in V1.

## Snowflake destination for production data warehouse

In a production deployment, extracted form data eventually feeds downstream analytics — reporting, audit trails, longitudinal patient records, etc. Snowflake is the obvious destination given Mark's existing experience and its prevalence in healthcare/insurance analytics stacks.

The portfolio build keeps everything in Aurora Serverless v2 to minimize cost ($5–10/month) and architecture complexity. Adding Snowflake would increase monthly cost to $75+ for a small portfolio-scale warehouse, with no portfolio-narrative payoff (the cascade and review UI are the demonstrative artifacts; data warehousing is downstream).

Defer to production deployment with paying customers. Migration path: dual-write from cascade output writer (Aurora + Snowflake), or scheduled CDC from Aurora to Snowflake via AWS DMS or Snowpipe. Either is straightforward when the volume justifies the cost.

## vLLM as production scale-up path

Ollama is the local development inference engine — easy to operate, good for single-user workloads, terrible for high-throughput multi-tenant production. vLLM (or alternatives like TGI, llama.cpp server with continuous batching, SGLang) is the standard scale-up answer for local-hosted inference at production scale.

Defer because Ollama is operationally sufficient at portfolio scale in both V1 and V2. V1 runs single-user against the build machine — Ollama is the right tool. V2's deployed demo runs self-hosted Tier 1 + Tier 2-local + Tier 3 — local on the project's combined RTX 4080 + RTX 4060 Ti, reached from AWS Step Functions via a Cloudflare Tunnel bridge to the home GPU — but at portfolio traffic the single-user concurrency model Ollama's serving fits cleanly. vLLM's continuous-batching wins matter when concurrent request load saturates GPU memory; that's not the regime a portfolio demo operates in. A vLLM swap would also require either rebuilding the FastAPI wrapper service on the home GPU around vLLM's serving stack or migrating Tier 3 to vLLM-on-Kubernetes / vLLM-on-EKS — a substantial migration outside the portfolio scope. Revisit if production deployment crosses sustained-concurrent-request thresholds that Ollama can't keep up with (rule of thumb: >5 concurrent active requests per GPU; well above portfolio-scale traffic).

## Cost circuit breaker upgrade

Current cost controls: AWS Budgets ($5/day threshold, alerts via SNS topic), AWS WAF rate-based rule at the CloudFront edge (100 req / 5 min per IP, BLOCK), Cost Anomaly Detection (account-level via the AWS-suggested Default-Services-Subscription delivering anomaly alerts to email at $100/40% threshold), robots.txt + CloudFront UA blocking. Personal-PII subscription endpoints (e.g., the SNS-topic email subscriber) live outside Terraform — `aws_sns_topic_subscription.endpoint` persists to state regardless of any `sensitive = true` flag on the source variable, so personal email gets subscribed manually post-apply via `aws sns subscribe`. Realistic monthly cost: $10–15.

Upgrade trigger: deployed-demo monthly cost sustained above $25. At that point, revisit:

1. **AWS WAF Bot Control** — currently deferred because the per-IP rate-based rule catches most abuse patterns at lower cost. WAF Bot Control is ~$10/month base + per-request charges, only worth it if abuse is sophisticated enough to evade simple rate limiting.
2. **Tighter Budget thresholds** — drop daily threshold to $3, or move to hourly budgets for finer-grained alerts.
3. **Aurora Serverless v2 → RDS Proxy + always-on minimum-size instance** — Aurora Serverless v2's minimum 0 ACU + auto-pause is great for portfolio traffic but creates unpredictable spikes during cold-start scaling. Always-on Aurora at the smallest instance size is more expensive baseline but more predictable.

Defer until cost telemetry indicates the problem. Don't pre-engineer.

## WAF Bot Control upgrade trigger condition

Specifically: sustained abuse pattern not caught by per-IP rate limit. Examples that would trigger:

- Distributed bot pattern (1000 IPs each making 5 requests, evading the per-IP rate limit)
- Requests with valid User-Agent strings that pass the CloudFront UA filter but are programmatic
- Account-takeover-style attacks if authentication is added later

Single-IP abuse won't trigger this — the WAF rate-based rule handles that at lower cost. Defer until first observed sustained distributed pattern.

## Self-hosted demo deployment vs cloud

Cloud deployment (AWS) is locked. Revisit only if monthly costs become unmanageable. The reliability calculus for a public demo strongly favors managed cloud: a demo that's down when a visitor arrives is worse than the cost difference between cloud and self-hosted.

Self-hosted alternatives considered and rejected:

- Home server with reverse proxy (Cloudflare Tunnel or Tailscale Funnel) serving the **entire demo**: zero monthly cost but requires home internet uptime and home machine availability. A visitor at 2 AM local time gets a dead demo. (Distinct from V2's Phase 7 bridge layer, which also uses Cloudflare Tunnel but only for the Tier 1 / Tier 2-local / Tier 3 inference path — the public-facing V2 edge stays on CloudFront so visits during a home-GPU outage land on the wake page and the cascade fails over to `EXTRACTION_MODE=degraded` rather than to a dead site.)
- VPS (DigitalOcean, Hetzner, etc.): $20–40/month for adequate-sized droplet, eliminates Aurora's auto-pause cost-saving model, more operational burden than AWS managed services.

Calculus would have to genuinely flip — cost spike to $100+/month sustained, or AWS account compromise — before this is worth re-evaluating.

## Synthetic-handwriting authenticity upgrade

Phase 3 renders intake forms with both typed and handwritten signature variants (~70/30 split) using Google Fonts handwriting fonts + SVG ink-bleed filter. This is rendered handwriting, not real handwriting samples.

If Phase 6 F1 measurement shows the cascade can't generalize from font-rendered handwriting to real handwriting samples (e.g., the cascade hits high F1 on the synthetic eval but visibly fails when shown a real handwritten sample), upgrade options:

1. **Public handwriting datasets** (IAM Handwriting Database, HWDB): freely available academic datasets, would require integration into the rendering pipeline as alternative signature sources.
2. **Small handwriting GAN**: train a small GAN on IAM to generate synthetic but realistic handwriting samples. More work, more authentic-looking output.

Don't pre-engineer. Phase 6 F1 measurement on synthetic data is the baseline; only revisit if generalization to real handwriting becomes a measurable concern (e.g., the synthetic-data authenticity surfaces as a real-world generalization gap).

## LossEvent inner-field PHI flagging

The `LossEvent` schema has nested fields (date_of_loss, description, claim_amount, etc.) that may individually contain PHI when used in healthcare-adjacent insurance contexts (e.g., workers' comp claims with medical detail in description). Currently `LossEvent` is treated as a single `data_class` unit at the top level.

For production HIPAA-mode routing, inner-field PHI flagging would let the cascade route specific LossEvent fields through BAA-eligible providers while letting non-PHI inner fields use cost-optimized providers. Requires either: extending `FieldMeta` to support nested overrides, or refactoring `LossEvent` to expose inner fields with their own `data_class` annotations.

Defer to production deployment. Synthetic data doesn't expose this requirement; real workers' comp / health insurance integration would. Estimated work: ~6 hours when the requirement materializes.

## Revisit cadence

Items on this list are not on a schedule. The build's main instructions document is the source of truth for what's locked and shipping; this document is where deferred items live so they don't get lost. Revisit triggers are stated per item — when the trigger fires, that's when the item moves from this document into a real decision.

Phase 10 polish includes a sweep of this document to verify nothing has rotted (e.g., upstream models or services changing in ways that invalidate the deferred reasoning) and to add anything new that emerged during Phases 4–9 build work.
