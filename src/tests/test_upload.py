"""Unit tests for ``synthetic_data.render.upload`` (local content store).

V1 is local-first: the store writer copies (PNG, sidecar) pairs into a
content-addressable local directory tree — no AWS, no moto, no network.
Every test runs against ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from synthetic_data.render.upload import (
    DEFAULT_STORE_PREFIX,
    SIDECAR_SCHEMA_VERSION_SUPPORTED,
    StoreResult,
    _load_sidecar,
    _normalize_prefix,
    derive_store_paths,
    find_render_pairs,
    main,
    store_pair,
    store_render_dir,
)

# A real PNG isn't needed — the store writer treats the file as opaque
# bytes. Arbitrary bytes keep the fixture cheap and drop the renderer
# (Chromium) dependency from the unit tests.
SAMPLE_PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-payload-for-tests"
SAMPLE_SOURCE_ID = "aee7bbe1-0c45-c028-1e62-1f4cdb30c273"
SAMPLE_PREFIX = "synthetic/healthcare/cms1500"


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
# Pure-function tests
# ---------------------------------------------------------------------------


def test_normalize_prefix_strips_trailing_slash() -> None:
    assert _normalize_prefix("synthetic/healthcare/cms1500/") == "synthetic/healthcare/cms1500"


def test_normalize_prefix_passes_through_when_no_trailing_slash() -> None:
    assert _normalize_prefix("synthetic/healthcare/cms1500") == "synthetic/healthcare/cms1500"


def test_normalize_prefix_empty_stays_empty() -> None:
    assert _normalize_prefix("") == ""


def test_derive_store_paths_with_prefix(tmp_path: Path) -> None:
    sha = "a" * 64
    png_path, json_path = derive_store_paths(sha, tmp_path, SAMPLE_PREFIX)
    assert png_path == tmp_path / SAMPLE_PREFIX / f"{sha}.png"
    assert json_path == tmp_path / SAMPLE_PREFIX / f"{sha}.json"


def test_derive_store_paths_normalizes_trailing_slash_on_prefix(tmp_path: Path) -> None:
    sha = "b" * 64
    png_path, json_path = derive_store_paths(sha, tmp_path, f"{SAMPLE_PREFIX}/")
    # No empty path segment even though the caller passed a trailing slash.
    assert png_path == tmp_path / SAMPLE_PREFIX / f"{sha}.png"
    assert json_path == tmp_path / SAMPLE_PREFIX / f"{sha}.json"


def test_derive_store_paths_empty_prefix_lands_directly_under_root(tmp_path: Path) -> None:
    sha = "c" * 64
    png_path, json_path = derive_store_paths(sha, tmp_path, "")
    assert png_path == tmp_path / f"{sha}.png"
    assert json_path == tmp_path / f"{sha}.json"


def test_derive_store_paths_pair_shares_hash_differs_only_in_extension(tmp_path: Path) -> None:
    """The pairing-by-hash invariant: the two paths differ only in suffix."""
    sha = "d" * 64
    png_path, json_path = derive_store_paths(sha, tmp_path, SAMPLE_PREFIX)
    assert png_path.with_suffix("") == json_path.with_suffix("")


def test_derive_store_paths_rejects_short_hash(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex chars"):
        derive_store_paths("abc123", tmp_path, SAMPLE_PREFIX)


def test_derive_store_paths_rejects_uppercase_hash(tmp_path: Path) -> None:
    """Sidecars are written lowercase; uppercase would split content-addressable paths."""
    with pytest.raises(ValueError, match="64 lowercase hex chars"):
        derive_store_paths("A" * 64, tmp_path, SAMPLE_PREFIX)


def test_derive_store_paths_rejects_non_hex_chars(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="64 lowercase hex chars"):
        derive_store_paths("z" * 64, tmp_path, SAMPLE_PREFIX)


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
    length+type validation here and only fail later inside derive_store_paths —
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


def test_load_sidecar_rejects_whitespace_only_source_id(tmp_path: Path) -> None:
    """A whitespace-only string is non-empty but functionally invalid as an audit id."""
    sidecar_path = tmp_path / "x.json"
    bad = _make_sidecar_dict("x.png", "a" * 64, source_id="   ")
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
# store_pair / store_render_dir — filesystem store
# ---------------------------------------------------------------------------


def test_store_pair_writes_png_and_sidecar(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    png_path, sidecar_path, sha = _write_render_pair(src)

    result = store_pair(png_path, sidecar_path, store, SAMPLE_PREFIX)

    assert isinstance(result, StoreResult)
    assert result.image_sha256 == sha
    assert result.source_id == SAMPLE_SOURCE_ID
    assert result.png_path == store / SAMPLE_PREFIX / f"{sha}.png"
    assert result.sidecar_path == store / SAMPLE_PREFIX / f"{sha}.json"

    assert result.png_path.read_bytes() == SAMPLE_PNG_BYTES
    # Only the PNG + sidecar exist under the prefix dir — nothing else.
    assert sorted(p.name for p in (store / SAMPLE_PREFIX).iterdir()) == [
        f"{sha}.json",
        f"{sha}.png",
    ]


def test_store_pair_creates_store_root_if_missing(tmp_path: Path) -> None:
    """The store-root tree is created on demand — caller need not pre-mkdir."""
    src = tmp_path / "src"
    src.mkdir()
    png_path, sidecar_path, sha = _write_render_pair(src)
    store = tmp_path / "deep" / "not" / "created" / "yet"

    result = store_pair(png_path, sidecar_path, store, SAMPLE_PREFIX)

    assert result.png_path.is_file()
    assert result.sidecar_path.is_file()


def test_store_pair_audit_trail_survives_in_stored_sidecar(tmp_path: Path) -> None:
    """source_id is recoverable from the stored sidecar (no object metadata on FS)."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    png_path, sidecar_path, _ = _write_render_pair(src)

    result = store_pair(png_path, sidecar_path, store, SAMPLE_PREFIX)

    stored = json.loads(result.sidecar_path.read_text(encoding="utf-8"))
    assert stored["source_id"] == SAMPLE_SOURCE_ID == result.source_id


