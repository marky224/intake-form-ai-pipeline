"""Tests for the DocILE annotation parser.

The fixture at ``tests/fixtures/docile/`` is a synthetic 3-document
mini-dataset hand-crafted to match the upstream DocILE annotation
schema (verified against the real
``rossumai/docile/tests/data/sample-dataset/`` at upstream commit
``12f9502d1e…``). Real upstream samples are CC BY-NC-ND-licensed and
intentionally NOT redistributed in this MIT-licensed repo; the
synthetic fixtures cover parser correctness, while the slow integration
test in Task #4 will exercise the parser against the live downloaded
corpus.

Fixture map:
  - ``aaa…001`` — single page, 5 KILE fields, one LIR entry the parser
    must ignore. Train split.
  - ``aaa…002`` — three pages, fields distributed across pages, one
    field with ``text: null``. No LIR. Train split.
  - ``aaa…003`` — minimum: 1 page, 1 field, empty LIR. Val split.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from synthetic_data.docile.parse import (
    ALLOWED_SPLITS,
    DocileDocument,
    DocileField,
    iter_split,
    load_document,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "docile"
ANNOTATIONS_DIR = FIXTURE_DIR / "annotations"

DOC_ID_SINGLE = "aaa000000000000000000001"
DOC_ID_MULTI = "aaa000000000000000000002"
DOC_ID_MINIMAL = "aaa000000000000000000003"


def test_allowed_splits_is_train_val_only() -> None:
    """The half-now-half-later lock surfaces here too."""
    assert frozenset({"train", "val"}) == ALLOWED_SPLITS


def test_load_document_single_page_train() -> None:
    """Single-page invoice fixture parses into all 5 KILE fields, LIR dropped."""
    doc = load_document(ANNOTATIONS_DIR / f"{DOC_ID_SINGLE}.json", split="train")
    assert isinstance(doc, DocileDocument)
    assert doc.doc_id == DOC_ID_SINGLE
    assert doc.split == "train"
    assert doc.page_count == 1
    assert doc.page_sizes_at_200dpi == ((1700, 2200),)
    assert len(doc.fields) == 5
    fieldtypes = [f.fieldtype for f in doc.fields]
    assert fieldtypes == [
        "document_id",
        "date_issue",
        "vendor_name",
        "customer_billing_address",
        "amount_total_gross",
    ]
    for f in doc.fields:
        assert isinstance(f, DocileField)
        assert f.page == 0
        assert 0.0 <= f.bbox[0] <= f.bbox[2] <= 1.0
        assert 0.0 <= f.bbox[1] <= f.bbox[3] <= 1.0


def test_load_document_multi_page_handles_null_text_and_cross_page_fields() -> None:
    """Multi-page fixture: 5 fields across 3 pages, one with text=null."""
    doc = load_document(ANNOTATIONS_DIR / f"{DOC_ID_MULTI}.json", split="train")
    assert doc.page_count == 3
    assert len(doc.page_sizes_at_200dpi) == 3
    assert len(doc.fields) == 5
    pages = sorted(f.page for f in doc.fields)
    assert pages == [0, 0, 1, 2, 2]
    # One field carries text=null in the fixture — parser preserves None.
    null_text_fields = [f for f in doc.fields if f.text is None]
    assert len(null_text_fields) == 1
    assert null_text_fields[0].fieldtype == "date_issue"


def test_load_document_minimal_val() -> None:
    """Minimal fixture exercises the empty-LIR path."""
    doc = load_document(ANNOTATIONS_DIR / f"{DOC_ID_MINIMAL}.json", split="val")
    assert doc.split == "val"
    assert doc.page_count == 1
    assert len(doc.fields) == 1
    assert doc.fields[0].fieldtype == "vendor_name"
    assert doc.fields[0].text == "Stark Industries"


def test_load_document_rejects_test_split() -> None:
    """``test`` is reserved per the partitioning lock; parser refuses it."""
    with pytest.raises(ValueError, match="process-batch"):
        load_document(ANNOTATIONS_DIR / f"{DOC_ID_SINGLE}.json", split="test")


def test_load_document_rejects_unknown_split() -> None:
    """Typos / mismatched splits fail loudly rather than silently mislabel."""
    with pytest.raises(ValueError):
        load_document(ANNOTATIONS_DIR / f"{DOC_ID_SINGLE}.json", split="dev")


def test_load_document_rejects_out_of_range_bbox(tmp_path: Path) -> None:
    """A bbox value > 1.0 trips normalized-range validation."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "field_extractions": [
                    {"bbox": [0.1, 0.1, 1.5, 0.2], "fieldtype": "x", "page": 0, "text": "t"}
                ],
                "metadata": {"page_count": 1, "page_sizes_at_200dpi": [[1700, 2200]]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="normalized"):
        load_document(bad, split="train")


