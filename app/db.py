import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    med_key              TEXT PRIMARY KEY,
    state                TEXT NOT NULL DEFAULT 'IDLE',
    snooze_until         TEXT,
    last_nudged_at       TEXT,
    first_nudged_at      TEXT,
    next_followup_at     TEXT,
    followup_final_sent  INTEGER NOT NULL DEFAULT 0,
    pending_fill_date    TEXT,
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS calls (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    med_key            TEXT NOT NULL,
    provider_call_sid  TEXT,
    started_at         TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at           TEXT,
    outcome            TEXT,
    transcript         TEXT,
    summary            TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    med_key     TEXT,
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def init_db(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Adds columns introduced after a DB already existed. `CREATE TABLE IF NOT EXISTS`
    only covers fresh databases, so a deployed DB needs this to pick up new columns."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
    for column, ddl in (
        ("first_nudged_at", "ALTER TABLE threads ADD COLUMN first_nudged_at TEXT"),
        ("next_followup_at", "ALTER TABLE threads ADD COLUMN next_followup_at TEXT"),
        (
            "followup_final_sent",
            "ALTER TABLE threads ADD COLUMN followup_final_sent INTEGER NOT NULL DEFAULT 0",
        ),
        ("pending_fill_date", "ALTER TABLE threads ADD COLUMN pending_fill_date TEXT"),
    ):
        if column not in existing:
            conn.execute(ddl)
    conn.commit()


@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
