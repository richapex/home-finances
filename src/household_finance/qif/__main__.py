"""Command line: python -m household_finance.qif FILE.qif [options]

Reads a Quicken QIF export and prints the import plan. Nothing is written to
any database.
"""

from __future__ import annotations

import argparse
import sys

from .parser import read_qif_file
from .plan import build_plan
from .report import format_report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m household_finance.qif",
        description="Check a Quicken QIF export: parse it, match transfers and "
                    "print balances to compare with Quicken. Writes nothing.")
    ap.add_argument("file", help="QIF file exported from Quicken")
    ap.add_argument("--year", type=int, help="also print category totals for this year")
    ap.add_argument("--register", metavar="ACCOUNT",
                    help="also print this account's register with running balance")
    ap.add_argument("--date-window", type=int, default=7, metavar="DAYS",
                    help="accept transfer halves up to this many days apart (default 7)")
    ap.add_argument("--max-items", type=int, default=50, metavar="N",
                    help="list at most N warnings / transfer issues (default 50)")
    ap.add_argument("--account-name", metavar="NAME",
                    help="account name for a single-account file without an !Account "
                         "header (default: the file name)")
    args = ap.parse_args(argv)

    qif = read_qif_file(args.file, args.account_name)
    plan = build_plan(qif, date_window_days=args.date_window)
    sys.stdout.write(format_report(plan, year=args.year, register=args.register,
                                   max_items=args.max_items))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
