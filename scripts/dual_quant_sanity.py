"""Phase 4 PR (d-V1) Tier 3 validation F1 (rescoped 2026-05-17).

Originally a Q8_0-vs-Q6_K dual-quant sanity test; the Mungert Q8_0/Q6_K
imports proved infeasible on the build box (M-RoPE ``seq_add`` assert /
VRAM — see ``architecture-locked.md`` "Quantization choice and contingency
tree"), so Tier 3 ships the registry ``qwen2.5vl:32b`` (Q4_K_M) and this
script is now the **rescoped single-config validation harness**: a focused,
deliberately coarse field-level micro-F1 used only to apply the contingency
tree's still-applicable *absolute*-F1 branches:

  - F1 >= 0.80            -> ship as locked, V1 cascade terminates
  - F1 0.65-0.80          -> document the gap publicly, ship
  - F1 < 0.65 / hallucinating -> InternVL3.5-8B fallback

NOT the Phase 6 eval harness. Two corpora, scored the *same* coarse way:

  - **CMS-1500** (``HealthcareIntakeForm``): the 6 committed validation
    PNGs. Sidecars carry only the ~8 mappable CMS-1500-box scalars.
  - **DocILE** (``BusinessDocumentForm``): the local rasterized render dir
    (uncommitted — DocILE is CC-BY-NC-ND). KILE ``fieldtype`` values map
    *verbatim* to ``BusinessDocumentForm`` scalar field names
    (``intake_schemas`` KILE_FIELDTYPES contract), so the sidecar->schema
    map is identity over the scalar fields.

If the DocILE render dir is absent (token not yet refreshed / not ingested)
the DocILE corpus is skipped with a note and only CMS-1500 is scored.

Run live (no eval cache), one or more model tags (default: the locked
registry build)::

    EVAL_LIVE=true uv run python scripts/dual_quant_sanity.py [<tag> ...]
"""

from __future__ import annotations

import json
import pathlib
import re
import sys
import time

from ollama import Client

from _paths import src_root
from cascade.providers import _qwen_vl
from intake_schemas import BusinessDocumentForm, HealthcareIntakeForm

# tests/ and synthetic_data/ both moved under src/ in the 2026-05-19
# src-layout refactor; was cwd-relative root paths. See memory project_src_layout.
CMS1500_DIR = src_root() / "tests" / "fixtures" / "eval-validation" / "cms1500"
DOCILE_RENDER_DIR = src_root() / "synthetic_data" / "output" / "docile" / "render"

DEFAULT_TAG = "qwen2.5vl:32b"  # locked registry Q4_K_M

#: CMS-1500 sidecar (CMS-1500-box) field name -> schema scalar field(s).
#: ``patient_name`` is "Last, First" -> two schema fields. Fields with no
#: scalar schema equivalent (diagnosis, signature, ...) are unscored.
CMS1500_SIDECAR_TO_SCHEMA: dict[str, list[str]] = {
    "patient_name": ["last_name", "first_name"],
    "patient_birth_date": ["date_of_birth"],
    "patient_address_line": ["address_street"],
    "patient_city": ["address_city"],
    "patient_state": ["address_state"],
    "patient_postal_code": ["address_zip"],
    "patient_phone": ["phone"],
    "date_signed": ["date_signed"],
}

#: BusinessDocumentForm scalar field names (KILE fieldtypes that resolve to
#: a single ExtractedField scalar). ``metadata``/``signature`` are non-scalar.
_BUSINESS_SCALARS = frozenset(
    n for n in BusinessDocumentForm.model_fields if n not in ("metadata", "signature")
)


def _norm(v: object) -> str:
    """Loose normalization for value comparison (case/space/punctuation)."""
    s = str(v).strip().lower()
    return re.sub(r"[^a-z0-9]+", "", s)


def _cms1500_gt(sidecar: dict) -> dict[str, set[str]]:
    """CMS-1500 sidecar -> {schema_field: {normalized expected value}}."""
    gt: dict[str, set[str]] = {}
    for fld in sidecar.get("fields", []):
        name, value = fld.get("name"), fld.get("value")
        if name not in CMS1500_SIDECAR_TO_SCHEMA or value in (None, ""):
            continue
        if name == "patient_name" and "," in str(value):
            last, first = (p.strip() for p in str(value).split(",", 1))
            gt.setdefault("last_name", set()).add(_norm(last))
            gt.setdefault("first_name", set()).add(_norm(first))
        else:
            for t in CMS1500_SIDECAR_TO_SCHEMA[name]:
                gt.setdefault(t, set()).add(_norm(value))
    return gt