def test_load_document_rejects_inverted_bbox(tmp_path: Path) -> None:
    """left > right or top > bottom fails validation."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "field_extractions": [
                    {"bbox": [0.5, 0.1, 0.2, 0.3], "fieldtype": "x", "page": 0, "text": "t"}
                ],
                "metadata": {"page_count": 1, "page_sizes_at_200dpi": [[1700, 2200]]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_document(bad, split="train")


def test_load_document_rejects_field_page_past_page_count(tmp_path: Path) -> None:
    """A field.page >= page_count indicates corrupted data."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "field_extractions": [
                    {"bbox": [0.1, 0.1, 0.2, 0.2], "fieldtype": "x", "page": 5, "text": "t"}
                ],
                "metadata": {"page_count": 1, "page_sizes_at_200dpi": [[1700, 2200]]},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"page=5"):
        load_document(bad, split="train")


def test_load_document_rejects_page_sizes_length_mismatch(tmp_path: Path) -> None:
    """page_sizes_at_200dpi length must equal page_count."""
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "field_extractions": [],
                "metadata": {
                    "page_count": 3,
                    "page_sizes_at_200dpi": [[1700, 2200]],  # only 1, should be 3
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="page_sizes_at_200dpi"):
        load_document(bad, split="train")


def test_load_document_rejects_missing_metadata(tmp_path: Path) -> None:
    """Absent ``metadata`` block surfaces clearly."""
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"field_extractions": []}), encoding="utf-8")
    with pytest.raises(ValueError, match="metadata"):
        load_document(bad, split="train")


def test_iter_split_train_yields_two_docs() -> None:
    """train.json fixture lists two doc_ids."""
    results = list(iter_split(FIXTURE_DIR, "train"))
    assert [doc_id for doc_id, _ in results] == [DOC_ID_SINGLE, DOC_ID_MULTI]
    for _doc_id, ann_path in results:
        assert ann_path.is_file()


def test_iter_split_val_yields_minimal() -> None:
    """val.json fixture lists the minimal doc."""
    results = list(iter_split(FIXTURE_DIR, "val"))
    assert [doc_id for doc_id, _ in results] == [DOC_ID_MINIMAL]


def test_iter_split_rejects_test() -> None:
    """Defense-in-depth: even if a test.json file appeared on disk, iter_split refuses."""
    with pytest.raises(ValueError, match="process-batch"):
        list(iter_split(FIXTURE_DIR, "test"))


def test_iter_split_raises_when_index_missing(tmp_path: Path) -> None:
    """Missing split index surfaces the download contract violation."""
    with pytest.raises(FileNotFoundError, match="Split index"):
        list(iter_split(tmp_path, "train"))


def test_iter_split_raises_when_referenced_annotation_missing(tmp_path: Path) -> None:
    """train.json lists a doc_id with no matching annotation file."""
    (tmp_path / "train.json").write_text(json.dumps(["nonexistent_doc_id"]), encoding="utf-8")
    (tmp_path / "annotations").mkdir()
    with pytest.raises(FileNotFoundError, match="Annotation file missing"):
        list(iter_split(tmp_path, "train"))


def test_iter_split_yields_paths_loadable_by_load_document() -> None:
    """End-to-end: iter_split + load_document produce the train-split docs."""
    docs = [load_document(p, split="train") for _, p in iter_split(FIXTURE_DIR, "train")]
    assert {d.doc_id for d in docs} == {DOC_ID_SINGLE, DOC_ID_MULTI}
    assert all(d.split == "train" for d in docs)
