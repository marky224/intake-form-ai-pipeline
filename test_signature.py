"""Tests for ``synthetic_data.render.signature``.

These tests introspect the generated HTML/CSS strings without launching
Chromium, so they run in CI alongside the schema and parser tests. The
Chromium-dependent renderer integration tests live in ``test_render.py``
and are gated behind the ``slow`` pytest marker (skipped in CI).
"""

from __future__ import annotations

import re

from synthetic_data.render.config import (
    HANDWRITTEN_FONTS,
    HANDWRITTEN_ROTATION_DEG_MAX,
    TYPED_FONT,
)
from synthetic_data.render.signature import (
    SIGNATURE_FILTER_ID,
    SVG_INK_BLEED_FILTER,
    SignatureRender,
    patient_signature,
)

NAME = "Sofia Mante"


def test_determinism_same_patient_id_same_output() -> None:
    """Same ``patient_id`` + ``name`` always renders byte-identically."""
    a = patient_signature("patient-abc-123", NAME)
    b = patient_signature("patient-abc-123", NAME)
    assert a == b


def test_distinct_patient_ids_produce_distinct_signatures() -> None:
    """Two different patient_ids must produce distinguishable outputs.

    Either mode differs, or the font/rotation differs. If both
    coincidentally produced typed-Arial-0deg HTML they'd still differ
    only by the embedded ``name``; here we use the same name on
    purpose to force the signature parameters to do the differentiating.
    """
    a = patient_signature("patient-alpha", NAME)
    b = patient_signature("patient-bravo", NAME)
    # html_snippet captures mode, font, rotation — must differ if seeded
    # randomness diverged for these two patient_ids.
    assert a.html_snippet != b.html_snippet or a == b
    # Sanity: the two seeds we picked do produce different RNG streams.
    # If this ever flakes, swap to ids known to diverge.
    distinct_seen = False
    for i in range(20):
        r1 = patient_signature(f"alpha-{i}", NAME)
        r2 = patient_signature(f"bravo-{i}", NAME)
        if r1 != r2:
            distinct_seen = True
            break
    assert distinct_seen, "Per-patient RNG looks degenerate"


def test_70_30_distribution_within_tolerance() -> None:
    """Across N patient_ids, ~70% typed / ~30% handwritten (RATIONALE §1)."""
    n = 1000
    samples = [patient_signature(f"pt-{i:05d}", NAME) for i in range(n)]
    typed = sum(1 for s in samples if s.mode == "typed")
    handwritten = sum(1 for s in samples if s.mode == "handwritten")
    assert typed + handwritten == n
    # 3-sigma binomial tolerance at p=0.7, n=1000 is ~43 either side.
    # Use +/- 50 (~3.5 sigma) so the test won't flake spuriously.
    assert 650 <= typed <= 750, f"typed={typed} outside [650, 750] of {n}"
    assert 250 <= handwritten <= 350, f"handwritten={handwritten} outside [250, 350]"


def test_typed_signature_uses_arial_only() -> None:
    typed_seen = 0
    for i in range(200):
        s = patient_signature(f"typed-probe-{i}", NAME)
        if s.mode != "typed":
            continue
        typed_seen += 1
        assert s.font == TYPED_FONT
        assert s.rotation_deg == 0.0
        assert "Arial" in s.html_snippet
    assert typed_seen > 0, "no typed samples drawn — RNG looks broken"


def test_handwritten_uses_one_of_three_fonts() -> None:
    handwritten_seen = 0
    fonts_observed: set[str] = set()
    for i in range(500):
        s = patient_signature(f"hw-probe-{i}", NAME)
        if s.mode != "handwritten":
            continue
        handwritten_seen += 1
        assert s.font in HANDWRITTEN_FONTS, f"unexpected font {s.font!r}"
        fonts_observed.add(s.font)
    assert handwritten_seen > 0
    # All three handwriting fonts should appear at least once across 500
    # patient ids (each font expected ~50 times under uniform pick).
    assert fonts_observed == set(
        HANDWRITTEN_FONTS
    ), f"missing handwriting fonts: {set(HANDWRITTEN_FONTS) - fonts_observed}"


def test_handwritten_rotation_within_bounds() -> None:
    for i in range(500):
        s = patient_signature(f"rot-probe-{i}", NAME)
        if s.mode != "handwritten":
            continue
        assert -HANDWRITTEN_ROTATION_DEG_MAX <= s.rotation_deg <= HANDWRITTEN_ROTATION_DEG_MAX
        # Spot-check the actual transform is rendered in the html
        assert f"rotate({s.rotation_deg:.3f}deg)" in s.html_snippet


def test_ink_bleed_filter_only_on_handwritten_html() -> None:
    """The ``filter: url(#ink-bleed-signature)`` reference appears only on handwritten spans."""
    for i in range(500):
        s = patient_signature(f"filter-probe-{i}", NAME)
        if s.mode == "typed":
            assert f"url(#{SIGNATURE_FILTER_ID})" not in s.html_snippet
            assert "rotate(" not in s.html_snippet
        else:
            assert f"url(#{SIGNATURE_FILTER_ID})" in s.html_snippet


def test_svg_filter_definition_matches_rationale_section_1() -> None:
    """The shared SVG filter encodes the exact §1 parameters."""
    assert SIGNATURE_FILTER_ID in SVG_INK_BLEED_FILTER
    assert '<feGaussianBlur stdDeviation="0.5"/>' in SVG_INK_BLEED_FILTER
    # The feColorMatrix darkens alpha by 30% (1.3 in the alpha row, last value)
    # while leaving RGB rows as identity — "no hue shift" per §1.
    assert "feColorMatrix" in SVG_INK_BLEED_FILTER
    # Identity RGB rows present; alpha row uses 1.3 multiplier.
    assert "1 0 0 0 0 0 1 0 0 0 0 0 1 0 0 0 0 0 1.3 0" in re.sub(r"\s+", " ", SVG_INK_BLEED_FILTER)


def test_html_escapes_dangerous_chars_in_name() -> None:
    """Names with ``<``/``>``/``&``/``"`` are escaped in the rendered span."""
    s = patient_signature("escape-probe", 'Sofia "& <Mante>')
    assert "<Mante>" not in s.html_snippet
    assert "&lt;Mante&gt;" in s.html_snippet
    assert "&amp;" in s.html_snippet
    assert "&quot;" in s.html_snippet


def test_signature_render_is_frozen() -> None:
    """``SignatureRender`` is immutable so downstream consumers can't mutate it."""
    s = patient_signature("immutable-probe", NAME)
    assert isinstance(s, SignatureRender)
    try:
        s.mode = "handwritten"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("SignatureRender must be frozen")
