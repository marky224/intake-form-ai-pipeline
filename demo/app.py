"""Phase 7-V1 local demo — Streamlit view (run via ``just demo``).

Thin presentation layer only: every cascade run, F1 sweep, and file read
lives in :mod:`demo.data` (which CI imports and tests without Streamlit).
This module is never imported by CI and never unit-tested — keep logic out
of it.

Run locally on ``openclaw-pc``::

    just demo            # cached replay: $0, no GPU, no Ollama/Paddle
    EVAL_LIVE=true just demo   # drive the real local models instead

Everything renders honestly:

* The headline F1-over-time chart is the **Tier-1 stage** (it climbs as the
  alias table fills). The end-to-end **cascade** F1 is shown right beside it,
  flat ≈0.78 — labeled the *robustness* stat, never relabeled as a climb.
* Documents land in ``review_queue`` because the locked coerced-scalar
  confidence (0.5) sits under the 0.80 gate. That panel is presented as the
  intended human-in-the-loop surface, not an error.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``streamlit run demo/app.py`` puts *this file's* directory on sys.path, not
# the repo root, so the ``demo`` / ``cascade`` / ``evals`` packages aren't
# importable by default. Prepend the repo root before any first-party import.
# (This is the standard Streamlit-entrypoint bootstrap; ``just demo`` runs
# from the repo root regardless.)
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import streamlit as st  # noqa: E402

from demo.data import (  # noqa: E402
    ESCALATION_GATE,
    GATE_TIER1_TO_TIER2,
    CorrectionReplay,
    DemoRun,
    TwoStageF1,
    f1_chart_svg,
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
def _chart_svg() -> str:
    return f1_chart_svg()


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


def _render_two_stage(ts: TwoStageF1) -> None:
    st.subheader("F1 over time — the two-stage story")
    st.markdown(
        "The portfolio chart below plots **Tier-1-stage F1**: it climbs from "
        f"**{ts.tier1_start:.3f}** to **{ts.tier1_end:.3f}** as the "
        "progressive alias table fills in known phrasings — that is the "
        "self-improvement signal. End-to-end **cascade F1 stays flat at "
        f"≈{ts.cascade_mean:.2f}**: strong Tier 2/3 escalation already "
        "compensates for what the alias table later learns, so the merged "
        "result barely moves. The flat number is the **robustness** stat — "
        "the cascade is resilient regardless of alias-table maturity — *not* "
        "a climbing curve in disguise."
    )
    left, right = st.columns([3, 2])
    with left:
        st.markdown("**Headline — Tier-1-stage F1 (committed artifact)**")
        st.image(_chart_svg(), use_container_width=True)
    with right:
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
            "6 Synthea-rendered CMS-1500 forms (the Phase 6 fixtures "
            "manifest). DocILE is local-only (CC-BY-NC-ND) and not shipped."
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
    _render_two_stage(_two_stage())


if __name__ == "__main__":
    main()
