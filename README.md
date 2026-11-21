# Household Finance

A local, private replacement built for one household's
personal finances. It covers manual transaction entry, budgeting, reconciliation
and reports. It never connects to banks or cloud services.

> **Status:** early design. The database schema ([db/schema.sql](db/schema.sql))
> is drafted, and implementation details agreed during planning are in
> [docs/design-notes.md](docs/design-notes.md). The application itself has not
> been written yet.

## Goals

- **Replace Quicken.** Bring over existing history through Quicken's QIF export
  and continue from there.
- **Keep data local.** Everything is stored in a local MySQL database. The web
  UI is served only on `127.0.0.1`.
- **Support several household members.** There are no passwords: each person
  picks their name from a list, and the app pre-selects one based on the
  Windows username and computer name. Every change records who made it.
- **Stay simple.** USD only. No investment tracking, no bank downloads and no
  sync server.

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
- QIF import, with batches that can be rolled back

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

## Data model

The schema in [db/schema.sql](db/schema.sql) targets MySQL 8.0.16 or later
(8.4 LTS recommended). Key conventions:

| Topic        | Convention                                                                                                                                                                           |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Double entry | Every transaction has one or more splits, and the splits sum to zero. Categories are accounts of kind `income` or `expense`.                                                         |
| Signs        | Positive amounts are debits and negative amounts are credits. Liability, income and equity balances are therefore stored as negative numbers, and the UI flips the sign for display. |
| Money        | `DECIMAL(15,2)` throughout, never floating point.                                                                                                                                    |
| Time         | `DATETIME` columns hold UTC. Each connection sets `time_zone = '+00:00'`.                                                                                                            |
| Identity     | Integer primary keys are used for joins. `BINARY(16)` UUIDs give records a stable identity across installations.                                                                     |
| Auditing     | `created_by`, `updated_by` and `audit_log` rows are written by the application, not by triggers.                                                                                     |

Useful views:

- `v_account_paths`: the full colon-separated path of every account
- `v_selectable_accounts`: the accounts and categories that are not archived,
  for the pickers used when entering transactions
- `v_account_balances`: current, cleared and reconciled balance per account
- `v_unbalanced_transactions`: an integrity check that should always return no rows

### Creating the database

```sh
mysql -u root -p < db/schema.sql
```

This creates the `household_finance` database with the seed accounts
`Opening Balances` (equity) and `Uncategorized` (expense). The app's first-run
setup will create the local installation record and the first user.

## Planned technology

Not final yet:

- Python back end using FastAPI, SQLAlchemy and Alembic for migrations
- Local web UI in the browser
- MySQL 8

## Repository layout

```
db/
  schema.sql       MySQL schema, version 1
docs/
  design-notes.md  Export/import format and rules, QIF import, performance, backups
src/household_finance/
  qif/             QIF parser, transfer matching and import plan (no database yet)
tests/             pytest suite; fixtures are synthetic, never real data
```

## Development

```sh
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

Check a Quicken QIF export without writing to any database:

```sh
.venv\Scripts\python -m household_finance.qif path\to\export.qif --year 2025
```

The report shows each account's balance, cleared balance and reconciled
balance to compare with Quicken, transfers that need review, and warnings.
Add `--register NAME` to list one account's transactions with a running
balance.
