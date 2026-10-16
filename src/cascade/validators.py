"""Deterministic format / plausibility validators for extracted values.

The orchestrator's Tier 3 second read (``orchestrator._reconcile_second_read``)
uses these on a disagreement: a Tier-2 value that passes its field's
validator is kept, so Tier 3 never overwrites a validated value — in
particular a validated date. A Tier-2 value is also kept when Tier 3 read
nothing; everywhere else the higher tier's reading wins.

A validator says a value is well-formed and plausible, not that it is
correct: a misread digit in a real-looking date still passes. Fields with no
validator (names, street, free text) return ``None``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

#: Plausible range for any extracted date (birth, signature, subscriber DOB).
DATE_MIN = date(1900, 1, 1)
DATE_MAX = date(2100, 12, 31)

_ZIP_RE = re.compile(r"\d{5}(-\d{4})?")

#: USPS state codes (50 states + DC).
_US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split()
)


def validate(field_name: str, value: Any) -> bool | None:
    """``True``/``False`` if ``field_name``'s value has a validator, else ``None``.

    Dates are checked by type (any ``date`` value), the rest by field-name
    suffix: ``*zip`` (5 or 5+4 digits), ``*state`` (USPS code), ``*phone``
    (10 digits once punctuation is stripped).
    """
    if isinstance(value, date):
        return DATE_MIN <= value <= DATE_MAX
    text = str(value).strip()
    if field_name.endswith("zip"):
        return _ZIP_RE.fullmatch(text) is not None
    if field_name.endswith("state"):
        return text.upper() in _US_STATES
    if field_name.endswith("phone"):
        return len(re.sub(r"\D", "", text)) == 10
    return None
