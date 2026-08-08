from types import SimpleNamespace

from fastapi.testclient import TestClient
from telnyx.lib.webhooks_ed25519 import WebhookVerificationError

import app.main as main_mod
from app.db import get_conn
from app.models import create_call, set_call_outcome, upsert_thread_state
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


def _hangup_event(call_control_id: str):
    return SimpleNamespace(
        data=SimpleNamespace(
            event_type="call.hangup",
            payload=SimpleNamespace(call_control_id=call_control_id),
        )
    )


def _status_client(settings, monkeypatch, event, sent_messages):
    monkeypatch.setattr(main_mod, "get_settings", lambda: settings)
    monkeypatch.setattr(main_mod, "_verify_telnyx_webhook", lambda s, p, h: event)
    monkeypatch.setattr(main_mod, "send_sms", lambda s, body, to=None: sent_messages.append(body))
    return TestClient(main_mod.app)


def test_hangup_without_a_relay_unwedges_the_thread(settings, one_med, monkeypatch):
    """A call that dies before the relay connects must not leave the thread in CALLING —
    nothing would ever nudge that medication again, and the owner was told a call started."""
    with get_conn(str(settings.db_file)) as conn:
        create_call(conn, one_med.med_key, "ccid-dead")
        upsert_thread_state(conn, one_med.med_key, "CALLING")

    sent = []
    client = _status_client(settings, monkeypatch, _hangup_event("ccid-dead"), sent)
    resp = client.post("/voice/status", content=b"{}")

    assert resp.status_code == 200
    with get_conn(str(settings.db_file)) as conn:
        thread = conn.execute("SELECT state FROM threads WHERE med_key = ?", (one_med.med_key,)).fetchone()
        call = conn.execute(
            "SELECT outcome, ended_at FROM calls WHERE provider_call_sid = ?", ("ccid-dead",)
        ).fetchone()

    assert thread["state"] == "FAILED"
    assert call["outcome"] == "failed"
    assert call["ended_at"] is not None
    assert len(sent) == 1, "the owner must be told the call failed"


def test_hangup_preserves_an_amd_voicemail_outcome(settings, one_med, monkeypatch):
    with get_conn(str(settings.db_file)) as conn:
        create_call(conn, one_med.med_key, "ccid-vm")
        set_call_outcome(conn, "ccid-vm", "voicemail")

    sent = []
    client = _status_client(settings, monkeypatch, _hangup_event("ccid-vm"), sent)
    client.post("/voice/status", content=b"{}")

    with get_conn(str(settings.db_file)) as conn:
        call = conn.execute("SELECT outcome FROM calls WHERE provider_call_sid = ?", ("ccid-vm",)).fetchone()
    assert call["outcome"] == "voicemail", "AMD's finding must not be overwritten with 'failed'"


def test_hangup_is_idempotent(settings, one_med, monkeypatch):
    with get_conn(str(settings.db_file)) as conn:
        create_call(conn, one_med.med_key, "ccid-twice")

    sent = []
    client = _status_client(settings, monkeypatch, _hangup_event("ccid-twice"), sent)
    client.post("/voice/status", content=b"{}")
    client.post("/voice/status", content=b"{}")

    assert len(sent) == 1, "a repeated hangup webhook must not text the owner twice"


def test_hangup_defers_to_a_relay_that_connected(settings, one_med, monkeypatch):
    """The relay finalizes its own calls when the socket closes — hangup must not race it."""
    with get_conn(str(settings.db_file)) as conn:
        create_call(conn, one_med.med_key, "ccid-live")
    main_mod._seen_relays.add("ccid-live")
    try:
        sent = []
        client = _status_client(settings, monkeypatch, _hangup_event("ccid-live"), sent)
        client.post("/voice/status", content=b"{}")
    finally:
        main_mod._seen_relays.discard("ccid-live")

    assert sent == []
    with get_conn(str(settings.db_file)) as conn:
        call = conn.execute(
            "SELECT ended_at FROM calls WHERE provider_call_sid = ?", ("ccid-live",)
        ).fetchone()
    assert call["ended_at"] is None, "the relay's finally block owns finalization here"


