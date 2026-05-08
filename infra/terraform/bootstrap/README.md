# Terraform bootstrap

One-time stack that creates the foundation everything else depends on:

- S3 bucket for remote Terraform state (versioned, encrypted, public-access-blocked, lifecycle-managed)
- DynamoDB table for state locking (PAY_PER_REQUEST, encrypted, point-in-time recovery)
- Two IAM roles for GitHub Actions OIDC:
  - **deploy** role assumable on push to `main` only — write perms for VPC + S3, plus inline deny on the state bucket and lock table so a buggy apply can't recursively nuke its own backend.
  - **plan** role assumable on `pull_request` workflows only — `ReadOnlyAccess` managed policy. PR CI runs `terraform plan -lock=false` so it can read state without DynamoDB write perms.

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

Prerequisites: AWS CLI authenticated as a principal with permissions to create the bootstrap resources (S3 bucket + versioning/encryption/lifecycle settings, DynamoDB table, IAM role) plus `iam:GetOpenIDConnectProvider` to look up the shared OIDC provider — typically an administrator or a scoped one-off bootstrap policy. Terraform `~> 1.14` installed. GitHub OIDC provider already created in the AWS account (see top of README).

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

Set four repo-level GitHub Actions variables so the workflow can assume the right role and reach the state backend:

```bash
gh variable set AWS_OIDC_ROLE_ARN_DEPLOY --body "$(terraform output -raw deploy_role_arn)"
gh variable set AWS_OIDC_ROLE_ARN_PLAN   --body "$(terraform output -raw plan_role_arn)"
gh variable set TF_STATE_BUCKET          --body "$(terraform output -raw state_bucket_name)"
gh variable set TF_LOCK_TABLE            --body "$(terraform output -raw state_lock_table_name)"
```

The CI workflow picks the role to assume based on event type — `pull_request` → plan, push to `main` → deploy. `TF_STATE_BUCKET` and `TF_LOCK_TABLE` feed `terraform init -backend-config=...` against the main stack so the AWS account ID stays out of the public repo.

A fifth variable, `TF_APPLY_ENABLED`, is the safety brake on the CI `terraform apply` step. The workflow only runs `apply` when `TF_APPLY_ENABLED == 'true'` (strict equality — `'false'` and any other value disables it). Leave it unset until the deploy role's IAM scope matches what you want running in CI; once it does, lift the brake:

```bash
gh variable set TF_APPLY_ENABLED --body "true"
```

## What's intentionally not here

- **Deploy-role IAM is mostly scoped; `AmazonVPCFullAccess` remains the one managed-policy attachment.** Phase 2 PR 3 replaced `AmazonS3FullAccess` with a scoped inline policy (state bucket Get/Put/List on objects + ListBucket; project buckets full-manage on `intake-form-ai-pipeline-{documents,artifacts}-*`) and added a DynamoDB lock-table allow on the deploy role so CI applies can acquire the state lock. Post-merge sequence: PR 3 lands on `main` → this stack is re-applied locally (`just tf-bootstrap-apply`) so the live role picks up the scoped policy → `gh variable set TF_APPLY_ENABLED --body "true"` lifts the safety brake on the CI `apply` step. `AmazonVPCFullAccess` stays managed for now; the VPC plan is narrow enough that the managed policy adds little risk.
- **No `prevent_destroy = false` escape hatch.** The state bucket and lock table both have `prevent_destroy = true`. Tearing them down requires editing `main.tf` first, which is the intended friction.
- **No KMS-managed encryption.** State files use SSE-S3 (AES256) for cost simplicity. Bucket access is already restricted to the bootstrap principal and the OIDC CI role; KMS would add per-request cost without changing the threat model. Revisit if the project takes on real customer data.

## Files

| File | Purpose |
|------|---------|
| `versions.tf` | Pinned Terraform `~> 1.14` and AWS provider `~> 6.44` |
| `backend.tf` | Partial S3 backend; concrete values supplied via `.tfbackend` at init time |
| `main.tf` | Provider config, S3 state bucket, DynamoDB lock table |
| `oidc.tf` | GitHub OIDC provider (data source) + deploy role (push to main, write) + plan role (PR, read-only) |
| `variables.tf` | `aws_region`, `project_name`, `github_repo`, `github_main_branch` |
| `outputs.tf` | Bucket name, lock table name, deploy/plan role ARNs, OIDC provider ARN, ready-to-paste `tfbackend_example` |
| `.tfbackend.example` | Template for `.tfbackend` (which is gitignored) |
