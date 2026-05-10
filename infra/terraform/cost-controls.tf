# Phase 2 PR 6: cost guardrails.
#
# Daily-spend Budget at $5 (100% actual + 80% forecasted thresholds),
# Cost Anomaly Detection on AWS-services dimension with $1 threshold,
# both routed to a single SNS topic with email subscription. The
# breaker story: portfolio target is ~$10-15/month total; either
# alarm firing means something unexpected happened the same day.
#
# IAM scope for these resources lives in the bootstrap stack
# (oidc.tf statements: BudgetsManage / BudgetsDescribeForRefresh /
# CostExplorerAnomalyCreate / CostExplorerAnomalyManage / SnsTopicManage
# / SnsListAccountWide). Bootstrap re-applied as part of PR 6a.

locals {
  cost_alerts_topic_name       = "${var.project_name}-cost-alerts"
  daily_budget_name            = "${var.project_name}-daily"
  ce_anomaly_monitor_name      = "${var.project_name}-services-anomalies"
  ce_anomaly_subscription_name = "${var.project_name}-anomaly-alerts"
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
# Detection) to publish. Scoped via aws:SourceAccount to close the
# cross-account confused-deputy attack surface.
#
# aws:SourceArn would tighten further (per-budget / per-subscription),
# but the budget's notification block and the anomaly subscription's
# subscriber address both reference this topic's ARN - making the
# topic policy depend on those resource ARNs would force a two-phase
# apply (topic without policy, then resources, then policy attaches).
# Single-account scoping closes the meaningful risk; document the
# trade-off here so a future reader doesn't tighten without thinking
# through the apply ordering.
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
# Two notifications: 100% actual + 80% forecasted, both routed to the
# cost-alerts SNS topic. Budgets resolves the SNS topic ARN against
# this account at apply time; the topic policy above grants
# budgets.amazonaws.com the sns:Publish permission needed to deliver.
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

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alerts.arn]
  }

  depends_on = [aws_sns_topic_policy.cost_alerts]
}

# Cost Anomaly Detection: dimensional monitor on SERVICE. Tracks
# anomalous spend across all AWS services. At portfolio scale the top
# expected services are RDS / NAT / CloudFront / WAF; anomalies in any
# of those surface here regardless of which Project tag they carry,
# complementing the project-tag-filtered Budget above.
resource "aws_ce_anomaly_monitor" "services" {
  name              = local.ce_anomaly_monitor_name
  monitor_type      = "DIMENSIONAL"
  monitor_dimension = "SERVICE"
}

# IMMEDIATE delivery: anomalies above $1 absolute impact notify the
# SNS topic as soon as detected. At portfolio scale a $1 anomaly is
# meaningful (~10% of monthly baseline); the breaker pattern wants to
# know immediately, not in a daily digest.
resource "aws_ce_anomaly_subscription" "alerts" {
  name      = local.ce_anomaly_subscription_name
  frequency = "IMMEDIATE"

  monitor_arn_list = [aws_ce_anomaly_monitor.services.arn]

  subscriber {
    type    = "SNS"
    address = aws_sns_topic.cost_alerts.arn
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      values        = ["1"]
      match_options = ["GREATER_THAN_OR_EQUAL"]
    }
  }

  depends_on = [aws_sns_topic_policy.cost_alerts]
}
