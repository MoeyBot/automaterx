from types import SimpleNamespace

from fastapi.testclient import TestClient
from telnyx.lib.webhooks_ed25519 import WebhookVerificationError

import app.main as main_mod


def _event(from_number: str, text: str):
    return SimpleNamespace(
        data=SimpleNamespace(
            event_type="message.received",
            payload=SimpleNamespace(
                from_=SimpleNamespace(phone_number=from_number),
                text=text,
            ),
        )
    )


def _client(settings, monkeypatch, event=None, valid_signature=True, sent_messages=None):
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)

    def _verify(settings, payload, headers):
        if not valid_signature:
            raise WebhookVerificationError("bad signature")
        return event

    monkeypatch.setattr(main_mod, "_verify_telnyx_webhook", _verify)
    monkeypatch.setattr(main_mod, "handle_inbound_reply", lambda s, body, base_url: "here's your answer")

    if sent_messages is not None:
        monkeypatch.setattr(main_mod, "send_sms", lambda s, body, to=None: sent_messages.append(body))

    return TestClient(main_mod.app)


def test_health():
    client = TestClient(main_mod.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_invalid_signature_rejected(settings, monkeypatch):
    client = _client(settings, monkeypatch, valid_signature=False)
    resp = client.post("/sms/inbound", content=b"{}")
    assert resp.status_code == 403


def test_non_owner_sender_gets_no_reply(settings, monkeypatch):
    sent_messages = []
    event = _event("+19995551234", "hi")
    client = _client(settings, monkeypatch, event=event, sent_messages=sent_messages)
    resp = client.post("/sms/inbound", content=b"{}")
    assert resp.status_code == 200
    assert sent_messages == []


def test_owner_sender_gets_reply(settings, monkeypatch):
    sent_messages = []
    event = _event(settings.owner_phone, "question")
    client = _client(settings, monkeypatch, event=event, sent_messages=sent_messages)
    resp = client.post("/sms/inbound", content=b"{}")
    assert resp.status_code == 200
    assert sent_messages == ["here's your answer"]
