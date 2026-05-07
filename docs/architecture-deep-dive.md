# Architecture deep-dive

Detailed view of the cascade orchestration, routing layer, and data model. Sections fill out as the corresponding phases land; the README is the canonical entry point until then.

## Four-tier routing structure (Tier 3a / 3b distinction)

> Lands in Phase 4 with the provider implementations. The cascade is described as "three tiers" in the README, but Tier 3 splits internally into 3a (vision-capable open-weights LLMs — Qwen 2.5 VL local + Together AI 72B in production) and 3b (strongest closed LLMs — Claude Sonnet 4.6 via Bedrock). The 3a/3b distinction matters because 3a is local-capable on the project hardware while 3b never is, and because the BAA-routing rules treat them differently. This section will diagram the per-tier escalation thresholds (0.85 / 0.80 / 0.75), the retry-then-escalate failure handling, and the per-field provenance trail in `ExtractedField.escalation_history`.

## Step Functions state machine layout

> Lands in Phase 5 with the orchestration code. State machine has one state per tier plus the two-stage classifier, with Retry/Catch wired per state for the hybrid retry-then-escalate failure-handling pattern (4xx fails immediately; 5xx/timeout retries 3× with exponential backoff 1s/2s/4s with jitter then escalates; 429 respects Retry-After; schema-validation failure retries once with stricter prompt then escalates). Tier 3b exhaustion routes to a review-queue state with full error history attached.

## Sequence diagrams

> Lands in Phase 10 polish. Mermaid or Excalidraw export covering: standard-mode happy path (Tier 1 only), low-confidence escalation through Tier 2 → 3a → 3b, two-stage classifier with Stage 2 fallback, and the full failure-handling tree.

## Schema introspection patterns

> Lands in Phase 4 alongside the provider implementations. The Pydantic v2 schemas are introspected at runtime to drive: prompt template generation per vertical, alias-table vocabulary extraction for the Stage 1 classifier, and the `compute_form_confidence` aggregation that decides auto-approval vs review-queue. Section will cover `get_field_metadata`, `field_metadata_as_dict`, and the `FieldMeta`-keyed canonical-name invariant enforced at module import.

## Data model

> Lands incrementally as Aurora schema migrations land in Phase 2 (eval/staging schemas) and Phase 7 (demo schema). Section will cover the three-schema layout (`demo`, `eval`, `staging`), the alias-table key structure (`canonical_name`, `vertical`, `alias_text`), the pgvector embedding columns, and the session-keyed reset path for `demo`.
