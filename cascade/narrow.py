"""Synthetic narrowed sub-model construction + merge-back.

Phase 5's orchestrator escalates *fields*, not whole documents: when Tier 1
populates a form but some fields land below the 0.85 confidence gate, only
those fields go to Tier 2; only the still-weak ones after Tier 2 go to
Tier 3. The frozen ``CascadeProvider`` Protocol is ``extract(png, form_cls)``
— there is no field-subset argument and the providers must not change.

The Protocol-preserving lever: build a **dynamic Pydantic sub-model** of
``form_cls`` carrying only the escalated fields (plus ``metadata``), and hand
*that* to the provider as ``form_cls``. ``cascade.providers._qwen_vl``
derives its prompt and JSON response-schema purely from
``_extractable_fields(form_cls)`` (an MRO-resolved
``get_type_hints(..., include_extras=True)`` walk keyed on the ``FieldMeta``
in each field's ``Annotated[...]`` metadata), so a sub-model whose only
``FieldMeta``-annotated fields are the escalated ones makes the Qwen-VL
prompt narrow automatically — zero provider or Protocol change. This is the
mechanism ``docs/architecture-deep-dive.md`` describes as "Phase 5's
orchestrator narrows the prompt to just the fields that escalated."

Two deliberate constraints, both load-bearing:

1. **Field annotations are copied verbatim.** Each narrowed field reuses the
   exact ``Annotated[ExtractedField[X], FieldMeta(...)]`` hint resolved from
   ``form_cls`` — same inner type ``X``, same ``FieldMeta`` instance — so the
   provider's prompt text, response-schema type, and the
   ``ExtractedField[X]`` inner-type validation (bad ``Literal`` / unparseable
   date → demoted to confidently-blank by ``parse_response``) behave
   identically to a full-form call.

2. **No cross-field / model validators on the sub-model.** The sub-model is
   built on a bare ``BaseModel`` base, not ``form_cls``, so ``form_cls``'s
   class-level / cross-field validators do **not** run on the partial
   extraction payload (they would spuriously fail — half the form is
   absent). Those validators run later on the *merged* full form, which is
   the real document. (Per-field inner-type validation still runs: it lives
   on ``ExtractedField[X]`` itself, which is copied.) Inheriting from
   ``form_cls`` would also defeat the narrowing entirely —
   ``IntakeFormBase``'s ``FieldMeta``-annotated person fields would leak
   back into ``_extractable_fields`` and re-widen the prompt.

``merge_fields`` writes the higher tier's fields back onto the running
full-form instance. Only the escalated names are copied, so confident lower
-tier values are never overwritten by a higher tier that wasn't asked about
them.
"""

from __future__ import annotations

from typing import Any, get_type_hints

from pydantic import BaseModel, Field, create_model

from cascade.providers._base import T
from intake_schemas import ExtractedField, FormMetadata


def _ef_default() -> ExtractedField[Any]:
    """Default for a narrowed field: unattempted ``ExtractedField``.

    ``parse_response`` always passes an explicit override for every
    extractable field (a value or a confidently-blank stamp), so this
    default is effectively unused — it only keeps the sub-model
    instantiable if a caller ever constructs it without all fields.
    """
    return ExtractedField()


def narrow_form_cls(form_cls: type[T], field_names: list[str]) -> type[BaseModel]:
    """Build a Pydantic sub-model of ``form_cls`` with only ``field_names``.

    The returned model has exactly ``metadata: FormMetadata`` plus one field
    per name in ``field_names``, each carrying the *verbatim*
    ``Annotated[ExtractedField[X], FieldMeta(...)]`` hint resolved from
    ``form_cls``. ``_extractable_fields`` on the sub-model therefore returns
    exactly ``field_names`` (``metadata`` has no ``FieldMeta`` so it is
    skipped), which is what narrows the downstream Qwen-VL prompt.

    ``field_names`` must be declared, ``FieldMeta``-annotated fields of
    ``form_cls``. Order is preserved so the prompt + schema stay
    deterministic. An empty list is a programming error (the orchestrator
    must not escalate with nothing to ask) and raises ``ValueError``.
    """
    if not field_names:
        raise ValueError(
            "narrow_form_cls called with no field_names; the orchestrator "
            "must not escalate a tier with zero sub-threshold fields."
        )

    hints = get_type_hints(form_cls, include_extras=True)
    field_defs: dict[str, Any] = {}
    for name in field_names:
        if name not in hints:
            raise ValueError(f"{name!r} is not a field of {form_cls.__name__}")
        # (annotation, FieldInfo) — the annotation is the exact Annotated
        # hint, so ExtractedField[X] inner-type validation + the FieldMeta
        # the provider prompts from are both preserved. default_factory
        # (not a bare callable, which Pydantic would treat as a literal
        # default value) keeps the sub-model instantiable on its own.
        field_defs[name] = (hints[name], Field(default_factory=_ef_default))

    return create_model(
        f"Narrowed_{form_cls.__name__}",
        __base__=BaseModel,
        metadata=(FormMetadata, ...),
        **field_defs,
    )


def merge_fields(
    running: T,
    higher: BaseModel,
    field_names: list[str],
) -> None:
    """Copy ``field_names`` from a higher tier's result onto ``running``.

    ``running`` is the full-form instance the cascade is assembling;
    ``higher`` is a narrowed sub-model instance a higher tier just produced.
    Only the escalated names are copied — fields the higher tier was never
    asked about don't exist on ``higher`` and are left untouched on
    ``running`` (so a confident Tier 1 value is never clobbered by a Tier 3
    that didn't see that field).

    Mutates ``running`` in place. ``running``'s model_config has
    ``validate_assignment=True``; each assigned value is already a valid
    ``ExtractedField[X]`` for that field's ``X``, so re-validation is a
    no-op pass, not a coercion.
    """
    for name in field_names:
        setattr(running, name, getattr(higher, name))
