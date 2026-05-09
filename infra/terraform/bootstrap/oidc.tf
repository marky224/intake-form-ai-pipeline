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
# DEPLOY role: trusts pushes to main only. Has AmazonVPCFullAccess
# managed plus a scoped inline allow for the Terraform state backend and
# project S3 buckets (Phase 2 PR 3), and an inline deny on the state
# bucket and lock table so a buggy apply can't recursively nuke its own
# backend.
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
  description        = "Assumed by GitHub Actions on push to main. AmazonVPCFullAccess plus scoped inline allow for the Terraform state backend, project S3 buckets, project Aurora resources (RDS/KMS/Secrets Manager), with an inline deny guarding the state bucket and lock table."

  max_session_duration = 3600
}

resource "aws_iam_role_policy_attachment" "deploy_vpc" {
  role       = aws_iam_role.github_actions_deploy.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonVPCFullAccess"
}

# Scoped S3 + DynamoDB allow for the deploy role. Replaces the previous
# AmazonS3FullAccess managed-policy attachment so the deploy role can no
# longer touch S3 buckets belonging to Mark's other projects in this
# account (the AWS account hosts more than just this pipeline).
#
# Three concerns:
#   1. State bucket — read/write/list state objects so terraform init,
#      plan, and apply can fetch and overwrite the state file. No Delete
#      perms (Terraform overwrites via PutObject; the deny policy below
#      blocks deletes anyway).
#   2. Project buckets (documents + artifacts) — full-manage so the main
#      stack's storage module can create the buckets and configure
#      versioning, encryption, lifecycle, public-access-block, and the
#      TLS-only bucket policy. Wildcard suffix matches the
#      `<account_id>` form in main.tf and leaves room for per-environment
#      buckets without re-scoping IAM.
#   3. DynamoDB lock table — GetItem/PutItem/DeleteItem to acquire and
#      release the state lock during apply. DescribeTable is required by
#      some Terraform versions to verify the lock table exists at init.
#      Without this, CI apply fails before it can even talk to S3.
data "aws_iam_policy_document" "deploy_scoped_allow" {
  statement {
    sid       = "TfStateBucketList"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.tfstate.arn]
  }

  statement {
    sid    = "TfStateObjectReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.tfstate.arn}/*"]
  }

  statement {
    sid       = "ProjectBucketsFullManage"
    effect    = "Allow"
    actions   = ["s3:*"]
    resources = local.project_bucket_arns
  }

  statement {
    sid    = "TfStateLockItemOps"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [aws_dynamodb_table.tflock.arn]
  }

  # Gap in `AmazonVPCFullAccess` v13: the managed policy includes
  # `ec2:DescribeAddresses` but not `ec2:DescribeAddressesAttribute`.
  # AWS provider 6.x reads that newer attribute on every EIP refresh
  # to populate the `domain_name` field, so the NAT-gateway EIP errors
  # 403 on first apply without this. The action does not support
  # resource-level permissions, so the resource has to be `*`.
  statement {
    sid       = "Ec2EipDescribeAttribute"
    effect    = "Allow"
    actions   = ["ec2:DescribeAddressesAttribute"]
    resources = ["*"]
  }

  # Phase 2 PR 4: Aurora Serverless v2 cluster + KMS CMK + Secrets
  # Manager secret for the master password. Inline scope rather than
  # `AmazonRDSFullAccess` so this role can't reach RDS resources
  # belonging to Mark's other projects in this account.
  statement {
    sid    = "AuroraResourceManage"
    effect = "Allow"
    actions = [
      "rds:*",
    ]
    resources = local.project_rds_arns
  }

  # Provider 6.x runs RDS describe calls on every plan/apply refresh,
  # and several of them (e.g. DescribeDBClusterEndpoints) list
  # account-wide rather than accepting an ARN. Without this, the first
  # main-stack apply 403s on the post-create cluster refresh — same
  # failure mode as PR #12's `ec2:DescribeAddressesAttribute` patch.
  # Read-only blast radius: the deploy role can list RDS metadata
  # across the account. Acceptable since the account is Mark's.
  statement {
    sid    = "RdsDescribeForRefresh"
    effect = "Allow"
    actions = [
      "rds:Describe*",
      "rds:ListTagsForResource",
    ]
    resources = ["*"]
  }

  # KMS CMK management for the Aurora cluster's encryption key.
  # KMS key ARNs are UUID-based, so name-prefix scoping does not
  # apply. Tag-condition scoping does not work either — most of these
  # actions are evaluated either against a key that does not yet exist
  # (CreateKey) or in a context that does not surface tag conditions
  # reliably. The deploy role gets account-wide management of CMKs;
  # the key policy on the CMK itself is the second line of defense
  # (limits which principals can use the key for encrypt/decrypt).
  statement {
    sid    = "KmsKeyManage"
    effect = "Allow"
    actions = [
      "kms:CreateKey",
      "kms:CreateAlias",
      "kms:DeleteAlias",
      "kms:UpdateAlias",
      "kms:ListAliases",
      "kms:Describe*",
      "kms:Get*",
      "kms:List*",
      "kms:Put*",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:ScheduleKeyDeletion",
      "kms:CancelKeyDeletion",
      "kms:EnableKey",
      "kms:EnableKeyRotation",
      "kms:DisableKey",
      "kms:DisableKeyRotation",
    ]
    resources = ["*"]
  }

  # Secrets Manager scope for the Aurora master-password secret.
  # Path-prefixed under `<project>/` so other projects in the account
  # remain out of reach.
  statement {
    sid    = "SecretsManagerManage"
    effect = "Allow"
    actions = [
      "secretsmanager:*",
    ]
    resources = local.project_secret_arns
  }
}

resource "aws_iam_role_policy" "deploy_scoped_allow" {
  name   = "scoped-deploy-allow"
  role   = aws_iam_role.github_actions_deploy.id
  policy = data.aws_iam_policy_document.deploy_scoped_allow.json
}

# Block deploy role from destroying or weakening the Terraform backend.
# Bootstrap-stack changes are applied locally with admin creds, never via
# CI; this deny scope only prevents a buggy main-stack apply from
# recursively nuking its own backend or removing the security posture
# that protects the state bucket. The scoped allow above grants the
# normal init/apply ops (Get/Put/List on state objects, GetItem/PutItem/
# DeleteItem on lock items); this deny is defense-in-depth, since the
# scoped allow also doesn't grant the destructive actions blocked here.
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
