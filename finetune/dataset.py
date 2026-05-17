"""Corrections → JSONL instruction pairs (manifest-split, leakage-guarded).

Two example sources, both deterministic / cached / $0 / no GPU:

1. **Seeded corrections** — for every manifest ``train`` document, run the
   cached cascade, compare each scorable field's extracted token to the
   CMS-1500 ground truth, and emit a ``(field, cascade-value) → corrected
   value`` instruction pair for the mismatched/missing ones. **The manifest
   is the leakage guard:** the committed corpus is 6 CMS-1500, all ``test``
   (0 ``train``), so this honestly yields **zero** training pairs on the
   committed set. That is the point — there is no non-leaky training signal
   at V1 committed scale; the real pairs appear when the deferred local
   500-doc corpus populates ``train`` (``evals.manifest.assign_split``).
   We report that plainly rather than training on the eval documents.

2. **Synthetic format-kind** — a tiny, hand-curated, leakage-safe table of
   generic ``noisy → canonical`` pairs keyed by ``FIELD_KIND`` category
   (date/sex/…). No test-document value appears in it; it teaches the
   format-normalization *skill* (e.g. ``"01/02/2020" → "2020-01-02"``) so
   the QLoRA pipeline has something to train on end-to-end on the GPU box
   and the unit tests are meaningful. Tagged ``source`` so the write-up can
   separate "pipeline smoke" from "real correction signal".

The eval metric is **not** pair-level — :mod:`finetune.evaluate` measures
cascade-stage field-F1 with vs. without the post-corrector over the
``test`` split via the existing ``score_form``. This module only builds the
*training* JSONL + an honest summary.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

#: Runtime artifact (``data/`` is fully gitignored — never committed, like
#: ``data/v1.db`` / the alias overlay). Module-level so tests can repoint it.
_REPO_ROOT = Path(__file__).resolve().parent.parent
TRAIN_JSONL_PATH = _REPO_ROOT / "data" / "finetune_train.jsonl"

#: The stable corrector instruction. Kept verbatim at train + inference time
#: (``finetune.correct`` imports this) so the fine-tuned behavior matches
#: how it is invoked.
CORRECTOR_INSTRUCTION = (
    "You correct a single extracted form field. Given the field name, its "
    "type, and the value an upstream OCR/VL cascade extracted, output ONLY "
    "the corrected canonical value with no prose or quotes. If the cascade "
    "value is already correct, echo it unchanged."
)

#: Leakage-safe generic format-normalization pairs by FIELD_KIND category.
#: NONE of these are values from the committed CMS-1500 corpus — they teach
#: the canonicalization shape only. ``evals.ground_truth.canonicalize`` is
#: the canonical target space; these mirror its transforms generically.
_SYNTHETIC_BY_KIND: dict[str, list[tuple[str, str]]] = {
    "date": [
        ("01/02/2020", "2020-01-02"),
        ("March 5, 1991", "1991-03-05"),
        ("1991-03-05", "1991-03-05"),
    ],
    "sex": [("Male", "M"), ("female", "F"), ("M", "M")],
    "str": [("  jane   doe ", "jane doe"), ("ACME, INC.", "ACME, INC.")],
}


class Example(BaseModel):
    """One SFT instruction pair (JSONL row)."""

    model_config = ConfigDict(extra="forbid")

    instruction: str
    input: str
    response: str
    #: ``seeded_correction`` (real, manifest-``train`` only) or
    #: ``synthetic_format_kind`` (leakage-safe pipeline smoke).
    source: str
    split: str


def _field_input(field_name: str, kind: str, cascade_value: str | None) -> str:
    """The model's ``input`` block — field name, type, cascade value."""
    shown = "<BLANK>" if cascade_value is None else cascade_value
    return f"Field: {field_name}\nField type: {kind}\nCascade extracted: {shown}"


def build_seeded_correction_examples(
    manifest_path: Path | str | None = None,
) -> list[Example]:
    """Real correction pairs from manifest ``train`` documents only.

    Returns ``[]`` on the committed manifest (all ``test``) — by design,
    the leakage guard. Heavy/first-party imports are local so importing
    this module stays cheap and CI-safe.
    """
    from cascade.orchestrator import process_document
    from evals.ground_truth import FIELD_KIND, extracted_token, load_cms1500_ground_truth
    from evals.manifest import CMS1500_VALIDATION_DIR, MANIFEST_PATH, load_manifest

    _, entries = load_manifest(manifest_path or MANIFEST_PATH)
    out: list[Example] = []
    for entry in entries:
        if entry.split != "train" or entry.vertical != "healthcare":
            continue
        png_path = CMS1500_VALIDATION_DIR / f"{entry.doc_id}.png"
        sidecar = CMS1500_VALIDATION_DIR / f"{entry.doc_id}.json"
        if not png_path.is_file() or not sidecar.is_file():
            continue
        truth = load_cms1500_ground_truth(sidecar)
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            record = process_document(png_path.read_bytes(), doc_id=entry.doc_id, db_path=tmp.name)
        for name, kind in FIELD_KIND.items():
            gt = truth.get(name)
            if gt is None:
                continue
            got = extracted_token(name, record.form)
            if got == gt:
                continue
            out.append(
                Example(
                    instruction=CORRECTOR_INSTRUCTION,
                    input=_field_input(name, kind, got),
                    response=gt,
                    source="seeded_correction",
                    split="train",
                )
            )
    return out


def synthetic_format_kind_examples() -> list[Example]:
    """Deterministic, leakage-safe pipeline-smoke pairs (no test values)."""
    return [
        Example(
            instruction=CORRECTOR_INSTRUCTION,
            input=_field_input(f"<{kind}_field>", kind, noisy),
            response=clean,
            source="synthetic_format_kind",
            split="train",
        )
        for kind, pairs in _SYNTHETIC_BY_KIND.items()
        for noisy, clean in pairs
    ]


def build_training_jsonl(
    out_path: Path | str | None = None,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Write the train JSONL; return an honest summary.

    ``note`` states the V1-committed-scale reality explicitly so the
    write-up (and any future reader) can't mistake the synthetic smoke set
    for real correction signal.
    """
    out_path = Path(out_path) if out_path is not None else TRAIN_JSONL_PATH
    seeded = build_seeded_correction_examples(manifest_path)
    synthetic = synthetic_format_kind_examples()
    rows = seeded + synthetic

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for ex in rows:
            fh.write(json.dumps(ex.model_dump(), sort_keys=True) + "\n")

    return {
        "path": str(out_path),
        "seeded_train": len(seeded),
        "synthetic_format_kind": len(synthetic),
        "total": len(rows),
        "note": (
            "0 seeded_train on the committed manifest is expected — the 6 "
            "CMS-1500 are all `test` (the leakage guard). Real correction "
            "pairs appear only when the deferred local 500-doc corpus "
            "populates `train`. The synthetic_format_kind rows are a "
            "leakage-safe pipeline smoke set, not correction signal."
        ),
    }
