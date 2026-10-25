"""Tests for ``cascade.orchestrator`` — the V1 cascade chain.

End-to-end on the 92 committed CMS-1500 docs (the patient-stratified
`test` split) via cached replay (no live model, $0, deterministic), plus
focused unit coverage of the escalation
predicate, retry-then-escalate policy, HIPAA_MODE no-op, and the Tier 1
exhaustion path.

Tier 3 second read (since 2026-10-16): Tier 3 re-reads every field Tier 2
handled and agreement sets the final confidence — matching readings are
accepted, any mismatch parks for review, and a Tier-2 value that passes its
validator is never overwritten (the old type heuristic sent every date to
Tier 3, which overwrote correct dates). The second-read tests below layer a
stub Tier 3 over the cached Tier 2 reading so each rule is exercised on a
real document.
"""

from __future__ import annotations

import logging
import pathlib
import re

import pytest

from cascade import orchestrator, store
from cascade.orchestrator import (
    TierExhausted,
    _is_retryable,
    _run_tier_with_retry,
    _should_escalate,
)
from cascade.providers import _qwen_vl
from cascade.providers import tier1_paddleocr_local as t1
from cascade.providers import tier2_qwen_7b_local as t2
from cascade.providers import tier3_qwen_32b_local as t3
from cascade.providers._base import ProviderResult
from intake_schemas import ExtractedField, HealthcareIntakeForm, TierAttempt

VALIDATION_DIR = pathlib.Path(__file__).parent / "fixtures" / "eval-validation" / "cms1500"


def _validation_pngs() -> list[pathlib.Path]:
    return sorted(VALIDATION_DIR.glob("*.png"))


@pytest.fixture
def memconn():
    c = store.connect(":memory:")
    store.init_db(c)
    yield c
    c.close()


@pytest.fixture(autouse=True)
def _no_live_calls(monkeypatch):
    """Cached-replay only: any live model seam firing is a test failure."""
    monkeypatch.delenv("EVAL_LIVE", raising=False)

    def _boom(*a, **k):
        raise AssertionError("live model seam fired during a cached-replay test")

    monkeypatch.setattr(t1, "_load_paddleocr_vl_pipeline", _boom)
    monkeypatch.setattr(t1, "_invoke_pipeline", _boom)
    monkeypatch.setattr(t2, "_load_ollama_client", _boom)
    monkeypatch.setattr(t2, "_invoke_model", _boom)
    monkeypatch.setattr(t3, "_load_ollama_client", _boom)
    monkeypatch.setattr(t3, "_invoke_model", _boom)


# --- End-to-end ------------------------------------------------------------


@pytest.mark.parametrize("png_path", _validation_pngs(), ids=lambda p: p.name[:18])
def test_e2e_cms1500_cached(png_path, memconn):
    providers = orchestrator.build_cascade()
    rec = orchestrator.process_document(
        png_path.read_bytes(),
        doc_id=png_path.stem,
        conn=memconn,
        providers=providers,
    )
    # Per-doc invariants that hold across the full 92-doc test split.
    # (Stage / escalation *shape* varies per doc at broad scale — the old
    # per-doc `router_stage == 1` held only for the 6 hand-picked docs;
    # asserting it per-doc now would mean cherry-picking the corpus. Tier 3
    # reads exactly Tier 2's field set — asserted on every doc by
    # test_tier3_second_reads_every_tier2_field below.)
    assert isinstance(rec.form, HealthcareIntakeForm)
    assert rec.vertical == "healthcare"
    assert rec.router_stage in {1, 2}
    if rec.router_stage == 1:
        assert rec.router_score >= orchestrator.router.STAGE1_THRESHOLD_N
    assert rec.final_tier in {"1", "2", "3a"}
    assert 0.0 <= rec.final_confidence <= 1.0
    esc2 = set(rec.escalations.get("2", []))
    esc3 = set(rec.escalations.get("3a", []))
    assert esc3 <= esc2  # Tier 3 prompt is never wider than Tier 2's

    # The run persisted, classified healthcare, and attempted fields (holds
    # for every doc — even a zero-Tier-1 layout still escalates and tries).
    run_row = memconn.execute(
        "SELECT vertical, status FROM runs WHERE doc_id=?", (png_path.stem,)
    ).fetchone()
    assert run_row[0] == "healthcare"
    assert (
        memconn.execute(
            "SELECT COUNT(*) FROM field_attempts WHERE doc_id=?",
            (png_path.stem,),
        ).fetchone()[0]
        > 0
    )


