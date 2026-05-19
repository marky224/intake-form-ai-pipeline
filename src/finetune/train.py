"""QLoRA SFT on Qwen2.5-7B-Instruct — GPU-box-only, FINETUNE_LIVE-gated.

Same gating discipline as `rag.embed` / the cascade providers: the heavy
stack (`torch`/`transformers`/`peft`/`bitsandbytes`/`trl`) is **undeclared
and lazy-imported** (it is GPU-box-only; CI must never install or import
it). Training runs only when ``FINETUNE_LIVE=true``; otherwise
:func:`train` is a hard no-op that explains itself. The produced LoRA
adapter is written to a **gitignored** ``data/`` path — never committed
(an adapter is multi-hundred-MB binary and reproducible from the script).

4-bit NF4 QLoRA on a 7B fits one 15.6 GB GPU comfortably for the short
single-field correction sequences here, so the 32 GB combined budget is
not the constraint (Phase 9 entry decision, confirmed with Mark). The
cascade is not resident during a training run.

Why this can't produce a headline number in CI or from the build machine:
the committed corpus is 6 CMS-1500, all ``test`` — `finetune.dataset`
yields **0** real training pairs by design (leakage guard). The real
fine-tune + measured F1 delta is the ``FINETUNE_LIVE=true just
finetune-train`` step on ``openclaw-pc`` once the deferred local corpus
populates ``train`` (handoff: ``phase9-live-train.md``). The committed
deliverable is the reproducible pipeline + harness + honest write-up.
"""

from __future__ import annotations

import os
from pathlib import Path

from _paths import src_root
from finetune.dataset import TRAIN_JSONL_PATH

#: Cascade-family base model (Phase 9 decision: "Llama 3.1 13B" does not
#: exist — 3.1 ships 8B/70B/405B; Qwen2.5 keeps coherence with the Qwen 2.5
#: VL cascade tiers and is a realistic Tier-2-adjacent story).
QWEN_BASE_MODEL = "Qwen/Qwen2.5-7B-Instruct"

#: Gitignored adapter output dir (``src/data/`` is fully gitignored — see
#: memory project_src_layout).
ADAPTER_DIR = src_root() / "data" / "finetune_adapter"

#: Truthy values for the live gate (mirrors ``cascade.eval_cache``).
_LIVE_TRUTHY = frozenset({"1", "true", "yes", "y", "on"})


class FineTuneUnavailable(RuntimeError):
    """Training was requested without ``FINETUNE_LIVE`` / a GPU stack."""


def is_finetune_live() -> bool:
    """True iff ``FINETUNE_LIVE`` is set truthy (read fresh every call)."""
    return os.environ.get("FINETUNE_LIVE", "").strip().lower() in _LIVE_TRUTHY


def _read_jsonl(path: Path) -> list[dict[str, str]]:
    import json

    rows: list[dict[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def train(
    *,
    jsonl_path: Path | str = TRAIN_JSONL_PATH,
    adapter_dir: Path | str = ADAPTER_DIR,
    base_model: str = QWEN_BASE_MODEL,
    epochs: int = 3,
    max_seq_len: int = 512,
) -> Path:
    """Run 4-bit QLoRA SFT; return the adapter dir. GPU-box-only.

    Raises :class:`FineTuneUnavailable` unless ``FINETUNE_LIVE`` is set, so
    a misconfigured CI/local invocation fails loudly instead of silently
    importing the heavy stack. With 0 training rows (committed manifest)
    it raises a clear "no signal at V1 scale" message rather than training
    on nothing.
    """
    if not is_finetune_live():
        raise FineTuneUnavailable(
            "FINETUNE_LIVE is not set. QLoRA training is GPU-box-only; run "
            "`FINETUNE_LIVE=true just finetune-train` on openclaw-pc "
            "(see .claude-context/starter-prompts/phase9-live-train.md)."
        )

    jsonl_path = Path(jsonl_path)
    rows = _read_jsonl(jsonl_path) if jsonl_path.is_file() else []
    real = [r for r in rows if r.get("source") == "seeded_correction"]
    if not real:
        raise FineTuneUnavailable(
            f"{jsonl_path} has 0 `seeded_correction` rows — the committed "
            "6-doc corpus is all `test` (leakage guard). Populate `train` "
            "with the deferred local 500-doc corpus before a real run; only "
            "the synthetic pipeline-smoke set is present."
        )

    # Heavy stack: imported only here, only under FINETUNE_LIVE, on the GPU
    # box. Undeclared deps (paddlepaddle-gpu / colpali-engine pattern).
    import torch
    from datasets import Dataset
    from peft import LoraConfig
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        BitsAndBytesConfig,
    )
    from trl import SFTConfig, SFTTrainer

    tokenizer = AutoTokenizer.from_pretrained(base_model)

    def _format(row: dict[str, str]) -> dict[str, str]:
        messages = [
            {"role": "system", "content": row["instruction"]},
            {"role": "user", "content": row["input"]},
            {"role": "assistant", "content": row["response"]},
        ]
        return {"text": tokenizer.apply_chat_template(messages, tokenize=False)}

    dataset = Dataset.from_list([_format(r) for r in rows])

    bnb = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=torch.bfloat16,
        bnb_4bit_use_double_quant=True,
    )
    model = AutoModelForCausalLM.from_pretrained(
        base_model, quantization_config=bnb, device_map="cuda:0"
    )
    lora = LoraConfig(
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    adapter_dir = Path(adapter_dir)
    adapter_dir.mkdir(parents=True, exist_ok=True)
    trainer = SFTTrainer(
        model=model,
        train_dataset=dataset,
        peft_config=lora,
        args=SFTConfig(
            output_dir=str(adapter_dir / "_run"),
            num_train_epochs=epochs,
            per_device_train_batch_size=4,
            gradient_accumulation_steps=4,
            learning_rate=2e-4,
            bf16=True,
            max_seq_length=max_seq_len,
            logging_steps=5,
            report_to=[],
        ),
    )
    trainer.train()
    trainer.save_model(str(adapter_dir))
    tokenizer.save_pretrained(str(adapter_dir))
    return adapter_dir