def test_dtmf_gets_a_response_like_speech(settings, one_med, monkeypatch):
    """A keypress must drive a turn. It used to only append to the transcript, so a caller who
    pressed a key instead of talking got dead air (observed live 2026-08-08).

    The trailing prompt is deliberate: it guarantees the socket produces a frame either way, so
    reintroducing the bug fails this test on the assertion instead of blocking on receive_json.
    """
    seen = []

    def _next_action(s, med, turns, caller_text):
        seen.append(caller_text)
        return CallAction(action="speak", text="Thanks, one moment.")

    monkeypatch.setattr(main_mod, "next_call_action", _next_action)
    summary = CallSummary(outcome="unclear", detail="n/a")
    sent = []
    client = _relay_client(settings, one_med, monkeypatch, None, summary, sent)

    with client.websocket_connect(_relay_url(settings, one_med)) as ws:
        ws.send_json({"type": "setup", "call_control_id": "ccid-dtmf"})
        ws.send_json({"type": "dtmf", "digit": "1"})
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "Still there?"})
        reply = ws.receive_json()

    assert seen[0] == "[caller pressed 1]", "the digit must reach the model, before the speech does"
    assert reply == {"type": "text", "token": "Thanks, one moment.", "last": True}


def test_dtmf_can_end_the_call(settings, one_med, monkeypatch):
    seen = []

    def _next_action(s, med, turns, caller_text):
        seen.append(caller_text)
        return CallAction(action="end_call")

    monkeypatch.setattr(main_mod, "next_call_action", _next_action)
    summary = CallSummary(outcome="unclear", detail="n/a")
    sent = []
    client = _relay_client(settings, one_med, monkeypatch, None, summary, sent)

    with client.websocket_connect(_relay_url(settings, one_med)) as ws:
        ws.send_json({"type": "setup", "call_control_id": "ccid-dtmf-end"})
        ws.send_json({"type": "dtmf", "digit": "9"})
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "Still there?"})
        assert ws.receive_json() == {"type": "end"}

    # The end must have come from the digit, not from the trailing prompt.
    assert seen == ["[caller pressed 9]"]


def test_press_digits_action_sends_senddigits(settings, one_med, monkeypatch):
    """The outbound frame shape is still unverified against Telnyx — this only pins what we
    send, so a change to it is deliberate rather than accidental."""
    monkeypatch.setattr(
        main_mod, "next_call_action", lambda s, m, t, c: CallAction(action="press_digits", digits="1")
    )
    summary = CallSummary(outcome="unclear", detail="n/a")
    sent = []
    client = _relay_client(settings, one_med, monkeypatch, None, summary, sent)

    with client.websocket_connect(_relay_url(settings, one_med)) as ws:
        ws.send_json({"type": "setup", "call_control_id": "ccid-press"})
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "Press 1 for refills."})
        assert ws.receive_json() == {"type": "sendDigits", "digits": "1"}


def test_empty_dtmf_frame_is_ignored(settings, one_med, monkeypatch):
    calls = []
    monkeypatch.setattr(
        main_mod,
        "next_call_action",
        lambda s, m, t, c: calls.append(c) or CallAction(action="end_call"),
    )
    summary = CallSummary(outcome="unclear", detail="n/a")
    sent = []
    client = _relay_client(settings, one_med, monkeypatch, None, summary, sent)

    with client.websocket_connect(_relay_url(settings, one_med)) as ws:
        ws.send_json({"type": "setup", "call_control_id": "ccid-empty"})
        ws.send_json({"type": "dtmf"})
        ws.send_json({"type": "prompt", "last": True, "voicePrompt": "Anyone there?"})
        ws.receive_json()

    assert calls == ["Anyone there?"], "a digitless dtmf frame must not burn a model call"
