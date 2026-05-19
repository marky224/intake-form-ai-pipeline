"""Shared Qwen 2.5 VL extraction core (Tier 2 7B + Tier 3 32B).

Tier 2 (``tier2_qwen_7b_local``) and Tier 3 (``tier3_qwen_32b_local``) are
the **same model family** — Qwen 2.5 VL, 7B → 32B. Escalation is "more
parameters," not "different model." Their prompt-building, response-schema
construction, JSON parsing, and the deterministic inner-type confidence
heuristic are therefore *identical*; only the model tag, tier ID, and
eval-cache directory differ (both tiers are plain registry ``ollama pull``
builds — ``qwen2.5vl:7b`` / ``qwen2.5vl:32b``).

This module is the single source of truth for that shared logic. Each tier
module keeps only its constants + a thin Protocol-conforming class +
``_invoke_model`` / ``_load_ollama_client`` shims (the test seams the tier's
own test file monkeypatches). The pure functions here take ``tier`` /
``pipeline_version`` as parameters so one parser serves both tiers and the
cached-replay path stays bit-identical to the live path for each.

All four V1 design decisions (per-field confidence is a deterministic
inner-type heuristic not model self-report; ``bounding_box=None``;
schema-constrained decoding; full ``form_cls`` extraction) and the
confidently-blank contract are documented on the tier modules — this module
just implements them once. See ``tier2_qwen_7b_local`` for the long-form
rationale.
"""

from __future__ import annotations

import json
import re
import typing
from datetime import UTC, date, datetime
from typing import Any, Literal, get_args, get_origin, get_type_hints

from pydantic import BaseModel, ValidationError

from cascade.providers._base import T
from intake_schemas import (
    ExtractedField,
    FieldMeta,
    FormMetadata,
    TierId,
)

#: Confidence for a string-like field used verbatim (no coercion). The inner
#: type decides this statically so the cached-replay path is reproducible
#: from the fixture alone (no runtime value-comparison heuristics).
CLEAN_VALUE_CONFIDENCE = 1.0

#: Confidence for a non-string scalar that had to be format-coerced from the
#: model's string output (date / int / float / bool).
FORMAT_COERCED_CONFIDENCE = 0.5

#: Ollama server. The default local endpoint; no cloud surface in V1. Shared
#: by both Qwen-VL tiers (same ``ollama serve``).
OLLAMA_HOST = "http://127.0.0.1:11434"

#: Asks Ollama to hold the model resident for an hour so it doesn't unload
#: between consecutive docs in an eval batch. Accepted verbatim by the
#: ``ollama`` client's ``keep_alive`` kwarg.
OLLAMA_KEEP_ALIVE = "1h"

#: Decoding temperature. 0.0 = greedy; extraction is a transcription task,
#: not a generation task — determinism beats diversity here.
OLLAMA_TEMPERATURE = 0.0

#: JSON-Schema ``type`` keyword per Python scalar. Every property is also
#: union'd with ``"null"`` so the model can confidently decline a field.
_JSON_SCALAR_TYPE: dict[type, str] = {
    str: "string",
    bool: "boolean",
    int: "integer",
    float: "number",
    date: "string",  # ISO 8601 date string; coerced to date by Pydantic.
}


def _strip_annotated(annotation: Any) -> Any:
    """Return ``X`` from ``Annotated[X, ...]``; passthrough otherwise."""
    if get_origin(annotation) is typing.Annotated:
        return get_args(annotation)[0]
    return annotation


def _inner_type(annotation: Any) -> Any:
    """Extract ``X`` from an ``Annotated[ExtractedField[X], FieldMeta(...)]`` hint.

    ``ExtractedField`` is a Pydantic v2 generic model: ``ExtractedField[str]``
    is a concrete synthetic subclass, so ``typing.get_args`` returns ``()``
    and the parametrization lives in ``__pydantic_generic_metadata__``
    instead. Fall back to ``typing.get_args`` for any non-Pydantic generic.
    Returns ``None`` if no type argument is recoverable.
    """
    base = _strip_annotated(annotation)
    pgm = getattr(base, "__pydantic_generic_metadata__", None)
    if pgm and pgm.get("args"):
        return pgm["args"][0]
    args = get_args(base)
    return args[0] if args else None


