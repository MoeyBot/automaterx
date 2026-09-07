import hashlib
import hmac
import logging
import re
import secrets
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo

from anthropic import Anthropic
from pydantic import BaseModel
from telnyx import Telnyx

from app.config import Settings
from app.models import Medication, count_calls_since, create_call, upsert_thread_state

logger = logging.getLogger(__name__)


class CallCapExceeded(Exception):
    """Raised when MAX_CALLS_PER_DAY has already been reached — a bug (or a chatty reply loop)
    must not be able to dial a prescriber's office an unbounded number of times in one day."""


MODEL = "claude-sonnet-5"

DISCLOSURE_GREETING = (
    "Hi, this is an automated assistant calling on behalf of {patient_name} regarding a prescription refill."
)

VOICEMAIL_SCRIPT = (
    "Hi, this is an automated assistant calling on behalf of {patient_name} regarding a "
    "prescription refill for {med} {dose}. Please call {callback} to process the refill "
    "request. Thank you."
)

CALL_SYSTEM_PROMPT = """You are an automated assistant placing a phone call on behalf of {patient_name} \
to request a prescription refill. You are talking to office staff at {prescriber}'s office, or \
navigating their phone system.

You already opened the call by identifying yourself as an automated assistant calling on \
{patient_name}'s behalf — never claim to be a human or the patient themselves.

You may state ONLY the facts listed below. If asked about anything else — other medications, \
symptoms, medical history, insurance, appointments, or anything not on this list — say you don't \
have that information and that {patient_name} will call back, and do not guess or improvise.

Facts you may state:
{fact_sheet}

Your goal: request a refill of {med} {dose} for {patient_name}, to be sent to {pharmacy_name} \
({pharmacy_phone}). If the office asks you to hold, wait quietly (respond with action "speak" \
and empty text, or a brief acknowledgement). If you reach an automated phone menu (an IVR), \
listen to the options and press the digit that leads toward refills or prescriptions using the \
press_digits action{phone_tree_hint_note}. If a human representative confirms the refill was \
sent to the pharmacy, thank them and end the call. If they say they can't process this over the \
phone, ask what {patient_name} needs to do instead, then end the call.

On every turn, call the call_action tool exactly once to decide what to do next. Never respond \
in plain text."""

CALL_ACTION_TOOL = {
    "name": "call_action",
    "description": "Decide the next thing to do on this phone call.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["speak", "press_digits", "end_call"]},
            "text": {
                "type": "string",
                "description": "What to say next aloud. Required when action == speak.",
            },
            "digits": {
                "type": "string",
                "description": (
                    "DTMF digits to send (0-9, w for a pause, #, *). Required when action == press_digits."
                ),
            },
        },
        "required": ["action"],
    },
}

SUMMARY_TOOL = {
    "name": "summarize_call",
    "description": "Summarize the outcome of a phone call that requested a prescription refill.",
    "input_schema": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["sent_to_pharmacy", "refused", "needs_appointment", "voicemail", "unclear"],
            },
            "detail": {
                "type": "string",
                "description": (
                    "One or two sentences on what happened, quoting the key line(s) "
                    "that determine the outcome."
                ),
            },
        },
        "required": ["outcome", "detail"],
    },
}

SUMMARY_SYSTEM_PROMPT = (
    "You summarize the outcome of an automated phone call that requested a prescription refill "
    "on the patient's behalf. Classify the outcome and quote the specific line(s) that support "
    "your classification. Call the summarize_call tool exactly once."
)


class CallAction(BaseModel):
    action: Literal["speak", "press_digits", "end_call"]
    text: str | None = None
    digits: str | None = None


class CallSummary(BaseModel):
    outcome: Literal["sent_to_pharmacy", "refused", "needs_appointment", "voicemail", "unclear"]
    detail: str


def build_fact_sheet(settings: Settings, med: Medication) -> str:
    lines = [
        f"- Patient: {settings.patient_name}, DOB {settings.patient_dob}, callback {settings.owner_phone}",
        f"- Medication: {med.med} {med.dose}, {med.qty} count, last filled {med.last_filled.isoformat()}",
        f"- Prescriber: {med.prescriber}",
        f"- Pharmacy: {settings.pharmacy_name}, {settings.pharmacy_address}, {settings.pharmacy_phone}",
    ]
    if med.notes:
        lines.append(f"- Notes: {med.notes}")
    return "\n".join(lines)


