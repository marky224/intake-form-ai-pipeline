"""Playwright-driven CMS-1500 renderer for the Synthea synthetic data pipeline.

Phase 3 PR (b): consumes a ``SyntheaPatient`` from
``synthetic_data.synthea.parse`` and produces a PNG-per-page rendering of a
CMS-1500-inspired template plus a sidecar JSON capturing field-level
bounding boxes for downstream eval.

Signature rendering parameters (typed/handwritten split, font choices,
ink-bleed filter, rotation jitter, distribution, seed) are locked in
``RATIONALE.md`` Section 1 and implemented in ``signature.py``.
"""
