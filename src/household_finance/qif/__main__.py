"""Command line for QIF import.

    python -m household_finance.qif report   FILE.qif [options]
    python -m household_finance.qif import   FILE.qif --db PATH [options]
    python -m household_finance.qif batches  --db PATH
    python -m household_finance.qif rollback BATCH_ID --db PATH

`report` writes nothing. `import` prints the same report, backs up the
database (when it already holds data) and imports the file as one batch.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from .. import db
from .parser import read_qif_file
from .plan import build_plan
from .report import format_report
from .writer import (
    DuplicateImport,
    ImportConflict,
    RollbackRefused,
    check_not_duplicate,
    file_sha256,
    import_plan,
    list_batches,
    rollback_batch,
)


def _plan_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("file", help="QIF file exported from Quicken")
    p.add_argument("--year", type=int, help="also print category totals for this year")
    p.add_argument("--register", metavar="ACCOUNT",
                   help="also print this account's register with running balance")
    p.add_argument("--date-window", type=int, default=7, metavar="DAYS",
                   help="accept transfer halves up to this many days apart (default 7)")
    p.add_argument("--max-items", type=int, default=50, metavar="N",
                   help="list at most N warnings / transfer issues (default 50)")
    p.add_argument("--account-name", metavar="NAME",
                   help="account name for a single-account file without an !Account "
                        "header (default: the file name)")


def _backup_if_needed(conn: sqlite3.Connection, db_path: str, label: str,
                      skip: bool) -> None:
    has_data = conn.execute("SELECT EXISTS (SELECT 1 FROM transactions)").fetchone()[0]
    if skip or not has_data:
        return
    dest = db.backup(conn, Path(db_path).resolve().parent / "backups", label)
    print(f"Backup written to {dest}")


def cmd_report(args: argparse.Namespace) -> int:
    plan = build_plan(read_qif_file(args.file, args.account_name), args.date_window)
    sys.stdout.write(format_report(plan, year=args.year, register=args.register,
                                   max_items=args.max_items))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    plan = build_plan(read_qif_file(args.file, args.account_name), args.date_window)
    sys.stdout.write(format_report(plan, year=args.year, register=args.register,
                                   max_items=args.max_items))
    digest = file_sha256(args.file)
    conn = db.connect(args.db)
    try:
        # Refuse a duplicate before backing up, so a refused import writes nothing.
        if not args.allow_duplicate:
            check_not_duplicate(conn, digest)
        _backup_if_needed(conn, args.db, "before-qif-import", args.no_backup)
        result = import_plan(conn, plan, file_name=Path(args.file).name,
                             file_sha256=digest,
                             allow_duplicate=args.allow_duplicate)
    except (DuplicateImport, ImportConflict) as exc:
        print(f"\nNot imported: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"\nIMPORTED into {args.db} as batch {result.batch_id}")
    print(f"  transactions: {result.transactions_inserted}")
    print(f"  accounts/categories created: {result.accounts_created}, "
          f"reused: {result.accounts_reused}")
    print(f"  payees created: {result.payees_created}   tags created: {result.tags_created}")
    for path, n in sorted(result.archived_accounts_used.items()):
        print(f"  posted to archived account {path!r}: {n} split(s)")
    print(f"Undo with: python -m household_finance.qif rollback {result.batch_id} "
          f"--db {args.db}")
    return 0


def cmd_batches(args: argparse.Namespace) -> int:
    conn = db.connect(args.db, create=False)
    try:
        rows = list_batches(conn)
    finally:
        conn.close()
    if not rows:
        print("No imports yet.")
    for b in rows:
        print(f"{b['id']:>4}  {b['imported_at']}  {b['source_type']:<4}  "
              f"{b['status']:<11}  {b['transactions_inserted']:>7} txns  {b['file_name']}")
    return 0


def cmd_rollback(args: argparse.Namespace) -> int:
    conn = db.connect(args.db, create=False)
    try:
        _backup_if_needed(conn, args.db, f"before-rollback-{args.batch_id}", args.no_backup)
        result = rollback_batch(conn, args.batch_id)
    except RollbackRefused as exc:
        print(f"Not rolled back: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    print(f"Rolled back batch {result.batch_id}: {result.transactions_deleted} transactions, "
          f"{result.accounts_deleted} accounts/categories, {result.payees_deleted} payees, "
          f"{result.tags_deleted} tags deleted")
    for item in result.kept:
        print(f"  kept {item} (still in use)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m household_finance.qif",
                                 description="Import Quicken QIF exports.")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="check a QIF file; writes nothing")
    _plan_options(p)
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("import", help="import a QIF file into a database")
    _plan_options(p)
    p.add_argument("--db", required=True,
                   help="SQLite database file (created if it does not exist)")
    p.add_argument("--allow-duplicate", action="store_true",
                   help="import even if this exact file was imported before")
    p.add_argument("--no-backup", action="store_true",
                   help="skip the automatic backup before importing")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("batches", help="list import batches")
    p.add_argument("--db", required=True)
    p.set_defaults(func=cmd_batches)

    p = sub.add_parser("rollback", help="undo an import batch")
    p.add_argument("batch_id", type=int)
    p.add_argument("--db", required=True)
    p.add_argument("--no-backup", action="store_true",
                   help="skip the automatic backup before rolling back")
    p.set_defaults(func=cmd_rollback)

    args = ap.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, db.SchemaVersionError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
