"""Tier 1 PaddleOCR-VL local provider.

Pinned model: **PaddleOCR-VL-1.5** (released 2026-01-29; OmniDocBench v1.5 =
94.5%; irregular-shaped bbox localization; seal recognition; robust to scan
skew/warping/screen-photography/complex illumination). Pulled via the
``paddleocr`` Python package — bumping the package version is what pins the
checkpoint, since ``PaddleOCRVL()`` constructs from the package's bundled
weights.

**Device pin:** ``gpu:1`` (RTX 4060 Ti, 16 GB). GPU 0 (RTX 4080, 16 GB) is
reserved for Tier 2 Qwen 2.5 VL 7B (V1) / Tier 3 Qwen 2.5 VL 32B so the
cascade can hold the larger models resident across a batch without paging.

**Role in the cascade:** cheap-and-fast first-pass extraction, sub-second per
page. A value that passes its format validator (a date, ZIP or state) and
was read under a label naming its field specifically gets
``VALIDATED_CONFIDENCE`` (0.9) and is final at Tier 1. Every other value is
capped just under the orchestrator's 0.85 Tier 1→2 gate and escalates to
Tier 2.

**Cost:** $0.00/call (local inference).

**Architectural note: PaddleOCR-VL is a layout parser, NOT a prompted VL
model.** ``pipeline.predict(image)`` returns ``parsing_res_list`` — a list of
layout blocks ``{block_bbox, block_label, block_content}`` — and ignores any
``prompt`` argument. The "VL" suffix is misleading; PaddleOCR-VL is positioned
as PP-Structure-V3's VL-trained successor (more robust OCR), not a Qwen-VL-
style prompt-and-extract model. Field-name extraction is therefore a downstream
step inside this provider: each ``block_content`` is matched against the
project's ``alias_table_seed.json`` to map recognized labels to canonical
field names. The locked ``extract(png,
form_cls) → ProviderResult[T]`` Protocol shape stays intact — Phase 5's
orchestrator and the downstream Tier 2 / Tier 3 providers don't need to know
that Tier 1's "field-by-field JSON" is synthesized post-OCR rather than
prompted out of the model.

**Lazy imports:** ``paddle`` and ``paddleocr`` are imported inside
``_load_paddleocr_vl_pipeline`` rather than at module top-level so the
``cascade`` package loads cleanly on machines without the GPU stack (e.g.,
CI). Cached-replay tests never touch the live path; only the
``EVAL_LIVE=true`` workflow on the build machine pays the import + load cost.
"""

from __future__ import annotations

import hashlib
import html as html_lib
import io
import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, date, datetime
from functools import lru_cache
from html.parser import HTMLParser
from itertools import pairwise
from typing import Any, Literal, get_args, get_origin

from PIL import Image
from pydantic import ValidationError

from _paths import repo_root
from cascade import eval_cache
from cascade.providers._base import ProviderResult, T
from cascade.providers._qwen_vl import _extractable_fields
from cascade.validators import validate
from intake_schemas import (
    BoundingBox,
    ExtractedField,
    FormMetadata,
    TierId,
    get_field_metadata,
)

#: Stable provider identifier. Used as the eval-cache subdirectory name
#: (``tests/fixtures/eval-cache/tier1_paddleocr_local/<sha>.json``).
PROVIDER_NAME = "tier1_paddleocr_local"

#: Cascade tier per ``intake_schemas.TierId``.
TIER: TierId = 1

#: Pinned PaddleOCR-VL checkpoint. Record-only — the actual checkpoint is
#: bundled with the ``paddleocr`` package version, so version-bumping the
#: package is what moves the pin.
PADDLEOCR_VL_VERSION = "PaddleOCR-VL-1.5"

#: PaddlePaddle device string. ``gpu:1`` is the RTX 4060 Ti per the locked
#: device split (GPU 0 reserved for Tier 2 + Tier 3 Qwen).
PADDLE_DEVICE = "gpu:1"

#: Pipeline version stamped onto stub ``FormMetadata``. Phase 5's orchestrator
#: overrides this with the live pipeline version; the stub keeps the form
#: instantiable since FormMetadata.pipeline_version is required.
PIPELINE_VERSION = f"tier1-paddleocr-vl-1.5@{TIER}"

#: Minimum alias-match score (0.0-1.0) required to accept a block → field
#: mapping. Below this, the block is dropped (the field stays unattempted:
#: ``tier_used=None``). 0.3 lets clean "Label: value" blocks pass (e.g.
#: "First Name: Jane" matches alias "First Name" at len(alias)/len(text) ≈
#: 0.55) while rejecting blocks where the alias is a tiny incidental
#: substring of a longer phrase. Phase 6 eval-sweep tunes if needed.
ALIAS_MATCH_THRESHOLD = 0.3

#: Separators stripped between a recognized label and its value text. Matches
#: one leading run of ``:``, ``-``, en-dash (U+2013), em-dash (U+2014), or
#: whitespace at the start of the post-label remainder. ``re.sub(..., count=1)``
#: applied to the remainder slice so a value like "2024-01-15" doesn't get its
#: internal dashes mangled.
_LABEL_VALUE_SEPARATORS = re.compile("^[\\s:\\-\u2013\u2014\t]+")

#: Confidence for a Tier-1 value that passes its format validator (a date,
#: ZIP or state), read from an aligned label/value pair whose label names the
#: field specifically (not the generic one-word "Date"). It clears the
#: orchestrator's 0.85 Tier 1→2 gate, so the value is final at Tier 1. On the
#: cached replay these values are right in every case: 256/256 dev cells,
#: 250/250 test (dates of birth, states, ZIPs).
VALIDATED_CONFIDENCE = 0.9

