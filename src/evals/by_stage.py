"""By-stage cumulative F1 + escalation funnel — the honest tier-ablation.

A two-panel portfolio artifact, measured on the 92-doc ``test`` split via
the committed replay cache ($0, deterministic — CI reproduces it):

**Panel 1 — F1 by cumulative stage.** Three points:

- **Tier 1** — the orchestrator's pre-escalation form (Tier 1 extract →
  route → re-parse), exactly ``harness._tier1_stage_form``.
- **Tier 1+2** — the cascade with the Tier-3 slot filled by Tier 2, so
  the second read is the 7B again and keeps Tier 2's value (no 32B). A
  faithful "Tier 3 disabled" ceiling without touching the orchestrator.
- **Tier 1+2+3** — the real cascade.

Result since 2026-10-26: ``0.868 → 0.806 → 0.904`` — not monotone. Tier 1
reads the CMS-1500 table (colspan grid, box numbers stripped, packed boxes
split, MM/DD/YYYY dates) and finalizes a date, ZIP or state that passes its
validator under a label naming the field specifically; every other
Tier-1 value goes to the 7B Tier 2, which often
replaces a correct Tier-1 reading (mostly Synthea's digit-suffixed names),
so Tier 1+2 dips below Tier 1. Tier 3 reads only the fields Tier 1 covers
where Tier 1 and Tier 2 disagree or Tier 1 read nothing (3.0 per document
on this split, against every Tier-2 field before): Tier 3 matching Tier 1
settles the field; Tier 3 matching Tier 2 settles it only when Tier 1 has
no valid reading from a specific label (against a different such Tier-1
reading it parks, the commonest parking reason); anything else parks, and a validated Tier-2
date is never overwritten. Read the +0.098 carefully: most of it is
Synthea's digit-suffixed names. Of the 107 first/last-name cells Tier 3
turns from wrong to right, 74 are names Tier 2 read without their digit
suffix, 6 are Tier-2 blanks and 27 are swaps and other misreads; it turns
no right name wrong. Street addresses, where Tier 3 drops the apartment
suffix, now lose 5 cells (37 when Tier 3 read every field), kept as
measured. The rules were chosen on dev with test results printed beside
it, after earlier test-split inspection, and the 2026-10-25/26 refinements
answered a review that named test documents — see
``docs/eval-methodology.md``.
Until 2026-10-24 Tier 1 settled 2 cells and scored 0.340 and Tier 3 read
every Tier-2 field (``0.340 → 0.791 → 0.853``); until 2026-10-16 Tier 3
regressed (``0.791 → 0.765``): a type heuristic (strings 1.0, dates 0.5)
sent every date to Tier 3, and the quantized 32B overwrote 29 correct
dates.

**Panel 2 — escalation funnel.** The same full ``(t1,t2,t3)`` cascade
run, counting each scorable cell by the tier whose value the cascade
kept. Cumulative cells resolved by tier ≤ N rises to 100% of the
populated cells (250 → 861 → 989 of 989). Tier 1's 250 cells are the
validated dates of birth, ZIPs and states. Tier 3's value is kept in 128 cells: 100
where it matched Tier 1, 26 free-text cells where Tier 2's value had no
validator to keep it, and 2 cells Tier 2 left blank. The 115 empty cells
are shown as an explicit remainder, split by ground truth: 92 **missed**
(the form has a value, the cascade produced none — the ``signature``
field, which no tier attempts) and 23 **true negatives** (absent on the
source form).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from _paths import repo_root
from cascade import store
from cascade.orchestrator import build_cascade, process_document, same_reading
from cascade.validators import validate
from evals.ground_truth import FIELD_KIND, GroundTruth, load_cms1500_ground_truth
from evals.harness import _tier1_stage_form
from evals.manifest import CMS1500_VALIDATION_DIR, CORPUS_RENDER_DIR, load_manifest
from evals.metrics import aggregate, score_form

#: Committed by-stage chart artifact under repo-root ``docs/assets/`` (docs/
#: stays at the repo root, not under src/ — see memory project_src_layout).
BY_STAGE_CHART_PATH = repo_root() / "docs" / "assets" / "f1-by-stage.svg"

_SCORABLE = list(FIELD_KIND)
STAGE_LABELS = ("Tier 1", "Tier 1+2", "Tier 1+2+3")


@dataclass(frozen=True)
class Funnel:
    """Scorable-cell escalation funnel from the full cascade run.

    ``tier1``/``tier2``/``tier3a`` count populated cells by the tier that
    finally produced their value. Empty cells split by ground truth:
    ``missed`` has a value on the form but none extracted (unattempted or
    confidently blank — a false negative in ``score_form``);
    ``true_negative`` is empty on the form too. Both are drawn as an
    explicit remainder rather than hidden. The cascade is monotone *here*:
    every populated cell is resolved by some tier, so the cumulative-by-tier
    curve rises to 100% of ``populated`` even where the F1 panel dips.

    The ``tier3a_*`` counts say why Tier 3's value was kept in each
    ``tier3a`` cell (they sum to ``tier3a``): it matched Tier 1's reading;
    otherwise Tier 2's value was free text with no validator, Tier 2 read
    nothing, or Tier 2's value failed its validator.
    """

    tier1: int
    tier2: int
    tier3a: int
    missed: int
    true_negative: int
    tier3a_matched_tier1: int = 0
    tier3a_free_text: int = 0
    tier3a_tier2_blank: int = 0
    tier3a_failed_validator: int = 0

    @property
    def total(self) -> int:
        """All scorable cells (populated + missed + true negatives)."""
        return self.tier1 + self.tier2 + self.tier3a + self.missed + self.true_negative

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


def _funnel_from_form(form: object, truth: GroundTruth) -> tuple[int, int, int, int, int]:
    """Bucket one form's scorable cells: ``(tier1, tier2, tier3a, missed, true_negative)``.

    A cell with no value is ``missed`` when ground truth has a value for it
    (the cascade missed it) and ``true_negative`` when it doesn't (absent on
    the source form). A populated cell is attributed to the tier that
    produced its value — ``ExtractedField.tier_used`` is ``1`` / ``2`` for
    Tier 1/2 and the lettered ``"3a"`` for Tier 3.
    """
    t1 = t2 = t3 = missed = true_negative = 0
    for name in _SCORABLE:
        ef = getattr(form, name, None)
        value = getattr(ef, "value", None) if ef is not None else None
        if value is None:
            if truth.get(name) is not None:
                missed += 1
            else:
                true_negative += 1
            continue
        tier = getattr(ef, "tier_used", None)
        if tier == 1:
            t1 += 1
        elif tier == 2:
            t2 += 1
        else:  # "3a" — Tier 3's reading was kept (a disagreement it won)
            t3 += 1
    return t1, t2, t3, missed, true_negative


def _tier3_reasons(final: object, first: object, tier1: object) -> tuple[int, int, int, int]:
    """Why Tier 3's value was kept in each ``"3a"`` cell of ``final``:
    ``(matched_tier1, free_text, tier2_blank, failed_validator)``. ``first``
    is the Tier-1+2 form, i.e. Tier 2's reading of the same document, and
    ``tier1`` the Tier-1-stage form.
    """
    matched = free_text = tier2_blank = failed = 0
    for name in _SCORABLE:
        ef = getattr(final, name, None)
        if ef is None or ef.value is None or ef.tier_used != "3a":
            continue
        tier1_value = getattr(getattr(tier1, name, None), "value", None)
        tier2_value = getattr(getattr(first, name, None), "value", None)
        if same_reading(ef.value, tier1_value):
            matched += 1
        elif tier2_value is None:
            tier2_blank += 1
        elif validate(name, tier2_value) is None:
            free_text += 1
        else:
            failed += 1
    return matched, free_text, tier2_blank, failed


def compute_by_stage(split: str = "test", image_dir: Path | str | None = None) -> ByStage:
    """Measure both panels in one cached pass over a manifest split.

    ``split`` defaults to ``test``, the committed measurement. Only the
    ``test`` images and replay fixtures are committed: for ``dev`` (the
    held-out confirmation run) the images come from the gitignored local
    corpus (``CORPUS_RENDER_DIR``), and the run is cache-through — leave
    ``EVAL_LIVE`` unset, keep the models up, and each provider reads a dev
    document live once on its first cache miss and saves it, so all three
    stages are scored from the same reading. ``image_dir`` overrides where
    the ``<doc_id>.png`` / ``<doc_id>.json`` pairs are read.

    On the committed test split this is cached replay — deterministic and
    $0, so the committed SVG has a CI drift guard. ``EVAL_LIVE`` is honored
    transparently by the providers if a caller sets it (it skips the cache
    on every call), but CI never does. The funnel reuses the *same* full
    ``(t1,t2,t3)`` RunRecord scored for the Tier-1+2+3 F1 point, so the
    two panels can never disagree about the cascade.
    """
    if image_dir is None:
        image_dir = CMS1500_VALIDATION_DIR if split == "test" else CORPUS_RENDER_DIR
    image_dir = Path(image_dir)
    _, entries = load_manifest()
    docs = [e for e in entries if e.split == split]
    if not docs:
        raise ValueError(f"no {split!r} documents in the manifest")
    missing = [e.doc_id for e in docs if not (image_dir / f"{e.doc_id}.png").is_file()]
    if missing:
        raise FileNotFoundError(
            f"{len(missing)} of {len(docs)} {split!r} documents have no PNG in {image_dir} "
            "(dev images live in the gitignored local corpus — run "
            "`just synthetic-data-render-500` first)"
        )
    t1, t2, t3 = build_cascade()

    rows: dict[str, list] = {label: [] for label in STAGE_LABELS}
    fn_t1 = fn_t2 = fn_t3 = fn_missed = fn_tn = 0
    why_t1 = why_free = why_blank = why_failed = 0
    conn12 = store.connect(":memory:")
    store.init_db(conn12)
    conn123 = store.connect(":memory:")
    store.init_db(conn123)
    try:
        for e in docs:
            png = (image_dir / f"{e.doc_id}.png").read_bytes()
            truth = load_cms1500_ground_truth(image_dir / f"{e.doc_id}.json")

            form1, vert1, _ = _tier1_stage_form(png, (t1, t2, t3))
            rows["Tier 1"].append((vert1, score_form(form1, truth, _SCORABLE), 0.0))

            r12 = process_document(png, doc_id=e.doc_id, conn=conn12, providers=(t1, t2, t2))
            rows["Tier 1+2"].append((r12.vertical, score_form(r12.form, truth, _SCORABLE), 0.0))

            r123 = process_document(png, doc_id=e.doc_id, conn=conn123, providers=(t1, t2, t3))
            rows["Tier 1+2+3"].append((r123.vertical, score_form(r123.form, truth, _SCORABLE), 0.0))
            d1, d2, d3, dm, dn = _funnel_from_form(r123.form, truth)
            fn_t1 += d1
            fn_t2 += d2
            fn_t3 += d3
            fn_missed += dm
            fn_tn += dn
            w_t1, w_free, w_blank, w_failed = _tier3_reasons(r123.form, r12.form, form1)
            why_t1 += w_t1
            why_free += w_free
            why_blank += w_blank
            why_failed += w_failed
    finally:
        conn12.close()
        conn123.close()

    stages = [(label, aggregate(rows[label]).f1) for label in STAGE_LABELS]
    return ByStage(
        stages=stages,
        funnel=Funnel(
            tier1=fn_t1,
            tier2=fn_t2,
            tier3a=fn_t3,
            missed=fn_missed,
            true_negative=fn_tn,
            tier3a_matched_tier1=why_t1,
            tier3a_free_text=why_free,
            tier3a_tier2_blank=why_blank,
            tier3a_failed_validator=why_failed,
        ),
    )


# --- SVG (dependency-free, deterministic — sibling of evals/chart.py) -------


def _esc(text: str) -> str:
    """XML-escape text-node content so a ``<``, ``>`` or ``&`` in a caption
    or label can't break well-formedness."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_W = 720
