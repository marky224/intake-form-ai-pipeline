provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = "demo"
      ManagedBy   = "terraform"
      Owner       = "mark"
      Stack       = "main"
    }
  }
}

# Aliased provider for CloudFront-edge resources that AWS forces into
# us-east-1 regardless of the project's primary region: ACM certificates
# attached to a CloudFront distribution, and CLOUDFRONT-scope WAFv2 web
# ACLs (and their regex/IP sets). Pinning the literal here keeps these
# resources correct even if `var.aws_region` is ever overridden.
# Default-tags inherit the Stack=main posture so downstream resources
# remain attributable.
provider "aws" {
  alias  = "edge"
  region = "us-east-1"

  default_tags {
    tags = {
      Project     = var.project_name
      Environment = "demo"
      ManagedBy   = "terraform"
      Owner       = "mark"
      Stack       = "main"
    }
  }
}

data "aws_caller_identity" "current" {}

# Pick the first 3 availability zones that don't require explicit opt-in.
# Excludes Local Zones, Wavelength Zones, and AZs in opt-in regions.
data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  azs        = slice(data.aws_availability_zones.available.names, 0, 3)

  documents_bucket_name       = "${var.project_name}-documents-${local.account_id}"
  artifacts_bucket_name       = "${var.project_name}-artifacts-${local.account_id}"
  access_logs_bucket_name     = "${var.project_name}-access-logs-${local.account_id}"
  cloudtrail_logs_bucket_name = "${var.project_name}-cloudtrail-logs-${local.account_id}"
  state_bucket_name           = "${var.project_name}-tfstate-${local.account_id}"

  # CloudTrail trail name + S3 key prefix + log file prefix. The trail
  # itself lives in cloudtrail.tf; reused here so the bucket policy on
  # cloudtrail-logs can scope its grant to this trail's ARN via
  # aws:SourceArn AND so the PutObject Resource pattern matches the
  # actual CloudTrail object key (which embeds the prefix).
  #
  # CloudTrail's pre-create policy validation rejects the policy if the
  # PutObject Resource doesn't cover the prefix path — see
  # https://docs.aws.amazon.com/awscloudtrail/latest/userguide/create-s3-bucket-policy-for-cloudtrail.html
  cloudtrail_trail_name      = "${var.project_name}-trail"
  cloudtrail_trail_arn       = "arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.cloudtrail_trail_name}"
  cloudtrail_trail_s3_prefix = "cloudtrail"

  # Phase 2 PR 5b — edge protection.
  #
  # Landing bucket is the CloudFront origin: serves the placeholder
  # index.html + robots.txt today, swaps to the React review UI in
  # Phase 7. Same name shape as documents/artifacts so per-environment
  # variants land without re-scoping IAM.
  landing_bucket_name = "${var.project_name}-landing-${local.account_id}"

  # CloudFront v2 access logs delivery prefix on the access-logs bucket.
  # Same lesson as `cloudtrail_trail_s3_prefix` above: promoted to a
  # local so the delivery destination's S3-suffix and the access-logs
  # bucket-policy PutObject Resource ARN can't drift (cf. PR #27, where
  # CloudTrail's prefix-vs-bucket-policy mismatch caused the trail
  # CreateTrail call to fail validation).
  cloudfront_log_s3_prefix = "cloudfront"
}
