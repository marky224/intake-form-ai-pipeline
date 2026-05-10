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
  description        = "Assumed by GitHub Actions on push to main. AmazonVPCFullAccess plus scoped inline allow for the Terraform state backend, project S3 buckets (incl. access-logs and cloudtrail-logs targets), project Aurora resources (RDS / KMS / Secrets Manager incl. AWS-managed master password / CloudWatch log exports), project CloudTrail trails, project CloudFront distributions and ACM certificates, project WAFv2 web ACLs, the project's Route 53 hosted zone, and CloudWatch Logs delivery for CloudFront access logs, with an inline deny guarding the state bucket and lock table."

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
  # checkov:skip=CKV_AWS_109:Project-bucket statements use s3:* on a name-scoped resource list (local.project_bucket_arns); some IAM-action surfaces (iam:PassRole, kms:CreateGrant) genuinely require admin-equivalent perms on RDS-managed objects that don't expose narrower resource shapes.
  # checkov:skip=CKV_AWS_111:Same as CKV_AWS_109 — write-action statements scope via project_*_arns locals; AWS APIs that don't support resource-level perms (e.g., describe-* across regions) keep `*` resources but condition on action taxonomy.
  # checkov:skip=CKV_AWS_356:Statements using resources=["*"] are limited to actions that AWS does not support resource-level permissions for (a known IAM limitation tracked in https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_aws-services-that-work-with-iam.html). All resource-restrictable actions are constrained.
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
  #
  # Cryptographic actions (Encrypt/Decrypt/GenerateDataKey/ReEncrypt*)
  # plus CreateGrant/RetireGrant/RevokeGrant are required by the
  # principal calling `RDS.CreateDBCluster` when storage_encrypted is
  # true and kms_key_id references a customer-managed key. Per AWS
  # docs, the calling principal needs IAM permissions on the key
  # in addition to the key policy granting access (key policy allows
  # the account-root delegation, which only takes effect when IAM
  # also allows the action). Without these, cluster create fails with
  # "KMSKeyNotAccessibleFault: ... isn't accessible by the current
  # user."
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
      "kms:Encrypt",
      "kms:Decrypt",
      "kms:GenerateDataKey",
      "kms:GenerateDataKeyWithoutPlaintext",
      "kms:ReEncrypt*",
      "kms:CreateGrant",
      "kms:RetireGrant",
      "kms:RevokeGrant",
    ]
    resources = ["*"]
  }

  # Secrets Manager scope for explicit project-namespaced secrets.
  # Path-prefixed under `<project>/` so other projects in the account
  # remain out of reach. Phase 2 PR 4b initially used this for the
  # explicit Aurora master-password secret; PR 4c swapped that for
  # AWS-managed master passwords (see the next two statements), so
  # this scope now only covers any future explicit secrets the
  # project creates outside the RDS-managed flow.
  statement {
    sid    = "SecretsManagerManage"
    effect = "Allow"
    actions = [
      "secretsmanager:*",
    ]
    resources = local.project_secret_arns
  }

  # Phase 2 PR 4c — Tier 1 security improvement #1: AWS-managed master
  # password for the Aurora cluster (`manage_master_user_password = true`
  # on aws_rds_cluster). Per the AWS docs, the principal calling
  # CreateDBCluster needs `secretsmanager:CreateSecret` so RDS can
  # provision the secret on its behalf. CreateSecret accepts
  # resource-level scoping against the requested secret ARN; AWS-owned
  # secrets always start with `rds!cluster-`, so we can scope here
  # against the same ARN pattern used by AuroraManagedSecretManage.
  # This is tighter than the `*` scope the AWS docs example shows by
  # default (cf. CodeRabbit feedback on PR #18).
  statement {
    sid    = "AuroraManagedSecretCreate"
    effect = "Allow"
    actions = [
      "secretsmanager:CreateSecret",
    ]
    resources = local.project_managed_secret_arns
  }

  # Tag and rotate the AWS-managed Aurora master secret. Both actions
  # support resource-level perms; scope to the `rds!cluster-*` AWS-owned
  # namespace so other RDS clusters in the account stay out of reach.
  # The `aws:ResourceTag/Project` condition narrows further: any AWS-
  # managed Aurora secret already in this account that doesn't carry the
  # project tag (e.g., from other projects sharing the account) is
  # excluded. RDS auto-propagates the cluster's tags onto its managed
  # secret, so this project's secret keeps the `Project` tag set by
  # `aws_rds_cluster.tags` and remains in scope. Verified post-PR-#16
  # apply that the tag does propagate. CreateSecret cannot use this
  # condition (the resource doesn't exist yet at evaluation time), so
  # the tag-condition tightening applies only to Manage actions.
  statement {
    sid    = "AuroraManagedSecretManage"
    effect = "Allow"
    actions = [
      "secretsmanager:TagResource",
      "secretsmanager:RotateSecret",
    ]
    resources = local.project_managed_secret_arns

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_name]
    }
  }

  # Phase 2 PR 4c — Tier 1 security improvement #3: CloudWatch log
  # exports for the Aurora cluster. Terraform manages the
  # aws_cloudwatch_log_group resource at
  # `/aws/rds/cluster/<cluster>/postgresql`; RDS itself populates the
  # streams. Resource-level scope is the project-prefixed log-group ARN
  # (and `:*` suffix for streams within). AssociateKmsKey /
  # DisassociateKmsKey support log-group encryption under our existing
  # CMK; the key policy already allows account root admin so the
  # cross-service flow works without an explicit Logs-service grant.
  statement {
    sid    = "LogGroupForAuroraExports"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:DeleteRetentionPolicy",
      "logs:TagResource",
      "logs:UntagResource",
      "logs:ListTagsForResource",
      "logs:AssociateKmsKey",
      "logs:DisassociateKmsKey",
    ]
    resources = local.project_log_group_arns
  }

  # Provider 6.x runs `logs:DescribeLogGroups` on every refresh and the
  # action doesn't accept resource-level constraints (lists across the
  # account by name prefix). Read-only blast radius: the deploy role
  # can list log-group metadata across the account. Same shape as the
  # RdsDescribeForRefresh shim above.
  statement {
    sid    = "LogsDescribeForRefresh"
    effect = "Allow"
    actions = [
      "logs:Describe*",
    ]
    resources = ["*"]
  }

  # Phase 2 PR 4 closeout: CloudTrail trail management for the project's
  # audit trail (S3 data events on documents/artifacts/tfstate buckets +
  # RDS management events). Resource-level perms supported on trail ARNs;
  # scope to the project name prefix so the deploy role can't touch other
  # trails in the account.
  statement {
    sid    = "CloudTrailManage"
    effect = "Allow"
    actions = [
      "cloudtrail:*",
    ]
    resources = [
      "arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/${var.project_name}-*",
    ]
  }

  # Provider 6.x reads several CloudTrail status/selector APIs on every
  # refresh, and most of them list account-wide rather than accepting a
  # trail ARN. Same shape as the RdsDescribeForRefresh and
  # LogsDescribeForRefresh shims above. Read-only blast radius: the
  # deploy role can list trail metadata across the account.
  statement {
    sid    = "CloudTrailDescribeForRefresh"
    effect = "Allow"
    actions = [
      "cloudtrail:Describe*",
      "cloudtrail:Get*",
      "cloudtrail:List*",
    ]
    resources = ["*"]
  }

  # Phase 2 PR 5: edge protection (CloudFront + WAFv2 + ACM + Route 53 +
  # CloudFront v2 access-log delivery).
  #
  # CloudFront is split Create / Manage / Describe to follow the PR #28
  # pattern. Create-flavored actions cannot evaluate `aws:ResourceTag`
  # because the resource does not exist at evaluation time, so they sit
  # on `*` without a tag condition. Manage-flavored actions (Update*,
  # Delete*, Tag*, Untag*) operate on existing resources and do support
  # ResourceTag, so they get the tighter `Project` tag condition. The
  # describe shim mirrors RdsDescribeForRefresh / LogsDescribeForRefresh
  # / CloudTrailDescribeForRefresh: read-only across the account, which
  # is acceptable since the account is Mark's. CloudFront resource ARNs
  # are UUID-based, so name-prefix scoping does not apply (cf. KMS
  # precedent above).
  statement {
    sid    = "CloudFrontCreate"
    effect = "Allow"
    actions = [
      "cloudfront:Create*",
      "cloudfront:CopyDistribution",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "CloudFrontManage"
    effect = "Allow"
    actions = [
      "cloudfront:Update*",
      "cloudfront:Delete*",
      "cloudfront:TagResource",
      "cloudfront:UntagResource",
      "cloudfront:Associate*",
      "cloudfront:Disassociate*",
      "cloudfront:Publish*",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_name]
    }
  }

  statement {
    sid    = "CloudFrontDescribeForRefresh"
    effect = "Allow"
    actions = [
      "cloudfront:Get*",
      "cloudfront:List*",
    ]
    resources = ["*"]
  }

  # ACM certificate management for the CloudFront distribution's TLS
  # cert (`ai-intake.<apex>`). Resource ARNs are UUID-based, so the
  # resource scope is the account-region certificate namespace; the
  # tag condition on the Manage statement narrows mutating actions
  # to project-tagged certs. RequestCertificate cannot accept a tag
  # condition (resource doesn't exist yet) and ACM does not accept tags
  # inline on RequestCertificate, so the create stays unconditioned.
  statement {
    sid    = "AcmCreate"
    effect = "Allow"
    actions = [
      "acm:RequestCertificate",
      "acm:ImportCertificate",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "AcmManage"
    effect = "Allow"
    actions = [
      "acm:DeleteCertificate",
      "acm:UpdateCertificateOptions",
      "acm:RenewCertificate",
      "acm:AddTagsToCertificate",
      "acm:RemoveTagsFromCertificate",
      "acm:ResendValidationEmail",
    ]
    resources = local.project_acm_arns

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.project_name]
    }
  }

  statement {
    sid    = "AcmDescribeForRefresh"
    effect = "Allow"
    actions = [
      "acm:Describe*",
      "acm:Get*",
      "acm:List*",
    ]
    resources = ["*"]
  }

  # WAFv2 web ACL management. Names are user-controlled, so name-prefix
  # scoping bounds the deploy role to project-owned web ACLs, regex
  # pattern sets, and IP sets without needing a tag condition.
  # CLOUDFRONT-scope WAF lives in us-east-1 globally — the ARN's
  # `global` segment in `local.project_wafv2_arns` reflects that.
  statement {
    sid    = "WafV2Manage"
    effect = "Allow"
    actions = [
      "wafv2:*",
    ]
    resources = local.project_wafv2_arns
  }

  # Provider 6.x runs `wafv2:List*` and `wafv2:GetWebACLForResource`
  # account-wide on refresh; the Get/Describe call against the project's
  # own ARN is already covered by WafV2Manage's wildcard, but the List*
  # surface needs `*` because lists don't accept resource constraints.
  statement {
    sid    = "WafV2DescribeForRefresh"
    effect = "Allow"
    actions = [
      "wafv2:List*",
      "wafv2:GetWebACLForResource",
    ]
    resources = ["*"]
  }

  # Route 53 record-set management on the project's public hosted zone.
  # The zone itself is created out-of-band on Mark's apex domain; this
  # statement scopes record changes to that zone only, so the deploy
  # role cannot mutate other zones in the account. Read access is
  # account-wide for refresh shims (same shape as RdsDescribeForRefresh).
  statement {
    sid    = "Route53RecordManage"
    effect = "Allow"
    actions = [
      "route53:ChangeResourceRecordSets",
    ]
    resources = local.project_route53_zone_arns
  }

  statement {
    sid    = "Route53DescribeForRefresh"
    effect = "Allow"
    actions = [
      "route53:Get*",
      "route53:List*",
    ]
    resources = ["*"]
  }

  # CloudFront v2 access-log delivery wiring. The v2 path uses CloudWatch
  # Logs Delivery primitives (DeliverySource + DeliveryDestination +
  # Delivery) to route distribution logs to the project's access-logs
  # S3 bucket under `cloudfront/`. These actions do not accept resource
  # ARNs (the delivery, source, and destination resources are created
  # by these actions; refreshing them needs account-wide list).
  # `logs:Describe*` and `logs:Get*` overlap with LogsDescribeForRefresh
  # above, so they are not duplicated here.
  statement {
    sid    = "LogDeliveryManage"
    effect = "Allow"
    actions = [
      "logs:CreateDelivery",
      "logs:CreateDeliverySource",
      "logs:CreateDeliveryDestination",
      "logs:UpdateDeliveryConfiguration",
      "logs:DeleteDelivery",
      "logs:DeleteDeliverySource",
      "logs:DeleteDeliveryDestination",
      "logs:PutDeliverySource",
      "logs:PutDeliveryDestination",
      "logs:PutDeliveryDestinationPolicy",
      "logs:DeleteDeliveryDestinationPolicy",
      "logs:GetDelivery",
      "logs:GetDeliverySource",
      "logs:GetDeliveryDestination",
      "logs:GetDeliveryDestinationPolicy",
      "logs:ListDeliveries",
      "logs:ListDeliverySources",
      "logs:ListDeliveryDestinations",
      "logs:TagDelivery",
      "logs:UntagDelivery",
    ]
    resources = ["*"]
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
