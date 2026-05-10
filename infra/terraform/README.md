# Terraform main stack

The second Terraform stack in the project, holding everything that isn't bootstrap-state-backend or bootstrap-IAM. The state lives in the same S3 bucket the bootstrap stack created, but under a different key (`main/terraform.tfstate`) so the two stacks don't share state files.

## Current scope (Phase 2 complete)

- `module.network` — VPC (`10.0.0.0/16`), 3 public + 3 private subnets, single NAT gateway, IGW, route tables, S3 + DynamoDB gateway endpoints. CIDR plan: public `10.0.{0,1,2}.0/24`, private `10.0.{10,11,12}.0/24`.
- `module.documents_bucket` — `<project>-documents-<account>`. Synthea + DocILE inputs and downstream outputs.
- `module.artifacts_bucket` — `<project>-artifacts-<account>`. Rendered PDFs, eval fixtures.
- `module.database` — Aurora Serverless v2 PostgreSQL 16.4 cluster (`<project>-aurora`), single `db.serverless` instance, `min 0 / max 1.0 ACU` with 5-minute auto-pause. Tier 1 security pass: AWS-managed master password (`manage_master_user_password = true`, 7-day rotation default; secret name `rds!cluster-<UUID>-<suffix>`, RDS-owned), `rds.force_ssl = 1` enforces TLS at the parameter level, postgresql logs exported to a CloudWatch log group with 30-day retention. Cluster parameter group preloads pgvector. A customer-managed KMS CMK encrypts the cluster volume, the master secret, and the log group; the key policy grants account root admin plus the Aurora and CloudWatch Logs service principals their respective grants. Cluster security group with no ingress yet (Lambda/bastion ingress lands with the compute layer); single initial database (`intake`) — schemas `demo`/`eval`/`staging` are added post-apply via `just db-init-schemas`.
- `module.access_logs_bucket` + `module.cloudtrail_logs_bucket` + `module.landing_bucket` — audit-trail and CloudFront-origin buckets, same hardening as the documents/artifacts buckets. The access-logs bucket is the S3 server-access-log target AND the CloudFront v2 access-logs delivery destination; the cloudtrail-logs bucket is the CloudTrail delivery target with a `cloudtrail/` key prefix.
- `aws_cloudtrail.this` — single-region us-east-1 trail with management events (read+write, including global service events) plus S3 data events on documents/artifacts/tfstate. Log file validation enabled.
- `aws_cloudfront_distribution.this` + `aws_cloudfront_origin_access_control.landing` + ACM cert + Route 53 alias records — public demo edge at `https://ai-intake.markandrewmarquez.com/`. PriceClass_100, HTTP/2 + HTTP/3, IPv6, TLS 1.2 SNI minimum, AWS-managed `CachingOptimized` cache policy + `SecurityHeadersPolicy` response headers (HSTS, X-Frame-Options, etc.), redirect-to-https, GET/HEAD only.
- `aws_wafv2_web_acl.edge` — CLOUDFRONT-scope WAF with five rules (priority order): per-IP rate-based 100 req/5 min (BLOCK), `BlockKnownScrapeUserAgents` byte-match on `python-requests`/`curl`/`scrapy`/`wget` + empty UA, plus three AWS-managed rule groups (`CommonRuleSet`, `KnownBadInputs`, `AmazonIpReputationList`). Default action ALLOW, sampled requests + CW metrics enabled per-rule.
- v2 CloudFront access logs delivery: `aws_cloudwatch_log_delivery_source.cloudfront` → `aws_cloudwatch_log_delivery_destination.cloudfront_s3` → `aws_cloudwatch_log_delivery.cloudfront`. Logs land at `<access-logs>/cloudfront/AWSLogs/<account>/CloudFront/<distid>/...`.
- `aws_sns_topic.cost_alerts` + `aws_sns_topic_policy.cost_alerts` + `aws_budgets_budget.daily_spend` — cost circuit breaker. Daily $5 USD budget, tag-filtered to project-tagged spend, single 100% ACTUAL notification routes to the SNS topic. Topic policy grants `budgets.amazonaws.com` and `costalerts.amazonaws.com` `sns:Publish` scoped via `aws:SourceAccount`. Subscriptions managed manually post-apply via `aws sns subscribe` (see `docs/production-roadmap.md` for the rationale on keeping personal-PII endpoints out of Terraform state).