def _docile_gt(sidecar: dict) -> dict[str, set[str]]:
    """DocILE sidecar -> {schema_field: {normalized expected value}}.

    KILE ``fieldtype`` == schema scalar field name (verbatim contract).
    A page may repeat a fieldtype; collapse to a set (coarse: a predicted
    value counts as a hit if it matches *any* ground-truth instance).
    """
    gt: dict[str, set[str]] = {}
    for fld in sidecar.get("docile", {}).get("fields", []):
        ft, text = fld.get("fieldtype"), fld.get("text")
        if ft in _BUSINESS_SCALARS and text not in (None, ""):
            gt.setdefault(ft, set()).add(_norm(text))
    return gt


def _score_doc(form, gt: dict[str, set[str]]) -> tuple[int, int, int]:
    """Coarse per-doc (tp, fp, fn) over the ground-truth scalar fields."""
    tp = fp = fn = 0
    for schema_field, expected in gt.items():
        got = getattr(form, schema_field, None)
        got_val = _norm(got.value) if got is not None and got.value is not None else ""
        if got_val and got_val in expected:
            tp += 1
        elif got_val:
            fp += 1
        else:
            fn += 1
    return tp, fp, fn


def _corpus_pairs(
    corpus: str,
) -> list[tuple[pathlib.Path, dict, type, str]]:
    """Return [(png, ground_truth, form_cls, label)] for a corpus, or []."""
    if corpus == "cms1500":
        pngs = sorted(CMS1500_DIR.glob("*.png"))
        return [
            (
                p,
                _cms1500_gt(json.loads(p.with_suffix(".json").read_text())),
                HealthcareIntakeForm,
                "cms1500",
            )
            for p in pngs
        ]
    pngs = sorted(DOCILE_RENDER_DIR.glob("*.png")) if DOCILE_RENDER_DIR.is_dir() else []
    return [
        (
            p,
            _docile_gt(json.loads(p.with_suffix(".json").read_text())),
            BusinessDocumentForm,
            "docile",
        )
        for p in pngs
    ]


def score_quant(model_tag: str, docile_limit: int = 14) -> dict:
    """Run CMS-1500 + (up to ``docile_limit``) DocILE docs through one tag.

    Returns per-corpus and combined cross-vertical coarse micro-F1 + timing.
    """
    client = Client(host=_qwen_vl.OLLAMA_HOST)
    pairs = _corpus_pairs("cms1500") + _corpus_pairs("docile")[:docile_limit]
    agg: dict[str, dict[str, int]] = {}
    latencies: list[float] = []
    for png, gt, form_cls, label in pairs:
        bucket = agg.setdefault(label, {"tp": 0, "fp": 0, "fn": 0, "docs": 0})
        t0 = time.perf_counter()
        raw = _qwen_vl.invoke_model(client, png.read_bytes(), form_cls, model_tag=model_tag)
        latencies.append(time.perf_counter() - t0)
        form = _qwen_vl.parse_response(
            raw, form_cls, tier="3a", pipeline_version="tier3-validation"
        )
        tp, fp, fn = _score_doc(form, gt)
        bucket["tp"] += tp
        bucket["fp"] += fp
        bucket["fn"] += fn
        bucket["docs"] += 1

    def _f1(tp: int, fp: int, fn: int) -> dict:
        p = tp / (tp + fp) if (tp + fp) else 0.0
        r = tp / (tp + fn) if (tp + fn) else 0.0
        f = 2 * p * r / (p + r) if (p + r) else 0.0
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(p, 4),
            "recall": round(r, 4),
            "f1": round(f, 4),
        }

    out: dict = {"model": model_tag, "by_corpus": {}}
    ctp = cfp = cfn = cdocs = 0
    for label, b in agg.items():
        out["by_corpus"][label] = {"docs": b["docs"], **_f1(b["tp"], b["fp"], b["fn"])}
        ctp += b["tp"]
        cfp += b["fp"]
        cfn += b["fn"]
        cdocs += b["docs"]
    out["combined"] = {"docs": cdocs, **_f1(ctp, cfp, cfn)}
    out["latency_s_mean"] = round(sum(latencies) / len(latencies), 1) if latencies else 0.0
    out["latency_s_total"] = round(sum(latencies), 1)
    f1 = out["combined"]["f1"]
    if f1 >= 0.80:
        out["tree"] = "ship-as-locked"
    elif f1 >= 0.65:
        out["tree"] = "document-gap-and-ship"
    else:
        out["tree"] = "fallback-InternVL3.5-8B"
    return out


def main() -> None:
    tags = sys.argv[1:] or [DEFAULT_TAG]
    for tag in tags:
        try:
            r = score_quant(tag)
        except Exception as e:
            r = {"model": tag, "error": f"{type(e).__name__}: {str(e)[:200]}"}
        print(json.dumps(r), flush=True)


if __name__ == "__main__":
    main()
