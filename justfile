# intake-form-ai-pipeline task runner
#
# Run `just` to list available recipes.
# Phase 1 set: install, test, lint, format, pre-commit hygiene.
# Phase 3+ recipes (synthetic-data, demo, eval, deploy, review-ui) land with
# their respective phases — adding stub recipes that print "Lands in Phase N"
# is noise, so the recipe lands when the backing code does.

set shell := ["bash", "-cu"]
set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]

# Default: list recipes
default:
    @just --list

# One-time setup: install Python + deps + pre-commit hooks
install:
    uv sync
    uv run pre-commit install

# Sync deps without modifying uv.lock (CI-equivalent)
install-frozen:
    uv sync --frozen

# Update uv.lock to latest compatible versions
lock-update:
    uv lock --upgrade

# Run the full test suite
test:
    uv run pytest

# Run tests with verbose output and short traceback (matches CI)
test-ci:
    uv run pytest -v --tb=short

# Lint: ruff check + black check + ruff-format check (read-only)
lint:
    uv run ruff check .
    uv run ruff format --check .
    uv run black --check .

# Format: ruff fix + ruff-format + black (mutating)
format:
    uv run ruff check --fix .
    uv run ruff format .
    uv run black .

# Run all pre-commit hooks across all files
pre-commit:
    uv run pre-commit run --all-files

# Regenerate alias_table_seed.json from intake_schemas.py + the curated alias map
alias-seed:
    uv run python build_alias_seed.py

# Terraform fmt + validate locally for both stacks (mirrors CI; no AWS creds needed)
tf-check:
    terraform fmt -check -recursive infra/terraform
    terraform -chdir=infra/terraform/bootstrap init -backend=false
    terraform -chdir=infra/terraform/bootstrap validate
    terraform -chdir=infra/terraform init -backend=false
    terraform -chdir=infra/terraform validate

# Security scans: gitleaks (via pre-commit) + checkov (via uvx, no install).
# Mirrors the CI secret-scan + iac-scan jobs. Run before pushing to catch
# findings locally rather than in the PR.
#
# Note: `checkov.cmd` is the Windows uv-managed entry point. On Linux, swap
# it for `checkov` (or alias the recipe). uvx publishes the same package as
# `checkov.cmd` on Windows and `checkov` on Linux/macOS — the binary names
# differ but the underlying tool is identical.
sec-scan:
    uv run pre-commit run gitleaks --all-files
    uvx --from checkov checkov.cmd -d infra/terraform --framework terraform --config-file .checkov.yaml --download-external-modules false --quiet

# Bootstrap stack: first-time init with local state (no backend yet)
tf-bootstrap-init:
    terraform -chdir=infra/terraform/bootstrap init -backend=false

# Bootstrap stack: apply (creates state bucket, lock table, deploy + plan CI roles)
tf-bootstrap-apply:
    terraform -chdir=infra/terraform/bootstrap apply

# Bootstrap stack: migrate local state into the S3 bucket it just created
tf-bootstrap-migrate:
    terraform -chdir=infra/terraform/bootstrap init -migrate-state -backend-config=.tfbackend

# Main stack: init with S3 backend (requires .tfbackend file present)
tf-init:
    terraform -chdir=infra/terraform init -backend-config=.tfbackend

# Main stack: plan
tf-plan:
    terraform -chdir=infra/terraform plan

# Main stack: apply
tf-apply:
    terraform -chdir=infra/terraform apply

# Database: print the SQL needed to initialize demo/eval/staging schemas + pgvector.
# Phase 2 PR 4 doesn't ship a connection method — bastion / Session Manager / Lambda
# runner lands in a follow-up PR alongside the compute layer. This recipe is a
# documentation stub so the schema-init step is visible in the build flow.
db-init-schemas:
    @echo "-- Run against the Aurora cluster (connection method TBD in follow-up PR):"
    @echo "CREATE SCHEMA IF NOT EXISTS demo;"
    @echo "CREATE SCHEMA IF NOT EXISTS eval;"
    @echo "CREATE SCHEMA IF NOT EXISTS staging;"
    @echo "CREATE EXTENSION IF NOT EXISTS vector;"
    @echo ""
    @echo "Pull master credentials with the cluster's AWS-managed secret ARN:"
    @echo "  SECRET_ARN=$(terraform -chdir=infra/terraform output -raw aurora_secret_arn)"
    @echo "  aws secretsmanager get-secret-value --secret-id \"$SECRET_ARN\" --query SecretString --output text | jq ."
