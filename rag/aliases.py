"""Live correction-driven alias overlay (the production half of the loop).

The committed ``alias_table_seed.json`` is **v1.0.0 and frozen**: it is the
canonical-priority corpus the progressive-partition F1 chart is plotted
from, and ``docs/eval-methodology.md`` (L117-123) explicitly forbids
in-place edits to it — a real reseed is a deliberate v2.0.0 bump, not a
side effect of the demo loop.

So the live reviewer-correction loop never touches the seed. New
recognized phrasings are appended to a **gitignored runtime overlay**
(``data/corrections_aliases.json`` — same ``data/`` runtime-state
convention as ``data/v1.db``). The two alias consumers union the overlay on
top of the seed:

- ``cascade.router.build_distinctive_vocabulary`` (Stage 1 vocab)
- ``cascade.providers.tier1_paddleocr_local._load_alias_table_raw``
  (Tier 1's layout-to-fields alias map)

Both are ``lru_cache``'d; :func:`invalidate_alias_caches` clears them after
the overlay changes so the next extraction sees the new alias.

**The progressive-partition F1 sweep must not see the overlay.** That sweep
(``evals/alias_partition.active_alias_batch``) tells the *frozen offline
analogue* of this loop — feeding a growing slice of the v1.0.0 seed and
plotting Tier-1 F1. If runtime corrections leaked into it the portfolio
chart would silently drift. So ``active_alias_batch`` enters
:func:`suppress_overlay`; while suppressed :func:`overlay_records` returns
``[]`` and the loaders see the seed alone. The live loop and the offline
analogue therefore share one mechanism (alias growth → Tier 1 + router)
but the measured artifact stays honest.

This module imports nothing from ``cascade`` at module load (the loaders
import *it* lazily); :func:`invalidate_alias_caches` reaches back into the
cascade caches only when called.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

#: Repo root, resolved from this file so paths work regardless of cwd.
_REPO_ROOT = Path(__file__).resolve().parent.parent

#: Gitignored runtime overlay. ``data/`` is fully gitignored (see
#: ``.gitignore``) — the overlay is runtime state like ``data/v1.db``, never
#: committed, never a portfolio artifact. Module-level so tests can repoint
#: it the same way they repoint ``ALIAS_TABLE_PATH``.
OVERLAY_PATH = _REPO_ROOT / "data" / "corrections_aliases.json"

#: Overlay schema version. Independent of the seed's ``version`` — the
#: overlay is never a seed reseed, so it carries its own marker.
OVERLAY_VERSION = "1"

#: When True, :func:`overlay_records` returns ``[]`` regardless of the file.
#: Toggled only by :func:`suppress_overlay` (the frozen-chart sweep).
_suppressed = False


@contextmanager
def suppress_overlay() -> Iterator[None]:
    """Make the alias loaders see the seed alone for the duration.

    Used by the progressive-partition F1 sweep so runtime corrections never
    contaminate the frozen portfolio chart. Re-entrant-safe: nested
    suppression restores to *suppressed* on the inner exit, not to active.
    """
    global _suppressed
    prev = _suppressed
    _suppressed = True
    try:
        yield
    finally:
        _suppressed = prev


@contextmanager
def temporary_overlay(path: Path | str) -> Iterator[Path]:
    """Repoint :data:`OVERLAY_PATH` at ``path`` for the block, then restore.

    The same path-repoint seam the test suite uses for ``ALIAS_TABLE_PATH``.
    The demo wraps its correction flow in this so a demo run never mutates
    the real ``data/corrections_aliases.json`` (mirrors how the demo runs
    the cascade into a throwaway temp DB, never ``data/v1.db``).
    """
    global OVERLAY_PATH
    prev = OVERLAY_PATH
    OVERLAY_PATH = Path(path)
    try:
        yield OVERLAY_PATH
    finally:
        OVERLAY_PATH = prev


def _read_overlay() -> dict[str, Any]:
    """Parse the overlay file, or an empty skeleton if absent/blank."""
    try:
        text = OVERLAY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": OVERLAY_VERSION, "records": []}
    if not text.strip():
        return {"version": OVERLAY_VERSION, "records": []}
    data = json.loads(text)
    data.setdefault("records", [])
    return data


def overlay_records() -> list[dict[str, Any]]:
    """Correction-derived alias records to union onto the seed ``fields``.

    Returns ``[]`` when suppressed (the frozen-chart sweep) or when no
    overlay exists yet. Each record mirrors a seed ``fields`` entry's shape
    for the keys the loaders read: ``canonical_name``, ``vertical``,
    ``aliases``. The Tier 1 alias map dedups against the seed's aliases by
    value, so an overlay record sharing a ``canonical_name`` with a seed
    record simply extends that field's recognized phrasings.
    """
    if _suppressed:
        return []
    return list(_read_overlay().get("records", []))


def _atomic_write(payload: dict[str, Any]) -> None:
    """Write the overlay atomically (temp file + rename) under ``data/``."""
    OVERLAY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".corrections_aliases.", suffix=".json", dir=str(OVERLAY_PATH.parent)
    )
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        tmp.replace(OVERLAY_PATH)
    finally:
        tmp.unlink(missing_ok=True)


def append_correction_alias(canonical_name: str, vertical: str, alias: str) -> bool:
    """Append a learned phrasing to the overlay record for a field.

    Returns ``True`` iff the alias was newly added (``False`` when it was
    blank or already recognized — case-insensitive, whitespace-trimmed,
    matched against *both* the overlay and the committed seed so the loop
    never re-learns a phrasing the seed already covered). Records are keyed
    by ``(canonical_name, vertical)``. The caller is responsible for
    :func:`invalidate_alias_caches` after a ``True`` so the next extraction
    sees it (``record_correction`` does this).
    """
    norm = alias.strip()
    if not norm:
        return False
    norm_key = norm.upper()
    if norm_key in _seed_aliases_for(canonical_name, vertical):
        return False

    data = _read_overlay()
    records: list[dict[str, Any]] = data.setdefault("records", [])
    for rec in records:
        if rec["canonical_name"] == canonical_name and rec["vertical"] == vertical:
            existing = {a.strip().upper() for a in rec["aliases"]}
            if norm_key in existing:
                return False
            rec["aliases"].append(norm)
            _atomic_write(data)
            return True

    records.append({"canonical_name": canonical_name, "vertical": vertical, "aliases": [norm]})
    data["version"] = OVERLAY_VERSION
    _atomic_write(data)
    return True


def _seed_aliases_for(canonical_name: str, vertical: str) -> set[str]:
    """Uppercased aliases the *committed seed* already has for this field.

    Used so a correction whose phrasing the seed already covers is a no-op
    (it would otherwise add a redundant overlay row). Reads the seed
    directly via the router's path constant so it tracks any test repoint.
    """
    from cascade.router import ALIAS_TABLE_PATH

    try:
        fields = json.loads(ALIAS_TABLE_PATH.read_text(encoding="utf-8"))["fields"]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        return set()
    out: set[str] = set()
    for rec in fields:
        if rec["canonical_name"] == canonical_name and rec["vertical"] == vertical:
            out.update(a.strip().upper() for a in rec["aliases"] if a.strip())
    return out


def invalidate_alias_caches() -> None:
    """Clear both alias loaders' ``lru_cache``s after an overlay change.

    Imported lazily so this module stays free of a ``cascade`` import at
    load time (the loaders import *this* module lazily — clearing here
    would otherwise be a cycle). Mirrors
    ``evals.alias_partition._clear_caches`` exactly; the two are the only
    places that reach into these caches.
    """
    from cascade import router
    from cascade.providers import tier1_paddleocr_local as tier1_mod

    router.build_distinctive_vocabulary.cache_clear()
    tier1_mod._load_alias_table_raw.cache_clear()
