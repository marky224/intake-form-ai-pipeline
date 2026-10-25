"""Deterministic by-stage SVG + committed-artifact drift guard.

The by-stage measurement is fully
cached/deterministic from the committed 92-doc fixtures, so both the
measured numbers and the rendered SVG are pinned: a change to the
cascade, the fixtures, or the renderer that moves either is a deliberate,
visible diff (regenerate via ``just by-stage``).
"""

from __future__ import annotations

import re

import pytest

from evals.by_stage import (
    BY_STAGE_CHART_PATH,
    ByStage,
    Funnel,
    compute_by_stage,
    render_by_stage_svg,
)

# Re-measured 2026-10-24, cached, $0. Tier 1 reads the CMS-1500 boxes and
# finalizes a date, ZIP or state that passes its validator (336 cells); Tier
# 3 reads only the fields Tier 1 covers where Tier 1 and Tier 2 disagree or
# Tier 1 read nothing (agreement with Tier 1 or Tier 2 settles a field; a
# validated Tier-2 value is never overwritten). Tier 1+2 sits below Tier 1:
# Tier 2 replaces correct Tier-1 readings it doesn't finalize.
# Of the 115 empty cells, 92 are missed (signature) and 23 true negatives.
_EXPECTED_F1 = (0.865, 0.807, 0.904)
_EXPECTED_FUNNEL = Funnel(
    tier1=336,
    tier2=526,
    tier3a=127,
    missed=92,
    true_negative=23,
    tier3a_matched_tier1=99,
    tier3a_free_text=26,
    tier3a_tier2_blank=2,
    tier3a_failed_validator=0,
)


def _sample() -> ByStage:
    return ByStage(
        stages=[("Tier 1", 0.865), ("Tier 1+2", 0.807), ("Tier 1+2+3", 0.904)],
        funnel=_EXPECTED_FUNNEL,
    )


def test_render_is_deterministic_and_has_no_timestamp():
    a = render_by_stage_svg(_sample(), seed_version="1.0.0")
    b = render_by_stage_svg(_sample(), seed_version="1.0.0")
    assert a == b
    assert "<svg" in a and a.endswith("</svg>\n")
    assert "0.865" in a and "0.807" in a and "0.904" in a
    assert "alias_table_seed v1.0.0" in a
    # Both panels present.
    assert "F1 by cumulative cascade stage" in a
    assert "Escalation funnel" in a
    # Funnel remainder shown explicitly, not hidden — and split by truth.
    assert "92 missed" in a and "23 blank on form" in a
    assert "92 missed (value on the form, none extracted)" in a
    assert "127 cells (99 matching Tier 1, 26 free text, 2 Tier-2 blanks" in a
    # Every stage label is drawn under both panels (cap labels must not
    # replace the last bar's axis label).
    for stage in ("Tier 1", "Tier 1+2", "Tier 1+2+3"):
        assert a.count(f">{stage}</text>") == 2, stage
    # No churn vectors in a committed artifact ("720"/"786" dims are fine).
    for token in ("generated_at", "git", "T00:"):
        assert token not in a


def test_empty_stage_list_rejected():
    with pytest.raises(ValueError, match="empty stage list"):
        render_by_stage_svg(ByStage(stages=[], funnel=_EXPECTED_FUNNEL), seed_version="1.0.0")


def test_funnel_is_monotone_and_totals_consistent():
    f = _EXPECTED_FUNNEL
    assert f.total == 1104
    assert f.populated == 989
    cum = [c for _, c in f.cumulative]
    assert cum == [336, 862, 989]
    assert cum == sorted(cum), "the funnel must be monotone by tier"
    reasons = (
        f.tier3a_matched_tier1
        + f.tier3a_free_text
        + f.tier3a_tier2_blank
        + f.tier3a_failed_validator
    )
    assert reasons == f.tier3a


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


# --- --split dev (the held-out confirmation run) ---------------------------


