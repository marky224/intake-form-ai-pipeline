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
  account_id          = data.aws_caller_identity.current.account_id
  state_bucket_name   = "${var.project_name}-tfstate-${local.account_id}"
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
  # allow (Phase 2 PR 4). Aurora master-password secret lives at
  # `<project>/aurora/master`; the wildcard leaves room for additional
  # secrets under the project namespace without re-scoping IAM.
  project_secret_arns = [
    "arn:aws:secretsmanager:${var.aws_region}:${local.account_id}:secret:${var.project_name}/*",
  ]
}

resource "aws_s3_bucket" "tfstate" {
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
