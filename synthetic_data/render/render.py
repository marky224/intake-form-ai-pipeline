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
import functools
import hashlib
import json
import re
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


# Filenames are derived from patient_id. Synthea writes UUID-style ids
# (hex digits + dashes only) which are inherently filesystem-safe, but
# the renderer accepts arbitrary SyntheaPatient inputs — sanitize as
# defense-in-depth against path traversal and overwriting files outside
# output_dir.
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9._-]")

# Windows reserves these basenames even with an extension (CON.png is
# reserved the same way CON is). Linux ignores them but the renderer
# should produce filenames that work on either host so artifacts can be
# copied across.
_WINDOWS_RESERVED_NAMES = frozenset(
    {
        "con", "prn", "aux", "nul",
        "com0", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
        "lpt0", "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
    }
)  # fmt: skip


@functools.cache
def _font_data_uri(filename: str) -> str:
    """Base64-encode a vendored font as a ``data:`` URI for inline CSS embedding.

    Cached: the 3 vendored fonts are read + encoded exactly once per
    process, saving ~600 KB of disk reads + base64 encoding per render
    across a 500-doc batch.
    """
    raw = (FONTS_DIR / filename).read_bytes()
    return f"data:font/ttf;base64,{base64.b64encode(raw).decode('ascii')}"


@functools.cache
def _jinja_env() -> jinja2.Environment:
    """Module-level Jinja environment.

    Cached so the FileSystemLoader is constructed once per process
    rather than per render — autoescape config and template loader
    don't vary across renders.
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(str(RENDER_DIR)),
        autoescape=True,
        keep_trailing_newline=False,
    )


def _safe_stem(patient_id: str) -> str:
    """Sanitize a patient_id for use as an output filename stem.

    Replaces any char outside ``[A-Za-z0-9._-]`` with ``_``. Falls back
    to a sha256-prefix when the cleaned result would be:

    * empty or dot-only (``.``, ``..``) — invalid as a basename
    * a Windows-reserved name (``CON``, ``NUL``, ``COM1`` etc.) —
      reserved even with an extension, so ``CON.png`` is also illegal
    * trailing dot — NTFS silently strips trailing dots, collapsing
      ``foo.`` and ``foo`` to the same filename on Windows hosts

    The whitelist regex already strips trailing spaces, the other
    Windows-illegal char that NTFS treats specially.

    The original ``patient_id`` is preserved verbatim inside the
    sidecar JSON's ``patient_id`` field, so the audit trail survives
    regardless of how aggressively the stem is rewritten.
    """
    cleaned = _SAFE_STEM_RE.sub("_", patient_id)
    if (
        not cleaned
        or cleaned in {".", ".."}
        or cleaned.lower() in _WINDOWS_RESERVED_NAMES
        or cleaned.endswith(".")
    ):
        cleaned = hashlib.sha256(patient_id.encode("utf-8")).hexdigest()[:16]
    return cleaned


def _render_stem(patient_id: str) -> str:
    """Collision-safe output stem for a given patient_id.

    Combines the sanitized stem with a stable 8-char sha256 prefix
    derived from the *unsanitized* patient_id. Two patient_ids that
    normalize to the same stem (e.g., ``a/b`` and ``a_b``) still
    produce distinct filenames because the hash suffix differs.
    Deterministic: the same patient_id always yields the same stem,
    so re-rendering a subset is byte-stable.
    """
    sanitized = _safe_stem(patient_id)
    disambiguator = hashlib.sha256(patient_id.encode("utf-8")).hexdigest()[:8]
    return f"{sanitized}-{disambiguator}"


def _build_html(patient: SyntheaPatient, signature: SignatureRender) -> str:
    template = _jinja_env().get_template(TEMPLATE_NAME)
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

        stem = _render_stem(patient.patient_id)
        png_path = output_dir / f"{stem}.png"
        page.screenshot(path=str(png_path), full_page=True)
    finally:
        page.close()

    sidecar = _build_sidecar(patient, signature, png_path, field_bboxes)
    sidecar_path = output_dir / f"{stem}.json"
    sidecar_path.write_text(json.dumps(sidecar, indent=2, sort_keys=True), encoding="utf-8")
    return png_path, sidecar_path


def _extract_field_bboxes(page: Any) -> list[dict]:
    fields: list[dict] = []
    for field_name, selector, cms_box in BBOX_FIELDS:
        locator = page.locator(selector)
        match_count = locator.count()
        if match_count == 0:
            # Field absent from this render (e.g., template omitted it).
            continue
        if match_count > 1:
            # Each data-field attribute must be unique within the
            # template. A duplicate is a template-authoring bug we
            # should surface immediately, not silently bbox only the
            # first occurrence and lose the rest.
            raise RuntimeError(
                f"data-field selector {selector!r} matched {match_count} elements; "
                f"expected exactly 1 (BBOX_FIELDS entry {field_name!r})"
            )
        first = locator.first
        box = first.bounding_box()
        if box is None:
            # display:none / zero-size / detached element with a
            # data-field attribute is a template bug — surface it
            # rather than emit an incomplete sidecar that silently
            # drops the field's ground truth.
            raise RuntimeError(
                f"data-field selector {selector!r} matched an element with no "
                f"layout box (BBOX_FIELDS entry {field_name!r})"
            )
        value = (first.text_content() or "").strip()
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
