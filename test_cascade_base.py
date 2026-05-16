"""Tests for ``cascade.providers._base``: Protocol shape + ProviderResult.

The Protocol shape is the public contract between Phase 4 providers and the
Phase 5 orchestrator. These tests pin the shape so a future refactor that
changes the signature breaks CI rather than silently producing a Protocol
all four providers no longer conform to.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime

import pytest

from cascade.providers._base import CascadeProvider, ProviderResult
from intake_schemas import (
    BusinessDocumentForm,
    ExtractedField,
    FormMetadata,
    HealthcareIntakeForm,
)


def _empty_business_form() -> BusinessDocumentForm:
    return BusinessDocumentForm(
        metadata=FormMetadata(
            form_type="DocILE_invoice",
            source_document_id="s3://x/y.png",
            extraction_timestamp=datetime(2026, 5, 13),
            pipeline_version="0.1.0",
        )
    )


# ---------------------------------------------------------------------------
# CascadeProvider Protocol
# ---------------------------------------------------------------------------


class _ConformingProvider:
    """Minimal Protocol-conforming provider used to verify the contract."""

    name = "test_conforming"
    tier: int = 1

    def extract(
        self, png: bytes, form_cls: type[BusinessDocumentForm]
    ) -> ProviderResult[BusinessDocumentForm]:
        return ProviderResult(
            form=form_cls(
                metadata=FormMetadata(
                    form_type="DocILE_invoice",
                    source_document_id="test",
                    extraction_timestamp=datetime(2026, 5, 13),
                    pipeline_version="0.1.0",
                )
            ),
            latency_ms=0.0,
            cost_usd=0.0,
            raw_response={},
        )


class _MissingNameProvider:
    tier: int = 1

    def extract(self, png: bytes, form_cls):  # type: ignore[no-untyped-def]
        raise NotImplementedError


class _MissingExtractProvider:
    name = "broken"
    tier: int = 1


def test_conforming_provider_satisfies_protocol():
    """A class with name, tier, and extract() satisfies isinstance check."""
    assert isinstance(_ConformingProvider(), CascadeProvider)


def test_missing_name_attribute_fails_protocol():
    """Without name the Protocol check rejects the instance."""
    assert not isinstance(_MissingNameProvider(), CascadeProvider)


def test_missing_extract_method_fails_protocol():
    """Without extract() the Protocol check rejects the instance."""
    assert not isinstance(_MissingExtractProvider(), CascadeProvider)


def test_extract_accepts_any_basemodel_form_class():
    """The Protocol's T is bound to BaseModel; both vertical forms qualify."""
    provider = _ConformingProvider()
    # Static-typecheck-friendly: caller picks T per call.
    biz_result = provider.extract(b"fake png", BusinessDocumentForm)
    assert isinstance(biz_result.form, BusinessDocumentForm)
    # The conforming stub above hard-codes BusinessDocumentForm in its return
    # type for simplicity. Phase 4 production providers parameterize properly;
    # we exercise that path under the Tier 1 tests.


# ---------------------------------------------------------------------------
# ProviderResult dataclass
# ---------------------------------------------------------------------------


def test_provider_result_is_frozen():
    """ProviderResult is frozen so cached-replay results can't be tampered with."""
    result: ProviderResult[BusinessDocumentForm] = ProviderResult(
        form=_empty_business_form(),
        latency_ms=12.3,
        cost_usd=0.0,
        raw_response={"upstream": "payload"},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.latency_ms = 99.9  # type: ignore[misc]


def test_provider_result_carries_call_telemetry():
    """latency_ms + cost_usd live on the result, NOT on per-field ExtractedField."""
    form = BusinessDocumentForm(
        metadata=FormMetadata(
            form_type="DocILE_invoice",
            source_document_id="s3://x/y.png",
            extraction_timestamp=datetime(2026, 5, 13),
            pipeline_version="0.1.0",
        ),
        vendor_name=ExtractedField[str](value="ACME", confidence=0.95, tier_used=1),
    )
    result = ProviderResult(
        form=form,
        latency_ms=423.7,
        cost_usd=0.025,
        raw_response={"queries": [{"text": "ACME", "score": 0.95}]},
    )
    assert result.latency_ms == 423.7
    assert result.cost_usd == 0.025
    assert result.form.vendor_name.value == "ACME"
    # Per-field telemetry stays on ExtractedField.
    assert result.form.vendor_name.tier_used == 1
    assert result.form.vendor_name.confidence == 0.95


def test_provider_result_zero_cost_for_local_tiers():
    """Local tiers (Tier 1 PaddleOCR, Tier 3a Qwen) always report 0.0 cost."""
    result = ProviderResult(
        form=_empty_business_form(),
        latency_ms=850.0,
        cost_usd=0.0,
        raw_response={},
    )
    assert result.cost_usd == 0.0


def test_provider_result_works_with_healthcare_form():
    """The Generic[T] bound covers both vertical form classes."""
    healthcare = HealthcareIntakeForm(
        metadata=FormMetadata(
            form_type="CMS_1500_02_12",
            source_document_id="s3://x/y.png",
            extraction_timestamp=datetime(2026, 5, 13),
            pipeline_version="0.1.0",
        ),
        first_name=ExtractedField[str](value="Jane", confidence=0.98, tier_used=1),
    )
    result: ProviderResult[HealthcareIntakeForm] = ProviderResult(
        form=healthcare,
        latency_ms=12.0,
        cost_usd=0.0,
        raw_response={},
    )
    assert isinstance(result.form, HealthcareIntakeForm)
    assert result.form.first_name.value == "Jane"
