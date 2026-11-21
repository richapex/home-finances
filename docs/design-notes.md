# Design notes

Decisions agreed during planning that are not captured by
[README.md](../README.md) or [db/schema.sql](../db/schema.sql). Read this
before implementing export/import, the QIF importer, account registers or
backups.

## 1. JSON export file format

A satellite exports its unreconciled transactions to one human-readable JSON
file. Structure:

```json
{
  "format": "household-finance-export",
  "format_version": 1,
  "exported_at": "2026-11-19T14:32:00Z",
  "source_installation": { "uuid": "…", "name": "Laptop" },
  "exported_by": { "uuid": "…", "name": "Pat" },

  "accounts": [
    { "uuid": "…", "path": "Visa", "kind": "liability", "subtype": "credit_card" },
    { "uuid": "…", "path": "Groceries", "kind": "expense", "subtype": "category" }
  ],
  "payees": [ { "uuid": "…", "name": "Safeway" } ],
  "tags":   [],
  "users":  [ { "uuid": "…", "name": "Pat" } ],

  "transactions": [
    {
      "uuid": "…",
      "date": "2026-11-17",
      "payee": "…payee uuid…",
      "check_number": null,
      "memo": "",
      "created_by": "…user uuid…",
      "created_at": "2026-11-17T18:05:12Z",
      "updated_at": "2026-11-17T18:05:12Z",
      "splits": [
        { "uuid": "…", "account": "…Visa uuid…",      "amount": "-120.00", "cleared": "uncleared", "memo": "", "tags": [] },
        { "uuid": "…", "account": "…Groceries uuid…", "amount": "100.00",  "cleared": "uncleared", "memo": "", "tags": [] },
        { "uuid": "…", "account": "…Household uuid…", "amount": "20.00",   "cleared": "uncleared", "memo": "", "tags": [] }
      ]
    }
  ]
}
```

Rules:

- **Amounts are strings** with exactly two decimals (`"-120.00"`), parsed
  with `decimal.Decimal`. Never serialize money as a JSON number.
- UUIDs are written in the standard hyphenated text form.
- Timestamps are UTC with a `Z` suffix; dates are `YYYY-MM-DD`.
- The `accounts`, `payees`, `tags` and `users` sections list **only the items
  referenced** by the exported transactions, with both UUID and a readable
  name or full colon-separated path. This is how accounts or payees created on
  the satellite travel with their transactions.
- Transactions refer to those items by UUID.

## 2. Export and import rules

### Which transactions are exported

- Any transaction with **at least one split that is not `reconciled`**. A
  transfer can be reconciled on one side but not the other, so the check is
  per split.
- Optional filters: "entered on this installation"
  (`origin_installation_id`) and "changed since the last export".
- Re-exporting the same transactions is harmless because import is
  idempotent, so exports do not need to track exactly what was sent. Each
  export is logged in the `exports` table.

### How the primary imports a file

1. **Validate the file:** reject an unknown `format`, or a `format_version`
   newer than the app supports. Every transaction must have at least one split,
   and its splits must sum to zero.
2. **Map reference data** (accounts, payees, tags, users), in this order:
   - a remembered mapping in `import_mappings` for this source installation;
   - a local row with the same UUID;
   - a local row with the same name or path;
   - otherwise show the mapping screen: "Create new" or "Map to existing…".
   Save every choice in `import_mappings` so the screen appears only once.
3. **Apply each transaction by UUID:**

   | Situation on the primary | Action |
   | --- | --- |
   | UUID not present | Insert it, with `import_batch_id` set |
   | Present and identical | Skip |
   | Present but different | Show local and incoming versions side by side; the user chooses which to keep |
   | Present and any split already `reconciled` | Never overwrite; list it in the import summary as skipped |

4. **Run the whole import in one database transaction.** If anything fails,
   nothing is applied. Record counts in `import_batches` and write
   `audit_log` rows with `context = 'json_import'`.
5. The import summary also lists transactions that landed on archived
   accounts (see the archiving rules in schema.sql).

### Known limitations (accepted)

- **Deletions do not propagate.** A transaction deleted on the satellite after
  export stays on the primary. The import preview makes this easy to spot.
- **No general merge.** Reconciliation happens only on the primary.

## 3. Refreshing satellites

Data flows in two directions, by two different mechanisms:

| Direction | Mechanism | Contents |
| --- | --- | --- |
| Satellite → primary | Incremental JSON export (above) | Unreconciled transactions only |
| Primary → satellite | Full database copy | Everything, replacing the satellite's data |

The full copy keeps satellites' accounts, payees, history and reconciled
status current. It can be a `mysqldump` of the primary or an app-level full
export; either way the satellite's own `installations` row (its
`is_this_installation` flag and identity) must survive the refresh. Before
replacing its data, the satellite should warn if it holds transactions that
have not been exported to the primary since they were last changed.

## 4. QIF import from Quicken for Windows

### Source

- The existing data is in the latest **Quicken for Windows**.
- Export with **File → Export → QIF File**, choosing all accounts.
- QIF may drop or simplify some data, such as attachments and some
  scheduled-bill details. Expect to recreate scheduled transactions by hand.

