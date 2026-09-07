from datetime import datetime, timedelta

import app.followup as followup_mod
from app.db import get_conn
from app.models import mark_nudged


def _seed_awaiting_reply(
    settings, med_key: str, next_followup_at: datetime, first_nudged_at: datetime | None = None
):
    with get_conn(str(settings.db_file)) as conn:
        mark_nudged(conn, med_key, next_followup_at)
        if first_nudged_at is not None:
            conn.execute(
                "UPDATE threads SET first_nudged_at = ? WHERE med_key = ?",
                (first_nudged_at.isoformat(sep=" "), med_key),
            )


def test_followup_sent_when_due(settings, one_med, monkeypatch):
    now = datetime(2026, 9, 6, 14, 0, 0)  # 9am local (America/Chicago, UTC-5)
    _seed_awaiting_reply(
        settings, one_med.med_key, next_followup_at=now - timedelta(hours=1), first_nudged_at=now
    )

    sent = []
    monkeypatch.setattr(followup_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(followup_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(followup_mod, "now_utc", lambda: now)

    followed_up = followup_mod.run_followup_check(settings)

    assert followed_up == [one_med.med_key]
    assert len(sent) == 1
    assert one_med.med in sent[0]

    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, next_followup_at FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "AWAITING_REPLY"
        assert datetime.fromisoformat(row["next_followup_at"]) > now


def test_followup_not_yet_due_is_skipped(settings, one_med, monkeypatch):
    now = datetime(2026, 9, 6, 14, 0, 0)
    _seed_awaiting_reply(
        settings, one_med.med_key, next_followup_at=now + timedelta(hours=2), first_nudged_at=now
    )

    sent = []
    monkeypatch.setattr(followup_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(followup_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(followup_mod, "now_utc", lambda: now)

    followed_up = followup_mod.run_followup_check(settings)
    assert followed_up == []
    assert sent == []


def test_followup_outside_window_reschedules_without_sending(settings, one_med, monkeypatch):
    # 2am local (America/Chicago, UTC-5) — outside the default 9-21 window.
    now = datetime(2026, 9, 6, 7, 0, 0)
    _seed_awaiting_reply(
        settings, one_med.med_key, next_followup_at=now - timedelta(hours=1), first_nudged_at=now
    )

    sent = []
    monkeypatch.setattr(followup_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(followup_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(followup_mod, "now_utc", lambda: now)

    followed_up = followup_mod.run_followup_check(settings)
    assert followed_up == []
    assert sent == []

    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT next_followup_at FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert datetime.fromisoformat(row["next_followup_at"]) > now


def test_followup_cap_sends_final_message_and_stops(settings, one_med, monkeypatch):
    now = datetime(2026, 9, 6, 14, 0, 0)
    first_nudged_at = now - timedelta(days=settings.followup_cap_days, hours=1)
    _seed_awaiting_reply(
        settings, one_med.med_key, next_followup_at=now - timedelta(hours=1), first_nudged_at=first_nudged_at
    )

    sent = []
    monkeypatch.setattr(followup_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(followup_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(followup_mod, "now_utc", lambda: now)

    followed_up = followup_mod.run_followup_check(settings)
    assert followed_up == []
    assert len(sent) == 1
    assert "stop checking in" in sent[0]

    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT followup_final_sent FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["followup_final_sent"] == 1

    # A second run must not resend the final message.
    sent.clear()
    followup_mod.run_followup_check(settings)
    assert sent == []


def test_medication_missing_from_sheet_is_skipped(settings, one_med, monkeypatch):
    now = datetime(2026, 9, 6, 14, 0, 0)
    _seed_awaiting_reply(
        settings, one_med.med_key, next_followup_at=now - timedelta(hours=1), first_nudged_at=now
    )

    sent = []
    monkeypatch.setattr(followup_mod, "load_medications", lambda s: [])  # sheet row removed/renamed
    monkeypatch.setattr(followup_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(followup_mod, "now_utc", lambda: now)

    followed_up = followup_mod.run_followup_check(settings)
    assert followed_up == []
    assert sent == []


def test_sheet_unreachable_fails_silently(settings, monkeypatch):
    """Unlike run_nudge_check, this runs every 30 min — it must not text the owner every time
    the Sheet happens to be unreachable."""

    def _raise(s):
        raise ConnectionError("Google Sheets API is down")

    sent = []
    monkeypatch.setattr(followup_mod, "load_medications", _raise)
    monkeypatch.setattr(followup_mod, "send_sms", lambda s, body: sent.append(body))

    followed_up = followup_mod.run_followup_check(settings)
    assert followed_up == []
    assert sent == []
