"""Tests for ``cascade.router`` — the two-stage V1 document router.

Covers the locked behaviors and the schema-version drift guard:

1. **Vocabulary inclusion rule** — distinctive = in ``healthcare`` and not
   in ``base``/``insurance``/``hr``. Hard-asserts the locked example terms
   (drift guard per architecture-locked.md "Schema-version coupling") plus
   the global invariant that no excluded-vertical alias ever leaks.
2. **OCR-line flattening** from a Tier 1 raw_response (table + text blocks).
3. **Stage 1 scoring** — inverse-frequency weight, once-per-alias.
4. **Stage 2 fallback** — Qwen-7B routing, replay-cached, tolerant parse.
5. **N spot-check** — all 6 committed CMS-1500 docs classify ``healthcare``
   at Stage 1 with score ≥ N (the recorded value; see PR body).
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from cascade import eval_cache, router
from intake_schemas import BusinessDocumentForm, HealthcareIntakeForm

VALIDATION_DIR = pathlib.Path("tests/fixtures/eval-validation/cms1500")


def _validation_pngs() -> list[pathlib.Path]:
    return sorted(VALIDATION_DIR.glob("*.png"))


@pytest.fixture(autouse=True)
def _fresh_vocab_cache():
    """The vocabulary is process-cached (locked: built once). Clear it so a
    test that monkeypatches the seed path doesn't poison later tests."""
    router.build_distinctive_vocabulary.cache_clear()
    yield
    router.build_distinctive_vocabulary.cache_clear()


# --- 1. Vocabulary inclusion rule -----------------------------------------

# Locked examples (architecture-locked.md "Router (V1)"). These exact
# verticals were verified against alias_table_seed.json; if a seed
# regeneration moves any of these, this test fails loudly (the intended
# schema-version drift guard).
_KNOWN_DISTINCTIVE = ["MRN", "PATIENT ID", "SUBSCRIBER ID", "CHIEF COMPLAINT"]
_KNOWN_SHARED = ["FIRST NAME", "LAST NAME", "DATE OF BIRTH", "PHONE"]


def test_known_distinctive_terms_present():
    vocab = router.build_distinctive_vocabulary()
    for term in _KNOWN_DISTINCTIVE:
        assert term in vocab, f"{term} should be healthcare-distinctive"


def test_known_shared_terms_excluded():
    vocab = router.build_distinctive_vocabulary()
    for term in _KNOWN_SHARED:
        assert term not in vocab, f"{term} is cross-vertical, must be excluded"


def test_no_excluded_vertical_alias_leaks():
    """Global invariant: every vocab entry is absent from every
    base/insurance/hr record (the inclusion rule, checked exhaustively)."""
    vocab = router.build_distinctive_vocabulary()
    fields = json.loads(router.ALIAS_TABLE_PATH.read_text())["fields"]
    excluded: set[str] = set()
    for rec in fields:
        if rec["vertical"] in router._EXCLUDING_VERTICALS:
            excluded.update(a.strip().upper() for a in rec["aliases"])
    assert vocab.keys().isdisjoint(excluded)


def test_inverse_frequency_weight_range():
    """Weights are 1/freq ∈ (0, 1]; a unique phrasing weighs 1.0."""
    vocab = router.build_distinctive_vocabulary()
    assert all(0.0 < w <= 1.0 for w in vocab.values())
    assert max(vocab.values()) == 1.0


# --- 2. OCR-line flattening -----------------------------------------------


def test_ocr_lines_from_table_and_text_blocks():
    raw = {
        "parsing_res_list": [
            {"block_label": "paragraph_title", "block_content": "HEALTH FORM"},
            {
                "block_label": "table",
                "block_content": "<table><tr><td>2. PATIENT'S NAME</td>"
                "<td>3. MRN</td></tr><tr><td>Doe, Jane</td><td>X1</td></tr></table>",
            },
            {"block_label": "text", "block_content": "line one\nline two"},
            {"block_label": "text", "block_content": "   "},  # skipped
            "not-a-dict",  # skipped
        ]
    }
    lines = router.ocr_lines_from_tier1_raw(raw)
    assert "HEALTH FORM" in lines
    assert "2. PATIENT'S NAME" in lines
    assert "3. MRN" in lines
    assert "line one" in lines and "line two" in lines
    assert "   " not in lines


