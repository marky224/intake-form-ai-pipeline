"""Format / plausibility validators used by the Tier 3 second read."""

from __future__ import annotations

from datetime import date

import pytest

from cascade.validators import DATE_MAX, DATE_MIN, validate


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(1957, 4, 4), True),
        (DATE_MIN, True),
        (DATE_MAX, True),
        (date(1899, 12, 31), False),
        (date(2101, 1, 1), False),
    ],
)
def test_date_range(value, expected):
    # Dates are checked by type, whatever the field is called.
    assert validate("date_of_birth", value) is expected
    assert validate("subscriber_dob", value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("01543", True), ("01543-1234", True), (" 01543 ", True), ("1543", False), ("0154A", False)],
)
def test_zip(value, expected):
    assert validate("address_zip", value) is expected


@pytest.mark.parametrize(("value", "expected"), [("MA", True), ("ma", True), ("XX", False)])
def test_state(value, expected):
    assert validate("address_state", value) is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("555-630-6862", True), ("(555) 630 6862", True), ("630-6862", False)],
)
def test_phone(value, expected):
    assert validate("phone", value) is expected
    assert validate("emergency_contact_phone", value) is expected


def test_free_text_has_no_validator():
    assert validate("last_name", "Heredia716") is None
    assert validate("address_street", "434 Toy Arcade") is None
