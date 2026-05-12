"""Unit tests for ``synthetic_data.render.upload``.

The pure-helper tests (key derivation, sidecar validation, pair
discovery) run without any AWS surface. The PutObject-driven tests
exercise the real boto3 client against moto's in-process S3 mock via
``mock_aws`` — no AWS credentials or network access required, so these
run in CI alongside the renderer helper tests.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from synthetic_data.render.upload import (
    DEFAULT_S3_PREFIX,
    SIDECAR_SCHEMA_VERSION_SUPPORTED,
    UploadResult,
    _load_sidecar,
    _normalize_prefix,
    derive_s3_keys,
    find_render_pairs,
    main,
    upload_pair,
    upload_render_dir,
)

# A real PNG isn't needed — the uploader treats the file as opaque bytes
# and sets Content-Type explicitly. Using arbitrary bytes makes the test
# fixture cheap and removes the renderer (Chromium) dependency.
SAMPLE_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-payload-for-tests"
SAMPLE_SOURCE_ID = "aee7bbe1-0c45-c028-1e62-1f4cdb30c273"
SAMPLE_PREFIX = "synthetic/healthcare/cms1500"
SAMPLE_BUCKET = "intake-form-ai-pipeline-documents-test"


def _png_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_sidecar_dict(
    image_filename: str,
    image_sha256: str,
    source_id: str = SAMPLE_SOURCE_ID,
) -> dict:
    """Minimal sidecar JSON shape matching what render.py writes."""
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION_SUPPORTED,
        "image": image_filename,
        "image_sha256": image_sha256,
        "source_id": source_id,
        "page": {"number": 1, "width_px": 850, "height_px": 1100},
        "signature": {"mode": "typed", "font": "Arial", "rotation_deg": 0.0},
        "fields": [],
    }


def _write_render_pair(
    out_dir: Path,
    *,
    stem: str = "patient-d31b73e1",
    png_bytes: bytes = SAMPLE_PNG_BYTES,
    source_id: str = SAMPLE_SOURCE_ID,
    sidecar_sha_override: str | None = None,
) -> tuple[Path, Path, str]:
    """Write one (PNG, sidecar) pair to ``out_dir`` and return (png, sidecar, sha)."""
    png_path = out_dir / f"{stem}.png"
    sidecar_path = out_dir / f"{stem}.json"
    png_path.write_bytes(png_bytes)
    image_sha = sidecar_sha_override or _png_sha256(png_bytes)
    sidecar = _make_sidecar_dict(png_path.name, image_sha, source_id=source_id)
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True), encoding="utf-8")
    return png_path, sidecar_path, image_sha


# ---------------------------------------------------------------------------
# Pure-function tests (no AWS surface)
# ---------------------------------------------------------------------------


def test_normalize_prefix_strips_trailing_slash() -> None:
    assert _normalize_prefix("synthetic/healthcare/cms1500/") == "synthetic/healthcare/cms1500"


def test_normalize_prefix_passes_through_when_no_trailing_slash() -> None:
    assert _normalize_prefix("synthetic/healthcare/cms1500") == "synthetic/healthcare/cms1500"


def test_normalize_prefix_empty_stays_empty() -> None:
    assert _normalize_prefix("") == ""


def test_derive_s3_keys_with_prefix() -> None:
    sha = "a" * 64
    png_key, json_key = derive_s3_keys(sha, SAMPLE_PREFIX)
    assert png_key == f"{SAMPLE_PREFIX}/{sha}.png"
    assert json_key == f"{SAMPLE_PREFIX}/{sha}.json"


def test_derive_s3_keys_normalizes_trailing_slash_on_prefix() -> None:
    sha = "b" * 64
    png_key, json_key = derive_s3_keys(sha, f"{SAMPLE_PREFIX}/")
    # No double slash even though the caller passed one.
    assert png_key == f"{SAMPLE_PREFIX}/{sha}.png"
    assert json_key == f"{SAMPLE_PREFIX}/{sha}.json"


def test_derive_s3_keys_empty_prefix_produces_bare_filenames() -> None:
    sha = "c" * 64
    png_key, json_key = derive_s3_keys(sha, "")
    assert png_key == f"{sha}.png"
    assert json_key == f"{sha}.json"


def test_derive_s3_keys_pair_shares_hash_differs_only_in_extension() -> None:
    """The pairing-by-hash invariant: HEAD on either key yields the other."""
    sha = "d" * 64
    png_key, json_key = derive_s3_keys(sha, SAMPLE_PREFIX)
    assert png_key.removesuffix(".png") == json_key.removesuffix(".json")


def test_derive_s3_keys_rejects_short_hash() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex chars"):
        derive_s3_keys("abc123", SAMPLE_PREFIX)


def test_derive_s3_keys_rejects_uppercase_hash() -> None:
    """Sidecars are written lowercase; uppercase would split content-addressable keys."""
    with pytest.raises(ValueError, match="64 lowercase hex chars"):
        derive_s3_keys("A" * 64, SAMPLE_PREFIX)


def test_derive_s3_keys_rejects_non_hex_chars() -> None:
    with pytest.raises(ValueError, match="64 lowercase hex chars"):
        derive_s3_keys("z" * 64, SAMPLE_PREFIX)


def test_load_sidecar_round_trip(tmp_path: Path) -> None:
    """Valid sidecar parses; image_sha256 + source_id are returned verbatim."""
    sidecar_path = tmp_path / "x.json"
    sha = "e" * 64
    sidecar_path.write_text(json.dumps(_make_sidecar_dict("x.png", sha)), encoding="utf-8")
    data = _load_sidecar(sidecar_path)
    assert data["image_sha256"] == sha
    assert data["source_id"] == SAMPLE_SOURCE_ID


def test_load_sidecar_rejects_wrong_schema_version(tmp_path: Path) -> None:
    sidecar_path = tmp_path / "x.json"
    bad = _make_sidecar_dict("x.png", "f" * 64)
    bad["schema_version"] = 99
    sidecar_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        _load_sidecar(sidecar_path)


def test_load_sidecar_rejects_short_image_sha256(tmp_path: Path) -> None:
    sidecar_path = tmp_path / "x.json"
    bad = _make_sidecar_dict("x.png", "abcdef")
    sidecar_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="64-char hex"):
        _load_sidecar(sidecar_path)


def test_load_sidecar_rejects_non_hex_image_sha256(tmp_path: Path) -> None:
    """A 64-char non-hex image_sha256 is rejected at load time, not downstream.

    Without the hex check inside _load_sidecar, the bad value would pass the
    length+type validation here and only fail later inside derive_s3_keys —
    where the error message wouldn't name the sidecar file that produced it,
    making corrupt-fixture debugging harder.
    """
    sidecar_path = tmp_path / "x.json"
    bad = _make_sidecar_dict("x.png", "z" * 64)
    sidecar_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match=r"Sidecar .* image_sha256 must be a 64-char hex string"):
        _load_sidecar(sidecar_path)


def test_load_sidecar_rejects_empty_source_id(tmp_path: Path) -> None:
    sidecar_path = tmp_path / "x.json"
    bad = _make_sidecar_dict("x.png", "a" * 64, source_id="")
    sidecar_path.write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(ValueError, match="source_id"):
        _load_sidecar(sidecar_path)


def test_find_render_pairs_matches_stems(tmp_path: Path) -> None:
    _write_render_pair(tmp_path, stem="a-11111111")
    _write_render_pair(tmp_path, stem="b-22222222", png_bytes=b"second")
    pairs = find_render_pairs(tmp_path)
    assert [(p.name, j.name) for p, j in pairs] == [
        ("a-11111111.png", "a-11111111.json"),
        ("b-22222222.png", "b-22222222.json"),
    ]


def test_find_render_pairs_empty_dir_returns_empty(tmp_path: Path) -> None:
    assert find_render_pairs(tmp_path) == []


def test_find_render_pairs_raises_on_orphan_png(tmp_path: Path) -> None:
    (tmp_path / "lonely.png").write_bytes(b"x")
    with pytest.raises(FileNotFoundError, match="PNG without sidecar"):
        find_render_pairs(tmp_path)


def test_find_render_pairs_raises_on_orphan_sidecar(tmp_path: Path) -> None:
    (tmp_path / "lonely.json").write_text("{}", encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="sidecar without PNG"):
        find_render_pairs(tmp_path)


# ---------------------------------------------------------------------------
# Moto-driven PutObject tests
# ---------------------------------------------------------------------------


@pytest.fixture
def s3_bucket():
    """Yield (boto3 S3 client, bucket name) backed by moto's in-process S3.

    ``mock_aws`` patches the boto3 endpoint resolver for the duration of
    the test, so any boto3.client("s3") created inside the context talks
    to moto. Region is fixed to us-east-1 to match the project's
    real-bucket region.
    """
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=SAMPLE_BUCKET)
        yield client, SAMPLE_BUCKET


def test_upload_pair_writes_png_and_sidecar(tmp_path: Path, s3_bucket) -> None:
    client, bucket = s3_bucket
    png_path, sidecar_path, sha = _write_render_pair(tmp_path)

    result = upload_pair(png_path, sidecar_path, bucket, SAMPLE_PREFIX, s3_client=client)

    assert isinstance(result, UploadResult)
    assert result.image_sha256 == sha
    assert result.source_id == SAMPLE_SOURCE_ID
    assert result.png_key == f"{SAMPLE_PREFIX}/{sha}.png"
    assert result.sidecar_key == f"{SAMPLE_PREFIX}/{sha}.json"

    listed = client.list_objects_v2(Bucket=bucket)["Contents"]
    keys = sorted(obj["Key"] for obj in listed)
    assert keys == sorted([result.png_key, result.sidecar_key])

    png_obj = client.get_object(Bucket=bucket, Key=result.png_key)
    assert png_obj["Body"].read() == SAMPLE_PNG_BYTES


def test_upload_pair_sets_content_types(tmp_path: Path, s3_bucket) -> None:
    client, bucket = s3_bucket
    png_path, sidecar_path, _ = _write_render_pair(tmp_path)
    result = upload_pair(png_path, sidecar_path, bucket, SAMPLE_PREFIX, s3_client=client)

    png_head = client.head_object(Bucket=bucket, Key=result.png_key)
    sidecar_head = client.head_object(Bucket=bucket, Key=result.sidecar_key)
    assert png_head["ContentType"] == "image/png"
    assert sidecar_head["ContentType"] == "application/json"


def test_upload_pair_sets_source_id_metadata(tmp_path: Path, s3_bucket) -> None:
    """HEAD on either object exposes the source_id without a Body fetch."""
    client, bucket = s3_bucket
    png_path, sidecar_path, _ = _write_render_pair(tmp_path)
    result = upload_pair(png_path, sidecar_path, bucket, SAMPLE_PREFIX, s3_client=client)

    png_head = client.head_object(Bucket=bucket, Key=result.png_key)
    sidecar_head = client.head_object(Bucket=bucket, Key=result.sidecar_key)
    assert png_head["Metadata"]["source-id"] == SAMPLE_SOURCE_ID
    assert sidecar_head["Metadata"]["source-id"] == SAMPLE_SOURCE_ID


def test_upload_pair_key_derives_from_sidecar_hash_not_recomputed(
    tmp_path: Path, s3_bucket
) -> None:
    """The uploader trusts the sidecar's image_sha256 verbatim.

    If the sidecar claims hash X for PNG bytes that actually hash to Y,
    the uploaded key uses X. This documents the deliberate trust
    boundary — the renderer is the single source of truth for the hash,
    and this module does not re-verify (the cost of re-hashing every
    PNG would scale poorly on the full 500-doc corpus).
    """
    client, bucket = s3_bucket
    bogus_hash = "9" * 64
    png_path, sidecar_path, _ = _write_render_pair(tmp_path, sidecar_sha_override=bogus_hash)
    result = upload_pair(png_path, sidecar_path, bucket, SAMPLE_PREFIX, s3_client=client)
    assert result.image_sha256 == bogus_hash
    assert result.png_key.endswith(f"{bogus_hash}.png")


def test_upload_pair_idempotent_at_key_namespace(tmp_path: Path, s3_bucket) -> None:
    """Re-uploading identical content lands at the same key (no key sprawl).

    S3 versioning would record a new version per PutObject regardless
    of content equality, but this test runs against an unversioned moto
    bucket so the second PUT simply overwrites in place. Either way the
    key namespace stays at exactly 2 keys (PNG + sidecar) for one logical
    document — which is the content-addressable invariant we care about.
    """
    client, bucket = s3_bucket
    png_path, sidecar_path, _ = _write_render_pair(tmp_path)

    first = upload_pair(png_path, sidecar_path, bucket, SAMPLE_PREFIX, s3_client=client)
    second = upload_pair(png_path, sidecar_path, bucket, SAMPLE_PREFIX, s3_client=client)

    assert first.png_key == second.png_key
    assert first.sidecar_key == second.sidecar_key

    listed = client.list_objects_v2(Bucket=bucket)["Contents"]
    assert len({obj["Key"] for obj in listed}) == 2


def test_upload_pair_default_s3_client_uses_boto3_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``s3_client`` is None, the module imports boto3 and constructs one.

    Verifying via mock_aws + dummy AWS env vars so boto3's default credential
    chain finds something rather than raising NoCredentialsError. This pins
    the "no creds passed by callers" convention from CLAUDE.md.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    png_path, sidecar_path, sha = _write_render_pair(tmp_path)

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=SAMPLE_BUCKET)
        result = upload_pair(png_path, sidecar_path, SAMPLE_BUCKET, SAMPLE_PREFIX)
        assert result.image_sha256 == sha
        # Verify the implicit client actually wrote to the mock bucket.
        verify = boto3.client("s3", region_name="us-east-1")
        head = verify.head_object(Bucket=SAMPLE_BUCKET, Key=result.png_key)
        assert head["Metadata"]["source-id"] == SAMPLE_SOURCE_ID


def test_upload_render_dir_uploads_every_pair(tmp_path: Path, s3_bucket) -> None:
    client, bucket = s3_bucket
    pair1 = _write_render_pair(tmp_path, stem="a-11111111", png_bytes=b"first-content")
    pair2 = _write_render_pair(tmp_path, stem="b-22222222", png_bytes=b"second-content")
    pair3 = _write_render_pair(tmp_path, stem="c-33333333", png_bytes=b"third-content")

    results = upload_render_dir(tmp_path, bucket, SAMPLE_PREFIX, s3_client=client)

    assert len(results) == 3
    listed = client.list_objects_v2(Bucket=bucket)["Contents"]
    keys = {obj["Key"] for obj in listed}
    assert len(keys) == 6  # 3 pairs * 2 objects each
    for _, _, sha in (pair1, pair2, pair3):
        assert f"{SAMPLE_PREFIX}/{sha}.png" in keys
        assert f"{SAMPLE_PREFIX}/{sha}.json" in keys


def test_upload_render_dir_empty_dir_returns_empty(tmp_path: Path, s3_bucket) -> None:
    client, bucket = s3_bucket
    assert upload_render_dir(tmp_path, bucket, SAMPLE_PREFIX, s3_client=client) == []
    assert "Contents" not in client.list_objects_v2(Bucket=bucket)


def test_upload_render_dir_raises_on_unpaired_files(tmp_path: Path, s3_bucket) -> None:
    """An orphan PNG should fail before any upload, not after partial progress."""
    client, bucket = s3_bucket
    _write_render_pair(tmp_path, stem="paired-11111111")
    (tmp_path / "orphan-22222222.png").write_bytes(b"orphan")

    with pytest.raises(FileNotFoundError, match="unpaired files"):
        upload_render_dir(tmp_path, bucket, SAMPLE_PREFIX, s3_client=client)

    # The paired upload must not have run either — we want fail-fast, not
    # partial-success-then-error.
    assert "Contents" not in client.list_objects_v2(Bucket=bucket)


# ---------------------------------------------------------------------------
# CLI tests — drive ``main()`` directly, with moto patching the boto3 client
# the CLI constructs itself (no s3_client injection on the CLI surface).
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_aws_env(monkeypatch: pytest.MonkeyPatch):
    """Dummy AWS env vars so boto3's default credential chain resolves under mock_aws."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    yield