def _scalar_kind(inner: Any) -> Literal["string_like", "coerced"] | None:
    """Classify a field's inner type for prompting + confidence.

    - ``"string_like"`` → ``str`` or ``Literal[...]`` of strings. Used
      verbatim; ``confidence=1.0`` when it validates.
    - ``"coerced"`` → ``bool`` / ``int`` / ``float`` / ``date``. The model
      emits a string we hand to Pydantic to coerce; ``confidence=0.5``.
    - ``None`` → not a promptable scalar (``SignatureCapture``,
      ``list[...]``, nested ``BaseModel``). Left unattempted.
    """
    if inner is str:
        return "string_like"
    if get_origin(inner) is Literal:
        return "string_like" if all(isinstance(a, str) for a in get_args(inner)) else None
    # ``bool`` is a subclass of ``int`` — check it first.
    if inner is bool or inner is int or inner is float or inner is date:
        return "coerced"
    return None


def _json_schema_type(inner: Any) -> str:
    """JSON-Schema scalar ``type`` string for a string-like / coerced inner type."""
    if get_origin(inner) is Literal:
        return "string"
    return _JSON_SCALAR_TYPE.get(inner, "string")


def _extractable_fields(form_cls: type[T]) -> dict[str, tuple[Any, FieldMeta]]:
    """Map ``field_name -> (inner_type, FieldMeta)`` for promptable scalar fields.

    Walks ``form_cls``'s annotations (MRO-resolved so subclass overrides
    win — same resolution ``get_field_metadata`` uses), keeps only fields
    whose ``ExtractedField`` inner type is a promptable scalar, and pairs
    each with its ``FieldMeta`` (for the canonical name + human description
    the prompt is built from). Insertion order follows declaration order so
    the prompt + schema are deterministic across runs.
    """
    hints = get_type_hints(form_cls, include_extras=True)
    out: dict[str, tuple[Any, FieldMeta]] = {}
    for field_name, hint in hints.items():
        meta = next(
            (m for m in getattr(hint, "__metadata__", ()) if isinstance(m, FieldMeta)),
            None,
        )
        if meta is None:
            continue
        inner = _inner_type(hint)
        if inner is None or _scalar_kind(inner) is None:
            continue
        out[field_name] = (inner, meta)
    return out


def _type_hint_text(inner: Any) -> str:
    """Human-readable type label for the prompt's field list."""
    if get_origin(inner) is Literal:
        return "one of " + " | ".join(repr(a) for a in get_args(inner))
    return {
        str: "string",
        bool: "boolean (true/false)",
        int: "integer",
        float: "number",
        date: "date in ISO 8601 (YYYY-MM-DD)",
    }.get(inner, "string")


def build_extraction_prompt(form_cls: type[T]) -> str:
    """Build the text prompt enumerating every promptable field.

    Pure function of ``form_cls`` — no model state, no I/O. Both
    ``HealthcareIntakeForm`` and ``BusinessDocumentForm`` flow through this
    unchanged (schema-driven, no per-vertical alias machinery — that's Tier
    1's concern, not the prompted-VL tiers').
    """
    fields = _extractable_fields(form_cls)
    lines = [
        "You are a precise document-extraction engine. Read the attached "
        "single-page form image and extract the following fields. Return a "
        "single JSON object whose keys are EXACTLY the field names listed "
        "below.",
        "",
        "Rules:",
        "- Transcribe values exactly as printed on the form. Do not infer, "
        "normalize, or invent values.",
        "- If a field is not present, not filled in, or not legible, return "
        "null for that field. Returning null is correct and expected for "
        "absent fields — do not guess.",
        "- For date fields, output ISO 8601 (YYYY-MM-DD).",
        "- Return ONLY the JSON object, no prose, no markdown fences.",
        "",
        "Fields:",
    ]
    for field_name, (inner, meta) in fields.items():
        lines.append(f'- "{field_name}" ({_type_hint_text(inner)}): {meta.description}')
    return "\n".join(lines)


