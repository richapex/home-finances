"""Plain-text report of an import plan, for checking it against Quicken."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from .plan import ZERO, ImportPlan, PlannedAccount, path_key
from .values import name_key

CLEARED_FLAG = {"uncleared": " ", "cleared": "c", "reconciled": "R"}


def money(value: Decimal) -> str:
    return f"{value:,.2f}"


def account_balances(plan: ImportPlan) -> dict[str, tuple[Decimal, Decimal, Decimal]]:
    """path -> (balance, cleared balance, reconciled balance), debit-positive,
    computed like v_account_balances."""
    totals: dict[str, list[Decimal]] = defaultdict(lambda: [ZERO, ZERO, ZERO])
    for t in plan.transactions:
        for s in t.splits:
            row = totals[path_key(s.account)]
            row[0] += s.amount
            if s.cleared != "uncleared":
                row[1] += s.amount
            if s.cleared == "reconciled":
                row[2] += s.amount
    return {a.path: tuple(totals[k]) for k, a in plan.accounts.items()}


def _table(headers: list[str], rows: list[list[str]], right: set[int]) -> list[str]:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row)]

    def fmt(row: list[str]) -> str:
        return "  ".join(c.rjust(w) if i in right else c.ljust(w)
                         for i, (c, w) in enumerate(zip(row, widths))).rstrip()

    return [fmt(headers), fmt(["-" * w for w in widths])] + [fmt(r) for r in rows]


def format_report(plan: ImportPlan, year: int | None = None,
                  register: str | None = None, max_items: int = 50) -> str:
    out: list[str] = []
    real = [a for a in plan.accounts.values() if not a.is_category and a.kind != "equity"]
    categories = [a for a in plan.accounts.values() if a.is_category]
    balances = account_balances(plan)

    out.append("QIF IMPORT PLAN")
    out.append("=" * 15)
    out.append(f"Register transactions read:   {plan.register_transactions}")
    out.append(f"Transactions to import:       {len(plan.transactions)}")
    out.append(f"Transfers matched:            {plan.matched_transfers}")
    issues = Counter(i.status for i in plan.transfer_issues)
    out.append(f"  matched with different date: {issues['date_mismatch']}")
    out.append(f"  unmatched (review):          {issues['unmatched']}")
    out.append(f"Accounts: {len(real)}   Categories: {len(categories)}   "
               f"Payees: {len(plan.payees)}   Tags: {len(plan.tags)}")
    if plan.skipped_investment:
        skipped = ", ".join(f"{a} ({n})" for a, n in sorted(plan.skipped_investment.items()))
        out.append(f"Investment records skipped:   {skipped}")
    if plan.skipped_records:
        skipped = ", ".join(f"!{s} ({n})" for s, n in sorted(plan.skipped_records.items()))
        out.append(f"Other sections skipped:       {skipped}")

    out.append("")
    out.append("ACCOUNT BALANCES (compare with Quicken; liabilities are negative)")
    rows = []
    for a in real:
        bal, clr, rec = balances[a.path]
        note = "" if a.history_imported else "no register in file: transfers only"
        rows.append([a.path, f"{a.kind}/{a.subtype}", money(bal), money(clr),
                     money(rec), note])
    out.extend(_table(["Account", "Type", "Balance", "Cleared", "Reconciled", ""],
                      rows, right={2, 3, 4}))
    ob = balances.get("Opening Balances", (ZERO,))[0]
    out.append(f"Opening Balances (equity): {money(ob)}")

    if plan.renames:
        out.append("")
        out.append("RENAMED")
        out.extend(f"  {r}" for r in plan.renames)

    if plan.transfer_issues:
        out.append("")
        out.append("TRANSFERS TO REVIEW")
        rows = []
        for i in plan.transfer_issues[:max_items]:
            detail = (f"other half {i.other_date} ({i.other_source_ref})"
                      if i.status == "date_mismatch" else "no matching half found")
            rows.append([i.status, str(i.date), i.account, f"[{i.other_account}]",
                         money(i.amount), i.payee or "", i.source_ref, detail])
        out.extend(_table(["Status", "Date", "Register", "Transfer", "Amount", "Payee",
                           "Source", "Detail"], rows, right={4}))
        if len(plan.transfer_issues) > max_items:
            out.append(f"  ... {len(plan.transfer_issues) - max_items} more")

    if year is not None:
        out.append("")
        out.extend(category_totals(plan, year))

    if register is not None:
        out.append("")
        out.extend(register_listing(plan, register))

    if plan.warnings:
        out.append("")
        out.append(f"WARNINGS ({len(plan.warnings)})")
        out.extend(f"  {w}" for w in plan.warnings[:max_items])
        if len(plan.warnings) > max_items:
            out.append(f"  ... {len(plan.warnings) - max_items} more (use --max-items)")

    return "\n".join(out) + "\n"


def category_totals(plan: ImportPlan, year: int) -> list[str]:
    """Totals per category for one year, income and expenses shown positive,
    with subtotals rolled up into parent categories like Quicken reports."""
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for t in plan.transactions:
        if t.date.year != year:
            continue
        for s in t.splits:
            acct = plan.account(s.account)
            if acct.is_category:
                path = acct.path
                while True:  # roll up to every ancestor
                    totals[path_key(path)] += s.amount
                    if ":" not in path:
                        break
                    path = path.rsplit(":", 1)[0]
    lines = [f"CATEGORY TOTALS {year} (including subcategories)"]
    for kind in ("income", "expense"):
        cats = sorted((a for a in plan.accounts.values()
                       if a.kind == kind and totals.get(path_key(a.path))),
                      key=lambda a: path_key(a.path))
        rows = []
        for a in cats:
            value = totals[path_key(a.path)]
            value = -value if kind == "income" else value
            depth = a.path.count(":")
            rows.append(["  " * depth + a.name, money(value)])
        grand = sum((totals[path_key(a.path)] for a in cats if ":" not in a.path), ZERO)
        grand = -grand if kind == "income" else grand
        rows.append(["TOTAL " + kind.upper(), money(grand)])
        lines.extend(_table([kind.capitalize(), "Amount"], rows, right={1}))
        lines.append("")
    return lines[:-1]


def register_listing(plan: ImportPlan, name: str) -> list[str]:
    acct: PlannedAccount | None = plan.account(name)
    if acct is None:
        return [f"REGISTER: no account named {name!r}"]
    rows, running = [], ZERO
    key = path_key(acct.path)
    for t in plan.transactions:
        mine = [s for s in t.splits if path_key(s.account) == key]
        if not mine:
            continue
        amount = sum((s.amount for s in mine), ZERO)
        running += amount
        others = [s.account for s in t.splits if path_key(s.account) != key]
        if len({name_key(o) for o in others}) > 1:
            other = "--split--"
        elif others:
            other_acct = plan.account(others[0])
            other = (other_acct.path if other_acct.is_category or other_acct.kind == "equity"
                     else f"[{other_acct.path}]")
        else:
            other = ""
        rows.append([str(t.date), t.check_number or "", (t.payee or "")[:30], other[:30],
                     money(amount), CLEARED_FLAG[mine[0].cleared], money(running),
                     "; ".join(t.notes)])
    return [f"REGISTER: {acct.path}"] + _table(
        ["Date", "Num", "Payee", "Category", "Amount", "C", "Balance", "Notes"],
        rows, right={4, 6})
