import logging
from datetime import date
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import get_conn
from app.models import get_thread, mark_nudged, log_message
from app.sheet import load_medications, SheetValidationError
from app.sms import send_sms

logger = logging.getLogger(__name__)


def local_today(settings: Settings) -> date:
    return date.today() if not settings.timezone else _now_in_tz(settings.timezone).date()


def _now_in_tz(tz_name: str):
    from datetime import datetime

    return datetime.now(ZoneInfo(tz_name))


def run_nudge_check(settings: Settings) -> list[str]:
    """Checks every active medication against its nudge date and texts any that are due.

    Returns the list of med_keys nudged, mainly for tests/manual runs.
    """
    try:
        meds = load_medications(settings)
    except SheetValidationError as e:
        logger.error("Sheet validation failed, skipping nudge check: %s", e.row_errors)
        send_sms(settings, f"⚠️ Your med sheet has bad rows, fix and I'll retry tomorrow:\n{'; '.join(e.row_errors)}")
        return []

    today = local_today(settings)
    nudged: list[str] = []

    with get_conn(str(settings.db_file)) as conn:
        for med in meds:
            thread = get_thread(conn, med.med_key)

            if thread is not None and thread.state == "SNOOZED" and thread.snooze_until and today < thread.snooze_until:
                continue
            if thread is not None and thread.state == "AWAITING_REPLY":
                continue  # already nudged, waiting on a reply
            if thread is not None and thread.state == "CALLING":
                continue

            if not med.is_due(today):
                continue

            body = (
                f"{med.med} {med.dose} runs out around {med.runs_out.isoformat()}. "
                f"Want me to call {med.prescriber} for a refill? Reply or ask a question anytime."
            )
            send_sms(settings, body)
            log_message(conn, med.med_key, "out", body)
            mark_nudged(conn, med.med_key)
            nudged.append(med.med_key)

    return nudged
