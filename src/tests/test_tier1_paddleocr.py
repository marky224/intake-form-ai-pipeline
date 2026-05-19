"""Tests for ``cascade.providers.tier1_paddleocr_local``.

Five layers:

1. **Provider shape** — Protocol conformance + metadata constants.
2. **Alias-table machinery** — ``_alias_map_for_form`` + ``_strip_label_prefix``
   + ``_match_block``. These power the post-OCR layout-to-fields step.
3. **Bbox + page-size parsers** — ``_parse_bbox`` (pixel vs normalized) +
   ``_parse_page_size``. Pure helpers.
4. **Response parser** — ``_parse_response`` consumes PaddleOCR-VL's
   ``parsing_res_list`` shape and produces a populated form.
5. **End-to-end ``extract()``** — cached-replay path + live-path stub +
   ``EVAL_LIVE`` bypass + sha256 cache-key correctness.
6. **Validation set** — the 92-doc CMS-1500 `test`-split cached fixtures
   must parse cleanly; field population is asserted in aggregate (≥90%),
   the honest broad-scale form of the old per-doc acceptance gate.
"""

from __future__ import annotations

import hashlib
import os
import pathlib

import pytest

from cascade import eval_cache
from cascade.providers import tier1_paddleocr_local
from cascade.providers._base import CascadeProvider, ProviderResult
from cascade.providers.tier1_paddleocr_local import (
    ALIAS_MATCH_THRESHOLD,
    PADDLEOCR_VL_VERSION,
    PROVIDER_NAME,
    TIER,
    Tier1PaddleOcrLocal,
    _alias_map_for_form,
    _iter_table_label_value_pairs,
    _match_block,
    _match_label_only,
    _parse_bbox,
    _parse_html_table,
    _parse_page_size,
    _parse_response,
    _strip_label_prefix,
)
from intake_schemas import (
    BusinessDocumentForm,
    DataClass,
    HealthcareIntakeForm,
    get_field_metadata,
)

# Tiny PNG-shaped byte payload — sha256 is stable across runs so tests can pin
# against it. Real provider runs use renderer output, not this stub.
_FAKE_PNG = b"PNG_PAYLOAD_for_tests"
_FAKE_PNG_SHA256 = hashlib.sha256(_FAKE_PNG).hexdigest()


@pytest.fixture
def isolated_cache_root(tmp_path, monkeypatch):
    """Swap CACHE_ROOT to a tmp dir so tests don't write to checked-in fixtures."""
    monkeypatch.setattr(eval_cache, "CACHE_ROOT", tmp_path)
    return tmp_path


@pytest.fixture
def eval_live_off(monkeypatch):
    """Force EVAL_LIVE off — exercises the cache-first path."""
    monkeypatch.delenv("EVAL_LIVE", raising=False)


@pytest.fixture
def eval_live_on(monkeypatch):
    """Force EVAL_LIVE on — bypasses cache, hits the live path."""
    monkeypatch.setenv("EVAL_LIVE", "true")


def _hc_raw(blocks: list[dict]) -> dict:
    """Build a raw_response with parsing_res_list + a representative page_size."""
    return {"page_size_px": [1700, 2200], "parsing_res_list": blocks}


# ---------------------------------------------------------------------------
# Provider metadata + Protocol conformance
# ---------------------------------------------------------------------------


def test_provider_metadata_constants():
    """name + tier are stable; PaddleOCR-VL version pin is recorded."""
    assert PROVIDER_NAME == "tier1_paddleocr_local"
    assert TIER == 1
    assert PADDLEOCR_VL_VERSION == "PaddleOCR-VL-1.5"
    assert 0.0 < ALIAS_MATCH_THRESHOLD < 1.0


def test_provider_satisfies_cascade_provider_protocol():
    """Tier1PaddleOcrLocal conforms to the locked Protocol shape."""
    assert isinstance(Tier1PaddleOcrLocal(), CascadeProvider)


def test_provider_attributes_match_protocol():
    p = Tier1PaddleOcrLocal()
    assert p.name == PROVIDER_NAME
    assert p.tier == TIER


# ---------------------------------------------------------------------------
# _alias_map_for_form
# ---------------------------------------------------------------------------


