from types import SimpleNamespace

import app.voice as voice_mod
from app.db import get_conn
from app.voice import (
    build_fact_sheet,
    build_relay_url,
    place_refill_call,
    sign_relay_token,
    verify_relay_token,
)

BASE_URL = "https://example.trycloudflare.com"


def test_build_fact_sheet_includes_identity_and_medication(settings, one_med):
    sheet = build_fact_sheet(settings, one_med)
    assert settings.patient_name in sheet
    assert settings.patient_dob in sheet
    assert settings.owner_phone in sheet
    assert one_med.med in sheet
    assert one_med.dose in sheet
    assert one_med.prescriber in sheet
    assert settings.pharmacy_name in sheet


def test_relay_token_roundtrip():
    secret = "test-secret"
    token = sign_relay_token(secret, "rid123", "lisinopril_10mg")
    assert verify_relay_token(secret, "rid123", "lisinopril_10mg", token)


def test_relay_token_rejects_tampered_med_key():
    secret = "test-secret"
    token = sign_relay_token(secret, "rid123", "lisinopril_10mg")
    assert not verify_relay_token(secret, "rid123", "metformin_500mg", token)


def test_relay_token_rejects_wrong_secret():
    token = sign_relay_token("secret-a", "rid123", "lisinopril_10mg")
    assert not verify_relay_token("secret-b", "rid123", "lisinopril_10mg", token)


def test_build_relay_url_uses_wss_scheme():
    url = build_relay_url(BASE_URL, "test-secret", "rid123", "lisinopril_10mg")
    assert url.startswith("wss://")
    assert "rid=rid123" in url
    assert "med_key=lisinopril_10mg" in url
    assert "token=" in url


def test_place_refill_call_dials_and_persists_state(settings, one_med, monkeypatch):
    dial_calls = []

    class FakeCalls:
        def dial(self, **kwargs):
            dial_calls.append(kwargs)
            return SimpleNamespace(data=SimpleNamespace(call_control_id="fake_ccid"))

    class FakeTelnyxClient:
        def __init__(self, api_key):
            self.calls = FakeCalls()

    monkeypatch.setattr(voice_mod, "Telnyx", FakeTelnyxClient)

    with get_conn(str(settings.db_file)) as conn:
        call_control_id = place_refill_call(settings, one_med, BASE_URL, conn)

    assert call_control_id == "fake_ccid"
    assert len(dial_calls) == 1
    assert dial_calls[0]["to"] == one_med.prescriber_phone
    assert dial_calls[0]["from_"] == settings.telnyx_from_number
    assert dial_calls[0]["conversation_relay_config"]["url"].startswith("wss://")

    with get_conn(str(settings.db_file)) as conn:
        call_row = conn.execute("SELECT * FROM calls WHERE provider_call_sid = ?", ("fake_ccid",)).fetchone()
        thread_row = conn.execute(
            "SELECT state FROM threads WHERE med_key = ?", (one_med.med_key,)
        ).fetchone()

    assert call_row["med_key"] == one_med.med_key
    assert thread_row["state"] == "CALLING"