#: Every other Tier-1 value keeps its alias-match score, capped just under the
#: 0.85 gate so it always escalates to Tier 2. The score measures how well
#: the *label* matched an alias, not whether the value is right, so it never
#: finalizes a value on its own.
UNVALIDATED_CONFIDENCE_CAP = 0.84

#: Caps on what the table parser reads from one ``<table>`` block. The OCR
#: text comes from uploaded forms, so a malformed or hostile block must not
#: blow up time or memory. The cached corpus peaks at 24 rows, 7 cells per
#: row and colspan 10.
_MAX_TABLE_ROWS = 200
_MAX_TABLE_COLS = 32
_MAX_CELL_CHARS = 512

#: A box number in front of a form label ("5C. STATE", "1A.INSURED'S",
#: "21.DIAGNOSIS") including OCR misreads seen in the corpus ("58.", "SC.",
#: "XC.", "50,"). Stripped before the alias score so the score measures the
#: label text, not the box number. Bounded quantifiers only: linear time.
_BOX_NUMBER = re.compile(r"^\s*(?:[0-9][0-9A-Z]{0,2}|[A-Z][A-E])\s*[.,]\s*")

#: US-style date as printed on the form (MM/DD/YYYY). Matched with
#: ``fullmatch`` on one whitespace-free token, so it is linear time.
_US_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")

#: Words for label-mention checks ("LAST, FIRST" → ``LAST``, ``FIRST``).
_WORD = re.compile(r"[A-Z0-9']+")

#: Fields a "LAST, FIRST" name box packs together.
_NAME_PAIR = ("first_name", "last_name")

#: Fields Tier 1 *covers*, per form class: the CMS-1500 boxes the table
#: parser has rules for (box 2 name, 3 date of birth + sex, 5 street, 5b to 5e
#: city / state / ZIP / phone, 12 date signed, 21 diagnosis). The
#: orchestrator compares Tier 1's reading of a covered field with Tier 2's
#: and sends a disagreement, or a covered field Tier 1 found nothing for, to
#: Tier 3. An uncovered field has only Tier 2's reading. Other on-form boxes
#: (insured ID, insured name, relationship, account number, physician) and
#: every ``BusinessDocumentForm`` field are uncovered; a form class with no
#: covered field keeps the second read of every Tier-2 field.
COVERED_FIELDS: dict[str, frozenset[str]] = {
    "HealthcareIntakeForm": frozenset(
        {
            "first_name",
            "last_name",
            "address_street",
            "address_city",
            "address_state",
            "address_zip",
            "phone",
            "reason_for_visit",
            "date_of_birth",
            "date_signed",
            "sex",
        }
    ),
}


def covered_fields(form_cls: type) -> frozenset[str]:
    """The fields Tier 1 covers for ``form_cls`` (see ``COVERED_FIELDS``)."""
    return COVERED_FIELDS.get(form_cls.__name__, frozenset())


#: ``alias_table_seed.json`` path. Lives at the **repo root** (not under
#: ``src/``) by canonical-artifact contract — see memory project_src_layout.
ALIAS_TABLE_PATH = repo_root() / "alias_table_seed.json"

#: Map from form class name → set of seed verticals to draw aliases from.
#: Each form class pulls aliases from the union of these verticals' seed
#: records. ``base`` always included for cross-vertical fields (first_name,
#: address_*, etc.). BusinessDocumentForm has no seed records (DocILE's
#: KILE taxonomy isn't in the seed), so it falls through to the synthetic-
#: alias-from-canonical-name path in ``_alias_map_for_form``.
_FORM_VERTICAL_MAP: dict[str, frozenset[str]] = {
    "HealthcareIntakeForm": frozenset({"base", "healthcare"}),
    "InsuranceIntakeForm": frozenset({"base", "insurance"}),
    "HRIntakeForm": frozenset({"base", "hr"}),
    "BusinessDocumentForm": frozenset({"base"}),
}


@lru_cache(maxsize=1)
def _load_alias_table_raw() -> list[dict[str, Any]]:
    """Load + return the ``fields`` array from ``alias_table_seed.json``.

    Cached for the lifetime of the process — the seed file is checked in
    and immutable at runtime. Tests that need a different seed
    ``monkeypatch`` ``ALIAS_TABLE_PATH`` and call ``_load_alias_table_raw.cache_clear()``.

    The live correction overlay (``rag.aliases``) is unioned on top of the
    frozen seed. It is empty during the progressive-partition sweep
    (suppressed) so the portfolio F1 chart only ever reflects the seed.
    """
    from rag.aliases import overlay_records

    seed_fields = json.loads(ALIAS_TABLE_PATH.read_text(encoding="utf-8"))["fields"]
    return seed_fields + overlay_records()


def _alias_map_for_form(form_cls: type[T]) -> dict[str, list[str]]:
    """Build ``{canonical_name: [aliases]}`` for ``form_cls``'s vertical.

    Aliases come from the union of seed records whose ``vertical`` is in
    ``_FORM_VERTICAL_MAP[form_cls.__name__]`` AND whose ``canonical_name`` is a
    field on the form. Order: first-seen wins (base records typically come
    before vertical-specific records in the seed JSON), but duplicates are
    suppressed.

    For canonical_names on the form that have no seed record (e.g., all of
    BusinessDocumentForm's DocILE-derived fields), synthesize a single alias
    from the canonical_name itself (``vendor_name`` → ``Vendor Name``). Crude
    but functional for unit tests; real-world DocILE extraction quality lives
    at Phase 4 PR (c)'s Tier 2 Qwen 7B prompted extraction, not at Tier 1.
    """
    canonical_in_form = set(get_field_metadata(form_cls).keys())
    vertical_filter = _FORM_VERTICAL_MAP.get(form_cls.__name__, frozenset({"base"}))
    raw = _load_alias_table_raw()

    result: dict[str, list[str]] = {}
    for record in raw:
        if record["vertical"] not in vertical_filter:
            continue
        name = record["canonical_name"]
        if name not in canonical_in_form:
            continue
        bucket = result.setdefault(name, [])
        for alias in record["aliases"]:
            if alias not in bucket:
                bucket.append(alias)

    # Fallback: synthesize an alias from the canonical_name for any field
    # not covered by the seed.
    for name in canonical_in_form:
        if name not in result:
            result[name] = [name.replace("_", " ").title()]

    return result


