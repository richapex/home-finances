"""Money is stored as integer cents and handled in Python as Decimal."""

from __future__ import annotations

from decimal import Decimal

CENT = Decimal("0.01")


def to_cents(amount: Decimal) -> int:
    """Decimal dollars -> integer cents. Raises ValueError for fractions of a cent."""
    cents = amount * 100
    if cents != cents.to_integral_value():
        raise ValueError(f"amount {amount} has fractions of a cent")
    return int(cents)


def from_cents(cents: int) -> Decimal:
    return (Decimal(cents) / 100).quantize(CENT)


def format_money(amount: Decimal) -> str:
    """Plain two-decimal string used in JSON files and audit rows: "-120.00"."""
    return f"{amount.quantize(CENT):f}"
