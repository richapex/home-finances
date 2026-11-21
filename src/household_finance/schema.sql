-- =============================================================================
-- Household Finance — SQLite schema, version 1
-- Requires SQLite 3.37+ (STRICT tables). Applied by household_finance.db,
-- which also sets the per-connection pragmas below.
--
-- DESIGN NOTES
-- * Double-entry: every transaction has >= 1 split, and its splits SUM to 0.
--   Categories are accounts of kind 'income' / 'expense'.
--   Sum-to-zero is enforced by the application inside a DB transaction;
--   view v_unbalanced_transactions is the safety check.
-- * Sign convention: amount > 0 = debit (money INTO an asset, an expense
--   incurred); amount < 0 = credit (money OUT of an asset, a charge on a
--   credit card, income earned). Liability / income / equity balances are
--   therefore negative; the UI flips the sign for display.
-- * Money: INTEGER cents everywhere (-12000 = -$120.00). Never REAL.
--   household_finance.money converts to and from decimal.Decimal.
-- * Dates are TEXT 'YYYY-MM-DD'. Timestamps are TEXT UTC
--   'YYYY-MM-DD HH:MM:SS' (SQLite's CURRENT_TIMESTAMP is UTC).
-- * IDs: integer PKs for internal joins; 16-byte BLOB uuid columns are the
--   stable identity used by satellite -> primary JSON export/import. The app
--   supplies them (uuid.uuid4().bytes); there is no database default.
-- * Names: name_key holds the case- and accent-insensitive form of name
--   (household_finance.text.name_key), filled by the app; uniqueness is
--   enforced on name_key, so "Café" and "cafe" cannot both exist.
-- * Users / audit: created_by / updated_by, updated_at and audit_log rows are
--   written by the application (it knows the current user); there are no
--   audit triggers. The app must bump updated_at on every change.
-- * Per connection the app runs: PRAGMA foreign_keys = ON;
--   PRAGMA journal_mode = WAL; PRAGMA busy_timeout = 5000.
-- * Schema version: PRAGMA user_version.
-- * Reconciliation is done only on the primary installation; editing
--   reconciled splits is blocked by the application.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- Settings: key/value app preferences
-- -----------------------------------------------------------------------------
CREATE TABLE settings (
  setting_key    TEXT NOT NULL PRIMARY KEY,
  setting_value  TEXT NULL
) STRICT;

-- -----------------------------------------------------------------------------
-- Installations: this copy of the program plus every copy it has exchanged
-- data with. Exactly one row is flagged is_this_installation; at most one
-- row is flagged is_primary. The app creates the local row on first run.
-- -----------------------------------------------------------------------------
CREATE TABLE installations (
  id                    INTEGER NOT NULL PRIMARY KEY,
  uuid                  BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  name                  TEXT    NOT NULL,                 -- "Den PC", "Laptop"
  is_primary            INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
  is_this_installation  INTEGER NOT NULL DEFAULT 0 CHECK (is_this_installation IN (0, 1)),
  created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE UNIQUE INDEX uq_installations_primary ON installations (is_primary) WHERE is_primary = 1;
CREATE UNIQUE INDEX uq_installations_this ON installations (is_this_installation) WHERE is_this_installation = 1;

-- -----------------------------------------------------------------------------
-- Users: household members. No passwords. The default user is chosen by
-- matching the Windows account (and optionally computer name) running the
-- back end; the UI lets the person switch.
-- -----------------------------------------------------------------------------
CREATE TABLE users (
  id                INTEGER NOT NULL PRIMARY KEY,
  uuid              BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  display_name      TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  windows_username  TEXT    NULL,
  computer_name     TEXT    NULL,   -- NULL = matches on any computer
  is_active         INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  created_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE INDEX ix_users_windows ON users (windows_username, computer_name);

-- -----------------------------------------------------------------------------
-- Import batches: one row per QIF import or satellite JSON import.
-- Rolling back a batch deletes the transactions it inserted, then the
-- accounts, payees and tags it created that nothing else uses any more.
-- Updates it made to existing transactions are recoverable from audit_log.
-- -----------------------------------------------------------------------------
CREATE TABLE import_batches (
  id                      INTEGER NOT NULL PRIMARY KEY,
  source_type             TEXT    NOT NULL CHECK (source_type IN ('qif', 'json')),
  source_installation_id  INTEGER NULL REFERENCES installations (id),  -- set for 'json'
  file_name               TEXT    NOT NULL,
  file_sha256             TEXT    NULL,   -- warns when the same file is imported twice
  imported_by             INTEGER NULL REFERENCES users (id),
  imported_at             TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status                  TEXT    NOT NULL DEFAULT 'in_progress'
                            CHECK (status IN ('in_progress', 'completed', 'rolled_back')),
  transactions_inserted   INTEGER NOT NULL DEFAULT 0,
  transactions_updated    INTEGER NOT NULL DEFAULT 0,
  transactions_skipped    INTEGER NOT NULL DEFAULT 0,
  rolled_back_by          INTEGER NULL REFERENCES users (id),
  rolled_back_at          TEXT    NULL,
  notes                   TEXT    NULL
) STRICT;
CREATE INDEX ix_import_batches_sha ON import_batches (file_sha256);

-- -----------------------------------------------------------------------------
-- Accounts: real accounts (asset / liability), categories (income / expense)
-- and equity. Hierarchical via parent_id; full path is "Auto:Fuel".
-- A child must have the same kind as its parent (enforced by the app).
--
-- ARCHIVING (accounts and categories alike). An archived account keeps all
-- of its splits, reconciliations and budget history; it is only removed from
-- the lists used to enter new data. Rules the app enforces:
-- * Not offered in account/category pickers for new transactions or splits,
--   scheduled transactions, payee default categories or new budget lines.
--   A payee whose default category is archived gets no category auto-filled.
-- * Existing transactions still show it, and editing one may keep a split on
--   it, but a split cannot be moved TO an archived account.
-- * Reports, registers and searches still include it (the account list
--   offers a "show archived" toggle).
-- * Archiving a parent archives all its descendants; un-archiving a child
--   un-archives its ancestors. So an active account never has an archived
--   parent.
-- * Archiving warns (does not block) when the balance is non-zero, there are
--   uncleared splits, or an active schedule uses the account.
-- * System accounts (is_system) cannot be archived (CHECK below).
-- * Imports may still post to an archived account (e.g. a satellite mapping
--   or a QIF category); the import summary lists those transactions.
-- -----------------------------------------------------------------------------
CREATE TABLE accounts (
  id                    INTEGER NOT NULL PRIMARY KEY,
  uuid                  BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  parent_id             INTEGER NULL REFERENCES accounts (id) ON DELETE RESTRICT,
  name                  TEXT    NOT NULL CHECK (name <> '' AND instr(name, ':') = 0),
  name_key              TEXT    NOT NULL,
  kind                  TEXT    NOT NULL
                          CHECK (kind IN ('asset', 'liability', 'income', 'expense', 'equity')),
  subtype               TEXT    NOT NULL
                          CHECK (subtype IN ('checking', 'savings', 'cash', 'credit_card',
                                             'loan', 'mortgage', 'line_of_credit',
                                             'investment', 'retirement', 'property',
                                             'other_asset', 'other_liability',
                                             'category', 'equity')),
  institution           TEXT    NULL,
  account_number_last4  TEXT    NULL CHECK (length(account_number_last4) = 4),
  tax_line              TEXT    NULL,   -- e.g. 'Schedule A: Charitable'
  is_system             INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0, 1)),  -- seed accounts the app relies on
  is_archived           INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
  archived_at           TEXT    NULL,                    -- set when is_archived becomes 1
  sort_order            INTEGER NOT NULL DEFAULT 0,
  notes                 TEXT    NULL,
  source_ref            TEXT    NULL,   -- e.g. original QIF account/category name
  import_batch_id       INTEGER NULL REFERENCES import_batches (id) ON DELETE SET NULL,  -- created by this import
  created_by            INTEGER NULL REFERENCES users (id),
  created_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by            INTEGER NULL REFERENCES users (id),
  updated_at            TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CHECK ((kind IN ('income', 'expense')) = (subtype = 'category')),
  CHECK ((kind = 'equity') = (subtype = 'equity')),
  CHECK (is_archived = (archived_at IS NOT NULL)),
  CHECK (NOT (is_system AND is_archived))
) STRICT;
-- Sibling names are unique, top-level (NULL parent) names included.
CREATE UNIQUE INDEX uq_accounts_sibling_name ON accounts (ifnull(parent_id, 0), name_key);
CREATE INDEX ix_accounts_kind ON accounts (kind);
CREATE INDEX ix_accounts_import_batch ON accounts (import_batch_id);

-- -----------------------------------------------------------------------------
-- Payees
-- -----------------------------------------------------------------------------
CREATE TABLE payees (
  id                  INTEGER NOT NULL PRIMARY KEY,
  uuid                BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  name                TEXT    NOT NULL,
  name_key            TEXT    NOT NULL UNIQUE,
  default_account_id  INTEGER NULL REFERENCES accounts (id) ON DELETE SET NULL,  -- category auto-filled on entry
  is_hidden           INTEGER NOT NULL DEFAULT 0 CHECK (is_hidden IN (0, 1)),
  notes               TEXT    NULL,
  import_batch_id     INTEGER NULL REFERENCES import_batches (id) ON DELETE SET NULL,
  created_by          INTEGER NULL REFERENCES users (id),
  created_at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by          INTEGER NULL REFERENCES users (id),
  updated_at          TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE INDEX ix_payees_import_batch ON payees (import_batch_id);

-- -----------------------------------------------------------------------------
-- Tags (Quicken "classes" import as tags)
-- -----------------------------------------------------------------------------
CREATE TABLE tags (
  id               INTEGER NOT NULL PRIMARY KEY,
  uuid             BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  name             TEXT    NOT NULL,
  name_key         TEXT    NOT NULL UNIQUE,
  description      TEXT    NULL,
  is_hidden        INTEGER NOT NULL DEFAULT 0 CHECK (is_hidden IN (0, 1)),
  import_batch_id  INTEGER NULL REFERENCES import_batches (id) ON DELETE SET NULL,
  created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE INDEX ix_tags_import_batch ON tags (import_batch_id);

-- -----------------------------------------------------------------------------
-- Import mappings: remembers "satellite account/payee/tag/user X = local Y"
-- so the mapping screen is shown only the first time.
-- local_id refers to the table named by entity_type (no FK: polymorphic).
-- -----------------------------------------------------------------------------
CREATE TABLE import_mappings (
  id                      INTEGER NOT NULL PRIMARY KEY,
  source_installation_id  INTEGER NOT NULL REFERENCES installations (id) ON DELETE CASCADE,
  entity_type             TEXT    NOT NULL CHECK (entity_type IN ('account', 'payee', 'tag', 'user')),
  source_uuid             BLOB    NOT NULL CHECK (length(source_uuid) = 16),
  local_id                INTEGER NOT NULL,
  created_at              TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (source_installation_id, entity_type, source_uuid)
) STRICT;

-- -----------------------------------------------------------------------------
-- Exports: history of JSON exports made from this installation
-- -----------------------------------------------------------------------------
CREATE TABLE exports (
  id                 INTEGER NOT NULL PRIMARY KEY,
  exported_at        TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  exported_by        INTEGER NULL REFERENCES users (id),
  file_name          TEXT    NOT NULL,
  transaction_count  INTEGER NOT NULL DEFAULT 0,
  filter_json        TEXT    NULL CHECK (filter_json IS NULL OR json_valid(filter_json))
) STRICT;

-- -----------------------------------------------------------------------------
-- Reconciliations: one per statement reconciled (primary installation only)
-- -----------------------------------------------------------------------------
CREATE TABLE reconciliations (
  id                        INTEGER NOT NULL PRIMARY KEY,
  account_id                INTEGER NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
  statement_date            TEXT    NOT NULL CHECK (statement_date = date(statement_date)),
  statement_start_balance   INTEGER NOT NULL,   -- cents
  statement_ending_balance  INTEGER NOT NULL,   -- cents
  status                    TEXT    NOT NULL DEFAULT 'in_progress'
                              CHECK (status IN ('in_progress', 'completed')),
  started_by                INTEGER NULL REFERENCES users (id),
  started_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_by              INTEGER NULL REFERENCES users (id),
  completed_at              TEXT    NULL
) STRICT;
CREATE INDEX ix_reconciliations_account ON reconciliations (account_id, statement_date);

-- -----------------------------------------------------------------------------
-- Scheduled (recurring) transactions and their split templates
-- -----------------------------------------------------------------------------
CREATE TABLE scheduled_transactions (
  id               INTEGER NOT NULL PRIMARY KEY,
  uuid             BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  name             TEXT    NOT NULL,
  payee_id         INTEGER NULL REFERENCES payees (id) ON DELETE SET NULL,
  memo             TEXT    NOT NULL DEFAULT '',
  frequency        TEXT    NOT NULL
                     CHECK (frequency IN ('once', 'daily', 'weekly', 'biweekly', 'semimonthly',
                                          'monthly', 'quarterly', 'semiannually', 'annually')),
  interval_count   INTEGER NOT NULL DEFAULT 1 CHECK (interval_count >= 1),   -- every N periods
  next_due_date    TEXT    NOT NULL CHECK (next_due_date = date(next_due_date)),
  end_date         TEXT    NULL CHECK (end_date IS NULL OR end_date = date(end_date)),
  remaining_count  INTEGER NULL CHECK (remaining_count IS NULL OR remaining_count >= 0),  -- NULL = no limit
  auto_enter       INTEGER NOT NULL DEFAULT 0 CHECK (auto_enter IN (0, 1)),
  days_in_advance  INTEGER NOT NULL DEFAULT 0 CHECK (days_in_advance >= 0),  -- remind/enter N days early
  is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  created_by       INTEGER NULL REFERENCES users (id),
  created_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by       INTEGER NULL REFERENCES users (id),
  updated_at       TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE INDEX ix_scheduled_transactions_due ON scheduled_transactions (is_active, next_due_date);

CREATE TABLE scheduled_splits (
  id                        INTEGER NOT NULL PRIMARY KEY,
  scheduled_transaction_id  INTEGER NOT NULL REFERENCES scheduled_transactions (id) ON DELETE CASCADE,
  account_id                INTEGER NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
  amount                    INTEGER NOT NULL,   -- cents
  memo                      TEXT    NOT NULL DEFAULT '',
  sort_order                INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX ix_scheduled_splits_parent ON scheduled_splits (scheduled_transaction_id);
CREATE INDEX ix_scheduled_splits_account ON scheduled_splits (account_id);

-- -----------------------------------------------------------------------------
-- Transactions: the header (date, payee, who/where entered)
-- -----------------------------------------------------------------------------
CREATE TABLE transactions (
  id                        INTEGER NOT NULL PRIMARY KEY,
  uuid                      BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  txn_date                  TEXT    NOT NULL CHECK (txn_date = date(txn_date)),
  payee_id                  INTEGER NULL REFERENCES payees (id) ON DELETE SET NULL,
  check_number              TEXT    NULL,   -- '1234', 'ATM', 'EFT', ...
  memo                      TEXT    NOT NULL DEFAULT '',
  origin_installation_id    INTEGER NULL REFERENCES installations (id),           -- where it was first entered
  import_batch_id           INTEGER NULL REFERENCES import_batches (id) ON DELETE SET NULL,  -- set if it arrived by import
  scheduled_transaction_id  INTEGER NULL REFERENCES scheduled_transactions (id) ON DELETE SET NULL,
  source_ref                TEXT    NULL,   -- e.g. QIF account + record position
  created_by                INTEGER NULL REFERENCES users (id),
  created_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by                INTEGER NULL REFERENCES users (id),
  -- The app must bump updated_at whenever the transaction or any of its
  -- splits change: import conflict detection compares this value.
  updated_at                TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE INDEX ix_transactions_date ON transactions (txn_date);
CREATE INDEX ix_transactions_payee ON transactions (payee_id, txn_date);
CREATE INDEX ix_transactions_import_batch ON transactions (import_batch_id);
CREATE INDEX ix_transactions_origin ON transactions (origin_installation_id);
CREATE INDEX ix_transactions_scheduled ON transactions (scheduled_transaction_id);

-- -----------------------------------------------------------------------------
-- Splits: the lines of a transaction. SUM(amount) per transaction must be 0.
-- txn_date is a copy of transactions.txn_date so account registers and
-- reports are served from one index. The app supplies it on insert; the
-- triggers below reject a mismatch and follow changes to the transaction.
-- 'reconciled' splits may have reconciliation_id NULL (history imported from
-- Quicken has no reconciliation records).
-- -----------------------------------------------------------------------------
CREATE TABLE splits (
  id                 INTEGER NOT NULL PRIMARY KEY,
  uuid               BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  transaction_id     INTEGER NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
  account_id         INTEGER NOT NULL REFERENCES accounts (id) ON DELETE RESTRICT,
  txn_date           TEXT    NOT NULL,
  amount             INTEGER NOT NULL,   -- cents
  memo               TEXT    NOT NULL DEFAULT '',
  cleared            TEXT    NOT NULL DEFAULT 'uncleared'
                       CHECK (cleared IN ('uncleared', 'cleared', 'reconciled')),
  reconciliation_id  INTEGER NULL REFERENCES reconciliations (id) ON DELETE SET NULL,
  sort_order         INTEGER NOT NULL DEFAULT 0
) STRICT;
CREATE INDEX ix_splits_transaction ON splits (transaction_id);
-- Covering index for registers, running balances and per-account reports.
CREATE INDEX ix_splits_register ON splits (account_id, txn_date, transaction_id, amount, cleared);
-- Covering index for date-range reports across all categories.
CREATE INDEX ix_splits_date ON splits (txn_date, account_id, amount);
CREATE INDEX ix_splits_reconciliation ON splits (reconciliation_id);

CREATE TABLE split_tags (
  split_id  INTEGER NOT NULL REFERENCES splits (id) ON DELETE CASCADE,
  tag_id    INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
  PRIMARY KEY (split_id, tag_id)
) STRICT, WITHOUT ROWID;
CREATE INDEX ix_split_tags_tag ON split_tags (tag_id);

-- Keep splits.txn_date equal to its transaction's date.
CREATE TRIGGER trg_splits_check_date BEFORE INSERT ON splits
WHEN NEW.txn_date IS NOT (SELECT t.txn_date FROM transactions t WHERE t.id = NEW.transaction_id)
BEGIN
  SELECT RAISE(ABORT, 'splits.txn_date must equal its transaction''s txn_date');
END;

CREATE TRIGGER trg_splits_check_date_update BEFORE UPDATE OF txn_date, transaction_id ON splits
WHEN NEW.txn_date IS NOT (SELECT t.txn_date FROM transactions t WHERE t.id = NEW.transaction_id)
BEGIN
  SELECT RAISE(ABORT, 'splits.txn_date must equal its transaction''s txn_date');
END;

CREATE TRIGGER trg_transactions_sync_split_date AFTER UPDATE OF txn_date ON transactions
BEGIN
  UPDATE splits SET txn_date = NEW.txn_date
   WHERE transaction_id = NEW.id AND txn_date <> NEW.txn_date;
END;

-- -----------------------------------------------------------------------------
-- Budgets: one amount per category per month
-- -----------------------------------------------------------------------------
CREATE TABLE budgets (
  id          INTEGER NOT NULL PRIMARY KEY,
  uuid        BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  name        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  is_active   INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
  notes       TEXT    NULL,
  created_by  INTEGER NULL REFERENCES users (id),
  created_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;

CREATE TABLE budget_lines (
  id            INTEGER NOT NULL PRIMARY KEY,
  budget_id     INTEGER NOT NULL REFERENCES budgets (id) ON DELETE CASCADE,
  account_id    INTEGER NOT NULL REFERENCES accounts (id) ON DELETE CASCADE,  -- an income or expense category
  period_start  TEXT    NOT NULL   -- first day of the month
                  CHECK (period_start = date(period_start) AND substr(period_start, 9, 2) = '01'),
  amount        INTEGER NOT NULL,  -- cents, entered as a positive number
  UNIQUE (budget_id, account_id, period_start)
) STRICT;
CREATE INDEX ix_budget_lines_account ON budget_lines (account_id);

-- -----------------------------------------------------------------------------
-- Attachments (optional): files live on disk; only metadata is stored here
-- -----------------------------------------------------------------------------
CREATE TABLE attachments (
  id              INTEGER NOT NULL PRIMARY KEY,
  uuid            BLOB    NOT NULL UNIQUE CHECK (length(uuid) = 16),
  transaction_id  INTEGER NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,
  file_path       TEXT    NOT NULL,   -- relative to the app's attachment folder
  original_name   TEXT    NOT NULL,
  mime_type       TEXT    NULL,
  size_bytes      INTEGER NULL,
  sha256          TEXT    NULL,
  uploaded_by     INTEGER NULL REFERENCES users (id),
  uploaded_at     TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
) STRICT;
CREATE INDEX ix_attachments_transaction ON attachments (transaction_id);

-- -----------------------------------------------------------------------------
-- Audit log: written by the application for every insert/update/delete of
-- user data. old_values / new_values hold the row (and, for transactions,
-- its splits) as JSON, with money as strings such as "-120.00".
-- -----------------------------------------------------------------------------
CREATE TABLE audit_log (
  id               INTEGER NOT NULL PRIMARY KEY,
  occurred_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
  user_id          INTEGER NULL REFERENCES users (id),
  installation_id  INTEGER NULL REFERENCES installations (id),
  context          TEXT    NOT NULL DEFAULT 'ui'
                     CHECK (context IN ('ui', 'qif_import', 'json_import', 'scheduler', 'system')),
  import_batch_id  INTEGER NULL REFERENCES import_batches (id) ON DELETE SET NULL,
  table_name       TEXT    NOT NULL,
  row_id           INTEGER NOT NULL,
  row_uuid         BLOB    NULL,
  action           TEXT    NOT NULL CHECK (action IN ('insert', 'update', 'delete')),
  old_values       TEXT    NULL CHECK (old_values IS NULL OR json_valid(old_values)),
  new_values       TEXT    NULL CHECK (new_values IS NULL OR json_valid(new_values))
) STRICT;
CREATE INDEX ix_audit_row ON audit_log (table_name, row_id);
CREATE INDEX ix_audit_time ON audit_log (occurred_at);
CREATE INDEX ix_audit_user ON audit_log (user_id, occurred_at);
CREATE INDEX ix_audit_import_batch ON audit_log (import_batch_id);

-- =============================================================================
-- Views
-- =============================================================================

-- Full colon-separated path for every account: "Auto:Fuel"
CREATE VIEW v_account_paths AS
WITH RECURSIVE p (id, path, depth) AS (
  SELECT id, name, 0
    FROM accounts WHERE parent_id IS NULL
  UNION ALL
  SELECT a.id, p.path || ':' || a.name, p.depth + 1
    FROM accounts a JOIN p ON a.parent_id = p.id
)
SELECT a.id, a.uuid, a.parent_id, a.kind, a.subtype, p.path, p.depth,
       a.is_system, a.is_archived, a.archived_at, a.sort_order
  FROM accounts a JOIN p ON p.id = a.id;

-- What the account/category pickers offer when entering new data.
CREATE VIEW v_selectable_accounts AS
SELECT * FROM v_account_paths WHERE is_archived = 0;

-- Current, cleared and reconciled balance of each account, in cents (debit-positive).
CREATE VIEW v_account_balances AS
SELECT a.id AS account_id,
       a.kind,
       COALESCE(SUM(s.amount), 0)                                              AS balance,
       COALESCE(SUM(CASE WHEN s.cleared <> 'uncleared' THEN s.amount END), 0) AS cleared_balance,
       COALESCE(SUM(CASE WHEN s.cleared = 'reconciled' THEN s.amount END), 0) AS reconciled_balance,
       MAX(s.txn_date)                                                         AS last_txn_date
  FROM accounts a
  LEFT JOIN splits s ON s.account_id = a.id
 GROUP BY a.id, a.kind;

-- Integrity check: should always return zero rows.
CREATE VIEW v_unbalanced_transactions AS
SELECT t.id AS transaction_id,
       t.txn_date,
       COALESCE(SUM(s.amount), 0) AS imbalance,
       COUNT(s.id)                AS split_count
  FROM transactions t
  LEFT JOIN splits s ON s.transaction_id = t.id
 GROUP BY t.id, t.txn_date
HAVING imbalance <> 0 OR split_count = 0;

-- =============================================================================
-- Seed data
-- (The local installations row and the first user are created by the app's
--  first-run setup, which asks for the installation name and primary/satellite.)
-- System accounts have fixed UUIDs so every installation agrees on them.
-- =============================================================================
INSERT INTO accounts (uuid, name, name_key, kind, subtype, is_system, notes) VALUES
  (X'5d0f6e2a8c4b4f1e9a3d7b6c1e2f0a01', 'Opening Balances', 'opening balances',
   'equity', 'equity', 1, 'Offsets each account''s starting balance.'),
  (X'5d0f6e2a8c4b4f1e9a3d7b6c1e2f0a02', 'Uncategorized', 'uncategorized',
   'expense', 'category', 1, 'Used for transactions entered or imported without a category.');

PRAGMA user_version = 1;
