"""SQLite connection and schema. Only the last 4 digits of a phone number are ever stored."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone_last4   TEXT NOT NULL CHECK (length(phone_last4) = 4),
    code          TEXT NOT NULL UNIQUE,
    start         TEXT NOT NULL,
    "end"         TEXT NOT NULL,
    purpose       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'confirmed'
);
CREATE INDEX IF NOT EXISTS idx_bookings_start ON bookings(start);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    tier      TEXT NOT NULL,
    tool      TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    blocked   INTEGER NOT NULL,
    reason    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    metrics_json TEXT NOT NULL
);
"""


def default_db_path() -> Path:
    return Path(os.environ.get("VOICEGATE_DB_PATH", "voicegate.db"))


def connect(path: Path | str) -> sqlite3.Connection:
    """Autocommit connection; callers open explicit transactions where atomicity matters."""
    conn = sqlite3.connect(path, timeout=10, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db(path: Path | str) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