def test_compute_by_stage_scores_only_the_requested_split(monkeypatch):
    """``split`` picks the manifest partition and ``image_dir`` where its
    images live. Make three cached test docs the whole ``dev`` split (the
    real dev docs move to ``train``) and point ``image_dir`` at the
    committed validation dir: only those three docs are scored (12 scorable
    cells each)."""
    import dataclasses

    import evals.by_stage as by_stage
    from evals.manifest import CMS1500_VALIDATION_DIR, load_manifest

    seed_version, entries = load_manifest()
    test_ids = {e.doc_id for e in entries if e.split == "test"}
    chosen = sorted(test_ids)[:3]

    def relabel(e):
        if e.doc_id in chosen:
            return dataclasses.replace(e, split="dev")
        return dataclasses.replace(e, split="train") if e.split == "dev" else e

    relabeled = [relabel(e) for e in entries]
    monkeypatch.setattr(by_stage, "load_manifest", lambda: (seed_version, relabeled))
    result = by_stage.compute_by_stage(split="dev", image_dir=CMS1500_VALIDATION_DIR)
    assert result.funnel.total == 3 * 12


def test_compute_by_stage_dev_without_local_corpus_says_where_to_look(tmp_path):
    """The dev split's images are not committed; a missing corpus is a clear
    error naming the directory, not a bare missing-file traceback."""
    with pytest.raises(FileNotFoundError, match=re.escape(str(tmp_path))):
        compute_by_stage(split="dev", image_dir=tmp_path)


def test_cli_by_stage_dev_prints_but_never_rewrites_the_committed_chart(monkeypatch):
    """``python -m evals by-stage --split dev`` measures and logs; the
    committed SVG is the test-split artifact, so only the default (test)
    run may rewrite it."""
    import evals.__main__ as cli

    calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "compute_by_stage", lambda **kw: calls.setdefault("kw", kw) and _sample()
    )
    monkeypatch.setattr(
        cli, "write_by_stage_chart", lambda *a, **k: calls.setdefault("wrote", True)
    )

    assert cli.main(["by-stage", "--split", "dev"]) == 0
    assert calls["kw"] == {"split": "dev", "image_dir": None}
    assert "wrote" not in calls

    calls.clear()
    assert cli.main(["by-stage"]) == 0
    assert calls["kw"] == {"split": "test", "image_dir": None}
    assert calls.get("wrote") is True


def test_cli_by_stage_image_dir_reads_that_folder_and_never_rewrites_the_chart(
    monkeypatch, tmp_path
):
    """``--image-dir`` points the run at an existing render folder (e.g. the
    local corpus in another checkout). A run with it set — dev or test —
    only logs: the committed SVG comes from the committed images alone."""
    import evals.__main__ as cli

    calls: dict[str, object] = {}
    monkeypatch.setattr(
        cli, "compute_by_stage", lambda **kw: calls.setdefault("kw", kw) and _sample()
    )
    monkeypatch.setattr(
        cli, "write_by_stage_chart", lambda *a, **k: calls.setdefault("wrote", True)
    )

    assert cli.main(["by-stage", "--split", "dev", "--image-dir", str(tmp_path)]) == 0
    assert calls["kw"] == {"split": "dev", "image_dir": tmp_path}
    assert "wrote" not in calls

    calls.clear()
    assert cli.main(["by-stage", "--image-dir", str(tmp_path)]) == 0
    assert calls["kw"] == {"split": "test", "image_dir": tmp_path}
    assert "wrote" not in calls


def test_cli_by_stage_summary_survives_a_logging_reset(monkeypatch, capsys):
    """A live dev run loads PaddleOCR-VL, which can reset Python logging; the
    summary line must still reach the console, so it is printed, not logged."""
    import logging

    import evals.__main__ as cli

    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level

    def compute_and_reset_logging(**_kw):
        root.handlers.clear()  # what a library resetting logging leaves behind
        logging.disable(logging.CRITICAL)
        return _sample()

    monkeypatch.setattr(cli, "compute_by_stage", compute_and_reset_logging)
    monkeypatch.setattr(cli, "write_by_stage_chart", lambda *a, **k: None)
    try:
        assert cli.main(["by-stage", "--split", "dev"]) == 0
    finally:
        logging.disable(logging.NOTSET)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
    out = capsys.readouterr()
    assert "[dev split] by-stage F1: 0.865 → 0.807 → 0.904" in out.out + out.err
