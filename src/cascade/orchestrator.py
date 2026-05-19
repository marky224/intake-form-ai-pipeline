"""V1 local cascade orchestrator.

Chains the three frozen single-shot providers — Tier 1 PaddleOCR-VL → Tier 2
Qwen 2.5 VL 7B → Tier 3 Qwen 2.5 VL 32B — into one in-process pipeline:

1. **Tier 1 + routing.** PaddleOCR-VL runs once. Its layout output is
   form-agnostic, so the same pass feeds both the router (OCR text → Stage 1
   vocabulary classify, Stage 2 Qwen-7B fallback) and Tier 1 field
   extraction. The routed vertical picks the Pydantic ``form_cls``; Tier 1's
   raw response is re-parsed into that class (pure, no second OCR run).
2. **Field-level escalation.** Fields whose confidence is below the
   Tier 1→2 gate (0.85) escalate to Tier 2; of those, fields still below the
   Tier 2→3 gate (0.80) escalate to Tier 3. Escalation **narrows the
   prompt**: only the sub-threshold fields are sent up, via a synthetic
   ``narrow_form_cls`` sub-model — the frozen ``extract(png, form_cls)``
   Protocol is unchanged and providers never learn they got a subset (see
   ``cascade.narrow``).
3. **Exhaustion → review queue.** A field still sub-threshold after Tier 3,
   or never produced by any tier, exhausts the V1 cascade (no cloud Sonnet
   above Tier 3 — that's V2). The document's partial extraction is kept and
   the run is parked in the SQLite ``review_queue`` with the full per-tier
   error history.

**Escalation predicate.** The locked rule is per-field confidence < gate
(0.85 / 0.80). "Confidence" is read through the schema's own
attempted/blank/unattempted trichotomy (``ExtractedField`` docstring;
``compute_form_confidence``): a field escalates when it was **never
attempted** (``tier_used is None``) or was **extracted with a value** below
the gate (``value is not None and confidence < gate``). A
*confidently-blank* field (``value is None`` with ``tier_used`` set) is a
tier's confident "absent on this form" judgment — not a weak extraction — so
it does **not** escalate and does not by itself trigger review. This is the
schema's documented semantics, not a new threshold; Phase 6's eval sweep
tunes the gate values, not this predicate.

**Retry-then-escalate** (locked, all-local). A timeout / connection error /
Ollama HTTP 5xx retries 3x with exponential backoff (1/2/4 s + jitter) then
escalates. A per-request wall-clock cap (180 s; Tier 3 Q4_K_M measures
~52 s/doc) is advisory in V1 — a blocking sync Ollama call can't be hard
-killed in-process, so an over-cap call is treated as a timeout-class
failure on return (a true hard interrupt is V2 Step Functions work). The
locked "schema-validation failure → retry once with a *stricter prompt*"
rule degrades in V1: the frozen Protocol exposes no prompt knob and the
providers already internalize ``ValidationError`` (demoting bad values to
confidently-blank), so a provider almost never raises for schema reasons;
if one does, it gets one plain retry then escalates. Documented degradation,
not a silent gap. No 429/Retry-After path — no cloud rate limits in V1.

**HIPAA_MODE** is read and logged as an explicit V1 no-op: V1 has no
cloud/provider routing surface for the flag to assert against (all-local,
synthetic data only). The hook exists so V2 can activate the BAA-eligibility
assertion + audit-log verbosity bump without an orchestrator reshape.
"""

from __future__ import annotations

import logging
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from cascade import router, store
from cascade.narrow import merge_fields, narrow_form_cls
from cascade.providers import _qwen_vl
from cascade.providers import tier1_paddleocr_local as tier1_mod
from cascade.providers._base import CascadeProvider, ProviderResult, T
from cascade.providers.tier1_paddleocr_local import Tier1PaddleOcrLocal
from cascade.providers.tier2_qwen_7b_local import Tier2Qwen7bLocal
from cascade.providers.tier3_qwen_32b_local import Tier3Qwen32bLocal
from cascade.router import RouteDecision, route
from intake_schemas import (
    ExtractedField,
    HealthcareIntakeForm,
    compute_form_confidence,
)

logger = logging.getLogger(__name__)

#: Locked per-field escalation gates (architecture-locked.md "Router (V1)" /
#: README). Tuned in Phase 6 eval sweeps — these are the starting values.
GATE_TIER1_TO_TIER2 = 0.85
GATE_TIER2_TO_TIER3 = 0.80

#: Retry-then-escalate (locked). 3 retries, 1/2/4 s base backoff + jitter.
MAX_RETRIES = 3
_BACKOFF_BASE_S = (1.0, 2.0, 4.0)

