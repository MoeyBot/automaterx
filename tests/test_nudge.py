from datetime import date

import app.nudge as nudge_mod
from app.db import get_conn
from app.models import set_snooze


def test_due_medication_gets_nudged(settings, one_med, monkeypatch):
    sent = []
    monkeypatch.setattr(nudge_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(nudge_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(nudge_mod, "local_today", lambda s: one_med.nudge_date)

    nudged = nudge_mod.run_nudge_check(settings)

    assert nudged == [one_med.med_key]
    assert len(sent) == 1
    assert one_med.med in sent[0]

    with get_conn(str(settings.db_file)) as conn:
        row = conn.execute("SELECT state FROM threads WHERE med_key = ?", (one_med.med_key,)).fetchone()
        assert row["state"] == "AWAITING_REPLY"


def test_not_yet_due_is_skipped(settings, one_med, monkeypatch):
    sent = []
    monkeypatch.setattr(nudge_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(nudge_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(nudge_mod, "local_today", lambda s: date(2026, 1, 1))

    nudged = nudge_mod.run_nudge_check(settings)
    assert nudged == []
    assert sent == []


def test_snoozed_medication_stays_quiet_until_date(settings, one_med, monkeypatch):
    with get_conn(str(settings.db_file)) as conn:
        set_snooze(conn, one_med.med_key, date(2026, 9, 1))

    sent = []
    monkeypatch.setattr(nudge_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(nudge_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(nudge_mod, "local_today", lambda s: one_med.nudge_date)

    nudged = nudge_mod.run_nudge_check(settings)
    assert nudged == []
    assert sent == []


def test_already_awaiting_reply_is_not_re_nudged(settings, one_med, monkeypatch):
    from app.models import mark_nudged

    with get_conn(str(settings.db_file)) as conn:
        mark_nudged(conn, one_med.med_key)

    sent = []
    monkeypatch.setattr(nudge_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(nudge_mod, "send_sms", lambda s, body: sent.append(body))
    monkeypatch.setattr(nudge_mod, "local_today", lambda s: one_med.nudge_date)

    nudged = nudge_mod.run_nudge_check(settings)
    assert nudged == []
    assert sent == []


def test_sheet_validation_error_alerts_owner_instead_of_crashing(settings, monkeypatch):
    from app.sheet import SheetValidationError

    def _raise(s):
        raise SheetValidationError(["row 2: bad status"])

    sent = []
    monkeypatch.setattr(nudge_mod, "load_medications", _raise)
    monkeypatch.setattr(nudge_mod, "send_sms", lambda s, body: sent.append(body))

    nudged = nudge_mod.run_nudge_check(settings)
    assert nudged == []
    assert len(sent) == 1
    assert "bad status" in sent[0]