### Mapping

| QIF | This app |
| --- | --- |
| `!Account` blocks, `!Type:Bank` / `CCard` / `Cash` / `Oth A` / `Oth L` | `accounts` with matching kind/subtype |
| `!Type:Invst` accounts | **Skipped** (no investment tracking) |
| Category list, `Auto:Fuel` | Parent/child `accounts` of kind income/expense |
| `L[Account Name]` | Transfer to that account |
| `/Class` after a category | Tag |
| `P` payee | `payees` (create on first use) |
| `N` | `check_number` |
| `M` | memo |
| `C*` | `cleared` |
| `CX` | `reconciled` (no `reconciliation_id`) |
| `S` / `E` / `$` lines | One split each |
| No category | The system `Uncategorized` account |
| Opening Balance record | Transaction against the system `Opening Balances` account |

Quicken's date format varies (for example `1/ 5'26`); the parser must handle
these. Keep the original QIF account/category name in `source_ref`.

### Transfers: the riskiest part

A transfer appears in **both** accounts' registers in a QIF export. The
importer must merge the two halves into a single double-entry transaction:

- Match on date, opposite amount, and each side naming the other with
  `[Account]`. Split transactions can contain transfer lines, which must be
  matched against the other account's whole transaction.
- Keep each side's own cleared status on its own split.
- Report unmatched halves for review rather than guessing.

### Implementation rules (src/household_finance/qif)

The importer works in two stages. `parser.py` reads the file into typed
records. `plan.py` turns those into a database-independent **import plan**:
the accounts, payees and tags to create, plus balanced transactions. The
database writer (not written yet) will insert the plan. Rules decided while
building it:

- **Transfer merge.** Matched register transactions are merged into one
  transaction. It contains each register transaction's own account split plus
  its unmatched lines. A matched pair of transfer lines is dropped, because
  the other side's account split already represents it. So a merged group
  always balances, including a split with several transfer lines.
- **Matching order.** Same date first, then up to 7 days apart (the
  `--date-window` option). A date mismatch is accepted but reported, and the
  transaction takes the date of its primary half: the split side, or else the
  half that appears first in the file.
- **Unmatched transfer line.** It still posts to the named account, so the
  register it came from keeps the right balance, and it is listed for review.
  If both halves exist but fail to match, the target account is
  double-counted. Comparing balances with Quicken catches this.
- **Accounts without a register in the file**, meaning investment accounts
  and accounts that are named in `[...]` but were not exported, are created
  as placeholders (`investment` / `retirement` / `other_asset`). Transfers to
  them post there without being reported, so their balance shows only net
  transfers. Investment registers themselves are skipped.
- **Self-transfer** (`L[Checking]` inside Checking, Quicken's opening balance
  record) posts to `Opening Balances`.
- **Cleared status of category splits** copies the register's status. If
  category splits stayed `uncleared`, every imported transaction would count
  as unreconciled and be exported from satellites forever.
- **Name clash** between a top-level category and an account (for example
  account `Mortgage` and category `Mortgage:Interest`): the category is renamed
  to `Mortgage (category)`. Names are compared case- and accent-insensitively,
  like the database collation. A `:` in an account name becomes `-`.
- **Categories used but not listed** are created as income if their net
  register amount is positive, otherwise expense. A child takes its parent's
  kind.
- **Split totals** that differ from the transaction amount put the remainder
  in `Uncategorized` and add a warning.
- **Tags.** A Quicken class is kept as one tag, even when it has a `:`
  subclass (for example `Business:Travel`). A class on a matched transfer line
  goes on the other account's split.
- **Payee and memo of a merge.** The payee, check number and memo come from
  the primary half. A memo on the other half is kept on its account split
  when it differs from the header memo.

Check a file without touching any database:

```sh
python -m household_finance.qif export.qif --year 2025 --register Checking
```

### Validation

1. Import into a **scratch database** first, never straight into the real one.
2. Compare every account's ending balance and reconciled balance with Quicken.
3. Spot-check category totals for a couple of years against Quicken reports.
4. Check that `v_unbalanced_transactions` returns no rows.
5. Only then import into the real database. A bad import can be rolled back
   by its `import_batches` row.

## 5. Running balances and performance

- Do not store running balances.
- An account register page of N rows gets its starting balance from one
  `SUM(amount)` of the account's splits before the page, then a window
  function over only the visible rows:
  `SUM(amount) OVER (ORDER BY txn_date, transaction_id, id)`.
  The cost stays roughly constant as history grows.
- The covering index `ix_splits_register` makes these sums index-only.
- Fallback, only if measurement shows it is needed: a
  `balance_snapshots(account_id, as_of_date, balance)` table, with
  balance = snapshot + splits since the snapshot.

## 6. Backups

- Run a scheduled `mysqldump` of the primary's database, for example daily,
  kept on a different drive or cloud folder.
- Take a backup automatically before any import (QIF or JSON) and before a
  satellite refresh.
- Attachment files on disk must be backed up alongside the database dump.
