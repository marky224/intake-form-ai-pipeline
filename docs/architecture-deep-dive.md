# Architecture deep-dive

Detailed view of the cascade orchestration, routing layer, and data model. Sections fill out as the corresponding phases land; the README is the canonical entry point until then.

The project ships in two iterations. **V1** is the active local-first build — three local cascade tiers (PaddleOCR-VL → Qwen 2.5 VL 7B → Qwen 2.5 VL 32B), local Python orchestrator, plain SQLite for storage (ColQwen multivectors as BLOB + brute-force MaxSim, not sqlite-vec), no AWS. **V2** is the deferred cloud rebuild — adds BAA-eligible AWS services in the middle of the cascade (Textract) and at the top (Bedrock Sonnet), wires the local tiers to deployed Lambda via Cloudflare Tunnel, and stands up the public demo at `ai-intake.markandrewmarquez.com`. Sections below are labeled V1 / V2 / both as appropriate.

## V1 local orchestrator (active build)

The V1 cascade runs as an in-process Python orchestrator on the home GPU machine. No state machine, no managed service, no network hop between tiers — the orchestrator constructs each provider instance once, then iterates documents through Tier 1 → Tier 2 → Tier 3 with per-field confidence checks at each escalation boundary.

Trust boundary: the V1 orchestrator runs as the local user with filesystem access to `synthetic_data/output/`, the SQLite database at `data/v1.db`, the eval-cache fixtures under `tests/fixtures/eval-cache/`, and the Ollama daemon at `localhost:11434`. No network egress beyond Ollama's localhost. The cascade is fully reproducible on a fresh clone given the locked model weights — same fixture inputs + same model versions = same outputs.

### V1 tier wiring

