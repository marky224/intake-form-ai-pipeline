"""Phase 7-V1 local demo — Streamlit view (run via ``just demo``).

Thin presentation layer only: every cascade run, F1 sweep, and file read
lives in :mod:`demo.data` (which CI imports and tests without Streamlit).
This module is never imported by CI and never unit-tested — keep logic out
of it.

Run locally on ``openclaw-pc``::

    just demo            # cached replay: $0, no GPU, no Ollama/Paddle
    EVAL_LIVE=true just demo   # drive the real local models instead

Everything renders honestly:

* The headline by-stage chart shows the cascade is **not monotone** — the
  Q4_K_M Tier 3 regresses — beside an escalation funnel whose cumulative
  cells-resolved coverage *does* rise to 100%. Both readings, same run.
* The two-stage F1 numbers below show the **Tier-1 stage** climbing as the
  alias table fills, beside the flat end-to-end **cascade** F1 (≈0.78) —
  labeled the *robustness* stat, never relabeled as a climb.
* Documents land in ``review_queue`` because the locked coerced-scalar
  confidence (0.5) sits under the 0.80 gate. That panel is presented as the
  intended human-in-the-loop surface, not an error.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``streamlit run src/demo/app.py`` puts *this file's* directory on sys.path,
# not the src/ tree, so the ``demo`` / ``cascade`` / ``evals`` / ``_paths``
# top-level imports aren't resolvable by default. Prepend the src/ root before
# any first-party import. Belt-and-suspenders: with the editable-installed
# package (``uv sync``) this is redundant; without it (a fresh checkout someone
# runs streamlit on directly) this is what keeps things working. Computed
# inline rather than via ``_paths`` because ``_paths`` itself isn't importable
# until this line runs. See memory project_src_layout.
_SRC_ROOT = str(Path(__file__).resolve().parent.parent)
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

import streamlit as st  # noqa: E402

from demo.data import (  # noqa: E402
    ESCALATION_GATE,
    GATE_TIER1_TO_TIER2,
    ByStageSummary,
    CorrectionReplay,
    DemoRun,
    TwoStageF1,
    by_stage_chart_svg,
    by_stage_summary,
    list_demo_docs,
    replay_review_queue_corrections,
    run_document,
    submit_correction,
    two_stage_f1,
)


# cache_resource (not cache_data): the return values hold Pydantic models with
# parametrized generics (ExtractedField[str], …) that st.cache_data's pickle
# round-trip can't serialize. cache_resource stores the object as-is and is
# still keyed by the function args, which is exactly what we want here (one
# cached cascade run per doc_id; one cached sweep).
@st.cache_resource(show_spinner="Running the cascade (cached replay)…")
def _run(doc_id: str) -> DemoRun:
    return run_document(doc_id)


@st.cache_resource(show_spinner="Sweeping the progressive alias table…")
def _two_stage() -> TwoStageF1:
    return two_stage_f1()


@st.cache_data
def _by_stage_chart_svg() -> str:
    return by_stage_chart_svg()


@st.cache_resource(show_spinner="Measuring the by-stage ablation (cached)…")
def _by_stage() -> ByStageSummary:
    return by_stage_summary()


@st.cache_resource(show_spinner="Replaying the review queue (cached)…")
def _replay() -> CorrectionReplay:
    return replay_review_queue_corrections()


def _render_run_summary(run: DemoRun) -> None:
    rec = run.record
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Routed vertical", rec.vertical)
    c2.metric("Final tier", rec.final_tier)
    c3.metric("Latency", f"{rec.total_latency_ms:,.0f} ms")
    c4.metric("Cost", "$0.00", help="V1 is all-local — every provider rate is $0.")

    st.caption(
        f"Router stage {rec.router_stage} · score {rec.router_score:.2f} · "
        f"status `{rec.status}`"
    )

    if rec.escalations:
        st.markdown("**Per-tier escalations** — fields each tier was asked to fix:")
        for tier, names in rec.escalations.items():
            with st.expander(f"Tier {tier} — {len(names)} field(s)"):
                st.write(", ".join(sorted(names)) or "_(none)_")
    else:
        st.info("No escalations — Tier 1 cleared every field above the gate.")


def _render_fields(run: DemoRun) -> None:
    st.subheader("Per-field extraction")
    st.caption(
        f"Confidence is read through the schema; a populated value below the "
        f"**{ESCALATION_GATE:.2f}** Tier-2→3 gate (highlighted) escalates "
        f"rather than landing in the form unchallenged. The Tier-1→2 gate is "
        f"{GATE_TIER1_TO_TIER2:.2f}."
    )
    rows = [
        {
            "field": r.name,
            "value": r.value if not r.blank else "—",
            "confidence": round(r.confidence, 3),
            "tier": r.tier_used or "—",
            "status": ("⚠ below gate" if r.below_gate else ("blank" if r.blank else "ok")),
        }
        for r in run.fields
    ]
    # Explicit column_config so confidence renders as a labeled 0-1 progress
    # bar (the bare numeric column collapses to an unreadable sliver under
    # use_container_width); fixed widths keep the other columns legible too.
    st.dataframe(
        rows,
        use_container_width=True,
        hide_index=True,
        column_config={
            "field": st.column_config.TextColumn("Field", width="medium"),
            "value": st.column_config.TextColumn("Extracted value", width="large"),
            "confidence": st.column_config.ProgressColumn(
                "Confidence",
                help=f"Tier-2→3 gate is {ESCALATION_GATE:.2f}",
                format="%.3f",
                min_value=0.0,
                max_value=1.0,
                width="medium",
            ),
            "tier": st.column_config.TextColumn("Tier", width="small"),
            "status": st.column_config.TextColumn("Status", width="small"),
        },
    )


def _render_review_queue(run: DemoRun) -> None:
    st.subheader("Human-in-the-loop review queue")
    if not run.in_review_queue:
        st.success("This document cleared the cascade without parking for review.")
        return
    st.warning(
        "This document is parked in `review_queue` — **by design, not by "
        "failure.** The locked Tier-2/3 confidence heuristic scores a coerced "
        "date/int/float/bool field at 0.5, under the 0.80 gate, so any form "
        "with such a field necessarily reaches a human after Tier 3. This is "
        "the intended human-in-the-loop surface the V1 cascade is built "
        "around — Phase 8 wires reviewer corrections back into the alias "
        "table and the retrieval corpus."
    )
    if run.review_drivers:
        st.markdown(
            "**Fields that drove the review parking** "
            "(populated but under gate): " + ", ".join(f"`{n}`" for n in run.review_drivers)
        )
    if run.record.error_history:
        with st.expander("Per-tier error history (persisted to review_queue)"):
            st.json(run.record.error_history)


def _render_correction_loop(run: DemoRun) -> None:
    st.subheader("Phase 8 — reviewer correction feedback loop")
    st.markdown(
        "A correction on a parked field does three things: it is logged to "
        "the `corrections` table, the on-form label the cascade missed is "
        "appended to a **live alias overlay** (the seed v1.0.0 + F1 chart "
        "stay frozen — this is the runtime extension, the *same* alias path "
        "the progressive-partition sweep simulates offline), and the "
        "document is re-embedded into the ColQwen retrieval corpus. "
        "Everything here runs into a throwaway DB + overlay — the demo never "
        "mutates persistent state."
    )

    if run.in_review_queue and run.review_drivers:
        with st.form("submit_correction"):
            st.markdown("**Submit one correction** (interactive, this parked document)")
            field = st.selectbox("Field to correct", options=run.review_drivers)
            corrected = st.text_input("Corrected value")
            label = st.text_input(
                "On-form label the cascade missed (optional)",
                help="Supplying the printed label closes the alias half of "
                "the loop — it is learned only if not already recognized.",
            )
            if st.form_submit_button("Submit correction") and corrected.strip():
                outcome = submit_correction(
                    run.record.doc_id, field, corrected.strip(), label.strip() or None
                )
                st.success(
                    f"Logged correction #{outcome.correction_id} for "
                    f"`{outcome.field_name}`: {outcome.original_value!r} → "
                    f"{outcome.corrected_value!r}."
                )
                if outcome.alias_learned:
                    st.info(f"New alias learned: **{outcome.learned_alias}**")
                elif label.strip():
                    st.caption(
                        "That phrasing was already a recognized alias — nothing "
                        "added (the loop never fabricates a phantom alias)."
                    )

    st.markdown("**Or replay the whole review queue** (seeded-reviewer, cached/$0)")
    rp = _replay()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Parked docs", len(rp.docs))
    c2.metric("Corrections logged", rp.corrections_applied)
    c3.metric("New aliases learned", rp.aliases_learned)
    c4.metric(
        "Embeddings refreshed",
        rp.embeddings_refreshed,
        help="0 without ColQwen .npy fixtures — CI/no-GPU degrades cleanly.",
    )
    for d in rp.docs:
        with st.expander(f"{d.label} — {len(d.corrections)} correction(s)"):
            st.dataframe(
                [
                    {
                        "field": c.field_name,
                        "original": c.original_value or "—",
                        "corrected": c.corrected_value,
                        "tier": c.tier_that_produced_original or "—",
                        "alias learned": "✓" if c.alias_learned else "",
                    }
                    for c in d.corrections
                ],
                use_container_width=True,
                hide_index=True,
            )
            if d.neighbors:
                st.caption(
                    "Nearest corrected documents (ColQwen MaxSim): "
                    + ", ".join(f"`{n.doc_id[:8]}` ({n.score:.2f})" for n in d.neighbors)
                )


def _render_by_stage(bs: ByStageSummary) -> None:
    st.subheader("By-stage ablation + escalation funnel — the headline result")
    (_, f1_t1), (_, f1_t12), (_, f1_t123) = bs.stages
    verdict = (
        f"**not monotone**: the Q4_K_M Tier 3 *regresses* " f"({f1_t12:.3f} → {f1_t123:.3f})"
        if bs.regresses
        else "monotone"
    )
    st.markdown(
        f"Ablating the cascade tier-by-tier on the 92-doc test split: "
        f"**{f1_t1:.3f} → {f1_t12:.3f} → {f1_t123:.3f}**. The Qwen 7B Tier 2 "
        f"does the real lift; the result is {verdict}. It ships as-is — the "
        "honest-results gate, not a smoothed curve. What *does* rise "
        "monotonically is the **escalation funnel**: cumulative cells "
        f"resolved by tier ≤ N climbs to **100% of the {bs.populated} "
        f"populated cells**, because Tier 3 still finalizes the residual the "
        f"earlier tiers couldn't clear. The {bs.blank} genuinely-blank cells "
        "are an explicit remainder, not hidden in the denominator."
    )
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Committed artifact — funnel (top) + F1 by stage (bottom)**")
        st.image(_by_stage_chart_svg(), use_container_width=True)
    with right:
        st.markdown("**Cells resolved by tier ≤ N, live (this cached run)**")
        st.dataframe(
            [
                {
                    "stage": label,
                    "F1": round(f1, 3),
                    "cells resolved": cum,
                    "% of populated": f"{cum / bs.populated * 100:.1f}%",
                }
                for (label, f1), (_, cum) in zip(bs.stages, bs.funnel_cumulative, strict=True)
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.caption(
            f"{bs.populated} populated + {bs.blank} genuinely blank = "
            f"{bs.total} scorable cells. F1 not monotone; coverage is."
        )


def _render_two_stage(ts: TwoStageF1) -> None:
    st.subheader("F1 over time — the supporting two-stage story")
    st.markdown(
        "**Tier-1-stage F1** climbs from "
        f"**{ts.tier1_start:.3f}** to **{ts.tier1_end:.3f}** as the "
        "progressive alias table fills in known phrasings — that is the "
        "self-improvement signal. End-to-end **cascade F1 stays flat at "
        f"≈{ts.cascade_mean:.2f}**: strong Tier 2/3 escalation already "
        "compensates for what the alias table later learns, so the merged "
        "result barely moves. The flat number is the **robustness** stat — "
        "the cascade is resilient regardless of alias-table maturity — *not* "
        "a climbing curve in disguise."
    )
    st.markdown("**Both series, live (this cached run)**")
    st.dataframe(
        [
            {
                "batch": b,
                "tier-1 (headline)": round(t1, 3),
                "cascade (robustness)": round(c1, 3),
            }
            for (b, t1), (_, c1) in zip(ts.tier1_series, ts.cascade_series, strict=True)
        ],
        use_container_width=True,
        hide_index=True,
    )


def main() -> None:
    st.set_page_config(page_title="Intake-form cascade — V1 local demo", layout="wide")
    st.title("Self-improving intake-form cascade — V1 local demo")
    st.caption(
        "PaddleOCR-VL → Qwen 2.5 VL 7B → Qwen 2.5 VL 32B, all local. This demo "
        "runs the real cascade on committed CMS-1500 fixtures through the "
        "cached-replay path: $0, deterministic, nothing on the GPU."
    )

    docs = list_demo_docs()
    if not docs:
        st.error(
            "No committed CMS-1500 fixtures found under " "tests/fixtures/eval-validation/cms1500/."
        )
        return

    with st.sidebar:
        st.header("Document")
        choice = st.selectbox(
            "Pick a committed CMS-1500",
            options=docs,
            format_func=lambda d: f"{d.label}  ·  {d.doc_id[:8]}",
        )
        st.caption(
            f"{len(docs)} Synthea-rendered CMS-1500 forms — the committed "
            "92-doc held-out `test` split of the 584-doc corpus. DocILE is "
            "local-only (CC-BY-NC-ND) and not shipped."
        )

    run = _run(choice.doc_id)

    img_col, sum_col = st.columns([2, 3])
    with img_col:
        st.image(
            str(choice.png_path),
            caption=f"{choice.label} — {choice.doc_id}",
            use_container_width=True,
        )
    with sum_col:
        _render_run_summary(run)

    _render_fields(run)
    _render_review_queue(run)
    st.divider()
    _render_correction_loop(run)
    st.divider()
    _render_by_stage(_by_stage())
    st.divider()
    _render_two_stage(_two_stage())


if __name__ == "__main__":
    main()