def build_response_schema(form_cls: type[T]) -> dict[str, Any]:
    """Build the JSON schema handed to Ollama ``chat(format=...)``.

    A flat object: one nullable property per promptable field, all required
    (so the model emits every key — an explicit ``null`` is the
    confidently-blank signal), ``additionalProperties: false`` so the model
    can't wander off-schema.
    """
    fields = _extractable_fields(form_cls)
    properties: dict[str, Any] = {
        name: {"type": [_json_schema_type(inner), "null"]}
        for name, (inner, _meta) in fields.items()
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def extract_json_object(content: str) -> dict[str, Any]:
    """Parse the model's response text into a dict. Tolerant.

    Schema-constrained decoding makes ``content`` a clean JSON object in the
    normal case. The fallbacks cover a degraded server / unconstrained
    response: strip ``json`` markdown fences, then scrape the first
    balanced ``{...}`` span. Returns ``{}`` if nothing parses — the caller
    turns that into an all-confidently-blank form rather than crashing.
    """
    if not isinstance(content, str) or not content.strip():
        return {}
    try:
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        pass

    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fenced:
        try:
            parsed = json.loads(fenced.group(1))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass

    start = content.find("{")
    if start < 0:
        return {}
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(content[start : i + 1])
                    return parsed if isinstance(parsed, dict) else {}
                except json.JSONDecodeError:
                    return {}
    return {}


def _response_content(raw: dict[str, Any]) -> str:
    """Pull the assistant message text out of an Ollama ``chat`` response dict.

    The cached fixture is ``ChatResponse.model_dump(mode="json")`` — the
    text lives at ``message.content``. Defensive: a malformed/legacy shape
    returns ``""`` and the parser produces an all-blank form.
    """
    message = raw.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    return ""


def _blank_field(tier: TierId) -> ExtractedField[Any]:
    """A confidently-blank field: model attempted it, found nothing usable.

    ``tier_used=<tier>`` with ``value=None`` is the "attempted, blank"
    signal ``compute_form_confidence`` keys off (vs. ``tier_used=None`` =
    "never attempted"). ``tier`` is whatever the calling tier passes — an
    ``int`` (Tier 2 = ``2``) or a ``str`` (Tier 3 = ``"3a"``); both flow
    through ``ExtractedField.tier_used`` (a ``TierId`` literal) and the
    ``is None`` confidence check identically.
    """
    return ExtractedField(value=None, confidence=0.0, tier_used=tier, bounding_box=None)


def stub_metadata(form_cls: type, *, pipeline_version: str) -> FormMetadata:
    """Placeholder ``FormMetadata`` for a single provider call.

    The provider doesn't know the upstream ``source_document_id``; Phase 5's
    orchestrator replaces this when assembling the cascade output. The stub
    just keeps the Pydantic instance valid. ``pipeline_version`` is
    tier-specific (passed by the caller) so the stub is self-describing.
    """
    return FormMetadata(
        form_type=form_cls.__name__,
        source_document_id="<pending-orchestrator>",
        extraction_timestamp=datetime.now(UTC),
        pipeline_version=pipeline_version,
    )


def parse_response(
    raw: dict[str, Any],
    form_cls: type[T],
    *,
    tier: TierId,
    pipeline_version: str,
) -> T:
    """Parse an Ollama response dict into a populated ``form_cls`` instance.

    Pure function — no I/O, no model state. The cached-replay path and the
    live path both run through this so behavior is identical. ``tier`` and
    ``pipeline_version`` are the only tier-specific inputs; everything else
    is shared between the 7B and 32B Qwen-VL providers.

    For every promptable scalar field:
      - Model returned a usable value → ``ExtractedField`` with the
        deterministic heuristic confidence (1.0 string-like / 0.5 coerced)
        and ``bounding_box=None``.
      - Model returned null / missing / a value Pydantic rejects → stamped
        confidently-blank (``tier_used=<tier>``, ``value=None``).
    Non-scalar fields are left at their default (unattempted,
    ``tier_used=None``).

    The form is built once with all candidate values; on a
    ``ValidationError`` the offending fields are demoted to confidently-blank
    and the form is rebuilt — same drop-rather-than-crash discipline Tier 1
    uses for column-shift / format mispairs.
    """
    extractable = _extractable_fields(form_cls)
    parsed = extract_json_object(_response_content(raw))

    overrides: dict[str, ExtractedField[Any]] = {}
    for field_name, (inner, _meta) in extractable.items():
        value = parsed.get(field_name)
        if isinstance(value, str):
            value = value.strip() or None
        if value is None:
            overrides[field_name] = _blank_field(tier)
            continue
        kind = _scalar_kind(inner)
        conf = CLEAN_VALUE_CONFIDENCE if kind == "string_like" else FORMAT_COERCED_CONFIDENCE
        overrides[field_name] = ExtractedField(
            value=value,
            confidence=conf,
            tier_used=tier,
            raw_text=str(value),
            bounding_box=None,
        )

    meta = stub_metadata(form_cls, pipeline_version=pipeline_version)
    try:
        return form_cls(metadata=meta, **overrides)
    except ValidationError as e:
        bad = {err["loc"][0] for err in e.errors() if err["loc"]}
        # A value the schema rejected (bad Literal, unparseable date, …) was
        # still *attempted* — demote to confidently-blank, don't drop to
        # unattempted. That's the prompted-VL contract (differs from Tier 1).
        for name in bad:
            if name in extractable:
                overrides[name] = _blank_field(tier)
        try:
            return form_cls(metadata=meta, **overrides)
        except ValidationError:
            # Pathological: a blank-stamp itself can't validate. Fall back
            # to an all-blank form so the cascade never crashes.
            all_blank = {name: _blank_field(tier) for name in extractable}
            return form_cls(metadata=meta, **all_blank)


def load_ollama_client(host: str = OLLAMA_HOST) -> Any:
    """Lazy ``ollama`` import; construct a client pinned to ``host``.

    Raises ``ImportError`` with a generic install hint when ``ollama`` is
    missing — the cached-replay path never reaches this code, so CI doesn't
    need a running Ollama server. Each tier module wraps this in its own
    ``_load_ollama_client`` so the error message can point at that tier's
    ``docs/local-development.md`` setup section, and so the tier's test file
    keeps its ``monkeypatch.setattr(<tier_module>, "_load_ollama_client")``
    seam.
    """
    try:
        from ollama import Client  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "Qwen-VL live inference requires the `ollama` client and a "
            "running `ollama serve`. See docs/local-development.md. "
            "Cached-replay tests do not require this."
        ) from e
    return Client(host=host)


