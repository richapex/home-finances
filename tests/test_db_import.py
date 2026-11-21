import json
import sqlite3
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from household_finance import db
from household_finance.money import from_cents, to_cents
from household_finance.qif import build_plan, read_qif_file
from household_finance.qif.__main__ import main
from household_finance.qif.report import account_balances
from household_finance.qif.writer import (
    DuplicateImport,
    ImportConflict,
    RollbackRefused,
    file_sha256,
    import_plan,
    rollback_batch,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample.qif"


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture(scope="module")
def plan():
    return build_plan(read_qif_file(FIXTURE))


def balances_by_path(conn):
    return {r["path"]: (r["balance"], r["cleared_balance"], r["reconciled_balance"])
            for r in conn.execute(
                "SELECT ap.path, b.* FROM v_account_balances b"
                " JOIN v_account_paths ap ON ap.id = b.account_id")}


def count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


# -- schema -------------------------------------------------------------------------

def test_new_database_has_schema_and_seed(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    names = {r["path"]: r["is_system"] for r in conn.execute("SELECT * FROM v_account_paths")}
    assert names == {"Opening Balances": 1, "Uncategorized": 1}


def test_reopen_and_refuse_foreign_files(tmp_path):
    path = tmp_path / "books.sqlite"
    db.connect(path).close()
    db.connect(path, create=False).close()  # reopens without recreating
    other = tmp_path / "other.sqlite"
    c = sqlite3.connect(other)
    c.execute("CREATE TABLE x (y)")
    c.close()
    with pytest.raises(db.SchemaVersionError):
        db.connect(other)
    with pytest.raises(FileNotFoundError):
        db.connect(tmp_path / "missing.sqlite", create=False)


def test_schema_constraints(conn):
    u = lambda: uuid.uuid4().bytes  # noqa: E731
    with pytest.raises(sqlite3.IntegrityError):  # ':' not allowed in names
        conn.execute("INSERT INTO accounts (uuid, name, name_key, kind, subtype)"
                     " VALUES (?, 'A:B', 'a:b', 'asset', 'checking')", (u(),))
    with pytest.raises(sqlite3.IntegrityError):  # category subtype only for categories
        conn.execute("INSERT INTO accounts (uuid, name, name_key, kind, subtype)"
                     " VALUES (?, 'X', 'x', 'asset', 'category')", (u(),))
    with pytest.raises(sqlite3.IntegrityError):  # same name_key at top level
        conn.execute("INSERT INTO accounts (uuid, name, name_key, kind, subtype)"
                     " VALUES (?, 'UNCATEGORIZED', 'uncategorized', 'expense', 'category')",
                     (u(),))
    conn.execute("INSERT INTO transactions (id, uuid, txn_date) VALUES (1, ?, '2026-01-05')",
                 (u(),))
    with pytest.raises(sqlite3.IntegrityError):  # STRICT: money must be an integer
        conn.execute("INSERT INTO splits (uuid, transaction_id, account_id, txn_date, amount)"
                     " VALUES (?, 1, 1, '2026-01-05', '12.50')", (u(),))
    with pytest.raises(sqlite3.IntegrityError):  # split date must match transaction
        conn.execute("INSERT INTO splits (uuid, transaction_id, account_id, txn_date, amount)"
                     " VALUES (?, 1, 1, '2026-01-06', 100)", (u(),))
    with pytest.raises(sqlite3.IntegrityError):  # invalid date
        conn.execute("INSERT INTO transactions (uuid, txn_date) VALUES (?, '2026-02-30')",
                     (u(),))
    conn.execute("INSERT INTO splits (uuid, transaction_id, account_id, txn_date, amount)"
                 " VALUES (?, 1, 1, '2026-01-05', 100)", (u(),))
    conn.execute("UPDATE transactions SET txn_date = '2026-02-01' WHERE id = 1")
    assert conn.execute("SELECT txn_date FROM splits").fetchone()[0] == "2026-02-01"


def test_money_conversion():
    assert to_cents(Decimal("-120.00")) == -12000
    assert from_cents(-12000) == Decimal("-120.00")
    with pytest.raises(ValueError):
        to_cents(Decimal("0.001"))


# -- import -------------------------------------------------------------------------

def test_import_matches_plan(conn, plan):
    result = import_plan(conn, plan, file_name="sample.qif", file_sha256="abc")
    assert result.transactions_inserted == len(plan.transactions) == 10
    assert result.accounts_reused == 2  # the two system accounts
    assert result.accounts_created == len(plan.accounts) - 2
    assert count(conn, "v_unbalanced_transactions") == 0

    expected = {path: tuple(to_cents(v) for v in vals)
                for path, vals in account_balances(plan).items()}
    assert balances_by_path(conn) == expected

    batch = conn.execute("SELECT * FROM import_batches").fetchone()
    assert (batch["status"], batch["transactions_inserted"]) == ("completed", 10)
    assert conn.execute("SELECT count(*) FROM transactions WHERE import_batch_id = ?",
                        (result.batch_id,)).fetchone()[0] == 10

    # Tags and parent/child categories survive the round trip.
    tagged = conn.execute(
        "SELECT ap.path, tg.name FROM split_tags st JOIN splits s ON s.id = st.split_id"
        " JOIN tags tg ON tg.id = st.tag_id JOIN v_account_paths ap ON ap.id = s.account_id"
    ).fetchall()
    assert [tuple(r) for r in tagged] == [("Groceries", "Business")]
    fuel = conn.execute("SELECT * FROM v_account_paths WHERE path = 'Auto:Fuel'").fetchone()
    assert fuel["depth"] == 1 and fuel["kind"] == "expense"

    # One audit row per created account, payee, tag and transaction.
    audit = conn.execute("SELECT table_name, count(*) FROM audit_log GROUP BY table_name")
    assert dict(audit.fetchall()) == {
        "accounts": result.accounts_created, "payees": result.payees_created,
        "tags": 1, "transactions": 10}
    row = conn.execute("SELECT new_values FROM audit_log WHERE table_name = 'transactions'"
                       " AND new_values LIKE '%QIF Checking#4%'").fetchone()
    snap = json.loads(row[0])
    assert {s["account"]: s["amount"] for s in snap["splits"]} == {
        "Checking": "-300.00", "Auto:Fuel": "100.00", "Savings": "150.00", "Visa": "50.00"}


def test_duplicate_file_is_refused(conn, plan):
    import_plan(conn, plan, file_name="sample.qif", file_sha256="abc")
    with pytest.raises(DuplicateImport):
        import_plan(conn, plan, file_name="sample.qif", file_sha256="abc")
    result = import_plan(conn, plan, file_name="sample.qif", file_sha256="abc",
                         allow_duplicate=True)
    assert result.accounts_created == 0  # everything reused the second time
    assert count(conn, "transactions") == 20


def test_kind_conflict_writes_nothing(conn, plan):
    conn.execute("INSERT INTO accounts (uuid, name, name_key, kind, subtype)"
                 " VALUES (?, 'Groceries', 'groceries', 'asset', 'cash')",
                 (uuid.uuid4().bytes,))
    with pytest.raises(ImportConflict):
        import_plan(conn, plan, file_name="sample.qif")
    assert count(conn, "import_batches") == 0
    assert count(conn, "accounts") == 3


def test_archived_account_is_reported(conn, plan):
    conn.execute("INSERT INTO accounts (uuid, name, name_key, kind, subtype, is_archived,"
                 " archived_at) VALUES (?, 'Groceries', 'groceries', 'expense', 'category',"
                 " 1, CURRENT_TIMESTAMP)", (uuid.uuid4().bytes,))
    result = import_plan(conn, plan, file_name="sample.qif")
    assert result.archived_accounts_used == {"Groceries": 1}
    notes = conn.execute("SELECT notes FROM import_batches").fetchone()[0]
    assert "Groceries (1)" in notes


# -- rollback -----------------------------------------------------------------------

def test_rollback_removes_everything(conn, plan):
    result = import_plan(conn, plan, file_name="sample.qif", file_sha256="abc")
    rb = rollback_batch(conn, result.batch_id)
    assert rb.transactions_deleted == 10
    assert rb.accounts_deleted == result.accounts_created
    assert rb.payees_deleted == result.payees_created and rb.tags_deleted == 1
    assert rb.kept == []
    for table in ("transactions", "splits", "split_tags", "payees", "tags"):
        assert count(conn, table) == 0, table
    assert count(conn, "accounts") == 2  # system accounts remain
    assert conn.execute("SELECT status FROM import_batches").fetchone()[0] == "rolled_back"
    deletes = conn.execute("SELECT count(*) FROM audit_log WHERE action = 'delete'")
    assert deletes.fetchone()[0] == 10 + rb.accounts_deleted + rb.payees_deleted + 1

    with pytest.raises(RollbackRefused):
        rollback_batch(conn, result.batch_id)
    # A rolled-back file may be imported again.
    import_plan(conn, plan, file_name="sample.qif", file_sha256="abc")


def test_rollback_keeps_accounts_still_in_use(conn, plan):
    result = import_plan(conn, plan, file_name="sample.qif")
    checking = conn.execute("SELECT id FROM accounts WHERE name = 'Checking'").fetchone()[0]
    conn.execute("INSERT INTO transactions (id, uuid, txn_date) VALUES (999, ?, '2026-03-01')",
                 (uuid.uuid4().bytes,))
    for account_id, amount in ((checking, -500), (2, 500)):
        conn.execute("INSERT INTO splits (uuid, transaction_id, account_id, txn_date, amount)"
                     " VALUES (?, 999, ?, '2026-03-01', ?)",
                     (uuid.uuid4().bytes, account_id, amount))
    rb = rollback_batch(conn, result.batch_id)
    assert rb.kept == ["account 'Checking'"]
    assert count(conn, "transactions") == 1


def test_rollback_refused_after_reconciliation(conn, plan):
    result = import_plan(conn, plan, file_name="sample.qif")
    conn.execute("INSERT INTO reconciliations (id, account_id, statement_date,"
                 " statement_start_balance, statement_ending_balance)"
                 " SELECT 1, id, '2026-01-31', 0, 0 FROM accounts WHERE name = 'Checking'")
    conn.execute("UPDATE splits SET reconciliation_id = 1 WHERE id = (SELECT min(id) FROM splits)")
    with pytest.raises(RollbackRefused):
        rollback_batch(conn, result.batch_id)
    assert count(conn, "transactions") == 10


# -- command line -------------------------------------------------------------------

def test_cli_import_batches_rollback(tmp_path, capsys):
    qif = tmp_path / "export.qif"
    qif.write_bytes(FIXTURE.read_bytes())
    books = tmp_path / "books.sqlite"

    assert main(["import", str(qif), "--db", str(books)]) == 0
    out = capsys.readouterr().out
    assert "IMPORTED" in out and "transactions: 10" in out
    assert not (tmp_path / "backups").exists()  # empty database: nothing to back up

    assert main(["import", str(qif), "--db", str(books)]) == 1  # same file again
    assert "already imported" in capsys.readouterr().err

    assert main(["batches", "--db", str(books)]) == 0
    assert "completed" in capsys.readouterr().out

    assert main(["rollback", "1", "--db", str(books)]) == 0
    assert "10 transactions" in capsys.readouterr().out
    backups = list((tmp_path / "backups").glob("books-*-before-rollback-1.sqlite"))
    assert len(backups) == 1
    restored = db.connect(backups[0], create=False)
    assert count(restored, "transactions") == 10
    restored.close()

    c = db.connect(books, create=False)
    assert count(c, "transactions") == 0
    c.close()
    assert file_sha256(qif) == file_sha256(FIXTURE)
