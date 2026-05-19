"""F1-over-time static SVG — the README's headline portfolio artifact.

Dependency-free, hand-rolled SVG (no matplotlib in the dependency tree for
one chart). Deterministic by construction: the committed
``docs/assets/f1-over-time.svg`` must produce a stable diff when
regenerated, so the SVG body embeds the seed version (the comparability
key per ``eval-methodology.md``) but **no** timestamp or git SHA — those
would churn the committed file on every run without adding signal.

The x-axis is the alias-partition batch index (Batch 1 = canonical phrasing
only … Batch N = the full seed). The curve climbing then plateauing is the
self-improvement narrative: more recognized phrasings → more Tier 1 hits →
higher F1, asymptoting as later batches add fewer marginal aliases.
"""

from __future__ import annotations

from pathlib import Path

from _paths import repo_root

#: Committed chart artifact under repo-root ``docs/assets/`` (docs/ stays at
#: the repo root, not under src/ — see memory project_src_layout).
CHART_PATH = repo_root() / "docs" / "assets" / "f1-over-time.svg"

_W, _H = 720, 420
_PAD_L, _PAD_R, _PAD_T, _PAD_B = 70, 30, 50, 60


def _x(i: int, n: int) -> float:
    if n <= 1:
        return _PAD_L
    return _PAD_L + (i / (n - 1)) * (_W - _PAD_L - _PAD_R)


def _y(f1: float) -> float:
    # F1 axis fixed 0..1 so charts across seed versions are comparable.
    return _H - _PAD_B - f1 * (_H - _PAD_T - _PAD_B)


def render_f1_over_time_svg(
    series: list[tuple[int, float]],
    *,
    seed_version: str,
) -> str:
    """Render ``[(batch_id, f1), ...]`` to a deterministic SVG string."""
    if not series:
        raise ValueError("cannot chart an empty series")
    series = sorted(series)
    n = len(series)

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_W}" height="{_H}" '
        f'viewBox="0 0 {_W} {_H}" font-family="sans-serif">',
        f'<rect width="{_W}" height="{_H}" fill="#ffffff"/>',
        f'<text x="{_W / 2}" y="28" text-anchor="middle" font-size="18" '
        f'font-weight="bold">F1 over progressive alias-table batches</text>',
    ]

    # Y gridlines + labels (0.0 … 1.0 by 0.2).
    for t in range(6):
        f = t / 5.0
        y = _y(f)
        parts.append(
            f'<line x1="{_PAD_L}" y1="{y:.1f}" x2="{_W - _PAD_R}" y2="{y:.1f}" '
            f'stroke="#e5e5e5" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_PAD_L - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="12" fill="#555">{f:.1f}</text>'
        )

    # Axes.
    parts.append(
        f'<line x1="{_PAD_L}" y1="{_PAD_T}" x2="{_PAD_L}" y2="{_H - _PAD_B}" '
        f'stroke="#333" stroke-width="1.5"/>'
    )
    parts.append(
        f'<line x1="{_PAD_L}" y1="{_H - _PAD_B}" x2="{_W - _PAD_R}" '
        f'y2="{_H - _PAD_B}" stroke="#333" stroke-width="1.5"/>'
    )

    # X labels (batch index).
    for i, (batch_id, _) in enumerate(series):
        x = _x(i, n)
        parts.append(
            f'<text x="{x:.1f}" y="{_H - _PAD_B + 22}" text-anchor="middle" '
            f'font-size="12" fill="#555">{batch_id}</text>'
        )
    parts.append(
        f'<text x="{(_PAD_L + _W - _PAD_R) / 2:.1f}" y="{_H - 14}" '
        f'text-anchor="middle" font-size="13" fill="#333">'
        f"Alias-table batch (1 = canonical only … {series[-1][0]} = full seed)</text>"
    )
    parts.append(
        f'<text x="18" y="{_H / 2:.1f}" text-anchor="middle" font-size="13" '
        f'fill="#333" transform="rotate(-90 18 {_H / 2:.1f})">F1 (micro)</text>'
    )

    # The F1 polyline + point markers.
    pts = " ".join(f"{_x(i, n):.1f},{_y(f1):.1f}" for i, (_, f1) in enumerate(series))
    parts.append(f'<polyline points="{pts}" fill="none" stroke="#1f77b4" stroke-width="2.5"/>')
    for i, (_, f1) in enumerate(series):
        x, y = _x(i, n), _y(f1)
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" fill="#1f77b4"/>')
        parts.append(
            f'<text x="{x:.1f}" y="{y - 10:.1f}" text-anchor="middle" '
            f'font-size="11" fill="#1f77b4">{f1:.3f}</text>'
        )

    parts.append(
        f'<text x="{_W - _PAD_R}" y="{_H - 14}" text-anchor="end" '
        f'font-size="11" fill="#999">alias_table_seed v{seed_version}</text>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def write_chart(
    series: list[tuple[int, float]],
    *,
    seed_version: str,
    path: Path | str = CHART_PATH,
) -> None:
    """Write the deterministic F1-over-time SVG to ``path``."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_f1_over_time_svg(series, seed_version=seed_version), encoding="utf-8")
