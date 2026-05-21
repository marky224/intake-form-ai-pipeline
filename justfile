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

# Regenerate alias_table_seed.json (at repo root) from src/intake_schemas.py +
# the curated alias map. Invoked via `python -m` since build_alias_seed moved
# to src/ as a top-level py-module in the 2026-05-19 src-layout refactor.
alias-seed:
    uv run python -m build_alias_seed

# Generate Synthea FHIR patient bundles for the Phase 3 healthcare corpus.
# Output: src/synthetic_data/output/synthea/fhir/*.json (gitignored).
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
#   3. local-store copy into src/synthetic_data/output/store — sub-second, 1000 files
#
# V1 is local-first: no S3, no AWS credentials. Re-running is safe —
# content-addressable paths (<sha256>.{png,json}) land at the same store
# location across runs, so a partial-failure retry resumes cleanly.
# (V2 restores the S3 uploader; see src/synthetic_data/render/upload.py docstring.)
#
# Pre-reqs: Docker, Playwright + Chromium (see docs/local-development.md "Synthea
# workflow"). No AWS.
#
# Full Phase 3 healthcare corpus: Synthea 500 patients -> CMS-1500 render -> local store.
[unix]
synthetic-data-render-500: (synthetic-data-patients "500" "42")
    uv run python -m synthetic_data.render.batch \
        --input src/synthetic_data/output/synthea/fhir \
        --output src/synthetic_data/output/render
    uv run python -m synthetic_data.render.upload \
        --input src/synthetic_data/output/render \
        --store-root src/synthetic_data/output/store

# Phase 3.5 DocILE business-document corpus: download annotated-trainval -> rasterize +
# sidecar -> local-store copy under synthetic/business/docile/.
#
# Default `limit=0` processes the full ~6.6K-document corpus
# (~33K page PNGs, ~1.6 GB on disk, ~30-60 min wallclock). For smoke runs pass
# a non-zero cap, e.g. `just synthetic-data-docile-build 5` for 5 documents.
# `limit` counts documents, not pages — multi-page docs contribute >1 PNG each.
#
# Pre-reqs:
#   * `DOCILE_ACCESS_TOKEN` in `.env` (obtained via docile.rossum.ai, gitignored,
#     auto-loaded into the recipe environment via `set dotenv-load := true` above).
#   * Pillow + pypdfium2 (installed by `uv sync`; no system Poppler/Cairo needed).
#   * No AWS — V1 is local-first.
#
# Re-runs are safe: download skips if annotations/ is already populated,
# rasterize overwrites in place, and the store copy lands at content-addressable
# paths (<sha256>.{png,json}) so retries after a partial failure resume cleanly.
[unix]
synthetic-data-docile-build limit="0":
    uv run python -m synthetic_data.docile.download \
        --dest src/synthetic_data/output/docile
    uv run python -m synthetic_data.docile.ingest \
        --dataset-root src/synthetic_data/output/docile \
        --render-dir src/synthetic_data/output/docile/render \
        --limit {{limit}}
    uv run python -m synthetic_data.render.upload \
        --input src/synthetic_data/output/docile/render \
        --store-root src/synthetic_data/output/store \
        --prefix synthetic/business/docile

# Phase 6 eval harness: progressive-batch sweep over the test split,
# cached replay ($0, deterministic), persists to src/data/v1.db, regenerates
# src/evals/fixtures_manifest.json.
eval:
    uv run python -m evals run

# Same sweep against the live on-GPU models (Ollama + PaddleOCR-VL must be
# up on this box). Regenerates fixtures from fresh inference.
eval-live:
    EVAL_LIVE=true uv run python -m evals run

# Regenerate the committed src/evals/manifest.json from the full local corpus
# (gitignored src/synthetic_data/output/render/), patient-stratified
# train/dev/test. Local-only — run after `just synthetic-data-render-500`.
build-manifest:
    uv run python -m evals build-manifest

# One-time live regen of the committed eval-cache fixtures for every
# test-split doc across all 4 replay namespaces (tier1/2/3 + router
# stage 2). GPU box only (PaddleOCR-VL + Ollama Qwen 7B/32B). ~2 h.
# Resumable — re-run to fill gaps after a partial pass. Run this, then
# `just by-stage` (cached) regenerates the SVG CI reproduces.
regen-fixtures:
    EVAL_LIVE=true uv run python -m scripts.regen_eval_fixtures

# Regenerate only the committed by-stage SVG (docs/assets/f1-by-stage.svg):
# the F1-by-cumulative-tier panel + the escalation-funnel panel, from a
# fresh cached measurement (run this if the by-stage drift guard fails CI).
by-stage:
    uv run python -m evals by-stage

# Phase 7-V1 local demo: Streamlit app on openclaw-pc. Runs the real cascade
# on the committed CMS-1500 fixtures through the cached-replay path ($0, no
# GPU, no Ollama/Paddle). Prefix `EVAL_LIVE=true` to drive the live models.
demo:
    uv run streamlit run src/demo/app.py

# Phase 8 (V1) correction-feedback loop: seeded-reviewer replay over the
# parked CMS-1500 (cached/$0, no GPU). Logs corrections, learns new alias
# phrasings into a throwaway overlay, re-embeds (no-op without ColQwen
# fixtures). Never touches src/data/v1.db or src/data/corrections_aliases.json.
correct:
    uv run python -m rag correct

# Regenerate the committed ColQwen 2.5 .npy embedding fixtures from live
# inference (GPU box only — colpali-engine + a CUDA device required).
# Mirrors `eval-live`: EVAL_LIVE bypasses the cache and rewrites fixtures.
embed:
    EVAL_LIVE=true uv run python -m rag embed

# Phase 9 QLoRA experiment (text post-corrector). data + eval are cached/$0/
# no-GPU; train is GPU-box-only (peft/bitsandbytes/trl + a CUDA device).
finetune-data:
    uv run python -m finetune data

finetune-eval:
    uv run python -m finetune eval

# GPU box only. FINETUNE_LIVE gates the heavy stack (mirrors eval-live).
finetune-train:
    FINETUNE_LIVE=true uv run python -m finetune train

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
# V2-only recipe (V1 is local-first SQLite — no Aurora). The schema-init
# connection mechanism (bastion / Session Manager / Lambda runner) is a
# deferred V2 compute-layer item, tracked in docs/production-roadmap.md; this
# recipe is intentionally a printable documentation stub until V2 builds it.
db-init-schemas:
    @echo "-- V2-only: run against the Aurora cluster once the V2 compute layer"
    @echo "-- ships a connection mechanism (deferred — see docs/production-roadmap.md):"
    @echo "CREATE SCHEMA IF NOT EXISTS demo;"
    @echo "CREATE SCHEMA IF NOT EXISTS eval;"
    @echo "CREATE SCHEMA IF NOT EXISTS staging;"
    @echo "CREATE EXTENSION IF NOT EXISTS vector;"
    @echo ""
    @echo "Pull master credentials with the cluster's AWS-managed secret ARN:"
    @echo "  SECRET_ARN=$(terraform -chdir=infra/terraform output -raw aurora_secret_arn)"
    @echo "  aws secretsmanager get-secret-value --secret-id \"$SECRET_ARN\" --query SecretString --output text | jq ."
