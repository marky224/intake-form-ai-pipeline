"""Progressive alias-table partition — the F1-over-time mechanism.

``alias_table_seed.json`` is the cascade's recognized-phrasing vocabulary.
The self-improvement story (``docs/eval-methodology.md``) is told by feeding
the cascade a *growing* slice of it and watching F1 climb:

    Batch N includes positions 0 through N-1 of every record's ``aliases``
    array. Records with fewer aliases than N contribute their full list.

Position 0 is the canonical/authoritative phrasing; later positions are
curator-ranked variants. Batch 1 is canonical-only; the last batch is the
full seed. This is the *stable contract* — the exact alias counts drift as
the seed evolves; the position rule does not.

Both alias consumers read a module-level ``ALIAS_TABLE_PATH`` through an
``lru_cache``'d loader (the locked "built once per process" behavior):

- ``cascade.router.build_distinctive_vocabulary`` (Stage 1 vocab)
- ``cascade.providers.tier1_paddleocr_local._load_alias_table_raw``
  (Tier 1's layout-to-fields alias map)

The Tier 1 PaddleOCR *OCR* output is form-agnostic and replay-cached, so it
is unchanged across batches; the alias-driven *parse* of that cached output
and the router vocab both rebuild per batch. That is why the progressive
mechanism genuinely moves F1 on cached replay at $0.

``active_alias_batch`` is a context manager: it writes the partitioned seed
to a temp file, repoints both ``ALIAS_TABLE_PATH`` constants at it, clears
both caches on entry, and restores + clears on exit. Repointing a module
constant for the duration is the same seam the test suite already uses; the
harness owns the lifetime so it is honest, not a monkeypatch escape hatch.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from cascade import router
from cascade.providers import tier1_paddleocr_local as tier1_mod

#: The canonical, full seed. Repo root next to this package.
ALIAS_SEED_PATH = Path(__file__).resolve().parent.parent / "alias_table_seed.json"


def load_seed(seed_path: Path | str = ALIAS_SEED_PATH) -> dict[str, Any]:
    """Load the full seed JSON (``version`` + ``fields`` + metadata)."""
    return json.loads(Path(seed_path).read_text(encoding="utf-8"))


def batch_count(seed: dict[str, Any]) -> int:
    """Natural batch count = the longest ``aliases`` list in the seed.

    Batch ``batch_count`` is the full seed (every record has contributed
    all of its aliases by then).
    """
    return max((len(r["aliases"]) for r in seed["fields"]), default=0)


def partition_seed(seed: dict[str, Any], batch_n: int) -> dict[str, Any]:
    """Return a copy of ``seed`` truncated to alias positions ``0..batch_n-1``.

    ``batch_n`` is 1-based (Batch 1 = canonical phrasing only). A record
    with fewer than ``batch_n`` aliases keeps its full list. The top-level
    seed metadata (``version`` etc.) is preserved verbatim so the fixtures
    manifest still records the originating seed version.
    """
    if batch_n < 1:
        raise ValueError(f"batch_n is 1-based; got {batch_n}")
    out = deepcopy(seed)
    for record in out["fields"]:
        record["aliases"] = record["aliases"][:batch_n]
    return out


def _clear_caches() -> None:
    router.build_distinctive_vocabulary.cache_clear()
    tier1_mod._load_alias_table_raw.cache_clear()


@contextmanager
def active_alias_batch(
    batch_n: int,
    seed_path: Path | str = ALIAS_SEED_PATH,
) -> Iterator[int]:
    """Make Batch ``batch_n`` the active alias vocabulary for the block.

    Repoints ``router.ALIAS_TABLE_PATH`` and
    ``tier1_paddleocr_local.ALIAS_TABLE_PATH`` at a temp partitioned seed,
    clears both alias caches on enter and exit, and always restores the
    original paths + temp file even if the block raises. Yields ``batch_n``
    for convenience.
    """
    partitioned = partition_seed(load_seed(seed_path), batch_n)
    orig_router = router.ALIAS_TABLE_PATH
    orig_tier1 = tier1_mod.ALIAS_TABLE_PATH

    # mkstemp (not NamedTemporaryFile): the file must outlive its handle —
    # the alias loaders reopen it by path inside the block — so we close
    # immediately and unlink in finally.
    fd, name = tempfile.mkstemp(suffix=f".batch{batch_n}.json")
    tmp_path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(partitioned, fh)
        router.ALIAS_TABLE_PATH = tmp_path
        tier1_mod.ALIAS_TABLE_PATH = tmp_path
        _clear_caches()
        yield batch_n
    finally:
        router.ALIAS_TABLE_PATH = orig_router
        tier1_mod.ALIAS_TABLE_PATH = orig_tier1
        _clear_caches()
        tmp_path.unlink(missing_ok=True)