def test_tier3_second_reads_every_tier2_field(memconn):
    """Tier 3 is a second reader: on every doc it gets exactly the field set
    Tier 2 handled (no narrowing between Tier 2 and Tier 3)."""
    providers = orchestrator.build_cascade()
    for p in _validation_pngs():
        rec = orchestrator.process_document(
            p.read_bytes(), doc_id=p.stem, conn=memconn, providers=providers
        )
        esc2 = rec.escalations.get("2", [])
        assert rec.escalations.get("3a", []) == esc2


class _Tier3Override:
    """Stub Tier 3: the cached Tier 2 reading, stamped ``tier_used="3a"``,
    with chosen fields replaced.

    ``overrides`` maps field name → value (``None`` = Tier 3 reads the field
    as blank). Fields not overridden agree with Tier 2 by construction.
    """

    name = "tier3_override"
    tier = "3a"

    def __init__(self, overrides: dict):
        self.overrides = overrides

    def extract(self, png, form_cls):
        form = t2.Tier2Qwen7bLocal().extract(png, form_cls).form
        for name in type(form).model_fields:
            ef = getattr(form, name)
            if isinstance(ef, ExtractedField) and ef.tier_used is not None:
                setattr(form, name, ef.model_copy(update={"tier_used": "3a"}))
        for name, value in self.overrides.items():
            confidence = 0.0 if value is None else 1.0
            setattr(form, name, ExtractedField(value=value, confidence=confidence, tier_used="3a"))
        return ProviderResult(form=form, latency_ms=1.0, cost_usd=0.0, raw_response={})


#: A test doc whose Tier 1 reads no date of birth, so the DOB reaches Tier 2
#: and Tier 3 (since 2026-10-24 a validated Tier-1 date is final at Tier 1).
_NO_T1_DOB_DOC = 15


def _second_read(memconn, overrides: dict):
    """Run one cached doc with (Tier 1, Tier 2, stub Tier 3) and also return
    the Tier-1+2 baseline form (Tier 3 slot = Tier 2) for comparison."""
    png = _validation_pngs()[_NO_T1_DOB_DOC]
    t1p, t2p, _ = orchestrator.build_cascade()
    base = orchestrator.process_document(
        png.read_bytes(), doc_id="base", conn=memconn, providers=(t1p, t2p, t2p)
    )
    rec = orchestrator.process_document(
        png.read_bytes(),
        doc_id=png.stem,
        conn=memconn,
        providers=(t1p, t2p, _Tier3Override(overrides)),
    )
    return base.form, rec


def _parked(rec) -> set[str]:
    return {n for e in rec.error_history for n in e.get("unresolved_fields", [])}


def test_tier3_never_overwrites_validated_date(memconn):
    """Tier 2's date passes its validator, Tier 3 reads a different date:
    Tier 2's value stays and the field parks for review."""
    probe, _ = _second_read(memconn, {})
    t2_dob = probe.date_of_birth.value
    assert t2_dob is not None
    other = t2_dob.replace(year=t2_dob.year - 1)
    base, rec = _second_read(memconn, {"date_of_birth": other})
    dob = rec.form.date_of_birth
    assert dob.value == base.date_of_birth.value == t2_dob
    assert dob.tier_used == 2
    assert "date_of_birth" in _parked(rec)
    assert rec.status == store.RUN_STATUS_REVIEW
    assert dob.confidence == orchestrator.DISAGREEMENT_CONFIDENCE


