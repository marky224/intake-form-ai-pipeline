"""Tests for the DocILE end-to-end ingest orchestrator.

Built around the same synthetic fixture set as ``test_docile_parse``
(``tests/fixtures/docile/{annotations,train.json,val.json}``). PDFs
are synthesized into a tmp directory per-test rather than committed
as binaries — they're generated deterministically by pypdfium2 with
the same page counts the annotation fixtures declare, so the tests
exercise the real rasterize + sidecar + pair-discovery path without
hand-curated binary commits.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from synthetic_data.docile._make_pdf_fixture import make_blank_pdf
from synthetic_data.docile.ingest import (
    DEFAULT_S3_PREFIX_DOCILE,
    ingest_dataset,
    ingest_document,
    main,
)
from synthetic_data.docile.parse import load_document
from synthetic_data.render.upload import upload_render_dir

FIXTURE_DIR = Path(__file__).parent / "tests" / "fixtures" / "docile"
ANNOTATIONS_DIR = FIXTURE_DIR / "annotations"

DOC_ID_SINGLE = "aaa000000000000000000001"  # 1 page, train
DOC_ID_MULTI = "aaa000000000000000000002"  # 3 pages, train
DOC_ID_MINIMAL = "aaa000000000000000000003"  # 1 page, val

FIXTURE_DOC_IDS = (DOC_ID_SINGLE, DOC_ID_MULTI, DOC_ID_MINIMAL)


@pytest.fixture
def dataset_root(tmp_path: Path) -> Path:
    """Materialize a DocILE-shaped dataset dir backed by the committed annotations
    plus synthesized PDFs whose page counts come from each annotation's own
    ``metadata.page_count`` (single source of truth — avoids duplicating
    page-count knowledge between the fixtures and this helper).
    """
    root = tmp_path / "docile"
    (root / "annotations").mkdir(parents=True)
    (root / "pdfs").mkdir()

    for doc_id in FIXTURE_DOC_IDS:
        src = ANNOTATIONS_DIR / f"{doc_id}.json"
        annotation = json.loads(src.read_text(encoding="utf-8"))
        page_count = annotation["metadata"]["page_count"]
        (root / "annotations" / f"{doc_id}.json").write_bytes(src.read_bytes())
        make_blank_pdf(root / "pdfs" / f"{doc_id}.pdf", num_pages=page_count)

    (root / "train.json").write_bytes((FIXTURE_DIR / "train.json").read_bytes())
    (root / "val.json").write_bytes((FIXTURE_DIR / "val.json").read_bytes())
    return root


def test_ingest_document_writes_pair_for_each_page(dataset_root: Path, tmp_path: Path) -> None:
    """Multi-page doc → one (PNG, sidecar) pair per page, flat in render_dir."""
    render_dir = tmp_path / "render"
    doc = load_document(dataset_root / "annotations" / f"{DOC_ID_MULTI}.json", split="train")
    pdf_path = dataset_root / "pdfs" / f"{DOC_ID_MULTI}.pdf"

    pages = ingest_document(doc, pdf_path, render_dir)
    assert len(pages) == 3

    for page in pages:
        assert page.png_path.is_file()
        sidecar_path = page.png_path.with_suffix(".json")
        assert sidecar_path.is_file()
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        assert sidecar["source_id"] == f"{DOC_ID_MULTI}-p{page.page_number}"
        assert sidecar["docile"]["doc_id"] == DOC_ID_MULTI
        assert sidecar["docile"]["page_number"] == page.page_number


def test_ingest_document_image_sha256_matches_png_bytes(dataset_root: Path, tmp_path: Path) -> None:
    """The sidecar's image_sha256 must match the on-disk PNG bytes."""
    import hashlib

    render_dir = tmp_path / "render"
    doc = load_document(dataset_root / "annotations" / f"{DOC_ID_MINIMAL}.json", split="val")
    pdf_path = dataset_root / "pdfs" / f"{DOC_ID_MINIMAL}.pdf"

    pages = ingest_document(doc, pdf_path, render_dir)
    page = pages[0]
    sidecar = json.loads(page.png_path.with_suffix(".json").read_text(encoding="utf-8"))
    expected_sha = hashlib.sha256(page.png_path.read_bytes()).hexdigest()
    assert sidecar["image_sha256"] == expected_sha


def test_ingest_dataset_processes_both_splits(dataset_root: Path, tmp_path: Path) -> None:
    """Default splits=(train, val) → 3 docs processed (2 train + 1 val)."""
    render_dir = tmp_path / "render"
    processed = ingest_dataset(dataset_root, render_dir)
    assert processed == 3

    # Multi-page doc contributes 3 pairs; single-page docs contribute 1 each.
    expected_pairs = 1 + 3 + 1
    pngs = sorted(render_dir.glob("*.png"))
    jsons = sorted(render_dir.glob("*.json"))
    assert len(pngs) == expected_pairs
    assert len(jsons) == expected_pairs


def test_ingest_dataset_respects_limit(dataset_root: Path, tmp_path: Path) -> None:
    """limit=1 stops after one document, not one page."""
    render_dir = tmp_path / "render"
    processed = ingest_dataset(dataset_root, render_dir, limit=1)
    assert processed == 1
    # The first train-split doc is DOC_ID_SINGLE (1 page) per train.json's order.
    pngs = sorted(p.stem for p in render_dir.glob("*.png"))
    assert pngs == [f"{DOC_ID_SINGLE}-p1"]


def test_ingest_dataset_limit_larger_than_corpus_processes_all(
    dataset_root: Path, tmp_path: Path
) -> None:
    """A limit above the corpus size is a no-op cap."""
    render_dir = tmp_path / "render"
    processed = ingest_dataset(dataset_root, render_dir, limit=100)
    assert processed == 3


