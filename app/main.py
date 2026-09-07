import json
import logging
from contextlib import asynccontextmanager
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from telnyx import Telnyx
from telnyx.lib.webhooks_ed25519 import WebhookVerificationError, unwrap_with_ed25519

from app.config import Settings, get_settings
from app.db import get_conn, init_db
from app.followup import run_followup_check
from app.logging_config import configure_logging
from app.models import (
    Medication,
    claim_call_finalization,
    finish_call,
    get_call,
    log_message,
    set_call_outcome,
    upsert_thread_state,
)
from app.nudge import run_nudge_check
from app.reply import handle_inbound_reply
from app.sheet import load_medications, mark_filled
from app.sms import send_sms
from app.voice import (
    DISCLOSURE_GREETING,
    VOICEMAIL_SCRIPT,
    apply_interruption,
    is_usable_utterance,
    next_call_action,
    summarize_call,
    verify_relay_token,
)

configure_logging()
logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_db(str(settings.db_file))

    scheduler.add_job(
        run_nudge_check,
        CronTrigger(hour=settings.nudge_hour_local, minute=0, timezone=settings.timezone),
        args=[settings],
        id="daily_nudge_check",
        replace_existing=True,
    )
    scheduler.add_job(
        run_followup_check,
        IntervalTrigger(minutes=30),
        args=[settings],
        id="followup_check",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(
        "Scheduler started, daily nudge check at %02d:00 %s, follow-up check every 30 min",
        settings.nudge_hour_local,
        settings.timezone,
    )

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


def _verify_telnyx_webhook(settings: Settings, payload: bytes, headers):
    client = Telnyx(api_key=settings.telnyx_api_key, public_key=settings.telnyx_public_key)
    return unwrap_with_ed25519(client, payload, headers)


def _base_url(request: Request) -> str:
    """The externally-visible origin (scheme://host), used to build the /voice/relay wss:// URL
    and /voice/status webhook URL passed to Telnyx when placing a call.

    Fly.io (and most PaaS) terminate TLS at the edge and forward plain HTTP to the container,
    so request.url reports "http://" even when the outside world sees "https://".
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/sms/inbound")
async def sms_inbound(request: Request):
    settings = get_settings()
    payload = await request.body()

    try:
        event = _verify_telnyx_webhook(settings, payload, request.headers)
    except WebhookVerificationError:
        logger.warning("Rejected inbound SMS webhook with invalid Telnyx signature")
        return Response(status_code=403)

    if event.data is None or event.data.event_type != "message.received" or event.data.payload is None:
        return Response(status_code=200)

    message = event.data.payload
    from_number = message.from_.phone_number if message.from_ else ""
    body = message.text or ""

    if from_number != settings.owner_phone:
        logger.warning("Rejected SMS from non-owner number %s", from_number)
        # Deliberately silent: no reply sent, so an unknown sender gets no confirmation
        # that this number is live and hooked up to anything.
        return Response(status_code=200)

    reply_text = handle_inbound_reply(settings, body, _base_url(request))
    send_sms(settings, reply_text)
    return Response(status_code=200)


# Live calls in progress, keyed by call_control_id — lets /voice/status (AMD result) reach into
# an already-open /voice/relay websocket to cut it over to the voicemail script. Single-process
# in-memory state is fine here per PLAN.md §2 (the websocket is why this isn't serverless).
_active_relays: dict[str, WebSocket] = {}

# Calls whose relay websocket has connected at some point in this process. `_active_relays`
# can't answer "did a relay ever run for this call?" — an entry disappears when the socket
# closes, so an absent key means either "never connected" or "already finished". The
# call.hangup handler needs to tell those apart: only the first kind has nobody else to
# finalize it. Same process hosts both halves by design (PLAN.md §2).
_seen_relays: set[str] = set()


async def _send_voicemail_and_close(websocket: WebSocket, settings: Settings, med: Medication) -> None:
    script = VOICEMAIL_SCRIPT.format(
        patient_name=settings.patient_name,
        med=med.med,
        dose=med.dose,
        callback=settings.owner_phone,
    )
    await websocket.send_json({"type": "text", "token": script, "last": True})
    await websocket.send_json({"type": "end"})
    await websocket.close()


def _finish_call_and_notify(
    settings: Settings, med: Medication, call_control_id: str, turns: list[dict]
) -> None:
    with get_conn(str(settings.db_file)) as conn:
        # Only an existing-but-already-finalized row means someone else handled this. A
        # missing row is a different problem, and staying silent about a finished call is
        # worse than a duplicate text, so fall through and notify.
        if get_call(conn, call_control_id) is not None and not claim_call_finalization(conn, call_control_id):
            logger.info("Call %s was already finalized; skipping", call_control_id)
            return

    summary = summarize_call(settings, med, turns)

    with get_conn(str(settings.db_file)) as conn:
        finish_call(conn, call_control_id, summary.outcome, json.dumps(turns), summary.detail)
        upsert_thread_state(conn, med.med_key, "DONE" if summary.outcome == "sent_to_pharmacy" else "FAILED")

    if summary.outcome == "sent_to_pharmacy":
        try:
            mark_filled(settings, med.med_key, date.today())
        except Exception:
            logger.exception("Failed to write last_filled back to the Sheet for %s", med.med_key)

    # Logged like every other outbound body (app/reply.py does this for the SMS path) — this
    # is the message that tells the owner what happened on the call, so it's the last one that
    # should be missing from the messages table when reconstructing a call after the fact.
    outcome_text = f"Call to {med.prescriber} about {med.med} {med.dose}: {summary.detail}"
    send_sms(settings, outcome_text)
    with get_conn(str(settings.db_file)) as conn:
        log_message(conn, med.med_key, "out", outcome_text)


async def _run_agent_turn(
    websocket: WebSocket, settings: Settings, med: Medication, turns: list[dict], caller_text: str
) -> tuple[bool, str | None]:
    """Runs one turn of the call: decide, record, send. Returns (call_should_end, text_spoken).

    Speech and DTMF both come through here. They used to be handled separately, and the DTMF
    branch only appended to `turns` without ever asking the model what to do — so a caller who
    pressed a key instead of talking got dead air until they spoke (observed live 2026-08-08:
    the callee pressed 1, the agent said nothing at all, and the call died with a two-line
    transcript).
    """
    action = next_call_action(settings, med, turns, caller_text)
    turns.append({"role": "user", "content": caller_text})

    if action.action == "speak":
        text = action.text or ""
        turns.append({"role": "assistant", "content": text})
        await websocket.send_json({"type": "text", "token": text, "last": True})
        return False, text

    if action.action == "press_digits":
        digits = action.digits or ""
        turns.append({"role": "assistant", "content": f"[pressed {digits}]"})
        # The sendDigits shape is still unverified — Telnyx's example app only ever sends
        # `text`, and an unrecognized field elsewhere in this API silently broke a whole call
        # rather than erroring. Log what goes out so a failure here is diagnosable.
        logger.info("Sending outbound frame: %s", {"type": "sendDigits", "digits": digits})
        await websocket.send_json({"type": "sendDigits", "digits": digits})
        return False, None

    turns.append({"role": "assistant", "content": "[ending call]"})
    await websocket.send_json({"type": "end"})
    return True, None


def _finish_unconnected_call(settings: Settings, call_control_id: str) -> None:
    """Closes out a call whose relay websocket never connected.

    Observed live on 2026-08-08: a bad conversation_relay_config made Telnyx answer the call,
    run AMD, then hang up without ever starting Conversation Relay. Nothing finalized the
    call — the thread sat in CALLING forever (so no future nudge could fire for that
    medication) and the owner never heard back, having just been texted that a call was
    starting. There's no transcript to summarize here, so the outcome is recorded directly
    rather than via summarize_call.
    """
    with get_conn(str(settings.db_file)) as conn:
        call_row = get_call(conn, call_control_id)
        if call_row is None or not claim_call_finalization(conn, call_control_id):
            return
        med_key = call_row["med_key"]
        # AMD may already have recorded an interim outcome — don't discard what it learned.
        outcome = call_row["outcome"] or "failed"
        detail = (
            "reached voicemail, and no message could be left"
            if outcome == "voicemail"
            else "didn't connect properly, so nothing was requested"
        )
        finish_call(conn, call_control_id, outcome, json.dumps([]), detail)
        upsert_thread_state(conn, med_key, "FAILED")

    logger.warning("Call %s ended with no relay connection (outcome=%s)", call_control_id, outcome)
    text = f"The refill call for {med_key} {detail}. You may want to call yourself."
    send_sms(settings, text)
    with get_conn(str(settings.db_file)) as conn:
        log_message(conn, med_key, "out", text)


@app.websocket("/voice/relay")
async def voice_relay(websocket: WebSocket):
    """Conversation Relay's live call loop.

    Inbound `setup`/`prompt`/`dtmf` field names (`sessionId`, `voicePrompt`, `last`, `digit`)
    are confirmed against Telnyx's own example (github.com/team-telnyx/telnyx-code-examples,
    conversation-relay-voice-bot-python/app.py) after a live controlled-number test showed the
    original guesses (`text`, `voice_text`) left every caller turn empty. `call_control_id` on
    the setup frame isn't in that example but empirically worked in the same test.

    Still unverified (that example doesn't cover them): the outbound `sendDigits`/`end` message
    shapes, and the AMD event-type/result field names checked in `/voice/status` below.
    """
    settings = get_settings()
    rid = websocket.query_params.get("rid", "")
    med_key = websocket.query_params.get("med_key", "")
    token = websocket.query_params.get("token", "")

    has_params = rid and med_key and token
    if not (has_params and verify_relay_token(settings.relay_signing_secret, rid, med_key, token)):
        logger.warning("Rejected /voice/relay connection with an invalid or missing token")
        await websocket.close(code=4403)
        return

    try:
        meds = load_medications(settings)
    except Exception:
        logger.exception("Sheet unreadable while starting a call for %s", med_key)
        await websocket.close(code=4500)
        return

    med = next((m for m in meds if m.med_key == med_key), None)
    if med is None:
        logger.error("Medication %s not found for an active call", med_key)
        await websocket.close(code=4404)
        return

    await websocket.accept()

    # Conversation Relay speaks DISCLOSURE_GREETING itself, before this websocket sees any
    # caller audio — so it's genuinely our first turn and belongs in the transcript. Without
    # it the model believes it hasn't spoken yet and opens by re-introducing itself (the
    # callee hears the same disclosure twice, observed on the 2026-08-08 live call), and
    # summarize_call sees a transcript in which the call never disclosed at all.
    greeting = DISCLOSURE_GREETING.format(patient_name=settings.patient_name)
    turns: list[dict] = [{"role": "assistant", "content": greeting}]
    last_spoken: str = greeting
    call_control_id: str | None = None

    try:
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")
            # Frame-shape logging: the field names here were guessed wrong once already (see
            # the docstring), and a silent call is impossible to diagnose without seeing what
            # actually arrived — but the caller's actual words are what they said to a live
            # human on a call about their prescriptions, so only its presence/length is logged,
            # never the content itself.
            if msg_type == "prompt":
                voice_prompt = msg.get("voicePrompt")
                logger.info(
                    "relay prompt frame: last=%r has_voice_prompt=%s other_keys=%s",
                    msg.get("last"),
                    bool(voice_prompt),
                    sorted(k for k in msg if k not in {"type", "last", "voicePrompt"}),
                    extra={
                        "event": "relay_frame",
                        "call_control_id": call_control_id,
                        "frame_type": msg_type,
                        "voice_prompt_len": len(voice_prompt) if voice_prompt else 0,
                    },
                )
            else:
                logger.info(
                    "relay %s frame: %s",
                    msg_type,
                    {k: msg.get(k) for k in sorted(msg)},
                    extra={
                        "event": "relay_frame",
                        "call_control_id": call_control_id,
                        "frame_type": msg_type,
                    },
                )

            if msg_type == "setup":
                call_control_id = msg.get("call_control_id") or msg.get("callControlId")
                if call_control_id:
                    _active_relays[call_control_id] = websocket
                    _seen_relays.add(call_control_id)
                    with get_conn(str(settings.db_file)) as conn:
                        call_row = get_call(conn, call_control_id)
                    if call_row and call_row["outcome"] == "voicemail":
                        # AMD already flagged a machine before this websocket connected.
                        await _send_voicemail_and_close(websocket, settings, med)
                        break
                continue

            if msg_type == "prompt":
                # Telnyx sends multiple "prompt" frames per utterance (partial + final,
                # confirmed against team-telnyx/telnyx-code-examples/conversation-relay-voice-bot-python)
                # — only act on the final one, and read the caller's speech from `voicePrompt`,
                # not `text` (that guess was wrong and left every turn empty in the first test).
                if msg.get("last") is not True:
                    continue
                raw_text = msg.get("voicePrompt") or msg.get("text") or msg.get("transcript") or ""
                caller_text = str(raw_text).strip()
                if not is_usable_utterance(caller_text, last_spoken):
                    logger.info("Ignoring non-actionable prompt frame: %r", caller_text)
                    continue
                ended, spoken = await _run_agent_turn(websocket, settings, med, turns, caller_text)
                if spoken is not None:
                    last_spoken = spoken
                if ended:
                    break

            elif msg_type == "dtmf":
                # `digit` confirmed live 2026-08-08; `digits` kept as a harmless fallback.
                digit = msg.get("digit") or msg.get("digits") or ""
                if not digit:
                    continue
                # Each keypress is its own turn, so someone entering several digits in a row
                # produces several model calls. Acceptable here: we're the caller, so digits
                # pressed at us are rare and usually single ("press 1 if you're a human").
                ended, spoken = await _run_agent_turn(
                    websocket, settings, med, turns, f"[caller pressed {digit}]"
                )
                if spoken is not None:
                    last_spoken = spoken
                if ended:
                    break

            elif msg_type == "interrupt":
                # The caller talked over our TTS. There's nothing queued to cancel, but the
                # transcript must not keep claiming we delivered a line they cut off — see
                # apply_interruption. Observed live: the callee barged in 341ms into the
                # greeting, i.e. over the disclosure itself.
                apply_interruption(turns, msg.get("utteranceUntilInterrupt"))
                if turns and turns[-1]["role"] == "assistant":
                    last_spoken = turns[-1]["content"]

            elif msg_type == "error":
                logger.error("Conversation Relay reported an error: %s", msg)
                break

    except WebSocketDisconnect:
        pass
    finally:
        if call_control_id:
            _active_relays.pop(call_control_id, None)
            _finish_call_and_notify(settings, med, call_control_id, turns)


@app.post("/voice/status")
async def voice_status(request: Request):
    """Telnyx Call Control webhook: AMD result, call.hangup, etc.

    NOTE: the AMD event-type/result field check below is a best guess (unverified against live
    traffic, same caveat as /voice/relay above) — confirm during the controlled-number test.
    """
    settings = get_settings()
    payload = await request.body()

    try:
        event = _verify_telnyx_webhook(settings, payload, request.headers)
    except WebhookVerificationError:
        logger.warning("Rejected /voice/status webhook with invalid Telnyx signature")
        return Response(status_code=403)

    if event.data is None or event.data.payload is None:
        return Response(status_code=200)

    event_type = event.data.event_type or ""
    call_payload = event.data.payload
    call_control_id = getattr(call_payload, "call_control_id", None)
    if not call_control_id:
        return Response(status_code=200)

    if "machine" in event_type and "detection" in event_type:
        result = getattr(call_payload, "result", "")
        if result == "machine":
            with get_conn(str(settings.db_file)) as conn:
                set_call_outcome(conn, call_control_id, "voicemail")
                call_row = get_call(conn, call_control_id)

            websocket = _active_relays.get(call_control_id)
            if websocket is not None and call_row is not None:
                try:
                    meds = load_medications(settings)
                    med = next((m for m in meds if m.med_key == call_row["med_key"]), None)
                    if med is not None:
                        await _send_voicemail_and_close(websocket, settings, med)
                except Exception:
                    logger.exception("Sheet unreadable while handling an AMD result")

    elif event_type == "call.hangup" and call_control_id not in _seen_relays:
        # A relay that connected finalizes the call itself when its websocket closes; this
        # only covers calls where one never connected, which is the case nothing else sees.
        _finish_unconnected_call(settings, call_control_id)

    return Response(status_code=200)
