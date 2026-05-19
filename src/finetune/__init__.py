"""Phase 9 — QLoRA fine-tuning *experiment* (text post-corrector).

An honest experiment, not a tier replacement. The question: does a small
QLoRA-fine-tuned **text** model, applied as a *post-cascade field
corrector*, raise field-level F1 over the raw cascade output?

Decisions (surfaced to + confirmed with Mark at Phase 9 entry):

- **Shape: text post-corrector**, not a VL Tier-2 swap. The `corrections`
  corpus is text (`field, original, corrected`) with no image; a text LLM
  cannot replace a *vision* Tier 2/3. So the fine-tuned model is evaluated
  as a layer *after* the cascade, scored through the **existing** harness
  metric. The frozen cascade providers, replay-cache fixtures, and the
  two-stage F1 artifact are **not** touched.
- **Base model: Qwen2.5-7B** (cascade-family coherence; "Llama 3.1 13B"
  from the original plan does not exist — 3.1 ships 8B/70B/405B). 4-bit
  QLoRA fits one 15.6 GB GPU for short intake-field sequences; the 32 GB
  combined budget is not the constraint at this size.
- **Honest result.** The committed corpus is 6 CMS-1500, all `test`
  split (0 `train`) — the manifest *is* the leakage guard. There is no
  non-leaky training signal at V1 committed scale, so the credible Phase 9
  finding is the experiment *pipeline + harness*, with the real number
  produced when the deferred local 500-doc corpus populates `train`. This
  is stated plainly (same seeded-vs-live honesty as Phase 6/7/8), not
  papered over with a result computed on the eval docs.

Heavy deps (`peft`/`bitsandbytes`/`transformers`/`trl`) are **undeclared +
lazy-imported**, same pattern as `colpali-engine`/`paddlepaddle-gpu`:
training is GPU-box-only behind ``FINETUNE_LIVE=true`` and never runs in CI;
without an adapter the post-corrector degrades to an **identity no-op** so
the whole pipeline is unit-testable for $0 with no GPU.

Modules: :mod:`finetune.dataset` (corrections → JSONL, manifest-split,
leakage-guarded), :mod:`finetune.train` (QLoRA SFT, gated),
:mod:`finetune.correct` (apply adapter / identity degrade),
:mod:`finetune.evaluate` (cascade-stage field-F1 delta via the existing
`score_form`).
"""
