from datetime import date

import app.reply as reply_mod
from app.db import get_conn
from app.intent import Intent
from app.models import mark_nudged


def _seed_awaiting_reply(settings, med_key: str):
    with get_conn(str(settings.db_file)) as conn:
        mark_nudged(conn, med_key)


def test_no_active_thread_gives_graceful_reply(settings, one_med, monkeypatch):
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    result = reply_mod.handle_inbound_reply(settings, "yes please")
    assert result == reply_mod.NO_ACTIVE_THREAD_REPLY


def test_request_refill_stub_reply(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(reply_mod, "classify_reply", lambda *a, **k: Intent(kind="request_refill"))

    result = reply_mod.handle_inbound_reply(settings, "yes call them")

    assert one_med.prescriber in result
    assert one_med.prescriber_phone in result
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute("SELECT state FROM threads WHERE med_key = ?", (one_med.med_key,)).fetchone()
        assert row["state"] == "CALL_QUEUED"


def test_snooze_updates_thread(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod, "classify_reply", lambda *a, **k: Intent(kind="snooze", snooze_until=date(2026, 9, 1))
    )

    result = reply_mod.handle_inbound_reply(settings, "remind me in a week")

    assert "2026-09-01" in result
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, snooze_until FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "SNOOZED"
        assert row["snooze_until"] == "2026-09-01"


def test_question_returns_model_answer(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod, "classify_reply", lambda *a, **k: Intent(kind="question", answer="You last filled it June 2.")
    )

    result = reply_mod.handle_inbound_reply(settings, "when did I last fill this?")
    assert result == "You last filled it June 2."


def test_medication_missing_from_sheet_after_nudge(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [])  # sheet row removed/renamed

    result = reply_mod.handle_inbound_reply(settings, "yes")
    assert one_med.med_key in result
