# Local development

This document covers the local development environment — GPU configuration, Ollama model setup, the Synthea + rendering workflow, the DocILE workflow, and the Tier 1 PaddleOCR-VL setup. Sections fill out as the build progresses.

**As of 2026-05-14 the project pivoted to a local-first V1 build.** The full cascade runs locally — Tier 1 (PaddleOCR-VL) + Tier 2 (Qwen 2.5 VL 7B local; NEW post-pivot, replaces V2's planned AWS Textract at the Tier 2-local slot) + Tier 3 (Qwen 2.5 VL 32B local; was Tier 3a in V2 numbering). The cloud-only AWS upload steps in the Synthea + DocILE workflows below are V2 work — V1 follow-up refactors the uploaders to write to a local filesystem store under `synthetic_data/output/render/`. Until that refactor lands, the upload step is deferred (deliverable corpora can still be regenerated locally; just not pushed to S3).

## Hardware overview

- AMD Ryzen 7 5800X3D
- RTX 4080 (16 GB) + RTX 4060 Ti (16 GB), both on motherboard PCIe slots, no NVLink
- Combined 32 GB VRAM, PCIe-only inter-GPU communication
- Ubuntu (hostname `openclaw-pc`)

## Local Tier 2 model setup (Qwen 2.5 VL 7B)

V1 Tier 2 runs Qwen 2.5 VL 7B on the RTX 4080. ~16 GB FP16 fits one GPU comfortably; same model family as V1 Tier 3 so escalation is "more parameters" rather than "different model family."

```bash
# Pull from the Ollama registry — 7B has a stable Q8_0 build there.
ollama pull qwen2.5vl:7b
ollama list | grep qwen2.5vl
```

The Tier 2 provider pins `OLLAMA_HOST=http://127.0.0.1:11434` (default) and `keep_alive=1h` during eval batches to prevent unload between consecutive documents. Tier 2 and Tier 3 can be co-resident on the GPU pool when VRAM allows (Q6_K Tier 3 + 7B Tier 2 fits cleanly; Q8_0 Tier 3 + 7B Tier 2 spills extra to CPU). The cascade orchestrator's tier-batched eval pattern (process all Tier 1 docs first, then escalated docs through Tier 2, then escalated docs through Tier 3) avoids the coexistence pressure entirely.

## Local Tier 3 model setup

V1 Tier 3 runs Qwen 2.5 VL 32B on the combined VRAM. The locked default is **Q8_0 imported from the `Mungert/Qwen2.5-VL-32B-Instruct-GGUF` HuggingFace repository via custom Ollama Modelfile**. The Ollama registry only ships Q4_K_M, Q8_0, and FP16 for Qwen 2.5 VL (no Q6_K), so the higher-precision Q6_K and the locked Q8_0 default both come via custom import.

Q4_K_M from the Ollama registry is retained as a fast-iteration fallback and is what the quickstart pulls. The Phase 4 dual-quant sanity test compares Q8_0 against Q6_K to lock the locked default empirically; see the contingency tree in the project's main instructions document for the full decision logic.

### Phase 1 prep: download both quants

You're downloading Q8_0 (locked default) and Q6_K (Phase 4 comparison candidate) at the same time. Doing both now means Phase 4 is fully unblocked when it starts. Total download: ~66 GB.

**Pre-flight checks:**

```bash
# Need ~140 GB free temporarily — 66 GB for HF downloads + 66 GB for
# Ollama blob copies during 'ollama create'. After both creates succeed,
# the HF download dir can be deleted to reclaim ~66 GB.
df -h ~/models /usr/share/ollama 2>/dev/null

# Ollama version — VL Modelfile syntax has shifted across versions
ollama --version

# Verify the existing Q4_K_M baseline still works
ollama list | grep qwen2.5vl
```

**Confirm exact filenames before pulling 65 GB:**

```bash
pip install -U "huggingface_hub[cli]" --break-system-packages

huggingface-cli download Mungert/Qwen2.5-VL-32B-Instruct-GGUF README.md \
  --local-dir ~/models/qwen-vl-32b

grep -iE "Q8_0|Q6_K|mmproj" ~/models/qwen-vl-32b/README.md
```

That `grep` gives you the exact filenames Mungert uses. Filename casing and naming conventions vary by uploader (e.g., `Q8_0` vs `q8_0`); use what the README shows.

**Pull Q8_0 LLM, Q6_K LLM, and mmproj projector together:**

```bash
huggingface-cli download Mungert/Qwen2.5-VL-32B-Instruct-GGUF \
  --include "*[Qq]8_0*.gguf" \
  --include "*[Qq]6_[Kk]*.gguf" \
  --include "*mmproj*" \
  --local-dir ~/models/qwen-vl-32b
```

The `mmproj` (vision projector) is shared — both quants reference the same projector file. At ~50–70 MB/s typical HuggingFace download speed, 66 GB lands in 15–30 minutes.

### Modelfile templates

Adjust filenames below to match what `huggingface-cli` actually downloaded.

```bash
cat > ~/models/qwen-vl-32b/Modelfile-q8_0 <<'EOF'
FROM ./Qwen2.5-VL-32B-Instruct-Q8_0.gguf
FROM ./mmproj-Qwen2.5-VL-32B-Instruct-f16.gguf
PARAMETER num_gpu 99
PARAMETER num_ctx 4096
EOF

cat > ~/models/qwen-vl-32b/Modelfile-q6_k <<'EOF'
FROM ./Qwen2.5-VL-32B-Instruct-Q6_K.gguf
FROM ./mmproj-Qwen2.5-VL-32B-Instruct-f16.gguf
PARAMETER num_gpu 99
PARAMETER num_ctx 4096
EOF
```

`num_gpu 99` tells Ollama "put as many layers on GPU as fit, spill the rest to CPU." For Q8_0 you should see a meaningful CPU spill in `ollama ps` (~4 GB / ~11% of layers). For Q6_K all layers should fit on GPU.

If the dual `FROM` syntax doesn't work in your Ollama version, the fallback for the projector is:

```
PARAMETER mmproj ./mmproj-Qwen2.5-VL-32B-Instruct-f16.gguf
```

(without a second `FROM` line). Some Ollama versions only accept one `FROM` directive per Modelfile.

### Register with Ollama

```bash
cd ~/models/qwen-vl-32b

ollama create qwen2.5vl:32b-q8_0 -f Modelfile-q8_0
ollama create qwen2.5vl:32b-q6_k -f Modelfile-q6_k

# Verify both registered
ollama list | grep qwen2.5vl
```

`ollama create` copies the GGUFs into Ollama's blob store at `/usr/share/ollama/.ollama/models/blobs/`. After both creates succeed, the original `~/models/qwen-vl-32b/*.gguf` files can be deleted to reclaim ~66 GB. Keep the `README.md` and Modelfiles around for reference.

### Load and vision-capability validation

```bash
# Use any test image — a scanned PDF page or simple photo works.
# Synthea-rendered samples don't exist yet (Phase 3).
TEST_IMAGE=~/path/to/some/test-image.png

# Q8_0 load test
ollama run qwen2.5vl:32b-q8_0 "What is in this image? Describe what you see." "$TEST_IMAGE"

# Concurrent in another terminal — check actual VRAM/CPU split:
ollama ps

# Q6_K load test
ollama run qwen2.5vl:32b-q6_k "What is in this image? Describe what you see." "$TEST_IMAGE"

ollama ps
```

### Validation checklist

For each quant, record:

1. **Loaded cleanly?** Any OOM errors, missing-file errors, or Modelfile syntax errors.
2. **Actual VRAM/CPU split** per `ollama ps`. Q8_0 should show ~89% GPU / ~11% CPU; Q6_K should show 100% GPU. Materially different splits indicate either a different layer-allocation strategy than expected (worth investigating) or a Modelfile parameter issue.
3. **Tokens/sec on a vision query.** `ollama run` reports the eval rate at the end of each response. Q8_0 should land in the 8–15 tok/s range; Q6_K in the 25–40 tok/s range. If Q8_0 lands much lower (e.g., 3 tok/s), the layer split is hitting cold PCIe heavily and the Phase 6 fixture-generation batch timing estimate needs revising.
4. **Vision capability sanity.** Did the model produce a sensible description of the test image, or did it hallucinate / produce text-only output / produce garbage? GGUFs occasionally have broken projector linkage; the load test catches this.

If anything breaks during validation, capture the exact error message plus `ollama ps` output before troubleshooting. The most common gotchas are Modelfile syntax differences across Ollama versions and mmproj filename mismatches.

### Contingency

If Q8_0 load testing reveals problems (vision broken, F1 too low on the Phase 4 sanity test, or unmanageable CPU spill), the contingency tree in the project's main instructions document defines the fallback path: Q6_K → Q4_K_M → InternVL3.5-8B local → Tier 3 marked unavailable. In V1, "Tier 3 unavailable" means the cascade fails escalated documents to a local review queue with full error history — there's no V1 cloud fallback above Tier 3. In V2 the same contingency routes Tier-3-bound escalations to Tier 3b (Bedrock Sonnet) instead, so V2 stays operational at higher cost. Don't deviate from the documented contingency without surfacing the issue first.

V2 also defines a broader operational failover via `EXTRACTION_MODE=degraded` in `.env.example`: triggered when the home-GPU bridge from deployed Lambda is unreachable, all local tiers (Tier 1, Tier 2-local, Tier 3) are skipped and every document escalates straight through Tier 2-cloud → Tier 3b. Not applicable in V1 — V1 has no Lambda, no bridge, and no degraded mode (the build machine runs everything in-process).

## Multi-GPU layer split details

The locked Modelfile uses `num_gpu 99` (let Ollama figure out the split). On Mark's hardware, this typically lands as:

- RTX 4080 (16 GB): bottom half of layers (~32 layers for Q4_K_M, fewer for Q8_0)
- RTX 4060 Ti (16 GB): top half of layers
- CPU/system RAM: remaining layers when model size exceeds combined VRAM

Ollama distributes layers automatically based on each card's available VRAM, accounting for KV cache and image-token overhead. The 4080 typically gets slightly more layers than the 4060 Ti when both have similar free VRAM, since the 4080 is faster.

If you need to override the automatic split for debugging, `PARAMETER num_gpu N` (where N is a specific layer count) forces N layers onto GPU and the rest onto CPU. Useful when validating that CPU spill happens predictably for the Q8_0 case.

## Tier 1 PaddleOCR-VL setup

Local Tier 1 (Phase 4 PR (a+b)) runs **PaddleOCR-VL-1.5** on the RTX 4060 Ti — GPU 0 (RTX 4080) is reserved for Tier 3a Qwen so the cascade can hold both models resident across a batch. Sub-second per page, $0/call.

### Install (manual, not via `uv sync`)

PaddlePaddle 3.x (required by PaddleOCR-VL-1.5) ships from PaddlePaddle's own CDN — not PyPI — so it's installed manually rather than declared in `pyproject.toml`'s dependency tree. CI doesn't need it (Tier 1 runs against cached eval-fixtures), so this is a build-machine-only step. Order matters: install `paddleocr` *first* because its transitive deps pull in the CPU `paddlepaddle` wheel from PyPI; then force the GPU wheel on top of that.

```bash
# From repo root, inside the project's uv-managed venv.
# 1) Install paddleocr + paddlex[ocr] extra (this pulls in CPU paddlepaddle from PyPI).
uv pip install paddleocr "paddlex[ocr]==3.5.2"

# 2) Force-reinstall the GPU build over the CPU one. The package name on
#    Baidu's CDN is `paddlepaddle-gpu` but the wheel's distribution metadata
#    is `paddlepaddle`, so `uv pip install` via `--index-url` rejects it
#    ("expected paddlepaddle-gpu, got paddlepaddle"). Pass the bcebos origin
#    URL directly instead. Note: do not use the `cu128/paddlepaddle-gpu/`
#    path advertised on paddlepaddle.org.cn — it serves a CPU wheel despite
#    the name. Use `cu126` (paddle 3.0.0) for driver 570.x.
uv pip install --reinstall-package paddlepaddle \
  "https://paddle-whl.bj.bcebos.com/stable/cu129/paddlepaddle-gpu/paddlepaddle_gpu-3.1.1-cp311-cp311-linux_x86_64.whl"

# 3) Verify CUDA + device pin succeeds:
uv run python -c "import paddle; paddle.set_device('gpu:1'); paddle.utils.run_check()"
```

The cu129 wheel (paddle 3.1.1) ships against CUDA 12.9 runtime — your driver (570.x) reports `Driver API Version: 12.8` but CUDA's intra-12.x forward-compat makes 12.9 runtime work fine. PaddleOCR-VL-1.5 needs paddle ≥ 3.1 for `paddle.incubate.nn.functional.fused_rms_norm_ext` — the cu126 paddle 3.0.0 wheel is missing that symbol. PaddleOCR's pretrained PaddleOCR-VL-1.5 checkpoint downloads on first `PaddleOCRVL()` construction (~5 GB to `~/.paddlex/`).

#### Network notes

`www.paddlepaddle.org.cn` is behind BAIDU_WAF and returns `content-length: 0` for `.whl` requests from non-Chinese IPs (the wheel URL responds HTTP 200 with an empty body). The actual CDN origin `paddle-whl.bj.bcebos.com` is unaffected — always download wheels from there directly.

### Device pin

The provider pins `paddle.set_device("gpu:1")` so Tier 1 runs on the RTX 4060 Ti. If you need to inspect GPU memory:

```bash
nvidia-smi -i 1
```

### Generating eval-cache fixtures

Cached responses live at `tests/fixtures/eval-cache/tier1_paddleocr_local/<image_sha256>.json` and are checked in so CI can exercise the cached-replay path without paddle installed. To regenerate against the real model:

```bash
EVAL_LIVE=true uv run python - <<'PY'
from pathlib import Path
from cascade.providers.tier1_paddleocr_local import Tier1PaddleOcrLocal
from intake_schemas import HealthcareIntakeForm

provider = Tier1PaddleOcrLocal()
for png in sorted(Path("tests/fixtures/eval-validation/cms1500").glob("*.png")):
    provider.extract(png.read_bytes(), HealthcareIntakeForm)
PY

# Commit the regenerated fixtures:
git add tests/fixtures/eval-cache/tier1_paddleocr_local/
```

`EVAL_LIVE=true` bypasses the cache, runs live inference against the 6 CMS-1500 validation PNGs in `tests/fixtures/eval-validation/cms1500/`, and writes the fresh responses back. The CI machine never sets `EVAL_LIVE`, so the committed fixtures drive every CI run.

### Validation set

The checked-in validation corpus is CMS-1500 only (6 PNGs rendered from the Synthea fixtures). DocILE PDFs are CC-BY-NC-ND 4.0 and cannot be redistributed in this MIT public repo, so DocILE-side Tier 1 validation runs on the GPU build machine against the downloaded `docile-labeled-trainval` corpus and the generated eval-cache fixtures stay local (gitignored). Phase 6 revisits whether DocILE-side fixtures need a separate redistribution-clean strategy.

## Synthea workflow

The healthcare half of the synthetic corpus is generated end-to-end via three chained steps: Synthea generates FHIR patient bundles, the renderer rasterizes each bundle into a CMS-1500 PNG plus a bbox-sidecar JSON, and the uploader pushes the pairs to S3 under content-addressable keys. Signature rendering parameters (Google Fonts handwriting fonts, SVG ink-bleed filter, ~70/30 typed/handwritten split, ±3° rotation) are locked in `RATIONALE.md` Section 1.

### Pre-requisites

- **Docker** — Synthea runs in a pinned container (`synthetic_data/synthea/Dockerfile` checksum-verifies the upstream JAR at build time).
- **Playwright + Chromium** — the renderer drives a headless Chromium via Playwright:

  ```bash
  uv sync                                    # installs playwright (dev dep)
  uv run playwright install chromium         # downloads the Chromium binary
  ```

  On Ubuntu 24.04, Chromium also needs a handful of system libs that Playwright's `install-deps` would install via sudo. In headless / no-TTY environments install them explicitly:

  ```bash
  sudo apt install -y libcairo2 libcups2t64 libpango-1.0-0 \
                      libxcomposite1 libxdamage1 libxfixes3 \
                      libatk1.0-0 libatk-bridge2.0-0 libnss3
  ```

- **AWS credentials** for the upload step, resolvable via the standard boto3 chain (`~/.aws/credentials`, env vars, or instance profile). The CLI never reads keys from arguments.

### One-shot full corpus (Phase 3 closeout)

The full 500-patient corpus is one recipe:

```bash
just synthetic-data-render-500
```

This chains `just synthetic-data-patients 500 42` → `python -m synthetic_data.render.batch` → `python -m synthetic_data.render.upload`. End-to-end takes ~15-20 minutes (Synthea ~3-5 min, render ~10 min, upload ~1-2 min) and produces 1000 S3 objects (500 PNG + 500 JSON, ~25 MB total) under `s3://intake-form-ai-pipeline-documents/synthetic/healthcare/cms1500/`. Local disk usage peaks at ~1-2 GB during the run; the `synthetic_data/output/` tree is gitignored and can be deleted after upload.

### Step-by-step (smaller runs, debugging)

For development you usually want fewer patients than 500. Run each step individually:

```bash
# 1. Synthea: generate 10 FHIR bundles with seed 42 (matches the committed test fixture).
just synthetic-data-patients 10 42
# Output: synthetic_data/output/synthea/fhir/*.json

# 2. Render: walk the FHIR dir and produce one (PNG, sidecar JSON) pair per patient.
uv run python -m synthetic_data.render.batch \
    --input synthetic_data/output/synthea/fhir \
    --output synthetic_data/output/render
# Output: synthetic_data/output/render/<patient-id>-<sha8>.{png,json}

# 3. Upload: push the pairs to S3 under content-addressable keys.
uv run python -m synthetic_data.render.upload \
    --input synthetic_data/output/render \
    --bucket intake-form-ai-pipeline-documents
# S3 keys: synthetic/healthcare/cms1500/<image_sha256>.{png,json}
```

`synthetic-data-patients` accepts a count and seed (`just synthetic-data-patients 500 1337`). The render and upload CLIs always process whatever's in the input directory — there's no built-in limit flag on upload, and `--limit N` on `render.batch` renders the first N patients in sorted-by-filename order.

### S3 key shape and idempotency

The uploader derives each object's key from the PNG's `image_sha256` (already computed by the renderer and recorded in the sidecar — the uploader does not re-hash). PNG and sidecar share the hash and differ only in extension:

```text
synthetic/healthcare/cms1500/<image_sha256>.png
synthetic/healthcare/cms1500/<image_sha256>.json
```

Each object carries `x-amz-meta-source-id` so a HEAD on either key surfaces the source record (a Synthea `patient_id` for healthcare uploads, a DocILE `<doc_id>-p<page>` slug for the business-documents vertical) without fetching the sidecar body. Re-runs land at the same keys (the renderer is deterministic for a given Chromium version), so partial-failure retries resume cleanly without external bookkeeping. The documents bucket is versioned, which records a new object version per PutObject regardless of content equality — that's a footnote, not a benefit; content-addressable keys are about keeping the key namespace clean across re-runs, not about storage dedup.

Cross-Chromium-version PNG byte stability is not a project guarantee. Bumping the pinned Playwright minor version shifts PNG bytes → new hashes → new objects (the old keys become orphans). Acceptable because the corpus is regenerable.

## DocILE workflow

The business-documents half of the synthetic corpus comes from the DocILE academic dataset (Rossum.ai, CC BY-NC-ND 4.0). Phase 3.5 wires up three chained steps: download the `labeled-trainval` archive (6680 annotated train+val docs combined), rasterize each PDF to per-page PNGs at 200 DPI, and upload the (PNG, sidecar JSON) pairs to S3 under `synthetic/business/docile/`. The 55-field KILE taxonomy is staged into each sidecar's `docile.fields[]` block; the cascade's `BusinessDocumentForm` consumes those annotations in Phase 4.

### Pre-requisites

- **`DOCILE_ACCESS_TOKEN` in `.env`** — register at [docile.rossum.ai](https://docile.rossum.ai) to obtain the token (an S3 share-link path segment). The token lives in `.env` (gitignored, NOT mirrored in `.env.example` per token-surface privacy) and is auto-loaded into the recipe environment via `set dotenv-load := true` in the justfile.
- **pypdfium2 + Pillow** — pure-wheel Python deps, installed by `uv sync`. No system Poppler / Cairo install required (pypdfium2 bundles the PDFium native binary).
- **AWS credentials** for the upload step, resolvable via the standard boto3 chain — same as the Synthea workflow.

### Scope (locked)

- **Splits downloaded:** `labeled-trainval` only (combined train + val). The `test`, `synthetic`, and `unlabeled` archives are reserved for the post-launch Phase 7-V2 `just process-batch` recipe per the half-now-half-later corpus-partitioning lock in `.claude-context/cost-model.md`. The download wrapper enforces this — passing `dataset != "labeled-trainval"` raises immediately.
- **Annotation task:** KILE only. The `line_item_extractions` (LIR) block is parsed but not staged into the sidecar — Phase 4 cascade work uses the 55-field KILE taxonomy against `BusinessDocumentForm`.
- **Rasterization DPI:** 200, matching DocILE's `metadata.page_sizes_at_200dpi` so bbox coordinates round-trip cleanly between normalized and pixel space.

### One-shot full corpus (Phase 3.5 closeout)

```bash
just synthetic-data-docile-build
```

End-to-end ~30-60 minutes wallclock (the bulk is the 1.6 GB ZIP download from `docile-dataset-rossum.s3.eu-west-1.amazonaws.com`). Produces ~33,000 page PNGs + ~33,000 sidecar JSONs (~66,000 S3 objects, ~1.6 GB total) under `s3://intake-form-ai-pipeline-documents/synthetic/business/docile/`. Local disk peaks at ~2 GB during the run; `synthetic_data/output/docile/` is gitignored.

### Smoke run (5-10 documents)

For the first end-to-end against the real bucket, cap document count:

```bash
just synthetic-data-docile-build 5     # 5 documents from the train split
```

`limit` counts documents, not pages — multi-page docs contribute multiple PNGs each. A 5-doc smoke against typical DocILE multi-page invoices runs in ~1-2 minutes after the initial download.

### Step-by-step (debugging)

```bash
# 1. Download labeled-trainval (~1.6 GB extracted into synthetic_data/output/docile/).
#    Idempotent: skips if annotations/ is already populated.
uv run python -m synthetic_data.docile.download \
    --dest synthetic_data/output/docile

# 2. Ingest: parse + rasterize + build sidecar per page. No S3 surface.
#    Output: synthetic_data/output/docile/render/<doc_id>-p<N>.{png,json}
uv run python -m synthetic_data.docile.ingest \
    --dataset-root synthetic_data/output/docile \
    --render-dir synthetic_data/output/docile/render \
    --limit 5

# 3. Upload: push pairs to S3 under content-addressable keys.
uv run python -m synthetic_data.render.upload \
    --input synthetic_data/output/docile/render \
    --bucket intake-form-ai-pipeline-documents \
    --prefix synthetic/business/docile
```

### S3 key shape

Identical to the healthcare path; only the prefix differs:

```text
synthetic/business/docile/<image_sha256>.png
synthetic/business/docile/<image_sha256>.json
```

Each object's `x-amz-meta-source-id` is `<doc_id>-p<page_number>` (1-indexed page), letting a HEAD recover both the DocILE document id and the specific page without fetching the sidecar body.

### Notes on the vendored download script

`synthetic_data/docile/download_dataset.sh` is a verbatim copy of `rossumai/docile/download_dataset.sh` pinned to upstream commit `12f9502d1ee80143c24eb98d89abc324db8003b6`. The wrapper sha256-verifies the file on every invocation so an accidental local edit or supply-chain drift fails loudly before any network call. Re-vendor + bump `VENDORED_SCRIPT_SHA256` together when intentionally upgrading.

The token is interpolated into the URL path (`https://docile-dataset-rossum.s3.eu-west-1.amazonaws.com/<token>/<dataset>.zip`) — it's a presigned-share-link path segment, not an HTTP header. The wrapper passes it via positional argv to the script, so it briefly appears in `ps` output during the curl. Acceptable for this dev environment given the token is registration-scoped (not write-credentialed).

## Local-only mode for the quickstart

> Lands in Phase 7-V1. The quickstart `just demo` command runs the cascade against local fixture documents using cached responses (default) or live local inference (with `EVAL_LIVE=true`); no cloud calls, no AWS credentials needed. This is the default V1 demo shape — alternatives at Phase 7-V1 entry are CLI + screenshot artifacts or no-demo / eval-report-only. This section will document the cached fixture format, how to add new local-only test documents, and how to switch between cached-replay and live-local modes via environment flags.

## Coexistence with general-purpose Ollama workflows

> Lands incrementally as the cascade orchestrator is built. The locked architectural pattern: Mark's existing Gemma 4 31B general-purpose workflow shares the GPU resources with the cascade. `OLLAMA_KEEP_ALIVE=10m` controls model unload behavior; the cascade orchestrator handles its own model loading/unloading via per-request `keep_alive: "1h"` overrides during eval batches. ~60–90 second swap latency between Gemma and Qwen is accepted.
