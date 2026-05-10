# Phase 2 PR 6: cost guardrails.
#
# Daily-spend Budget at $5 (100% actual threshold) routed to an SNS
# topic. The breaker story: portfolio target is ~$10-15/month total;
# the budget firing means something unexpected happened the same day.
#
# Cost Anomaly Detection is intentionally NOT managed here. The
# account already has a Default-Services-Monitor (auto-suggested by
# AWS when the CE console is first opened) with a Default-Services-
# Subscription delivering anomaly alerts directly to mark's email at
# a $100/40% threshold - and AWS's per-account quota on dimensional
# monitors blocks creating a parallel monitor here. If the IMMEDIATE-
# frequency / $1-threshold pattern is wanted later, the path is to
# import the existing Default-Services-Monitor into this stack and
# either modify or replace its subscription. The
# CostExplorerAnomalyCreate / CostExplorerAnomalyManage statements
# in the bootstrap deploy role IAM (added in PR 6a) cover that
# future change without further bootstrap work.
#
# IAM scope for the resources here lives in the bootstrap stack
# (oidc.tf statements: BudgetsManage / BudgetsDescribeForRefresh /
# SnsTopicManage / SnsListAccountWide). Bootstrap re-applied as
# part of PR 6a.

locals {
  cost_alerts_topic_name = "${var.project_name}-cost-alerts"
  daily_budget_name      = "${var.project_name}-daily"
}

# SNS topic for cost alerts. Encrypted with the AWS-managed
# `alias/aws/sns` CMK rather than a project CMK - same threat model as
# the SSE-S3 default elsewhere in this stack: alert payloads carry
# threshold dollar amounts and budget names, no customer data, and the
# AWS-managed key is free vs project CMK overhead at $1/month +
# additional crypto IAM scope.
resource "aws_sns_topic" "cost_alerts" {
  # checkov:skip=CKV_AWS_26:SSE with the AWS-managed alias/aws/sns key matches the project's locked encryption posture for state and audit-trail buckets - same threat model (alert payloads carry threshold dollar amounts and budget names, no customer data); customer-managed CMK adds operational cost without changing the model.
  name              = local.cost_alerts_topic_name
  kms_master_key_id = "alias/aws/sns"
}

# Topic policy: allow AWS service principals (Budgets + Cost Anomaly
# Detection) to publish. Scoped via aws:SourceAccount AND, where AWS
# publishes a documented pattern, aws:SourceArn at account-wildcard
# level - both close the cross-account confused-deputy attack
# surface.
#
# Per-resource ARN scoping (e.g., scoping SourceArn to the specific
# `aws_budgets_budget.daily_spend.arn` or
# `aws_ce_anomaly_subscription.alerts.arn`) WOULD create a circular
# dependency: those resources reference the topic ARN as their
# notification target, and the topic policy would in turn reference
# their ARNs. AWS sidesteps this by documenting wildcard-at-account
# patterns - `arn:aws:budgets::<account>:*` for the Budgets leg -
# which depend only on `local.account_id`, no cycle.
#
# For Cost Anomaly Detection (`costalerts.amazonaws.com`), AWS hasn't
# published a verified SNS policy template I could confirm against
# their docs, so the costalerts leg uses aws:SourceAccount only. The
# CE Service Authorization Reference suggests the analog pattern
# would be `arn:aws:ce::<account>:anomalysubscription/*`, but
# applying it speculatively risks silently dropping anomaly
# notifications if the actual SourceArn AWS sends doesn't match.
# Tighten in a follow-up after verifying post-apply.
data "aws_iam_policy_document" "cost_alerts_topic" {
  statement {
    sid    = "AllowBudgetsServicePublish"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["budgets.amazonaws.com"]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.cost_alerts.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }

    # AWS-documented pattern for Budgets SNS policies: wildcard at the
    # account level (no per-budget ARN reference) so the policy depends
    # only on local.account_id - no circular dependency on the budget
    # resource that uses this topic.
    # https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-sns-policy.html
    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:budgets::${local.account_id}:*"]
    }
  }

  statement {
    sid    = "AllowCostAnomalyServicePublish"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["costalerts.amazonaws.com"]
    }

    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.cost_alerts.arn]

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_sns_topic_policy" "cost_alerts" {
  arn    = aws_sns_topic.cost_alerts.arn
  policy = data.aws_iam_policy_document.cost_alerts_topic.json
}

# Email/HTTPS subscriptions are intentionally NOT managed in Terraform.
# AWS SNS persists `aws_sns_topic_subscription.endpoint` in plaintext
# to the Terraform state file regardless of any `sensitive = true`
# flag on the source variable - that flag only masks CLI output, not
# state storage (state holds the rendered attribute values, not the
# variable references). For personal-email destinations, the residual
# exposure (state in the project S3 bucket, replicated through 90-day
# versioning, accessible to any principal with admin or deploy-role
# read on the state bucket) is undesirable even with strong bucket
# protections.
#
# Operational pattern: subscribe endpoints manually via the AWS console
# or `aws sns subscribe --topic-arn <arn> --protocol email --notification-endpoint <addr>`
# post-apply. The topic + budget + anomaly subscription resources here
# all reference the topic ARN, which is stable and IaC-defined; only
# the human-facing endpoint addresses live outside Terraform.
#
# Non-PII subscription types (Lambda, SQS, account-internal HTTPS
# webhooks) CAN be IaC-managed safely since their endpoints are
# technical identifiers rather than personal contact info — add those
# as `aws_sns_topic_subscription` resources here if/when they're
# needed.

# Daily-spend budget at $5 limit. Tag-filtered to project-tagged
# spend only, so other projects in this AWS account stay outside this
# budget's scope. Caveat: the filter excludes spend tagged with a
# different Project value AND untagged spend (e.g., AWS-internal
# overhead) - that's correct at portfolio scale where the project's
# explicit-tag spend is the actionable surface.
#
# Single ACTUAL notification at 100% threshold. AWS Budgets only
# supports ACTUAL notifications on DAILY budgets - FORECASTED is
# reserved for monthly/quarterly time units (per the AWS API
# contract; CreateNotification returns InvalidParameterException
# "this budget time unit: DAILY only supports notification type as
# ACTUAL" if FORECASTED is attempted on a DAILY budget). The
# notification routes to the cost-alerts SNS topic; the topic
# policy above grants budgets.amazonaws.com the sns:Publish
# permission needed to deliver.
resource "aws_budgets_budget" "daily_spend" {
  name         = local.daily_budget_name
  budget_type  = "COST"
  time_unit    = "DAILY"
  limit_amount = "5.0"
  limit_unit   = "USD"

  cost_filter {
    name = "TagKeyValue"
    # AWS Budgets TagKeyValue format is `user:<TagKey>$<TagValue>` (literal
    # `$` separator). Built via format() because HCL's `$${...}` escape
    # produces a literal `${...}` token, not "literal $ followed by
    # interpolation" - direct string interpolation can't produce this
    # shape without help from a function.
    values = [
      format("user:Project$%s", var.project_name),
    ]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alerts.arn]
  }

  depends_on = [aws_sns_topic_policy.cost_alerts]
}
