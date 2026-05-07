# Production roadmap

This document collects items deliberately deferred from the portfolio build — decisions that were considered, reasoned about, and parked because the answer depends on factors the build can't yet measure (real traffic, real corrections, real cost telemetry, or real hiring-target signal). It is the canonical home for "considered, not done, will revisit when X" items.

The build itself is a portfolio piece, not a production system. Items below describe what would change if this were deployed at production scale, or what alternatives are worth exploring once Phase 6+ produces empirical evidence to inform the choice.

This file is the long-form treatment. The locked architectural decisions in the project's main instructions document keep the brief "deferred" framing with don't-re-debate signals; this file is where the actual reasoning lives.

## Tier 3a v2 candidate: Qwen3-VL-32B

Qwen3-VL-32B was released October 2025, with both Instruct and Thinking variants. The relevant new capability is mixed-precision GGUF support — different bit widths for language vs vision components within a single quantized model. This directly addresses the finding from the MBQ paper (arxiv 2412.19509) that VLM language tokens are roughly 10× more sensitive to quantization than vision tokens. In a Qwen 2.5 VL 32B GGUF, the LLM and vision encoder are quantized at the same bit width, so aggressive LLM quantization drags down VLM accuracy more than necessary; mixed precision lets you quantize the LLM aggressively while keeping the vision encoder at full precision.

The locked architecture is Qwen 2.5 VL 32B per the main instructions document. Qwen3-VL-32B is a v2 candidate, not a v1 swap. Revisit conditions:

1. Phase 4 dual-quant testing reveals Q8_0 limitations on Qwen 2.5 VL that mixed-precision Qwen3-VL would mitigate.
2. Qwen3-VL community GGUF repositories mature enough to provide stable, well-documented mixed-precision builds (likely 6+ months post-release).
3. Phase 9 QLoRA fine-tuning experiments suggest mixed-precision training would yield meaningful gains.

Estimated cost of switching once revisit conditions are met: ~3–5 hours (download, Modelfile import, sanity test, prompt template adjustments for any API differences). Low-risk migration.

## Tier 2 local model upgrade

Replace AWS Textract Regular Queries with a fine-tuned local InternVL3.5-8B on Synthea + DocILE annotations. Estimated work: ~20 hours. Estimated savings: ~$4.50/1K deployed-demo cost (Textract Tier 2 escalation rate × $15/1K page price).

Defer until hiring-target signal calls for ML-engineering depth over AWS-service-consumption demonstration. The current cascade architecture deliberately uses Textract Regular Queries to demonstrate AWS service integration and HIPAA-mode BAA boundary discipline; replacing it with a local fine-tuned model shifts the portfolio narrative toward "built a custom model" and away from "wired the right managed services."

Revisit only if the target role explicitly emphasizes model-building over infrastructure orchestration.

## Spanish-language alias table extension

Texas healthcare consideration. Most CMS-1500 forms in Texas are issued bilingually (English and Spanish) and reviewer-correction loops in production would surface Spanish field-label variants quickly. The alias table is structured to support a `language` axis cleanly — adding `("first_name", "base", "es"): ["Nombre", "Primer Nombre", ...]` and a corresponding `language` column on the `field_aliases` Postgres table is mechanical extension work.

Defer because the build's eval corpus is English-only (Synthea generates English forms; DocILE is primarily English). Adding Spanish aliases without Spanish documents to extract from would create dead-letter alias entries with no F1 contribution.

Revisit when:
- A bilingual document corpus is available for eval
- Or production deployment hits Texas healthcare traffic and reviewer corrections start flagging unrecognized Spanish labels

Estimated work to extend: ~10 hours (~5 hours adding Spanish aliases for existing fields, ~5 hours extending the router to detect document language and choose vocabulary set).

## Bedrock model import for QLoRA-trained adapters

Phase 9 of the portfolio build is a QLoRA fine-tuning experiment on Llama 3.1 13B over accumulated corrections, run on the local 32 GB combined VRAM during fine-tuning sessions. The resulting adapter is a portfolio artifact demonstrating the full feedback loop, not a productized model.

Open question: in a production deployment, would the trained adapter be imported into Bedrock via Custom Model Import, or kept as a local-only inference artifact? Custom Model Import has cost implications (storage + per-token pricing on imported models) that may or may not justify the operational simplicity over self-hosted inference.

Defer until Phase 9 outcomes are measured. If the adapter produces meaningful F1 gains on real corrections, Bedrock import becomes a real consideration. If the adapter's gains are marginal (likely, given the small correction corpus a portfolio demo can accumulate), document as future work and don't pursue.

## Snowflake destination for production data warehouse

In a production deployment, extracted form data eventually feeds downstream analytics — reporting, audit trails, longitudinal patient records, etc. Snowflake is the obvious destination given Mark's existing experience and its prevalence in healthcare/insurance analytics stacks.

The portfolio build keeps everything in Aurora Serverless v2 to minimize cost ($5–10/month) and architecture complexity. Adding Snowflake would increase monthly cost to $75+ for a small portfolio-scale warehouse, with no portfolio-narrative payoff (the cascade and review UI are the demonstrative artifacts; data warehousing is downstream).

Defer to production deployment with paying customers. Migration path: dual-write from cascade output writer (Aurora + Snowflake), or scheduled CDC from Aurora to Snowflake via AWS DMS or Snowpipe. Either is straightforward when the volume justifies the cost.