def test_default_s3_prefix_matches_locked_design() -> None:
    """The CLI default prefix must match the locked design path.

    Bumping this string requires an updated ``current-state.md`` and the
    next-phase cascade providers that read these keys.
    """
    assert DEFAULT_S3_PREFIX == "synthetic/healthcare/cms1500"


def test_cli_uploads_all_pairs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mock_aws_env
) -> None:
    _write_render_pair(tmp_path, stem="a-11111111", png_bytes=b"alpha-content")
    _write_render_pair(tmp_path, stem="b-22222222", png_bytes=b"beta-content")

    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=SAMPLE_BUCKET)
        rc = main(
            [
                "--input",
                str(tmp_path),
                "--bucket",
                SAMPLE_BUCKET,
                "--prefix",
                SAMPLE_PREFIX,
            ]
        )
        assert rc == 0
        verify = boto3.client("s3", region_name="us-east-1")
        keys = {obj["Key"] for obj in verify.list_objects_v2(Bucket=SAMPLE_BUCKET)["Contents"]}
        assert len(keys) == 4  # 2 pairs * 2 objects

    out = capsys.readouterr().out
    assert "Uploading 2 pair(s)" in out
    assert "Done — 2 pair(s) uploaded." in out


def test_cli_uses_default_prefix_when_omitted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mock_aws_env
) -> None:
    """Omitting --prefix uses the locked DEFAULT_S3_PREFIX path."""
    _, _, sha = _write_render_pair(tmp_path)
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=SAMPLE_BUCKET)
        rc = main(["--input", str(tmp_path), "--bucket", SAMPLE_BUCKET])
        assert rc == 0
        verify = boto3.client("s3", region_name="us-east-1")
        keys = {obj["Key"] for obj in verify.list_objects_v2(Bucket=SAMPLE_BUCKET)["Contents"]}
        assert f"{DEFAULT_S3_PREFIX}/{sha}.png" in keys
        assert f"{DEFAULT_S3_PREFIX}/{sha}.json" in keys