def _strip_label_prefix(text: str, alias: str) -> str | None:
    """Strip ``alias`` (case-insensitive) + a separator from ``text``'s start-or-mid.

    Returns the remaining value text, or None if the alias isn't found or the
    remainder is empty. The match finds the FIRST occurrence of the alias
    anywhere in ``text`` — block_content may include some preamble before the
    label, but typically the alias appears at the start.
    """
    if not alias.strip():
        return None
    upper_text = text.upper()
    upper_alias = alias.upper().strip()
    pos = upper_text.find(upper_alias)
    if pos < 0:
        return None
    remainder = text[pos + len(upper_alias) :]
    remainder = _LABEL_VALUE_SEPARATORS.sub("", remainder, count=1).rstrip()
    return remainder if remainder else None


def _match_block(
    text: str, alias_map: dict[str, list[str]]
) -> tuple[str, str | None, float] | None:
    """Match a block's text against the alias table.

    Returns ``(canonical_name, value_or_None, score)`` for the best match, or
    None if no match clears ``ALIAS_MATCH_THRESHOLD``.

    Scoring: ``len(alias) / len(text)`` clamped to [0, 1]. Longer alias
    relative to block text = higher confidence. A label-only block (text
    equals the alias) scores 1.0 but ``value`` will be None — Phase 5's
    orchestrator can pair label-only blocks with their spatially-adjacent
    value blocks; V1's single-block heuristic skips them.

    Tie-breaking on equal scores: first-seen wins. Aliases are iterated in
    seed order, so position 0 (the canonical phrasing) wins over later
    variants of the same field.
    """
    text_stripped = text.strip()
    if not text_stripped:
        return None

    text_len = len(text_stripped)
    best: tuple[str, str | None, float] | None = None

    for canonical_name, aliases in alias_map.items():
        for alias in aliases:
            alias_stripped = alias.strip()
            if not alias_stripped:
                continue
            if alias_stripped.upper() not in text_stripped.upper():
                continue
            score = min(len(alias_stripped) / text_len, 1.0)
            if best is None or score > best[2]:
                value = _strip_label_prefix(text_stripped, alias_stripped)
                best = (canonical_name, value, score)

    if best is not None and best[2] >= ALIAS_MATCH_THRESHOLD:
        return best
    return None


def _match_label_only(text: str, alias_map: dict[str, list[str]]) -> tuple[str, float] | None:
    """Match a known-to-be-label cell against the alias map.

    Like ``_match_block`` but skips the value-strip step — when the caller
    has already separated label and value into different table cells, the
    label cell has no trailing value to extract. A leading box number
    ("5C. STATE") is stripped first, so the score is the share of the label
    text the alias covers. Returns ``(canonical_name, score)`` of the best
    match or None below threshold.
    """
    match = _best_label_match(text, alias_map)
    return None if match is None else (match[0], match[1])


def _best_label_match(
    text: str,
    alias_map: dict[str, list[str]],
    *,
    whole_words: bool = False,
) -> tuple[str, float, str] | None:
    """``(canonical_name, score, alias)`` for the best alias in ``text``.

    See :func:`_match_label_only`. With ``whole_words`` the alias must
    appear as whole words ("DATE" in "ADMIT DATE", not in "UPDATED").
    """
    text_stripped = _strip_box_number(text[:_MAX_CELL_CHARS]).strip()
    if not text_stripped:
        return None
    text_len = len(text_stripped)
    text_upper = text_stripped.upper()
    text_words = _words(text_stripped) if whole_words else []
    best: tuple[str, float, str] | None = None
    for canonical_name, aliases in alias_map.items():
        for alias in aliases:
            alias_stripped = alias.strip()
            if not alias_stripped:
                continue
            if alias_stripped.upper() not in text_upper:
                continue
            if whole_words and _word_index(text_words, _words(alias_stripped)) is None:
                continue
            score = min(len(alias_stripped) / text_len, 1.0)
            if best is None or score > best[1]:
                best = (canonical_name, score, alias_stripped)
    if best is not None and best[1] >= ALIAS_MATCH_THRESHOLD:
        return best
    return None


def _generic_aliases(alias_map: dict[str, list[str]]) -> frozenset[str]:
    """Aliases that don't name their field specifically: the alias's words
    appear, as whole words, inside an alias of a *different* field ("Date"
    for ``date_signed`` sits inside "Date of Birth"). A label matched only
    by such an alias can't finalize a value at Tier 1. Upper-cased."""
    phrases = {
        name: [_words(a) for a in aliases if a.strip()] for name, aliases in alias_map.items()
    }
    generic: set[str] = set()
    for name, aliases in alias_map.items():
        for alias in aliases:
            words = _words(alias)
            if words and any(
                _word_index(other, words) is not None
                for other_name, others in phrases.items()
                if other_name != name
                for other in others
            ):
                generic.add(alias.strip().upper())
    return frozenset(generic)


