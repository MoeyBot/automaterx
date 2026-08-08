import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
    med_key         TEXT PRIMARY KEY,
    state           TEXT NOT NULL DEFAULT 'IDLE',
    snooze_until    TEXT,
    last_nudged_at  TEXT,
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
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


@contextmanager
def get_conn(db_path: str):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