def test_cli_no_pairs_returns_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mock_aws_env
) -> None:
    """An empty (but existing) input dir exits 1 with a clear message, no AWS call."""
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=SAMPLE_BUCKET)
        rc = main(["--input", str(tmp_path), "--bucket", SAMPLE_BUCKET])
        assert rc == 1
        verify = boto3.client("s3", region_name="us-east-1")
        assert "Contents" not in verify.list_objects_v2(Bucket=SAMPLE_BUCKET)
    err = capsys.readouterr().err
    assert "No render pairs found" in err


def test_cli_missing_input_dir_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd --input that doesn't resolve to a dir exits 2 before any AWS call."""
    rc = main(["--input", str(tmp_path / "does-not-exist"), "--bucket", SAMPLE_BUCKET])
    assert rc == 2
    err = capsys.readouterr().err
    assert "is not a directory" in err


def test_cli_unpaired_files_propagates_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mock_aws_env
) -> None:
    """An orphan in the input dir surfaces as a FileNotFoundError from find_render_pairs."""
    (tmp_path / "orphan-22222222.png").write_bytes(b"orphan")
    with mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=SAMPLE_BUCKET)
        with pytest.raises(FileNotFoundError, match="unpaired files"):
            main(["--input", str(tmp_path), "--bucket", SAMPLE_BUCKET])