def _call_system_prompt(settings: Settings, med: Medication) -> str:
    hint_note = (
        f" — a known path for this office's menu is: {med.phone_tree_hint}" if med.phone_tree_hint else ""
    )
    return CALL_SYSTEM_PROMPT.format(
        patient_name=settings.patient_name,
        prescriber=med.prescriber,
        fact_sheet=build_fact_sheet(settings, med),
        med=med.med,
        dose=med.dose,
        pharmacy_name=settings.pharmacy_name,
        pharmacy_phone=settings.pharmacy_phone,
        phone_tree_hint_note=hint_note,
    )


_FILLER_WORDS = {"uh", "um", "umm", "mm", "mmm", "hm", "hmm", "ah", "er", "eh"}


def _normalize(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]", " ", text.lower()).split())


def is_usable_utterance(text: str, last_spoken: str | None) -> bool:
    """Whether a final `prompt` frame is real caller speech worth acting on.

    The ASR emits fragments (a live call produced bare `"to"` as its own final frame) and can
    pick up our own outbound audio. Every accepted utterance costs a model call and an awkward
    pause on a real office call, so these are worth dropping — but short answers ("yes",
    "speaking") are legitimate here, so this filters on content, never on length.

    Echo suppression only applies at 3+ words: a one-word "yes" may genuinely also appear
    inside our own last line, and dropping a real answer is worse than acting on an echo.
    """
    normalized = _normalize(text)
    if not normalized:
        return False
    words = normalized.split()
    if all(word in _FILLER_WORDS for word in words):
        return False
    if last_spoken and len(words) >= 3 and normalized in _normalize(last_spoken):
        return False
    return True


INTERRUPTION_MARKER = "[cut off here — the caller barged in and may not have heard the rest]"


def apply_interruption(turns: list[dict], delivered: str | None) -> None:
    """Records that the caller talked over our last line, so the transcript reflects what
    they plausibly heard rather than what we queued up to say.

    Without this the model reasons as though it delivered a full sentence the other party
    may have heard three words of — and then never repeats the information, because as far
    as it knows it already said it.

    `delivered` is Telnyx's `utteranceUntilInterrupt`, whose exact semantics are unconfirmed:
    on the 2026-08-08 call it carried the *complete* greeting alongside
    durationUntilInterruptMs=341, far too short to have spoken it, so it is either the
    delivered portion or the whole interrupted utterance. Telnyx's own example app only logs
    the event without reading the field. Rather than guess, keep whichever text is shorter —
    that is correct under either reading, since it never claims the caller heard more than
    they did.
    """
    last = next((t for t in reversed(turns) if t["role"] == "assistant"), None)
    if last is None or INTERRUPTION_MARKER in last["content"]:
        return
    spoken = last["content"]
    delivered = (delivered or "").strip()
    text = delivered if delivered and len(delivered) < len(spoken) else spoken
    last["content"] = f"{text} {INTERRUPTION_MARKER}"


def _format_turns(turns: list[dict]) -> str:
    return "\n".join(f"{'You' if t['role'] == 'assistant' else 'Them'}: {t['content']}" for t in turns)


def next_call_action(settings: Settings, med: Medication, turns: list[dict], caller_text: str) -> CallAction:
    """Decides what the agent should say/do next, given what the caller/IVR just said.

    `turns` is the call so far as [{"role": "assistant"|"user", "content": str}, ...]. Mirrors
    app/intent.py's classify_reply pattern: one independent forced-tool-call per turn with the
    running history serialized as text, rather than a chained multi-turn tool-use conversation.
    """
    client = Anthropic(api_key=settings.anthropic_api_key)
    context = _format_turns(turns) if turns else "This is the start of the call."
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=_call_system_prompt(settings, med),
        tools=[CALL_ACTION_TOOL],
        tool_choice={"type": "tool", "name": "call_action"},
        messages=[{"role": "user", "content": f"Call so far:\n{context}\n\nThey just said: {caller_text}"}],
    )
    tool_use = next(b for b in resp.content if b.type == "tool_use")
    return CallAction(**tool_use.input)


