from datetime import date, datetime

import app.reply as reply_mod
from app.db import get_conn
from app.intent import FillConfirmation, Intent
from app.models import mark_nudged, set_pending_fill

BASE_URL = "https://example.trycloudflare.com"


def _seed_awaiting_reply(settings, med_key: str):
    with get_conn(str(settings.db_file)) as conn:
        mark_nudged(conn, med_key)


def test_no_active_thread_gives_graceful_reply(settings, one_med, monkeypatch):
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    result = reply_mod.handle_inbound_reply(settings, "yes please", BASE_URL)
    assert result == reply_mod.NO_ACTIVE_THREAD_REPLY


def test_request_refill_places_call(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(reply_mod, "classify_reply", lambda *a, **k: Intent(kind="request_refill"))

    calls = []

    def fake_place_refill_call(settings, med, base_url, conn):
        calls.append((med.med_key, base_url))
        return "fake_call_control_id"

    monkeypatch.setattr(reply_mod, "place_refill_call", fake_place_refill_call)

    result = reply_mod.handle_inbound_reply(settings, "yes call them", BASE_URL)

    assert one_med.prescriber in result
    assert calls == [(one_med.med_key, BASE_URL)]


def test_request_refill_call_failure_gives_graceful_reply(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(reply_mod, "classify_reply", lambda *a, **k: Intent(kind="request_refill"))

    def failing_place_refill_call(*args, **kwargs):
        raise RuntimeError("Telnyx exploded")

    monkeypatch.setattr(reply_mod, "place_refill_call", failing_place_refill_call)

    result = reply_mod.handle_inbound_reply(settings, "yes call them", BASE_URL)

    assert one_med.prescriber in result
    assert "problem" in result.lower()


def test_snooze_updates_thread(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod, "classify_reply", lambda *a, **k: Intent(kind="snooze", snooze_until=date(2026, 9, 1))
    )

    result = reply_mod.handle_inbound_reply(settings, "remind me in a week", BASE_URL)

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
    answer_intent = Intent(kind="question", answer="You last filled it June 2.")
    monkeypatch.setattr(reply_mod, "classify_reply", lambda *a, **k: answer_intent)

    result = reply_mod.handle_inbound_reply(settings, "when did I last fill this?", BASE_URL)
    assert result == "You last filled it June 2."


def test_question_reply_reschedules_followup(settings, one_med, monkeypatch):
    stale_next_followup = datetime(2026, 1, 1, 0, 0, 0)
    with get_conn(str(settings.db_file)) as conn:
        mark_nudged(conn, one_med.med_key, stale_next_followup)

    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    answer_intent = Intent(kind="question", answer="You last filled it June 2.")
    monkeypatch.setattr(reply_mod, "classify_reply", lambda *a, **k: answer_intent)

    reply_mod.handle_inbound_reply(settings, "when did I last fill this?", BASE_URL)

    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, next_followup_at FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "AWAITING_REPLY"
        assert datetime.fromisoformat(row["next_followup_at"]) > stale_next_followup


def test_medication_missing_from_sheet_after_nudge(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [])  # sheet row removed/renamed

    result = reply_mod.handle_inbound_reply(settings, "yes", BASE_URL)
    assert one_med.med_key in result


def test_mark_filled_named_with_no_open_thread_starts_confirmation(settings, one_med, monkeypatch):
    # No mark_nudged call — this is an unprompted text naming the medication directly.
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod, "classify_reply", lambda *a, **k: Intent(kind="mark_filled", filled_on=date(2026, 9, 5))
    )

    result = reply_mod.handle_inbound_reply(settings, "just filled the Lisinopril", BASE_URL)

    assert "2026-09-05" in result
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, pending_fill_date FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "AWAITING_FILL_CONFIRMATION"
        assert row["pending_fill_date"] == "2026-09-05"


def test_mark_filled_without_date_defaults_to_today(settings, one_med, monkeypatch):
    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(reply_mod, "classify_reply", lambda *a, **k: Intent(kind="mark_filled"))
    monkeypatch.setattr(reply_mod, "local_today", lambda s: date(2026, 9, 6))

    result = reply_mod.handle_inbound_reply(settings, "just filled it", BASE_URL)

    assert "2026-09-06" in result
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, pending_fill_date FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "AWAITING_FILL_CONFIRMATION"
        assert row["pending_fill_date"] == "2026-09-06"


def test_fill_confirmation_confirmed_writes_to_sheet(settings, one_med, monkeypatch):
    with get_conn(str(settings.db_file)) as conn:
        set_pending_fill(conn, one_med.med_key, date(2026, 9, 5))

    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod, "classify_fill_confirmation", lambda *a, **k: FillConfirmation(kind="confirmed")
    )

    written = []
    monkeypatch.setattr(
        reply_mod, "mark_filled", lambda s, med_key, filled_on: written.append((med_key, filled_on))
    )

    result = reply_mod.handle_inbound_reply(settings, "yes", BASE_URL)

    assert "2026-09-05" in result
    assert written == [(one_med.med_key, date(2026, 9, 5))]
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, pending_fill_date FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "IDLE"
        assert row["pending_fill_date"] is None


