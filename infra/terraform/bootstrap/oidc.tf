# GitHub Actions OIDC federation. The OIDC provider for the official GitHub
# URL is a singleton account-wide resource — only one can exist per account.
# This stack does not own its lifecycle (other projects in the account share
# it), so we reference it as a data source. The provider must be created
# out-of-band on first use of GitHub OIDC in the account; after that, every
# project just looks it up.
#
# AWS no longer validates the thumbprint for the official GitHub OIDC URL
# (the field is required by IAM but ignored at auth time), so existing
# providers with any historical thumbprint work for our purposes. See
# https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc_verify-thumbprint.html
data "aws_iam_openid_connect_provider" "github" {
  url = "https://${local.oidc_provider_url}"
}

# CI role split (Phase 2 PR 2): write perms first attach here, so the trust
# boundary needs to enforce least privilege.
#
# DEPLOY role: trusts pushes to main only. Has VPC + S3 managed write
# policies plus an inline deny on the Terraform state bucket and lock
# table so a buggy apply can't recursively nuke its own backend.
#
# PLAN role: trusts pull_request workflows. ReadOnlyAccess only. PR CI
# runs `terraform plan -lock=false` so it can read state from S3 without
# DynamoDB write perms.
#
# Forked PRs cannot assume either role (id-token: write downgrades to
# read for fork PR workflows). Same-repo PR branches can assume the plan
# role, but it cannot mutate AWS state.

# ---------- Deploy role (push to main only, write perms) ----------

data "aws_iam_policy_document" "deploy_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider_url}:aud"
      values   = [local.oidc_audience]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider_url}:sub"
      values   = ["repo:${var.github_repo}:ref:refs/heads/${var.github_main_branch}"]
    }
  }
}

resource "aws_iam_role" "github_actions_deploy" {
  name               = local.ci_role_name_deploy
  assume_role_policy = data.aws_iam_policy_document.deploy_assume_role.json
  description        = "Assumed by GitHub Actions on push to main. Write perms for VPC + S3 plus inline deny on the tfstate bucket and lock table."

  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "deploy_vpc" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonVPCFullAccess"
}

resource "aws_iam_role_policy_attachment" "deploy_s3" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonS3FullAccess"
}

# Block deploy role from destroying or weakening the Terraform backend.
# Bootstrap-stack changes are applied locally with admin creds, never via
# CI; this deny scope only prevents a buggy main-stack apply from
# recursively nuking its own backend or removing the security posture
# that protects the state bucket. Normal init/apply ops (Get/Put/List on
# state objects, GetItem/PutItem/DeleteItem on lock items) remain allowed
# via AmazonS3FullAccess and the absence of a DynamoDB allow conflict.
#
# Explicit IAM denies override allows, so the action lists below must
# stay tight — adding s3:* or dynamodb:* would block init.
data "aws_iam_policy_document" "deploy_state_backend_deny" {
  statement {
    sid    = "DenyTfStateBucketDestructive"
    effect = "Deny"

    actions = [
      "s3:DeleteBucket",
      "s3:DeleteBucketPolicy",
      "s3:PutBucketPolicy",
      "s3:PutBucketAcl",
      "s3:PutBucketVersioning",
      "s3:PutEncryptionConfiguration",
      "s3:DeleteBucketEncryption",
      "s3:PutLifecycleConfiguration",
      "s3:DeleteLifecycleConfiguration",
      "s3:PutBucketPublicAccessBlock",
      "s3:DeletePublicAccessBlock",
    ]
    resources = [aws_s3_bucket.tfstate.arn]
  }

  # Object-level delete actions are evaluated against object ARNs, not
  # bucket ARNs, so the bucket-level deny above doesn't cover them. Without
  # this statement, the deploy role's AmazonS3FullAccess attachment would
  # let it delete individual state objects (e.g., `main/terraform.tfstate`)
  # even though it can't delete the bucket itself. Terraform writes state
  # via PutObject (overwrite), never DeleteObject, so denying these is safe.
  statement {
    sid    = "DenyTfStateObjectDeletes"
    effect = "Deny"

    actions = [
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
    ]
    resources = ["${aws_s3_bucket.tfstate.arn}/*"]
  }

  statement {
    sid    = "DenyTfStateLockTableDestructive"
    effect = "Deny"

    actions = [
      "dynamodb:DeleteTable",
      "dynamodb:UpdateTable",
    ]
    resources = [aws_dynamodb_table.tflock.arn]
  }
}

resource "aws_iam_role_policy" "deploy_state_backend_deny" {
  name   = "deny-tfstate-mutation"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.deploy_state_backend_deny.json
}

# ---------- Plan role (pull_request only, read-only) ----------

data "aws_iam_policy_document" "plan_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [data.aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider_url}:aud"
      values   = [local.oidc_audience]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider_url}:sub"
      values   = ["repo:${var.github_repo}:pull_request"]
    }
  }
}

resource "aws_iam_role" "github_actions_plan" {
  name               = local.ci_role_name_plan
  assume_role_policy = data.aws_iam_policy_document.plan_assume_role.json
  description        = "Assumed by GitHub Actions on pull_request workflows. ReadOnlyAccess only - runs terraform plan -lock=false."

  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "plan_readonly" {
  role       = aws_iam_role.github_actions_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}
