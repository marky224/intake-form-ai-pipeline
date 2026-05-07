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

# Terraform fmt + validate locally (mirrors CI's terraform job, no AWS creds needed)
tf-check:
    terraform fmt -check -recursive infra/terraform
    terraform -chdir=infra/terraform/bootstrap init -backend=false
    terraform -chdir=infra/terraform/bootstrap validate

# Bootstrap stack: first-time init with local state (no backend yet)
tf-bootstrap-init:
    terraform -chdir=infra/terraform/bootstrap init -backend=false

# Bootstrap stack: apply (creates state bucket, lock table, OIDC provider, CI role)
tf-bootstrap-apply:
    terraform -chdir=infra/terraform/bootstrap apply

# Bootstrap stack: migrate local state into the S3 bucket it just created
tf-bootstrap-migrate:
    terraform -chdir=infra/terraform/bootstrap init -migrate-state -backend-config=.tfbackend
