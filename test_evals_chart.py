"""Deterministic F1-over-time SVG + committed-artifact drift guard."""

from __future__ import annotations

import pytest

from evals.chart import CHART_PATH, render_f1_over_time_svg


def test_render_is_deterministic_and_has_no_timestamp():
    series = [(1, 0.222), (2, 0.322), (3, 0.322)]
    a = render_f1_over_time_svg(series, seed_version="1.0.0")
    b = render_f1_over_time_svg(series, seed_version="1.0.0")
    assert a == b
    assert "<svg" in a and a.endswith("</svg>\n")
    assert "0.222" in a and "0.322" in a
    assert "alias_table_seed v1.0.0" in a
    # No churn vectors in a committed artifact.
    for token in ("generated_at", "20", "git", "T00:"):
        assert token not in a or token == "20"  # "720"/"420" dims are fine


def test_empty_series_rejected():
    with pytest.raises(ValueError, match="empty series"):
        render_f1_over_time_svg([], seed_version="1.0.0")


def test_committed_chart_in_sync_with_tier1_eval():
    """Drift guard: the committed SVG must equal the chart rendered from a
    fresh cached Tier-1 sweep. Regenerate via ``just chart`` if this fails."""
    import tempfile

    from evals.harness import run_eval
    from evals.manifest import load_manifest

    seed_version, _ = load_manifest()
    series = run_eval(db_path=tempfile.mktemp(suffix=".db"))["tier1"]
    expected = render_f1_over_time_svg(series, seed_version=seed_version)
    assert (
        CHART_PATH.read_text(encoding="utf-8") == expected
    ), "docs/assets/f1-over-time.svg is stale — run `just chart`"