def summarize_call(settings: Settings, med: Medication, transcript: list[dict]) -> CallSummary:
    client = Anthropic(api_key=settings.anthropic_api_key)
    transcript_text = (
        _format_turns(transcript) if transcript else "(no conversation — call did not connect to a person)"
    )
    resp = client.messages.create(
        model=MODEL,
        max_tokens=512,
        system=SUMMARY_SYSTEM_PROMPT,
        tools=[SUMMARY_TOOL],
        tool_choice={"type": "tool", "name": "summarize_call"},
        messages=[
            {
                "role": "user",
                "content": f"Medication: {med.med} {med.dose}\n\nTranscript:\n{transcript_text}",
            }
        ],
    )
    tool_use = next(b for b in resp.content if b.type == "tool_use")
    return CallSummary(**tool_use.input)


def sign_relay_token(secret: str, rid: str, med_key: str) -> str:
    message = f"{rid}.{med_key}".encode()
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_relay_token(secret: str, rid: str, med_key: str, token: str) -> bool:
    return hmac.compare_digest(sign_relay_token(secret, rid, med_key), token)


def build_relay_url(base_url: str, secret: str, rid: str, med_key: str) -> str:
    wss_base = base_url.replace("https://", "wss://").replace("http://", "ws://")
    token = sign_relay_token(secret, rid, med_key)
    return f"{wss_base}/voice/relay?rid={rid}&med_key={med_key}&token={token}"


def _start_of_today_utc(settings: Settings) -> datetime:
    tz = ZoneInfo(settings.timezone)
    local_midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    return local_midnight.astimezone(UTC).replace(tzinfo=None)


def place_refill_call(settings: Settings, med: Medication, base_url: str, conn) -> str:
    """Places the outbound refill-request call. Returns the Telnyx call_control_id.

    Takes an open `conn` rather than opening its own — callers (e.g. app/reply.py) already
    hold a connection mid-transaction when this fires, and SQLite only allows one writer at a
    time, so a second connection here would deadlock against the caller's uncommitted writes.
    """
    placed_today = count_calls_since(conn, _start_of_today_utc(settings))
    if placed_today >= settings.max_calls_per_day:
        raise CallCapExceeded(
            f"{placed_today} calls already placed today (limit {settings.max_calls_per_day})"
        )

    rid = secrets.token_hex(16)
    relay_url = build_relay_url(base_url, settings.relay_signing_secret, rid, med.med_key)
    greeting = DISCLOSURE_GREETING.format(patient_name=settings.patient_name)

    client = Telnyx(api_key=settings.telnyx_api_key)
    resp = client.calls.dial(
        connection_id=settings.telnyx_connection_id,
        from_=settings.telnyx_from_number,
        to=med.prescriber_phone,
        answering_machine_detection="premium",
        webhook_url=f"{base_url}/voice/status",
        conversation_relay_config={
            "url": relay_url,
            "dtmf_detection": True,
            "greeting": greeting,
            # NOTE: do not add "interruptible_greeting": "none" here. The SDK's generated
            # ConversationRelayEmbeddedConfigParam documents it (enum none/any/speech/dtmf),
            # but sending it on a real dial breaks the call: Telnyx returns 200, then
            # Conversation Relay never starts — the 2026-08-08 08:12 call leg has no
            # call.conversation.created / stream_start / playback_start at all, just answer,
            # AMD, then 20s of silence and a hangup. The generated types are ahead of (or
            # disagree with) the deployed API here. The disclosure is therefore still
            # interruptible — an open safety gap, see PLAN.md §5.
        },
    )
    call_control_id = resp.data.call_control_id

    create_call(conn, med.med_key, call_control_id)
    upsert_thread_state(conn, med.med_key, "CALLING")

    logger.info("Placed refill call for %s: call_control_id=%s", med.med_key, call_control_id)
    return call_control_id
