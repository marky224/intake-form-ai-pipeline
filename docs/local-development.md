# Local development

This document covers the local development environment — GPU configuration, Ollama model setup, the Synthea + rendering workflow, and the local-only mode of the quickstart. Sections fill out as the build progresses; the Phase 1 / Phase 4 GPU-and-model material is the most fleshed out because that's what lands first.

## Hardware overview

- AMD Ryzen 7 5800X3D
- RTX 4080 (16 GB) + RTX 4060 Ti (16 GB), both on motherboard PCIe slots, no NVLink
- Combined 32 GB VRAM, PCIe-only inter-GPU communication
- Ubuntu (hostname `openclaw-pc`)

## Local Tier 3a model setup

Local Tier 3a runs Qwen 2.5 VL 32B on the combined VRAM. The locked default is **Q8_0 imported from the `Mungert/Qwen2.5-VL-32B-Instruct-GGUF` HuggingFace repository via custom Ollama Modelfile**. The Ollama registry only ships Q4_K_M, Q8_0, and FP16 for Qwen 2.5 VL (no Q6_K), so the higher-precision Q6_K and the locked Q8_0 default both come via custom import.

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

If Q8_0 load testing reveals problems (vision broken, F1 too low on the Phase 4 sanity test, or unmanageable CPU spill), the contingency tree in the project's main instructions document defines the fallback path: Q6_K → Q4_K_M → InternVL3.5-8B local → Tier 3a marked unavailable (orchestrator routes Tier-3a-bound traffic directly to Tier 3b, structurally identical to the `EXTRACTION_MODE=degraded` operational fallback in `.env.example`). Don't deviate from the documented contingency without surfacing the issue first.

## Multi-GPU layer split details

The locked Modelfile uses `num_gpu 99` (let Ollama figure out the split). On Mark's hardware, this typically lands as:

- RTX 4080 (16 GB): bottom half of layers (~32 layers for Q4_K_M, fewer for Q8_0)
- RTX 4060 Ti (16 GB): top half of layers
- CPU/system RAM: remaining layers when model size exceeds combined VRAM

Ollama distributes layers automatically based on each card's available VRAM, accounting for KV cache and image-token overhead. The 4080 typically gets slightly more layers than the 4060 Ti when both have similar free VRAM, since the 4080 is faster.

If you need to override the automatic split for debugging, `PARAMETER num_gpu N` (where N is a specific layer count) forces N layers onto GPU and the rest onto CPU. Useful when validating that CPU spill happens predictably for the Q8_0 case.

## Synthea workflow

> Lands in Phase 3. Synthea Docker setup, HTML+Playwright rendering pipeline, signature rendering parameters (Google Fonts handwriting fonts + SVG ink-bleed filter, ~70/30 typed/handwritten split). See `RATIONALE.md` Section 1 for the locked signature rendering parameters that the Phase 3 implementer should treat as locked input.

## Local-only mode for the quickstart

> Lands in Phase 7. The quickstart `just demo` command runs the cascade against three local fixture documents using cached responses, with no cloud calls or AWS credentials needed. This section will document the cached fixture format, how to add new local-only test documents, and how to switch between local-only and full-cascade modes via environment flags.

## Coexistence with general-purpose Ollama workflows

> Lands incrementally as the cascade orchestrator is built. The locked architectural pattern: Mark's existing Gemma 4 31B general-purpose workflow shares the GPU resources with the cascade. `OLLAMA_KEEP_ALIVE=10m` controls model unload behavior; the cascade orchestrator handles its own model loading/unloading via per-request `keep_alive: "1h"` overrides during eval batches. ~60–90 second swap latency between Gemma and Qwen is accepted.