# --- 3. Stage 1 scoring ----------------------------------------------------


def test_stage1_score_sums_distinct_alias_weights_once():
    vocab = router.build_distinctive_vocabulary()
    w_mrn = vocab["MRN"]
    # Same alias on two lines counts once; two distinct aliases sum.
    score = router.stage1_score(["Patient MRN: X", "MRN again", "Chief Complaint"])
    assert score == pytest.approx(w_mrn + vocab["CHIEF COMPLAINT"])


def test_stage1_score_zero_for_non_healthcare_text():
    assert router.stage1_score(["Invoice Total", "Vendor Name", "PO Number"]) == 0.0


# --- 4. Stage 2 fallback ---------------------------------------------------


def test_route_stage1_classifies_healthcare_without_stage2(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("Stage 2 must not run when Stage 1 clears N")

    monkeypatch.setattr(router, "_stage2_classify", _boom)
    decision = router.route(["MRN", "Chief Complaint", "Allergies"], b"png")
    assert decision.vertical == "healthcare"
    assert decision.stage == 1
    assert decision.form_cls is HealthcareIntakeForm
    assert decision.score >= router.STAGE1_THRESHOLD_N


def test_route_falls_through_to_stage2_business(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_cache, "CACHE_ROOT", tmp_path)
    monkeypatch.delenv("EVAL_LIVE", raising=False)

    def _fake_invoke(client, png):
        return {"message": {"content": '{"vertical": "business"}'}}

    monkeypatch.setattr(router, "_load_ollama_client", lambda: object())
    monkeypatch.setattr(router, "_stage2_invoke", _fake_invoke)

    decision = router.route(["Invoice", "Vendor", "Total Due"], b"pngbytes")
    assert decision.stage == 2
    assert decision.vertical == "business"
    assert decision.form_cls is BusinessDocumentForm
    assert decision.score < router.STAGE1_THRESHOLD_N


def test_stage2_replay_cache_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(eval_cache, "CACHE_ROOT", tmp_path)
    monkeypatch.delenv("EVAL_LIVE", raising=False)
    calls = []

    def _fake_invoke(client, png):
        calls.append(1)
        return {"message": {"content": '{"vertical": "healthcare"}'}}

    monkeypatch.setattr(router, "_load_ollama_client", lambda: object())
    monkeypatch.setattr(router, "_stage2_invoke", _fake_invoke)

    png = b"ambiguous-doc"
    assert router._stage2_classify(png) == "healthcare"  # live → cached
    assert router._stage2_classify(png) == "healthcare"  # replay
    assert len(calls) == 1  # second call served from cache


def test_parse_stage2_defaults_healthcare_on_garbage():
    assert router._parse_stage2({"message": {"content": "garbage"}}) == "healthcare"
    assert router._parse_stage2({"message": {"content": '{"vertical":"business"}'}}) == "business"


# --- 5. N spot-check (recorded) -------------------------------------------


@pytest.mark.parametrize("png_path", _validation_pngs(), ids=lambda p: p.name[:18])
def test_n_spotcheck_cms1500_classifies_healthcare(png_path):
    """Every committed CMS-1500 classifies healthcare at Stage 1 with a
    score comfortably ≥ the locked N=1.0. Recorded value: observed Stage 1
    score is ~5.5 on these 6 docs (≫ 1.0), so N=1.0 holds with wide margin.
    The broader ~50-doc hand-classified spot-check incl. DocILE negatives is
    a build-machine task (DocILE corpus is CC-BY-NC-ND, not committed).
    """
    sha = hashlib.sha256(png_path.read_bytes()).hexdigest()
    raw = eval_cache.load_cached("tier1_paddleocr_local", sha)
    assert raw is not None, f"missing Tier 1 fixture for {png_path.name}"
    score = router.stage1_score(router.ocr_lines_from_tier1_raw(raw))
    assert score >= router.STAGE1_THRESHOLD_N
    assert score >= 1.0


def test_n_is_locked_starting_value():
    assert router.STAGE1_THRESHOLD_N == 1.0