class _TableHTMLParser(HTMLParser):
    """Parse ``<table><tr><td>...</td></tr></table>`` into rows of cell text.

    Stdlib-only. HTML entities (``&#x27;``, ``&amp;``, ``&lt;``) get unescaped
    via ``html.unescape``. Nested tags inside cells are ignored — only the
    raw text content of each ``<td>`` is captured. ``spans`` holds each
    cell's ``colspan`` (1 when absent or malformed, at most
    ``_MAX_TABLE_COLS``). Rows past ``_MAX_TABLE_ROWS`` are ignored.
    """

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.spans: list[list[int]] = []
        self._row: list[str] | None = None
        self._row_spans: list[int] = []
        self._cell: list[str] | None = None
        self._span = 1

    def handle_starttag(self, tag: str, attrs: list[Any]) -> None:
        if tag == "tr" and len(self.rows) < _MAX_TABLE_ROWS:
            self._row = []
            self._row_spans = []
        elif tag == "td" and self._row is not None:
            self._cell = []
            self._span = _colspan(attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "td" and self._row is not None and self._cell is not None:
            text = html_lib.unescape("".join(self._cell)).strip()
            self._row.append(text)
            self._row_spans.append(self._span)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self.spans.append(self._row_spans)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _colspan(attrs: list[Any]) -> int:
    """A ``<td>``'s ``colspan``: 1 when absent or not a small positive int."""
    for name, value in attrs:
        if name == "colspan" and isinstance(value, str) and value.strip().isdecimal():
            return max(1, min(int(value.strip()), _MAX_TABLE_COLS))
    return 1


def _feed_table(html_content: str) -> _TableHTMLParser | None:
    parser = _TableHTMLParser()
    try:
        parser.feed(html_content)
    except Exception:
        return None
    return parser


def _parse_html_table(html_content: str) -> list[list[str]]:
    """Return ``<table>`` HTML as ``[[cell_text, ...], ...]``. ``[]`` on parse failure.

    One entry per ``<td>`` as written (``colspan`` is not expanded), which is
    what the router's OCR-line view wants. Field pairing uses
    :func:`_parse_html_table_grid`.
    """
    parser = _feed_table(html_content)
    return parser.rows if parser is not None else []


def _table_cells(html_content: str) -> list[list[tuple[str, int, int]]]:
    """Return ``<table>`` HTML as rows of ``(text, first_column, end_column)``
    cells, ``end_column`` exclusive, from each cell's ``colspan``.

    Rows stop at ``_MAX_TABLE_COLS`` columns and cell text is cut to
    ``_MAX_CELL_CHARS``. ``[]`` on parse failure.
    """
    parser = _feed_table(html_content)
    if parser is None:
        return []
    table: list[list[tuple[str, int, int]]] = []
    for cells, spans in zip(parser.rows, parser.spans, strict=True):
        row: list[tuple[str, int, int]] = []
        col = 0
        for text, span in zip(cells, spans, strict=True):
            if col >= _MAX_TABLE_COLS:
                break
            end = min(col + span, _MAX_TABLE_COLS)
            row.append((text[:_MAX_CELL_CHARS], col, end))
            col = end
        table.append(row)
    return table


def _parse_html_table_grid(html_content: str) -> list[list[str]]:
    """Return ``<table>`` HTML as a column grid: each cell repeated ``colspan``
    times, so cells in the same visual column share an index across rows.

    Rows are capped at ``_MAX_TABLE_COLS`` cells and cell text at
    ``_MAX_CELL_CHARS``. ``[]`` on parse failure.
    """
    return [
        [text for text, start, end in row for _ in range(end - start)]
        for row in _table_cells(html_content)
    ]


def _iter_table_label_value_pairs(html_content: str) -> Iterator[tuple[str, str]]:
    """Yield (label_cell, value_cell) candidate pairs from a table HTML block.

    See :func:`_table_pairs`, which also says whether each pair is aligned.
    """
    for label, value, _aligned in _table_pairs(html_content):
        yield label, value


def _table_pairs(html_content: str) -> Iterator[tuple[str, str, bool]]:
    """Yield ``(label_cell, value_cell, aligned)`` pairs from a table block.

    Strategy: pair each cell with every non-empty cell directly above it
    that shares a column on the colspan grid (:func:`_table_cells`). A pair
    is **aligned** when the label cell sits above the value cell's first
    column. A value stretched under several labels pairs with each of them
    but is aligned only with the first: box 14's date runs under "14. DATE
    OF CURRENT ILLNESS" and "15. OTHER DATE", and only the box-14 pairing is
    aligned. A label spanning several value cells ("BIRTH DATE / SEX" over a
    date cell and a sex cell) is aligned with each of them. Pairs with an
    empty side are skipped; each pair is yielded once (aligned if any of
    its occurrences is). Cells that don't alias-match a known field are
    filtered downstream by the matcher.

    Unaligned pairs are kept because PaddleOCR-VL's columns drift: a street
    value can start under the insured-name label and end under the address
    label. They lose to an aligned reading of the same field, never
    finalize at Tier 1, and are dropped when only a generic alias matched
    the label (:func:`_parse_response`).

    Known limitation: PaddleOCR-VL's table-to-HTML conversion sometimes
    merges or drops cells (a ZIP cell that also holds the state, a missing
    label cell), so some pairs are still mis-aligned. They reach the matcher
    and lose on score or on the field validator; every field Tier 1
    doesn't finalize goes on to Tier 2.
    """
    rows = _table_cells(html_content)
    for upper, lower in pairwise(rows):
        pairs: dict[tuple[str, str], bool] = {}
        for value, first, end in lower:
            if not value:
                continue
            for label, start, stop in upper:
                if label and start < end and stop > first:
                    aligned = start <= first < stop
                    pairs[(label, value)] = pairs.get((label, value), False) or aligned
        for (label, value), aligned in pairs.items():
            yield label, value, aligned


def _strip_box_number(label: str) -> str:
    """Drop a leading form box number ("5C. STATE" → "STATE")."""
    return _BOX_NUMBER.sub("", label, count=1)


def _words(text: str) -> list[str]:
    """Upper-cased words of ``text`` (letters, digits, apostrophes)."""
    return _WORD.findall(text.upper())


def _word_index(words: list[str], phrase: list[str]) -> int | None:
    """Index where ``phrase`` starts as a whole-word run in ``words``, or None."""
    n = len(phrase)
    if not n:
        return None
    for i in range(len(words) - n + 1):
        if words[i : i + n] == phrase:
            return i
    return None


def _mentions(words: list[str], alias_words: dict[str, list[list[str]]]) -> dict[str, int]:
    """``{field: word index}`` for every field an alias of which appears in
    ``words`` as whole words ("LAST" in "NAME (LAST, FIRST)", but not "ST"
    inside "STREET"). The index is that of the field's *last* mention, so a
    shared lead-in ("PATIENT'S NAME") doesn't hide where "LAST" and "FIRST"
    sit."""
    found: dict[str, int] = {}
    for name, phrases in alias_words.items():
        hits = [i for p in phrases if (i := _word_index(words, p)) is not None]
        if hits:
            found[name] = max(hits)
    return found


def _coerce(inner: Any, text: str) -> Any:
    """Type a Tier-1 string for its field. A date field takes MM/DD/YYYY (as
    printed on US forms) or ISO and returns a ``date``, or None if it is
    neither. Every other type returns the text unchanged; Pydantic validates
    it when the form is built."""
    if inner is not date:
        return text
    token = text.strip()
    m = _US_DATE.fullmatch(token)
    try:
        if m:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        return date.fromisoformat(token)
    except ValueError:
        return None


def _literal_value(inner: Any, token: str) -> str | None:
    """``token`` as one of a string ``Literal``'s values (case-insensitive)."""
    if get_origin(inner) is not Literal:
        return None
    for arg in get_args(inner):
        if isinstance(arg, str) and arg.casefold() == token.casefold():
            return arg
    return None


def _cell_readings(
    label: str,
    value: str,
    field: str,
    kinds: dict[str, Any],
    alias_words: dict[str, list[list[str]]],
) -> list[tuple[str, Any]]:
    """``(field, value)`` readings from one label/value cell pair.

    ``field`` is the label's best alias match. A form often packs several
    fields into one box, and the label names them, so other fields whose
    aliases appear in the label as whole words are read from the same value:

    - **Name**: the label names both last and first name and the value has
      one comma ("LAST, FIRST" → ``Doe, Jane``): split in the label's order.
    - **Date + more**: under a date field's label, a leading date token
      goes to the date field and any token that is one of a named
      ``Literal`` field's values goes to that field ("06/13/1955 F" under
      "BIRTH DATE / SEX"; also a lone "F" when the box splits into two
      cells).
    - **Validator rescue**: a value that fails the field's validator but has
      exactly one token that passes it ("MA 02481" under "ZIP CODE"): that
      token is the reading. Fields the label also names get their own
      passing token.

    Anything else is the whole value for ``field``.
    """
    mentioned = _mentions(_words(label), alias_words)
    here = mentioned.pop(field, None)

    if field in _NAME_PAIR and here is not None and value.count(",") == 1:
        other = next(n for n in _NAME_PAIR if n != field)
        before, _, after = value.partition(",")
        first_part, second_part = before.strip(), after.strip()
        if other in mentioned and first_part and second_part:
            order = (field, other) if here < mentioned[other] else (other, field)
            return [(order[0], first_part), (order[1], second_part)]

    tokens = value.split()
    if kinds.get(field) is date and tokens:
        readings: list[tuple[str, Any]] = []
        rest = tokens
        if _coerce(date, tokens[0]) is not None:
            readings.append((field, tokens[0]))
            rest = tokens[1:]
        for token in rest:
            for name in mentioned:
                literal = _literal_value(kinds.get(name), token)
                if literal is not None:
                    readings.append((name, literal))
        if readings:
            return readings

    if validate(field, value) is False and len(tokens) > 1:
        readings = []
        for name in (field, *mentioned):
            passing = [t for t in tokens if validate(name, t) is True]
            if len(passing) == 1:
                readings.append((name, passing[0]))
        if readings:
            return readings

    return [(field, value)]


def _same_cell_readings(
    cell: str,
    alias_map: dict[str, list[str]],
    kinds: dict[str, Any],
    alias_words: dict[str, list[list[str]]],
) -> list[tuple[str, Any, float, str]]:
    """A label and its value in one cell. Two layouts:

    - **Stacked lines** ("3. BIRTH DATE / SEX\n06/28/1942\nM",
      "SIGNED\nDATE\n04/01/2026"): the label is the leading lines that
      best alias-match a field — every split point is tried, the best label
      score wins — and the remaining lines are the value, read by
      :func:`_cell_readings`.
    - **One line** ("DATE 03/01/2026"): the last whitespace-separated token
      is the value and the rest must alias-match a field as whole words
      ("UPDATED 01/02/2026" is not a date label). Only a value that passes
      that field's validator is read, so free text never splits this way.

    Returns ``[(field, value, score, matched_alias), ...]``; the alias is
    the one that matched the label (for a field the label only names, the
    label's own best alias).
    """
    cell = cell[:_MAX_CELL_CHARS]
    lines = [line.strip() for line in cell.split("\n") if line.strip()]
    if len(lines) > 1:
        best: tuple[str, float, str, str, str] | None = None
        for k in range(1, len(lines)):
            label = " ".join(lines[:k])
            match = _best_label_match(label, alias_map)
            if match is not None and (best is None or match[1] > best[1]):
                best = (match[0], match[1], match[2], label, " ".join(" ".join(lines[k:]).split()))
        if best is None:
            return []
        name, score, alias, label, value = best
        return [
            (n, v, score, alias) for n, v in _cell_readings(label, value, name, kinds, alias_words)
        ]
    parts = cell.rsplit(maxsplit=1)
    if len(parts) != 2:
        return []
    match = _best_label_match(parts[0], alias_map, whole_words=True)
    if match is None:
        return []
    name, score, alias = match
    typed = _coerce(kinds.get(name), parts[1])
    if typed is None or validate(name, typed) is not True:
        return []
    return [(name, typed, score, alias)]


def _finalizes(name: str, value: Any) -> bool:
    """True for a value Tier 1 may finalize: a date, ZIP or state that
    passes its validator. The caller also requires the label to name the
    field specifically (see :func:`_generic_aliases`)."""
    if not (isinstance(value, date) or name.endswith(("zip", "state"))):
        return False
    return validate(name, value) is True


def _parse_bbox(bbox: Any, page_size_px: tuple[int, int] | None = None) -> BoundingBox | None:
    """Parse a 4-tuple bbox, normalizing pixel coords to [0, 1] when possible.

    Accepts ``[x1, y1, x2, y2]`` as list/tuple of numerics. PaddleOCR-VL's
    real output is pixel coords; the stub fixtures in this repo use
    normalized [0, 1]. The pixel-vs-normalized detection is purely
    value-magnitude — any coordinate > 1.0 implies pixel coords.

    Returns ``BoundingBox(page_number=1, ...)`` (single-page assumption;
    Phase 5's orchestrator rewrites page_number when stitching multi-page
    extractions). Returns None on malformed input or when pixel coords are
    given without page_size_px.
    """
    if not isinstance(bbox, list | tuple) or len(bbox) != 4:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None

    if max(x1, y1, x2, y2) > 1.0:
        # Pixel coords. Need page_size_px to normalize.
        if page_size_px is None:
            return None
        w, h = page_size_px
        if w <= 0 or h <= 0:
            return None
        x1, x2 = x1 / w, x2 / w
        y1, y2 = y1 / h, y2 / h

    # Clamp to [0, 1] defensively — a pixel bbox that slightly exceeds the
    # page (e.g. a glyph the layout detector traced past the page edge)
    # shouldn't reject the whole match.
    x1 = max(0.0, min(1.0, x1))
    y1 = max(0.0, min(1.0, y1))
    x2 = max(0.0, min(1.0, x2))
    y2 = max(0.0, min(1.0, y2))

    try:
        return BoundingBox(page_number=1, x1=x1, y1=y1, x2=x2, y2=y2)
    except ValueError:
        # Defensive: any future BoundingBox validator addition that rejects
        # malformed input shouldn't crash the cascade. As of intake_schemas
        # today, BoundingBox accepts any 4-float quadruple — this branch is
        # dead code currently but cheap forward-compat.
        return None


def _parse_response(raw: dict[str, Any], form_cls: type[T]) -> T:
    """Parse a PaddleOCR-VL response into a populated ``form_cls`` instance.

    Pure function — no I/O, no model state. The cached-replay path and the
    live path both run through this so behavior stays identical.

    Expected response shape (the layout-parser output PaddleOCR-VL actually
    returns)::

        {
            "parsing_res_list": [
                {
                    "block_bbox": [x1, y1, x2, y2],
                    "block_label": "text",
                    "block_content": "Patient Name: Jane Doe",
                },
                ...
            ],
            "page_size_px": [w, h],  # optional; needed only if bboxes are pixel
        }

    The alias-table-driven post-processor:
      1. Builds the alias map for ``form_cls``'s vertical.
      2. Walks ``parsing_res_list``.
      3. For ``block_label == "table"`` blocks (dense forms like CMS-1500
         that PaddleOCR-VL serializes as ``<table>`` HTML), lays the cells
         out on a colspan grid, pairs each cell with the one below it, and
         alias-matches the label cell (box number stripped). The value cell
         is read by :func:`_cell_readings`, which also splits packed boxes
         (``LAST, FIRST``; ``DOB SEX``; a ZIP cell that also holds the
         state). A cell holding both a label and its value
         ("DATE 03/01/2026", or label and value on stacked lines) is read by
         :func:`_same_cell_readings`.
      4. For all other block labels, matches the whole ``block_content``
         against the alias table and strips the recognized label prefix
         to get the value.
      5. Types each reading for its field (MM/DD/YYYY → ``date``) and keeps,
         per field, a reading that passes the field's validator over one
         that doesn't, then the higher alias score.

    Rules:
      - ``tier_used=1`` stamped on every populated field. Confidently-blank
        fields (model attempted, value=None) aren't producible from
        PaddleOCR-VL's layout-parser output, so V1 Tier 1 never emits them —
        all populated fields carry a non-None value.
      - Confidence (changed 2026-10-24): a date, ZIP or state that passes its
        validator gets ``VALIDATED_CONFIDENCE`` and is final at Tier 1, if it
        came from an aligned pair (or a same-cell label) whose matched alias
        names the field specifically (since 2026-10-25: not the generic
        "Date", which also sits inside "Date of Birth").
        Anything else keeps its alias-match score capped at
        ``UNVALIDATED_CONFIDENCE_CAP``, under the 0.85 gate. The score says
        how well the label matched, not whether the value is right, and
        PaddleOCR-VL emits no per-field confidence of its own.
    """
    blocks = raw.get("parsing_res_list", [])
    if not isinstance(blocks, list):
        return form_cls(metadata=_stub_metadata(form_cls))

    page_size = _parse_page_size(raw.get("page_size_px"))
    alias_map = _alias_map_for_form(form_cls)
    alias_words = {name: [_words(a) for a in aliases] for name, aliases in alias_map.items()}
    kinds = {name: inner for name, (inner, _meta) in _extractable_fields(form_cls).items()}
    generic = _generic_aliases(alias_map)

    # canonical_name → (ExtractedField, rank, final_ok). Best rank wins on
    # collision: a reading that passes its validator, then an aligned
    # label/value pair (see _table_pairs), then the higher alias score.
    # ``final_ok``: an aligned reading whose matched alias names this field
    # specifically (see _generic_aliases), the only kind that may finalize.
    best_by_field: dict[str, tuple[ExtractedField[Any], tuple[bool, bool, float], bool]] = {}

    def offer(
        name: str,
        value: Any,
        score: float,
        raw_text: str,
        bbox: Any,
        *,
        specific: bool,
        aligned: bool = True,
    ) -> None:
        if isinstance(value, str):
            value = _coerce(kinds.get(name), value)
            if value is None:
                return  # a date field whose text isn't a date
        rank = (validate(name, value) is True, aligned, score)
        existing = best_by_field.get(name)
        if existing is None or rank > existing[1]:
            field = ExtractedField(
                value=value,
                confidence=score,
                tier_used=TIER,
                raw_text=raw_text,
                bounding_box=bbox,
            )
            best_by_field[name] = (field, rank, specific and aligned)

    for block in blocks:
        if not isinstance(block, dict):
            continue
        content = block.get("block_content")
        if not isinstance(content, str) or not content.strip():
            continue
        bbox = _parse_bbox(block.get("block_bbox"), page_size)

        if block.get("block_label") == "table":
            # PaddleOCR-VL renders dense forms (CMS-1500, business intake,
            # claim forms) as a single ``table`` block whose ``block_content``
            # is an HTML <table>. Expand it into column-aligned (label, value)
            # cell pairs so the alias matcher operates on individual fields
            # rather than the whole HTML blob.
            for label_text, value_text, aligned in _table_pairs(content):
                tmatch = _best_label_match(label_text, alias_map)
                if tmatch is None:
                    continue
                canonical_name, score, alias = tmatch
                raw_text = f"{label_text} | {value_text}"
                specific = alias.upper() not in generic
                if not aligned and not specific:
                    # A value stretched under a label that only a generic
                    # alias matched ("15. OTHER DATE" over box 14's date):
                    # too weak to count as a reading at all.
                    continue
                for name, value in _cell_readings(
                    label_text, value_text, canonical_name, kinds, alias_words
                ):
                    offer(
                        name,
                        value,
                        score,
                        raw_text,
                        bbox,
                        specific=specific and name == canonical_name,
                        aligned=aligned,
                    )
            for row in _table_cells(content):
                for cell in dict.fromkeys(text for text, _start, _end in row):
                    readings = _same_cell_readings(cell, alias_map, kinds, alias_words)
                    primary = readings[0][0] if readings else None
                    for name, value, score, alias in readings:
                        specific = alias.upper() not in generic and name == primary
                        offer(name, value, score, cell, bbox, specific=specific)
            continue

        match = _match_block(content, alias_map)
        if match is None:
            continue
        canonical_name, value, score = match
        if value is None:
            # Label-only block; Phase 5 orchestrator can pair labels with
            # adjacent value blocks via layout heuristics. V1 Tier 1 skips.
            continue
        offer(canonical_name, value, score, content, bbox, specific=False)

    field_overrides = {}
    for name, (ef, _rank, final_ok) in best_by_field.items():
        if final_ok and _finalizes(name, ef.value):
            confidence = VALIDATED_CONFIDENCE
        else:
            confidence = min(ef.confidence, UNVALIDATED_CONFIDENCE_CAP)
        field_overrides[name] = ef.model_copy(update={"confidence": confidence})
    # Drop fields whose value doesn't fit the schema — typically a
    # column-shifted table cell (a ``Literal`` field reading a value from the
    # wrong box). Dropping is better than crashing the form: Tier 2 reruns
    # against the same PNG and reads every field Tier 1 didn't finalize.
    try:
        return form_cls(metadata=_stub_metadata(form_cls), **field_overrides)
    except ValidationError as e:
        bad_fields = {err["loc"][0] for err in e.errors() if err["loc"]}
        cleaned = {k: v for k, v in field_overrides.items() if k not in bad_fields}
        return form_cls(metadata=_stub_metadata(form_cls), **cleaned)


def _parse_page_size(raw: Any) -> tuple[int, int] | None:
    """Parse ``[w, h]`` from a raw_response field. Returns None on malformed input."""
    if not isinstance(raw, list | tuple) or len(raw) != 2:
        return None
    try:
        w, h = int(raw[0]), int(raw[1])
    except (TypeError, ValueError):
        return None
    if w <= 0 or h <= 0:
        return None
    return (w, h)


def _stub_metadata(form_cls: type) -> FormMetadata:
    """Build a placeholder ``FormMetadata`` for a single provider call.

    The provider doesn't know the upstream ``source_document_id``; Phase 5's
    orchestrator replaces this with the real provenance value when assembling
    the cascade output. The stub just keeps the Pydantic instance valid.
    """
    return FormMetadata(
        form_type=form_cls.__name__,
        source_document_id="<pending-orchestrator>",
        extraction_timestamp=datetime.now(UTC),
        pipeline_version=PIPELINE_VERSION,
    )


def _load_paddleocr_vl_pipeline() -> Any:
    """Lazy paddle + paddleocr import; pin device; instantiate pipeline.

    Called once per provider instance (memoized in ``Tier1PaddleOcrLocal``).
    Raises ``ImportError`` with an install hint when paddle/paddleocr are
    missing — the cached-replay path never reaches this code, so CI doesn't
    need the GPU stack.

    Tests stub via ``monkeypatch.setattr`` on this module attribute.
    """
    try:
        import paddle  # type: ignore[import-not-found]
        from paddleocr import PaddleOCRVL  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "Tier 1 live inference requires paddlepaddle-gpu + paddleocr. "
            "See docs/local-development.md `Tier 1 PaddleOCR-VL setup` for "
            "the install commands. Cached-replay tests do not require this "
            "stack."
        ) from e

    paddle.set_device(PADDLE_DEVICE)
    return PaddleOCRVL()