_M_L, _M_R = 70, 30  # left / right plot margins (shared by both panels)

# Top panel — escalation funnel. Axis 0 … total scorable cells; the empty
# remainder is two stacked caps on the final bar — missed (magenta) under
# true negatives (gray) — an explicit remainder, never a thin full-width
# band the value labels collide with. The funnel leads (coverage); the F1
# panel below shows accuracy by stage, exactly as measured.
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
    "Tier 1 finalizes a valid date, ZIP or state under a specific label; the 7B",
    "re-reads the rest and replaces many right Tier-1 reads (Tier 1+2 dips).",
    "Tier 3 reads only fields where Tier 1 and Tier 2 disagree or Tier 1 read",
    "nothing. Tier 3 = Tier 1 settles; Tier 3 = Tier 2 only if Tier 1 has no",
    "specific valid reading (else it parks, commonest); anything else parks.",
    "Most of Tier 3's gain is Synthea's number-suffixed names. One template;",
    "rules chosen after test results were seen. Live Tier 3: ~6 s/doc.",
)

#: "Missed" cap color — validated against the green bar and the gray cap
#: (deutan ΔE 16.9, normal ΔE 32.5); the gray cap is a neutral remainder.
_MISSED_FILL = "#a05195"


def _funnel_caption(funnel: Funnel) -> tuple[str, ...]:
    """Top-panel caption — the funnel reading, honest about which cells each
    tier settles and about the empty cells (counts formatted from ``funnel``)."""
    return (
        "Cells resolved by tier ≤ N (cumulative), by the tier whose value was kept. Tier 1's",
        f"{funnel.tier1} cells are valid dates, ZIPs and states under specific labels. Tier 3 "
        "reads only",
        "fields where Tier 1 and Tier 2 disagree or Tier 1 read nothing; its value is kept in",
        f"{funnel.tier3a} cells ({funnel.tier3a_matched_tier1} matching Tier 1, "
        f"{funnel.tier3a_free_text} free text, {funnel.tier3a_tier2_blank} Tier-2 blanks, "
        f"{funnel.tier3a_failed_validator} failed validator).",
        f"Empty cells, shown explicitly: {funnel.missed} missed (value on the form, none "
        f"extracted) + {funnel.true_negative} blank on the form.",
    )


