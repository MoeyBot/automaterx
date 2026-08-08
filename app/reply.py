import logging

from app.config import Settings
from app.db import get_conn
from app.intent import classify_reply
from app.models import get_active_thread, log_message, recent_messages, set_snooze
from app.nudge import local_today
from app.sheet import SheetValidationError, load_medications
from app.voice import place_refill_call

logger = logging.getLogger(__name__)

NO_ACTIVE_THREAD_REPLY = (
    "I don't have an open refill reminder to match that to yet. I'll text you when one comes up."
)


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

        thread = get_active_thread(conn)
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
        elif intent.kind == "question":
            reply = intent.answer or "I don't have enough information to answer that."
        else:  # unclear
            reply = intent.answer or "Sorry, I didn't follow that — could you rephrase?"

        log_message(conn, med.med_key, "out", reply)
        return reply