def _invoke_pipeline(pipeline: Any, png: bytes) -> dict[str, Any]:
    """Run one PaddleOCR-VL prediction. Tests stub via ``monkeypatch.setattr``.

    No ``prompt`` parameter — PaddleOCR-VL is a layout parser, not a prompted
    VL model. ``pipeline.predict(input=arr)`` returns a one-element list
    wrapping a result dict with a dozen keys; we only keep the fields
    ``_parse_response`` needs (parsing blocks + page size) and discard the
    rest (preprocessor/layout-detection ndarrays, table_res_list, etc.) so
    the cached fixture stays under ~10 KB and round-trips through ``json``.

    Each ``parsing_res_list`` entry is a ``PaddleOCRVLBlock`` instance with
    attributes ``label``/``bbox``/``content``. We project them to the dict
    keys ``_parse_response`` expects: ``block_label``/``block_bbox``/
    ``block_content`` (verbatim names from the prior synthetic-stub schema,
    kept stable so cached-replay fixtures interop with both).
    """
    import numpy as np

    img = Image.open(io.BytesIO(png))
    page_size_px = list(img.size)  # PIL .size is (w, h)
    # PaddleOCR-VL accepts numpy.ndarray or str path — not PIL. Convert via
    # RGB so 1-bit / palette images don't blow up the downstream decoder.
    raw = pipeline.predict(input=np.asarray(img.convert("RGB")))

    if isinstance(raw, list) and len(raw) == 1:
        result_obj = raw[0]
    elif isinstance(raw, dict):
        result_obj = raw
    else:
        return {
            "_warning": "unexpected_predict_shape",
            "raw_type": type(raw).__name__,
            "page_size_px": page_size_px,
            "parsing_res_list": [],
        }

    parsing_blocks_raw = result_obj.get("parsing_res_list", []) or []
    parsing_blocks: list[dict[str, Any]] = []
    for blk in parsing_blocks_raw:
        bbox = getattr(blk, "bbox", None)
        if isinstance(bbox, np.ndarray):
            bbox = bbox.tolist()
        parsing_blocks.append(
            {
                "block_label": getattr(blk, "label", None),
                "block_bbox": bbox,
                "block_content": getattr(blk, "content", None),
            }
        )

    # Prefer the model's own width/height (more authoritative than PIL when
    # the preprocessor rescaled the input); fall back to PIL .size.
    width = result_obj.get("width", page_size_px[0])
    height = result_obj.get("height", page_size_px[1])
    return {
        "parsing_res_list": parsing_blocks,
        "page_size_px": [int(width), int(height)],
        "width": int(width),
        "height": int(height),
    }


