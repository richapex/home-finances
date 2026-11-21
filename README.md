# Household Finance

A local, private replacement built for one household's
personal finances. It covers manual transaction entry, budgeting, reconciliation
and reports. It never connects to banks or cloud services.

> **Status:** early development. The SQLite schema
> ([src/household_finance/schema.sql](src/household_finance/schema.sql)) and
> the QIF importer work from the command line. Implementation details agreed
> during planning are in [docs/design-notes.md](docs/design-notes.md). The
> web application has not been written yet.

## Goals

- **Replace Quicken.** Bring over existing history through Quicken's QIF export
  and continue from there.
- **Keep data local.** Everything is stored in one SQLite database file. The
  web UI is served only on `127.0.0.1`.
- **Support several household members.** There are no passwords: each person
  picks their name from a list, and the app pre-selects one based on the
  Windows username and computer name. Every change records who made it.
- **Stay simple.** USD only. No investment tracking, no bank downloads, no
  sync server and no database server to install.

## Features (planned)

- Double-entry bookkeeping with split transactions
- Hierarchical accounts and categories (for example `Auto:Fuel`)
- Archiving of accounts and categories: archived ones keep all their history
  and still appear in reports, but are not offered when entering new
  transactions
- Payees with default categories, and tags (Quicken classes import as tags)
- Scheduled and recurring transactions, with optional automatic entry
- Statement reconciliation
- Monthly budgets by category
- Receipt and document attachments (files stored on disk)
- Full audit log of inserts, updates and deletes
- QIF import, with batches that can be rolled back (working)

## Primary and satellite installations

The program can run on more than one computer without a shared server:

- One installation is the **primary**. It holds the authoritative books, and
  reconciliation happens only there.
- Other installations are **satellites**, such as a laptop used for entering
  transactions while away. A satellite exports its unreconciled transactions
  to a human-readable JSON file.
- The primary imports that file. Records are matched by UUID, so importing the
  same file twice is safe. Satellite accounts, payees, tags and users are mapped
  to local ones once, and the mapping is remembered for later imports.
- Deletions are not propagated, and there is no general two-way merge.
- A satellite is refreshed from the primary by copying the primary's database
  file.

## Data model

The schema in [src/household_finance/schema.sql](src/household_finance/schema.sql)
needs SQLite 3.37 or later for STRICT tables. The SQLite built into Python
3.12 and later is recent enough, so nothing has to be installed. Key
conventions:

| Topic        | Convention                                                                                                                                                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Double entry | Every transaction has one or more splits, and the splits sum to zero. Categories are accounts of kind `income` or `expense`.                                                         |
| Signs        | Positive amounts are debits and negative amounts are credits. Liability, income and equity balances are therefore stored as negative numbers, and the UI flips the sign for display. |
| Money        | Integer cents (`-12000` is -$120.00), never floating point. Python code uses `decimal.Decimal`.                                                                                     |
| Time         | Dates are `YYYY-MM-DD` text; timestamps are UTC text, `YYYY-MM-DD HH:MM:SS`.                                                                                                        |
| Identity     | Integer primary keys are used for joins. 16-byte UUIDs give records a stable identity across installations.                                                                         |
| Names        | Names are unique without regard to case or accents, using a `name_key` column the app fills in.                                                                                     |
| Auditing     | `created_by`, `updated_by`, `updated_at` and `audit_log` rows are written by the application, not by triggers.                                                                      |

Useful views:

- `v_account_paths`: the full colon-separated path of every account
- `v_selectable_accounts`: the accounts and categories that are not archived,
  for the pickers used when entering transactions
- `v_account_balances`: current, cleared and reconciled balance per account, in cents
- `v_unbalanced_transactions`: an integrity check that should always return no rows

### Creating the database

The program creates the schema the first time it opens a new database file
(see [src/household_finance/db.py](src/household_finance/db.py)), with the
seed accounts `Opening Balances` (equity) and `Uncategorized` (expense). The
app's first-run setup will create the local installation record and the first
user. Each connection turns on foreign keys and write-ahead logging.

## Planned technology

Not final yet:

- Python back end using FastAPI
- Local web UI in the browser
- SQLite through Python's built-in `sqlite3` module

## Repository layout

```
docs/
  design-notes.md  Export/import format and rules, QIF import, performance, backups
src/household_finance/
  schema.sql       SQLite schema, version 1
  db.py            Connections, schema creation, transactions, backups
  money.py         Cents <-> Decimal
  qif/             QIF parser, transfer matching, import plan, database writer
tests/             pytest suite; fixtures are synthetic, never real data
```

## Development

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

## Importing from Quicken

Export from Quicken with **File → Export → QIF File**, choosing all accounts.
Keep the file outside this folder. Then:

```sh
# Check the file. Writes nothing.
.venv\Scripts\python -m household_finance.qif report export.qif --year 2025

# Import into a scratch database first (the file is created if missing).
.venv\Scripts\python -m household_finance.qif import export.qif --db scratch.sqlite

# List imports, and undo one.
.venv\Scripts\python -m household_finance.qif batches --db scratch.sqlite
.venv\Scripts\python -m household_finance.qif rollback 1 --db scratch.sqlite
```

The report shows each account's balance, cleared balance and reconciled
balance to compare with Quicken, transfers that need review, and warnings.
Add `--register NAME` to list one account's transactions with a running
balance. Importing or rolling back a database that already holds
transactions first writes a backup to a `backups` folder next to it. The
same file cannot be imported twice unless its earlier batch was rolled back
or `--allow-duplicate` is given.
