"""Playwright-driven CMS-1500 renderer for synthetic intake forms.

Consumes a ``SyntheaPatient`` from ``synthetic_data.synthea.parse`` and
produces:

* ``<patient_id>.png`` — a rasterized PNG of the rendered CMS-1500
  template at PAGE_WIDTH_PX x PAGE_HEIGHT_PX (8.5x11 inches @ 100 DPI).
* ``<patient_id>.json`` — sidecar JSON capturing the rendered signature
  parameters (mode / font / rotation_deg) plus per-field bounding boxes
  extracted from Playwright's layout engine. Phase 6 eval will compare
  cascade-extracted field bboxes against these as ground truth.

Signature parameters are reproducible byte-for-byte from PROJECT_SEED +
patient_id (see ``signature.py``). The PNG bytes themselves are stable
for a given Chromium version; cross-browser-version stability is not a
project guarantee — bump the pinned Playwright minor version and the
sidecar PNG hashes will shift.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import jinja2
from markupsafe import Markup

from synthetic_data.synthea.parse import SyntheaPatient

from .config import PAGE_HEIGHT_PX, PAGE_WIDTH_PX
from .signature import SVG_INK_BLEED_FILTER, SignatureRender, patient_signature

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

RENDER_DIR = Path(__file__).parent
FONTS_DIR = RENDER_DIR / "fonts"
TEMPLATE_NAME = "template_cms1500.html"
SIDECAR_SCHEMA_VERSION = 1

# (field_name, CSS selector, CMS-1500 box number). The selectors point at
# the [data-field=...] attributes in template_cms1500.html so the
# template is the source of truth for what gets a bbox.
BBOX_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("patient_name", '[data-field="patient_name"]', "2"),
    ("patient_birth_date", '[data-field="patient_birth_date"]', "3"),
    ("patient_address_line", '[data-field="patient_address_line"]', "5"),
    ("patient_city", '[data-field="patient_city"]', "5b"),
    ("patient_state", '[data-field="patient_state"]', "5c"),
    ("patient_postal_code", '[data-field="patient_postal_code"]', "5d"),
    ("patient_phone", '[data-field="patient_phone"]', "5e"),
    ("date_of_current_illness", '[data-field="date_of_current_illness"]', "14"),
    ("diagnosis", '[data-field="diagnosis"]', "21"),
    ("signature", '[data-field="signature"]', "12"),
    ("date_signed", '[data-field="date_signed"]', "12-date"),
)


def _font_data_uri(filename: str) -> str:
    """Base64-encode a vendored font as a ``data:`` URI for inline CSS embedding."""
    raw = (FONTS_DIR / filename).read_bytes()
    return f"data:font/ttf;base64,{base64.b64encode(raw).decode('ascii')}"


def _build_html(patient: SyntheaPatient, signature: SignatureRender) -> str:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(RENDER_DIR)),
        autoescape=True,
        keep_trailing_newline=False,
    )
    template = env.get_template(TEMPLATE_NAME)
    return template.render(
        patient=patient,
        # Both pre-escaped by signature.py / signature module constant;
        # marking Markup prevents Jinja's autoescape from double-escaping.
        signature_html=Markup(signature.html_snippet),
        svg_filter=Markup(SVG_INK_BLEED_FILTER),
        caveat_data_uri=_font_data_uri("Caveat-Regular.ttf"),
        sacramento_data_uri=_font_data_uri("Sacramento-Regular.ttf"),
        homemade_apple_data_uri=_font_data_uri("HomemadeApple-Regular.ttf"),
        page_width=PAGE_WIDTH_PX,
        page_height=PAGE_HEIGHT_PX,
    )


def render_one(patient: SyntheaPatient, output_dir: Path) -> tuple[Path, Path]:
    """Render a single patient with a one-shot browser launch.

    Convenience wrapper for ad-hoc renders (tests, dev). For the full
    500-doc corpus use ``render_batch`` so the Chromium process is
    reused across documents (~1-2 s vs ~10 s per doc).
    """
    from playwright.sync_api import sync_playwright

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": PAGE_WIDTH_PX, "height": PAGE_HEIGHT_PX})
        try:
            return _render_in_context(ctx, patient, output_dir)
        finally:
            browser.close()


def render_batch(patients: Iterable[SyntheaPatient], output_dir: Path) -> list[tuple[Path, Path]]:
    """Render N patients reusing a single Chromium process.

    Cuts cold-start cost from ~10 s per doc to ~1-2 s. For 500 docs
    this is the difference between an 80-minute run and a 10-minute run.
    """
    from playwright.sync_api import sync_playwright

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[tuple[Path, Path]] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": PAGE_WIDTH_PX, "height": PAGE_HEIGHT_PX})
        try:
            for patient in patients:
                results.append(_render_in_context(ctx, patient, output_dir))
        finally:
            browser.close()
    return results


def _render_in_context(
    context: BrowserContext, patient: SyntheaPatient, output_dir: Path
) -> tuple[Path, Path]:
    full_name = f"{patient.given_name} {patient.family_name}".strip()
    signature = patient_signature(patient.patient_id, full_name)
    html = _build_html(patient, signature)

    page = context.new_page()
    try:
        page.set_content(html, wait_until="load")
        # Wait for vendored handwriting fonts to finish loading so
        # measurements + screenshot capture the styled glyphs.
        page.evaluate("document.fonts.ready")

        field_bboxes = _extract_field_bboxes(page)

        png_path = output_dir / f"{patient.patient_id}.png"
        page.screenshot(path=str(png_path), full_page=True)
    finally:
        page.close()

    sidecar = _build_sidecar(patient, signature, png_path, field_bboxes)
    sidecar_path = output_dir / f"{patient.patient_id}.json"
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True), encoding="utf-8")
    return png_path, sidecar_path


def _extract_field_bboxes(page: Any) -> list[dict]:
    fields: list[dict] = []
    for field_name, selector, cms_box in BBOX_FIELDS:
        locator = page.locator(selector).first
        if locator.count() == 0:
            continue
        box = locator.bounding_box()
        if box is None:
            continue
        value = (locator.text_content() or "").strip()
        fields.append(
            {
                "name": field_name,
                "cms1500_box": cms_box,
                "value": value,
                "bbox": {
                    "x1": round(float(box["x"]), 2),
                    "y1": round(float(box["y"]), 2),
                    "x2": round(float(box["x"] + box["width"]), 2),
                    "y2": round(float(box["y"] + box["height"]), 2),
                },
            }
        )
    return fields


def _build_sidecar(
    patient: SyntheaPatient,
    signature: SignatureRender,
    png_path: Path,
    field_bboxes: list[dict],
) -> dict:
    image_sha256 = hashlib.sha256(png_path.read_bytes()).hexdigest()
    return {
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "image": png_path.name,
        "image_sha256": image_sha256,
        "patient_id": patient.patient_id,
        "page": {
            "number": 1,
            "width_px": PAGE_WIDTH_PX,
            "height_px": PAGE_HEIGHT_PX,
        },
        "signature": {
            "mode": signature.mode,
            "font": signature.font,
            "rotation_deg": round(float(signature.rotation_deg), 3),
        },
        "fields": field_bboxes,
    }