def test_agreement_accepts_without_review(memconn):
    """Tier 3 reads every field exactly as Tier 2 did: every populated
    second-read field is accepted at the agreement confidence and the doc
    clears the cascade (dates included — no type-based parking)."""
    _, rec = _second_read(memconn, {})
    assert not _parked(rec)
    assert rec.status == store.RUN_STATUS_EXTRACTED
    read_twice = rec.escalations["3a"]
    populated = [n for n in read_twice if getattr(rec.form, n).value is not None]
    assert "date_of_birth" in populated
    agreed = orchestrator.AGREEMENT_CONFIDENCE
    assert all(getattr(rec.form, n).confidence == agreed for n in populated)


def test_disagreement_on_unvalidated_field_takes_tier3_and_parks(memconn):
    """A free-text field has no validator: on a mismatch Tier 3's reading
    wins (the higher tier) and the field still parks for review."""
    base, rec = _second_read(memconn, {"last_name": "Zzyzx"})
    assert base.last_name.value != "Zzyzx"
    last = rec.form.last_name
    assert (last.value, last.tier_used) == ("Zzyzx", "3a")
    assert "last_name" in _parked(rec)
    assert last.confidence == orchestrator.DISAGREEMENT_CONFIDENCE


def test_tier3_blank_keeps_tier2_value_and_parks(memconn):
    """Tier 3 reads nothing where Tier 2 read a value: the value is kept (a
    blank can't be reviewed against) and the field parks for review."""
    base, rec = _second_read(memconn, {"last_name": None})
    assert base.last_name.value is not None
    last = rec.form.last_name
    assert (last.value, last.tier_used) == (base.last_name.value, 2)
    assert "last_name" in _parked(rec)
    assert last.confidence == orchestrator.DISAGREEMENT_CONFIDENCE


def test_both_blank_never_attempted_takes_tier3_blank_stamp():
    """Tier 2 failed, so a field was never attempted (``tier_used`` None);
    Tier 3 reads it as blank. The field takes Tier 3's blank stamp —
    confidently blank, so it does not park — instead of staying unattempted
    and parking for nothing."""
    form = HealthcareIntakeForm(metadata=t1._stub_metadata(HealthcareIntakeForm))
    second = HealthcareIntakeForm(metadata=t1._stub_metadata(HealthcareIntakeForm))
    second.email = ExtractedField(value=None, confidence=0.0, tier_used="3a")
    assert form.email.tier_used is None
    orchestrator._reconcile_second_read(form, second, ["email"])
    assert (form.email.value, form.email.tier_used) == (None, "3a")
    assert not _should_escalate(form.email, orchestrator.GATE_TIER2_TO_TIER3)


def test_parked_disagreement_keeps_tier3_reading_in_its_3a_row(memconn):
    """The final form keeps Tier 2's validated date, but the ``field_attempts``
    rows record what each tier actually read — Tier 3's own date is in its
    ``3a`` row, and Tier 2's row says it went on to a second read."""
    probe, _ = _second_read(memconn, {})
    t2_dob = probe.date_of_birth.value
    other = t2_dob.replace(year=t2_dob.year - 1)
    _, rec = _second_read(memconn, {"date_of_birth": other})
    assert rec.form.date_of_birth.value == t2_dob

    def row(tier: str):
        return memconn.execute(
            "SELECT value, escalation_reason FROM field_attempts "
            "WHERE doc_id=? AND field_name='date_of_birth' AND tier=?",
            (rec.doc_id, tier),
        ).fetchone()

    assert row("3a") == (str(other), None)
    assert row("2") == (str(t2_dob), "second_read")


def test_second_read_reason_is_a_valid_tier_attempt_reason():
    """The orchestrator's escalation reasons stay inside the schema's
    ``TierAttempt.reason_escalated`` vocabulary."""
    for reason in ("low_confidence", "second_read"):
        assert (
            TierAttempt(tier=2, confidence=0.5, reason_escalated=reason).reason_escalated == reason
        )


class _DeadTier:
    """A provider whose every call fails with a retryable transport error."""

    def __init__(self, name: str, tier):
        self.name = name
        self.tier = tier

    def extract(self, png, form_cls):
        raise ConnectionError(f"{self.name} down")


