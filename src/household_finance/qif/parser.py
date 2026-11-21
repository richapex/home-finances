"""Read a QIF file into typed records, without interpreting transfers.

Handles the layout written by Quicken for Windows' "export all accounts":

    !Option:AutoSwitch
    !Account            <- list of every account
    ...^
    !Clear:AutoSwitch
    !Type:Cat           <- category list
    !Type:Class         <- class list
    !Account            <- one account header...
    NChecking
    TBank
    ^
    !Type:Bank          <- ...followed by that account's register
    D1/ 5'26
    ...

Sections this app does not use (memorized transactions, securities, prices,
investment registers, ...) are counted and skipped.
"""

from __future__ import annotations

import codecs
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from .values import (
    CategoryRef,
    QifValueError,
    name_key,
    parse_amount,
    parse_category_field,
    parse_cleared,
    parse_date,
)

REGISTER_SECTIONS = {
    "type:bank": "Bank",
    "type:cash": "Cash",
    "type:ccard": "CCard",
    "type:oth a": "Oth A",
    "type:oth l": "Oth L",
}
INVESTMENT_SECTION = "type:invst"


def is_investment_type(qif_type: str) -> bool:
    t = qif_type.strip().casefold()
    return any(k in t for k in ("invst", "port", "401", "403", "mutual"))


@dataclass
class QifAccount:
    name: str
    qif_type: str  # as written, e.g. "Bank", "CCard", "Oth L", "Invst"
    description: str = ""
    line_no: int = 0

    @property
    def is_investment(self) -> bool:
        return is_investment_type(self.qif_type)


@dataclass
class QifCategory:
    name: str  # full path, "Auto:Fuel"
    description: str = ""
    is_income: bool = False
    tax_related: bool = False
    tax_line: str | None = None
    line_no: int = 0


@dataclass
class QifClass:
    name: str
    description: str = ""
    line_no: int = 0


@dataclass
class QifLine:
    """One category assignment: the whole transaction when it is not split,
    otherwise one S/E/$ group. amount has the register's sign (a payment
    out of a bank account is negative)."""

    ref: CategoryRef
    amount: Decimal
    memo: str = ""


@dataclass
class QifTransaction:
    account: str  # name of the register it was read from
    index: int  # position within that register, starting at 1
    date: date
    amount: Decimal
    cleared: str  # 'uncleared' | 'cleared' | 'reconciled'
    check_number: str | None
    payee: str | None
    memo: str
    lines: list[QifLine]
    is_split: bool
    line_no: int

    @property
    def source_ref(self) -> str:
        return f"QIF {self.account}#{self.index}"


@dataclass
class QifWarning:
    line_no: int
    message: str

    def __str__(self) -> str:
        return f"line {self.line_no}: {self.message}" if self.line_no else self.message


@dataclass
class QifFile:
    accounts: dict[str, QifAccount] = field(default_factory=dict)  # by name_key
    categories: list[QifCategory] = field(default_factory=list)
    classes: list[QifClass] = field(default_factory=list)
    transactions: list[QifTransaction] = field(default_factory=list)
    skipped_records: Counter[str] = field(default_factory=Counter)  # section -> count
    skipped_investment: Counter[str] = field(default_factory=Counter)  # account -> count
    warnings: list[QifWarning] = field(default_factory=list)

    def account(self, name: str) -> QifAccount | None:
        return self.accounts.get(name_key(name))


def decode_qif(data: bytes) -> str:
    """Quicken writes UTF-8 (sometimes with a BOM) or Windows-1252."""
    if data.startswith(codecs.BOM_UTF8):
        return data[len(codecs.BOM_UTF8) :].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def read_qif_file(path: str | Path, default_account: str | None = None) -> QifFile:
    path = Path(path)
    return parse_qif(decode_qif(path.read_bytes()), default_account or path.stem)


def parse_qif(text: str, default_account: str = "Imported Account") -> QifFile:
    return _Parser(default_account).run(text)


