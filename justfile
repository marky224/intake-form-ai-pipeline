# intake-form-ai-pipeline task runner
#
# Run `just` to list available recipes.
# Phase 1 set: install, test, lint, format, pre-commit hygiene.
# Phase 3+ recipes (synthetic-data, demo, eval, deploy, review-ui) land with
# their respective phases — adding stub recipes that print "Lands in Phase N"
# is noise, so the recipe lands when the backing code does.

set shell := ["bash", "-cu"]
set windows-shell := ["powershell.exe", "-NoProfile", "-Command"]
# Auto-load `.env` into recipe environments. Phase 3.5's docile-build needs
# DOCILE_ACCESS_TOKEN (gitignored, not mirrored in `.env.example`); other
# recipes don't read .env vars so this is a no-op for them.
set dotenv-load := true

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

# Generate Synthea FHIR patient bundles for the Phase 3 healthcare corpus.
# Output: synthetic_data/output/synthea/fhir/*.json (gitignored).
# Args: <count> [seed]. Default count=10, seed=42.
#
#   just synthetic-data-patients               # 10 patients, seed 42
#   just synthetic-data-patients 500           # 500 patients, seed 42
#   just synthetic-data-patients 500 1337      # 500 patients, seed 1337
#
# Linux-only (uses $(id -u)/$(id -g) so Synthea writes output owned by the host user).
[unix]
synthetic-data-patients count="10" seed="42":
    docker build -t intake-synthea ./synthetic_data/synthea
    mkdir -p ./synthetic_data/output/synthea
    docker run --rm \
        --user "$(id -u):$(id -g)" \
        -v "$PWD/synthetic_data/output/synthea:/opt/synthea/output" \
        intake-synthea \
        -p {{count}} -s {{seed}} Massachusetts

# Chains the three steps end-to-end:
#   1. Synthea Docker (via synthetic-data-patients 500 42) — ~3-5 min, ~1-2 GB on disk
#   2. Playwright/Chromium render (one reused browser) — ~10 min, ~25 MB local output
#   3. boto3 upload to intake-form-ai-pipeline-documents — ~1-2 min, 1000 objects
#
# Re-running is safe: content-addressable S3 keys (<sha256>.{png,json}) land at the
# same paths across runs. See synthetic_data/render/upload.py module docstring for
# the S3 versioning footnote.
#
# Pre-reqs: Docker, Playwright + Chromium (see docs/local-development.md "Synthea
# workflow"), and AWS credentials resolvable via the boto3 default chain
# (~/.aws/credentials / env vars / instance profile). No keys are passed on the CLI.
#
# Full Phase 3 healthcare corpus: Synthea 500 patients -> CMS-1500 render -> S3 upload.
[unix]
synthetic-data-render-500: (synthetic-data-patients "500" "42")
    uv run python -m synthetic_data.render.batch \
        --input synthetic_data/output/synthea/fhir \
        --output synthetic_data/output/render
    uv run python -m synthetic_data.render.upload \
        --input synthetic_data/output/render \
        --bucket intake-form-ai-pipeline-documents

# Phase 3.5 DocILE business-document corpus: download annotated-trainval -> rasterize +
# sidecar -> S3 upload under synthetic/business/docile/.
#
# Default `limit=0` processes the full ~6.6K-document corpus
# (~33K page PNGs, ~1.6 GB on S3, ~30-60 min wallclock). For smoke runs pass
# a non-zero cap, e.g. `just synthetic-data-docile-build 5` for 5 documents.
# `limit` counts documents, not pages — multi-page docs contribute >1 PNG each.
#
# Pre-reqs:
#   * `DOCILE_ACCESS_TOKEN` in `.env` (obtained via docile.rossum.ai, gitignored,
#     auto-loaded into the recipe environment via `set dotenv-load := true` above).
#   * Pillow + pypdfium2 (installed by `uv sync`; no system Poppler/Cairo needed).
#   * AWS credentials resolvable via the boto3 default chain
#     (~/.aws/credentials / env vars / instance profile). No keys on the CLI.
#
# Re-runs are safe: download skips if annotations/ is already populated,
# rasterize overwrites in place, and uploads land at content-addressable keys
# (<sha256>.{png,json}) so retries after a partial failure resume cleanly.
[unix]
synthetic-data-docile-build limit="0":
    uv run python -m synthetic_data.docile.download \
        --dest synthetic_data/output/docile
    uv run python -m synthetic_data.docile.ingest \
        --dataset-root synthetic_data/output/docile \
        --render-dir synthetic_data/output/docile/render \
        --limit {{limit}}
    uv run python -m synthetic_data.render.upload \
        --input synthetic_data/output/docile/render \
        --bucket intake-form-ai-pipeline-documents \
        --prefix synthetic/business/docile

# Phase 6 eval harness: progressive-batch sweep over the test split,
# cached replay ($0, deterministic), persists to data/v1.db, regenerates
# docs/assets/f1-over-time.svg + evals/fixtures_manifest.json.
eval:
    uv run python -m evals run

# Same sweep against the live on-GPU models (Ollama + PaddleOCR-VL must be
# up on this box). Regenerates fixtures from fresh inference.
eval-live:
    EVAL_LIVE=true uv run python -m evals run

# Regenerate only the committed F1-over-time SVG + fixtures manifest from a
# fresh cached Tier-1 sweep (run this if the chart drift guard fails CI).
chart:
    uv run python -m evals chart

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
# Platform-split because uvx on Windows doesn't auto-resolve `checkov` to
# the `.cmd` shim (uv issue with Python entry-point resolution on Windows);
# Linux/macOS pick up the bare `checkov` binary fine. The two recipes are
# otherwise identical.

[unix]
sec-scan:
    uv run pre-commit run gitleaks --all-files
    uvx --from checkov checkov -d infra/terraform --framework terraform --config-file .checkov.yaml --download-external-modules false --quiet

[windows]
sec-scan:
    uv run pre-commit run gitleaks --all-files
    uvx --from checkov checkov.cmd -d infra/terraform --framework terraform --config-file .checkov.yaml --download-external-modules false --quiet

# Bicep (Azure parallel) syntax check. Mirrors the CI bicep-build job —
# compiles main.bicep + the bicepparam file via the standalone bicep CLI.
# Requires `bicep` on PATH (single Go binary from
# https://github.com/Azure/bicep/releases). Does not deploy.
bicep-build:
    bicep build infra/bicep/main.bicep --outfile /tmp/main.json
    bicep build-params infra/bicep/main.bicepparam --outfile /tmp/main.parameters.json

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
