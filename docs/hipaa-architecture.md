# HIPAA architecture

The pipeline is HIPAA-mode-capable: a single `HIPAA_MODE` environment flag governs a deployment-posture assertion (audit-log verbosity, startup-time check that every routed provider is BAA-eligible). Under the Hybrid architecture (locked 2026-05-12), the HIPAA-safe deployment posture is **on-prem inside the operator's HIPAA-compliant infrastructure** (hospital data center, HITRUST-certified hosting, or any environment meeting HIPAA Security Rule controls). Local Tier 1 + Tier 3a inference runs on operator-controlled GPUs; cloud Tier 2 (Textract), Tier 3b (Bedrock Sonnet), and Router Stage 2 (Bedrock Nova Lite) run as AWS BAA-eligible services under the operator's own BAA with AWS. Same cascade as standard mode, same `~$9.50 per 1,000 documents` cost — what changes is the *deployment environment*, not the *provider mix*.

The portfolio project's deployed demo at `ai-intake.markandrewmarquez.com` is documented as a non-HIPAA deployment: the data shipped with this project is Synthea-generated synthetic, so the home-GPU host's compliance posture is moot for the public demo. HIPAA mode is a documented capability of the architecture intended for real-customer on-prem deployments, not a runtime mode the public demo exercises against PHI.

## BAA boundary

The boundary is enforced at three layers:

1. **Deployment environment.** Local Tier 1 (PaddleOCR-VL) and Tier 3a (Qwen 2.5 VL 32B) are HIPAA-safe **not because the models are open-weights and the inference is free**, but because the *environment running them* meets the HIPAA Security Rule's technical, physical, and administrative safeguards. The same Qwen 2.5 VL weights running on a home PC (no access logging on physical entry, no formal breach response plan, no workforce HIPAA training) would not be HIPAA-safe. Model license is orthogonal to HIPAA compliance; deployment infrastructure controls are the gating factor. Real-customer deployments target hospital data centers, HITRUST-certified hosting, or equivalent on-prem environments.
2. **Routing layer.** `is_baa_required(meta)` returns True for `DataClass.PHI` and `DataClass.PCI`. Under the Hybrid architecture, the project's entire provider surface is BAA-eligible by design — local providers (HIPAA-safe via the operator's environment) plus AWS BAA-eligible cloud services (Textract, Bedrock Sonnet, Bedrock Nova Lite). The routing layer's `HIPAA_MODE=on` behavior is a startup-time assertion: enumerate every provider in the routing table and reject any non-BAA-eligible entry. In the current architecture this is a no-op (no non-BAA providers exist); it remains in code as a defense-in-depth check against future configuration mistakes.
3. **Two-stage classifier.** Stage 1 (vocabulary keyword match) runs locally inside the operator's environment and never sends document content over the network. Healthcare documents that match Stage 1 never reach Bedrock for routing purposes — the vocabulary classifier alone routes them. Only the ~20% of inputs that fall through Stage 1 reach Bedrock Nova Lite, which is itself BAA-eligible.

PII routing is `HIPAA_MODE`-dependent and handled in the routing layer rather than at the schema level — the same field flagged `DataClass.PII` may route differently in healthcare-mode vs default mode depending on which `IntakeFormBase` subclass owns it. See `RATIONALE.md` for the orthogonal-axis reasoning behind keeping `data_class` (WHAT) separate from `sensitivity` (HOW).

## Cascade composition (same in both modes)

Per the Hybrid architecture (locked 2026-05-12), the cascade structure is identical in standard mode and HIPAA mode — the differentiator is the deployment environment, not the provider mix:

- **Tier 1 (PaddleOCR-VL)** — local on operator-controlled GPUs (RTX 4060 Ti in the portfolio rig; equivalent on a customer's on-prem hardware).
- **Router Stage 2 (Bedrock Nova Lite)** — AWS BAA-eligible cloud, escalation only when Stage 1 vocabulary score falls below threshold.
- **Tier 2 (AWS Textract Queries)** — AWS BAA-eligible cloud, ~30% escalation rate.
- **Tier 3a (Qwen 2.5 VL 32B via Ollama)** — local on operator-controlled GPUs (combined RTX 4080 + RTX 4060 Ti in the portfolio rig).
- **Tier 3b (Bedrock Claude Sonnet 4.6)** — AWS BAA-eligible cloud, ~3% escalation rate.

Cost: **~$9.50 per 1,000 documents** at estimated cascade escalation rates. HIPAA-mode deployment adds the operator's on-prem hardware amortization (Tier 1 + Tier 3a GPU silicon they're already buying for other workloads) and their existing BAA with AWS for the cloud tiers. There is no second cost number to publish — the cascade fires identically whether documents are PHI or not.

The pre-pivot architecture (locked-then-revised 2026-05-12) routed Tier 1 to Novita and Tier 3a to Together AI in standard mode and swapped them out at the routing layer for `HIPAA_MODE=on`. Neither vendor publishes a BAA; the swap was correct in principle but produced a ~6.25× standard-vs-HIPAA cost differential ($24 → $150/1K) and a more complex routing story. Dropping the non-BAA cloud surface entirely eliminated both. Architecture pivot details in the project's internal `.claude-context/cost-model.md` "Architecture pivot history" section.

## Synthetic-to-real-PHI swap path

> Lands in Phase 5 alongside the `HIPAA_MODE` flag implementation. Section will cover: BAA execution checklist for the adopting organization (AWS BAA via the AWS Artifact portal covers Bedrock, Textract, S3, Aurora, and the supporting AWS services the cascade uses; no third-party BAAs needed because Tier 1 + Tier 3a are operator-hosted local inference). Operator deployment-environment checklist (physical access controls, audit logging, encryption at rest/in-transit, workforce HIPAA training, breach response runbook). Synthea-generated S3 input bucket swap to a real-PHI bucket with the same prefix structure. Aurora schema migration is no-op (`HealthcareIntakeForm` schema is identical). This project provides a reference architecture for HIPAA-aware intake-form processing — the cloud-side audit-trail plumbing (CloudWatch + Aurora logs structured to support HIPAA's accounting-of-disclosures requirement), the BAA-eligible AWS service mapping (Bedrock + Textract + Aurora + S3), and the local-tier integration points designed for deployment inside a HIPAA-compliant on-prem environment. It is not, by itself, a HIPAA-compliant system. The adopting organization brings the on-prem HIPAA-compliant infrastructure, the signed BAA with AWS, workforce HIPAA training, breach response runbook, and the legal/compliance validation that the combined deployment meets HIPAA's requirements end-to-end. HIPAA mode in this codebase exists to make this reference architecture deployable into that compliant environment without further routing-layer surgery.

## Healthcare-specific routing rules

> Lands in Phase 5. Section will enumerate the per-DataClass routing predicates and the Stage 1 vocabulary-matching threshold N (starting value 1.0, tuned in Phase 5 spot-check against ~50 hand-classified docs and again in Phase 6 eval).
