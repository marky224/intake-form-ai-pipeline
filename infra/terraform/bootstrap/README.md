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

Set five repo-level GitHub Actions configuration values so the workflow can assume the right role and reach the state backend. **Three are stored as secrets** (they embed the AWS account ID, which we keep out of public CI logs); **two are stored as variables** (no sensitive content):

```bash
# Secrets — embed the AWS account ID. GitHub auto-masks secret values
# everywhere they appear in workflow logs.
gh secret set   AWS_OIDC_ROLE_ARN_DEPLOY --body "$(terraform output -raw deploy_role_arn)"
gh secret set   AWS_OIDC_ROLE_ARN_PLAN   --body "$(terraform output -raw plan_role_arn)"
gh secret set   TF_STATE_BUCKET          --body "$(terraform output -raw state_bucket_name)"

# Variables — no sensitive content. Lock table name and apply-brake
# flag stay as variables so they're visible in repo settings.
gh variable set TF_LOCK_TABLE            --body "$(terraform output -raw state_lock_table_name)"
```

The CI workflow picks the role to assume based on event type — `pull_request` → plan, push to `main` → deploy. The state bucket and lock-table values feed `terraform init -backend-config=...` against the main stack. Storing the role ARNs and bucket name as **secrets** ensures the AWS account ID embedded in those values is auto-masked in workflow logs; the workflow's `mask-aws-account-id: true` setting on `aws-actions/configure-aws-credentials` adds a second mask layer so the standalone account ID also gets masked in subsequent log output (e.g., `aws sts get-caller-identity` JSON, Terraform refresh lines).

A fifth value, `TF_APPLY_ENABLED`, is the safety brake on the CI `terraform apply` step (variable, not secret — its value is the literal string `'true'` or unset, no sensitive content). The workflow only runs `apply` when `TF_APPLY_ENABLED == 'true'` (strict equality — `'false'` and any other value disables it). Leave it unset until the deploy role's IAM scope matches what you want running in CI; once it does, lift the brake:

```bash
gh variable set TF_APPLY_ENABLED --body "true"
```

## What's intentionally not here

- **Deploy-role IAM is mostly scoped; `AmazonVPCFullAccess` remains the one managed-policy attachment.** Phase 2 PR 3 replaced `AmazonS3FullAccess` with a scoped inline policy (state bucket Get/Put/List on objects + ListBucket; project buckets full-manage on `intake-form-ai-pipeline-{documents,artifacts}-*`) and added a DynamoDB lock-table allow on the deploy role so CI applies can acquire the state lock. A follow-up hotfix (`ec2:DescribeAddressesAttribute` on `*`) patches a gap in `AmazonVPCFullAccess` v13 — the managed policy doesn't include the action, but AWS provider 6.x reads it on every EIP refresh; the fix sits as its own statement (`Ec2EipDescribeAttribute`) so it's easy to remove if AWS ever updates the managed policy. Phase 2 PR 4b (bootstrap-side) extended `deploy_scoped_allow` with four statements for the upcoming Aurora cluster: `AuroraResourceManage` (`rds:*` scoped to project-prefixed cluster/db/subgrp/cluster-pg/pg/secgrp ARNs), `RdsDescribeForRefresh` (read-only `rds:Describe*` + `rds:ListTagsForResource` on `*` to head off provider-6.x refresh-time 403s, mirroring the EIP-attribute pattern), `KmsKeyManage` (account-wide CMK management — KMS key ARNs are UUID-based so name-prefix scoping doesn't apply; key policy on the CMK is the second line of defense), and `SecretsManagerManage` (`secretsmanager:*` on `intake-form-ai-pipeline/*` secret-path prefix for explicit project-namespaced secrets). Phase 2 PR 4c (bootstrap-side) extended further for the Tier 1 security pass on the Aurora cluster: `AuroraManagedSecretCreate` (`secretsmanager:CreateSecret` scoped to the `rds!cluster-*` AWS-owned secret namespace so RDS can provision the AWS-managed master-password secret on the calling principal's behalf without granting create on arbitrary secret names), `AuroraManagedSecretManage` (`secretsmanager:TagResource` + `secretsmanager:RotateSecret` on the `rds!cluster-*` AWS-owned namespace), `LogGroupForAuroraExports` (CloudWatch Logs lifecycle on the project-prefixed `/aws/rds/cluster/intake-form-ai-pipeline-*` ARN pattern, including `AssociateKmsKey` / `DisassociateKmsKey` for log encryption under the same CMK), and `LogsDescribeForRefresh` (read-only `logs:Describe*` on `*` for provider-refresh, same shape as the RDS describe shim). `AmazonVPCFullAccess` stays managed for now; the VPC plan is narrow enough that the managed policy adds little risk. Future Phase 2 PRs (5: edge protection; 6: cost guards) follow the same bootstrap-then-main two-PR pattern: extend `deploy_scoped_allow` with the new resource type's actions, re-apply this stack locally, then open the main-stack PR.
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
