"""Tests for ``cascade.orchestrator`` — the V1 cascade chain.

End-to-end on the 92 committed CMS-1500 docs (the patient-stratified
`test` split) via cached replay (no live model, $0, deterministic), plus
focused unit coverage of the escalation
predicate, retry-then-escalate policy, HIPAA_MODE no-op, and the Tier 1
exhaustion path.

Documented emergent property (asserted, not worked around): the locked
Tier-2/3 confidence heuristic stamps *coerced* scalars (date / int / float
/ bool) at 0.5, and the locked Tier 2→3 gate is 0.80. A document with any
coerced field therefore necessarily exhausts the cascade and lands in the
review queue in V1 — a genuine consumer-hardware trade-off (local Qwen
can't self-confidence-score coerced values; V2's cloud tiers change this).
Phase 6's eval sweep tunes the gate; the orchestrator implements both
locked rules faithfully.
"""

from __future__ import annotations

import logging
import pathlib

import pytest

from cascade import orchestrator, store
from cascade.orchestrator import (
    TierExhausted,
    _is_retryable,
    _run_tier_with_retry,
    _should_escalate,
)
from cascade.providers import tier1_paddleocr_local as t1
from cascade.providers import tier2_qwen_7b_local as t2
from cascade.providers import tier3_qwen_32b_local as t3
from cascade.providers._base import ProviderResult
from intake_schemas import ExtractedField, HealthcareIntakeForm

VALIDATION_DIR = pathlib.Path("tests/fixtures/eval-validation/cms1500")


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
    # per-doc `router_stage == 1` / strict `esc3 < esc2` held only for the
    # 6 hand-picked docs; asserting them per-doc now would mean
    # cherry-picking the corpus. The strict-narrowing claim is proven in
    # aggregate by test_narrowing_is_provable_on_the_corpus below.)
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


def test_narrowing_is_provable_on_the_corpus(memconn):
    """Aggregate replacement for the old per-doc strict-subset assertion:
    across the broad test split, at least one doc demonstrably narrows
    (Tier 3 got a *strict* subset of Tier 2's escalation set). That single
    existence proof is the honest form of "narrowing actually narrows" —
    it does not require every doc to escalate identically."""
    providers = orchestrator.build_cascade()
    narrowed = 0
    for p in _validation_pngs():
        rec = orchestrator.process_document(
            p.read_bytes(), doc_id=p.stem, conn=memconn, providers=providers
        )
        esc2 = set(rec.escalations.get("2", []))
        esc3 = set(rec.escalations.get("3a", []))
        if esc2 and esc3 < esc2:
            narrowed += 1
    assert narrowed >= 1, "no doc demonstrated Tier 2→3 narrowing"


def test_e2e_coerced_fields_force_review_queue(memconn):
    """Documented emergent property: a coerced field (0.5) below the 0.80
    Tier 2→3 gate exhausts the cascade → review_queue with an 'exhausted'
    error-history entry. Every CMS-1500 has date fields, so any doc in the
    split exhibits this; the test exercises one representative doc."""
    png = _validation_pngs()[0]
    rec = orchestrator.process_document(png.read_bytes(), doc_id=png.stem, conn=memconn)
    assert rec.status == store.RUN_STATUS_REVIEW
    assert any(e.get("tier") == "exhausted" for e in rec.error_history)
    assert (
        memconn.execute("SELECT COUNT(*) FROM review_queue WHERE doc_id=?", (png.stem,)).fetchone()[
            0
        ]
        == 1
    )


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
