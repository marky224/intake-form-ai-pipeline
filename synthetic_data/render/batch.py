"""Batch CLI driver for the Synthea -> CMS-1500 renderer.

Walks a directory of Synthea FHIR bundles, parses each via
``synthetic_data.synthea.parse.extract_patient``, and renders one PNG +
one sidecar JSON per patient into the output directory using a single
reused Chromium process.

Usage::

    uv run python -m synthetic_data.render.batch \\
        --input  tests/fixtures/synthea/fhir \\
        --output synthetic_data/output/render-test

Phase 3 PR (c) will replace the local output dir with an S3 upload +
content-hash idempotency. PR (b) keeps the writes local to make the
6-fixture render test deterministic + verifiable on disk.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from synthetic_data.synthea.parse import extract_patient, find_patient_bundles, load_bundle

from .render import render_batch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render Synthea FHIR bundles to CMS-1500 PNG + sidecar JSON pairs.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Directory containing Synthea-generated FHIR Bundle JSON files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Destination directory. Created if missing. Existing files overwritten.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Render at most N patients (sorted-by-filename order). Default: all.",
    )
    args = parser.parse_args(argv)

    bundle_paths = find_patient_bundles(args.input)
    if args.limit is not None:
        bundle_paths = bundle_paths[: args.limit]
    if not bundle_paths:
        print(f"No patient bundles found in {args.input}", file=sys.stderr)
        return 1

    print(f"Rendering {len(bundle_paths)} patient(s) -> {args.output}")
    patients = (extract_patient(load_bundle(p)) for p in bundle_paths)
    results = render_batch(patients, args.output)

    for png_path, sidecar_path in results:
        print(f"  {png_path.name} ({png_path.stat().st_size:>7} B) + {sidecar_path.name}")
    print(f"Done — {len(results)} document(s) rendered.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
