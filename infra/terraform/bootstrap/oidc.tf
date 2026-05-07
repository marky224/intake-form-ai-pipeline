# GitHub Actions OIDC federation. AWS no longer validates the thumbprint for
# the official GitHub OIDC URL (the field is required by IAM but ignored at
# auth time), so we pin the historical thumbprints for documentation. See
# https://docs.aws.amazon.com/IAM/latest/UserGuide/id_roles_providers_create_oidc_verify-thumbprint.html
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://${local.oidc_provider_url}"
  client_id_list = [local.oidc_audience]
  thumbprint_list = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

data "aws_iam_policy_document" "ci_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_provider_url}:aud"
      values   = [local.oidc_audience]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_provider_url}:sub"
      values = [
        "repo:${var.github_repo}:ref:refs/heads/${var.github_main_branch}",
        "repo:${var.github_repo}:pull_request",
      ]
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name               = local.ci_role_name
  assume_role_policy = data.aws_iam_policy_document.ci_assume_role.json
  description        = "Assumed by GitHub Actions via OIDC. Permissions added per-PR as Phase 2 lands resources."

  max_session_duration = 3600
}

# No managed/inline policies attached yet. PR 1's CI usage exercises only
# sts:GetCallerIdentity (allowed by default for any IAM principal) and
# offline terraform fmt/validate. Subsequent PRs attach least-privilege
# policies as resources land (network, storage, database, edge).
