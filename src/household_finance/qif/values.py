"""Parsing of individual QIF field values: dates, amounts, cleared flags and
category/transfer fields."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

CENT = Decimal("0.01")


class QifValueError(ValueError):
    """A field value could not be parsed."""


# M/D'YY (Quicken, year 2000+), M/D/YY (Quicken, 1900s), M/D/YYYY, also with
# '-' or '.' separators. Quicken pads month and day with spaces ("1/ 5'26"),
# so spaces are removed before matching.
_MDY_RE = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})(['/\-.])(\d{1,4})$")
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


def parse_date(text: str) -> date:
    """Parse a QIF date.

    Quicken for Windows writes two-digit years with an apostrophe for
    2000 and later ("1/ 5'26" is 2026-01-05) and with a slash for the 1900s
    ("1/ 5/98" is 1998-01-05). Month comes before day (US format).
    """
    s = text.replace(" ", "")
    try:
        m = _ISO_RE.match(s)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = _MDY_RE.match(s)
        if m:
            month, day, sep, year_text = int(m[1]), int(m[2]), m[3], m[4]
            if len(year_text) <= 2:
                year = int(year_text) + (2000 if sep == "'" else 1900)
            elif len(year_text) == 4:
                year = int(year_text)
            else:
                raise QifValueError(f"unrecognized date {text!r}")
            return date(year, month, day)
    except ValueError as exc:  # date() out of range
        raise QifValueError(f"invalid date {text!r}: {exc}") from None
    raise QifValueError(f"unrecognized date {text!r}")


def parse_amount(text: str) -> Decimal:
    """Parse a QIF amount such as "-1,234.56" into a Decimal with two places.

    Raises QifValueError for text that is not a number or that has fractions
    of a cent.
    """
    s = text.strip().replace(",", "").replace("$", "").replace(" ", "")
    if not s:
        raise QifValueError("empty amount")
    try:
        value = Decimal(s)
    except InvalidOperation:
        raise QifValueError(f"invalid amount {text!r}") from None
    if not value.is_finite():
        raise QifValueError(f"invalid amount {text!r}")
    rounded = value.quantize(CENT)
    if rounded != value:
        raise QifValueError(f"amount {text!r} has fractions of a cent")
    return rounded


def parse_cleared(text: str) -> str | None:
    """Map a QIF C field to a splits.cleared value; None if unrecognized."""
    s = text.strip()
    if s == "":
        return "uncleared"
    if s in ("*", "c", "C"):
        return "cleared"
    if s in ("X", "x", "R", "r"):
        return "reconciled"
    return None


@dataclass(frozen=True)
class CategoryRef:
    """The parsed contents of an L or S field."""

    name: str | None  # category path or account name; None = no category
    is_transfer: bool  # True when written as [Account]
    tag: str | None  # the Quicken class after '/', if any


def parse_category_field(text: str) -> CategoryRef:
    """Parse "Auto:Fuel/Business", "[Savings]", "[Savings]/Business" etc."""
    s = text.strip()
    if s.startswith("["):
        close = s.find("]")
        if close != -1:
            name = s[1:close].strip()
            rest = s[close + 1 :].strip()
            tag = rest[1:].strip() if rest.startswith("/") else None
            return CategoryRef(name or None, bool(name), tag or None)
        # No closing bracket: treat the rest as the account name.
        return CategoryRef(s[1:].strip() or None, bool(s[1:].strip()), None)
    category, _, tag = s.partition("/")
    category = category.strip()
    if category.casefold() == "--split--":
        category = ""
    return CategoryRef(category or None, False, tag.strip() or None)


def name_key(name: str) -> str:
    """Comparison key matching MySQL's utf8mb4_0900_ai_ci collation closely
    enough for import: case- and accent-insensitive."""
    decomposed = unicodedata.normalize("NFKD", name)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.casefold()
