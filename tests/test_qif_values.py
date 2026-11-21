from datetime import date
from decimal import Decimal

import pytest

from household_finance.qif.values import (
    CategoryRef,
    QifValueError,
    parse_amount,
    parse_category_field,
    parse_cleared,
    parse_date,
)


@pytest.mark.parametrize("text, expected", [
    ("1/ 5'26", date(2026, 1, 5)),
    ("12/31'99", date(2099, 12, 31)),
    ("1/ 5' 9", date(2009, 1, 5)),
    (" 1/ 5/98", date(1998, 1, 5)),
    ("1/5/2026", date(2026, 1, 5)),
    ("01-05-2026", date(2026, 1, 5)),
    ("2026-01-05", date(2026, 1, 5)),
])
def test_parse_date(text, expected):
    assert parse_date(text) == expected


@pytest.mark.parametrize("text", ["", "13/1'26", "2/30'26", "1/5", "Jan 5 2026", "1/5/202"])
def test_parse_date_rejects(text):
    with pytest.raises(QifValueError):
        parse_date(text)


@pytest.mark.parametrize("text, expected", [
    ("1,234.56", Decimal("1234.56")),
    ("-120.00", Decimal("-120.00")),
    ("-0.5", Decimal("-0.50")),
    ("0", Decimal("0.00")),
    (" 12 ", Decimal("12.00")),
])
def test_parse_amount(text, expected):
    value = parse_amount(text)
    assert value == expected
    assert value.as_tuple().exponent == -2


@pytest.mark.parametrize("text", ["", "abc", "1.234", "NaN", "Infinity"])
def test_parse_amount_rejects(text):
    with pytest.raises(QifValueError):
        parse_amount(text)


@pytest.mark.parametrize("text, expected", [
    ("", "uncleared"), ("*", "cleared"), ("c", "cleared"),
    ("X", "reconciled"), ("R", "reconciled"), ("?", None),
])
def test_parse_cleared(text, expected):
    assert parse_cleared(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("Auto:Fuel", CategoryRef("Auto:Fuel", False, None)),
    ("Auto:Fuel/Business", CategoryRef("Auto:Fuel", False, "Business")),
    ("[Savings]", CategoryRef("Savings", True, None)),
    ("[Savings]/Business:Travel", CategoryRef("Savings", True, "Business:Travel")),
    ("", CategoryRef(None, False, None)),
    ("/Business", CategoryRef(None, False, "Business")),
    ("--Split--", CategoryRef(None, False, None)),
    ("[]", CategoryRef(None, False, None)),
])
def test_parse_category_field(text, expected):
    assert parse_category_field(text) == expected