class _Parser:
    def __init__(self, default_account: str) -> None:
        self.result = QifFile()
        self.default_account = default_account
        self.section: str | None = None
        self.current_account: QifAccount | None = None
        self.register_counts: Counter[str] = Counter()
        self.unknown_codes: Counter[tuple[str, str]] = Counter()

    def warn(self, line_no: int, message: str) -> None:
        self.result.warnings.append(QifWarning(line_no, message))

    def run(self, text: str) -> QifFile:
        fields: list[tuple[str, str, int]] = []
        start_line = 0
        for line_no, raw in enumerate(text.splitlines(), start=1):
            line = raw.rstrip()
            if not line.strip():
                continue
            if line.startswith("!"):
                if fields:
                    self.warn(start_line, "record not terminated by '^' before a header")
                    self.record(fields, start_line)
                    fields = []
                self.header(line[1:].strip().casefold(), line_no)
            elif line.strip() == "^":
                if fields:
                    self.record(fields, start_line)
                fields = []
            else:
                if not fields:
                    start_line = line_no
                fields.append((line[0], line[1:], line_no))
        if fields:
            self.warn(start_line, "last record not terminated by '^'")
            self.record(fields, start_line)
        for (section, code), count in sorted(self.unknown_codes.items()):
            self.warn(0, f"ignored field code {code!r} in {section} ({count} times)")
        return self.result

    # -- headers --------------------------------------------------------------

    def header(self, name: str, line_no: int) -> None:
        if name.startswith(("option:", "clear:")):
            return  # !Option:AutoSwitch / !Clear:AutoSwitch need no handling
        self.section = name

    # -- records --------------------------------------------------------------

    def record(self, fields: list[tuple[str, str, int]], line_no: int) -> None:
        section = self.section
        if section is None:
            self.warn(line_no, "record before any '!' header ignored")
        elif section == "account":
            self.account_record(fields, line_no)
        elif section == "type:cat":
            self.category_record(fields, line_no)
        elif section == "type:class":
            self.class_record(fields, line_no)
        elif section in REGISTER_SECTIONS or section == INVESTMENT_SECTION:
            self.register_record(section, fields, line_no)
        else:
            self.result.skipped_records[section] += 1

    def account_record(self, fields: list[tuple[str, str, int]], line_no: int) -> None:
        name = qif_type = description = ""
        for code, value, _ in fields:
            if code == "N":
                name = value.strip()
            elif code == "T":
                qif_type = value.strip()
            elif code == "D":
                description = value.strip()
        if not name:
            self.warn(line_no, "account record without a name ignored")
            return
        account = self.result.account(name)
        if account is None:
            account = QifAccount(name, qif_type, description, line_no)
            self.result.accounts[name_key(name)] = account
        else:
            if not account.qif_type:
                account.qif_type = qif_type
            elif qif_type and qif_type.casefold() != account.qif_type.casefold():
                self.warn(line_no, f"account {name!r} listed as both "
                                   f"{account.qif_type!r} and {qif_type!r}")
            if not account.description:
                account.description = description
        # An account record introduces the register that follows. Inside the
        # AutoSwitch account list no register follows, and the next register
        # always has its own !Account header, so this is safe either way.
        self.current_account = account

    def category_record(self, fields: list[tuple[str, str, int]], line_no: int) -> None:
        cat = QifCategory(name="", line_no=line_no)
        for code, value, _ in fields:
            if code == "N":
                cat.name = value.strip()
            elif code == "D":
                cat.description = value.strip()
            elif code == "I":
                cat.is_income = True
            elif code == "E":
                cat.is_income = False
            elif code == "T":
                cat.tax_related = True
            elif code == "R":
                cat.tax_line = value.strip() or None
        if cat.name:
            self.result.categories.append(cat)
        else:
            self.warn(line_no, "category record without a name ignored")

    def class_record(self, fields: list[tuple[str, str, int]], line_no: int) -> None:
        cls = QifClass(name="", line_no=line_no)
        for code, value, _ in fields:
            if code == "N":
                cls.name = value.strip()
            elif code == "D":
                cls.description = value.strip()
        if cls.name:
            self.result.classes.append(cls)
        else:
            self.warn(line_no, "class record without a name ignored")

    def register_account(self, section: str, line_no: int) -> QifAccount:
        if self.current_account is not None:
            return self.current_account
        # A single-account export has no !Account header.
        qif_type = REGISTER_SECTIONS.get(section, "Invst")
        account = self.result.account(self.default_account)
        if account is None:
            account = QifAccount(self.default_account, qif_type, "", line_no)
            self.result.accounts[name_key(self.default_account)] = account
            self.warn(line_no, f"register without an !Account header; using "
                               f"account name {self.default_account!r}")
        self.current_account = account
        return account

    def register_record(self, section: str, fields: list[tuple[str, str, int]],
                        line_no: int) -> None:
        account = self.register_account(section, line_no)
        if section == INVESTMENT_SECTION or account.is_investment:
            self.result.skipped_investment[account.name] += 1
            return

        txn_date: date | None = None
        amount_t: Decimal | None = None
        amount_u: Decimal | None = None
        cleared = "uncleared"
        check_number = payee = None
        memo = ""
        category: CategoryRef | None = None
        splits: list[dict] = []

        for code, value, field_line in fields:
            try:
                if code == "D":
                    txn_date = parse_date(value)
                elif code == "T":
                    amount_t = parse_amount(value)
                elif code == "U":
                    amount_u = parse_amount(value)
                elif code == "C":
                    parsed = parse_cleared(value)
                    if parsed is None:
                        self.warn(field_line, f"unknown cleared flag {value!r}; "
                                              "treated as uncleared")
                    else:
                        cleared = parsed
                elif code == "N":
                    check_number = value.strip() or None
                elif code == "P":
                    payee = value.strip() or None
                elif code == "M":
                    memo = value.strip()
                elif code == "L":
                    category = parse_category_field(value)
                elif code == "S":
                    splits.append({"ref": parse_category_field(value), "memo": "",
                                   "amount": None, "line": field_line})
                elif code == "E":
                    if splits:
                        splits[-1]["memo"] = value.strip()
                    else:
                        self.warn(field_line, "split memo (E) before any split (S)")
                elif code == "$":
                    if splits:
                        splits[-1]["amount"] = parse_amount(value)
                    else:
                        self.warn(field_line, "split amount ($) before any split (S)")
                elif code in ("A", "F", "%"):
                    pass  # address lines, reimbursable flag, split percentage
                else:
                    self.unknown_codes[(section, code)] += 1
            except QifValueError as exc:
                self.warn(field_line, str(exc))

        if txn_date is None:
            self.warn(line_no, f"transaction in {account.name!r} without a valid "
                               "date skipped")
            return

        amount = amount_t if amount_t is not None else amount_u
        if amount_t is not None and amount_u is not None and amount_t != amount_u:
            self.warn(line_no, f"T amount {amount_t} differs from U amount "
                               f"{amount_u}; using T")

        lines: list[QifLine] = []
        if splits:
            for s in splits:
                if s["amount"] is None:
                    self.warn(s["line"], "split without an amount; using 0.00")
                    s["amount"] = Decimal("0.00")
                lines.append(QifLine(s["ref"], s["amount"], s["memo"]))
            split_total = sum((ln.amount for ln in lines), Decimal("0.00"))
            if amount is None:
                amount = split_total
            elif split_total != amount:
                remainder = amount - split_total
                self.warn(line_no, f"splits total {split_total} but transaction is "
                                   f"{amount}; {remainder} left uncategorized")
                lines.append(QifLine(CategoryRef(None, False, None), remainder,
                                     "(split remainder)"))
        else:
            if amount is None:
                self.warn(line_no, "transaction without an amount; using 0.00")
                amount = Decimal("0.00")
            lines.append(QifLine(category or CategoryRef(None, False, None), amount))

        self.register_counts[account.name] += 1
        self.result.transactions.append(QifTransaction(
            account=account.name,
            index=self.register_counts[account.name],
            date=txn_date,
            amount=amount,
            cleared=cleared,
            check_number=check_number,
            payee=payee,
            memo=memo,
            lines=lines,
            is_split=bool(splits),
            line_no=line_no,
        ))
