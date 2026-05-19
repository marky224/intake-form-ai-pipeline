"""By-stage cumulative F1 + escalation funnel — the honest tier-ablation.

A two-panel portfolio artifact, measured on the 92-doc ``test`` split via
the committed replay cache ($0, deterministic — CI reproduces it):

**Panel 1 — F1 by cumulative stage.** Three points:

- **Tier 1** — the orchestrator's pre-escalation form (Tier 1 extract →
  route → re-parse), exactly ``harness._tier1_stage_form``.
- **Tier 1+2** — the cascade with the Tier-3 slot filled by Tier 2, so a
  sub-0.80 field that escalates is just re-confirmed by the 7B (no 32B).
  A faithful "Tier 3 disabled" ceiling without touching the frozen
  orchestrator escalation logic.
- **Tier 1+2+3** — the real cascade.

The honest result is **not** monotone: ``0.340 → 0.794 → 0.768``. The 7B
Tier 2 does the real lift; the **Q4_K_M Tier 3 regresses -0.026**.
Mechanism (probed field-by-field): Tier 3 only ever re-extracts the
fields that *escalated* (confidence < 0.80). The locked 0.5-confidence
heuristic on coerced scalars (`project_phase5_coerced_review_queue`)
forces **every date field** below that gate, so every date escalates
even when Tier 2 had it right — and the quantized 32B re-extracts those
dates worse than the 7B, overwriting correct values. 29 of 31 changed
fields are Tier 3 turning a correct value wrong; ~all are dates. This is
the sharp form of the flat-cascade finding, shipped as-is (honest-results
gate) — it motivates the step-4 local Qwen3-VL Tier-3b.

**Panel 2 — escalation funnel.** The same full ``(t1,t2,t3)`` cascade
run, counting each scorable cell by the tier that finally produced its
value. Cumulative cells resolved by tier ≤ N **rises monotonically** to
100% of the populated cells (2 → 797 → 981 of 981) even though Tier 3's
*slice* F1 is the worst: Tier 3 only ever finalizes the residual the
earlier tiers couldn't clear (the forced-escalated dates), so the
cascade still drives every populated cell to a resolved state. The 123
genuinely-blank cells (no value on the source form) are shown as an
explicit remainder, not hidden — Tier 1's funnel share is a tiny 2 cells,
consistent with its 0.34 F1; it is not dressed up.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cascade import store
from cascade.orchestrator import build_cascade, process_document
from evals.ground_truth import FIELD_KIND, load_cms1500_ground_truth
from evals.harness import _tier1_stage_form
from evals.manifest import CMS1500_VALIDATION_DIR, load_manifest
from evals.metrics import aggregate, score_form

#: Repo-root-relative committed by-stage chart artifact.
BY_STAGE_CHART_PATH = Path(__file__).resolve().parent.parent / "docs" / "assets" / "f1-by-stage.svg"

_SCORABLE = list(FIELD_KIND)
STAGE_LABELS = ("Tier 1", "Tier 1+2", "Tier 1+2+3")


@dataclass(frozen=True)
class Funnel:
    """Scorable-cell escalation funnel from the full cascade run.

    ``tier1``/``tier2``/``tier3a`` count populated cells by the tier that
    finally produced their value; ``blank`` is the genuinely-empty cells
    (no value on the source form — true negatives, shown as an explicit
    remainder rather than hidden). The cascade is monotone *here*: every
    populated cell is resolved by some tier, so the cumulative-by-tier
    curve rises to 100% of ``populated`` even where the F1 panel dips.
    """

    tier1: int
    tier2: int
    tier3a: int
    blank: int

    @property
    def total(self) -> int:
        """All scorable cells (populated + genuinely blank)."""
        return self.tier1 + self.tier2 + self.tier3a + self.blank

    @property
    def populated(self) -> int:
        """Cells with an extracted value (the funnel's 100% denominator)."""
        return self.tier1 + self.tier2 + self.tier3a

    @property
    def cumulative(self) -> list[tuple[str, int]]:
        """``[(stage_label, cells_resolved_by_tier<=N), ...]`` — monotone."""
        c1 = self.tier1
        c2 = c1 + self.tier2
        c3 = c2 + self.tier3a
        return list(zip(STAGE_LABELS, (c1, c2, c3), strict=True))


@dataclass(frozen=True)
class ByStage:
    """Both panels' measured data — F1 triple + escalation funnel."""

    stages: list[tuple[str, float]]
    funnel: Funnel


def _funnel_from_form(form: object) -> tuple[int, int, int, int]:
    """Bucket one form's scorable cells: ``(tier1, tier2, tier3a, blank)``.

    A cell with no value is ``blank`` (genuinely absent on the source
    form, a true negative). A populated cell is attributed to the tier
    that produced its value — ``ExtractedField.tier_used`` is ``1`` /
    ``2`` for Tier 1/2 and the lettered ``"3a"`` for Tier 3.
    """
    t1 = t2 = t3 = blank = 0
    for name in _SCORABLE:
        ef = getattr(form, name, None)
        value = getattr(ef, "value", None) if ef is not None else None
        if value is None:
            blank += 1
            continue
        tier = getattr(ef, "tier_used", None)
        if tier == 1:
            t1 += 1
        elif tier == 2:
            t2 += 1
        else:  # "3a" (or any Tier-3 label) — the forced-escalation residual
            t3 += 1
    return t1, t2, t3, blank


def compute_by_stage() -> ByStage:
    """Measure both panels in one cached pass over the test split.

    Cached replay only — deterministic and $0, so the committed SVG has a
    CI drift guard exactly like the F1-over-time chart. ``EVAL_LIVE`` is
    honored transparently by the providers if a caller sets it (the live
    regen path), but CI never does. The funnel reuses the *same* full
    ``(t1,t2,t3)`` RunRecord scored for the Tier-1+2+3 F1 point, so the
    two panels can never disagree about the cascade.
    """
    _, entries = load_manifest()
    test = [e for e in entries if e.split == "test"]
    t1, t2, t3 = build_cascade()

    rows: dict[str, list] = {label: [] for label in STAGE_LABELS}
    fn_t1 = fn_t2 = fn_t3 = fn_blank = 0
    conn12 = store.connect(":memory:")
    store.init_db(conn12)
    conn123 = store.connect(":memory:")
    store.init_db(conn123)
    try:
        for e in test:
            png = (CMS1500_VALIDATION_DIR / f"{e.doc_id}.png").read_bytes()
            truth = load_cms1500_ground_truth(CMS1500_VALIDATION_DIR / f"{e.doc_id}.json")

            form1, vert1, _ = _tier1_stage_form(png, (t1, t2, t3))
            rows["Tier 1"].append((vert1, score_form(form1, truth, _SCORABLE), 0.0))

            r12 = process_document(png, doc_id=e.doc_id, conn=conn12, providers=(t1, t2, t2))
            rows["Tier 1+2"].append((r12.vertical, score_form(r12.form, truth, _SCORABLE), 0.0))

            r123 = process_document(png, doc_id=e.doc_id, conn=conn123, providers=(t1, t2, t3))
            rows["Tier 1+2+3"].append((r123.vertical, score_form(r123.form, truth, _SCORABLE), 0.0))
            d1, d2, d3, db = _funnel_from_form(r123.form)
            fn_t1 += d1
            fn_t2 += d2
            fn_t3 += d3
            fn_blank += db
    finally:
        conn12.close()
        conn123.close()

    stages = [(label, aggregate(rows[label]).f1) for label in STAGE_LABELS]
    return ByStage(
        stages=stages,
        funnel=Funnel(tier1=fn_t1, tier2=fn_t2, tier3a=fn_t3, blank=fn_blank),
    )


# --- SVG (dependency-free, deterministic — sibling of evals/chart.py) -------


def _esc(text: str) -> str:
    """XML-escape text-node content (the mechanism note carries a literal
    ``<`` in ``confidence < 0.80``, which must not break well-formedness)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_W = 720
_M_L, _M_R = 70, 30  # left / right plot margins (shared by both panels)

# Top panel — escalation funnel. Axis 0 … total scorable cells; the
# genuinely-blank remainder is a stacked gray cap on the final bar (an
# explicit remainder, never a thin full-width band the value labels
# collide with). The funnel leads — it is the monotone coverage story;
# the F1 panel below is the honest non-monotone counterpoint.
_F_TITLE_Y = 28
_F_TOP = 52  # y for count == total (top of plot)
_F_BOT = 248  # zero baseline
_F_CAP_Y = 296  # caption block start

# Bottom panel — F1 by cumulative stage.
_S_TITLE_Y = 404
_S_TOP = 428  # F1 = 1.0 gridline
_S_BOT = 628  # F1 = 0.0 baseline
_S_NOTE_Y = 674  # mechanism annotation block start

_H = 778

#: The mechanism, wrapped to fit — Mark's ask: the logic lives in the graph.
_MECHANISM = (
    "Tier 3 only re-extracts fields that escalated (confidence < 0.80).",
    "The locked 0.5-confidence heuristic on coerced scalars forces every",
    "date field to escalate even when Tier 2 had it right — and the",
    "Q4_K_M 32B re-extracts those dates worse than the 7B, overwriting",
    "correct values (29/31 changed fields: correct → wrong, ~all dates).",
    "Shipped honest: motivates the local Qwen3-VL Tier-3b upgrade.",
)

#: Top-panel caption — the funnel reading, honest about Tier 1's tiny share.
_FUNNEL_CAPTION = (
    "Cells resolved by tier ≤ N (cumulative). Tier 3 only ever finalizes the",
    "residual the earlier tiers couldn't clear (the forced-escalated dates), so",
    "coverage still climbs to 100% of populated cells even though its slice F1 is",
    "worst. Tier 1's share is a tiny 2 cells — consistent with its 0.34 F1, not",
    "dressed up. The 123 genuinely-blank cells are an explicit remainder.",
)


def _stage_y(f1: float) -> float:
    return _S_BOT - f1 * (_S_BOT - _S_TOP)


def render_by_stage_svg(result: ByStage, *, seed_version: str) -> str:
    """Render both panels to one deterministic SVG string.

    Top: the escalation funnel — cumulative cells resolved by tier ≤ N
    against the total scorable cells, the genuinely-blank remainder drawn
    explicitly as a gray cap. Bottom: the 3 cumulative-tier F1 bars (the
    Tier-1+2+3 regression tinted) with the embedded mechanism annotation.
    The monotone coverage story leads; the honest non-monotone F1
    counterpoint follows. No timestamp / git SHA — committed-artifact
    stability.
    """
    stages = result.stages
    if not stages:
        raise ValueError("cannot chart an empty stage list")
    funnel = result.funnel
    n = len(stages)
    plot_l, plot_r = _M_L, _W - _M_R
    plot_w = plot_r - plot_l
    slot = plot_w / n
    bar_w = slot * 0.5

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" font-family="sans-serif">',
        f'<rect width="{_W}" height="{_H}" fill="#ffffff"/>',
    ]

    # ---- Top panel: escalation funnel -----------------------------------
    parts.append(
        f'<text x="{_W / 2}" y="{_F_TITLE_Y}" text-anchor="middle" '
        f'font-size="18" font-weight="bold">Escalation funnel — cells '
        f"resolved by tier ≤ N</text>"
    )
    populated = funnel.populated
    total = funnel.total
    span = _F_BOT - _F_TOP

    def _funnel_y(count: int) -> float:
        # Axis 0 … total scorable cells (populated + genuinely blank).
        return _F_BOT - (count / total) * span if total else _F_BOT

    y_pop = _funnel_y(populated)  # the "100% of populated" reference height
    parts.append(
        f'<line x1="{plot_l}" y1="{_F_TOP}" x2="{plot_l}" y2="{_F_BOT}" '
        f'stroke="#333" stroke-width="1.5"/>'
    )
    parts.append(
        f'<line x1="{plot_l}" y1="{_F_BOT}" x2="{plot_r}" y2="{_F_BOT}" '
        f'stroke="#333" stroke-width="1.5"/>'
    )
    # Faint reference line at 100%-of-populated; the blank remainder lives
    # above it and is drawn explicitly as a cap on the final bar.
    parts.append(
        f'<line x1="{plot_l}" y1="{y_pop:.1f}" x2="{plot_r}" y2="{y_pop:.1f}" '
        f'stroke="#bbbbbb" stroke-width="1" stroke-dasharray="4 3"/>'
    )
    parts.append(
        f'<text x="{plot_l + 4}" y="{y_pop - 6:.1f}" font-size="11" '
        f'fill="#888">100% of populated ({populated} cells)</text>'
    )
    for i, (label, cum) in enumerate(funnel.cumulative):
        cx = plot_l + slot * (i + 0.5)
        x = cx - bar_w / 2
        y = _funnel_y(cum)
        h = _F_BOT - y
        pct = (cum / populated * 100.0) if populated else 0.0
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{h:.1f}" fill="#2ca02c"/>'
        )
        # Last bar carries the genuinely-blank remainder as an explicit
        # gray cap (981 resolved + 123 blank = 1104 total) — never hidden.
        if i == len(funnel.cumulative) - 1 and funnel.blank:
            cap_h = y - _F_TOP
            parts.append(
                f'<rect x="{x:.1f}" y="{_F_TOP:.1f}" width="{bar_w:.1f}" '
                f'height="{cap_h:.1f}" fill="#d9d9d9"/>'
            )
            parts.append(
                f'<text x="{cx:.1f}" y="{(_F_TOP + y) / 2 + 4:.1f}" '
                f'text-anchor="middle" font-size="11" fill="#777">'
                f"{funnel.blank} blank</text>"
            )
        # Value label inside a tall bar (white), else above a short one.
        if h > 30:
            parts.append(
                f'<text x="{cx:.1f}" y="{y + 18:.1f}" text-anchor="middle" '
                f'font-size="13" font-weight="bold" fill="#ffffff">'
                f"{cum} ({pct:.1f}%)</text>"
            )
        else:
            parts.append(
                f'<text x="{cx:.1f}" y="{y - 8:.1f}" text-anchor="middle" '
                f'font-size="13" font-weight="bold" fill="#2ca02c">'
                f"{cum} ({pct:.1f}%)</text>"
            )
        parts.append(
            f'<text x="{cx:.1f}" y="{_F_BOT + 22:.1f}" text-anchor="middle" '
            f'font-size="13" fill="#333">{_esc(label)}</text>'
        )
    f_mid = (_F_TOP + _F_BOT) / 2
    parts.append(
        f'<text x="18" y="{f_mid:.1f}" text-anchor="middle" font-size="13" '
        f'fill="#333" transform="rotate(-90 18 {f_mid:.1f})">cells resolved</text>'
    )
    for j, line in enumerate(_FUNNEL_CAPTION):
        parts.append(
            f'<text x="{plot_l}" y="{_F_CAP_Y + j * 16:.1f}" '
            f'font-size="11.5" fill="#555">{_esc(line)}</text>'
        )

    # ---- Bottom panel: F1 by cumulative stage ---------------------------
    parts.append(
        f'<text x="{_W / 2}" y="{_S_TITLE_Y}" text-anchor="middle" '
        f'font-size="18" font-weight="bold">F1 by cumulative cascade '
        f"stage (same run)</text>"
    )
    for t in range(6):
        f = t / 5.0
        y = _stage_y(f)
        parts.append(
            f'<line x1="{plot_l}" y1="{y:.1f}" x2="{plot_r}" y2="{y:.1f}" '
            f'stroke="#e5e5e5" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{plot_l - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#555">{f:.1f}</text>'
        )
    parts.append(
        f'<line x1="{plot_l}" y1="{_S_TOP}" x2="{plot_l}" y2="{_S_BOT}" '
        f'stroke="#333" stroke-width="1.5"/>'
    )
    parts.append(
        f'<line x1="{plot_l}" y1="{_S_BOT}" x2="{plot_r}" y2="{_S_BOT}" '
        f'stroke="#333" stroke-width="1.5"/>'
    )
    for i, (label, f1) in enumerate(stages):
        cx = plot_l + slot * (i + 0.5)
        x = cx - bar_w / 2
        y = _stage_y(f1)
        h = _S_BOT - y
        regressed = i > 0 and f1 < stages[i - 1][1]
        fill = "#d62728" if regressed else "#1f77b4"
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
            f'height="{h:.1f}" fill="{fill}"/>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{y - 8:.1f}" text-anchor="middle" '
            f'font-size="13" font-weight="bold" fill="{fill}">{f1:.3f}</text>'
        )
        parts.append(
            f'<text x="{cx:.1f}" y="{_S_BOT + 22:.1f}" text-anchor="middle" '
            f'font-size="13" fill="#333">{_esc(label)}</text>'
        )
    s_mid = (_S_TOP + _S_BOT) / 2
    parts.append(
        f'<text x="18" y="{s_mid:.1f}" text-anchor="middle" font-size="13" '
        f'fill="#333" transform="rotate(-90 18 {s_mid:.1f})">F1 (micro)</text>'
    )
    for j, line in enumerate(_MECHANISM):
        parts.append(
            f'<text x="{plot_l}" y="{_S_NOTE_Y + j * 16:.1f}" '
            f'font-size="11.5" fill="#555">{_esc(line)}</text>'
        )

    parts.append(
        f'<text x="{plot_r}" y="{_H - 8}" text-anchor="end" '
        f'font-size="11" fill="#999">alias_table_seed v{seed_version}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_by_stage_chart(
    result: ByStage,
    *,
    seed_version: str,
    path: Path | str = BY_STAGE_CHART_PATH,
) -> None:
    """Write the deterministic two-panel by-stage SVG to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_by_stage_svg(result, seed_version=seed_version), encoding="utf-8")
