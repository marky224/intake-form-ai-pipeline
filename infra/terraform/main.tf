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

  # CloudTrail trail name + log file prefix. The trail itself lives in
  # cloudtrail.tf; reused here so the bucket policy on cloudtrail-logs
  # can scope its grant to this trail's ARN via aws:SourceArn.
  cloudtrail_trail_name = "${var.project_name}-trail"
  cloudtrail_trail_arn  = "arn:aws:cloudtrail:${var.aws_region}:${local.account_id}:trail/${local.cloudtrail_trail_name}"
}
