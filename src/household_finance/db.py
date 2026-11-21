"""SQLite connection handling, schema creation, transactions and backups."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Iterator

SCHEMA_VERSION = 1


class SchemaVersionError(RuntimeError):
    pass


def connect(path: str | Path, create: bool = True) -> sqlite3.Connection:
    """Open the database, creating the schema if the file is new or empty.

    The connection is in autocommit mode; group changes with transaction().
    """
    path_str = str(path)
    if not create and path_str != ":memory:" and not Path(path_str).exists():
        raise FileNotFoundError(path_str)
    conn = sqlite3.connect(path_str, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if path_str != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        if conn.execute("SELECT count(*) FROM sqlite_schema").fetchone()[0]:
            conn.close()
            raise SchemaVersionError(f"{path_str} is not a Household Finance database")
        if not create:
            conn.close()
            raise SchemaVersionError(f"{path_str} has no schema")
        create_schema(conn)
    elif version != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(
            f"{path_str} has schema version {version}; this program supports "
            f"version {SCHEMA_VERSION}")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    sql = resources.files("household_finance").joinpath("schema.sql").read_text("utf-8")
    # executescript runs outside any open transaction; wrap it so a failure
    # leaves an empty database rather than half a schema.
    conn.executescript(f"BEGIN;\n{sql}\nCOMMIT;")


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK if the block raises."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def utc_now() -> str:
    """Timestamp in the format CURRENT_TIMESTAMP uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def backup(conn: sqlite3.Connection, dest_dir: str | Path, label: str) -> Path:
    """Copy the open database to dest_dir/<db name>-<UTC time>-<label>.sqlite
    using SQLite's online backup, which is safe while the app is running."""
    db_path = Path(conn.execute("PRAGMA database_list").fetchone()["file"] or "memory")
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / f"{db_path.stem}-{stamp}-{label}.sqlite"
    target = sqlite3.connect(dest)
    try:
        conn.backup(target)
    finally:
        target.close()
    return dest
