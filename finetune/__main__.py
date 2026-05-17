"""``python -m finetune <data|train|eval>`` — the Phase 9 experiment CLI.

- ``data``  — build the train JSONL (cached/$0). Honest summary; 0
  ``seeded_train`` on the committed manifest is expected.
- ``train`` — QLoRA SFT (GPU-box-only; ``FINETUNE_LIVE=true just
  finetune-train``). No-op with a clear message otherwise.
- ``eval``  — cascade-stage field-F1 delta, baseline vs. post-corrector
  (cached/$0; identity baseline without a trained adapter).
"""

from __future__ import annotations

import sys

from finetune.dataset import build_training_jsonl
from finetune.evaluate import evaluate
from finetune.train import FineTuneUnavailable, train


def _data() -> int:
    summary = build_training_jsonl()
    print(
        f"wrote {summary['path']}\n"
        f"  seeded_train          : {summary['seeded_train']}\n"
        f"  synthetic_format_kind : {summary['synthetic_format_kind']}\n"
        f"  total                 : {summary['total']}\n\n{summary['note']}"
    )
    return 0


def _train() -> int:
    try:
        adapter = train()
    except FineTuneUnavailable as exc:
        print(exc, file=sys.stderr)
        return 1
    print(f"adapter saved → {adapter}")
    return 0


def _eval() -> int:
    r = evaluate()
    print(
        f"docs={r.doc_count}  adapter_present={r.adapter_present}\n"
        f"baseline  cascade-stage F1 : {r.baseline_f1:.4f}\n"
        f"corrected cascade-stage F1 : {r.corrected_f1:.4f}\n"
        f"delta                      : {r.delta_f1:+.4f}\n\n{r.note}"
    )
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "data":
        return _data()
    if cmd == "train":
        return _train()
    if cmd == "eval":
        return _eval()
    print("usage: python -m finetune <data|train|eval>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
