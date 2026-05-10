# CloudFront-fronting WAFv2 web ACL. Lives in us-east-1 globally because
# CLOUDFRONT-scope WAFs are only accepted in that region — pinned via
# the aliased edge provider.
#
# Rule layering (lower priority numbers evaluate first):
#   1. Rate-based: caps per-IP request volume to var.waf_rate_limit_per_5min
#      over a rolling 5-minute window. Above the limit, the source IP is
#      BLOCKed for the rule's evaluation window (~5 minutes).
#   2. UA byte-match: blocks requests whose User-Agent matches any
#      substring in var.blocked_user_agents (defaults cover the most
#      common scraping libraries) plus requests with no UA at all
#      (legitimate browsers always send one).
#   3. AWSManagedRulesCommonRuleSet: OWASP-flavored protections (XSS,
#      SQLi, traversal). Free, well-tuned, but does occasionally false-
#      positive on legitimate traffic. Matched + monitored here.
#   4. AWSManagedRulesKnownBadInputsRuleSet: blocks known-bad payloads
#      (Log4Shell, Spring4Shell, etc.). Free.
#   5. AWSManagedRulesAmazonIpReputationList: blocks source IPs from
#      AWS's threat-intel feed. Free, very low false-positive.
#
# Default action: ALLOW. Sampled requests + CloudWatch metrics on so
# blocked-traffic patterns surface in the console before the React UI
# lands in Phase 7.
resource "aws_wafv2_web_acl" "edge" {
  provider = aws.edge

  name        = "${var.project_name}-edge-waf"
  description = "CloudFront edge WAFv2 for the demo distribution: per-IP rate limit + UA block list + AWS-managed rule groups."
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "RateLimitPerIp"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit_per_5min
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-RateLimitPerIp"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "BlockKnownScrapeUserAgents"
    priority = 2

    action {
      block {}
    }

    statement {
      or_statement {
        # One byte-match per blocked UA substring.
        dynamic "statement" {
          for_each = var.blocked_user_agents
          content {
            byte_match_statement {
              field_to_match {
                single_header {
                  name = "user-agent"
                }
              }

              positional_constraint = "CONTAINS"
              search_string         = statement.value

              text_transformation {
                priority = 0
                type     = "LOWERCASE"
              }
            }
          }
        }

        # Empty / missing User-Agent header. SizeConstraintStatement on
        # length 0 catches both "header absent" (size = 0 by WAF
        # convention) and "header present but empty".
        statement {
          size_constraint_statement {
            field_to_match {
              single_header {
                name = "user-agent"
              }
            }

            comparison_operator = "EQ"
            size                = 0

            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-BlockKnownScrapeUserAgents"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedCommonRuleSet"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-AWSManagedCommonRuleSet"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedKnownBadInputs"
    priority = 4

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-AWSManagedKnownBadInputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedIpReputation"
    priority = 5

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesAmazonIpReputationList"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.project_name}-AWSManagedIpReputation"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${var.project_name}-edge-waf"
    sampled_requests_enabled   = true
  }
}
