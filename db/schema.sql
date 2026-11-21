-- =============================================================================
-- Household Finance — MySQL schema, version 1
-- Requires MySQL 8.0.16+ (expression defaults, enforced CHECK constraints,
-- recursive CTEs in views). MySQL 8.4 LTS recommended.
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
-- * Money: DECIMAL(15,2) everywhere. Never FLOAT/DOUBLE.
-- * Times: DATETIME columns hold UTC. The app must run
--   SET time_zone = '+00:00' on every connection.
-- * IDs: integer PKs for internal joins; BINARY(16) uuid columns are the
--   stable identity used by satellite -> primary JSON export/import.
--   Python stores uuid.UUID(...).bytes; SQL displays with BIN_TO_UUID(uuid).
-- * Users / audit: created_by / updated_by and audit_log rows are written by
--   the application (it knows the current user); there are no audit triggers.
-- * Reconciliation is done only on the primary installation; editing
--   reconciled splits is blocked by the application.
-- =============================================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS household_finance
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE household_finance;

-- -----------------------------------------------------------------------------
-- Settings: key/value app preferences (also holds schema_version)
-- -----------------------------------------------------------------------------
CREATE TABLE settings (
  setting_key    VARCHAR(64)  NOT NULL,
  setting_value  TEXT         NULL,
  PRIMARY KEY (setting_key)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Installations: this copy of the program plus every copy it has exchanged
-- data with. Exactly one row is flagged is_this_installation; at most one
-- row is flagged is_primary. The app creates the local row on first run.
-- -----------------------------------------------------------------------------
CREATE TABLE installations (
  id                    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid                  BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  name                  VARCHAR(100) NOT NULL,           -- "Den PC", "Laptop"
  is_primary            BOOLEAN      NOT NULL DEFAULT FALSE,
  is_this_installation  BOOLEAN      NOT NULL DEFAULT FALSE,
  created_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  -- NULL when false, 1 when true; UNIQUE allows many NULLs but only one 1.
  primary_flag  TINYINT GENERATED ALWAYS AS (IF(is_primary, 1, NULL)) STORED,
  this_flag     TINYINT GENERATED ALWAYS AS (IF(is_this_installation, 1, NULL)) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY uq_installations_uuid (uuid),
  UNIQUE KEY uq_installations_primary (primary_flag),
  UNIQUE KEY uq_installations_this (this_flag)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Users: household members. No passwords. The default user is chosen by
-- matching the Windows account (and optionally computer name) running the
-- back end; the UI lets the person switch.
-- -----------------------------------------------------------------------------
CREATE TABLE users (
  id                INT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid              BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  display_name      VARCHAR(100) NOT NULL,
  windows_username  VARCHAR(100) NULL,
  computer_name     VARCHAR(100) NULL,   -- NULL = matches on any computer
  is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_users_uuid (uuid),
  UNIQUE KEY uq_users_display_name (display_name),
  KEY ix_users_windows (windows_username, computer_name)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Accounts: real accounts (asset / liability), categories (income / expense)
-- and equity. Hierarchical via parent_id; full path is "Auto:Fuel".
-- A child must have the same kind as its parent (enforced by the app).
-- -----------------------------------------------------------------------------
CREATE TABLE accounts (
  id                    INT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid                  BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  parent_id             INT UNSIGNED NULL,
  name                  VARCHAR(100) NOT NULL,
  kind                  ENUM('asset','liability','income','expense','equity') NOT NULL,
  subtype               ENUM('checking','savings','cash','credit_card',
                             'loan','mortgage','line_of_credit',
                             'investment','retirement','property',
                             'other_asset','other_liability',
                             'category','equity') NOT NULL,
  institution           VARCHAR(100) NULL,
  account_number_last4  CHAR(4)      NULL,
  tax_line              VARCHAR(100) NULL,   -- e.g. 'Schedule A: Charitable'
  is_hidden             BOOLEAN      NOT NULL DEFAULT FALSE,
  is_closed             BOOLEAN      NOT NULL DEFAULT FALSE,
  sort_order            INT          NOT NULL DEFAULT 0,
  notes                 TEXT         NULL,
  source_ref            VARCHAR(255) NULL,   -- e.g. original QIF account/category name
  created_by            INT UNSIGNED NULL,
  created_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by            INT UNSIGNED NULL,
  updated_at            DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  -- Lets UNIQUE(parent_key, name) also cover top-level (NULL parent) names.
  parent_key            INT UNSIGNED GENERATED ALWAYS AS (IFNULL(parent_id, 0)) STORED,
  PRIMARY KEY (id),
  UNIQUE KEY uq_accounts_uuid (uuid),
  UNIQUE KEY uq_accounts_sibling_name (parent_key, name),
  KEY ix_accounts_kind (kind),
  CONSTRAINT fk_accounts_parent     FOREIGN KEY (parent_id)  REFERENCES accounts (id) ON DELETE RESTRICT,
  CONSTRAINT fk_accounts_created_by FOREIGN KEY (created_by) REFERENCES users (id),
  CONSTRAINT fk_accounts_updated_by FOREIGN KEY (updated_by) REFERENCES users (id),
  CONSTRAINT ck_accounts_name_no_colon CHECK (name NOT LIKE '%:%'),
  CONSTRAINT ck_accounts_category_subtype
    CHECK ((kind IN ('income','expense')) = (subtype = 'category')),
  CONSTRAINT ck_accounts_equity_subtype
    CHECK ((kind = 'equity') = (subtype = 'equity'))
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Payees
-- -----------------------------------------------------------------------------
CREATE TABLE payees (
  id                  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid                BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  name                VARCHAR(150) NOT NULL,
  default_account_id  INT UNSIGNED NULL,     -- category auto-filled on entry
  is_hidden           BOOLEAN      NOT NULL DEFAULT FALSE,
  notes               TEXT         NULL,
  created_by          INT UNSIGNED NULL,
  created_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by          INT UNSIGNED NULL,
  updated_at          DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_payees_uuid (uuid),
  UNIQUE KEY uq_payees_name (name),
  CONSTRAINT fk_payees_default_account FOREIGN KEY (default_account_id) REFERENCES accounts (id) ON DELETE SET NULL,
  CONSTRAINT fk_payees_created_by      FOREIGN KEY (created_by) REFERENCES users (id),
  CONSTRAINT fk_payees_updated_by      FOREIGN KEY (updated_by) REFERENCES users (id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Tags (Quicken "classes" import as tags)
-- -----------------------------------------------------------------------------
CREATE TABLE tags (
  id           INT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid         BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  name         VARCHAR(100) NOT NULL,
  description  VARCHAR(255) NULL,
  is_hidden    BOOLEAN      NOT NULL DEFAULT FALSE,
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_tags_uuid (uuid),
  UNIQUE KEY uq_tags_name (name)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Import batches: one row per QIF import or satellite JSON import.
-- Rolling back a batch deletes the transactions it inserted; updates it made
-- to existing transactions are recoverable from audit_log.
-- -----------------------------------------------------------------------------
CREATE TABLE import_batches (
  id                      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  source_type             ENUM('qif','json') NOT NULL,
  source_installation_id  INT UNSIGNED NULL,   -- set for 'json'
  file_name               VARCHAR(255) NOT NULL,
  file_sha256             CHAR(64)     NULL,   -- warns when the same file is imported twice
  imported_by             INT UNSIGNED NULL,
  imported_at             DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  status                  ENUM('in_progress','completed','rolled_back') NOT NULL DEFAULT 'in_progress',
  transactions_inserted   INT UNSIGNED NOT NULL DEFAULT 0,
  transactions_updated    INT UNSIGNED NOT NULL DEFAULT 0,
  transactions_skipped    INT UNSIGNED NOT NULL DEFAULT 0,
  notes                   TEXT         NULL,
  PRIMARY KEY (id),
  KEY ix_import_batches_sha (file_sha256),
  CONSTRAINT fk_import_batches_installation FOREIGN KEY (source_installation_id) REFERENCES installations (id),
  CONSTRAINT fk_import_batches_user         FOREIGN KEY (imported_by) REFERENCES users (id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Import mappings: remembers "satellite account/payee/tag/user X = local Y"
-- so the mapping screen is shown only the first time.
-- local_id refers to the table named by entity_type (no FK: polymorphic).
-- -----------------------------------------------------------------------------
CREATE TABLE import_mappings (
  id                      INT UNSIGNED NOT NULL AUTO_INCREMENT,
  source_installation_id  INT UNSIGNED NOT NULL,
  entity_type             ENUM('account','payee','tag','user') NOT NULL,
  source_uuid             BINARY(16)   NOT NULL,
  local_id                INT UNSIGNED NOT NULL,
  created_at              DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_import_mappings (source_installation_id, entity_type, source_uuid),
  CONSTRAINT fk_import_mappings_installation FOREIGN KEY (source_installation_id) REFERENCES installations (id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Exports: history of JSON exports made from this installation
-- -----------------------------------------------------------------------------
CREATE TABLE exports (
  id                 INT UNSIGNED NOT NULL AUTO_INCREMENT,
  exported_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  exported_by        INT UNSIGNED NULL,
  file_name          VARCHAR(255) NOT NULL,
  transaction_count  INT UNSIGNED NOT NULL DEFAULT 0,
  filter_json        JSON         NULL,   -- the options used (date range, accounts, ...)
  PRIMARY KEY (id),
  CONSTRAINT fk_exports_user FOREIGN KEY (exported_by) REFERENCES users (id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Reconciliations: one per statement reconciled (primary installation only)
-- -----------------------------------------------------------------------------
CREATE TABLE reconciliations (
  id                        INT UNSIGNED  NOT NULL AUTO_INCREMENT,
  account_id                INT UNSIGNED  NOT NULL,
  statement_date            DATE          NOT NULL,
  statement_start_balance   DECIMAL(15,2) NOT NULL,
  statement_ending_balance  DECIMAL(15,2) NOT NULL,
  status                    ENUM('in_progress','completed') NOT NULL DEFAULT 'in_progress',
  started_by                INT UNSIGNED  NULL,
  started_at                DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_by              INT UNSIGNED  NULL,
  completed_at              DATETIME      NULL,
  PRIMARY KEY (id),
  KEY ix_reconciliations_account (account_id, statement_date),
  CONSTRAINT fk_reconciliations_account      FOREIGN KEY (account_id)   REFERENCES accounts (id) ON DELETE RESTRICT,
  CONSTRAINT fk_reconciliations_started_by   FOREIGN KEY (started_by)   REFERENCES users (id),
  CONSTRAINT fk_reconciliations_completed_by FOREIGN KEY (completed_by) REFERENCES users (id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Scheduled (recurring) transactions and their split templates
-- -----------------------------------------------------------------------------
CREATE TABLE scheduled_transactions (
  id               INT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid             BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  name             VARCHAR(100) NOT NULL,
  payee_id         INT UNSIGNED NULL,
  memo             VARCHAR(255) NOT NULL DEFAULT '',
  frequency        ENUM('once','daily','weekly','biweekly','semimonthly',
                        'monthly','quarterly','semiannually','annually') NOT NULL,
  interval_count   SMALLINT UNSIGNED NOT NULL DEFAULT 1,   -- every N periods
  next_due_date    DATE         NOT NULL,
  end_date         DATE         NULL,
  remaining_count  INT UNSIGNED NULL,                      -- NULL = no limit
  auto_enter       BOOLEAN      NOT NULL DEFAULT FALSE,
  days_in_advance  SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- remind/enter N days early
  is_active        BOOLEAN      NOT NULL DEFAULT TRUE,
  created_by       INT UNSIGNED NULL,
  created_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by       INT UNSIGNED NULL,
  updated_at       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_scheduled_transactions_uuid (uuid),
  KEY ix_scheduled_transactions_due (is_active, next_due_date),
  CONSTRAINT fk_sched_payee      FOREIGN KEY (payee_id)   REFERENCES payees (id) ON DELETE SET NULL,
  CONSTRAINT fk_sched_created_by FOREIGN KEY (created_by) REFERENCES users (id),
  CONSTRAINT fk_sched_updated_by FOREIGN KEY (updated_by) REFERENCES users (id),
  CONSTRAINT ck_sched_interval   CHECK (interval_count >= 1)
) ENGINE=InnoDB;

CREATE TABLE scheduled_splits (
  id                        INT UNSIGNED  NOT NULL AUTO_INCREMENT,
  scheduled_transaction_id  INT UNSIGNED  NOT NULL,
  account_id                INT UNSIGNED  NOT NULL,
  amount                    DECIMAL(15,2) NOT NULL,
  memo                      VARCHAR(255)  NOT NULL DEFAULT '',
  sort_order                SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  KEY ix_scheduled_splits_parent (scheduled_transaction_id),
  CONSTRAINT fk_sched_splits_parent  FOREIGN KEY (scheduled_transaction_id) REFERENCES scheduled_transactions (id) ON DELETE CASCADE,
  CONSTRAINT fk_sched_splits_account FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE RESTRICT
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Transactions: the header (date, payee, who/where entered)
-- -----------------------------------------------------------------------------
CREATE TABLE transactions (
  id                        BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid                      BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  txn_date                  DATE         NOT NULL,
  payee_id                  INT UNSIGNED NULL,
  check_number              VARCHAR(20)  NULL,       -- '1234', 'ATM', 'EFT', ...
  memo                      VARCHAR(255) NOT NULL DEFAULT '',
  origin_installation_id    INT UNSIGNED NULL,       -- where it was first entered
  import_batch_id           INT UNSIGNED NULL,       -- set if it arrived by import
  scheduled_transaction_id  INT UNSIGNED NULL,       -- set if generated from a schedule
  source_ref                VARCHAR(255) NULL,       -- e.g. QIF account + record position
  created_by                INT UNSIGNED NULL,
  created_at                DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_by                INT UNSIGNED NULL,
  -- The app must bump updated_at whenever any of the transaction's splits
  -- change: import conflict detection compares this value.
  updated_at                DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_transactions_uuid (uuid),
  KEY ix_transactions_date (txn_date),
  KEY ix_transactions_payee (payee_id, txn_date),
  KEY ix_transactions_import_batch (import_batch_id),
  KEY ix_transactions_origin (origin_installation_id),
  CONSTRAINT fk_txn_payee        FOREIGN KEY (payee_id)                 REFERENCES payees (id) ON DELETE SET NULL,
  CONSTRAINT fk_txn_origin       FOREIGN KEY (origin_installation_id)   REFERENCES installations (id),
  CONSTRAINT fk_txn_import_batch FOREIGN KEY (import_batch_id)          REFERENCES import_batches (id) ON DELETE SET NULL,
  CONSTRAINT fk_txn_scheduled    FOREIGN KEY (scheduled_transaction_id) REFERENCES scheduled_transactions (id) ON DELETE SET NULL,
  CONSTRAINT fk_txn_created_by   FOREIGN KEY (created_by)               REFERENCES users (id),
  CONSTRAINT fk_txn_updated_by   FOREIGN KEY (updated_by)               REFERENCES users (id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Splits: the lines of a transaction. SUM(amount) per transaction must be 0.
-- txn_date is a copy of transactions.txn_date (kept in sync by triggers
-- below) so account registers and reports are served from one index.
-- 'reconciled' splits may have reconciliation_id NULL (history imported from
-- Quicken has no reconciliation records).
-- -----------------------------------------------------------------------------
CREATE TABLE splits (
  id                 BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid               BINARY(16)      NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  transaction_id     BIGINT UNSIGNED NOT NULL,
  account_id         INT UNSIGNED    NOT NULL,
  txn_date           DATE            NOT NULL,
  amount             DECIMAL(15,2)   NOT NULL,
  memo               VARCHAR(255)    NOT NULL DEFAULT '',
  cleared            ENUM('uncleared','cleared','reconciled') NOT NULL DEFAULT 'uncleared',
  reconciliation_id  INT UNSIGNED    NULL,
  sort_order         SMALLINT UNSIGNED NOT NULL DEFAULT 0,
  PRIMARY KEY (id),
  UNIQUE KEY uq_splits_uuid (uuid),
  KEY ix_splits_transaction (transaction_id),
  -- Covering index for registers, running balances and per-account reports.
  KEY ix_splits_register (account_id, txn_date, transaction_id, amount, cleared),
  -- Covering index for date-range reports across all categories.
  KEY ix_splits_date (txn_date, account_id, amount),
  KEY ix_splits_reconciliation (reconciliation_id),
  CONSTRAINT fk_splits_transaction    FOREIGN KEY (transaction_id)    REFERENCES transactions (id) ON DELETE CASCADE,
  CONSTRAINT fk_splits_account        FOREIGN KEY (account_id)        REFERENCES accounts (id) ON DELETE RESTRICT,
  CONSTRAINT fk_splits_reconciliation FOREIGN KEY (reconciliation_id) REFERENCES reconciliations (id) ON DELETE SET NULL
) ENGINE=InnoDB;

CREATE TABLE split_tags (
  split_id  BIGINT UNSIGNED NOT NULL,
  tag_id    INT UNSIGNED    NOT NULL,
  PRIMARY KEY (split_id, tag_id),
  KEY ix_split_tags_tag (tag_id),
  CONSTRAINT fk_split_tags_split FOREIGN KEY (split_id) REFERENCES splits (id) ON DELETE CASCADE,
  CONSTRAINT fk_split_tags_tag   FOREIGN KEY (tag_id)   REFERENCES tags (id)   ON DELETE CASCADE
) ENGINE=InnoDB;

-- Keep splits.txn_date equal to its transaction's date.
CREATE TRIGGER trg_splits_set_date BEFORE INSERT ON splits FOR EACH ROW
  SET NEW.txn_date = (SELECT t.txn_date FROM transactions t WHERE t.id = NEW.transaction_id);

CREATE TRIGGER trg_transactions_sync_split_date AFTER UPDATE ON transactions FOR EACH ROW
  UPDATE splits SET txn_date = NEW.txn_date
   WHERE transaction_id = NEW.id AND txn_date <> NEW.txn_date;

-- -----------------------------------------------------------------------------
-- Budgets: one amount per category per month
-- -----------------------------------------------------------------------------
CREATE TABLE budgets (
  id          INT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid        BINARY(16)   NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  name        VARCHAR(100) NOT NULL,
  is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
  notes       TEXT         NULL,
  created_by  INT UNSIGNED NULL,
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_budgets_uuid (uuid),
  UNIQUE KEY uq_budgets_name (name),
  CONSTRAINT fk_budgets_created_by FOREIGN KEY (created_by) REFERENCES users (id)
) ENGINE=InnoDB;

CREATE TABLE budget_lines (
  id            INT UNSIGNED  NOT NULL AUTO_INCREMENT,
  budget_id     INT UNSIGNED  NOT NULL,
  account_id    INT UNSIGNED  NOT NULL,   -- an income or expense category
  period_start  DATE          NOT NULL,   -- first day of the month
  amount        DECIMAL(15,2) NOT NULL,   -- entered as a positive number
  PRIMARY KEY (id),
  UNIQUE KEY uq_budget_lines (budget_id, account_id, period_start),
  KEY ix_budget_lines_account (account_id),
  CONSTRAINT fk_budget_lines_budget  FOREIGN KEY (budget_id)  REFERENCES budgets (id)  ON DELETE CASCADE,
  CONSTRAINT fk_budget_lines_account FOREIGN KEY (account_id) REFERENCES accounts (id) ON DELETE CASCADE,
  CONSTRAINT ck_budget_lines_month   CHECK (DAYOFMONTH(period_start) = 1)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Attachments (optional): files live on disk; only metadata is stored here
-- -----------------------------------------------------------------------------
CREATE TABLE attachments (
  id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  uuid            BINARY(16)      NOT NULL DEFAULT (UUID_TO_BIN(UUID())),
  transaction_id  BIGINT UNSIGNED NOT NULL,
  file_path       VARCHAR(500)    NOT NULL,   -- relative to the app's attachment folder
  original_name   VARCHAR(255)    NOT NULL,
  mime_type       VARCHAR(100)    NULL,
  size_bytes      BIGINT UNSIGNED NULL,
  sha256          CHAR(64)        NULL,
  uploaded_by     INT UNSIGNED    NULL,
  uploaded_at     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uq_attachments_uuid (uuid),
  KEY ix_attachments_transaction (transaction_id),
  CONSTRAINT fk_attachments_transaction FOREIGN KEY (transaction_id) REFERENCES transactions (id) ON DELETE CASCADE,
  CONSTRAINT fk_attachments_user        FOREIGN KEY (uploaded_by)    REFERENCES users (id)
) ENGINE=InnoDB;

-- -----------------------------------------------------------------------------
-- Audit log: written by the application for every insert/update/delete of
-- user data. old_values / new_values hold the row (and, for transactions,
-- its splits) as JSON.
-- -----------------------------------------------------------------------------
CREATE TABLE audit_log (
  id               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  occurred_at      DATETIME(3)     NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  user_id          INT UNSIGNED    NULL,
  installation_id  INT UNSIGNED    NULL,
  context          ENUM('ui','qif_import','json_import','scheduler','system') NOT NULL DEFAULT 'ui',
  import_batch_id  INT UNSIGNED    NULL,
  table_name       VARCHAR(64)     NOT NULL,
  row_id           BIGINT UNSIGNED NOT NULL,
  row_uuid         BINARY(16)      NULL,
  action           ENUM('insert','update','delete') NOT NULL,
  old_values       JSON            NULL,
  new_values       JSON            NULL,
  PRIMARY KEY (id),
  KEY ix_audit_row (table_name, row_id),
  KEY ix_audit_time (occurred_at),
  KEY ix_audit_user (user_id, occurred_at),
  CONSTRAINT fk_audit_user         FOREIGN KEY (user_id)         REFERENCES users (id),
  CONSTRAINT fk_audit_installation FOREIGN KEY (installation_id) REFERENCES installations (id),
  CONSTRAINT fk_audit_import_batch FOREIGN KEY (import_batch_id) REFERENCES import_batches (id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- =============================================================================
-- Views
-- =============================================================================

-- Full colon-separated path for every account: "Auto:Fuel"
CREATE VIEW v_account_paths AS
WITH RECURSIVE p (id, path, depth) AS (
  SELECT id, CAST(name AS CHAR(1000)), 0
    FROM accounts WHERE parent_id IS NULL
  UNION ALL
  SELECT a.id, CONCAT(p.path, ':', a.name), p.depth + 1
    FROM accounts a JOIN p ON a.parent_id = p.id
)
SELECT a.id, a.uuid, a.parent_id, a.kind, a.subtype, p.path, p.depth,
       a.is_hidden, a.is_closed
  FROM accounts a JOIN p ON p.id = a.id;

-- Current, cleared and reconciled balance of each account (debit-positive).
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
-- =============================================================================
INSERT INTO settings (setting_key, setting_value) VALUES
  ('schema_version', '1');

INSERT INTO accounts (name, kind, subtype, notes) VALUES
  ('Opening Balances', 'equity',  'equity',
   'Offsets each account''s starting balance.'),
  ('Uncategorized',    'expense', 'category',
   'Used for transactions entered or imported without a category.');