def test_tier3_down_keeps_tier2_provisional_confidences(memconn, monkeypatch):
    """Tier 3 fails past its retry budget: Tier 2's provisional type-heuristic
    confidences survive — the date parks, the string passes — and the error
    history names every field Tier 3 was asked to re-read."""
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)
    png = _validation_pngs()[_NO_T1_DOB_DOC]
    t1p, t2p, _ = orchestrator.build_cascade()
    rec = orchestrator.process_document(
        png.read_bytes(),
        doc_id=png.stem,
        conn=memconn,
        providers=(t1p, t2p, _DeadTier("tier3_qwen_32b_local", "3a")),
    )
    assert rec.escalations["3a"] == rec.escalations["2"]
    t3_error = next(e for e in rec.error_history if e.get("tier") == "3a")
    assert t3_error["fields"] == rec.escalations["2"]
    assert rec.final_tier == "2"
    parked = _parked(rec)
    assert "date_of_birth" in parked
    assert rec.form.date_of_birth.confidence == _qwen_vl.FORMAT_COERCED_CONFIDENCE
    assert "last_name" not in parked
    assert rec.form.last_name.confidence == _qwen_vl.CLEAN_VALUE_CONFIDENCE
    assert rec.status == store.RUN_STATUS_REVIEW


def test_tier2_down_reconciles_tier1_against_tier3(memconn, monkeypatch):
    """Tier 2 fails: Tier 3's second read is reconciled against Tier 1's
    values. A Tier-1 ZIP that passes its validator is final at Tier 1, so
    Tier 3 never reads it; one that fails it (a column-shifted ZIP) is
    replaced by Tier 3's."""
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)
    t1p, _, _ = orchestrator.build_cascade()
    dead2 = _DeadTier("tier2_qwen_7b_local", 2)

    # Doc 1: Tier 1 reads a valid ZIP (01749); the stub Tier 3 would read
    # another one, but never gets the field.
    png = _validation_pngs()[1]
    rec = orchestrator.process_document(
        png.read_bytes(),
        doc_id=png.stem,
        conn=memconn,
        providers=(t1p, dead2, _Tier3Override({"address_zip": "99999"})),
    )
    assert any(e.get("tier") == "2" for e in rec.error_history)
    assert (rec.form.address_zip.value, rec.form.address_zip.tier_used) == ("01749", 1)
    assert "address_zip" not in rec.escalations["3a"]
    assert "address_zip" not in _parked(rec)

    # Doc 0: Tier 1's ZIP is column-shifted ("555-451-8687") and fails the
    # validator, so Tier 3's reading is taken.
    png0 = _validation_pngs()[0]
    rec0 = orchestrator.process_document(
        png0.read_bytes(),
        doc_id=png0.stem,
        conn=memconn,
        providers=(t1p, dead2, _Tier3Override({})),
    )
    zip0 = rec0.form.address_zip
    assert zip0.tier_used == "3a"
    assert re.fullmatch(r"\d{5}", zip0.value)


def test_owns_conn_path_creates_db(tmp_path):
    """No conn passed → orchestrator opens/inits/closes its own DB file."""
    db = tmp_path / "v1.db"
    png = _validation_pngs()[0]
    rec = orchestrator.process_document(png.read_bytes(), doc_id="x", db_path=db)
    assert db.exists()
    c = store.connect(db)
    assert c.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    c.close()
    assert rec.doc_id == "x"


# --- Escalation predicate --------------------------------------------------


def test_should_escalate_predicate():
    gate = 0.85
    unattempted = ExtractedField()  # tier_used=None
    blank = ExtractedField(value=None, confidence=0.0, tier_used=2)
    weak = ExtractedField(value="x", confidence=0.5, tier_used=1)
    strong = ExtractedField(value="x", confidence=0.99, tier_used=1)
    assert _should_escalate(unattempted, gate) is True
    assert _should_escalate(blank, gate) is False  # confident absence
    assert _should_escalate(weak, gate) is True
    assert _should_escalate(strong, gate) is False


# --- Retry-then-escalate ---------------------------------------------------