## vLLM as production scale-up path

Ollama is the local development inference engine — easy to operate, good for single-user workloads, terrible for high-throughput multi-tenant production. vLLM (or alternatives like TGI, llama.cpp server with continuous batching, SGLang) is the standard scale-up answer for local-hosted inference at production scale.

Defer because the deployed demo runs Tier 1 on Novita and Tier 3a on Together AI — both managed inference providers. There's no self-hosted inference path in the deployed architecture for vLLM to replace. Revisit only if production deployment goes the self-hosted route, which would require rebuilding around vLLM-on-Kubernetes or vLLM-on-EKS — a substantial migration outside the portfolio scope.

## Cost circuit breaker upgrade

Current cost controls: AWS Budgets ($5/day threshold, alerts via SNS), Cost Anomaly Detection (free, anomaly alerts via SNS), per-IP rate-limit Lambda, robots.txt + CloudFront UA blocking. Realistic monthly cost: $10–15.

Upgrade trigger: deployed-demo monthly cost sustained above $25. At that point, revisit:

1. **AWS WAF Bot Control** — currently deferred because per-IP rate limiting catches most abuse patterns at lower cost. WAF Bot Control is ~$10/month base + per-request charges, only worth it if abuse is sophisticated enough to evade simple rate limiting.
2. **Tighter Budget thresholds** — drop daily threshold to $3, or move to hourly budgets for finer-grained alerts.
3. **Aurora Serverless v2 → RDS Proxy + always-on minimum-size instance** — Aurora Serverless v2's minimum 0 ACU + auto-pause is great for portfolio traffic but creates unpredictable spikes during cold-start scaling. Always-on Aurora at the smallest instance size is more expensive baseline but more predictable.

Defer until cost telemetry indicates the problem. Don't pre-engineer.

## WAF Bot Control upgrade trigger condition

Specifically: sustained abuse pattern not caught by per-IP rate limit. Examples that would trigger:

- Distributed bot pattern (1000 IPs each making 5 requests, evading the per-IP rate limit)
- Requests with valid User-Agent strings that pass the CloudFront UA filter but are programmatic
- Account-takeover-style attacks if authentication is added later

Single-IP abuse won't trigger this — the existing Lambda handles that at lower cost. Defer until first observed sustained distributed pattern.

## Self-hosted demo deployment vs cloud

Cloud deployment (AWS) is locked. Revisit only if monthly costs become unmanageable. The reliability and portfolio-signal calculus for a public demo strongly favors managed cloud: a demo that's down when a recruiter visits is worse than the cost difference between cloud and self-hosted.

Self-hosted alternatives considered and rejected:

- Home server with reverse proxy (Cloudflare Tunnel or Tailscale Funnel): zero monthly cost but requires home internet uptime and home machine availability. Recruiter visiting at 2 AM local time gets a dead demo.
- VPS (DigitalOcean, Hetzner, etc.): $20–40/month for adequate-sized droplet, eliminates Aurora's auto-pause cost-saving model, more operational burden than AWS managed services.

Calculus would have to genuinely flip — cost spike to $100+/month sustained, or AWS account compromise — before this is worth re-evaluating.

## Synthetic-handwriting authenticity upgrade

Phase 3 renders intake forms with both typed and handwritten signature variants (~70/30 split) using Google Fonts handwriting fonts + SVG ink-bleed filter. This is rendered handwriting, not real handwriting samples.

If Phase 6 F1 measurement shows the cascade can't generalize from font-rendered handwriting to real handwriting samples (e.g., the cascade hits high F1 on the synthetic eval but visibly fails when shown a real handwritten sample), upgrade options:

1. **Public handwriting datasets** (IAM Handwriting Database, HWDB): freely available academic datasets, would require integration into the rendering pipeline as alternative signature sources.
2. **Small handwriting GAN**: train a small GAN on IAM to generate synthetic but realistic handwriting samples. More work, more authentic-looking output.

Don't pre-engineer. Phase 6 F1 measurement on synthetic data is the baseline; only revisit if generalization to real handwriting becomes a portfolio-narrative concern (e.g., a recruiter questions the authenticity in an interview).

## LossEvent inner-field PHI flagging

The `LossEvent` schema has nested fields (date_of_loss, description, claim_amount, etc.) that may individually contain PHI when used in healthcare-adjacent insurance contexts (e.g., workers' comp claims with medical detail in description). Currently `LossEvent` is treated as a single `data_class` unit at the top level.

For production HIPAA-mode routing, inner-field PHI flagging would let the cascade route specific LossEvent fields through BAA-eligible providers while letting non-PHI inner fields use cost-optimized providers. Requires either: extending `FieldMeta` to support nested overrides, or refactoring `LossEvent` to expose inner fields with their own `data_class` annotations.

Defer to production deployment. Synthetic data doesn't expose this requirement; real workers' comp / health insurance integration would. Estimated work: ~6 hours when the requirement materializes.

## Revisit cadence

Items on this list are not on a schedule. The build's main instructions document is the source of truth for what's locked and shipping; this document is where deferred items live so they don't get lost. Revisit triggers are stated per item — when the trigger fires, that's when the item moves from this document into a real decision.

Phase 10 polish includes a sweep of this document to verify nothing has rotted (e.g., upstream models or services changing in ways that invalidate the deferred reasoning) and to add anything new that emerged during Phases 4–9 build work.