def test_ingest_dataset_limit_zero_means_no_cap(dataset_root: Path, tmp_path: Path) -> None:
    """``limit=0`` is the justfile-friendly sentinel for 'no cap'.

    Locks the semantic so the ``just synthetic-data-docile-build`` recipe
    can pass ``--limit {{limit}}`` unconditionally with default ``"0"``.
    """
    render_dir = tmp_path / "render"
    processed = ingest_dataset(dataset_root, render_dir, limit=0)
    assert processed == 3


def test_ingest_dataset_rejects_negative_limit(dataset_root: Path, tmp_path: Path) -> None:
    """Negative limit is invalid input — fails fast rather than silently meaning 'no cap'.

    A recipe that accidentally passes ``--limit -5`` (e.g., from a malformed
    env-substitution) should surface the typo instead of processing the
    whole corpus.
    """
    render_dir = tmp_path / "render"
    with pytest.raises(ValueError, match="non-negative"):
        ingest_dataset(dataset_root, render_dir, limit=-1)


def test_ingest_dataset_train_only(dataset_root: Path, tmp_path: Path) -> None:
    """Selecting a single split yields only that split's docs."""
    render_dir = tmp_path / "render"
    processed = ingest_dataset(dataset_root, render_dir, splits=("train",))
    assert processed == 2
    pngs = {p.stem.split("-p")[0] for p in render_dir.glob("*.png")}
    assert pngs == {DOC_ID_SINGLE, DOC_ID_MULTI}


def test_ingest_dataset_rejects_test_split(dataset_root: Path, tmp_path: Path) -> None:
    """Defense-in-depth: ingest_dataset refuses 'test' even before iter_split sees it.

    Matches on ``process-batch`` rather than the ``half-now-half-later``
    marketing phrase so the test stays green if the error wording is
    polished. ``process-batch`` is the locked Phase 7 recipe name in
    ``cost-model.md`` — semantically stable.
    """
    render_dir = tmp_path / "render"
    with pytest.raises(ValueError, match="process-batch"):
        ingest_dataset(dataset_root, render_dir, splits=("train", "test"))


def test_ingest_document_raises_on_page_count_mismatch(dataset_root: Path, tmp_path: Path) -> None:
    """A PDF whose page count disagrees with the annotation metadata aborts cleanly.

    Replace the matching PDF with a single-page blank — the annotation
    still says 3 pages, so the rasterizer produces 1 page and the
    page-count check raises before any sidecar is written.
    """
    pdf_path = dataset_root / "pdfs" / f"{DOC_ID_MULTI}.pdf"
    pdf_path.unlink()
    make_blank_pdf(pdf_path, num_pages=1)
    doc = load_document(dataset_root / "annotations" / f"{DOC_ID_MULTI}.json", split="train")
    render_dir = tmp_path / "render"

    with pytest.raises(ValueError, match="Page count mismatch"):
        ingest_document(doc, pdf_path, render_dir)

    # No sidecars/PNGs leaked into render_dir before the raise.
    assert not list(render_dir.glob("*.json"))


def test_ingest_dataset_raises_when_pdf_missing(dataset_root: Path, tmp_path: Path) -> None:
    """An annotation with no matching PDF surfaces a FileNotFoundError."""
    (dataset_root / "pdfs" / f"{DOC_ID_SINGLE}.pdf").unlink()
    render_dir = tmp_path / "render"
    with pytest.raises(FileNotFoundError, match="PDF missing"):
        ingest_dataset(dataset_root, render_dir)


def test_default_docile_s3_prefix() -> None:
    """Locked DocILE bucket prefix."""
    assert DEFAULT_S3_PREFIX_DOCILE == "synthetic/business/docile"


def test_main_returns_2_when_dataset_root_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A typo'd --dataset-root that doesn't exist exits 2 before any work."""
    rc = main(
        [
            "--dataset-root",
            str(tmp_path / "does-not-exist"),
            "--render-dir",
            str(tmp_path / "render"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "is not a directory" in err


def test_main_happy_path_logs_doc_count(
    dataset_root: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Successful CLI invocation reports the document count."""
    render_dir = tmp_path / "render"
    rc = main(
        [
            "--dataset-root",
            str(dataset_root),
            "--render-dir",
            str(render_dir),
            "--limit",
            "2",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "Ingested 2 document(s)" in out


# ---------------------------------------------------------------------------
# Slow integration test — ingest one fixture doc end-to-end via moto.
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_ingest_then_upload_one_fixture_end_to_end(dataset_root: Path, tmp_path: Path) -> None:
    """One DocILE doc → ingest → upload via moto, verify S3 state + metadata.

    The DocILE path reuses the existing ``synthetic_data.render.upload``
    module unchanged, so what we're really verifying here is the
    contract between the DocILE sidecar builder (this PR) and the
    refactored ``_load_sidecar`` (this PR's other half): every field
    the uploader requires (schema_version, image_sha256, source_id)
    is present in our sidecar.
    """
    render_dir = tmp_path / "render"
    processed = ingest_dataset(dataset_root, render_dir, splits=("val",))
    assert processed == 1

    bucket = "intake-form-ai-pipeline-documents-test"
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=bucket)
        results = upload_render_dir(
            render_dir,
            bucket,
            DEFAULT_S3_PREFIX_DOCILE,
            s3_client=client,
        )

        assert len(results) == 1
        r = results[0]
        assert r.source_id == f"{DOC_ID_MINIMAL}-p1"
        assert r.png_key.startswith(f"{DEFAULT_S3_PREFIX_DOCILE}/")
        assert r.png_key.endswith(".png")

        head = client.head_object(Bucket=bucket, Key=r.png_key)
        assert head["ContentType"] == "image/png"
        assert head["Metadata"]["source-id"] == r.source_id
