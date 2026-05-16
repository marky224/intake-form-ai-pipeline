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

The Tier 2 provider pins `OLLAMA_HOST=http://127.0.0.1:11434` (default) and `keep_alive=1h` during eval batches to prevent unload between consecutive documents. Tier 3 (registry `qwen2.5vl:32b`, Q4_K_M) uses ~31 GB across the GPU pool at its default context, so it does not co-reside with Tier 2 — the cascade orchestrator's tier-batched eval pattern (process all Tier 1 docs first, then escalated docs through Tier 2, then escalated docs through Tier 3) avoids the coexistence pressure entirely.

## Local Tier 3 model setup

V1 Tier 3 runs **Qwen 2.5 VL 32B from the Ollama registry — `qwen2.5vl:32b` (Q4_K_M, ~21 GB)**. Same install model as Tier 2's `qwen2.5vl:7b`: a plain registry pull, no custom Modelfile.

```bash
ollama pull qwen2.5vl:32b
ollama list | grep qwen2.5vl   # qwen2.5vl:32b (~21 GB) + qwen2.5vl:7b (~6 GB)
```

That is the entire setup. On the build box (`openclaw-pc`, RTX 4080 + RTX 4060 Ti, Ollama 0.20.7) this runs ~52 s/doc at the model's default `num_ctx 32768` (Ollama auto-splits the ~31 GB across the two cards with a small ~9 % CPU spill), producing clean schema-constrained JSON at ~18 populated fields/doc on a CMS-1500.

### Why registry Q4_K_M, not a higher-precision Mungert import?

The architecture *originally* locked a higher-precision path: Q8_0 imported from `Mungert/Qwen2.5-VL-32B-Instruct-GGUF` via a custom dual-`FROM` Ollama Modelfile, with a Phase 4 dual-quant sanity test against Q6_K (the Ollama registry only ships Q4_K_M / Q8_0 / FP16 for this model — no Q6_K — so the higher-precision quants required a custom import). That path was **empirically infeasible on 31.2 GB of consumer VRAM** and was rescoped (decided 2026-05-17). The findings, kept here because they are the substance of the consumer-hardware trade-off and a watch-list for any future revisit:

- **Per-card *usable* VRAM is ~15.6 GB** (≈800 MiB system reservation off the 16 GB nameplate) → combined usable ≈ **31.2 GB**.
- **Mungert Q8_0** (~35 GB resident incl. f16 mmproj + KV) does not fit 31.2 GB. It needs an explicit lowered `num_gpu` (e.g. `num_gpu 48` → ~26 % CPU / 74 % GPU) to force genuine spill, at multi-minute-per-document latency. Impractical.
- **Mungert Q6_K_M** (~29 GB resident) *does* fit — at `num_ctx 8192` / `num_gpu 99` it loads 100 % on GPU with no OOM — but **every vision inference then fails** with `GGML_ASSERT(hparams.n_pos_per_embd()==1 && "seq_add() is only supported for n_pos_per_embd()==1")` (HTTP 500). This is the M-RoPE context-shift assert — Qwen2.5-VL uses `n_pos_per_embd > 1` and llama.cpp's `seq_add()` (the context-shift primitive) is unimplemented for it: open issue [ggml-org/llama.cpp#19915](https://github.com/ggml-org/llama.cpp/issues/19915). It is **not** a `num_ctx` or VRAM problem — a known-good run needs only ~2315 context tokens (measured `prompt_eval_count` 1997 + `eval_count` 318), 8192 is ample, and the model is fully GPU-resident. The assert is intrinsic to the Mungert GGUF Modelfile-import path on this Ollama; the registry build never triggers it regardless of `num_ctx`.
- **`num_gpu 99` does NOT mean "fit what you can, spill the rest"** (Ollama 0.20.7). It forces *all* layers onto GPU; if they don't fit, the llama runner OOMs and the process crashes (`cudaMalloc failed: out of memory` → Go panic → 500). A controlled spill requires an explicit lowered `num_gpu`.
- **A bare `FROM`-only Modelfile silently produces garbage** (HTTP 200, `prompt_eval_count=None`, zero populated fields): the imported GGUF has no chat template, so the prompt is never wrapped in Qwen's `<|im_start|>…<|im_end|>` markers. A correct VL import must carry the registry model's `TEMPLATE` + `SYSTEM` + `PARAMETER temperature` (`ollama show --modelfile qwen2.5vl:32b | awk '/^TEMPLATE /{f=1}/^LICENSE /{f=0}f'`).
- Tooling notes that still bite anyone attempting the Mungert path: `huggingface-cli` is deprecated — the CLI is now `hf` (`pip install -U huggingface_hub`); Mungert's README documents `…-q6_k.gguf` but the actual upload is `…-q6_k_m.gguf` (the Q6_K_M variant); the repo also carries `bf16-q8_0`/`f16-q8_0`/`bf16-q6_k`/`f16-q6_k` mixed-precision variants you do not want — pin exact filenames, never glob.