def test_alias_map_healthcare_includes_base_and_healthcare_fields():
    """HealthcareIntakeForm pulls aliases from base + healthcare seed records."""
    alias_map = _alias_map_for_form(HealthcareIntakeForm)
    assert "first_name" in alias_map
    # Base alias is present (canonical phrasing from generic intake forms)
    assert "First Name" in alias_map["first_name"]
    # Healthcare-specific alias is also present
    assert "Patient's First Name" in alias_map["first_name"]


def test_alias_map_dedupes_across_verticals():
    """Same alias text in multiple seed records appears once in the merged list."""
    alias_map = _alias_map_for_form(HealthcareIntakeForm)
    # "ZIP" appears in BOTH base and healthcare records for address_zip
    zips = alias_map["address_zip"]
    assert zips.count("ZIP") == 1


def test_alias_map_business_uses_synthetic_aliases_for_docile_fields():
    """BusinessDocumentForm has no seed records → synthesize from canonical_name."""
    alias_map = _alias_map_for_form(BusinessDocumentForm)
    # vendor_name isn't in alias_table_seed.json — synthesized as title-case
    assert "vendor_name" in alias_map
    assert "Vendor Name" in alias_map["vendor_name"]


def test_alias_map_only_includes_form_canonical_fields():
    """Seed records for fields not on the form (e.g. HR's citizenship_status) aren't included."""
    alias_map = _alias_map_for_form(HealthcareIntakeForm)
    assert "citizenship_status" not in alias_map


# ---------------------------------------------------------------------------
# _strip_label_prefix
# ---------------------------------------------------------------------------


def test_strip_label_prefix_with_colon_separator():
    assert _strip_label_prefix("First Name: Jane Doe", "First Name") == "Jane Doe"


def test_strip_label_prefix_with_dash_separator():
    assert _strip_label_prefix("DOB - 1980-05-01", "DOB") == "1980-05-01"


def test_strip_label_prefix_case_insensitive():
    assert _strip_label_prefix("FIRST NAME: Jane", "first name") == "Jane"


def test_strip_label_prefix_returns_none_when_alias_absent():
    assert _strip_label_prefix("nothing useful here", "First Name") is None


def test_strip_label_prefix_returns_none_when_remainder_empty():
    """Block contains label only with no value — return None so caller can skip."""
    assert _strip_label_prefix("First Name:", "First Name") is None
    assert _strip_label_prefix("First Name", "First Name") is None


def test_strip_label_prefix_handles_whitespace_after_label():
    assert _strip_label_prefix("ZIP   02038", "ZIP") == "02038"


# ---------------------------------------------------------------------------
# _match_block
# ---------------------------------------------------------------------------


def test_match_block_returns_best_alias_match():
    alias_map = {"first_name": ["First Name", "Given Name"]}
    match = _match_block("First Name: Jane", alias_map)
    assert match is not None
    canonical, value, score = match
    assert canonical == "first_name"
    assert value == "Jane"
    assert score > 0.5


def test_match_block_prefers_longer_alias():
    """Two aliases match; the longer (higher-specificity) one wins."""
    alias_map = {"address_street": ["Street Address", "Address"]}
    match = _match_block("Street Address: 123 Main St", alias_map)
    assert match is not None
    canonical, value, score = match
    assert canonical == "address_street"
    assert value == "123 Main St"


def test_match_block_below_threshold_returns_none():
    """A short alias incidentally appearing in a long block is rejected."""
    alias_map = {"address_state": ["ST"]}
    # "ST" appears in "Patient was an Astronaut" but len(ST)/len(text) is tiny.
    match = _match_block("Patient was an Astronaut", alias_map)
    assert match is None


def test_match_block_empty_text_returns_none():
    assert _match_block("", {"x": ["X"]}) is None
    assert _match_block("   ", {"x": ["X"]}) is None


def test_match_block_label_only_block_yields_no_value():
    """Block contains only the label — match exists but value is None."""
    alias_map = {"first_name": ["First Name"]}
    match = _match_block("First Name", alias_map)
    # Score is exactly 1.0; passes threshold; but value is None.
    assert match is not None
    canonical, value, score = match
    assert canonical == "first_name"
    assert value is None
    assert score == 1.0


def test_match_block_no_aliases_match_returns_none():
    alias_map = {"first_name": ["First Name"]}
    assert _match_block("absolutely unrelated text", alias_map) is None


# ---------------------------------------------------------------------------
# _match_label_only (used for table-cell label matching)
# ---------------------------------------------------------------------------


