"""Ground-truth projection + canonicalization (CMS-1500 + DocILE)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from cascade.providers.tier1_paddleocr_local import _stub_metadata
from evals.ground_truth import (
    EXCLUDED_CMS1500_BOXES,
    FIELD_KIND,
    canonicalize,
    extracted_token,
    load_cms1500_ground_truth,
)
from intake_schemas import ExtractedField, HealthcareIntakeForm, SignatureCapture

# Self-anchored to this test file (src/tests/test_evals_ground_truth.py →
# fixtures under src/tests/fixtures/). Was cwd-relative
# ``glob.glob("tests/fixtures/...")`` which silently returned [] after the
# 2026-05-19 src-layout move and collapsed 92 parametrized cases into a
# single "empty parameter set" skip. See memory project_src_layout.
_SIDECARS = sorted(
    str(p)
    for p in (Path(__file__).parent / "fixtures" / "eval-validation" / "cms1500").glob("*.json")
)


def test_canon_str_casefold_and_whitespace_collapse():
    # NFKC folds the embedded NBSP to a space, then it is collapsed.
    assert canonicalize("first_name", "  Jane \u00a0 DOE ") == "jane doe"
    assert canonicalize("first_name", None) is None
    assert canonicalize("first_name", "") is None


def test_canon_date_accepts_date_object_and_mdy_string():
    assert canonicalize("date_of_birth", date(1996, 1, 26)) == "1996-01-26"
    assert canonicalize("date_of_birth", "01/26/1996") == "1996-01-26"
    assert canonicalize("date_of_birth", "not a date") is None


def test_canon_sex_first_token():
    assert canonicalize("sex", "F") == "F"
    assert canonicalize("sex", "female") == "F"
    assert canonicalize("sex", "X") is None


def test_canon_signature_presence():
    assert canonicalize("signature", True) == "present"
    assert canonicalize("signature", SignatureCapture(present=True)) == "present"
    assert canonicalize("signature", SignatureCapture(present=False)) == "absent"
    assert canonicalize("signature", None) is None


def test_box2_name_split_and_box3_dob_sex_split():
    """Box 2 packs "Last, First"; box 3 packs "MM/DD/YYYY <nbsp> Sex".
    Synthetic sidecar — exercises the split/canonicalize logic itself,
    independent of which docs happen to be in the committed corpus (the
    old test pinned a specific now-superseded validation doc)."""
    sidecar = {
        "fields": [
            {"name": "patient_name", "value": "Robel940, Jona712"},
            {"name": "patient_birth_date", "value": "06/24/1958 \xa0 F"},
            {"name": "signature", "value": "Jona712 Robel940"},
        ]
    }
    truth = load_cms1500_ground_truth(sidecar)
    assert truth["last_name"] == "robel940"
    assert truth["first_name"] == "jona712"
    assert truth["date_of_birth"] == "1958-06-24"
    assert truth["sex"] == "F"
    assert truth["signature"] == "present"


@pytest.mark.parametrize("sidecar", _SIDECARS)
def test_every_sidecar_projects_and_excludes_box14(sidecar):
    truth = load_cms1500_ground_truth(sidecar)
    assert truth, "every CMS-1500 sidecar yields some schema truth"
    # date_of_current_illness (box 14) has no schema home — never scored.
    assert "date_of_current_illness" in EXCLUDED_CMS1500_BOXES
    assert "date_of_current_illness" not in truth
    assert set(truth).issubset(set(FIELD_KIND))


def test_extracted_token_unpopulated_is_none_then_matches_truth():
    form = HealthcareIntakeForm(metadata=_stub_metadata(HealthcareIntakeForm))
    assert extracted_token("first_name", form) is None  # unpopulated
    form.first_name = ExtractedField(value="Jane", confidence=0.9, tier_used=1)
    assert extracted_token("first_name", form) == "jane"
    form.date_of_birth = ExtractedField(value=date(1996, 1, 26), confidence=0.9, tier_used=1)
    assert extracted_token("date_of_birth", form) == "1996-01-26"
