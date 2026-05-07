# HIPAA architecture

The pipeline is HIPAA-mode-capable: a single `HIPAA_MODE` environment flag flips the routing layer's allowed-providers table so that PHI/PCI fields route to BAA-eligible providers only. This document covers the BAA boundary, the service mapping, and the synthetic-to-real-PHI swap path. The healthcare data shipped with this project is Synthea-generated synthetic data; deploying with real PHI requires BAA execution.

## BAA boundary

The boundary is enforced at two layers:

1. **Routing layer.** `is_baa_required(meta)` returns True for `DataClass.PHI` and `DataClass.PCI`. When `HIPAA_MODE=on`, the routing table for these fields excludes Novita (PaddleOCR), Together AI (Qwen 72B), and any other non-BAA provider. PHI/PCI fields can only land on AWS Bedrock (Claude Sonnet, Nova Lite), AWS Textract, or local providers running on the project's own hardware.
2. **Two-stage classifier.** Stage 1 (vocabulary keyword match) runs locally inside AWS and never sends document content to a non-BAA service. Healthcare documents that match Stage 1 never reach Bedrock for routing purposes — the vocabulary classifier alone routes them. Only the ~20% of inputs that fall through Stage 1 reach Bedrock Nova Lite, which is itself BAA-eligible.

PII routing is `HIPAA_MODE`-dependent and handled in the routing layer rather than at the schema level — the same field flagged `DataClass.PII` may route differently in healthcare-mode vs default mode depending on which `IntakeFormBase` subclass owns it. See `RATIONALE.md` for the orthogonal-axis reasoning behind keeping `data_class` (WHAT) separate from `sensitivity` (HOW).

## BAA-eligible service mapping

> Lands in Phase 5 with the routing-layer implementation. Section will document the explicit mapping: standard-mode providers per tier (Novita Tier 1, Textract Tier 2, Together AI Tier 3a, Bedrock Sonnet Tier 3b, Bedrock Nova Lite Stage 2 router) vs HIPAA-mode providers per tier (Textract replaces Novita at Tier 1; Bedrock Sonnet replaces Together AI at Tier 3a; Tier 2 and Tier 3b unchanged; Stage 2 router unchanged). Cost differential is roughly 6.25× (~$24/1K standard vs ~$150/1K HIPAA).

## Synthetic-to-real-PHI swap path

> Lands in Phase 5 alongside the `HIPAA_MODE` flag implementation. Section will cover: BAA execution checklist with each provider an adopting organization would need (AWS BAA via the AWS Artifact portal; nothing else needed if HIPAA_MODE confines routing to AWS-only). Synthea-generated S3 input bucket swap to a real-PHI bucket with the same prefix structure. Aurora schema migration is no-op (`HealthcareIntakeForm` schema is identical). The CloudWatch and Aurora audit logs satisfy HIPAA's accounting-of-disclosures requirement.

## Healthcare-specific routing rules

> Lands in Phase 5. Section will enumerate the per-DataClass routing predicates and the Stage 1 vocabulary-matching threshold N (starting value 1.0, tuned in Phase 5 spot-check against ~50 hand-classified docs and again in Phase 6 eval).