- **Tier 1** (`cascade/providers/tier1_paddleocr_local.py`): instantiates `PaddleOCRVL()` on first call, pinning `paddle.set_device("gpu:1")` so it lands on the RTX 4060 Ti. Calls `pipeline.predict(input=image_bytes)` which returns `parsing_res_list` — layout blocks with `block_bbox` + `block_label` + `block_content`. A downstream layout-to-fields post-processor inside `_invoke_pipeline` walks the blocks, matches `block_content` text against `alias_table_seed.json`, and builds per-field `ExtractedField` outputs. Confidence is the post-processor's match-score against the alias table (a weighted-substring score, not a model confidence).
- **Tier 2** (`cascade/providers/tier2_qwen_7b_local.py`, shipped Phase 4-V1 PR (c) / PR #51): Ollama prompted-extraction against `qwen2.5vl:7b` — a true prompted VL model (vs Tier 1's layout parser). A schema-driven prompt is built from the form class and Ollama decodes under a JSON schema (`format=<schema>`), so the same provider serves both verticals with no per-vertical wiring. As a standalone Phase 4 provider it extracts the **full form** from the full-page PNG; Phase 5's orchestrator narrows the prompt to just the fields that escalated past Tier 1's 0.85 threshold. Confidence is a deterministic inner-type heuristic (string-like used verbatim → 1.0, format-coerced scalar → 0.5, null/unparseable → confidently-blank), not a model self-report. Tier 2 sets `bounding_box=None` — Tier 1 already attaches layout-parser bboxes to the fields it populates, and 7B bbox grounding is unreliable.
- **Tier 3** (`cascade/providers/tier3_qwen_32b_local.py`, lands Phase 4-V1 PR (d)): Ollama prompted-extraction against the registry build `qwen2.5vl:32b` (Q4_K_M). Same prompt shape as Tier 2 (shared `_qwen_vl` core), more parameters. The architecture originally planned a higher-precision Mungert-GGUF Q8_0/Q6_K Modelfile import with a dual-quant sanity test; that proved infeasible on the 31.2 GB consumer-VRAM build box (the Mungert imports hit an open llama.cpp M-RoPE `seq_add` assert / exceeded VRAM), so V1 ships the registry Q4_K_M build — a documented consumer-hardware trade-off.

### V1 router

Stage 1 is a vocabulary keyword classifier built at orchestrator startup from `alias_table_seed.json` — runtime build, ~50 ms, no cache file to invalidate. Substring match per line of OCR text (normalized to uppercase), weighted by alias specificity (inverse frequency in the seed file). Healthcare classification when accumulated weighted-match score ≥ N (starting value 1.0; Phase 5 spot-check tunes against ~50 hand-classified docs).

Stage 2 is the fallback for documents that score below N at Stage 1 (~20% of inputs). V1 routes Stage 2 to the local Qwen 2.5 VL 7B model with a routing prompt; the marginal cost is zero because the model is already loaded for Tier 2 of the cascade anyway. V2 swaps Stage 2 to Bedrock Nova Lite at the BAA boundary.

### V1 persistence

SQLite at `data/v1.db`, single file, gitignored. Schema (locked at Phase 5-V1 entry, `cascade/store.py`). Normalized rather than a single flat `eval_results` table: the orchestrator writes only what it can actually produce at run time, and Phase 6 / Phase 8 join in by `doc_id` instead of sharing one table's write path. `ground_truth` and `batch_id` are deliberately absent from the orchestrator's write surface — they are Phase 6 eval-harness concepts the orchestrator has no value for at run time; the eval harness joins truth in externally by `doc_id`.

- `runs` — one row per document processed. Columns: doc_id (PK), vertical, router_stage, router_score, final_tier, final_confidence, status (`extracted` | `review_queue`), total_latency_ms, created_at.
- `field_attempts` — one row per `(doc_id, field_name, tier)`. Columns: value, confidence, escalation_reason, latency_ms. A faithful persistent mirror of the in-memory `ExtractedField.escalation_history` trail — the granularity Phase 6's F1-over-time and Phase 8's correction loop both query.
- `review_queue` — one row per Tier-3-exhausted document. Columns: doc_id (PK), error_history (JSON — full per-tier failure trail), created_at. No cloud Sonnet above Tier 3 in V1.
- `corrections` / `embeddings` — **reserved for Phase 8** (reviewer corrections + ColQwen 2.5 vectors). Created by `init_db` so Phase 8 only adds writes, never a schema migration. `corrections` columns: doc_id, field_name, original_value, corrected_value, tier_that_produced_original, session_id, created_at. `embeddings.vector` is a `BLOB`; Phase 8 packs the ColQwen late-interaction multivector into it as a self-describing float32 blob and ranks by brute-force NumPy MaxSim — **no `vec0`/`sqlite-vec`, no schema migration** (single-vector KNN cannot express late interaction; rationale in `eval-methodology.md`). The alias seed stays a file (`alias_table_seed.json`, frozen at v1.0.0) loaded into the router's distinctive vocabulary at startup; Phase 8 reviewer corrections append to a gitignored runtime overlay unioned on top of it, not a DB table, in V1.

V2 migrates by replaying SQLite contents into the Aurora `staging` schema, then promoting to `demo` / `eval` as appropriate. The V1 schema is intentionally Aurora-compatible — type names map cleanly.

## V2 public edge (deferred cloud rebuild)

V2's public surface — `https://ai-intake.markandrewmarquez.com/` — is a CloudFront distribution fronting an S3-origin landing bucket, with WAFv2 attached and v2 access logs delivered to S3. The in-tree Terraform at `infra/terraform/` describes the full V2 target and was applied through 2026-05-12 (PRs #29 through #38 + #40); the `terraform destroy` on 2026-05-14 cleared the main stack for V1, leaving the bootstrap stack (TF state + DynamoDB lock + OIDC role) live. V2 re-applies the main-stack `.tf` files when the cloud rebuild begins. Phase 7-V2 swaps the bucket contents for the React review UI bundle without changing any of the surrounding edge configuration.

### Why this layout

CloudFront in front of S3 with Origin Access Control (OAC) is the standard pattern for serving static content from a non-public bucket. The bucket has full public-access-block on; CloudFront authenticates origin requests with SigV4 via OAC and the bucket policy grants `cloudfront.amazonaws.com` `s3:GetObject` scoped via `aws:SourceArn` to this exact distribution. No object goes out without going through CloudFront's edge — which means the WAF rules, security-headers policy, and access logging apply uniformly to every public read.

OAC replaces the deprecated Origin Access Identity (OAI). OAC works with SSE-KMS buckets (OAI doesn't), supports all S3 features including dynamic content, and signs origin requests with SigV4 rather than the legacy CloudFront-Group ACL grant. AWS recommends OAC for all new distributions.

### Bot blocking and rate limiting

Three layers, evaluated in WAF priority order at every request:

1. **Per-IP rate limit** (priority 1, BLOCK action). 100 requests per 5-minute rolling window per source IP. Above the limit, the IP is blocked for the rest of the evaluation window. Configurable via `var.waf_rate_limit_per_5min`. AWS WAF supports limits between 10 and 2,000,000,000 (the floor was lowered from 100 to 10 in August 2024).

2. **User-Agent block list** (priority 2, BLOCK action). Byte-match `or_statement` over `var.blocked_user_agents` (defaults: `python-requests`, `curl`, `scrapy`, `wget`) plus a `size_constraint_statement` matching empty UA — legitimate browsers always send a User-Agent. UA matching is case-insensitive (`text_transformation = LOWERCASE`).

3. **AWS-managed rule groups** (priorities 3-5, no override). `AWSManagedRulesCommonRuleSet` covers OWASP-flavored attack vectors (XSS, SQL injection, path traversal). `AWSManagedRulesKnownBadInputsRuleSet` blocks known-bad payloads including Log4Shell and Spring4Shell. `AWSManagedRulesAmazonIpReputationList` blocks source IPs from AWS's threat-intel feed. All three are free.

Default action is ALLOW; sampled requests and CloudWatch metrics are enabled per-rule so blocked-traffic patterns surface in the console without needing a separate logging pipeline today. Full WAF logging (Kinesis Firehose or S3 destination) is deferred to the compute-layer monitoring story; sampled requests are sufficient for the placeholder window.

The decision was AWS WAF rate-based vs Lambda@Edge + DynamoDB rate counters. WAF is the production-grade answer: rules are managed in one console, the rate-limit primitive is purpose-built rather than reverse-engineered, and the cost differential at portfolio scale (~$5/month) doesn't outweigh the ~10-15 build hours saved or the cleaner architecture story.

### Security headers and TLS

The distribution attaches AWS-managed `SecurityHeadersPolicy` via `response_headers_policy_id` on the default cache behavior. Every response gets:

- `Strict-Transport-Security: max-age=31536000` (1 year HSTS)
- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: SAMEORIGIN`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `X-XSS-Protection: 1; mode=block`

CSP is intentionally not in the AWS-managed policy — Phase 7-V2's React app has its own CSP requirements that vary by build, so a custom `aws_cloudfront_response_headers_policy` lands then.

TLS 1.2 minimum (`TLSv1.2_2021`), SNI-only (no IP-bound SSL — costs $600/month), HTTP/2 + HTTP/3 enabled, IPv6 enabled. The ACM certificate for `ai-intake.markandrewmarquez.com` lives in `us-east-1` (CloudFront only accepts certs from that region), validated via DNS records published into the project's Route 53 hosted zone. `aws_acm_certificate_validation` blocks the distribution from attaching an unissued cert; `create_before_destroy` lifecycle on the certificate so future renewals don't take down the distribution.

### Access logs (v2 CloudWatch Logs Delivery)

The distribution's access logs flow through the v2 CloudWatch Logs Delivery primitives:

```text
aws_cloudwatch_log_delivery_source.cloudfront         # source = distribution ARN
       ↓
aws_cloudwatch_log_delivery.cloudfront                # joins source to destination
       ↓
aws_cloudwatch_log_delivery_destination.cloudfront_s3 # dest = access-logs bucket
```

Output format is JSON for Athena-friendly downstream querying. The actual S3 write path is `<access-logs-bucket>/cloudfront/AWSLogs/<account>/CloudFront/<distribution-id>/<date>/...` — AWS auto-prepends the `AWSLogs/<account>/CloudFront/` partitioning after the user-supplied `suffix_path = "cloudfront"`.

Legacy ACL-based standard logging (the older `access_logs` block on `aws_cloudfront_distribution`) was rejected because the access-logs bucket has `BucketOwnerEnforced` on, which disallows the bucket-ACL grants standard logging requires. The v2 path uses an IAM-style bucket policy grant instead — `delivery.logs.amazonaws.com` gets `s3:PutObject` scoped to the `cloudfront/` prefix, plus `s3:GetBucketAcl` on the bucket itself for the destination's pre-create check.

### Origin separation

The landing bucket is deliberately separate from `documents` and `artifacts`. Mixing the SPA bundle into the documents bucket would either widen what's reachable through CloudFront (the documents bucket holds intake-form PDFs and extraction inputs/outputs) or require an elaborate origin-level path filter on the distribution. A separate landing bucket with its own scoped policy is the simpler boundary.

The same hardening applies as the documents/artifacts buckets: versioned, AES256, public-access-block on, TLS-only deny in the bucket policy, S3 server access logging delivering to the access-logs bucket under `landing/`. The OAC `s3:GetObject` grant is the only additional statement.

### Phase 7-V2 swap

Phase 7-V2 replaces the placeholder `index.html` with the React review UI bundle. Mechanics: `aws_s3_object` resources for the new bundle (multiple objects under `assets/` plus a transformed root `index.html`), updated `etag = filemd5(...)` triggers re-upload on content change, an optional cache invalidation via `cloudfront:CreateInvalidation` if the React bundle's filenames don't include hash suffixes (Vite/Next builds typically do, so the invalidation may be unnecessary). None of the surrounding CloudFront / WAF / ACM / Route 53 / log delivery configuration changes; only the bucket contents.

The wake-on-request UX (project pitch + F1-over-time chart during the 30-90s Aurora cold start, then auto-redirect to the React UI) lands in Phase 7-V2 as part of that swap, not in V2's initial edge bring-up.

## V2 cascade structure (five tiers when cloud is wired)

> Lands when V2 begins. V1's three-tier all-local cascade (PaddleOCR-VL → Qwen 2.5 VL 7B → Qwen 2.5 VL 32B) expands to five tiers in V2 by inserting Tier 2-cloud (AWS Textract Regular Queries) between V1's Tier 2-local and V1's Tier 3, and adding Tier 3b (Bedrock Claude Sonnet 4.6) after V1's Tier 3. The escalation order at the V2 boundary: Tier 1 → Tier 2-local → Tier 2-cloud → Tier 3 → Tier 3b. Per-tier escalation thresholds: 0.85 / 0.80 / 0.80 / 0.75 (V1's 0.85 / 0.80 plus two new thresholds at the V2 cloud boundaries). The Tier 2-cloud / Tier 3b distinction matters because both V1 local tiers AND Tier 3 are HIPAA-safe when the host environment meets HIPAA Security Rule controls, while the V2 cloud tiers are cloud-only managed inference (AWS BAA-eligible). This section will diagram the per-tier failure-handling tree (retry-then-escalate per tier) and the per-field provenance trail in `ExtractedField.escalation_history` once V2 lands.

## Step Functions state machine layout (V2)

> Lands in Phase 5-V2 with the cloud orchestration code. V1's local Python orchestrator (`cascade/orchestrator.py`) is the operational equivalent — same routing logic, same retry/escalate behavior, same confidence-threshold escalation. V2 wraps the V1 orchestrator in a Step Functions state machine (or replaces it; decided at V2 entry). One state per tier plus the two-stage classifier, with Retry/Catch wired per state for the hybrid retry-then-escalate failure-handling pattern (4xx fails immediately; 5xx/timeout retries 3× with exponential backoff 1s/2s/4s with jitter then escalates; 429 respects Retry-After; schema-validation failure retries once with stricter prompt then escalates). Tier 3b exhaustion routes to a review-queue state with full error history attached.

## Sequence diagrams

> Lands in Phase 10 polish. Mermaid or Excalidraw export covering: V1 happy path (Tier 1 only), V1 low-confidence escalation through Tier 2 → Tier 3, V2 happy path with cloud tiers wired, V2 frontier-fallback escalation through Tier 3b, two-stage classifier with Stage 2 fallback (V1 local, V2 Bedrock Nova Lite), and the full failure-handling tree.

## Schema introspection patterns

> Lands in Phase 4 alongside the provider implementations. The Pydantic v2 schemas are introspected at runtime to drive: prompt template generation per vertical, alias-table vocabulary extraction for the Stage 1 classifier, and the `compute_form_confidence` aggregation that decides auto-approval vs review-queue. Section will cover `get_field_metadata`, `field_metadata_as_dict`, and the `FieldMeta`-keyed canonical-name invariant enforced at module import. Same module surface works in both V1 (called directly by the local orchestrator) and V2 (called from Lambda handlers).

## Data model

V1's data model is a single SQLite file at `data/v1.db`: the orchestrator's normalized `runs` + `field_attempts` + `review_queue`, the harness-owned `eval_results` + `eval_batches`, and the Phase 8 RAG loop's `corrections` + `embeddings`. The alias table is a file (`alias_table_seed.json`) plus a gitignored runtime overlay, not a DB table, in V1. `embeddings.vector` holds the ColQwen multivector as a self-describing float32 BLOB, ranked by a brute-force NumPy MaxSim — not `sqlite-vec` (single-vector KNN cannot express late interaction; see `eval-methodology.md`). All gitignored. The V1 schema is intentionally Aurora-compatible so the V2 migration is a row-copy, not a redesign.

V2's data model is Aurora Serverless v2 PostgreSQL with three schemas: `demo` (session-keyed, truncated per visit by the `reset_demo` Lambda), `eval` (source of truth for F1-over-time numbers), `staging` (development sandbox). The alias table key structure is `(canonical_name, vertical, alias_text)` in both V1 and V2. V2 adds pgvector embedding columns. Session-keyed reset uses a DynamoDB single-item lock to prevent simultaneous-wake races.
