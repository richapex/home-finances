"""Write an ImportPlan to the database as one import batch, and roll back.

Everything happens inside one database transaction: if anything fails,
nothing is written. Accounts, payees and tags that already exist (same path
or name, compared with name_key) are reused; new ones are recorded with the
batch's id so a rollback can remove them again.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..db import transaction, utc_now
from ..money import format_money, from_cents, to_cents
from ..text import name_key
from .plan import ImportPlan, PlannedAccount, path_key


class ImportConflict(Exception):
    """The plan cannot be applied to this database as it stands."""


class DuplicateImport(Exception):
    """This file was already imported (and not rolled back)."""


class RollbackRefused(Exception):
    pass


@dataclass
class ImportResult:
    batch_id: int
    transactions_inserted: int = 0
    accounts_created: int = 0
    accounts_reused: int = 0
    payees_created: int = 0
    tags_created: int = 0
    archived_accounts_used: Counter[str] = field(default_factory=Counter)  # path -> splits


@dataclass
class RollbackResult:
    batch_id: int
    transactions_deleted: int = 0
    accounts_deleted: int = 0
    payees_deleted: int = 0
    tags_deleted: int = 0
    kept: list[str] = field(default_factory=list)  # created by the batch but now in use


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def new_uuid() -> bytes:
    return uuid.uuid4().bytes


# -- audit helpers -----------------------------------------------------------------

def _audit(conn: sqlite3.Connection, *, table: str, row_id: int, row_uuid: bytes | None,
           action: str, batch_id: int, user_id: int | None, installation_id: int | None,
           old: dict | None = None, new: dict | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_log (user_id, installation_id, context, import_batch_id,"
        " table_name, row_id, row_uuid, action, old_values, new_values)"
        " VALUES (?, ?, 'qif_import', ?, ?, ?, ?, ?, ?, ?)",
        (user_id, installation_id, batch_id, table, row_id, row_uuid, action,
         json.dumps(old) if old is not None else None,
         json.dumps(new) if new is not None else None))


def _row_snapshot(conn: sqlite3.Connection, table: str, row_id: int) -> dict:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return {k: (str(uuid.UUID(bytes=row[k])) if k == "uuid" else row[k]) for k in row.keys()}


def account_path_map(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT id, path FROM v_account_paths")}


def transaction_snapshot(conn: sqlite3.Connection, txn_id: int,
                         paths: dict[int, str]) -> dict:
    """A transaction and its splits as JSON-ready data, money as strings.
    paths is account_path_map(); pass it in so the recursive view is not
    re-evaluated for every transaction."""
    t = conn.execute(
        "SELECT t.*, p.name AS payee_name FROM transactions t"
        " LEFT JOIN payees p ON p.id = t.payee_id WHERE t.id = ?", (txn_id,)).fetchone()
    splits = conn.execute(
        "SELECT s.* FROM splits s WHERE s.transaction_id = ? ORDER BY s.sort_order, s.id",
        (txn_id,)).fetchall()
    out = []
    for s in splits:
        tags = [r[0] for r in conn.execute(
            "SELECT tg.name FROM split_tags st JOIN tags tg ON tg.id = st.tag_id"
            " WHERE st.split_id = ? ORDER BY tg.name_key", (s["id"],))]
        out.append({
            "id": s["id"], "uuid": str(uuid.UUID(bytes=s["uuid"])),
            "account_id": s["account_id"], "account": paths[s["account_id"]],
            "amount": format_money(from_cents(s["amount"])), "cleared": s["cleared"],
            "reconciliation_id": s["reconciliation_id"], "memo": s["memo"], "tags": tags,
        })
    return {
        "id": t["id"], "uuid": str(uuid.UUID(bytes=t["uuid"])), "date": t["txn_date"],
        "payee_id": t["payee_id"], "payee": t["payee_name"],
        "check_number": t["check_number"], "memo": t["memo"],
        "origin_installation_id": t["origin_installation_id"],
        "import_batch_id": t["import_batch_id"], "source_ref": t["source_ref"],
        "splits": out,
    }


# -- import ------------------------------------------------------------------------

def import_plan(conn: sqlite3.Connection, plan: ImportPlan, *, file_name: str,
                file_sha256: str | None = None, user_id: int | None = None,
                installation_id: int | None = None,
                allow_duplicate: bool = False) -> ImportResult:
    if file_sha256 and not allow_duplicate:
        prior = conn.execute(
            "SELECT id, imported_at FROM import_batches"
            " WHERE file_sha256 = ? AND status = 'completed'", (file_sha256,)).fetchone()
        if prior:
            raise DuplicateImport(
                f"this file was already imported as batch {prior['id']} on "
                f"{prior['imported_at']} UTC; roll that batch back first or allow duplicates")
    with transaction(conn):
        return _Writer(conn, plan, user_id, installation_id).run(file_name, file_sha256)


class _Writer:
    def __init__(self, conn: sqlite3.Connection, plan: ImportPlan,
                 user_id: int | None, installation_id: int | None) -> None:
        self.conn = conn
        self.plan = plan
        self.user_id = user_id
        self.installation_id = installation_id
        self.account_ids: dict[str, int] = {}  # path_key -> id
        self.archived: set[str] = set()  # path_keys of archived accounts reused
        self.payee_ids: dict[str, int] = {}  # name_key -> id
        self.tag_ids: dict[str, int] = {}  # name_key -> id

    def audit(self, table: str, row_id: int, row_uuid: bytes, new: dict) -> None:
        _audit(self.conn, table=table, row_id=row_id, row_uuid=row_uuid, action="insert",
               batch_id=self.batch_id, user_id=self.user_id,
               installation_id=self.installation_id, new=new)

    def run(self, file_name: str, sha: str | None) -> ImportResult:
        cur = self.conn.execute(
            "INSERT INTO import_batches (source_type, file_name, file_sha256, imported_by)"
            " VALUES ('qif', ?, ?, ?)", (file_name, sha, self.user_id))
        self.batch_id = cur.lastrowid
        self.result = ImportResult(self.batch_id)

        self.write_accounts()
        self.load_names("payees", self.plan.payees, self.payee_ids)
        self.load_names("tags", list(self.plan.tags), self.tag_ids)
        self.write_transactions()

        unbalanced = self.conn.execute(
            "SELECT count(*) FROM v_unbalanced_transactions u"
            " JOIN transactions t ON t.id = u.transaction_id WHERE t.import_batch_id = ?",
            (self.batch_id,)).fetchone()[0]
        if unbalanced:  # cannot happen unless the plan or writer is wrong
            raise AssertionError(f"{unbalanced} unbalanced transactions; import cancelled")

        notes = [f"{len(self.plan.transfer_issues)} transfer issue(s), "
                 f"{len(self.plan.warnings)} warning(s) in the import plan"]
        if self.result.archived_accounts_used:
            notes.append("posted to archived accounts: " + ", ".join(
                f"{p} ({n})" for p, n in sorted(self.result.archived_accounts_used.items())))
        self.conn.execute(
            "UPDATE import_batches SET status = 'completed', transactions_inserted = ?,"
            " notes = ? WHERE id = ?",
            (self.result.transactions_inserted, "\n".join(notes), self.batch_id))
        return self.result

    def write_accounts(self) -> None:
        existing = {}
        for row in self.conn.execute(
                "SELECT ap.id, ap.path, ap.kind, ap.is_archived FROM v_account_paths ap"):
            existing[path_key(row["path"])] = row
        # Parents before children.
        ordered = sorted(self.plan.accounts.values(), key=lambda a: a.path.count(":"))
        for acct in ordered:
            key = path_key(acct.path)
            row = existing.get(key)
            if row is not None:
                if (row["kind"] in ("income", "expense")) != acct.is_category:
                    raise ImportConflict(
                        f"{acct.path!r} exists in the database as {row['kind']} but the "
                        f"QIF file needs it as {acct.kind}; rename one of them first")
                self.account_ids[key] = row["id"]
                if row["is_archived"]:
                    self.archived.add(key)
                self.result.accounts_reused += 1
                continue
            self.account_ids[key] = self.insert_account(acct)
            self.result.accounts_created += 1

    def insert_account(self, acct: PlannedAccount) -> int:
        parent_id = self.account_ids[path_key(acct.parent_path)] if acct.parent_path else None
        acct_uuid = new_uuid()
        now = utc_now()
        cur = self.conn.execute(
            "INSERT INTO accounts (uuid, parent_id, name, name_key, kind, subtype, tax_line,"
            " notes, source_ref, import_batch_id, created_by, created_at, updated_by,"
            " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (acct_uuid, parent_id, acct.name, name_key(acct.name), acct.kind, acct.subtype,
             acct.tax_line, acct.description or None, acct.source_ref, self.batch_id,
             self.user_id, now, self.user_id, now))
        self.audit("accounts", cur.lastrowid, acct_uuid,
                   _row_snapshot(self.conn, "accounts", cur.lastrowid) | {"path": acct.path})
        return cur.lastrowid

    def load_names(self, table: str, names: list[str], ids: dict[str, int]) -> None:
        for row in self.conn.execute(f"SELECT id, name_key FROM {table}"):
            ids[row["name_key"]] = row["id"]
        for name in names:
            key = name_key(name)
            if key in ids:
                continue
            row_uuid = new_uuid()
            if table == "tags":
                cur = self.conn.execute(
                    "INSERT INTO tags (uuid, name, name_key, description, import_batch_id)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (row_uuid, name, key, self.plan.tags.get(name) or None, self.batch_id))
                self.result.tags_created += 1
            else:
                now = utc_now()
                cur = self.conn.execute(
                    "INSERT INTO payees (uuid, name, name_key, import_batch_id, created_by,"
                    " created_at, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (row_uuid, name, key, self.batch_id, self.user_id, now, self.user_id, now))
                self.result.payees_created += 1
            ids[key] = cur.lastrowid
            self.audit(table, cur.lastrowid, row_uuid,
                       _row_snapshot(self.conn, table, cur.lastrowid))

    def write_transactions(self) -> None:
        paths = account_path_map(self.conn)
        for pt in self.plan.transactions:
            txn_uuid = new_uuid()
            txn_date = pt.date.isoformat()
            now = utc_now()
            cur = self.conn.execute(
                "INSERT INTO transactions (uuid, txn_date, payee_id, check_number, memo,"
                " origin_installation_id, import_batch_id, source_ref, created_by,"
                " created_at, updated_by, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (txn_uuid, txn_date,
                 self.payee_ids[name_key(pt.payee)] if pt.payee else None,
                 pt.check_number, pt.memo, self.installation_id, self.batch_id,
                 pt.source_ref, self.user_id, now, self.user_id, now))
            txn_id = cur.lastrowid
            if sum(to_cents(s.amount) for s in pt.splits) != 0:
                raise AssertionError(f"unbalanced transaction from {pt.source_ref}")
            for order, s in enumerate(pt.splits):
                key = path_key(s.account)
                split_cur = self.conn.execute(
                    "INSERT INTO splits (uuid, transaction_id, account_id, txn_date, amount,"
                    " memo, cleared, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (new_uuid(), txn_id, self.account_ids[key], txn_date,
                     to_cents(s.amount), s.memo, s.cleared, order))
                for tag in s.tags:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO split_tags (split_id, tag_id) VALUES (?, ?)",
                        (split_cur.lastrowid, self.tag_ids[name_key(tag)]))
                if key in self.archived:
                    self.result.archived_accounts_used[s.account] += 1
            self.audit("transactions", txn_id, txn_uuid,
                       transaction_snapshot(self.conn, txn_id, paths))
            self.result.transactions_inserted += 1


# -- batches and rollback -----------------------------------------------------------

def list_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT b.*, u.display_name AS imported_by_name FROM import_batches b"
        " LEFT JOIN users u ON u.id = b.imported_by ORDER BY b.id").fetchall()


def rollback_batch(conn: sqlite3.Connection, batch_id: int, *, user_id: int | None = None,
                   installation_id: int | None = None) -> RollbackResult:
    """Delete the transactions a QIF batch inserted, then the accounts, payees
    and tags it created that nothing else uses."""
    with transaction(conn):
        batch = conn.execute("SELECT * FROM import_batches WHERE id = ?",
                             (batch_id,)).fetchone()
        if batch is None:
            raise RollbackRefused(f"there is no import batch {batch_id}")
        if batch["source_type"] != "qif":
            raise RollbackRefused(f"batch {batch_id} is not a QIF import")
        if batch["status"] != "completed":
            raise RollbackRefused(f"batch {batch_id} is {batch['status']}")
        reconciled = conn.execute(
            "SELECT count(*) FROM splits s JOIN transactions t ON t.id = s.transaction_id"
            " WHERE t.import_batch_id = ? AND s.reconciliation_id IS NOT NULL",
            (batch_id,)).fetchone()[0]
        if reconciled:
            raise RollbackRefused(
                f"{reconciled} split(s) from batch {batch_id} are part of a reconciliation "
                "done after the import; undo those reconciliations first")

        result = RollbackResult(batch_id)

        def audit_delete(table: str, row_id: int, old: dict) -> None:
            _audit(conn, table=table, row_id=row_id,
                   row_uuid=uuid.UUID(old["uuid"]).bytes, action="delete",
                   batch_id=batch_id, user_id=user_id, installation_id=installation_id,
                   old=old)

        txn_ids = [r[0] for r in conn.execute(
            "SELECT id FROM transactions WHERE import_batch_id = ? ORDER BY id", (batch_id,))]
        paths = account_path_map(conn)
        for txn_id in txn_ids:
            audit_delete("transactions", txn_id, transaction_snapshot(conn, txn_id, paths))
        conn.execute("DELETE FROM transactions WHERE import_batch_id = ?", (batch_id,))
        result.transactions_deleted = len(txn_ids)

        unused = {
            "tags": "NOT EXISTS (SELECT 1 FROM split_tags st WHERE st.tag_id = x.id)",
            "payees": "NOT EXISTS (SELECT 1 FROM transactions t WHERE t.payee_id = x.id)"
                      " AND NOT EXISTS (SELECT 1 FROM scheduled_transactions s"
                      " WHERE s.payee_id = x.id)",
        }
        for table, condition in unused.items():
            for row in conn.execute(f"SELECT id, name, ({condition}) AS is_unused"
                                    f" FROM {table} x WHERE import_batch_id = ?",
                                    (batch_id,)).fetchall():
                if not row["is_unused"]:
                    result.kept.append(f"{table[:-1]} {row['name']!r}")
                    continue
                audit_delete(table, row["id"], _row_snapshot(conn, table, row["id"]))
                conn.execute(f"DELETE FROM {table} WHERE id = ?", (row["id"],))
                if table == "tags":
                    result.tags_deleted += 1
                else:
                    result.payees_deleted += 1

        # Accounts deepest first, so a parent is free once its children are gone.
        accounts = conn.execute(
            "SELECT a.id, ap.path, ap.depth FROM accounts a JOIN v_account_paths ap"
            " ON ap.id = a.id WHERE a.import_batch_id = ? ORDER BY ap.depth DESC",
            (batch_id,)).fetchall()
        for row in accounts:
            in_use = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM splits WHERE account_id = :id)"
                " OR EXISTS (SELECT 1 FROM accounts WHERE parent_id = :id)"
                " OR EXISTS (SELECT 1 FROM scheduled_splits WHERE account_id = :id)"
                " OR EXISTS (SELECT 1 FROM budget_lines WHERE account_id = :id)"
                " OR EXISTS (SELECT 1 FROM reconciliations WHERE account_id = :id)"
                " OR EXISTS (SELECT 1 FROM payees WHERE default_account_id = :id)",
                {"id": row["id"]}).fetchone()[0]
            if in_use:
                result.kept.append(f"account {row['path']!r}")
                continue
            audit_delete("accounts", row["id"],
                         _row_snapshot(conn, "accounts", row["id"]) | {"path": row["path"]})
            conn.execute("DELETE FROM accounts WHERE id = ?", (row["id"],))
            result.accounts_deleted += 1

        conn.execute(
            "UPDATE import_batches SET status = 'rolled_back', rolled_back_by = ?,"
            " rolled_back_at = ? WHERE id = ?", (user_id, utc_now(), batch_id))
        return result