# ---------------------------------------------------------------------------
# Slow integration test — renders the 6 Synthea fixtures via render_batch
# then uploads them via moto. Verifies the renderer ↔ uploader contract
# (sidecar schema, pair-by-stem invariant, image_sha256 round-trip) doesn't
# drift. Needs Chromium installed locally; CI skips via `-m "not slow"`.
# ---------------------------------------------------------------------------

SYNTHEA_FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures" / "synthea" / "fhir"


@pytest.mark.slow
def test_render_then_upload_six_fixtures_end_to_end(tmp_path: Path, s3_bucket) -> None:
    """Render the 6 committed Synthea fixtures, upload, verify S3 state.

    This is the only test in the suite that exercises the *full*
    Synthea → renderer → uploader chain. Mark as `slow` because the
    renderer pulls Playwright + Chromium (~10 s amortized for 6 docs).

    What this catches that the unit tests don't:

    * Sidecar JSON schema fields the uploader needs actually appear in
      real renderer output (not just the synthetic fixtures the upload
      unit tests use).
    * `_render_stem` filenames pair correctly via `find_render_pairs`.
    * `image_sha256` from a *real* Playwright screenshot round-trips
      through the uploader to the S3 key without drift.
    """
    from synthetic_data.render.render import render_batch
    from synthetic_data.synthea.parse import extract_patient, find_patient_bundles, load_bundle

    client, bucket = s3_bucket
    render_dir = tmp_path / "render"

    bundle_paths = find_patient_bundles(SYNTHEA_FIXTURE_DIR)
    assert len(bundle_paths) == 6, "expected 6 checked-in Synthea fixtures"

    patients = [extract_patient(load_bundle(p)) for p in bundle_paths]
    render_results = render_batch(patients, render_dir)
    assert len(render_results) == 6

    upload_results = upload_render_dir(render_dir, bucket, SAMPLE_PREFIX, s3_client=client)
    assert len(upload_results) == 6

    listed = client.list_objects_v2(Bucket=bucket)["Contents"]
    keys = {obj["Key"] for obj in listed}
    assert len(keys) == 12  # 6 pairs * 2 objects each

    # Each result's hash must yield exactly the two keys we expect.
    for r in upload_results:
        assert f"{SAMPLE_PREFIX}/{r.image_sha256}.png" in keys
        assert f"{SAMPLE_PREFIX}/{r.image_sha256}.json" in keys
        head = client.head_object(Bucket=bucket, Key=r.png_key)
        assert head["ContentType"] == "image/png"
        assert head["Metadata"]["source-id"] == r.source_id

    # Patient IDs round-trip from Synthea bundles through to the S3 source-id metadata.
    expected_source_ids = {p.patient_id for p in patients}
    uploaded_source_ids = {r.source_id for r in upload_results}
    assert uploaded_source_ids == expected_source_ids
