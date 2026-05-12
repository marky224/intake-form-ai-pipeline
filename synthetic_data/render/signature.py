"""Programmatic signature rendering for synthetic CMS-1500 forms.

Implements the locked rendering parameters in ``RATIONALE.md`` Section 1
verbatim:

* Typed signatures use Arial, 0 degrees rotation, no ink-bleed filter.
* Handwritten signatures pick a font from {Caveat, Sacramento,
  Homemade Apple} uniformly at random, rotate by ±3 degrees uniform,
  and route through an SVG ``<filter>`` combining
  ``<feGaussianBlur stdDeviation="0.5"/>`` with a subtle
  ``<feColorMatrix>`` alpha darkening.
* 70% typed / 30% handwritten via a single seeded ``random.random() <
  0.7`` check per signature instance.

Determinism: each call derives a per-patient RNG from
``PROJECT_SEED`` mixed with a stable SHA-256 hash of ``patient_id``.
That way the rendering of any single patient is order-independent —
re-rendering a subset produces byte-identical signature parameters.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass
from typing import Literal

from .config import (
    HANDWRITTEN_FONTS,
    HANDWRITTEN_ROTATION_DEG_MAX,
    PROJECT_SEED,
    TYPED_FONT,
    TYPED_SIGNATURE_PROBABILITY,
)

SignatureMode = Literal["typed", "handwritten"]

SIGNATURE_FILTER_ID = "ink-bleed-signature"
"""SVG ``<filter>`` element id referenced by every handwritten signature."""

SVG_INK_BLEED_FILTER = (
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true">'
    "<defs>"
    f'<filter id="{SIGNATURE_FILTER_ID}" x="-5%" y="-5%" width="110%" height="110%">'
    '<feGaussianBlur stdDeviation="0.5"/>'
    '<feColorMatrix type="matrix" values="'
    "1 0 0 0 0 "
    "0 1 0 0 0 "
    "0 0 1 0 0 "
    '0 0 0 1.3 0"/>'
    "</filter></defs></svg>"
)
"""Shared SVG ``<filter>`` definition the rendered HTML embeds once.

The ``feColorMatrix`` RGB rows are identity (no hue shift, per §1); the
alpha row's 1.3 multiplier darkens the existing alpha by 30%, simulating
pen-pressure variance. The blur ``stdDeviation="0.5"`` simulates ink
absorption into paper. Both values are locked in RATIONALE §1.
"""


@dataclass(frozen=True)
class SignatureRender:
    """One rendered signature's HTML snippet plus the sidecar metadata.

    The renderer embeds ``html_snippet`` inside the CMS-1500 template
    (Box 12). The remaining fields populate the sidecar JSON that
    Phase 6 will consume for eval against the cascade's
    ``SignatureCapture`` output.
    """

    mode: SignatureMode
    font: str
    rotation_deg: float
    name: str
    html_snippet: str


def _patient_rng(patient_id: str) -> random.Random:
    """Per-patient deterministic RNG seeded by PROJECT_SEED ⊕ sha256(id)."""
    digest = hashlib.sha256(patient_id.encode("utf-8")).digest()
    patient_seed_int = int.from_bytes(digest[:8], "big")
    mixed = PROJECT_SEED ^ patient_seed_int
    return random.Random(mixed)


def patient_signature(patient_id: str, name: str) -> SignatureRender:
    """Render one signature for ``(patient_id, name)``.

    Deterministic: same ``patient_id`` + same ``PROJECT_SEED`` always
    produces the same output. The three random draws happen in a fixed
    order (mode, then font, then rotation) so future additions to the
    pipeline don't perturb the corpus's existing signatures.
    """
    rng = _patient_rng(patient_id)
    is_typed = rng.random() < TYPED_SIGNATURE_PROBABILITY
    if is_typed:
        return _typed(name)
    font = rng.choice(HANDWRITTEN_FONTS)
    rotation = rng.uniform(-HANDWRITTEN_ROTATION_DEG_MAX, HANDWRITTEN_ROTATION_DEG_MAX)
    return _handwritten(name, font, rotation)


def _typed(name: str) -> SignatureRender:
    # Single-quote the font name inside the double-quoted style attribute
    # so Chromium doesn't truncate the attribute at a nested double quote.
    style = f"font-family: '{TYPED_FONT}', sans-serif; font-size: 14px;"
    html = f'<span class="signature signature-typed" style="{style}">{_escape(name)}</span>'
    return SignatureRender(
        mode="typed",
        font=TYPED_FONT,
        rotation_deg=0.0,
        name=name,
        html_snippet=html,
    )


def _handwritten(name: str, font: str, rotation_deg: float) -> SignatureRender:
    # Single-quote the font name inside the double-quoted style attribute
    # so Chromium doesn't truncate the attribute at a nested double quote.
    # The single quotes also handle multi-word families like "Homemade Apple".
    style = (
        f"font-family: '{font}', cursive; font-size: 22px; "
        f"filter: url(#{SIGNATURE_FILTER_ID}); "
        "display: inline-block; "
        f"transform: rotate({rotation_deg:.3f}deg);"
    )
    html = (
        '<span class="signature signature-handwritten" '
        f'data-font="{font}" data-rotation="{rotation_deg:.3f}" '
        f'style="{style}">{_escape(name)}</span>'
    )
    return SignatureRender(
        mode="handwritten",
        font=font,
        rotation_deg=rotation_deg,
        name=name,
        html_snippet=html,
    )


def _escape(s: str) -> str:
    """Minimal HTML-attribute-safe escape for name fields."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
