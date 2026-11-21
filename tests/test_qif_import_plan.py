from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from household_finance.qif import build_plan, parse_qif, read_qif_file
from household_finance.qif.__main__ import main
from household_finance.qif.parser import decode_qif
from household_finance.qif.report import account_balances

FIXTURE = Path(__file__).parent / "fixtures" / "sample.qif"
D = Decimal


@pytest.fixture(scope="module")
def plan():
    return build_plan(read_qif_file(FIXTURE))


def find(plan, source_ref):
    return next(t for t in plan.transactions if source_ref in t.source_refs)


def splits_by_account(txn):
    return {s.account: s for s in txn.splits}


def test_parser_reads_sections():
    qif = read_qif_file(FIXTURE)
    assert [a.name for a in qif.accounts.values()] == [
        "Checking", "Savings", "Visa", "Brokerage", "Mortgage"]
    assert [c.name for c in qif.categories][:2] == ["Auto", "Auto:Fuel"]
    assert qif.categories[3].is_income and qif.categories[3].tax_line == "256"
    assert [c.name for c in qif.classes] == ["Business"]
    assert len(qif.transactions) == 13
    assert qif.skipped_investment == {"Brokerage": 1}
    assert qif.skipped_records == {"type:memorized": 1}


def test_every_transaction_balances(plan):
    assert plan.transactions
    for t in plan.transactions:
        assert t.splits
        assert sum(s.amount for s in t.splits) == 0, t.source_ref


def test_transfer_halves_are_merged(plan):
    # 13 register records; two halves merged once, three halves merged once.
    assert len(plan.transactions) == 10
    assert plan.matched_transfers == 3
    t = find(plan, "QIF Checking#3")
    assert t.source_refs == ["QIF Checking#3", "QIF Savings#1"]
    s = splits_by_account(t)
    assert set(s) == {"Checking", "Savings"}
    assert s["Checking"].amount == D("-500.00") and s["Checking"].cleared == "uncleared"
    assert s["Savings"].amount == D("500.00") and s["Savings"].cleared == "reconciled"


def test_split_with_transfer_lines(plan):
    t = find(plan, "QIF Checking#4")
    assert t.source_refs == ["QIF Checking#4", "QIF Savings#2", "QIF Visa#2"]
    assert t.memo == "January" and t.payee == "Split payment"
    s = splits_by_account(t)
    assert {a: x.amount for a, x in s.items()} == {
        "Checking": D("-300.00"), "Auto:Fuel": D("100.00"),
        "Savings": D("150.00"), "Visa": D("50.00")}
    assert s["Auto:Fuel"].memo == "Gas"
    assert s["Savings"].memo == "Save some"  # differs from header memo
    assert s["Visa"].cleared == "cleared"  # each side keeps its own status
    assert any("transfer halves dated" in n for n in t.notes)


def test_transfer_issues(plan):
    by_status = {(i.status, i.account): i for i in plan.transfer_issues}
    mismatch = by_status[("date_mismatch", "Checking")]
    assert mismatch.date == date(2026, 1, 15) and mismatch.other_date == date(2026, 1, 17)
    unmatched = by_status[("unmatched", "Savings")]
    assert unmatched.amount == D("10.00") and unmatched.other_account == "Checking"
    # Transfers to accounts without a register are not issues.
    assert len(plan.transfer_issues) == 2


def test_opening_balance_and_categories(plan):
    s = splits_by_account(find(plan, "QIF Checking#1"))
    assert s["Checking"].amount == D("1000.00")
    assert s["Opening Balances"].amount == D("-1000.00")
    assert s["Opening Balances"].cleared == "reconciled"  # same as its register

    s = splits_by_account(find(plan, "QIF Checking#2"))
    assert s["Groceries"].amount == D("120.00") and s["Groceries"].tags == ["Business"]
    assert find(plan, "QIF Checking#2").check_number == "1001"

    s = splits_by_account(find(plan, "QIF Checking#5"))
    assert s["Salary"].amount == D("-2000.00")


