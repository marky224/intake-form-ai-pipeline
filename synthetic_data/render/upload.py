"""S3 uploader for the Phase 3 CMS-1500 renderer output.

Consumes the (PNG, sidecar JSON) pairs produced by ``render_batch`` and
uploads them to S3 under content-addressable keys derived from the PNG's
``image_sha256`` — already computed by the renderer and recorded in the
sidecar, so this module never re-hashes the PNG. Re-running the renderer
with the same Chromium version reproduces the same hash → same key, so
retries after a partial failure resume cleanly without bookkeeping.
(S3 versioning records a new object version on every PutObject
regardless of content equality — there is no built-in content dedup —
but content-addressable keys keep the *key namespace* tidy across
re-runs, which is the durable benefit; the ~25 MB of duplicate
noncurrent versions per re-run is acceptable storage waste at corpus
scale.)

Key scheme::

    <prefix>/<image_sha256>.png
    <prefix>/<image_sha256>.json

Both objects share the hash; only the extension differs. The
content-addressable path keeps the upload step idempotent without
HEAD-before-PUT, and the sidecar's ``patient_id`` field provides the
audit trail back from hash to source bundle. ``patient_id`` is also
stamped on each object as ``x-amz-meta-patient-id`` so HEAD-only Phase 4
cascade traces can resolve the source patient without fetching the
sidecar body.

Cross-Chromium-version PNG byte stability is NOT a project guarantee
(see ``render.py`` docstring) — bumping the pinned Playwright minor
version shifts PNG bytes → new hashes → new objects, not overwrites.
Acceptable because the synthetic corpus is regenerable; the older
objects just become orphaned on the bucket until the next lifecycle
sweep.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
else:
    S3Client = object  # runtime placeholder; boto3 has no public client type

SIDECAR_SCHEMA_VERSION_SUPPORTED = 1


@dataclass(frozen=True)
class UploadResult:
    """Records the S3 keys produced for one (PNG, sidecar) pair.

    ``image_sha256`` and ``patient_id`` are echoed back from the sidecar
    so the caller can correlate uploads with source bundles without
    re-reading the JSON.
    """

    png_key: str
    sidecar_key: str
    image_sha256: str
    patient_id: str


def _normalize_prefix(prefix: str) -> str:
    """Drop a trailing slash so key construction can append unconditionally.

    ``""``, ``"foo"``, and ``"foo/"`` all normalize to a form the caller
    can concatenate with ``"/{hash}.png"`` without producing ``//`` or
    losing the prefix entirely.
    """
    return prefix.rstrip("/")


def derive_s3_keys(image_sha256: str, prefix: str) -> tuple[str, str]:
    """Return ``(png_key, sidecar_key)`` for a content-addressable image.

    ``image_sha256`` must be the lowercase hex sha256 of the PNG bytes
    (64 chars) — the same value the renderer writes into the sidecar's
    ``image_sha256`` field. An empty ``prefix`` produces bare ``<hash>.png``
    / ``<hash>.json`` keys at the bucket root.
    """
    if len(image_sha256) != 64 or not all(c in "0123456789abcdef" for c in image_sha256):
        raise ValueError(f"image_sha256 must be 64 lowercase hex chars, got {image_sha256!r}")
    norm = _normalize_prefix(prefix)
    if norm:
        return f"{norm}/{image_sha256}.png", f"{norm}/{image_sha256}.json"
    return f"{image_sha256}.png", f"{image_sha256}.json"


def _load_sidecar(sidecar_path: Path) -> dict:
    """Read + validate a sidecar JSON file written by the renderer.

    Validates the fields the uploader actually consumes (schema_version,
    image_sha256, patient_id) so a sidecar from a future schema or a
    half-written file fails loudly rather than uploading under a wrong
    or empty key.
    """
    data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Sidecar {sidecar_path} root must be a JSON object")

    schema = data.get("schema_version")
    if schema != SIDECAR_SCHEMA_VERSION_SUPPORTED:
        raise ValueError(
            f"Sidecar {sidecar_path} has schema_version={schema!r}; "
            f"uploader supports {SIDECAR_SCHEMA_VERSION_SUPPORTED}"
        )

    sha = data.get("image_sha256")
    # Mirror derive_s3_keys' hex-charset check at load time so an invalid
    # sidecar fails with a path-naming error rather than slipping through
    # to a less actionable error inside derive_s3_keys downstream.
    if not isinstance(sha, str) or len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha):
        raise ValueError(f"Sidecar {sidecar_path} image_sha256 must be a 64-char hex string")

    pid = data.get("patient_id")
    if not isinstance(pid, str) or not pid:
        raise ValueError(f"Sidecar {sidecar_path} patient_id must be a non-empty string")

    return data


def upload_pair(
    png_path: Path,
    sidecar_path: Path,
    bucket: str,
    prefix: str,
    s3_client: S3Client | None = None,
) -> UploadResult:
    """Upload one (PNG, sidecar) pair to S3 under content-addressable keys.

    The PNG is uploaded as ``image/png`` and the sidecar as
    ``application/json``; both objects carry ``x-amz-meta-patient-id``
    set to the sidecar's ``patient_id`` so a HEAD on either object
    surfaces the source patient without a Body fetch.

    ``s3_client`` is injectable for testing (moto's ``mock_aws``).
    When None, a fresh ``boto3.client("s3")`` is created — boto3 picks
    up credentials via the standard chain (env / shared file / instance
    profile), so callers don't pass keys.
    """
    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3")

    sidecar = _load_sidecar(Path(sidecar_path))
    image_sha256: str = sidecar["image_sha256"]
    patient_id: str = sidecar["patient_id"]
    png_key, sidecar_key = derive_s3_keys(image_sha256, prefix)

    metadata = {"patient-id": patient_id}

    s3_client.put_object(
        Bucket=bucket,
        Key=png_key,
        Body=Path(png_path).read_bytes(),
        ContentType="image/png",
        Metadata=metadata,
    )
    s3_client.put_object(
        Bucket=bucket,
        Key=sidecar_key,
        Body=Path(sidecar_path).read_bytes(),
        ContentType="application/json",
        Metadata=metadata,
    )
    return UploadResult(
        png_key=png_key,
        sidecar_key=sidecar_key,
        image_sha256=image_sha256,
        patient_id=patient_id,
    )


def find_render_pairs(input_dir: Path) -> list[tuple[Path, Path]]:
    """Discover (PNG, sidecar) pairs in a renderer-output directory.

    Pairs are matched by stem: ``<stem>.png`` pairs with ``<stem>.json``.
    Unpaired files raise ``FileNotFoundError`` listing the offenders —
    the renderer writes pairs atomically, so an orphan signals tooling
    drift (mid-run abort, manual deletion, mis-curated dir) that the
    upload step should not silently paper over.
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


def upload_render_dir(
    input_dir: Path,
    bucket: str,
    prefix: str,
    s3_client: S3Client | None = None,
) -> list[UploadResult]:
    """Upload every (PNG, sidecar) pair in ``input_dir`` to S3.

    Sequential upload — ~1-2 minutes for the full 500-doc corpus (1000
    objects, ~25 MB total). Re-runs of the same corpus land at the same
    keys (content-addressable), so retries after a partial failure
    resume cleanly without bookkeeping. See module docstring for the
    versioning footnote — re-runs do record duplicate object versions,
    just under the same key namespace.
    """
    if s3_client is None:
        import boto3

        s3_client = boto3.client("s3")

    pairs = find_render_pairs(Path(input_dir))
    return [upload_pair(png, sidecar, bucket, prefix, s3_client) for png, sidecar in pairs]


DEFAULT_S3_PREFIX = "synthetic/healthcare/cms1500"


def main(argv: list[str] | None = None) -> int:
    """Upload renderer-output PNG + sidecar pairs to S3.

    Usage::

        uv run python -m synthetic_data.render.upload \\
            --input  synthetic_data/output/render \\
            --bucket intake-form-ai-pipeline-documents \\
            --prefix synthetic/healthcare/cms1500

    Default ``--prefix`` matches the locked design path; override only
    for dev/test scratch uploads. AWS credentials come from the standard
    boto3 chain (env vars, ``~/.aws/credentials``, instance profile) —
    this CLI never reads keys from arguments or ``.env``.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Upload (PNG, sidecar JSON) pairs produced by the CMS-1500 renderer "
            "to S3 under content-addressable keys (<prefix>/<image_sha256>.{png,json})."
        ),
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="Directory of (PNG, sidecar JSON) pairs from the renderer.",
    )
    parser.add_argument(
        "--bucket",
        type=str,
        required=True,
        help="Destination S3 bucket (e.g., intake-form-ai-pipeline-documents).",
    )
    parser.add_argument(
        "--prefix",
        type=str,
        default=DEFAULT_S3_PREFIX,
        help=f"S3 key prefix. Default: {DEFAULT_S3_PREFIX}.",
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

    import boto3

    s3_client = boto3.client("s3")
    normalized_prefix = _normalize_prefix(args.prefix)
    destination = (
        f"s3://{args.bucket}/{normalized_prefix}/" if normalized_prefix else f"s3://{args.bucket}/"
    )
    print(f"Uploading {len(pairs)} pair(s) -> {destination}")
    for png_path, sidecar_path in pairs:
        result = upload_pair(png_path, sidecar_path, args.bucket, args.prefix, s3_client=s3_client)
        print(f"  {png_path.name} -> {result.png_key}")
    print(f"Done — {len(pairs)} pair(s) uploaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