def test_is_retryable_classification():
    assert _is_retryable(TimeoutError())
    assert _is_retryable(ConnectionError())
    assert _is_retryable(OSError())
    err = type("E", (Exception,), {})()
    err.status_code = 503
    assert _is_retryable(err)
    err.status_code = 404
    assert not _is_retryable(err)
    assert not _is_retryable(ValueError())


class _FlakyProvider:
    name = "flaky"
    tier = 2

    def __init__(self, fail_n: int, exc: BaseException):
        self.calls = 0
        self.fail_n = fail_n
        self.exc = exc

    def extract(self, png, form_cls):
        self.calls += 1
        if self.calls <= self.fail_n:
            raise self.exc
        return ProviderResult(
            form=form_cls(metadata=t1._stub_metadata(form_cls)),
            latency_ms=1.0,
            cost_usd=0.0,
            raw_response={},
        )


def test_retry_recovers_within_budget(monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)
    p = _FlakyProvider(fail_n=2, exc=ConnectionError())
    result = _run_tier_with_retry(p, b"png", HealthcareIntakeForm)
    assert isinstance(result, ProviderResult)
    assert p.calls == 3  # 2 retried failures + 1 success


def test_retry_exhausts_then_raises(monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)
    p = _FlakyProvider(fail_n=99, exc=TimeoutError())
    with pytest.raises(TierExhausted, match="exhausted"):
        _run_tier_with_retry(p, b"png", HealthcareIntakeForm)
    assert p.calls == orchestrator.MAX_RETRIES + 1


def test_slow_cold_tier3_call_fits_the_wall_clock_cap(monkeypatch):
    """A 200 s call (the 161 s cold full-field Tier 3 read in the replay
    fixtures, plus margin) returns on the first attempt instead of being
    treated as a timeout."""
    import itertools

    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)
    clock = itertools.cycle([0.0, 200.0])  # each attempt "takes" 200 s
    monkeypatch.setattr(orchestrator.time, "perf_counter", lambda: next(clock))
    p = _FlakyProvider(fail_n=0, exc=ConnectionError())
    _run_tier_with_retry(p, b"png", HealthcareIntakeForm)
    assert p.calls == 1


def test_non_retryable_gets_one_retry_then_escalates(monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)
    p = _FlakyProvider(fail_n=99, exc=ValueError("schema-ish"))
    with pytest.raises(TierExhausted):
        _run_tier_with_retry(p, b"png", HealthcareIntakeForm)
    assert p.calls == 2  # one plain retry then escalate (V1 degradation)


# --- HIPAA_MODE no-op ------------------------------------------------------


def test_hipaa_mode_is_logged_noop(monkeypatch, caplog):
    monkeypatch.setenv("HIPAA_MODE", "true")
    with caplog.at_level(logging.INFO, logger="cascade.orchestrator"):
        orchestrator._hipaa_mode_noop()
    assert any("V1 no-op" in r.message for r in caplog.records)


def test_hipaa_mode_unset_is_silent(monkeypatch, caplog):
    monkeypatch.delenv("HIPAA_MODE", raising=False)
    with caplog.at_level(logging.INFO, logger="cascade.orchestrator"):
        orchestrator._hipaa_mode_noop()
    assert not caplog.records


# --- Tier 1 exhaustion -----------------------------------------------------


def test_tier1_exhaustion_parks_for_review(memconn, monkeypatch):
    monkeypatch.setattr(orchestrator.time, "sleep", lambda *_: None)

    class _DeadTier1:
        name = "tier1_paddleocr_local"
        tier = 1

        def extract(self, png, form_cls):
            raise ConnectionError("ollama/paddle down")

    bad = (_DeadTier1(), object(), object())
    rec = orchestrator.process_document(b"png", doc_id="dead", conn=memconn, providers=bad)
    assert rec.status == store.RUN_STATUS_REVIEW
    assert rec.final_tier == "1"
    assert isinstance(rec.form, HealthcareIntakeForm)
    assert any(e.get("tier") == "1" for e in rec.error_history)
    assert (
        memconn.execute("SELECT COUNT(*) FROM review_queue WHERE doc_id='dead'").fetchone()[0] == 1
    )
