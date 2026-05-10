provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = "demo"
      ManagedBy   = "terraform"
      Owner       = "mark"
      Stack       = "bootstrap"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id        = data.aws_caller_identity.current.account_id
  state_bucket_name = "${var.project_name}-tfstate-${local.account_id}"

  # CloudFront edge resources (ACM certs for CloudFront, CLOUDFRONT-scope
  # WAF web ACLs / regex sets / IP sets) MUST live in us-east-1
  # regardless of the project's primary region. Pinning the literal here
  # keeps the deploy role's IAM scope correct even if `var.aws_region`
  # is ever overridden to a non-us-east-1 region for the rest of the
  # stack. Used only for `project_acm_arns` and `project_wafv2_arns`
  # below; project-region resources (RDS, KMS, secrets, log groups,
  # CloudTrail) keep using `var.aws_region`.
  edge_region = "us-east-1"

  lock_table_name     = "${var.project_name}-tflock"
  oidc_provider_url   = "token.actions.githubusercontent.com"
  oidc_audience       = "sts.amazonaws.com"
  ci_role_name_deploy = "${var.project_name}-github-actions-deploy"
  ci_role_name_plan   = "${var.project_name}-github-actions-plan"

  # Project-bucket ARN patterns for the deploy role's scoped S3 allow.
  # Wildcard suffix matches the `<account_id>` form the main stack uses
  # today and any future per-environment buckets (e.g.,
  # `<project>-documents-staging-<account>`) without re-scoping IAM.
  # `var.project_name` ("intake-form-ai-pipeline") is specific enough that
  # this won't accidentally match other projects in the account.
  project_bucket_arns = [
    "arn:aws:s3:::${var.project_name}-documents-*",
    "arn:aws:s3:::${var.project_name}-documents-*/*",
    "arn:aws:s3:::${var.project_name}-artifacts-*",
    "arn:aws:s3:::${var.project_name}-artifacts-*/*",
    # Audit-trail buckets (PR 4 closeout): S3 access logs target and
    # CloudTrail delivery target. Same wildcard shape so per-environment
    # variants land without re-scoping IAM.
    "arn:aws:s3:::${var.project_name}-access-logs-*",
    "arn:aws:s3:::${var.project_name}-access-logs-*/*",
    "arn:aws:s3:::${var.project_name}-cloudtrail-logs-*",
    "arn:aws:s3:::${var.project_name}-cloudtrail-logs-*/*",
    # Landing bucket (PR 5b): CloudFront origin for the demo. Same
    # wildcard shape covers per-environment variants.
    "arn:aws:s3:::${var.project_name}-landing-*",
    "arn:aws:s3:::${var.project_name}-landing-*/*",
  ]

  # Project-RDS ARN patterns for the deploy role's scoped Aurora allow
  # (Phase 2 PR 4). RDS resource ARNs are name-prefixed, so a single
  # wildcard per resource type covers cluster, instances, parameter
  # groups, subnet group, and DB-level security group as long as
  # everything created by the main stack carries the project prefix.
  project_rds_arns = [
    "arn:aws:rds:${var.aws_region}:${local.account_id}:cluster:${var.project_name}-*",
    "arn:aws:rds:${var.aws_region}:${local.account_id}:db:${var.project_name}-*",
    "arn:aws:rds:${var.aws_region}:${local.account_id}:subgrp:${var.project_name}-*",
    "arn:aws:rds:${var.aws_region}:${local.account_id}:cluster-pg:${var.project_name}-*",
    "arn:aws:rds:${var.aws_region}:${local.account_id}:pg:${var.project_name}-*",
    "arn:aws:rds:${var.aws_region}:${local.account_id}:secgrp:${var.project_name}-*",
  ]

  # Secret-path prefix for the deploy role's scoped Secrets Manager
  # allow (Phase 2 PR 4). Wildcard leaves room for additional secrets
  # under the project namespace without re-scoping IAM. Phase 2 PR 4b
  # initially used this for an explicit `<project>/aurora/master` secret;
  # PR 4c switched the cluster to AWS-managed master passwords (which
  # live at `rds!cluster-*` instead — see `project_managed_secret_arns`
  # below), so this scope now only covers any future explicit secrets.
  project_secret_arns = [
    "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:${var.project_name}/*",
  ]

  # AWS RDS owns the naming for managed master-user-password secrets:
  # the format is `rds!cluster-<UUID>-<6-char-suffix>` and the principal
  # creating the cluster needs `secretsmanager:TagResource` and
  # `secretsmanager:RotateSecret` on the secret ARN. Scope the deploy
  # role to that AWS-owned namespace; out-of-project clusters in this
  # account still get implicitDeny because the cluster create itself is
  # blocked by `AuroraResourceManage` (project-prefixed cluster ARNs).
  project_managed_secret_arns = [
    "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:rds!cluster-*",
  ]

  # CloudWatch Log Group ARN pattern for Aurora log exports (Phase 2
  # PR 4c). RDS owns the log-group name format
  # `/aws/rds/cluster/<cluster-id>/<log-type>` (e.g.,
  # `/aws/rds/cluster/intake-form-ai-pipeline-aurora/postgresql`). The
  # `:*` suffix covers log streams inside the group, which Terraform
  # never directly manages but provider refresh sometimes touches.
  project_log_group_arns = [
    "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/rds/cluster/${var.project_name}-*",
    "arn:aws:logs:${var.aws_region}:${local.account_id}:log-group:/aws/rds/cluster/${var.project_name}-*:*",
  ]

  # ACM certificate ARNs for the deploy role's scoped allow (Phase 2
  # PR 5). ACM resource ARNs are UUID-based, so name-prefix scoping
  # does not apply. The deploy role gets account-region management of
  # certificates; the AcmManage statement narrows further with an
  # `aws:ResourceTag/Project` condition so other projects' certs in this
  # account stay out of reach for mutating actions. Region pinned to
  # `local.edge_region` — ACM certs for CloudFront must be in us-east-1
  # regardless of the project's primary region.
  project_acm_arns = [
    "arn:aws:acm:${local.edge_region}:${local.account_id}:certificate/*",
  ]

  # WAF v2 ARN patterns for the deploy role's scoped allow (Phase 2
  # PR 5). CLOUDFRONT-scope web ACLs (and their regex/IP sets) live in
  # us-east-1 globally with `global` segment in the ARN; REGIONAL
  # resources would use the actual region. This project ships a single
  # CLOUDFRONT-scope web ACL — the global segment is what we need.
  # Names are user-controlled, so name-prefix scoping works without
  # tag conditions. Region pinned to `local.edge_region` for the same
  # reason as ACM above.
  project_wafv2_arns = [
    "arn:aws:wafv2:${local.edge_region}:${local.account_id}:global/webacl/${var.project_name}-*/*",
    "arn:aws:wafv2:${local.edge_region}:${local.account_id}:global/regexpatternset/${var.project_name}-*/*",
    "arn:aws:wafv2:${local.edge_region}:${local.account_id}:global/ipset/${var.project_name}-*/*",
  ]

  # Route 53 hosted-zone ARN for the deploy role's scoped allow (Phase 2
  # PR 5). The hosted zone itself is created out-of-band on Mark's apex
  # domain; this stack only manages records inside it. ARN scope here
  # is the single zone, so changes can't leak to other zones in the
  # account.
  project_route53_zone_arns = [
    "arn:aws:route53:::hostedzone/${var.route53_hosted_zone_id}",
  ]

  # Phase 2 PR 6: Budgets ARN pattern. Budgets is a global service, so
  # the ARN has no region segment (the double-colon is intentional).
  # Name-prefix scoping keeps the deploy role bounded to project-owned
  # budgets; other budgets in this account remain out of reach.
  project_budget_arns = [
    "arn:aws:budgets::${local.account_id}:budget/${var.project_name}-*",
  ]

  # SNS topic ARN pattern for the project's cost-alerts topic (Phase 2
  # PR 6) and any future project SNS topics. Name-prefix scoping bounds
  # the deploy role to project-owned topics without needing a tag
  # condition. Region-scoped (SNS is regional).
  project_sns_topic_arns = [
    "arn:aws:sns:${var.aws_region}:${local.account_id}:${var.project_name}-*",
  ]
}

resource "aws_s3_bucket" "tfstate" {
  # checkov:skip=CKV_AWS_18:Access logging on the state bucket would mean a separate logging bucket bootstrapped before the state bucket — chicken-and-egg without manual setup. CloudTrail data events on this bucket cover the audit need at less operational cost (see infra/terraform/cloudtrail.tf — the trail's data_resource block lists this bucket explicitly).
  bucket = local.state_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

data "aws_iam_policy_document" "tfstate_tls_only" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tfstate_tls_only" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate_tls_only.json
}

resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }
  }

  rule {
    id     = "abort-incomplete-multipart-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.tfstate]
}

resource "aws_dynamodb_table" "tflock" {
  name         = local.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}
