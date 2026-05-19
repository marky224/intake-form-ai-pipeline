"""Apply the QLoRA adapter as a post-cascade field corrector.

The fine-tuned model never sees the document image — it is a *text*
post-processor over the cascade's already-extracted values (Phase 9 shape
decision). It corrects **populated** fields only; a confidently-blank
field (``value is None`` with ``tier_used`` set) is left untouched so the
locked blank contract and the two-stage framing stay intact.

**Identity-degrade contract.** When no trained adapter is present (always
the case in CI / on the build machine — the adapter is a gitignored
GPU-box artifact), :func:`correct_field` returns the cascade value
unchanged and **nothing from the heavy stack is imported**. So the entire
evaluate pipeline runs $0/no-GPU and honestly reports a **zero delta
identity baseline**; a real delta only appears after
``FINETUNE_LIVE=true just finetune-train`` on ``openclaw-pc``.

:func:`corrected_form` returns a light shim whose scorable attributes look
like ``ExtractedField`` (``.value`` / ``.tier_used``) so the *existing*
``evals.metrics.score_form`` + ``evals.ground_truth.extracted_token`` score
the corrected values with the **identical** TP/FP/FN definition the harness
uses — the metric is reused, not re-implemented.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from finetune.dataset import CORRECTOR_INSTRUCTION, _field_input
from finetune.train import ADAPTER_DIR, QWEN_BASE_MODEL

#: Process-wide lazily-built (model, tokenizer); only ever populated on a
#: GPU box where a trained adapter exists.
_PIPE: Any = None


@dataclass(frozen=True)
class _Field:
    """Minimal ``ExtractedField`` stand-in for ``score_form``."""

    value: str | None
    tier_used: Any


class _CorrectedForm:
    """Exposes corrected scorable fields as ``_Field`` attributes."""

    def __init__(self, fields: dict[str, _Field]) -> None:
        self._fields = fields

    def __getattr__(self, name: str) -> Any:
        # score_form/extracted_token do getattr(form, name); unknown →
        # None-like field (treated as unpopulated, same as a real form).
        return self._fields.get(name, _Field(None, None))


def adapter_available(adapter_dir: Path | str = ADAPTER_DIR) -> bool:
    """True iff a saved PEFT adapter exists (GPU-box post-train only)."""
    return (Path(adapter_dir) / "adapter_config.json").is_file()


def _load_pipe(adapter_dir: Path) -> Any:
    """Lazily load base+adapter. Only reached when an adapter exists."""
    global _PIPE
    if _PIPE is not None:
        return _PIPE
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(str(adapter_dir))
    base = AutoModelForCausalLM.from_pretrained(
        QWEN_BASE_MODEL, torch_dtype=torch.bfloat16, device_map="cuda:0"
    )
    model = PeftModel.from_pretrained(base, str(adapter_dir)).eval()
    _PIPE = (model, tok)
    return _PIPE


def correct_field(
    field_name: str,
    kind: str,
    cascade_value: str | None,
    adapter_dir: Path | str = ADAPTER_DIR,
) -> str | None:
    """Corrected value for one populated field, or identity if no adapter.

    ``None`` in → ``None`` out (blanks are never hallucinated into). No
    adapter → identity (and no heavy import). Adapter present → greedy
    single-field generation with the train-time prompt format.
    """
    if cascade_value is None:
        return None
    if not adapter_available(adapter_dir):
        return cascade_value  # identity-degrade — the CI/no-GPU contract

    model, tok = _load_pipe(Path(adapter_dir))
    messages = [
        {"role": "system", "content": CORRECTOR_INSTRUCTION},
        {"role": "user", "content": _field_input(field_name, kind, cascade_value)},
    ]
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    inputs = tok(prompt, return_tensors="pt").to(model.device)
    out = model.generate(**inputs, max_new_tokens=64, do_sample=False)
    text = tok.decode(out[0][inputs["input_ids"].shape[1] :], skip_special_tokens=True)
    return text.strip() or cascade_value


def corrected_form(
    form: Any,
    scorable_fields: list[str],
    adapter_dir: Path | str = ADAPTER_DIR,
) -> _CorrectedForm:
    """A ``score_form``-compatible shim with each scorable field corrected.

    Reads each field's raw extracted value (not the canonical token — the
    corrector sees what the cascade produced) and runs it through
    :func:`correct_field`, preserving ``tier_used`` so confidently-blank
    accounting is unchanged.
    """
    from evals.ground_truth import FIELD_KIND

    fields: dict[str, _Field] = {}
    for name in scorable_fields:
        ef = getattr(form, name, None)
        value = getattr(ef, "value", None) if ef is not None else None
        tier_used = getattr(ef, "tier_used", None) if ef is not None else None
        new_value = correct_field(name, FIELD_KIND.get(name, "str"), value, adapter_dir)
        fields[name] = _Field(new_value, tier_used)
    return _CorrectedForm(fields)
