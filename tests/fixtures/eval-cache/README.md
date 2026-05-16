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
regenerate (e.g., after upgrading PaddleOCR-VL, switching the Textract
Queries shape, or onboarding a new validation document):

```bash
# On the build machine (RTX 4080/4060 Ti) with paddle installed:
uv sync --extra paddle
EVAL_LIVE=true uv run pytest -m slow test_tier1_paddleocr.py::test_live_inference_against_validation_set
```

`EVAL_LIVE=true` bypasses the cache, runs live inference, and writes the
fresh responses back to this directory. Commit the diff for reviewer
inspection.

## Initial seed (PR (a+b))

The initial seed for `tier1_paddleocr_local/` was generated programmatically
from the Synthea-rendered CMS-1500 validation set
(`tests/fixtures/eval-validation/cms1500/`) using realistic demographic
values lifted from each patient's FHIR bundle. They are **stand-ins**, not
real PaddleOCR-VL outputs. Mark must regenerate these on the GPU build
machine via the command above before merging PR (a+b) — and the diff at that
point will replace these stubs with the real model responses.

## Provider subdirectories

- `tier1_paddleocr_local/` — Phase 4 PR (a+b), seeded with CMS-1500 validation
- `tier2_textract/` — added in Phase 4 PR (c)
- `tier3a_qwen_local/` — added in Phase 4 PR (d)
- `tier3b_claude_bedrock/` — added in Phase 4 PR (e)
