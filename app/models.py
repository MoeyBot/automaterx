import re
from datetime import date, datetime, timedelta

from pydantic import BaseModel, field_validator


def slugify(med: str, dose: str) -> str:
    raw = f"{med}_{dose}".lower()
    return re.sub(r"[^a-z0-9]+", "_", raw).strip("_")


class Medication(BaseModel):
    med: str
    dose: str
    qty: int
    days_supply: int
    last_filled: date
    prescriber: str
    prescriber_phone: str
    phone_tree_hint: str = ""
    lead_days: int = 7
    status: str = "active"
    notes: str = ""

    @field_validator("status")
    @classmethod
    def _status_ok(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"active", "paused"}:
            raise ValueError(f"status must be 'active' or 'paused', got {v!r}")
        return v

    @property
    def med_key(self) -> str:
        return slugify(self.med, self.dose)

    @property
    def runs_out(self) -> date:
        return self.last_filled + timedelta(days=self.days_supply)

    @property
    def nudge_date(self) -> date:
        return self.runs_out - timedelta(days=self.lead_days)

    def is_due(self, today: date) -> bool:
        return self.status == "active" and today >= self.nudge_date


class Thread(BaseModel):
    med_key: str
    state: str = "IDLE"
    snooze_until: date | None = None
    last_nudged_at: datetime | None = None


def get_thread(conn, med_key: str) -> Thread | None:
    row = conn.execute("SELECT * FROM threads WHERE med_key = ?", (med_key,)).fetchone()
    if row is None:
        return None
    return Thread(
        med_key=row["med_key"],
        state=row["state"],
        snooze_until=date.fromisoformat(row["snooze_until"]) if row["snooze_until"] else None,
        last_nudged_at=datetime.fromisoformat(row["last_nudged_at"]) if row["last_nudged_at"] else None,
    )


def upsert_thread_state(conn, med_key: str, state: str) -> None:
    conn.execute(
        """
        INSERT INTO threads (med_key, state, updated_at) VALUES (?, ?, datetime('now'))
        ON CONFLICT(med_key) DO UPDATE SET state = excluded.state, updated_at = datetime('now')
        """,
        (med_key, state),
    )


def mark_nudged(conn, med_key: str) -> None:
    conn.execute(
        """
        INSERT INTO threads (med_key, state, last_nudged_at, updated_at)
        VALUES (?, 'AWAITING_REPLY', datetime('now'), datetime('now'))
        ON CONFLICT(med_key) DO UPDATE SET
            state = 'AWAITING_REPLY',
            last_nudged_at = datetime('now'),
            updated_at = datetime('now')
        """,
        (med_key,),
    )


def set_snooze(conn, med_key: str, until: date) -> None:
    conn.execute(
        """
        INSERT INTO threads (med_key, state, snooze_until, updated_at)
        VALUES (?, 'SNOOZED', ?, datetime('now'))
        ON CONFLICT(med_key) DO UPDATE SET
            state = 'SNOOZED',
            snooze_until = excluded.snooze_until,
            updated_at = datetime('now')
        """,
        (med_key, until.isoformat()),
    )


def log_message(conn, med_key: str | None, direction: str, body: str) -> None:
    conn.execute(
        "INSERT INTO messages (med_key, direction, body) VALUES (?, ?, ?)",
        (med_key, direction, body),
    )


def get_active_thread(conn) -> Thread | None:
    """The thread an incoming reply with no other context should be attributed to.

    Prefers a thread that's actively awaiting a decision; falls back to whichever
    thread was touched most recently, so a bare question ("what's the dose again?")
    still resolves to the medication you were just talking about.
    """
    row = conn.execute(
        "SELECT * FROM threads WHERE state = 'AWAITING_REPLY' ORDER BY last_nudged_at DESC LIMIT 1"
    ).fetchone()
    if row is None:
        row = conn.execute("SELECT * FROM threads ORDER BY updated_at DESC LIMIT 1").fetchone()
    if row is None:
        return None
    return Thread(
        med_key=row["med_key"],
        state=row["state"],
        snooze_until=date.fromisoformat(row["snooze_until"]) if row["snooze_until"] else None,
        last_nudged_at=datetime.fromisoformat(row["last_nudged_at"]) if row["last_nudged_at"] else None,
    )


def recent_messages(conn, med_key: str, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT direction, body, created_at FROM messages WHERE med_key = ? ORDER BY id DESC LIMIT ?",
        (med_key, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]