def test_store_pair_path_derives_from_sidecar_hash_not_recomputed(tmp_path: Path) -> None:
    """The store writer trusts the sidecar's image_sha256 verbatim.

    If the sidecar claims hash X for PNG bytes that actually hash to Y,
    the stored path uses X. This documents the deliberate trust boundary
    — the renderer is the single source of truth for the hash, and this
    module does not re-verify (re-hashing every PNG would scale poorly on
    the full 500-doc corpus).
    """
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    bogus_hash = "9" * 64
    png_path, sidecar_path, _ = _write_render_pair(src, sidecar_sha_override=bogus_hash)
    result = store_pair(png_path, sidecar_path, store, SAMPLE_PREFIX)
    assert result.image_sha256 == bogus_hash
    assert result.png_path.name == f"{bogus_hash}.png"


def test_store_pair_idempotent_overwrite(tmp_path: Path) -> None:
    """Re-storing identical content lands at the same path (no path sprawl)."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    png_path, sidecar_path, _ = _write_render_pair(src)

    first = store_pair(png_path, sidecar_path, store, SAMPLE_PREFIX)
    second = store_pair(png_path, sidecar_path, store, SAMPLE_PREFIX)

    assert first.png_path == second.png_path
    assert first.sidecar_path == second.sidecar_path
    # Exactly 2 files for one logical document — the content-addressable invariant.
    assert len(list((store / SAMPLE_PREFIX).iterdir())) == 2


def test_store_render_dir_stores_every_pair(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    pair1 = _write_render_pair(src, stem="a-11111111", png_bytes=b"first-content")
    pair2 = _write_render_pair(src, stem="b-22222222", png_bytes=b"second-content")
    pair3 = _write_render_pair(src, stem="c-33333333", png_bytes=b"third-content")

    results = store_render_dir(src, store, SAMPLE_PREFIX)

    assert len(results) == 3
    names = {p.name for p in (store / SAMPLE_PREFIX).iterdir()}
    assert len(names) == 6  # 3 pairs * 2 files each
    for _, _, sha in (pair1, pair2, pair3):
        assert f"{sha}.png" in names
        assert f"{sha}.json" in names


def test_store_render_dir_empty_dir_returns_empty(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    assert store_render_dir(src, store, SAMPLE_PREFIX) == []
    assert not store.exists()


def test_store_render_dir_raises_on_unpaired_files(tmp_path: Path) -> None:
    """An orphan PNG should fail before any file is stored, not after partial progress."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    _write_render_pair(src, stem="paired-11111111")
    (src / "orphan-22222222.png").write_bytes(b"orphan")

    with pytest.raises(FileNotFoundError, match="unpaired files"):
        store_render_dir(src, store, SAMPLE_PREFIX)

    # Fail-fast: nothing stored, not partial-success-then-error.
    assert not store.exists()


# ---------------------------------------------------------------------------
# CLI tests — drive ``main()`` directly against the local filesystem.
# ---------------------------------------------------------------------------


def test_default_store_prefix_matches_locked_design() -> None:
    """The CLI default prefix must match the locked design path.

    Bumping this string requires an updated ``current-state.md`` and the
    cascade providers that read these paths.
    """
    assert DEFAULT_STORE_PREFIX == "synthetic/healthcare/cms1500"


