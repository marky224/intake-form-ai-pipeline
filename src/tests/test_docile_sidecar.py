"""Tests for the DocILE per-page sidecar builder."""

from __future__ import annotations

from pathlib import Path

from synthetic_data.docile.parse import DocileDocument, DocileField
from synthetic_data.docile.rasterize import RasterizedPage
from synthetic_data.docile.sidecar import SIDECAR_SCHEMA_VERSION, build_docile_sidecar


def _make_doc(
    *,
    doc_id: str = "test-doc-001",
    split: str = "train",
    page_count: int = 2,
    fields: tuple[DocileField, ...] = (),
) -> DocileDocument:
    return DocileDocument(
        doc_id=doc_id,
        split=split,
        page_count=page_count,
        page_sizes_at_200dpi=tuple((1700, 2200) for _ in range(page_count)),
        fields=fields,
    )


def _make_page(*, page_number: int = 1, doc_id: str = "test-doc-001") -> RasterizedPage:
    return RasterizedPage(
        page_number=page_number,
        png_path=Path(f"/render/{doc_id}-p{page_number}.png"),
        width_px=1700,
        height_px=2200,
    )


def test_sidecar_schema_version_is_one() -> None:
    """Locked at v1 — bump only with a deliberate breaking change."""
    assert SIDECAR_SCHEMA_VERSION == 1


def test_sidecar_top_level_shape_matches_uploader_contract() -> None:
    """The fields ``_load_sidecar`` reads must be present at the top level."""
    doc = _make_doc()
    page = _make_page()
    sidecar = build_docile_sidecar(doc, page, image_sha256="a" * 64)

    assert sidecar["schema_version"] == 1
    assert sidecar["image"] == "test-doc-001-p1.png"
    assert sidecar["image_sha256"] == "a" * 64
    assert sidecar["source_id"] == "test-doc-001-p1"
    assert sidecar["page"] == {"number": 1, "width_px": 1700, "height_px": 2200}


def test_sidecar_docile_namespace_holds_vertical_specific_fields() -> None:
    """DocILE-specific data lives under a ``docile`` namespace, not at top level."""
    doc = _make_doc(doc_id="abc123", split="val")
    page = _make_page(doc_id="abc123", page_number=2)
    sidecar = build_docile_sidecar(doc, page, image_sha256="b" * 64)

    docile = sidecar["docile"]
    assert docile["doc_id"] == "abc123"
    assert docile["split"] == "val"
    assert docile["page_number"] == 2
    assert isinstance(docile["fields"], list)


def test_sidecar_fields_filtered_to_current_page() -> None:
    """KILE annotations on other pages don't leak into this page's sidecar.

    Upstream pages are 0-indexed; sidecar pages are 1-indexed. The
    filter is ``field.page + 1 == page.page_number``.
    """
    fields = (
        DocileField(
            page=0, bbox=(0.1, 0.1, 0.2, 0.2), fieldtype="document_id", text="page 1 field"
        ),
        DocileField(page=0, bbox=(0.3, 0.3, 0.4, 0.4), fieldtype="vendor_name", text="another p1"),
        DocileField(page=1, bbox=(0.5, 0.5, 0.6, 0.6), fieldtype="amount_due", text="page 2 field"),
        DocileField(
            page=2, bbox=(0.7, 0.7, 0.8, 0.8), fieldtype="payment_terms", text="page 3 field"
        ),
    )
    doc = _make_doc(page_count=3, fields=fields)

    page1_sidecar = build_docile_sidecar(doc, _make_page(page_number=1), "0" * 64)
    page2_sidecar = build_docile_sidecar(doc, _make_page(page_number=2), "0" * 64)
    page3_sidecar = build_docile_sidecar(doc, _make_page(page_number=3), "0" * 64)

    assert [f["text"] for f in page1_sidecar["docile"]["fields"]] == [
        "page 1 field",
        "another p1",
    ]
    assert [f["text"] for f in page2_sidecar["docile"]["fields"]] == ["page 2 field"]
    assert [f["text"] for f in page3_sidecar["docile"]["fields"]] == ["page 3 field"]


def test_sidecar_field_bbox_is_serialized_as_list_not_tuple() -> None:
    """JSON output uses list (tuple isn't valid JSON syntax)."""
    fields = (DocileField(page=0, bbox=(0.1, 0.2, 0.3, 0.4), fieldtype="document_id", text="X"),)
    doc = _make_doc(fields=fields)
    sidecar = build_docile_sidecar(doc, _make_page(page_number=1), "c" * 64)
    bbox = sidecar["docile"]["fields"][0]["bbox"]
    assert isinstance(bbox, list)
    assert bbox == [0.1, 0.2, 0.3, 0.4]


def test_sidecar_preserves_none_text() -> None:
    """A field with text=null in the upstream JSON keeps None through to sidecar."""
    fields = (DocileField(page=0, bbox=(0.0, 0.0, 0.1, 0.1), fieldtype="date_issue", text=None),)
    doc = _make_doc(fields=fields)
    sidecar = build_docile_sidecar(doc, _make_page(page_number=1), "d" * 64)
    assert sidecar["docile"]["fields"][0]["text"] is None


def test_sidecar_no_fields_for_blank_page() -> None:
    """A page with no annotations gets an empty ``fields`` list, not missing key."""
    doc = _make_doc(fields=())
    sidecar = build_docile_sidecar(doc, _make_page(page_number=1), "e" * 64)
    assert sidecar["docile"]["fields"] == []
