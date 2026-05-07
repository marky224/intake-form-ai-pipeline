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

data "aws_iam_policy_document" "ci_assume_role" {
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
#
# TODO (Phase 2 PR 2): when write policies first attach to this role, split
# into two roles to enforce a privilege boundary:
#   - intake-form-ai-pipeline-github-actions-deploy: main-only trust,
#     write perms for terraform apply on push-to-main.
#   - intake-form-ai-pipeline-github-actions-plan: main + pull_request
#     trust, read-only perms for terraform plan on PRs.
# The single-role + pull_request-trust shape here is safe today only because
# no policies are attached. Forked PRs cannot assume this role regardless
# (id-token: write downgrades to read for fork PR workflows), but same-repo
# PR branches can — and will gain real capability the moment policies land.