class Tier1PaddleOcrLocal:
    """Conforms to ``cascade.providers._base.CascadeProvider``.

    The class is a thin shell: state is just the lazily-loaded pipeline. All
    parsing lives in module-level pure functions so the cached-replay path
    and the live path share one parser.
    """

    name: str = PROVIDER_NAME
    tier: TierId = TIER

    def __init__(self) -> None:
        self._pipeline: Any | None = None

    def _ensure_pipeline_loaded(self) -> Any:
        """Lazy-load the PaddleOCR-VL pipeline. Memoized."""
        if self._pipeline is None:
            self._pipeline = _load_paddleocr_vl_pipeline()
        return self._pipeline

    def extract(self, png: bytes, form_cls: type[T]) -> ProviderResult[T]:
        """Extract fields from a single-page PNG.

        Cache-first: if ``EVAL_LIVE`` is unset and a cached response exists
        for ``sha256(png)``, parse the cached payload and return with
        ``latency_ms=0.0``. Otherwise call live, persist the response, and
        return real telemetry.

        ``image_sha256`` is recomputed from ``png`` here (not pulled from a
        sidecar) so a caller bug pairing the wrong PNG with a stale hash
        surfaces as a cache miss rather than serving the wrong cached result.
        """
        image_sha256 = hashlib.sha256(png).hexdigest()

        if not eval_cache.is_live_mode():
            cached = eval_cache.load_cached(PROVIDER_NAME, image_sha256)
            if cached is not None:
                return ProviderResult(
                    form=_parse_response(cached, form_cls),
                    latency_ms=0.0,
                    cost_usd=0.0,
                    raw_response=cached,
                )

        pipeline = self._ensure_pipeline_loaded()
        t0 = time.perf_counter()
        raw_response = _invoke_pipeline(pipeline, png)
        latency_ms = (time.perf_counter() - t0) * 1000.0

        eval_cache.save_cached(PROVIDER_NAME, image_sha256, raw_response)
        return ProviderResult(
            form=_parse_response(raw_response, form_cls),
            latency_ms=latency_ms,
            cost_usd=0.0,
            raw_response=raw_response,
        )