def invoke_model(
    client: Any,
    png: bytes,
    form_cls: type[T],
    *,
    model_tag: str,
    temperature: float = OLLAMA_TEMPERATURE,
    keep_alive: str = OLLAMA_KEEP_ALIVE,
) -> dict[str, Any]:
    """Run one Qwen 2.5 VL chat completion (7B or 32B per ``model_tag``).

    The image is passed via the message's ``images=[<raw png bytes>]`` key —
    the ``ollama`` client b64-encodes ``bytes`` itself. This is the *real*
    Ollama API shape verified on ``openclaw-pc`` (Tier 2 PR #51);
    ``architecture-locked.md`` describes the call conceptually in OpenAI
    ``content:[{type:image}]`` form, which is NOT how the Python client
    passes images. Tier 3's registry 32B (``qwen2.5vl:32b``, Q4_K_M) honors
    the same ``format=<schema>`` schema-constrained decoding as the 7B
    registry build (verified on the box before the provider was locked).

    Returns ``ChatResponse.model_dump(mode="json")`` — a fully
    JSON-serializable dict (no raw ``bytes``: the image is not echoed back in
    the response) that round-trips through the eval cache and is re-parsed by
    ``parse_response`` on a cache hit.
    """
    prompt = build_extraction_prompt(form_cls)
    schema = build_response_schema(form_cls)
    response = client.chat(
        model=model_tag,
        messages=[{"role": "user", "content": prompt, "images": [png]}],
        format=schema,
        options={"temperature": temperature},
        keep_alive=keep_alive,
    )
    # ChatResponse is a pydantic model on the modern client; older shapes are
    # already dicts. Normalize to a JSON-serializable dict either way.
    if isinstance(response, BaseModel):
        return response.model_dump(mode="json")
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    return dict(response)
