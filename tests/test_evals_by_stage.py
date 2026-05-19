"""Deterministic by-stage SVG + committed-artifact drift guard.

Mirrors ``test_evals_chart.py``. The by-stage measurement is fully
cached/deterministic from the committed 92-doc fixtures, so both the
measured numbers and the rendered SVG are pinned: a change to the
cascade, the fixtures, or the renderer that moves either is a deliberate,
visible diff (regenerate via ``just by-stage``).
"""

from __future__ import annotations

import pytest

from evals.by_stage import (
    BY_STAGE_CHART_PATH,
    ByStage,
    Funnel,
    compute_by_stage,
    render_by_stage_svg,
)

# Measured 2026-05-18, cached, $0 (memory project_tier3_q4km_net_negative).
# The Q4_K_M Tier-3 regression (0.768 < 0.794) is intentional and shipped.
_EXPECTED_F1 = (0.340, 0.794, 0.768)
_EXPECTED_FUNNEL = Funnel(tier1=2, tier2=795, tier3a=184, blank=123)


def _sample() -> ByStage:
    return ByStage(
        stages=[("Tier 1", 0.340), ("Tier 1+2", 0.794), ("Tier 1+2+3", 0.768)],
        funnel=_EXPECTED_FUNNEL,
    )


def test_render_is_deterministic_and_has_no_timestamp():
    a = render_by_stage_svg(_sample(), seed_version="1.0.0")
    b = render_by_stage_svg(_sample(), seed_version="1.0.0")
    assert a == b
    assert "<svg" in a and a.endswith("</svg>\n")
    assert "0.340" in a and "0.794" in a and "0.768" in a
    assert "alias_table_seed v1.0.0" in a
    # Both panels present.
    assert "F1 by cumulative cascade stage" in a
    assert "Escalation funnel" in a
    # Funnel remainder shown explicitly, not hidden.
    assert "123 genuinely-blank cells" in a
    # No churn vectors in a committed artifact ("720"/"786" dims are fine).
    for token in ("generated_at", "git", "T00:"):
        assert token not in a


def test_empty_stage_list_rejected():
    with pytest.raises(ValueError, match="empty stage list"):
        render_by_stage_svg(ByStage(stages=[], funnel=_EXPECTED_FUNNEL), seed_version="1.0.0")


def test_funnel_is_monotone_and_totals_consistent():
    f = _EXPECTED_FUNNEL
    assert f.total == 1104
    assert f.populated == 981
    cum = [c for _, c in f.cumulative]
    assert cum == [2, 797, 981]
    assert cum == sorted(cum), "the funnel must be monotone by tier"


def test_measured_numbers_match_committed_fixtures():
    """The F1 triple + funnel are deterministic from the committed
    fixtures. Tight tolerance — these are exact, not noisy."""
    result = compute_by_stage()
    got_f1 = tuple(round(f1, 3) for _, f1 in result.stages)
    assert got_f1 == _EXPECTED_F1, f"by-stage F1 drifted: {got_f1}"
    assert result.funnel == _EXPECTED_FUNNEL, f"funnel drifted: {result.funnel}"


def test_committed_chart_in_sync_with_by_stage():
    """Drift guard: the committed SVG must equal the chart rendered from a
    fresh cached measurement. Regenerate via ``just by-stage`` if this
    fails."""
    from evals.manifest import load_manifest

    seed_version, _ = load_manifest()
    expected = render_by_stage_svg(compute_by_stage(), seed_version=seed_version)
    assert (
        BY_STAGE_CHART_PATH.read_text(encoding="utf-8") == expected
    ), "docs/assets/f1-by-stage.svg is stale — run `just by-stage`"
