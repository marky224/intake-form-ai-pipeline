variable "aws_region" {
  description = "AWS region for the main stack. Must match the bootstrap region."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Project identifier used for resource naming and the Project tag."
  type        = string
  default     = "intake-form-ai-pipeline"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC. See modules/network/variables.tf for the per-subnet allocation plan."
  type        = string
  default     = "10.0.0.0/16"
}

variable "demo_domain" {
  description = "Public DNS name for the demo's CloudFront distribution. Used for the ACM cert SAN, the distribution alias, and the Route 53 alias records. Lives under the Mark-owned hosted zone identified by route53_hosted_zone_id."
  type        = string
  default     = "ai-intake.markandrewmarquez.com"

  validation {
    condition     = length(var.demo_domain) >= 1 && length(var.demo_domain) <= 253
    error_message = "demo_domain must be 1-253 characters per DNS hostname spec."
  }

  validation {
    # Each DNS label: starts and ends with alphanumeric, internal hyphens
    # only (no leading/trailing hyphen), 1-63 chars. At least two labels
    # required (no bare hostname). Lookahead-free for Terraform's
    # RE2-based regex engine.
    condition     = can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", var.demo_domain))
    error_message = "demo_domain must be a valid lowercase DNS name (per-label rules: 1-63 chars, alphanumeric with internal hyphens only, no empty labels, at least one dot)."
  }
}

variable "route53_hosted_zone_id" {
  description = "Route 53 hosted zone ID for demo_domain's parent zone. Bound into the deploy role's Route53Manage IAM scope (see bootstrap/variables.tf for the matching default)."
  type        = string
  default     = "Z04568022MZ21HXK15I1D"

  validation {
    condition     = can(regex("^Z[A-Z0-9]+$", var.route53_hosted_zone_id))
    error_message = "route53_hosted_zone_id must be a Route 53 zone ID (uppercase alphanumeric starting with Z)."
  }
}

variable "waf_rate_limit_per_5min" {
  description = "Per-IP request limit over a rolling 5-minute window enforced by the CloudFront WAFv2 rate-based rule. Above this, the source IP gets BLOCKed for the rule's evaluation window. 100 req / 5 min comfortably covers the recruiter-clicking-around demo flow while shutting down basic scraping."
  type        = number
  default     = 100

  validation {
    # Bounds reflect the August 2024 AWS WAF update: minimum lowered from
    # 100 to 10, maximum raised to 2,000,000,000.
    condition     = var.waf_rate_limit_per_5min >= 10 && var.waf_rate_limit_per_5min <= 2000000000
    error_message = "WAF rate-based rules require a limit between 10 and 2,000,000,000."
  }
}

variable "blocked_user_agents" {
  description = "User-Agent header substrings to BLOCK at the CloudFront edge via WAFv2 byte-match. Defaults cover the most common scraping libraries; empty UA is also blocked because legitimate browsers always send one."
  type        = list(string)
  default = [
    "python-requests",
    "curl",
    "scrapy",
    "wget",
  ]

  validation {
    condition = alltrue([
      for ua in var.blocked_user_agents : trimspace(ua) != ""
    ]) && length(distinct(var.blocked_user_agents)) == length(var.blocked_user_agents)
    error_message = "blocked_user_agents entries must be non-empty (after whitespace trim) and unique — duplicate or blank entries waste WAF rule budget."
  }
}

variable "alert_email" {
  description = "Email address subscribed to the cost-alerts SNS topic. Receives budget threshold alerts and Cost Anomaly Detection notifications. Set out-of-band via .tfvars or TF_VAR_alert_email env var (no default; kept out of the public repo). First apply triggers an AWS confirmation email; subscription stays in PendingConfirmation until the link is clicked."
  type        = string
  sensitive   = true

  validation {
    # Simplified syntactic check: at least one non-@ non-whitespace char,
    # an @, at least one domain label with a dot. Catches typos (bare
    # local-part, missing TLD, embedded whitespace) without trying to
    # enforce the full RFC 5322 grammar (which Terraform's RE2 engine
    # can't anyway).
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be a syntactically valid email (something like name@domain.tld)."
  }
}
