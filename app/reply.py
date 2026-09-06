import logging

from app.config import Settings
from app.db import get_conn
from app.followup import next_followup_time, now_utc
from app.intent import classify_fill_confirmation, classify_reply
from app.models import (
    Medication,
    Thread,
    get_active_thread,
    get_thread,
    log_message,
    recent_messages,
    resolve_pending_fill,
    set_next_followup_at,
    set_pending_fill,
    set_snooze,
)
from app.nudge import local_today
from app.sheet import SheetValidationError, load_medications, mark_filled
from app.voice import place_refill_call

logger = logging.getLogger(__name__)

NO_ACTIVE_THREAD_REPLY = (
    "I don't have an open refill reminder to match that to yet. I'll text you when one comes up."
)


def _match_med_by_name(meds: list[Medication], body: str) -> Medication | None:
    """Resolves a medication named directly in the text, so an unprompted message ("just
    filled the lisinopril") doesn't need an open thread to land on the right one. Ambiguous
    matches (none, or more than one) are left to the caller's existing thread-based fallback."""
    lowered = body.lower()
    matches = [m for m in meds if m.med.lower() in lowered]
    return matches[0] if len(matches) == 1 else None


def _resolve_thread(conn, meds: list[Medication], body: str) -> Thread | None:
    named = _match_med_by_name(meds, body)
    if named is not None:
        return get_thread(conn, named.med_key) or Thread(med_key=named.med_key, state="IDLE")
    return get_active_thread(conn)


def handle_inbound_reply(settings: Settings, body: str, base_url: str) -> str:
    """Classifies an inbound SMS reply and takes the corresponding action.

    Returns the text sent back to the user (the caller is responsible for actually
    sending it — kept separate so this stays easy to unit test).
    """
    try:
        meds = load_medications(settings)
    except SheetValidationError:
        return (
            "Your med sheet has a problem right now — I'll flag it, "
            "but can't look anything up until it's fixed."
        )

    with get_conn(str(settings.db_file)) as conn:
        log_message(conn, None, "in", body)

        thread = _resolve_thread(conn, meds, body)
        if thread is None:
            log_message(conn, None, "out", NO_ACTIVE_THREAD_REPLY)
            return NO_ACTIVE_THREAD_REPLY

        med = next((m for m in meds if m.med_key == thread.med_key), None)
        if med is None:
            reply = f"I lost track of that medication in the sheet ({thread.med_key}) — could you check it?"
            log_message(conn, thread.med_key, "out", reply)
            return reply

        history = recent_messages(conn, med.med_key, limit=10)
        today = local_today(settings)

        if thread.state == "AWAITING_FILL_CONFIRMATION":
            reply = _handle_fill_confirmation(settings, conn, med, thread, history, body, today)
        else:
            reply = _handle_decision_reply(settings, conn, med, thread, history, body, today, base_url)

        log_message(conn, med.med_key, "out", reply)
        return reply


def _handle_decision_reply(
    settings: Settings,
    conn,
    med: Medication,
    thread: Thread,
    history: list[dict],
    body: str,
    today,
    base_url: str,
) -> str:
    intent = classify_reply(settings, med, history, body, today)

    if intent.kind == "request_refill":
        try:
            place_refill_call(settings, med, base_url, conn)
            reply = (
                f"Got it — calling {med.prescriber} now to request a refill for "
                f"{med.med} {med.dose}. I'll text you as soon as I know how it went."
            )
        except Exception:
            logger.exception("Failed to place refill call for %s", med.med_key)
            reply = (
                f"I tried to call {med.prescriber} but hit a problem placing the call — "
                "I'll flag this so it can be looked into."
            )
    elif intent.kind == "snooze":
        if intent.snooze_until is None:
            reply = "Sure — when should I check back in?"
        else:
            set_snooze(conn, med.med_key, intent.snooze_until)
            reply = f"Okay, I'll hold off on {med.med} until {intent.snooze_until.isoformat()}."
    elif intent.kind == "mark_filled":
        proposed = intent.filled_on or today
        set_pending_fill(conn, med.med_key, proposed)
        reply = (
            f"Got it — mark {med.med} {med.dose} as filled on {proposed.isoformat()}? "
            "Reply yes to confirm, or tell me the right date."
        )
    elif intent.kind == "question":
        reply = intent.answer or "I don't have enough information to answer that."
    else:  # unclear
        reply = intent.answer or "Sorry, I didn't follow that — could you rephrase?"

    if intent.kind in ("question", "unclear") and thread.state == "AWAITING_REPLY":
        # The owner engaged without resolving the refill decision — push the follow-up
        # clock out from now rather than treating this like continued silence.
        set_next_followup_at(conn, med.med_key, next_followup_time(settings, now_utc()))

    return reply


def _handle_fill_confirmation(
    settings: Settings, conn, med: Medication, thread: Thread, history: list[dict], body: str, today
) -> str:
    proposed_date = thread.pending_fill_date
    confirmation = classify_fill_confirmation(settings, med, proposed_date, history, body, today)

    if confirmation.kind == "confirmed":
        try:
            mark_filled(settings, med.med_key, proposed_date)
            resolve_pending_fill(conn, med.med_key, "IDLE")
            reply = f"Marked {med.med} {med.dose} as filled on {proposed_date.isoformat()}."
        except Exception:
            logger.exception("Failed to write last_filled back to the sheet for %s", med.med_key)
            reply = "I tried to update the sheet but hit a problem — I'll flag this so it can be looked into."
    elif confirmation.kind == "corrected":
        if confirmation.corrected_date is None:
            reply = "What date should I use?"
        else:
            set_pending_fill(conn, med.med_key, confirmation.corrected_date)
            reply = (
                f"Got it — mark {med.med} {med.dose} as filled on "
                f"{confirmation.corrected_date.isoformat()}? Reply yes to confirm."
            )
    elif confirmation.kind == "cancelled":
        resolve_pending_fill(conn, med.med_key, "IDLE")
        reply = "Okay, I won't update it."
    else:  # unclear
        reply = confirmation.answer or (
            f"Should I mark {med.med} {med.dose} as filled on {proposed_date.isoformat()}? "
            "Reply yes or give me the right date."
        )

    return reply