def test_match_label_only_returns_canonical_and_score():
    alias_map = {"first_name": ["First Name"], "last_name": ["Last Name"]}
    match = _match_label_only("First Name", alias_map)
    assert match is not None
    assert match[0] == "first_name"
    assert match[1] == pytest.approx(1.0)


def test_match_label_only_below_threshold_returns_none():
    """Long label cell with tiny alias substring scores below threshold."""
    alias_map = {"sex": ["Sex"]}  # alias "Sex" is 3 chars
    # text 30 chars → 3/30 = 0.10 < 0.30
    assert _match_label_only("Astronaut Travel History Sex", alias_map) is None


def test_match_label_only_empty_text_returns_none():
    assert _match_label_only("", {"x": ["X"]}) is None
    assert _match_label_only("   ", {"x": ["X"]}) is None


def test_match_label_only_picks_higher_scoring_alias():
    """When two aliases match, the longer one wins on score."""
    alias_map = {"first_name": ["First"], "last_name": ["First Name"]}
    match = _match_label_only("First Name", alias_map)
    assert match is not None
    # "First Name" alias (10 chars) outscores "First" (5 chars) at text-len 10.
    assert match[0] == "last_name"


# ---------------------------------------------------------------------------
# _parse_html_table + _iter_table_label_value_pairs (table-aware extractor)
# ---------------------------------------------------------------------------


def test_parse_html_table_basic_two_row():
    html = "<table><tr><td>A</td><td>B</td></tr><tr><td>1</td><td>2</td></tr></table>"
    assert _parse_html_table(html) == [["A", "B"], ["1", "2"]]


def test_parse_html_table_unescapes_html_entities():
    """PaddleOCR-VL emits ``&#x27;`` for ``'`` — entity escapes must be undone."""
    html = "<table><tr><td>INSURED&#x27;S ID</td></tr></table>"
    rows = _parse_html_table(html)
    assert rows == [["INSURED'S ID"]]


def test_parse_html_table_empty_cells_preserved():
    """Empty <td></td> cells stay in the row so column indices align."""
    html = "<table><tr><td>A</td><td></td><td>C</td></tr></table>"
    assert _parse_html_table(html) == [["A", "", "C"]]


def test_parse_html_table_malformed_input_returns_empty():
    """Non-table garbage doesn't crash; empty rows are returned."""
    assert _parse_html_table("not html") == []


def test_iter_table_pairs_column_aligned_across_two_rows():
    html = (
        "<table>"
        "<tr><td>First Name</td><td>Last Name</td></tr>"
        "<tr><td>Jane</td><td>Doe</td></tr>"
        "</table>"
    )
    pairs = list(_iter_table_label_value_pairs(html))
    assert ("First Name", "Jane") in pairs
    assert ("Last Name", "Doe") in pairs


def test_iter_table_pairs_skips_empty_value_cells():
    html = (
        "<table>"
        "<tr><td>First Name</td><td>Last Name</td></tr>"
        "<tr><td>Jane</td><td></td></tr>"
        "</table>"
    )
    pairs = list(_iter_table_label_value_pairs(html))
    assert pairs == [("First Name", "Jane")]


def test_iter_table_pairs_handles_uneven_row_widths():
    """Short row caps the column iteration — no IndexError."""
    html = (
        "<table>"
        "<tr><td>A</td><td>B</td><td>C</td></tr>"
        "<tr><td>1</td><td>2</td></tr>"
        "</table>"
    )
    pairs = list(_iter_table_label_value_pairs(html))
    assert pairs == [("A", "1"), ("B", "2")]


def test_parse_response_extracts_fields_from_table_block():
    """A ``block_label='table'`` block expands into (label, value) cell pairs."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.0, 0.0, 1.0, 1.0],
                "block_label": "table",
                "block_content": (
                    "<table>"
                    "<tr><td>First Name</td><td>Last Name</td></tr>"
                    "<tr><td>Jane</td><td>Doe</td></tr>"
                    "</table>"
                ),
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.value == "Jane"
    assert form.first_name.tier_used == 1
    assert form.last_name.value == "Doe"


def test_parse_response_drops_table_pairs_that_fail_pydantic_validation():
    """A column-shift mis-pair (e.g. ``date_of_birth='F'``) is dropped, not crashes."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.0, 0.0, 1.0, 1.0],
                "block_label": "table",
                "block_content": (
                    "<table>"
                    "<tr><td>First Name</td><td>Date of Birth</td></tr>"
                    "<tr><td>Jane</td><td>F</td></tr>"  # 'F' is not a valid date
                    "</table>"
                ),
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    # first_name still populates; date_of_birth is dropped (would have crashed
    # form construction). tier_used=None means "not attempted".
    assert form.first_name.value == "Jane"
    assert form.date_of_birth.tier_used is None


