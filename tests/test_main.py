from types import SimpleNamespace

from fastapi.testclient import TestClient
from telnyx.lib.webhooks_ed25519 import WebhookVerificationError

import app.main as main_mod
from app.db import get_conn
from app.voice import (
    DISCLOSURE_GREETING,
    INTERRUPTION_MARKER,
    CallAction,
    CallSummary,
    sign_relay_token,
)


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


def _relay_client(settings, one_med, monkeypatch, actions, summary, sent_messages):
    """TestClient wired for a /voice/relay websocket run, with every external call stubbed.

    `actions` is consumed one per accepted caller utterance — so its length is exactly the
    number of frames that survived filtering, which is what these tests assert on.
    """
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "load_medications", lambda s: [one_med])
    monkeypatch.setattr(main_mod, "summarize_call", lambda s, med, turns: summary)
    monkeypatch.setattr(main_mod, "send_sms", lambda s, body, to=None: sent_messages.append(body))
    return TestClient(main_mod.app)


def _relay_url(settings, one_med):
    rid = "deadbeef"
    token = sign_relay_token(settings.relay_signing_secret, rid, one_med.med_key)
    return f"/voice/relay?rid={rid}&med_key={one_med.med_key}&token={token}"


def test_relay_seeds_greeting_and_filters_junk_frames(settings, one_med, monkeypatch):
    """The greeting Conversation Relay speaks must be the model's first turn, and ASR junk
    must never reach the model — each accepted frame costs a model call and a live pause."""
    seen = []

    def _next_action(s, med, turns, caller_text):
        seen.append((list(turns), caller_text))
        return CallAction(action="end_call")

    monkeypatch.setattr(main_mod, "next_call_action", _next_action)
    summary = CallSummary(outcome="refused", detail="They declined.")
    sent = []
    client = _relay_client(settings, one_med, monkeypatch, None, summary, sent)

    with client.websocket_connect(_relay_url(settings, one_med)) as ws:
        ws.send_json({"type": "setup", "call_control_id": "ccid-1"})
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "uh"})
        ws.send_json({"type": "prompt", "last": False, "voicePrompt": "partial fragment here"})
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "Yes, this is the office."})
        ws.receive_json()

    assert len(seen) == 1, "filler and non-final frames must not reach the model"
    turns_at_first_call, caller_text = seen[0]
    assert caller_text == "Yes, this is the office."
    assert turns_at_first_call == [
        {"role": "assistant", "content": DISCLOSURE_GREETING.format(patient_name=settings.patient_name)}
    ]


def test_relay_logs_outcome_sms_to_messages_table(settings, one_med, monkeypatch):
    monkeypatch.setattr(main_mod, "next_call_action", lambda s, m, t, c: CallAction(action="end_call"))
    summary = CallSummary(outcome="refused", detail="They want an appointment first.")
    sent = []
    client = _relay_client(settings, one_med, monkeypatch, None, summary, sent)

    with client.websocket_connect(_relay_url(settings, one_med)) as ws:
        ws.send_json({"type": "setup", "call_control_id": "ccid-2"})
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "We can't do that."})
        ws.receive_json()

    assert len(sent) == 1
    with get_conn(str(settings.db_file)) as conn:
        rows = conn.execute(
            "SELECT direction, body FROM messages WHERE med_key = ?", (one_med.med_key,)
        ).fetchall()

    assert [r["body"] for r in rows] == sent, "the outcome SMS must be logged like every other body"
    assert rows[0]["direction"] == "out"


def test_relay_interrupt_frame_reaches_the_transcript(settings, one_med, monkeypatch):
    """A barge-in must reach the model as context, not be silently dropped — the greeting
    is what gets interrupted in practice, and it carries the disclosure."""
    seen = []

    def _next_action(s, med, turns, caller_text):
        seen.append(list(turns))
        return CallAction(action="end_call")

    monkeypatch.setattr(main_mod, "next_call_action", _next_action)
    summary = CallSummary(outcome="refused", detail="No.")
    sent = []
    client = _relay_client(settings, one_med, monkeypatch, None, summary, sent)

    with client.websocket_connect(_relay_url(settings, one_med)) as ws:
        ws.send_json({"type": "setup", "call_control_id": "ccid-3"})
        ws.send_json(
            {
                "type": "interrupt",
                "durationUntilInterruptMs": 341,
                "utteranceUntilInterrupt": "Hi, this is an auto",
            }
        )
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "Who is this?"})
        ws.receive_json()

    assert len(seen) == 1
    greeting_turn = seen[0][0]
    assert greeting_turn["content"] == f"Hi, this is an auto {INTERRUPTION_MARKER}"