**Decision:** ship the registry **Q4_K_M** build — the only configuration that runs correctly on this hardware. No Ollama upgrade was taken (issue #19915 was open at decision time; an upgrade is an unconfirmed fix that also restarts the shared Ollama server). The full empirical decision + the still-applicable absolute-F1 contingency branches are recorded in the project's `architecture-locked.md` "Quantization choice and contingency tree" — treat that as the source of truth.

### Vision-capability sanity check

```bash
# Any test image works (a scanned form page or simple photo).
ollama run qwen2.5vl:32b "What is in this image? Describe what you see." ~/path/to/test-image.png
ollama ps   # confirm it loaded; expect ~31 GB across the two cards
```

The model should produce a sensible description, not text-only output or garbage. Registry builds ship with a working projector + chat template, so the broken-projector / missing-template failure modes that plague raw GGUF imports do not apply here.

### Contingency

If Tier 3 validation reveals problems (vision broken, or F1 below the contingency-tree bar), the fallback path is defined in `architecture-locked.md` "Quantization choice and contingency tree": F1 ≥ 0.80 ship; 0.65–0.80 document the gap publicly and ship; < 0.65 or hallucinating → InternVL3.5-8B local → Tier 3 marked unavailable. In V1, "Tier 3 unavailable" means the cascade fails escalated documents to a local review queue with full error history — there's no V1 cloud fallback above Tier 3. In V2 the same contingency routes Tier-3-bound escalations to Tier 3b (Bedrock Sonnet) instead, so V2 stays operational at higher cost. Don't deviate from the documented contingency without surfacing the issue first.

V2 also defines a broader operational failover via `EXTRACTION_MODE=degraded` in `.env.example`: triggered when the home-GPU bridge from deployed Lambda is unreachable, all local tiers (Tier 1, Tier 2-local, Tier 3) are skipped and every document escalates straight through Tier 2-cloud → Tier 3b. Not applicable in V1 — V1 has no Lambda, no bridge, and no degraded mode (the build machine runs everything in-process).

## Multi-GPU layer split details

Tier 3's registry `qwen2.5vl:32b` (Q4_K_M, ~21 GB weights + KV/image overhead ≈ ~31 GB at default context) is auto-split by Ollama across the GPU pool:

- RTX 4080 (16 GB): roughly half the layers
- RTX 4060 Ti (16 GB): the other half
- CPU/system RAM: the remainder (~9 % at default `num_ctx 32768`) — eliminable with a custom registry-based Modelfile at a lower `num_ctx` (only ~2315 context tokens are actually used; deferred — the default-context spill is not latency-fatal at ~52 s/doc).

Ollama distributes layers based on each card's available VRAM, accounting for KV cache and image-token overhead; the 4080 typically gets slightly more layers than the 4060 Ti when free VRAM is similar, since it is faster. For debugging, `PARAMETER num_gpu N` in a custom Modelfile forces exactly N layers onto GPU and the rest onto CPU (note: `num_gpu 99` forces *all* layers and crashes on OOM rather than spilling — see the Mungert findings above).

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

The checked-in validation corpus is CMS-1500 only (6 PNGs rendered from the Synthea fixtures). DocILE PDFs are CC-BY-NC-ND 4.0 and cannot be redistributed in this MIT public repo, so DocILE-side Tier 1 validation runs on the GPU build machine against the downloaded DocILE `annotated-trainval` corpus and the generated eval-cache fixtures stay local (gitignored). Phase 6 revisits whether DocILE-side fixtures need a separate redistribution-clean strategy.

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

The business-documents half of the synthetic corpus comes from the DocILE academic dataset (Rossum.ai, CC BY-NC-ND 4.0). Phase 3.5 wires up three chained steps: download the `annotated-trainval` archive (6680 annotated train+val docs combined; upstream renamed it from `labeled-trainval` after the pinned 2024-05-15 script commit), rasterize each PDF to per-page PNGs at 200 DPI, and upload the (PNG, sidecar JSON) pairs to S3 under `synthetic/business/docile/`. The 55-field KILE taxonomy is staged into each sidecar's `docile.fields[]` block; the cascade's `BusinessDocumentForm` consumes those annotations in Phase 4.

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
# 1. Download annotated-trainval (~1.1 GB zip, extracts into synthetic_data/output/docile/).
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
