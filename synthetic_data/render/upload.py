"""Local content-addressable store for the Phase 3 CMS-1500 renderer output.

Consumes the (PNG, sidecar JSON) pairs produced by ``render_batch`` and
writes them into a local filesystem store under content-addressable
paths derived from the PNG's ``image_sha256`` — already computed by the
renderer and recorded in the sidecar, so this module never re-hashes the
PNG. Re-running the renderer with the same Chromium version reproduces
the same hash → same path, so retries after a partial failure resume
cleanly by overwriting the identical bytes in place — no bookkeeping.

V1 is local-first (locked 2026-05-14): there is no S3. This module was
the S3 uploader through Phase 3; V1 refactors it to a filesystem store
under ``synthetic_data/output/`` (gitignored). The original S3
implementation is the V2 target — V2 reverts this module to ``boto3``
PutObject against the documents bucket (see ``.claude-context/phases.md``
item 3 V2 sub-bullet).

Path scheme::

    <store_root>/<prefix>/<image_sha256>.png
    <store_root>/<prefix>/<image_sha256>.json

Both files share the hash; only the extension differs. The
content-addressable layout keeps the store-write step idempotent without
a stat-before-write, and the sidecar's ``source_id`` field (a Synthea
``patient_id`` for the healthcare vertical, a DocILE ``<doc_id>-p<page>``
slug for the business vertical) provides the audit trail back from hash
to source record. Unlike the S3 path, ``source_id`` is not stamped as
object metadata — the sidecar is co-located on the local filesystem, so
reading it back is free and the HEAD-only optimization the S3 version
needed (``x-amz-meta-source-id``) is moot here.

Cross-Chromium-version PNG byte stability is NOT a project guarantee
(see ``render.py`` docstring) — bumping the pinned Playwright minor
version shifts PNG bytes → new hashes → new files, not overwrites. The
older files just become orphaned in the store until a manual sweep.
Acceptable because the synthetic corpus is regenerable.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

SIDECAR_SCHEMA_VERSION_SUPPORTED = 1


@dataclass(frozen=True)
class StoreResult:
    """Records the store paths produced for one (PNG, sidecar) pair.

    ``image_sha256`` and ``source_id`` are echoed back from the sidecar
    so the caller can correlate stored files with source records without
    re-reading the JSON. ``source_id`` is vertical-agnostic — a Synthea
    ``patient_id`` for CMS-1500 docs, a DocILE ``<doc_id>-p<page>`` for
    business-document docs.
    """

    png_path: Path
    sidecar_path: Path
    image_sha256: str
    source_id: str


def _normalize_prefix(prefix: str) -> str:
    """Drop a trailing slash so path construction can append unconditionally.

    ``""``, ``"foo"``, and ``"foo/"`` all normalize to a form the caller
    can join with ``"<hash>.png"`` without producing ``//`` or losing the
    prefix entirely.
    """
    return prefix.rstrip("/")


def derive_store_paths(image_sha256: str, store_root: Path, prefix: str) -> tuple[Path, Path]:
    """Return ``(png_path, sidecar_path)`` for a content-addressable image.

    ``image_sha256`` must be the lowercase hex sha256 of the PNG bytes
    (64 chars) — the same value the renderer writes into the sidecar's
    ``image_sha256`` field. An empty ``prefix`` puts the pair directly
    under ``store_root``.
    """
    if len(image_sha256) != 64 or not all(c in "0123456789abcdef" for c in image_sha256):
        raise ValueError(f"image_sha256 must be 64 lowercase hex chars, got {image_sha256!r}")
    base = Path(store_root) / _normalize_prefix(prefix)
    return base / f"{image_sha256}.png", base / f"{image_sha256}.json"


def _load_sidecar(sidecar_path: Path) -> dict:
    """Read + validate a sidecar JSON file written by a renderer/ingestor.

    Validates the fields the store writer actually consumes
    (schema_version, image_sha256, source_id) so a sidecar from a future
    schema or a half-written file fails loudly rather than landing under
    a wrong or empty path. ``source_id`` is the vertical-agnostic key
    (Synthea ``patient_id``, DocILE ``<doc_id>-p<page>``, etc.).
    """
    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Sidecar {sidecar_path} root must be a JSON object")

    schema = data.get("schema_version")
    if schema != SIDECAR_SCHEMA_VERSION_SUPPORTED:
        raise ValueError(
            f"Sidecar {sidecar_path} has schema_version={schema!r}; "
            f"store writer supports {SIDECAR_SCHEMA_VERSION_SUPPORTED}"
        )

    sha = data.get("image_sha256")
    # Mirror derive_store_paths' hex-charset check at load time so an
    # invalid sidecar fails with a path-naming error rather than slipping
    # through to a less actionable error inside derive_store_paths.
    if not isinstance(sha, str) or len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        raise ValueError(f"Sidecar {sidecar_path} image_sha256 must be a 64-char hex string")

    source_id = data.get("source_id")
    # `.strip()` catches whitespace-only values (e.g., `"  "`) — those
    # are non-empty by `not source_id`'s check but meaningless as an
    # audit identifier.
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError(f"Sidecar {sidecar_path} source_id must be a non-empty string")

    return data


def store_pair(
    png_path: Path,
    sidecar_path: Path,
    store_root: Path,
    prefix: str,
) -> StoreResult:
    """Copy one (PNG, sidecar) pair into the local store, content-addressed.

    The destination directory is created if needed. Re-running with the
    same sidecar hash overwrites the identical bytes in place, so a
    partial-failure retry resumes cleanly without external bookkeeping.

    The store writer trusts the sidecar's ``image_sha256`` verbatim — the
    renderer is the single source of truth for the hash, and re-hashing
    every PNG would scale poorly across the full 500-doc corpus.
    """
    sidecar = _load_sidecar(Path(sidecar_path))
    image_sha256: str = sidecar["image_sha256"]
    source_id: str = sidecar["source_id"]
    dest_png, dest_sidecar = derive_store_paths(image_sha256, store_root, prefix)

    dest_png.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(png_path), dest_png)
    shutil.copyfile(Path(sidecar_path), dest_sidecar)

    return StoreResult(
        png_path=dest_png,
        sidecar_path=dest_sidecar,
        image_sha256=image_sha256,
        source_id=source_id,
    )


def find_render_pairs(input_dir: Path) -> list[tuple[Path, Path]]:
    """Discover (PNG, sidecar) pairs in a renderer-output directory.

    Pairs are matched by stem: ``<stem>.png`` pairs with ``<stem>.json``.
    Unpaired files raise ``FileNotFoundError`` listing the offenders —
    the renderer writes pairs atomically, so an orphan signals tooling
    drift (mid-run abort, manual deletion, mis-curated dir) that the
    store step should not silently paper over.
    """
    input_dir = Path(input_dir)
    pngs = sorted(input_dir.glob("*.png"))
    jsons = sorted(input_dir.glob("*.json"))

    png_stems = {p.stem for p in pngs}
    json_stems = {j.stem for j in jsons}

    if png_stems != json_stems:
        png_only = sorted(png_stems - json_stems)
        json_only = sorted(json_stems - png_stems)
        parts: list[str] = []
        if png_only:
            parts.append(f"PNG without sidecar: {png_only[:5]}")
        if json_only:
            parts.append(f"sidecar without PNG: {json_only[:5]}")
        raise FileNotFoundError(
            f"Render directory {input_dir} has unpaired files: {'; '.join(parts)}"
        )

    return [(p, p.with_suffix(".json")) for p in pngs]


def store_render_dir(
    input_dir: Path,
    store_root: Path,
    prefix: str,
) -> list[StoreResult]:
    """Copy every (PNG, sidecar) pair in ``input_dir`` into the local store.

    Sequential — sub-second for the full 500-doc corpus (1000 files,
    ~25 MB total) since it's a local filesystem copy. Re-runs of the
    same corpus land at the same paths (content-addressable), so retries
    after a partial failure resume cleanly without bookkeeping.
    """
    pairs = find_render_pairs(Path(input_dir))
    return [store_pair(png, sidecar, store_root, prefix) for png, sidecar in pairs]


DEFAULT_STORE_PREFIX = "synthetic/healthcare/cms1500"
DEFAULT_STORE_ROOT = Path("synthetic_data/output/store")


def main(argv: list[str] | None = None) -> int:
    """Copy renderer-output PNG + sidecar pairs into the local store.

    Usage::

        uv run python -m synthetic_data.render.upload \\
            --input      synthetic_data/output/render \\
            --store-root synthetic_data/output/store \\
            --prefix     synthetic/healthcare/cms1500

    Default ``--prefix`` and ``--store-root`` match the locked V1 local
    layout; override only for dev/test scratch runs. No AWS, no
    credentials — V1 is local-first.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Copy (PNG, sidecar JSON) pairs produced by the CMS-1500 renderer "
            "into the local content-addressable store "
            "(<store-root>/<prefix>/<image_sha256>.{png,json})."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Directory of (PNG, sidecar JSON) pairs from the renderer.",
    )
    parser.add_argument(
        "--store-root",
        type=Path,
        default=DEFAULT_STORE_ROOT,
        help=f"Local store root directory. Default: {DEFAULT_STORE_ROOT}.",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=DEFAULT_STORE_PREFIX,
        help=f"Path prefix under the store root. Default: {DEFAULT_STORE_PREFIX}.",
    )
    args = parser.parse_args(argv)

    if not args.input.is_dir():
        # Path.glob() on a nonexistent dir silently returns empty; surface this
        # explicitly so a typo'd --input doesn't masquerade as an empty dir.
        print(f"--input {args.input} is not a directory", file=sys.stderr)
        return 2

    pairs = find_render_pairs(args.input)
    if not pairs:
        print(f"No render pairs found in {args.input}", file=sys.stderr)
        return 1

    normalized_prefix = _normalize_prefix(args.prefix)
    destination = (
        Path(args.store_root) / normalized_prefix if normalized_prefix else Path(args.store_root)
    )
    print(f"Storing {len(pairs)} pair(s) -> {destination}/")
    for png_path, sidecar_path in pairs:
        result = store_pair(png_path, sidecar_path, args.store_root, args.prefix)
        print(f"  {png_path.name} -> {result.png_path}")
    print(f"Done — {len(pairs)} pair(s) stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
