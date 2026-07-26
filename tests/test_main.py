from fastapi.testclient import TestClient

import app.main as main_mod


def _client(settings, monkeypatch, valid_signature=True, reply_text="ok"):
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "_validate_twilio_request", _async_bool(valid_signature))
    monkeypatch.setattr(main_mod, "handle_inbound_reply", lambda s, body: reply_text)
    return TestClient(main_mod.app)


def _async_bool(value: bool):
    async def _fn(request, form):
        return value

    return _fn


def test_health():
    client = TestClient(main_mod.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_invalid_signature_rejected(settings, monkeypatch):
    client = _client(settings, monkeypatch, valid_signature=False)
    resp = client.post("/sms/inbound", data={"From": settings.owner_phone, "Body": "hi"})
    assert resp.status_code == 403


def test_non_owner_sender_gets_silent_empty_response(settings, monkeypatch):
    client = _client(settings, monkeypatch, valid_signature=True)
    resp = client.post("/sms/inbound", data={"From": "+19995551234", "Body": "hi"})
    assert resp.status_code == 200
    assert "<Message>" not in resp.text


def test_owner_sender_gets_reply(settings, monkeypatch):
    client = _client(settings, monkeypatch, valid_signature=True, reply_text="here's your answer")
    resp = client.post("/sms/inbound", data={"From": settings.owner_phone, "Body": "question"})
    assert resp.status_code == 200
    assert "here's your answer" in resp.text