def test_fill_confirmation_write_failure_gives_graceful_reply(settings, one_med, monkeypatch):
    with get_conn(str(settings.db_file)) as conn:
        set_pending_fill(conn, one_med.med_key, date(2026, 9, 5))

    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod, "classify_fill_confirmation", lambda *a, **k: FillConfirmation(kind="confirmed")
    )

    def failing_mark_filled(*args, **kwargs):
        raise RuntimeError("Sheets exploded")

    monkeypatch.setattr(reply_mod, "mark_filled", failing_mark_filled)

    result = reply_mod.handle_inbound_reply(settings, "yes", BASE_URL)
    assert "problem" in result.lower()


def test_fill_confirmation_corrected_updates_pending_date(settings, one_med, monkeypatch):
    with get_conn(str(settings.db_file)) as conn:
        set_pending_fill(conn, one_med.med_key, date(2026, 9, 5))

    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod,
        "classify_fill_confirmation",
        lambda *a, **k: FillConfirmation(kind="corrected", corrected_date=date(2026, 9, 3)),
    )

    result = reply_mod.handle_inbound_reply(settings, "no, the 3rd", BASE_URL)

    assert "2026-09-03" in result
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, pending_fill_date FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "AWAITING_FILL_CONFIRMATION"
        assert row["pending_fill_date"] == "2026-09-03"


def test_fill_confirmation_cancelled_makes_no_sheet_write(settings, one_med, monkeypatch):
    with get_conn(str(settings.db_file)) as conn:
        set_pending_fill(conn, one_med.med_key, date(2026, 9, 5))

    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(
        reply_mod, "classify_fill_confirmation", lambda *a, **k: FillConfirmation(kind="cancelled")
    )

    written = []
    monkeypatch.setattr(
        reply_mod, "mark_filled", lambda s, med_key, filled_on: written.append((med_key, filled_on))
    )

    reply_mod.handle_inbound_reply(settings, "no nevermind", BASE_URL)

    assert written == []
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT state, pending_fill_date FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()
        assert row["state"] == "IDLE"
        assert row["pending_fill_date"] is None


def test_ambiguous_name_match_falls_back_to_active_thread(settings, one_med, monkeypatch):
    from app.sheet import parse_records
    from tests.conftest import GOOD_ROW

    other_row = dict(GOOD_ROW, dose="10mg HCTZ")
    other_row["med"] = "Lisinopril HCTZ"
    other_med = parse_records([other_row])[0]

    _seed_awaiting_reply(settings, one_med.med_key)
    monkeypatch.setattr(reply_mod, "load_medications", lambda s: [one_med, other_med])
    monkeypatch.setattr(reply_mod, "classify_reply", lambda *a, **k: Intent(kind="question", answer="ok"))

    result = reply_mod.handle_inbound_reply(settings, "question about Lisinopril", BASE_URL)

    # Ambiguous name match (matches both) falls back to get_active_thread, which resolves
    # to one_med's thread (the only one with an open AWAITING_REPLY state).
    assert result == "ok"
    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute(
            "SELECT med_key FROM messages WHERE direction = 'out' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row["med_key"] == one_med.med_key
