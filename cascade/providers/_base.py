"""Shared types for cascade providers.

The ``CascadeProvider`` Protocol pins the synchronous, single-doc shape every
tier conforms to. The cascade is strictly sequential — Phase 5's orchestrator
chains providers per confidence thresholds without parallel race-probes — so
no ``async`` here.

``ProviderResult`` carries the call-level telemetry (latency, cost) the
schema's per-field ``ExtractedField`` doesn't track. Per-field provenance
(``tier_used``, ``confidence``, ``raw_text``, ``bounding_box``) lives on
``ExtractedField`` directly per its locked shape.

The Protocol is the public contract between Phase 4 (providers) and Phase 5
(orchestrator). Once landed, changing its shape forces a coordinated PR
across all four providers — get it right here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

from pydantic import BaseModel

from intake_schemas import TierId

#: ``T`` is the form class the provider populates. In practice
#: ``HealthcareIntakeForm`` for CMS-1500 and ``BusinessDocumentForm`` for DocILE
#: invoices. Bound to ``BaseModel`` rather than ``IntakeFormBase`` so a future
#: non-IntakeFormBase form class (e.g., a standalone routing-decision payload)
#: could conform without a schema-package change.
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class ProviderResult(Generic[T]):
    """One provider call's outcome.

    ``form`` is an instance of the requested ``form_cls`` with each populated
    ``ExtractedField`` carrying its own ``tier_used``, ``confidence``,
    ``raw_text``, and ``bounding_box``. Providers MUST stamp ``tier_used`` on
    every field they populate, including confidently-blank ones
    (``value=None`` with ``tier_used`` set) — ``compute_form_confidence``
    distinguishes "never attempted" from "attempted, blank" by exactly that
    signal.

    ``latency_ms`` is wall-clock duration of the provider call, measured via
    ``time.perf_counter``. On an eval-cache hit (no live call), set to
    ``0.0``. ``cost_usd`` is the provider-reported or estimated USD cost; for
    local-inference tiers this is always ``0.0``.

    ``raw_response`` is the upstream API's response dict verbatim — this is
    what gets persisted to the eval cache. Providers re-parse it on cache hits
    so the cached-replay code path exercises the same parser as the live
    code path.
    """

    form: T
    latency_ms: float
    cost_usd: float
    raw_response: dict[str, Any]


@runtime_checkable
class CascadeProvider(Protocol):
    """The contract every tier provider implements.

    Attributes:
        name: Stable identifier. Used as the eval-cache subdirectory name
            (``tests/fixtures/eval-cache/<name>/<sha>.json``) so it must be a
            filesystem-safe slug. Convention: ``tier{N}_{shortname}_{location}``
            (e.g. ``tier1_paddleocr_local``, ``tier3b_claude_bedrock``).
        tier: The cascade tier ID per ``intake_schemas.TierId``. ``1`` / ``2``
            for the integer tiers, ``"3a"`` / ``"3b"`` for the lettered
            sub-tiers.

    Methods:
        extract: Run the provider against ``png`` bytes, returning an instance
            of ``form_cls`` populated with ``ExtractedField`` values plus
            per-call telemetry. Synchronous; one document per call. Batching,
            if needed, happens at the orchestrator layer in Phase 5.
    """

    name: str
    tier: TierId

    def extract(self, png: bytes, form_cls: type[T]) -> ProviderResult[T]: ...