def test_cli_stores_all_pairs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    _write_render_pair(src, stem="a-11111111", png_bytes=b"alpha-content")
    _write_render_pair(src, stem="b-22222222", png_bytes=b"beta-content")

    rc = main(["--input", str(src), "--store-root", str(store), "--prefix", SAMPLE_PREFIX])
    assert rc == 0

    names = {p.name for p in (store / SAMPLE_PREFIX).iterdir()}
    assert len(names) == 4  # 2 pairs * 2 files

    out = capsys.readouterr().out
    assert "Storing 2 pair(s)" in out
    assert "Done — 2 pair(s) stored." in out


def test_cli_uses_default_prefix_when_omitted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Omitting --prefix uses the locked DEFAULT_STORE_PREFIX path."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    _, _, sha = _write_render_pair(src)

    rc = main(["--input", str(src), "--store-root", str(store)])
    assert rc == 0

    base = store / DEFAULT_STORE_PREFIX
    assert (base / f"{sha}.png").is_file()
    assert (base / f"{sha}.json").is_file()


def test_cli_no_pairs_returns_1(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """An empty (but existing) input dir exits 1 with a clear message, nothing stored."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    rc = main(["--input", str(src), "--store-root", str(store)])
    assert rc == 1
    assert not store.exists()
    err = capsys.readouterr().err
    assert "No render pairs found" in err


def test_cli_missing_input_dir_returns_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd --input that doesn't resolve to a dir exits 2 before any store write."""
    store = tmp_path / "store"
    rc = main(["--input", str(tmp_path / "does-not-exist"), "--store-root", str(store)])
    assert rc == 2
    assert not store.exists()
    err = capsys.readouterr().err
    assert "is not a directory" in err


def test_cli_unpaired_files_propagates_error(tmp_path: Path) -> None:
    """An orphan in the input dir surfaces as a FileNotFoundError from find_render_pairs."""
    src = tmp_path / "src"
    src.mkdir()
    store = tmp_path / "store"
    (src / "orphan-22222222.png").write_bytes(b"orphan")
    with pytest.raises(FileNotFoundError, match="unpaired files"):
        main(["--input", str(src), "--store-root", str(store)])


# ---------------------------------------------------------------------------
# Slow integration test — renders the 6 Synthea fixtures via render_batch
# then stores them. Verifies the renderer ↔ store-writer contract (sidecar
# schema, pair-by-stem invariant, image_sha256 round-trip) doesn't drift.
# Needs Chromium installed locally; CI skips via `-m "not slow"`.
# ---------------------------------------------------------------------------

SYNTHEA_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "synthea" / "fhir"


@pytest.mark.slow
def test_render_then_store_six_fixtures_end_to_end(tmp_path: Path) -> None:
    """Render the 6 committed Synthea fixtures, store them, verify on-disk state.

    The only test that exercises the *full* Synthea → renderer →
    store-writer chain. Marked `slow` because the renderer pulls
    Playwright + Chromium (~10 s amortized for 6 docs).

    What this catches that the unit tests don't:

    * Sidecar JSON schema fields the store writer needs actually appear
      in real renderer output (not just the synthetic unit-test sidecars).
    * `_render_stem` filenames pair correctly via `find_render_pairs`.
    * `image_sha256` from a *real* Playwright screenshot round-trips
      through the store writer to the content-addressable path.
    """
    from synthetic_data.render.render import render_batch
    from synthetic_data.synthea.parse import extract_patient, find_patient_bundles, load_bundle

    render_dir = tmp_path / "render"
    store = tmp_path / "store"

    bundle_paths = find_patient_bundles(SYNTHEA_FIXTURE_DIR)
    assert len(bundle_paths) == 6, "expected 6 checked-in Synthea fixtures"

    patients = [extract_patient(load_bundle(p)) for p in bundle_paths]
    render_results = render_batch(patients, render_dir)
    assert len(render_results) == 6

    store_results = store_render_dir(render_dir, store, SAMPLE_PREFIX)
    assert len(store_results) == 6

    names = {p.name for p in (store / SAMPLE_PREFIX).iterdir()}
    assert len(names) == 12  # 6 pairs * 2 files each

    for r in store_results:
        assert f"{r.image_sha256}.png" in names
        assert f"{r.image_sha256}.json" in names
        assert r.png_path.is_file()
        stored = json.loads(r.sidecar_path.read_text(encoding="utf-8"))
        assert stored["source_id"] == r.source_id

    # Patient IDs round-trip from Synthea bundles through to the stored source_id.
    expected_source_ids = {p.patient_id for p in patients}
    stored_source_ids = {r.source_id for r in store_results}
    assert stored_source_ids == expected_source_ids
