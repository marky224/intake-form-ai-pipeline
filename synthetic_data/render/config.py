"""Project-wide reproducibility seed for the synthetic-data renderer.

Per ``RATIONALE.md`` Section 1: "Single project-wide seed defined in
``synthetic_data/render/config.py``. The synthetic dataset for any given
commit + seed combination is reproducible byte-for-byte. Changing the
seed regenerates the corpus; the same seed always produces the same
documents."

Each per-patient render derives a deterministic sub-seed from this
project seed mixed with the Synthea patient id — see
``signature.py::patient_signature``. That way the rendering of any one
patient is independent of render order, and re-rendering a subset
produces byte-identical output.
"""

from __future__ import annotations

PROJECT_SEED: int = 0x1A7E_F00D
"""Project-wide reproducibility seed.

The literal value is arbitrary; changing it regenerates the entire
synthetic corpus. The 32-bit constant fits cleanly into Python's
``random.Random`` seed argument and produces a different mixed value
when XOR'd with any reasonable patient-id hash.
"""

PAGE_WIDTH_PX: int = 850
"""Rendered CMS-1500 page width in pixels (8.5 inches @ 100 DPI)."""

PAGE_HEIGHT_PX: int = 1100
"""Rendered CMS-1500 page height in pixels (11 inches @ 100 DPI)."""

TYPED_SIGNATURE_PROBABILITY: float = 0.70
"""70/30 typed-vs-handwritten split per RATIONALE §1."""

HANDWRITTEN_FONTS: tuple[str, ...] = ("Caveat", "Sacramento", "Homemade Apple")
"""CSS family names for the three vendored handwriting fonts. Order is
stable so the seeded random.choice() is reproducible."""

TYPED_FONT: str = "Arial"
"""CSS family name for typed signatures. RATIONALE §1: Arial only."""

HANDWRITTEN_ROTATION_DEG_MAX: float = 3.0
"""±3 degrees rotation jitter on handwritten signatures only."""