#: Advisory per-request wall-clock cap. Tier 3 Q4_K_M ≈ 52 s/doc; 180 s
#: absorbs cold model load + headroom before an over-cap call is treated as
#: a timeout-class failure on return.
WALL_CLOCK_CAP_S = 180.0

#: Pipeline version stamped onto the assembled form's metadata.
PIPELINE_VERSION = "v1-cascade@phase5"

#: Exceptions that count as retryable transport/timeout failures. Ollama's
#: ``ResponseError`` (lazy import — never needed on the cached-replay path)
#: is additionally retried when its ``status_code`` is a 5xx.
_RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    OSError,
)


class TierExhausted(Exception):
    """Raised internally when a tier fails past its retry budget.

    Caught by the orchestrator: the tier contributed no improvement, its
    failure is appended to the run's error history, and the cascade
    continues to the next tier (or to the review queue after Tier 3).
    """


@dataclass(frozen=True)
class RunRecord:
    """The orchestrator's return value + the row written to ``runs``.

    ``form`` is the assembled full-form instance (every tier's contributions
    merged) so the eval harness (Phase 6) can score it without re-running
    the cascade. ``escalations`` maps tier label → the field names sent to
    that tier. ``error_history`` is the per-tier failure trail (empty on a
    clean run; persisted to ``review_queue`` when ``status`` is
    ``review_queue``).
    """

    doc_id: str
    vertical: str
    router_stage: int
    router_score: float
    final_tier: str
    final_confidence: float
    status: str
    total_latency_ms: float
    form: BaseModel
    escalations: dict[str, list[str]] = field(default_factory=dict)
    error_history: list[dict[str, Any]] = field(default_factory=list)


def build_cascade() -> tuple[CascadeProvider, CascadeProvider, CascadeProvider]:
    """Construct the three providers once (Tier 1, Tier 2, Tier 3).

    Matches the locked "orchestrator constructs each provider instance once"
    shape. A batch caller builds the tuple once and passes it to every
    ``process_document`` call so model handles stay resident across the
    batch (the ``keep_alive="1h"`` pin does the rest).
    """
    return Tier1PaddleOcrLocal(), Tier2Qwen7bLocal(), Tier3Qwen32bLocal()


def _is_retryable(exc: BaseException) -> bool:
    """True for transport/timeout failures and Ollama HTTP 5xx."""
    if isinstance(exc, _RETRYABLE_EXC):
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and 500 <= status < 600


def _run_tier_with_retry(
    provider: CascadeProvider,
    png: bytes,
    form_cls: type[T],
) -> ProviderResult[T]:
    """Call ``provider.extract`` with the locked retry-then-escalate policy.

    Retries a retryable failure (transport/timeout/5xx, or an over-cap
    wall-clock on return) up to ``MAX_RETRIES`` with 1/2/4 s + jitter
    backoff. A non-retryable exception gets exactly one plain retry (the V1
    degradation of the locked "retry once stricter" — see module docstring)
    then escalates. Exhausting the budget raises ``TierExhausted``; the
    orchestrator turns that into "this tier added nothing, continue".
    """
    non_retryable_used = False
    for attempt in range(MAX_RETRIES + 1):
        t0 = time.perf_counter()
        try:
            result = provider.extract(png, form_cls)
        except Exception as exc:  # classified by _is_retryable / escalated
            retryable = _is_retryable(exc)
            if not retryable:
                if non_retryable_used:
                    raise TierExhausted(
                        f"{provider.name}: non-retryable {type(exc).__name__}"
                    ) from exc
                non_retryable_used = True
            if attempt >= MAX_RETRIES:
                raise TierExhausted(
                    f"{provider.name}: exhausted after {attempt + 1} attempts "
                    f"({type(exc).__name__})"
                ) from exc
            backoff = _BACKOFF_BASE_S[min(attempt, len(_BACKOFF_BASE_S) - 1)]
            time.sleep(backoff + random.uniform(0.0, backoff * 0.25))
            continue

        elapsed = time.perf_counter() - t0
        if elapsed > WALL_CLOCK_CAP_S:
            # Over the advisory cap — treat as a timeout-class failure (we
            # can't hard-kill the sync call; the value it eventually
            # returned isn't trusted past the cap). A cached-replay hit
            # returns in well under the cap, so this never trips in CI.
            if attempt >= MAX_RETRIES:
                raise TierExhausted(
                    f"{provider.name}: exceeded {WALL_CLOCK_CAP_S}s wall-clock "
                    f"cap on every attempt"
                )
            backoff = _BACKOFF_BASE_S[min(attempt, len(_BACKOFF_BASE_S) - 1)]
            time.sleep(backoff + random.uniform(0.0, backoff * 0.25))
            continue
        return result

    # Unreachable: every loop branch returns or raises.
    raise TierExhausted(f"{provider.name}: retry loop fell through")