def _stage_y(f1: float) -> float:
    return _S_BOT - f1 * (_S_BOT - _S_TOP)


def render_by_stage_svg(result: ByStage, *, seed_version: str) -> str:
    """Render both panels to one deterministic SVG string.

    Top: the escalation funnel — cumulative cells resolved by tier ≤ N
    against the total scorable cells, the empty remainder drawn explicitly
    as missed + true-negative caps. Bottom: the 3 cumulative-tier F1 bars (a
    stage below its predecessor is tinted red) with the embedded mechanism
    annotation. No timestamp / git SHA — committed-artifact stability.
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
        # Axis 0 … total scorable cells (populated + missed + true negatives).
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
    # Faint reference line at 100%-of-populated; the empty remainder lives
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
        # Last bar carries the empty remainder as two explicit caps —
        # missed, then true negatives (populated + missed + true negatives
        # = total) — never hidden. Labels sit left of the bar in muted ink
        # (the caps are too thin to hold text); a 2px surface line
        # separates the stacked fills.
        if i == len(funnel.cumulative) - 1 and (funnel.missed or funnel.true_negative):
            y_missed = _funnel_y(cum + funnel.missed)
            for top, bottom, fill in (
                (y_missed, y, _MISSED_FILL),
                (_F_TOP, y_missed, "#d9d9d9"),
            ):
                parts.append(
                    f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                    f'height="{bottom - top:.1f}" fill="{fill}"/>'
                )
            for edge in (y, y_missed):
                parts.append(
                    f'<line x1="{x:.1f}" y1="{edge:.1f}" x2="{x + bar_w:.1f}" '
                    f'y2="{edge:.1f}" stroke="#ffffff" stroke-width="2"/>'
                )
            missed_y = (y_missed + y) / 2 + 4
            tn_y = min((_F_TOP + y_missed) / 2 + 4, missed_y - 12)
            for cap_y, cap_label in (
                (missed_y, f"{funnel.missed} missed"),
                (tn_y, f"{funnel.true_negative} blank on form"),
            ):
                parts.append(
                    f'<text x="{x - 6:.1f}" y="{cap_y:.1f}" text-anchor="end" '
                    f'font-size="11" fill="#777">{cap_label}</text>'
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
    for j, line in enumerate(_funnel_caption(funnel)):
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
