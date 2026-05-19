"""Field-level F1, latency p50/p99, and (V1: $0) cost.

F1 follows ``docs/eval-methodology.md`` verbatim, micro-averaged over
field-level counts across the batch:

- **TP** — populated field whose canonical value matches ground truth.
- **FP** — populated field whose value mismatches ground truth, *or* a
  populated field that has no ground truth (an extracted ghost value).
- **FN** — ground truth present but the field is unpopulated *or* populated
  with a wrong value (a wrong populated value is therefore both FP and FN,
  exactly as the methodology defines).

A *confidently-blank* field (``value is None`` with ``tier_used`` set — the
cascade affirmatively judged the field absent) is **excluded** from
precision/recall and tracked separately, per the locked methodology. An
*unattempted* field (``tier_used is None``) with ground truth present is a
plain FN (extraction returned nothing).

The scored field set is passed in by the caller: the CMS-1500 schema-mapped
set for healthcare (``ground_truth.FIELD_KIND``), the full
``BusinessDocumentForm`` field set for DocILE. Fields outside that set
(schema fields the form's source corpus never renders) are not scored.

Latency is wall-clock per document from the orchestrator's ``RunRecord``;
p50/p99 are nearest-rank percentiles over the batch. Cost is identically
``0.0`` in V1 (no cloud surface) — the column exists for V2 schema
continuity and the latency curve carries the self-improvement narrative.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from evals.ground_truth import GroundTruth, extracted_token


@dataclass(frozen=True)
class Counts:
    """Micro TP/FP/FN with derived precision/recall/F1.

    ``blank`` and ``unattempted_no_gt`` are tracked for the reviewer UI /
    diagnostics; they never enter precision or recall.
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0
    blank_excluded: int = 0

    def __add__(self, other: Counts) -> Counts:
        return Counts(
            self.tp + other.tp,
            self.fp + other.fp,
            self.fn + other.fn,
            self.blank_excluded + other.blank_excluded,
        )

    @property
    def precision(self) -> float:
        denom = self.tp + self.fp
        return self.tp / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.tp + self.fn
        return self.tp / denom if denom else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


@dataclass(frozen=True)
class BatchMetrics:
    """Aggregate metrics for one eval batch (one alias-partition step)."""

    counts: Counts
    latency_p50_ms: float
    latency_p99_ms: float
    cost_per_doc_usd: float
    doc_count: int
    per_vertical: dict[str, Counts] = field(default_factory=dict)

    @property
    def f1(self) -> float:
        return self.counts.f1


def score_form(form: Any, truth: GroundTruth, scorable_fields: list[str]) -> Counts:
    """TP/FP/FN for one form against its ground truth.

    ``scorable_fields`` is the set of schema fields this corpus can render;
    a populated field in that set with no ground truth is a ghost (FP).
    """
    tp = fp = fn = blank = 0
    for name in scorable_fields:
        ef = getattr(form, name, None)
        value = getattr(ef, "value", None) if ef is not None else None
        tier_used = getattr(ef, "tier_used", None) if ef is not None else None

        gt = truth.get(name)
        populated = value is not None
        confidently_blank = value is None and tier_used is not None

        if confidently_blank:
            # Affirmative "absent on this form" judgment — out of P/R.
            blank += 1
            continue

        if populated:
            got = extracted_token(name, form)
            if gt is not None and got == gt:
                tp += 1
            elif gt is not None:
                fp += 1
                fn += 1  # wrong populated value: both, per methodology
            else:
                fp += 1  # ghost: populated with no ground truth
        elif gt is not None:
            fn += 1  # ground truth present, extraction returned nothing
        # else: no gt + unpopulated → true negative, uncounted
    return Counts(tp, fp, fn, blank)


def _percentile(samples: list[float], pct: float) -> float:
    """Nearest-rank percentile. Empty → 0.0."""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def aggregate(
    per_doc: list[tuple[str, Counts, float]],
) -> BatchMetrics:
    """Combine per-document ``(vertical, Counts, latency_ms)`` into a batch.

    Cost is ``0.0`` per document in V1. Latency percentiles are over the
    per-document wall-clock times.
    """
    total = Counts()
    per_vertical: dict[str, Counts] = {}
    latencies: list[float] = []
    for vertical, counts, latency_ms in per_doc:
        total = total + counts
        per_vertical[vertical] = per_vertical.get(vertical, Counts()) + counts
        latencies.append(latency_ms)
    return BatchMetrics(
        counts=total,
        latency_p50_ms=_percentile(latencies, 50.0),
        latency_p99_ms=_percentile(latencies, 99.0),
        cost_per_doc_usd=0.0,
        doc_count=len(per_doc),
        per_vertical=per_vertical,
    )