All project buckets (documents, artifacts, access-logs, cloudtrail-logs, landing) mirror the state bucket's posture: versioning, SSE-S3 (AES256), public-access-block, TLS-only deny policy, lifecycle (90-day noncurrent expiry, 1-day MPU abort, 365-day current-version expiry on the audit-log buckets).

The first end-to-end CI apply went through on commit `e2f2bd7` after PR 3 scoped the deploy role's IAM and a one-line hotfix added `ec2:DescribeAddressesAttribute` (a gap in `AmazonVPCFullAccess` v13 that AWS provider 6.x reads on every EIP refresh). `vars.TF_APPLY_ENABLED` is `true` on the repo, so subsequent pushes to `main` apply automatically without further manual gating. See `docs/architecture-deep-dive.md` for the public-edge architecture detail.

## First-time apply (local)

Prerequisites: bootstrap stack already applied, `.tfbackend` file present (or pass values via `-backend-config` flags), and the `AWSServiceRoleForRDS` IAM service-linked role created in the account (see [Account-level prereqs](#account-level-prereqs) below).

```bash
cd infra/terraform

# Build .tfbackend from bootstrap outputs (one-time)
cat > .tfbackend <<EOF
bucket         = "$(terraform -chdir=bootstrap output -raw state_bucket_name)"
key            = "main/terraform.tfstate"
region         = "$(terraform -chdir=bootstrap output -raw aws_region)"
dynamodb_table = "$(terraform -chdir=bootstrap output -raw state_lock_table_name)"
encrypt        = true
EOF

terraform init -backend-config=.tfbackend
terraform plan
terraform apply
```

`.tfbackend` is gitignored. CI passes the same values via the `TF_STATE_BUCKET` and `TF_LOCK_TABLE` repo variables.

## Account-level prereqs

Two IAM resources have to exist in the AWS account before this stack can apply. Both are one-time-per-account, idempotent on re-create, and not owned by either Terraform stack — same out-of-band setup pattern the GitHub OIDC provider uses (see `bootstrap/README.md`):

```bash
# GitHub OIDC provider (also used by the bootstrap stack — see bootstrap/README.md)
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1

# Service-linked role for RDS — required before the database module can create Aurora
aws iam create-service-linked-role --aws-service-name rds.amazonaws.com
```

Both fail with a clear "already exists" error if you run them on an account that's already had them created (any prior RDS use creates the SLR, any prior GitHub Actions OIDC use creates the provider). The deploy role's IAM scope deliberately doesn't include `iam:CreateServiceLinkedRole`; pre-creating these resources keeps the deploy role's blast radius limited to the project's own resources.

## What's intentionally not here

- **Single NAT gateway is an AZ-affinity SPOF.** If `us-east-1a` (or whichever AZ holds the NAT) goes down, private subnets in the other two AZs lose internet egress. Acceptable for a wake-on-request demo where short outages are tolerable; multi-NAT is ~3× the cost. Revisit if Phase 7+ traffic patterns change the calculus.
- **NAT gateway runs $30+/month on its own.** Phase 1 cost estimate of $10-15/month total is achievable only if Aurora Serverless v2 stays in private subnets *and* the NAT is the dominant non-Aurora cost. If Lambdas later move out of the VPC entirely (Aurora Data API instead of native postgres), the NAT may become removable. Tracked as a deferred decision in `docs/production-roadmap.md`.
- **No NAT instance fallback.** A self-managed NAT instance (`t4g.nano`, ~$3/month) is cheaper than a NAT gateway but loses managed reliability. Not worth the ops complexity for a portfolio demo.
- **No interface endpoints (Bedrock, Textract, etc.).** Interface endpoints cost ~$7/month per service per AZ — compounds quickly. Demo workload is light enough that NAT-out for AWS API calls is cheaper. Revisit when egress traffic justifies the swap.