# ---------------------------------------------------------------------------
# _parse_bbox + _parse_page_size
# ---------------------------------------------------------------------------


def test_parse_bbox_normalized_coords_pass_through():
    bb = _parse_bbox([0.1, 0.2, 0.3, 0.4])
    assert bb is not None
    assert bb.x1 == 0.1
    assert bb.x2 == 0.3
    assert bb.page_number == 1


def test_parse_bbox_pixel_coords_normalized_via_page_size():
    """Pixel bbox + page_size_px → normalized [0, 1] bbox."""
    bb = _parse_bbox([170, 440, 510, 660], page_size_px=(1700, 2200))
    assert bb is not None
    assert abs(bb.x1 - 0.1) < 1e-9
    assert abs(bb.x2 - 0.3) < 1e-9
    assert abs(bb.y1 - 0.2) < 1e-9


def test_parse_bbox_pixel_coords_without_page_size_returns_none():
    """Pixel bbox with no page_size_px → can't normalize → None."""
    assert _parse_bbox([100, 200, 300, 400]) is None


def test_parse_bbox_wrong_length_returns_none():
    assert _parse_bbox([0.1, 0.2, 0.3]) is None
    assert _parse_bbox([0.1, 0.2, 0.3, 0.4, 0.5]) is None


def test_parse_bbox_non_numeric_returns_none():
    assert _parse_bbox(["a", "b", "c", "d"]) is None


def test_parse_bbox_clamps_slightly_out_of_bounds_pixel_coords():
    """Pixel bbox extending past page edge → clamped, not rejected."""
    bb = _parse_bbox([0, 0, 1701, 2200], page_size_px=(1700, 2200))
    assert bb is not None
    assert bb.x2 == 1.0  # clamped from 1.000588


def test_parse_page_size_happy_path():
    assert _parse_page_size([1700, 2200]) == (1700, 2200)
    assert _parse_page_size((800, 1100)) == (800, 1100)


def test_parse_page_size_rejects_zero_and_negative():
    assert _parse_page_size([0, 100]) is None
    assert _parse_page_size([-1, 100]) is None


def test_parse_page_size_rejects_malformed():
    assert _parse_page_size(None) is None
    assert _parse_page_size([100]) is None
    assert _parse_page_size(["a", "b"]) is None


# ---------------------------------------------------------------------------
# _parse_response
# ---------------------------------------------------------------------------


def test_parse_response_populates_field_from_labeled_block():
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.20, 0.07, 0.56, 0.09],
                "block_label": "text",
                "block_content": "First Name: Jane",
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.value == "Jane"
    assert form.first_name.tier_used == 1


def test_parse_response_stamps_tier_used_only_on_populated_fields():
    """Fields with no matching block stay unattempted (tier_used=None)."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name: Jane",
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.tier_used == 1
    assert form.last_name.tier_used is None


def test_parse_response_keeps_highest_scoring_match_when_multiple_blocks_match_same_field():
    """Two blocks both contain 'First Name' — the higher-scoring one wins."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name: Alice",
            },
            # Same alias appears in a longer block (lower score). 'Alice' should
            # win because alias-len/text-len is higher.
            {
                "block_bbox": [0.0, 0.5, 0.9, 0.55],
                "block_label": "text",
                "block_content": "Please confirm patient's First Name: Bob below",
            },
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.value == "Alice"


def test_parse_response_attaches_bounding_box():
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.20, 0.07, 0.56, 0.09],
                "block_label": "text",
                "block_content": "First Name: Jane",
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.bounding_box is not None
    assert form.first_name.bounding_box.page_number == 1
    assert form.first_name.bounding_box.x1 == 0.20


def test_parse_response_attaches_raw_text_from_full_block_content():
    """raw_text is the block_content verbatim (label + value), not just value."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.20, 0.07, 0.56, 0.09],
                "block_label": "text",
                "block_content": "First Name: Jane",
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.raw_text == "First Name: Jane"


def test_parse_response_skips_label_only_blocks():
    """Block with the alias but no value text doesn't populate the field."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name",
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.tier_used is None


