"""V1 local cascade orchestrator.

Chains the three frozen single-shot providers — Tier 1 PaddleOCR-VL → Tier 2
Qwen 2.5 VL 7B → Tier 3 Qwen 2.5 VL 32B — into one in-process pipeline:

1. **Tier 1 + routing.** PaddleOCR-VL runs once. Its layout output is
   form-agnostic, so the same pass feeds both the router (OCR text → Stage 1
   vocabulary classify, Stage 2 Qwen-7B fallback) and Tier 1 field
   extraction. The routed vertical picks the Pydantic ``form_cls``; Tier 1's
   raw response is re-parsed into that class (pure, no second OCR run).
2. **Field-level escalation to Tier 2.** Fields whose confidence is below
   the Tier 1→2 gate (0.85) escalate to Tier 2 — every field except a
   date, ZIP or state Tier 1 read and validated. Escalation **narrows the
   prompt**: only the sub-threshold fields are sent up, via a synthetic
   ``narrow_form_cls`` sub-model — the frozen ``extract(png, form_cls)``
   Protocol is unchanged and providers never learn they got a subset (see
   ``cascade.narrow``).
3. **Routing to Tier 3 by Tier-1/Tier-2 disagreement**
   (:func:`_route_to_tier3`, since 2026-10-24). For a field Tier 1 covers
   (``tier1_paddleocr_local.COVERED_FIELDS``): Tier 1 and Tier 2 read the
   same value → accepted at ``AGREEMENT_CONFIDENCE``, no Tier 3; they
   differ, or Tier 1 read nothing → Tier 3. A Tier-1 reading that fails its
   own validator counts as nothing (since 2026-10-25). A field Tier 1
   doesn't cover has only Tier 2's reading and is accepted at
   ``SINGLE_READ_CONFIDENCE`` (above the review gate, below agreement). A
   form class Tier 1 covers no field of (``BusinessDocumentForm``) keeps
   the second read of every Tier-2 field instead. If Tier 2 fails, Tier 3
   reads every field Tier 2 should have.
4. **Tier 3 reads only the routed fields** (one narrowed call), then
   :func:`_reconcile_second_read`: Tier 3 matching Tier 1 → Tier 3's value
   at ``AGREEMENT_CONFIDENCE`` (two independent readers). Otherwise the
   second-read rule: matching Tier 2 → ``AGREEMENT_CONFIDENCE``; a mismatch
   gets ``DISAGREEMENT_CONFIDENCE``, below the 0.80 gate, keeping the
   Tier-2 value if it passes its field validator (``cascade.validators`` —
   so Tier 3 never overwrites a validated date) or Tier 3 read nothing, and
   Tier 3's value otherwise. **Strict parking:** if Tier 1 has a valid
   reading of the field from a specific label and the kept value doesn't
   match it, the field gets ``DISAGREEMENT_CONFIDENCE`` even when the two
   Qwen readings agree. A reading from a generic label ("15. OTHER DATE",
   since 2026-10-26) still routes but never strict-parks: Tier 3 matching
   Tier 2 settles it. If
   Tier 3 fails, every routed field with a value parks. A field whose Qwen
   readings are both blank stays confidently blank, dropping Tier 1's
   reading (kept by design, 2026-10-25).
   Provider confidences (the ``_qwen_vl`` type heuristic) are provisional:
   the orchestrator replaces every one of them.
5. **Review queue.** A field still below 0.80 (a disagreement), or never
   produced by any tier, exhausts the V1 cascade (no cloud Sonnet above
   Tier 3 — that's V2). The document's partial extraction is kept and the
   run is parked in the SQLite ``review_queue`` with the full per-tier
   error history.

Why agreement (changed 2026-10-16): the type heuristic stamped every string
1.0 and every date 0.5, so escalation followed field type — every date went
to Tier 3, which overwrote correct Tier-2 dates. Why Tier-1/Tier-2
disagreement decides what Tier 3 reads (2026-10-24): Tier 1 (OCR + alias
table) and Tier 2 (a VLM) are different readers, and their agreement is a
strong signal on the cached replay: where Tier 1's reading of a covered
field equals Tier 2's, Tier 1's value is right in 458 of 458 dev cells and
480 of 482 test cells. Tier 3 then reads 3 to 4 fields per document instead of
every Tier-2 field (~26).

**Escalation predicate.** The rule is per-field confidence < gate
(0.85 into Tier 2; 0.80 for review after the second read). "Confidence" is
read through the schema's own
attempted/blank/unattempted trichotomy (``ExtractedField`` docstring;
``compute_form_confidence``): a field escalates when it was **never
attempted** (``tier_used is None``) or was **extracted with a value** below
the gate (``value is not None and confidence < gate``). A
*confidently-blank* field (``value is None`` with ``tier_used`` set) is a
tier's confident "absent on this form" judgment — not a weak extraction — so
it does **not** escalate and does not by itself trigger review. This is the
schema's documented semantics, not a new threshold.

**Retry-then-escalate** (locked, all-local). A timeout / connection error /
Ollama HTTP 5xx retries 3x with exponential backoff (1/2/4 s + jitter) then
escalates. A per-request wall-clock cap (240 s; Tier 3's full-field
Q4_K_M read averages ~117 s/doc, 161 s cold) is advisory in V1 — a blocking
sync Ollama call can't be hard-killed in-process, so an over-cap call is
treated as a timeout-class failure on return (a true hard interrupt is V2 Step Functions work). The
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
import unicodedata
from dataclasses import dataclass, field
from datetime import date
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
from cascade.validators import validate
from intake_schemas import (
    ExtractedField,
    HealthcareIntakeForm,
    compute_form_confidence,
)

logger = logging.getLogger(__name__)

#: Per-field gates (the locked router design / README). 0.85
#: decides which Tier-1 fields go to Tier 2. 0.80 is the review gate after
#: the Tier 3 second read (the name predates the second read).
GATE_TIER1_TO_TIER2 = 0.85
GATE_TIER2_TO_TIER3 = 0.80

#: Final confidence when Tier 2 and Tier 3 read a field identically.
AGREEMENT_CONFIDENCE = 0.95

#: Final confidence when the two readings differ (or only one found a
#: value). Below ``GATE_TIER2_TO_TIER3``, so the field parks for review.
DISAGREEMENT_CONFIDENCE = 0.5

#: Final confidence for a field only Tier 2 read (Tier 1 doesn't cover it).
#: Above the 0.80 review gate, below agreement, so one reading stays
#: distinguishable from two that agree.
SINGLE_READ_CONFIDENCE = 0.85

#: Retry-then-escalate (locked). 3 retries, 1/2/4 s base backoff + jitter.
MAX_RETRIES = 3
_BACKOFF_BASE_S = (1.0, 2.0, 4.0)

#: Advisory per-request wall-clock cap. Tier 3's full-field Q4_K_M read
#: averages ~117 s/doc in the committed replay fixtures (107-161 s; the
#: 161 s is the cold first call); 240 s leaves ~79 s of headroom over that
#: before an over-cap call is treated as a timeout-class failure on return.
#: (It was 180 s while Tier 3 read only the low-confidence fields.) The cap
#: can only fail a call under ``EVAL_LIVE=true``: otherwise a provider saves
#: its live reading to the replay cache before returning, so the retry after
#: an over-cap call replays that reading from the cache.
WALL_CLOCK_CAP_S = 240.0

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


def _normalized(value: Any) -> str:
    """Comparison form of a field value: a date in ISO, anything else NFKC,
    whitespace-collapsed and casefolded ("Ramona980 " == "ramona980")."""
    if isinstance(value, date):
        return value.isoformat()
    return " ".join(unicodedata.normalize("NFKC", str(value)).split()).casefold()


def same_reading(a: Any, b: Any) -> bool:
    """True when two non-empty readings say the same thing (see ``_normalized``)."""
    return a is not None and b is not None and _normalized(a) == _normalized(b)


def _route_to_tier3(
    form: BaseModel,
    names: list[str],
    tier1: dict[str, Any],
    covered: frozenset[str],
) -> list[str]:
    """Decide which of Tier 2's fields Tier 3 reads; settle the rest in place.

    ``tier1`` holds Tier 1's readings (field → value) and ``covered`` the
    fields Tier 1 covers. Per field in ``names`` (Tier 2's value is on
    ``form``):

    - not covered → only Tier 2 read it: a value gets
      ``SINGLE_READ_CONFIDENCE``, a blank stays confidently blank;
    - covered, and Tier 1's reading matches Tier 2's → ``AGREEMENT_CONFIDENCE``;
    - covered otherwise (they differ, or Tier 1 read nothing) → Tier 3.

    Returns the fields Tier 3 should read, in ``names`` order.
    """
    routed: list[str] = []
    for name in names:
        ef = getattr(form, name)
        if name not in covered:
            if ef.value is not None:
                setattr(form, name, ef.model_copy(update={"confidence": SINGLE_READ_CONFIDENCE}))
        elif same_reading(tier1.get(name), ef.value):
            setattr(form, name, ef.model_copy(update={"confidence": AGREEMENT_CONFIDENCE}))
        else:
            routed.append(name)
    return routed


def _reconcile_second_read(
    form: BaseModel,
    second: BaseModel,
    names: list[str],
    tier1: dict[str, Any] | None = None,
    no_strict: frozenset[str] = frozenset(),
) -> None:
    """Merge Tier 3's second read of ``names`` onto ``form`` (in place).

    ``tier1`` holds Tier 1's readings (field → value; empty when not given).
    ``no_strict`` names fields whose Tier-1 reading came from a generic
    label (``tier1_paddleocr_local.parse_with_generic_labels``): it still
    counts for the Tier-3 match below, but never strict-parks. Per field:

    - Tier 3's value matches Tier 1's (``same_reading``) → take Tier 3's
      field at ``AGREEMENT_CONFIDENCE``: two independent readers agree.

    Otherwise, comparing the current value (Tier 2's, or Tier 1's if Tier 2
    failed) with Tier 3's parsed value by exact equality:

    - both empty → unchanged (stays confidently blank), or Tier 3's blank
      stamp if the field had never been attempted (Tier 2 failed);
    - equal → keep, confidence ``AGREEMENT_CONFIDENCE``;
    - otherwise (a disagreement) → keep the first reading if it passes its
      validator or Tier 3 read nothing; otherwise take Tier 3's field. So a
      Tier-2 blank against a Tier-3 value takes Tier 3's, and a Tier-3
      blank against a Tier-2 value keeps Tier 2's (a reading a human can
      check is never dropped).

    Every case except "equal" and "both empty" ends at
    ``DISAGREEMENT_CONFIDENCE`` so the field parks for review. **Strict
    parking:** when Tier 1 has a valid reading (``tier1`` holds only those)
    from a specific label (not in ``no_strict``) and the kept value doesn't
    match it, the field also gets ``DISAGREEMENT_CONFIDENCE`` — two Qwen
    readings agreeing against the printed text go to a human. Under a
    generic label ("15. OTHER DATE"), Tier 3 matching Tier 2 settles the
    field at ``AGREEMENT_CONFIDENCE`` instead. A case or
    spacing difference between Tier 2 and Tier 3 counts as a disagreement
    (conservative: it goes to a human rather than being silently accepted).
    """
    tier1 = tier1 or {}
    for name in names:
        first, other = getattr(form, name), getattr(second, name)
        t1_value = tier1.get(name)
        if same_reading(other.value, t1_value):
            setattr(form, name, other.model_copy(update={"confidence": AGREEMENT_CONFIDENCE}))
            continue
        if first.value is None and other.value is None:
            if first.tier_used is None:
                setattr(form, name, other)  # only Tier 3 attempted it
            continue
        if first.value == other.value:
            chosen, confidence = first, AGREEMENT_CONFIDENCE
        else:
            keep_first = other.value is None or (
                first.value is not None and validate(name, first.value) is True
            )
            chosen, confidence = (first if keep_first else other), DISAGREEMENT_CONFIDENCE
        if (
            t1_value is not None
            and name not in no_strict
            and not same_reading(chosen.value, t1_value)
        ):
            confidence = DISAGREEMENT_CONFIDENCE
        setattr(form, name, chosen.model_copy(update={"confidence": confidence}))


def _park(form: BaseModel, names: list[str]) -> None:
    """Give every field in ``names`` that holds a value
    ``DISAGREEMENT_CONFIDENCE`` (Tier 3 failed, so nothing settled them)."""
    for name in names:
        ef = getattr(form, name)
        if ef.value is not None:
            setattr(form, name, ef.model_copy(update={"confidence": DISAGREEMENT_CONFIDENCE}))


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
    reason: str = "low_confidence",
) -> list[dict[str, Any]]:
    """Build ``field_attempts`` rows for the fields ``tier`` just handled.

    ``reason`` is recorded for the fields in ``escalated`` (sent on to the
    next tier).
    """
    rows: list[dict[str, Any]] = []
    for name in names:
        ef = getattr(form, name)
        rows.append(
            {
                "field_name": name,
                "tier": tier,
                "value": None if ef.value is None else str(ef.value),
                "confidence": ef.confidence,
                "escalation_reason": reason if name in escalated else None,
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
        form, tier1_generic = tier1_mod.parse_with_generic_labels(t1_result.raw_response, form_cls)
        _stamp_metadata(form, doc_id, decision)

        extractable = list(_qwen_vl._extractable_fields(form_cls).keys())
        covered = tier1_mod.covered_fields(form_cls)
        # Tier 1's own readings of the fields it covers, kept apart from the
        # form (Tier 2's merge overwrites them) for the Tier 3 routing.
        # A reading that fails its own validator (a ZIP read as the state)
        # counts as "Tier 1 read nothing" (since 2026-10-25).
        tier1_readings = {
            name: value
            for name in extractable
            if name in covered
            and (value := getattr(form, name).value) is not None
            and validate(name, value) is not False
        }

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
                # --- Routing: which Tier-2 fields Tier 3 reads ---------------
                # A form Tier 1 covers no field of (BusinessDocumentForm)
                # keeps the second read of every Tier-2 field.
                routed = _route_to_tier3(form, esc1, tier1_readings, covered) if covered else esc1
                attempt_rows += _attempt_rows(
                    form,
                    esc1,
                    2,
                    t2_result.latency_ms,
                    escalated=set(routed),
                    reason="second_read",
                )
                final_tier = "2"
            except TierExhausted as exc:
                error_history.append({"tier": "2", "error": str(exc), "fields": esc1})
                routed = esc1  # Tier 3 reads every field Tier 2 should have

            # --- Tier 3 second read of the routed fields --------------------
            if routed:
                escalations["3a"] = routed
                sub3 = narrow_form_cls(form_cls, routed)
                try:
                    t3_result = _run_tier_with_retry(tier3, png, sub3)
                    # The 3a rows record Tier 3's own reading; the reconciled
                    # value lives on the final form.
                    attempt_rows += _attempt_rows(
                        t3_result.form, routed, "3a", t3_result.latency_ms, escalated=set()
                    )
                    _reconcile_second_read(
                        form, t3_result.form, routed, tier1_readings, tier1_generic
                    )
                    final_tier = "3a"
                except TierExhausted as exc:
                    error_history.append({"tier": "3a", "error": str(exc), "fields": routed})
                    _park(form, routed)

        # --- Exhaustion check ----------------------------------------------
        # Fields still below the gate after the last tier that ran (a
        # second-read disagreement, or a field never produced at all)
        # exhaust the V1 cascade → review queue, partial extraction kept.
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
