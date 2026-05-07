# Partial backend configuration. Concrete values are provided at init time
# via -backend-config=.tfbackend (local) or via -backend-config=... CLI
# flags reading repo variables (CI). The state lives in the same bucket as
# the bootstrap stack but under a different key.
#
# This file MUST stay as a partial config. Hardcoding the bucket name here
# would either commit the AWS account ID to the public repo or break
# forks that have a different account.
terraform {
  backend "s3" {}
}
