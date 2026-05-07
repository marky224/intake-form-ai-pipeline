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

  documents_bucket_name = "${var.project_name}-documents-${local.account_id}"
  artifacts_bucket_name = "${var.project_name}-artifacts-${local.account_id}"
}
