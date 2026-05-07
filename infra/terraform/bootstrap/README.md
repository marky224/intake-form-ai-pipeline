# Terraform bootstrap

One-time stack that creates the foundation everything else depends on:

- S3 bucket for remote Terraform state (versioned, encrypted, public-access-blocked, lifecycle-managed)
- DynamoDB table for state locking (PAY_PER_REQUEST, encrypted, point-in-time recovery)
- GitHub Actions OIDC provider (no long-lived AWS keys)
- IAM role assumable from this repo's `main` branch and pull-request workflows

After this stack is applied and its state migrated to the bucket it just created, every other Terraform stack in `infra/terraform/` uses the same backend.

## First-time setup

Prerequisites: AWS CLI authenticated as a principal with `iam:*`, `s3:*`, `dynamodb:*` permissions on the target account; Terraform `~> 1.14` installed.

```bash
cd infra/terraform/bootstrap

# 1. Initialise with local state (no backend yet)
terraform init -backend=false

# 2. Apply — creates state bucket, lock table, OIDC provider, CI role
terraform apply

# 3. Build .tfbackend from the apply output
terraform output -raw tfbackend_example > .tfbackend

# 4. Re-init with the S3 backend, migrating the local state file
terraform init -migrate-state -backend-config=.tfbackend
# Answer 'yes' when prompted to migrate state.

# 5. Verify state migrated cleanly
terraform plan   # should report "No changes"

# 6. Delete the now-redundant local state file
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
| `oidc.tf` | GitHub OIDC provider + scoped CI role |
| `variables.tf` | `aws_region`, `project_name`, `github_repo`, `github_main_branch` |
| `outputs.tf` | Bucket name, lock table name, role ARN, OIDC provider ARN, ready-to-paste `tfbackend_example` |
| `.tfbackend.example` | Template for `.tfbackend` (which is gitignored) |
