# Architecture deep-dive

Detailed view of the cascade orchestration, routing layer, and data model. Sections fill out as the corresponding phases land; the README is the canonical entry point until then.

## Public edge (Phase 2 PR 5)

The project's public surface — `https://ai-intake.markandrewmarquez.com/` — is a CloudFront distribution fronting an S3-origin landing bucket, with WAFv2 attached and v2 access logs delivered to S3. Phase 2 PR 5 stood it up serving a static placeholder; Phase 7 swaps the bucket contents for the React review UI bundle without changing any of the surrounding edge configuration.

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

CSP is intentionally not in the AWS-managed policy — Phase 7's React app has its own CSP requirements that vary by build, so a custom `aws_cloudfront_response_headers_policy` lands then.

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

### Phase 7 swap

Phase 7 replaces the placeholder `index.html` with the React review UI bundle. Mechanics: `aws_s3_object` resources for the new bundle (multiple objects under `assets/` plus a transformed root `index.html`), updated `etag = filemd5(...)` triggers re-upload on content change, an optional cache invalidation via `cloudfront:CreateInvalidation` if the React bundle's filenames don't include hash suffixes (Vite/Next builds typically do, so the invalidation may be unnecessary). None of the surrounding CloudFront / WAF / ACM / Route 53 / log delivery configuration changes; only the bucket contents.

The wake-on-request UX (project pitch + F1-over-time chart during the 30-90s Aurora cold start, then auto-redirect to the React UI) lands in Phase 7 as part of that swap, not in PR 5.

## Four-tier routing structure (Tier 3a / 3b distinction)

> Lands in Phase 4 with the provider implementations. The cascade is described as "three tiers" in the README, but Tier 3 splits internally into 3a (vision-capable open-weights LLM — Qwen 2.5 VL 32B, local on the project's combined RTX 4080 + RTX 4060 Ti both in development and in the deployed demo via Cloudflare Tunnel bridge) and 3b (strongest closed LLM — Claude Sonnet 4.6 via Bedrock). The 3a/3b distinction matters because 3a is locally hosted (HIPAA-safe when the host environment meets HIPAA Security Rule controls) while 3b is cloud-only managed inference (AWS BAA-eligible). This section will diagram the per-tier escalation thresholds (0.85 / 0.80 / 0.75), the retry-then-escalate failure handling, and the per-field provenance trail in `ExtractedField.escalation_history`.

## Step Functions state machine layout

> Lands in Phase 5 with the orchestration code. State machine has one state per tier plus the two-stage classifier, with Retry/Catch wired per state for the hybrid retry-then-escalate failure-handling pattern (4xx fails immediately; 5xx/timeout retries 3× with exponential backoff 1s/2s/4s with jitter then escalates; 429 respects Retry-After; schema-validation failure retries once with stricter prompt then escalates). Tier 3b exhaustion routes to a review-queue state with full error history attached.

## Sequence diagrams

> Lands in Phase 10 polish. Mermaid or Excalidraw export covering: standard-mode happy path (Tier 1 only), low-confidence escalation through Tier 2 → 3a → 3b, two-stage classifier with Stage 2 fallback, and the full failure-handling tree.

## Schema introspection patterns

> Lands in Phase 4 alongside the provider implementations. The Pydantic v2 schemas are introspected at runtime to drive: prompt template generation per vertical, alias-table vocabulary extraction for the Stage 1 classifier, and the `compute_form_confidence` aggregation that decides auto-approval vs review-queue. Section will cover `get_field_metadata`, `field_metadata_as_dict`, and the `FieldMeta`-keyed canonical-name invariant enforced at module import.

## Data model

> Lands incrementally as Aurora schema migrations land in Phase 2 (eval/staging schemas) and Phase 7 (demo schema). Section will cover the three-schema layout (`demo`, `eval`, `staging`), the alias-table key structure (`canonical_name`, `vertical`, `alias_text`), the pgvector embedding columns, and the session-keyed reset path for `demo`.
