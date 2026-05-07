# Partial backend configuration. Concrete values are provided at init time
# via -backend-config=.tfbackend (see README.md for first-time setup).
#
# This file MUST stay as a partial config. Hardcoding the bucket name here
# would either commit the AWS account ID to the public repo or break forks
# that have a different account.
terraform {
  backend "s3" {}
}
