"""Replay cache for provider responses.

Default-on: providers check the cache first and short-circuit if a fixture
exists. The opt-in ``EVAL_LIVE=true`` env var bypasses the cache so the
provider hits the live upstream API and (on success) writes the fresh
response back to the fixture.

This is the cost-discipline lever from ``.claude-context/cost-model.md``:
CI runs without ``EVAL_LIVE`` and never makes live calls, so a PR can rerun
the cascade tests an arbitrary number of times for $0.

Cache layout::

    tests/fixtures/eval-cache/<provider_name>/<image_sha256>.json

The body of each file is the provider's ``raw_response`` dict verbatim — no
``ProviderResult`` wrapping, no normalization. Providers re-parse cached
responses through the same ``_parse_response`` they use for live calls so the
cached-replay path exercises the same code as production.

``EVAL_LIVE`` is read fresh on every ``is_live_mode()`` call (not cached at
module import) so test fixtures can ``monkeypatch.setenv("EVAL_LIVE", ...)``
without re-importing.

Phase 5's orchestrator never reads the cache — production code paths always
hit live providers. The cache is a dev/eval-only artifact.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: Truthy values for the ``EVAL_LIVE`` env var. Matches the common
#: shell-script convention for boolean flags (1 / true / yes). Comparison is
#: case-insensitive.
_EVAL_LIVE_TRUTHY = frozenset({"1", "true", "yes", "y", "on"})

#: Cache root, resolved relative to this file so the cache path works
#: regardless of the caller's cwd. Layout: ``<repo-root>/tests/fixtures/eval-cache/``.
CACHE_ROOT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "eval-cache"


def is_live_mode() -> bool:
    """Return True iff ``EVAL_LIVE`` env var is set to a truthy value.

    Read fresh on every call (not at module import) so tests can flip the
    mode via ``monkeypatch.setenv("EVAL_LIVE", "true")``.
    """
    return os.environ.get("EVAL_LIVE", "").strip().lower() in _EVAL_LIVE_TRUTHY


def cache_path(provider_name: str, image_sha256: str) -> Path:
    """Return the cache file path for ``(provider_name, image_sha256)``.

    Does not create the file or parent directory — read-only helper. The
    ``image_sha256`` must be a 64-char lowercase hex string (same shape the
    renderer/ingester writes into sidecars + the uploader validates).
    Mismatched shape raises ``ValueError`` so a caller bug fails fast rather
    than computing a cache key against malformed input.
    """
    if (
        not provider_name
        or "/" in provider_name
        or "\\" in provider_name
        or provider_name in {".", ".."}
    ):
        raise ValueError(
            f"provider_name must be a non-empty filesystem-safe slug; got {provider_name!r}"
        )
    if len(image_sha256) != 64 or not all(c in "0123456789abcdef" for c in image_sha256):
        raise ValueError(f"image_sha256 must be 64 lowercase hex chars, got {image_sha256!r}")
    return CACHE_ROOT / provider_name / f"{image_sha256}.json"


def load_cached(provider_name: str, image_sha256: str) -> dict[str, Any] | None:
    """Load a cached raw_response, or return None on cache miss.

    Returns the dict verbatim — no schema validation. Providers parse it
    through their ``_parse_response`` on the way back out.
    """
    path = cache_path(provider_name, image_sha256)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_cached(
    provider_name: str,
    image_sha256: str,
    raw_response: dict[str, Any],
) -> None:
    """Persist a raw_response under ``(provider_name, image_sha256)``.

    Creates the per-provider subdirectory on first use. Overwrites any
    existing fixture at the same path — the eval workflow assumes a fresh
    ``EVAL_LIVE=true`` run intentionally regenerates the cached responses.
    """
    path = cache_path(provider_name, image_sha256)
    path.parent.mkdir(parents=True, exist_ok=True)
    # ``indent=2`` keeps the on-disk fixtures human-diffable for code review.
    # Cache files are checked into git, so PRs that regenerate them must show
    # a readable diff for the reviewer to sanity-check.
    path.write_text(json.dumps(raw_response, indent=2, sort_keys=True), encoding="utf-8")