def test_accounts(plan):
    a = plan.accounts
    assert (a["checking"].kind, a["checking"].subtype) == ("asset", "checking")
    assert (a["savings"].kind, a["savings"].subtype) == ("asset", "savings")
    assert (a["visa"].kind, a["visa"].subtype) == ("liability", "credit_card")
    assert (a["mortgage"].kind, a["mortgage"].subtype) == ("liability", "mortgage")
    assert a["brokerage"].subtype == "investment" and not a["brokerage"].history_imported
    assert a["old savings"].origin == "referenced" and not a["old savings"].history_imported
    assert a["auto:fuel"].kind == "expense" and a["auto:fuel"].parent_path == "Auto"
    assert a["salary"].kind == "income" and a["salary"].tax_line == "256"
    assert a["dining out"].kind == "expense"  # used but not listed
    assert plan.tags == {"Business": "Side business"}
    assert "Safeway" in plan.payees


def test_balances(plan):
    b = account_balances(plan)
    # Checking is 1,715.00 in its own register; the unmatched Savings half
    # also posts -10.00 here, which the report flags for review.
    assert b["Checking"] == (D("1705.00"), D("880.00"), D("1000.00"))
    assert b["Savings"] == (D("660.00"), D("500.00"), D("500.00"))
    assert b["Visa"] == (D("-10.00"), D("50.00"), D("0.00"))
    assert b["Brokerage"][0] == D("250.00")
    assert b["Opening Balances"][0] == D("-1000.00")
    assert b["Auto:Fuel"][0] == D("160.00")
    assert sum(v[0] for v in b.values()) == 0


def test_category_account_name_collision():
    qif = parse_qif(
        "!Account\nNMortgage\nTOth L\n^\n"
        "!Type:Cat\nNMortgage:Interest\nE\n^\n"
        "!Account\nNChecking\nTBank\n^\n"
        "!Type:Bank\nD2/1'26\nT-900.00\nSMortgage:Interest\n$-400.00\n"
        "S[Mortgage]\n$-500.00\n^\n")
    plan = build_plan(qif)
    assert plan.account("Mortgage").kind == "liability"
    cat = plan.account("Mortgage (category):Interest")
    assert cat is not None and cat.kind == "expense"
    assert any("Mortgage (category)" in r for r in plan.renames)
    accounts = {s.account for s in plan.transactions[0].splits}
    assert accounts == {"Checking", "Mortgage (category):Interest", "Mortgage"}


def test_split_remainder_goes_to_uncategorized():
    qif = parse_qif("!Account\nNChecking\nTBank\n^\n!Type:Bank\nD3/1'26\nT-100.00\n"
                    "SGroceries\n$-60.00\n^\n")
    plan = build_plan(qif)
    s = splits_by_account(plan.transactions[0])
    assert s["Uncategorized"].amount == D("40.00")
    assert any("left uncategorized" in w for w in plan.warnings)


def test_single_account_file_without_header():
    qif = parse_qif("!Type:CCard\nD3/1'26\nT-25.00\nPLunch\n^\n", default_account="Amex")
    plan = build_plan(qif)
    assert plan.account("Amex").subtype == "credit_card"
    assert splits_by_account(plan.transactions[0])["Uncategorized"].amount == D("25.00")


def test_bad_date_is_reported_not_fatal():
    qif = parse_qif("!Account\nNChecking\nTBank\n^\n!Type:Bank\nDbogus\nT-1.00\n^\n"
                    "D3/2'26\nT-2.00\n^\n")
    assert len(qif.transactions) == 1
    assert any("unrecognized date" in str(w) for w in qif.warnings)


def test_windows_1252_decoding():
    assert decode_qif("PCafé\n".encode("cp1252")) == "PCafé\n"
    assert decode_qif("﻿PCafé\n".encode("utf-8")) == "PCafé\n"


def test_cli_report(capsys):
    assert main(["report", str(FIXTURE), "--year", "2026", "--register", "Checking"]) == 0
    out = capsys.readouterr().out
    assert "Transactions to import:       10" in out
    assert "TRANSFERS TO REVIEW" in out
    assert "CATEGORY TOTALS 2026" in out
    assert "REGISTER: Checking" in out