def test_parse_response_skips_unmatched_blocks():
    """Block text matching nothing in the alias map → no field populated."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "the quick brown fox",
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    # No fields populated; form valid; metadata stub present
    assert form.metadata.form_type == "HealthcareIntakeForm"
    populated = [
        name
        for name in get_field_metadata(HealthcareIntakeForm)
        if getattr(form, name).tier_used == 1
    ]
    assert populated == []


def test_parse_response_handles_empty_parsing_res_list():
    raw = _hc_raw([])
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.metadata.form_type == "HealthcareIntakeForm"
    assert form.first_name.tier_used is None


def test_parse_response_handles_missing_parsing_res_list_key():
    """Malformed response — be lenient, return empty extraction."""
    form = _parse_response({"page_size_px": [1700, 2200]}, HealthcareIntakeForm)
    assert form.first_name.tier_used is None


def test_parse_response_skips_non_dict_blocks():
    raw = _hc_raw(["not a dict", None])  # type: ignore[list-item]
    raw["parsing_res_list"].append(
        {  # type: ignore[union-attr]
            "block_bbox": [0.0, 0.0, 0.5, 0.05],
            "block_label": "text",
            "block_content": "First Name: Jane",
        }
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.value == "Jane"


def test_parse_response_skips_blocks_with_missing_or_empty_content():
    raw = _hc_raw(
        [
            {"block_bbox": [0.0, 0.0, 0.5, 0.05], "block_label": "text"},  # no content
            {"block_bbox": [0.0, 0.0, 0.5, 0.05], "block_label": "text", "block_content": ""},
            {"block_bbox": [0.0, 0.0, 0.5, 0.05], "block_label": "text", "block_content": "  "},
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name: Jane",
            },
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.value == "Jane"


def test_parse_response_normalizes_pixel_bbox_when_page_size_given():
    raw = {
        "page_size_px": [1700, 2200],
        "parsing_res_list": [
            {
                "block_bbox": [340, 154, 952, 198],  # pixel coords
                "block_label": "text",
                "block_content": "First Name: Jane",
            }
        ],
    }
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.bounding_box is not None
    assert abs(form.first_name.bounding_box.x1 - 0.2) < 1e-9


def test_parse_response_works_for_healthcare_form_phi_field():
    """HealthcareIntakeForm elevates first_name to PHI."""
    raw = _hc_raw(
        [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name: Jane",
            }
        ]
    )
    form = _parse_response(raw, HealthcareIntakeForm)
    assert form.first_name.value == "Jane"
    assert get_field_metadata(HealthcareIntakeForm)["first_name"].data_class == DataClass.PHI


def test_parse_response_works_for_business_form_with_synthetic_aliases():
    """BusinessDocumentForm uses synthetic title-case aliases from canonical_name."""
    raw = {
        "page_size_px": [1700, 2200],
        "parsing_res_list": [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "Vendor Name: ACME Industrial Supply",
            }
        ],
    }
    form = _parse_response(raw, BusinessDocumentForm)
    assert form.vendor_name.value == "ACME Industrial Supply"
    assert form.vendor_name.tier_used == 1


# ---------------------------------------------------------------------------
# extract() — cache-first replay path
# ---------------------------------------------------------------------------


def _cached_first_name_jane() -> dict:
    return {
        "page_size_px": [1700, 2200],
        "parsing_res_list": [
            {
                "block_bbox": [0.20, 0.07, 0.56, 0.09],
                "block_label": "text",
                "block_content": "First Name: Jane",
            }
        ],
    }


def test_extract_returns_cached_response_when_cache_hit(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache hit with EVAL_LIVE unset → no live call, latency_ms=0.0."""
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, _cached_first_name_jane())

    def _should_not_be_called() -> None:
        raise AssertionError("live path called despite cache hit")

    monkeypatch.setattr(tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", _should_not_be_called)

    provider = Tier1PaddleOcrLocal()
    result = provider.extract(_FAKE_PNG, HealthcareIntakeForm)

    assert isinstance(result, ProviderResult)
    assert result.form.first_name.value == "Jane"
    assert result.form.first_name.tier_used == 1
    assert result.latency_ms == 0.0
    assert result.cost_usd == 0.0
    assert "parsing_res_list" in result.raw_response


def test_extract_falls_through_to_live_on_cache_miss(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Cache miss (no EVAL_LIVE) → still hits live path per starter-prompt spec."""
    stub_response = _cached_first_name_jane()
    monkeypatch.setattr(
        tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub-pipeline"
    )
    monkeypatch.setattr(
        tier1_paddleocr_local, "_invoke_pipeline", lambda pipeline, png: stub_response
    )

    provider = Tier1PaddleOcrLocal()
    result = provider.extract(_FAKE_PNG, HealthcareIntakeForm)

    assert result.form.first_name.value == "Jane"
    assert result.latency_ms >= 0.0
    assert result.raw_response == stub_response


def test_extract_writes_back_to_cache_after_live_call(
    isolated_cache_root, eval_live_off, monkeypatch
):
    """Live call success → response persisted under the PNG's sha256."""
    stub = _cached_first_name_jane()
    monkeypatch.setattr(
        tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub-pipeline"
    )
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", lambda *a, **kw: stub)
    Tier1PaddleOcrLocal().extract(_FAKE_PNG, HealthcareIntakeForm)

    # Second call should hit cache and skip live.
    monkeypatch.setattr(
        tier1_paddleocr_local,
        "_invoke_pipeline",
        lambda *a, **kw: pytest.fail("second call should be cache hit"),
    )
    result2 = Tier1PaddleOcrLocal().extract(_FAKE_PNG, HealthcareIntakeForm)
    assert result2.form.first_name.value == "Jane"
    assert result2.latency_ms == 0.0


def test_extract_bypasses_cache_when_eval_live_set(isolated_cache_root, eval_live_on, monkeypatch):
    """EVAL_LIVE=true → always call live, overwriting any cached response."""
    stale = {
        "page_size_px": [1700, 2200],
        "parsing_res_list": [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name: Stale",
            }
        ],
    }
    fresh = {
        "page_size_px": [1700, 2200],
        "parsing_res_list": [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name: Fresh",
            }
        ],
    }
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, stale)
    monkeypatch.setattr(
        tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub-pipeline"
    )
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", lambda *a, **k: fresh)

    result = Tier1PaddleOcrLocal().extract(_FAKE_PNG, HealthcareIntakeForm)
    assert result.form.first_name.value == "Fresh"
    assert eval_cache.load_cached(PROVIDER_NAME, _FAKE_PNG_SHA256) == fresh