def _should_escalate(ef: ExtractedField[Any], gate: float) -> bool:
    """Locked per-field escalation predicate (see module docstring).

    Escalate when the field was never attempted (``tier_used is None``) or
    was extracted with a value below ``gate``. A confidently-blank field
    (value None, tier set) is a confident absence judgment — it does not
    escalate.
    """
    if ef.tier_used is None:
        return True
    if ef.value is None:
        return False  # confidently blank
    return ef.confidence < gate


def _escalating_fields(
    form: BaseModel,
    candidates: list[str],
    gate: float,
) -> list[str]:
    """Subset of ``candidates`` whose field on ``form`` clears the predicate."""
    return [name for name in candidates if _should_escalate(getattr(form, name), gate)]


def _attempt_rows(
    form: BaseModel,
    names: list[str],
    tier: Any,
    latency_ms: float,
    *,
    escalated: set[str],
) -> list[dict[str, Any]]:
    """Build ``field_attempts`` rows for the fields ``tier`` just handled."""
    rows: list[dict[str, Any]] = []
    for name in names:
        ef = getattr(form, name)
        rows.append(
            {
                "field_name": name,
                "tier": tier,
                "value": None if ef.value is None else str(ef.value),
                "confidence": ef.confidence,
                "escalation_reason": "low_confidence" if name in escalated else None,
                "latency_ms": latency_ms,
            }
        )
    return rows


def _hipaa_mode_noop() -> None:
    """Read + log ``HIPAA_MODE`` as the explicit V1 no-op (see docstring)."""
    if os.environ.get("HIPAA_MODE", "").strip().lower() in {"1", "true", "yes", "on"}:
        logger.info(
            "HIPAA_MODE set — V1 no-op: all-local synthetic-data cascade has "
            "no provider/cloud routing surface to assert BAA-eligibility "
            "against. Flag activates in V2."
        )


