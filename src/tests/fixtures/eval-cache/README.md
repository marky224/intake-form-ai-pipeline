# Eval cache

Provider responses keyed by `image_sha256` of the input PNG. The cascade
providers check these on every call when `EVAL_LIVE` is unset; on hit they
return the cached response without making a live API call.

## Layout

```
<provider_name>/<image_sha256>.json
```

Each file's body is the upstream provider's `raw_response` dict verbatim — no
`ProviderResult` wrapping, no normalization. Providers re-parse via their own
`_parse_response` on read so the cached-replay path exercises the same parser
as the live path.

## Regenerating

Cached fixtures are checked in so CI never makes live provider calls. To
regenerate (e.g., after upgrading PaddleOCR-VL or onboarding a new validation
document):

```bash
# On the GPU build machine (RTX 4080/4060 Ti), with PaddleOCR-VL installed and
# Ollama serving qwen2.5vl:7b and qwen2.5vl:32b. The regen takes ~2 h.
# Resumable: re-run it to fill gaps after a partial pass.
uv sync --locked --inexact
# --inexact keeps PaddleOCR-VL, which is installed by hand per docs/local-development.md, "Tier 1 PaddleOCR-VL setup".
just regen-fixtures
```

`just regen-fixtures` sets `EVAL_LIVE=true`, which bypasses the cache, runs
live inference, and writes the fresh responses back to this directory. Commit
the diff for reviewer inspection.

## Initial seed (PR (a+b))

The initial seed for `tier1_paddleocr_local/` was generated programmatically
from the Synthea-rendered CMS-1500 validation set
(`src/tests/fixtures/eval-validation/cms1500/`) using realistic demographic
values lifted from each patient's FHIR bundle. They are **stand-ins**, not
real PaddleOCR-VL outputs. Mark must regenerate these on the GPU build
machine via the command above before merging PR (a+b) — and the diff at that
point will replace these stubs with the real model responses.

## Provider subdirectories

- `tier1_paddleocr_local/`
- `tier2_qwen_7b_local/`
- `tier3_qwen_32b_local/`
- `router_stage2_qwen_7b/`