def test_extract_recomputes_sha256_from_png_bytes(isolated_cache_root, eval_live_off, monkeypatch):
    """Provider keys the cache on sha256(png) not on any caller-supplied hash."""
    correct = _cached_first_name_jane()
    eval_cache.save_cached(PROVIDER_NAME, _FAKE_PNG_SHA256, correct)
    other_png = b"DIFFERENT_PNG_BYTES"
    monkeypatch.setattr(tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", lambda: "stub")
    live_response = {
        "page_size_px": [1700, 2200],
        "parsing_res_list": [
            {
                "block_bbox": [0.0, 0.0, 0.5, 0.05],
                "block_label": "text",
                "block_content": "First Name: FromLive",
            }
        ],
    }
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", lambda *a, **k: live_response)
    result = Tier1PaddleOcrLocal().extract(other_png, HealthcareIntakeForm)
    assert result.form.first_name.value == "FromLive"


# ---------------------------------------------------------------------------
# Live path tests (gated)
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_live_inference_smoke(isolated_cache_root, eval_live_on):
    """Skipped unless paddle is installed AND EVAL_LIVE=true.

    Real live-inference validation runs via `just regen-fixtures` against
    the 92-doc CMS-1500 test split. This smoke test just confirms the live
    path doesn't blow up on import when paddle IS available. CI never sets
    EVAL_LIVE so this stays skipped.
    """
    try:
        import paddle  # noqa: F401
    except ImportError:
        pytest.skip("paddle not installed; see docs/local-development.md Tier 1 setup")

    provider = Tier1PaddleOcrLocal()
    assert provider.name == PROVIDER_NAME


def test_load_paddleocr_vl_pipeline_raises_helpful_error_when_paddle_missing(monkeypatch):
    """Without paddle installed the live path raises ImportError with an install hint."""
    if "paddle" in os.environ.get("_PYTHON_INSTALLED_PACKAGES", ""):
        pytest.skip("paddle is installed; can't exercise the missing-import path here")
    import sys

    monkeypatch.setitem(sys.modules, "paddle", None)
    with pytest.raises(ImportError, match="paddlepaddle-gpu"):
        tier1_paddleocr_local._load_paddleocr_vl_pipeline()


# ---------------------------------------------------------------------------
# Validation set: end-to-end cached replay against the 92-doc CMS-1500 test split
# ---------------------------------------------------------------------------

# Note on corpus composition: the starter prompt called for 10 docs (5
# CMS-1500 + 5 DocILE pages). Locked 2026-05-13 to CMS-1500-only — DocILE
# pages cannot be redistributed in this MIT public repo because DocILE is
# CC-BY-NC-ND 4.0. DocILE-side validation is a local-only workflow on
# Mark's GPU machine. See tests/fixtures/eval-cache/README.md.

VALIDATION_DIR = pathlib.Path(__file__).parent / "fixtures" / "eval-validation" / "cms1500"


def _validation_pngs() -> list[pathlib.Path]:
    return sorted(VALIDATION_DIR.glob("*.png"))


def test_validation_corpus_present():
    """The checked-in CMS-1500 validation set is the broad test split.

    92 = the deterministic ``test`` partition of the locked-seed 584-doc
    corpus (``evals.manifest.assign_split``); the canonical
    validation-dir↔manifest invariant lives in
    ``test_evals_manifest.py::test_validation_dir_is_exactly_the_test_split``.
    """
    pngs = _validation_pngs()
    assert len(pngs) == 92, f"Expected 92 validation PNGs, found {len(pngs)}"


@pytest.mark.parametrize("png_path", _validation_pngs(), ids=lambda p: p.name)
def test_tier1_cached_replay_on_validation_doc(png_path, monkeypatch):
    """Each validation doc has a cached Tier 1 response that parses cleanly.

    Exercises the full pipeline: real PNG bytes → sha256 → cache lookup →
    _parse_response → HealthcareIntakeForm, with no exception and a true
    cache hit. The acceptance gate per the PR (a+b) starter: 'Tier 1 must
    return SOMETHING (parseable response, no exceptions) on all N docs.'

    Per-doc *field population* is asserted in aggregate, not here: at the
    broad 92-doc scale PaddleOCR-VL's layout parser legitimately yields
    zero alias-matched fields on a small minority of layouts (the cascade
    then escalates — the locked Phase 6 two-stage finding). Forcing every
    doc to populate would mean cherry-picking the corpus.
    """
    monkeypatch.delenv("EVAL_LIVE", raising=False)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError(
            f"Cache miss on {png_path.name}: cached fixture missing or PNG bytes drifted."
        )

    monkeypatch.setattr(tier1_paddleocr_local, "_load_paddleocr_vl_pipeline", _should_not_be_called)
    monkeypatch.setattr(tier1_paddleocr_local, "_invoke_pipeline", _should_not_be_called)

    provider = Tier1PaddleOcrLocal()
    result = provider.extract(png_path.read_bytes(), HealthcareIntakeForm)

    assert isinstance(result, ProviderResult)
    assert result.latency_ms == 0.0  # cache hit
    assert result.cost_usd == 0.0
    assert isinstance(result.form, HealthcareIntakeForm)  # parsed, no exception


def _tier1_populated_count(png_path: pathlib.Path) -> int:
    sha = hashlib.sha256(png_path.read_bytes()).hexdigest()
    raw = eval_cache.load_cached("tier1_paddleocr_local", sha)
    assert raw is not None, f"missing Tier 1 fixture for {png_path.name}"
    form = tier1_paddleocr_local._parse_response(raw, HealthcareIntakeForm)
    return sum(
        1 for name in get_field_metadata(HealthcareIntakeForm) if getattr(form, name).tier_used == 1
    )


def test_tier1_populates_vast_majority_of_validation_docs():
    """Aggregate honesty guard (replaces the old per-doc ``assert
    populated``): Tier 1 alias-matches ≥1 field on the large majority of
    the broad test split. Measured 91/92 (98.9%) on the locked-seed
    corpus; the ≥0.90 floor leaves margin without engineering the corpus
    or hiding the handful of zero-field layouts the cascade escalates."""
    pngs = _validation_pngs()
    populated = sum(1 for p in pngs if _tier1_populated_count(p) > 0)
    frac = populated / len(pngs)
    assert frac >= 0.90, f"Tier-1 populated only {populated}/{len(pngs)} ({frac:.3f})"