def process_document(
    png: bytes,
    *,
    doc_id: str,
    conn: Any | None = None,
    db_path: Path | str = store.DEFAULT_DB_PATH,
    providers: tuple[CascadeProvider, CascadeProvider, CascadeProvider] | None = None,
) -> RunRecord:
    """Run one document through the full V1 cascade and persist the result.

    ``conn`` lets a batch caller / test pass an open SQLite connection
    (e.g. an in-memory DB); otherwise one is opened at ``db_path``,
    initialized, and closed. ``providers`` lets a batch caller reuse one
    constructed cascade across documents; otherwise it's built per call.
    """
    _hipaa_mode_noop()
    tier1, tier2, tier3 = providers if providers is not None else build_cascade()

    owns_conn = conn is None
    if owns_conn:
        conn = store.connect(db_path)
        store.init_db(conn)

    error_history: list[dict[str, Any]] = []
    escalations: dict[str, list[str]] = {}
    attempt_rows: list[dict[str, Any]] = []
    t_start = time.perf_counter()

    try:
        # --- Tier 1 + routing ------------------------------------------------
        # PaddleOCR-VL output is form-agnostic; one pass feeds both the router
        # and Tier 1 extraction. A provisional form_cls only satisfies the
        # frozen signature — the raw response is re-parsed into the routed
        # class below (pure, no second OCR call).
        try:
            t1_result = _run_tier_with_retry(tier1, png, HealthcareIntakeForm)
            t1_latency = t1_result.latency_ms
            ocr_lines = router.ocr_lines_from_tier1_raw(t1_result.raw_response)
        except TierExhausted as exc:
            # Tier 1 is the OCR source; without it there is nothing to route
            # or extract. Park immediately with the failure recorded.
            error_history.append({"tier": "1", "error": str(exc)})
            decision = RouteDecision("healthcare", 1, 0.0, HealthcareIntakeForm)
            form = decision.form_cls(metadata=tier1_mod._stub_metadata(decision.form_cls))
            return _finalize(
                conn=conn,
                owns_conn=owns_conn,
                doc_id=doc_id,
                decision=decision,
                form=form,
                final_tier="1",
                status=store.RUN_STATUS_REVIEW,
                t_start=t_start,
                attempt_rows=attempt_rows,
                escalations=escalations,
                error_history=error_history,
            )

        decision = route(ocr_lines, png)
        form_cls = decision.form_cls
        form = tier1_mod._parse_response(t1_result.raw_response, form_cls)
        _stamp_metadata(form, doc_id, decision)

        extractable = list(_qwen_vl._extractable_fields(form_cls).keys())

        # --- Tier 1 → Tier 2 escalation -------------------------------------
        esc1 = _escalating_fields(form, extractable, GATE_TIER1_TO_TIER2)
        attempt_rows += _attempt_rows(form, extractable, 1, t1_latency, escalated=set(esc1))
        final_tier = "1"

        if esc1:
            escalations["2"] = esc1
            sub2 = narrow_form_cls(form_cls, esc1)
            try:
                t2_result = _run_tier_with_retry(tier2, png, sub2)
                merge_fields(form, t2_result.form, esc1)
                attempt_rows += _attempt_rows(
                    form,
                    esc1,
                    2,
                    t2_result.latency_ms,
                    escalated=set(_escalating_fields(form, esc1, GATE_TIER2_TO_TIER3)),
                )
                final_tier = "2"
            except TierExhausted as exc:
                error_history.append({"tier": "2", "error": str(exc), "fields": esc1})

            # --- Tier 2 → Tier 3 escalation ---------------------------------
            esc2 = _escalating_fields(form, esc1, GATE_TIER2_TO_TIER3)
            if esc2:
                escalations["3a"] = esc2
                sub3 = narrow_form_cls(form_cls, esc2)
                try:
                    t3_result = _run_tier_with_retry(tier3, png, sub3)
                    merge_fields(form, t3_result.form, esc2)
                    attempt_rows += _attempt_rows(
                        form, esc2, "3a", t3_result.latency_ms, escalated=set()
                    )
                    final_tier = "3a"
                except TierExhausted as exc:
                    error_history.append({"tier": "3a", "error": str(exc), "fields": esc2})

        # --- Exhaustion check ----------------------------------------------
        # Fields still sub-threshold after the last tier that ran (or never
        # produced at all) exhaust the V1 cascade → review queue, partial
        # extraction kept.
        still_low = _escalating_fields(form, extractable, GATE_TIER2_TO_TIER3)
        if still_low:
            error_history.append({"tier": "exhausted", "unresolved_fields": still_low})
            status = store.RUN_STATUS_REVIEW
        else:
            status = store.RUN_STATUS_EXTRACTED

        return _finalize(
            conn=conn,
            owns_conn=owns_conn,
            doc_id=doc_id,
            decision=decision,
            form=form,
            final_tier=final_tier,
            status=status,
            t_start=t_start,
            attempt_rows=attempt_rows,
            escalations=escalations,
            error_history=error_history,
        )
    except Exception:
        if owns_conn:
            conn.close()
        raise


def _stamp_metadata(form: BaseModel, doc_id: str, decision: RouteDecision) -> None:
    """Replace the provider stub metadata with real run provenance."""
    form.metadata.source_document_id = doc_id
    form.metadata.pipeline_version = PIPELINE_VERSION
    form.metadata.routing_decision = f"stage{decision.stage}:{decision.vertical}"


def _finalize(
    *,
    conn: Any,
    owns_conn: bool,
    doc_id: str,
    decision: RouteDecision,
    form: BaseModel,
    final_tier: str,
    status: str,
    t_start: float,
    attempt_rows: list[dict[str, Any]],
    escalations: dict[str, list[str]],
    error_history: list[dict[str, Any]],
) -> RunRecord:
    """Compute aggregates, persist all rows, and build the RunRecord."""
    conf = compute_form_confidence(form)
    final_confidence = float(conf["mean"])
    form.metadata.overall_confidence = final_confidence
    total_latency_ms = (time.perf_counter() - t_start) * 1000.0

    try:
        store.record_run(
            conn,
            doc_id=doc_id,
            vertical=decision.vertical,
            router_stage=decision.stage,
            router_score=decision.score,
            final_tier=final_tier,
            final_confidence=final_confidence,
            status=status,
            total_latency_ms=total_latency_ms,
        )
        store.record_field_attempts(conn, doc_id, attempt_rows)
        if status == store.RUN_STATUS_REVIEW:
            store.enqueue_review(conn, doc_id, error_history)
    finally:
        if owns_conn:
            conn.close()

    return RunRecord(
        doc_id=doc_id,
        vertical=decision.vertical,
        router_stage=decision.stage,
        router_score=decision.score,
        final_tier=final_tier,
        final_confidence=final_confidence,
        status=status,
        total_latency_ms=total_latency_ms,
        form=form,
        escalations=escalations,
        error_history=error_history,
    )
