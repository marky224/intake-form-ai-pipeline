"""Two-stage document router (V1, all-local).

Stage 1 is a vocabulary keyword classifier built at orchestrator startup
from ``alias_table_seed.json`` — a runtime build (~50 ms once per process,
no cache file to invalidate). An alias is **healthcare-distinctive** when it
appears in a ``vertical="healthcare"`` seed record AND in no
``vertical="base" | "insurance" | "hr"`` record (the locked inclusion rule:
"MRN", "Patient ID", "Subscriber ID" qualify; "First Name", "Date of Birth"
do not). Each OCR line is uppercased and substring-matched against the
distinctive vocabulary; a matched alias contributes its inverse-frequency
weight (rarer alias → higher weight). The document is classified
``healthcare`` when the accumulated weighted score ≥ ``STAGE1_THRESHOLD_N``
(locked starting value ``1.0``; Phase 5 spot-check tunes — see
``tests/test_router.py`` and the PR body for the recorded value).

Stage 2 is the fallback for documents scoring below N (~20% of inputs). V1
routes Stage 2 to the **local Qwen 2.5 VL 7B** model with a one-shot
classification prompt (schema-constrained to ``{"vertical": ...}``); the
marginal cost is zero because that model is already resident for Tier 2 of
the cascade. V2 swaps Stage 2 to Bedrock Nova Lite at the BAA boundary —
not V1 work.

Stage 2 is replay-cached exactly like a cascade provider
(``tests/fixtures/eval-cache/router_stage2_qwen_7b/<sha>.json``) so CI
classifies ambiguous docs deterministically for $0 and never needs a live
Ollama server. ``EVAL_LIVE=true`` bypasses the cache and persists fresh
responses, same contract as the providers.

The router resolves a vertical to its Pydantic form class:
``healthcare`` → ``HealthcareIntakeForm``; ``business`` →
``BusinessDocumentForm`` (DocILE invoices). Those are the only two V1
verticals — the seed's ``insurance`` / ``hr`` records exist for the schema
work but no V1 corpus targets them, so they are not router outputs.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from cascade import eval_cache
from cascade.providers import _qwen_vl
from cascade.providers.tier1_paddleocr_local import _parse_html_table
from cascade.providers.tier2_qwen_7b_local import QWEN_MODEL_TAG
from intake_schemas import BusinessDocumentForm, HealthcareIntakeForm

Vertical = Literal["healthcare", "business"]

#: Locked Stage 1 classification threshold. Healthcare when the accumulated
#: inverse-frequency-weighted match score ≥ this. Starting value 1.0 per
#: architecture-locked.md "Router (V1)"; Phase 6 eval-sweep may retune.
STAGE1_THRESHOLD_N = 1.0

#: Seed verticals whose aliases *disqualify* an alias from the
#: healthcare-distinctive vocabulary (the locked inclusion rule's "AND not
#: in base/insurance/hr" clause).
_EXCLUDING_VERTICALS = frozenset({"base", "insurance", "hr"})

#: ``alias_table_seed.json`` path, resolved relative to this file (repo
#: root) so the import works regardless of the caller's cwd.
ALIAS_TABLE_PATH = Path(__file__).resolve().parent.parent / "alias_table_seed.json"

#: Stage 2 eval-cache slug. Same replay contract as the cascade providers.
STAGE2_PROVIDER_NAME = "router_stage2_qwen_7b"

#: Stage 2 schema-constrained decoding target. The 7B emits exactly this.
_STAGE2_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"vertical": {"type": "string", "enum": ["healthcare", "business"]}},
    "required": ["vertical"],
    "additionalProperties": False,
}

_STAGE2_PROMPT = (
    "You are a document-type classifier. Look at the attached single-page "
    "document image. Decide whether it is a HEALTHCARE document (e.g. a "
    "patient intake form, a CMS-1500 health insurance claim, a medical "
    "consent or medication form) or a BUSINESS document (e.g. an invoice, "
    "a purchase order, a vendor bill). Respond with a single JSON object "
    '{"vertical": "healthcare"} or {"vertical": "business"}. No prose.'
)

#: vertical → form class. The orchestrator seeds the cascade with this.
_FORM_BY_VERTICAL: dict[Vertical, type[BaseModel]] = {
    "healthcare": HealthcareIntakeForm,
    "business": BusinessDocumentForm,
}


@dataclass(frozen=True)
class RouteDecision:
    """The router's verdict for one document.

    ``stage`` is 1 when Stage 1 alone classified (score ≥ N), 2 when the
    document fell through to the Qwen 7B fallback. ``score`` is the Stage 1
    weighted score (always computed, even when Stage 2 ultimately decides —
    it's logged for the N spot-check / Phase 6 sweep). ``form_cls`` is the
    Pydantic class the cascade extracts into.
    """

    vertical: Vertical
    stage: Literal[1, 2]
    score: float
    form_cls: type[BaseModel]


@lru_cache(maxsize=1)
def build_distinctive_vocabulary() -> dict[str, float]:
    """Build ``{UPPERCASE_ALIAS: inverse_frequency_weight}`` once per process.

    An alias qualifies when it appears in ≥1 ``healthcare`` seed record and
    in **no** ``base`` / ``insurance`` / ``hr`` record (case-insensitive,
    whitespace-trimmed). Its weight is ``1 / freq`` where ``freq`` is the
    number of healthcare seed records carrying it — a phrasing unique to one
    field (freq 1, weight 1.0) outweighs one shared across several
    healthcare fields. ``lru_cache`` makes this the locked "built once at
    startup, reused across documents" behavior; tests that monkeypatch
    ``ALIAS_TABLE_PATH`` call ``build_distinctive_vocabulary.cache_clear()``.
    """
    fields = json.loads(ALIAS_TABLE_PATH.read_text(encoding="utf-8"))["fields"]

    excluded: set[str] = set()
    healthcare_counts: Counter[str] = Counter()
    for record in fields:
        vertical = record["vertical"]
        norm_aliases = {a.strip().upper() for a in record["aliases"] if a.strip()}
        if vertical == "healthcare":
            healthcare_counts.update(norm_aliases)
        elif vertical in _EXCLUDING_VERTICALS:
            excluded.update(norm_aliases)

    return {alias: 1.0 / freq for alias, freq in healthcare_counts.items() if alias not in excluded}


def ocr_lines_from_tier1_raw(raw: dict[str, Any]) -> list[str]:
    """Flatten a Tier 1 PaddleOCR-VL ``raw_response`` into OCR text lines.

    PaddleOCR-VL is form-agnostic: its ``parsing_res_list`` layout output is
    identical regardless of which ``form_cls`` Tier 1 was called with, so
    the orchestrator gets the router's OCR text from the *same* Tier 1
    invocation that does Tier 1 field extraction — one PaddleOCR pass, two
    consumers (the locked ~50 ms Stage 1 cost is the vocab match, not a
    second OCR run).

    ``block_label == "table"`` blocks (dense forms like CMS-1500 serialize
    as one ``<table>`` HTML blob) are expanded to one line per non-empty
    cell so multi-cell label rows like ``2. PATIENT'S NAME (LAST, FIRST)``
    are matchable. Other blocks split on newlines.
    """
    lines: list[str] = []
    for block in raw.get("parsing_res_list", []) or []:
        if not isinstance(block, dict):
            continue
        content = block.get("block_content")
        if not isinstance(content, str) or not content.strip():
            continue
        if block.get("block_label") == "table":
            for row in _parse_html_table(content):
                lines.extend(cell for cell in row if cell.strip())
        else:
            lines.extend(ln for ln in content.splitlines() if ln.strip())
    return lines


def stage1_score(ocr_lines: list[str]) -> float:
    """Accumulated inverse-frequency weight of distinctive aliases present.

    Each distinctive alias is counted **once** regardless of how many lines
    it hits — a label printed once on a form shouldn't outscore a rarer
    label by virtue of OCR line-splitting. Matching is uppercase substring
    (the vocabulary is pre-uppercased; lines are uppercased here).
    """
    vocab = build_distinctive_vocabulary()
    haystack = "\n".join(line.upper() for line in ocr_lines)
    return sum(weight for alias, weight in vocab.items() if alias in haystack)


def _load_ollama_client() -> Any:
    """Stage 2 Ollama client. Thin wrap of the shared loader.

    Separate seam (vs. importing ``_qwen_vl.load_ollama_client`` at call
    sites) so ``tests/test_router.py`` can
    ``monkeypatch.setattr(router, "_load_ollama_client", ...)`` without
    touching the cascade providers' own seams.
    """
    return _qwen_vl.load_ollama_client()


def _stage2_invoke(client: Any, png: bytes) -> dict[str, Any]:
    """One Qwen 7B routing classification. Tests stub via ``monkeypatch``.

    Same Ollama client call shape as the cascade providers (image via
    ``messages[].images``, schema-constrained ``format=``,
    ``keep_alive``/``temperature`` from ``_qwen_vl``) but a classification
    prompt + tiny ``{"vertical": ...}`` schema instead of the extraction
    prompt. Returns the response as a JSON-serializable dict so it
    round-trips the eval cache and is re-parsed identically on replay.
    """
    response = client.chat(
        model=QWEN_MODEL_TAG,
        messages=[{"role": "user", "content": _STAGE2_PROMPT, "images": [png]}],
        format=_STAGE2_SCHEMA,
        options={"temperature": _qwen_vl.OLLAMA_TEMPERATURE},
        keep_alive=_qwen_vl.OLLAMA_KEEP_ALIVE,
    )
    if isinstance(response, BaseModel):
        return response.model_dump(mode="json")
    if hasattr(response, "model_dump"):
        return response.model_dump(mode="json")
    return dict(response)


def _parse_stage2(raw: dict[str, Any]) -> Vertical:
    """Extract the vertical from a Stage 2 response dict.

    Schema-constrained decoding makes this a clean ``{"vertical": "..."}``;
    the tolerant ``extract_json_object`` handles a degraded server. Anything
    not explicitly ``business`` defaults to ``healthcare`` — the
    conservative choice for a healthcare-leaning portfolio corpus, and it
    keeps a malformed Stage 2 response from crashing the cascade.
    """
    parsed = _qwen_vl.extract_json_object(_qwen_vl._response_content(raw))
    return "business" if parsed.get("vertical") == "business" else "healthcare"


def _stage2_classify(png: bytes) -> Vertical:
    """Replay-cached Stage 2 classification (mirrors the provider contract)."""
    image_sha256 = hashlib.sha256(png).hexdigest()
    if not eval_cache.is_live_mode():
        cached = eval_cache.load_cached(STAGE2_PROVIDER_NAME, image_sha256)
        if cached is not None:
            return _parse_stage2(cached)
    client = _load_ollama_client()
    raw = _stage2_invoke(client, png)
    eval_cache.save_cached(STAGE2_PROVIDER_NAME, image_sha256, raw)
    return _parse_stage2(raw)


def form_cls_for(vertical: Vertical) -> type[BaseModel]:
    """Map a vertical to its cascade Pydantic form class."""
    return _FORM_BY_VERTICAL[vertical]


def route(ocr_lines: list[str], png: bytes) -> RouteDecision:
    """Classify a document: Stage 1 vocabulary, Stage 2 Qwen 7B fallback.

    ``ocr_lines`` come from the orchestrator's already-run Tier 1 pass (see
    ``ocr_lines_from_tier1_raw``). ``png`` is needed only if Stage 1 scores
    below N and Stage 2 has to look at the image. The Stage 1 score is
    reported on the decision either way (the N spot-check / Phase 6 sweep
    reads it even on Stage 2 docs).
    """
    score = stage1_score(ocr_lines)
    if score >= STAGE1_THRESHOLD_N:
        return RouteDecision("healthcare", 1, score, HealthcareIntakeForm)
    vertical = _stage2_classify(png)
    return RouteDecision(vertical, 2, score, form_cls_for(vertical))
