"""Single source of truth for repo-relative paths.

Introduced in the 2026-05-19 src-layout refactor (memory
``project_src_layout``). Before the move, modules computed "the repo root"
ad-hoc as ``Path(__file__).resolve().parent.parent`` because every package
sat one level below the root. With packages now under ``src/``, that
expression resolves to ``src/`` rather than the repo root, silently
breaking every cross-tree path (``alias_table_seed.json`` at root, etc.).

Two helpers, two distinct meanings:

* :func:`repo_root` — the true repository root, i.e. the parent of ``src/``.
  Use this for assets that **deliberately stay outside** ``src/``:
  ``alias_table_seed.json`` (canonical artifact contract).

* :func:`src_root` — the ``src/`` directory itself. Use this for paths
  that live **inside** the src tree but cross package boundaries:
  ``src/tests/fixtures/...``, ``src/data/v1.db``,
  ``src/synthetic_data/output/...``, ``src/docs`` does NOT exist (docs/
  stays at repo root as a sibling of README.md).

Both return absolute :class:`~pathlib.Path` objects. The functions are
cheap (one ``resolve()`` call) but module-level constants in callers can
freely cache them — neither return value changes at runtime.
"""

from __future__ import annotations

from pathlib import Path


def src_root() -> Path:
    """Return the absolute path to the ``src/`` directory.

    Computed from this module's own location: ``src/_paths.py`` →
    ``parent`` is ``src/``.
    """
    return Path(__file__).resolve().parent


def repo_root() -> Path:
    """Return the absolute path to the repository root (parent of ``src/``).

    Use this for files that intentionally live **outside** ``src/`` —
    notably ``alias_table_seed.json`` (root by canonical-artifact contract)
    and the root-sibling ``docs/`` / ``scripts/`` / ``infra/`` trees.
    """
    return src_root().parent
