# Terraform bootstrap

One-time stack that creates the foundation everything else depends on:

- S3 bucket for remote Terraform state (versioned, encrypted, public-access-blocked, lifecycle-managed)
- DynamoDB table for state locking (PAY_PER_REQUEST, encrypted, point-in-time recovery)
- IAM role assumable from this repo's `main` branch and pull-request workflows via GitHub Actions OIDC

The GitHub OIDC provider (`token.actions.githubusercontent.com`) is **not** owned by this stack. AWS allows only one OIDC provider per URL per account, so it's shared across every project that uses GitHub Actions in the account. This stack references it via a data source. If the provider doesn't yet exist in the account, create it once out-of-band before applying this stack:

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

(AWS no longer validates the thumbprint for the official GitHub OIDC URL, but the field is required.)

After this stack is applied and its state migrated to the bucket it just created, every other Terraform stack in `infra/terraform/` uses the same backend.

## First-time setup

Prerequisites: AWS CLI authenticated as a principal with `iam:*`, `s3:*`, `dynamodb:*` permissions on the target account; Terraform `~> 1.14` installed; GitHub OIDC provider already created in the AWS account (see top of README).

The chicken-and-egg dance: the stack creates its own state backend, so on first apply there's no backend yet. Modern Terraform (1.6+) requires backend init even for `apply`, so `init -backend=false` alone isn't enough — the `backend "s3" {}` block in `backend.tf` must be temporarily commented out for the first apply, then restored for the migrate.

```bash
cd infra/terraform/bootstrap

# 1. Comment out the `backend "s3" {}` block in backend.tf (do not commit).

# 2. Initialise with the default local backend
terraform init

# 3. Apply — creates state bucket, lock table, CI role
terraform apply

# 4. Restore the `backend "s3" {}` block in backend.tf.

# 5. Build .tfbackend from the apply output
terraform output -raw tfbackend_example > .tfbackend

# 6. Re-init with the S3 backend, migrating the local state file
terraform init -migrate-state -backend-config=.tfbackend
# Answer 'yes' when prompted to migrate state.

# 7. Verify state migrated cleanly
terraform plan   # should report "No changes"

# 8. Delete the now-redundant local state file
rm terraform.tfstate terraform.tfstate.backup
```

Subsequent runs from any machine: `terraform init -backend-config=.tfbackend` once, then normal `plan` / `apply`.

## After bootstrap

Set the OIDC role ARN as a repo-level GitHub Actions variable so CI can assume it:

```bash
gh variable set AWS_OIDC_ROLE_ARN --body "$(terraform output -raw ci_role_arn)"
```

The CI workflow's `terraform` job reads `vars.AWS_OIDC_ROLE_ARN` and exchanges its OIDC token for short-lived AWS credentials.

## What's intentionally not here

- **No managed/inline policies on the CI role yet.** PR 1's CI usage exercises only `sts:GetCallerIdentity` (always allowed) and offline `terraform fmt`/`validate`. Subsequent Phase 2 PRs (network → storage → database → edge → cost guards) attach least-privilege policies as resources land. This avoids granting write access ahead of need.
- **No `prevent_destroy = false` escape hatch.** The state bucket and lock table both have `prevent_destroy = true`. Tearing them down requires editing `main.tf` first, which is the intended friction.
- **No KMS-managed encryption.** State files use SSE-S3 (AES256) for cost simplicity. Bucket access is already restricted to the bootstrap principal and the OIDC CI role; KMS would add per-request cost without changing the threat model. Revisit if the project takes on real customer data.

## Files

| File | Purpose |
|------|---------|
| `versions.tf` | Pinned Terraform `~> 1.14` and AWS provider `~> 6.44` |
| `backend.tf` | Partial S3 backend; concrete values supplied via `.tfbackend` at init time |
| `main.tf` | Provider config, S3 state bucket, DynamoDB lock table |
| `oidc.tf` | GitHub OIDC provider (data source) + scoped CI role |
| `variables.tf` | `aws_region`, `project_name`, `github_repo`, `github_main_branch` |
| `outputs.tf` | Bucket name, lock table name, role ARN, OIDC provider ARN, ready-to-paste `tfbackend_example` |
| `.tfbackend.example` | Template for `.tfbackend` (which is gitignored) |
