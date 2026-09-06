import logging
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Settings
from app.db import get_conn
from app.models import (
    get_threads_due_for_followup,
    log_message,
    mark_followup_final_sent,
    set_next_followup_at,
)
from app.sheet import SheetValidationError, load_medications
from app.sms import send_sms

logger = logging.getLogger(__name__)

FOLLOWUP_TEMPLATES = [
    "Just checking back in — still want me to look into {med} {dose}?",
    "Following up on {med} {dose} — no rush, just say the word whenever.",
    "Circling back about {med} {dose} in case this got buried.",
]

FINAL_MESSAGE = (
    "Haven't heard back about {med} {dose} — I'll stop checking in on this one for now. "
    "Text me whenever you're ready."
)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _clamp_to_window(settings: Settings, naive_utc: datetime) -> datetime:
    """Snaps a candidate time into the local send window: pulled forward to that day's window
    start if it's too early, pushed to the next day's window start if it's at/after window end."""
    tz = ZoneInfo(settings.timezone)
    local = naive_utc.replace(tzinfo=UTC).astimezone(tz)
    window_start = local.replace(hour=settings.followup_window_start_hour, minute=0, second=0, microsecond=0)
    window_end = local.replace(hour=settings.followup_window_end_hour, minute=0, second=0, microsecond=0)

    if local < window_start:
        local = window_start
    elif local >= window_end:
        local = window_start + timedelta(days=1)

    return local.astimezone(UTC).replace(tzinfo=None)


def _within_window(settings: Settings, naive_utc: datetime) -> bool:
    tz = ZoneInfo(settings.timezone)
    local = naive_utc.replace(tzinfo=UTC).astimezone(tz)
    return settings.followup_window_start_hour <= local.hour < settings.followup_window_end_hour


def next_followup_time(settings: Settings, after: datetime) -> datetime:
    offset_hours = random.uniform(settings.followup_min_hours, settings.followup_max_hours)
    return _clamp_to_window(settings, after + timedelta(hours=offset_hours))


def run_followup_check(settings: Settings) -> list[str]:
    """Sends follow-up texts for AWAITING_REPLY threads whose next follow-up is due.

    Returns the list of med_keys that got a follow-up sent, mainly for tests/manual runs.
    """
    try:
        meds = load_medications(settings)
    except SheetValidationError as e:
        logger.error("Sheet validation failed, skipping follow-up check: %s", e.row_errors)
        return []

    med_by_key = {med.med_key: med for med in meds}
    now = now_utc()
    followed_up: list[str] = []

    with get_conn(str(settings.db_file)) as conn:
        for thread in get_threads_due_for_followup(conn, now):
            med = med_by_key.get(thread.med_key)
            if med is None:
                # Row renamed/removed from the Sheet since the original nudge — nothing to
                # follow up about.
                continue

            anchor = thread.first_nudged_at or thread.last_nudged_at or now
            if now - anchor >= timedelta(days=settings.followup_cap_days):
                body = FINAL_MESSAGE.format(med=med.med, dose=med.dose)
                send_sms(settings, body)
                log_message(conn, med.med_key, "out", body)
                mark_followup_final_sent(conn, med.med_key)
                continue

            if not _within_window(settings, now):
                # The checker runs on its own cadence and can land just past window end even
                # though next_followup_at was clamped into the window when it was set.
                set_next_followup_at(conn, med.med_key, _clamp_to_window(settings, now))
                continue

            body = random.choice(FOLLOWUP_TEMPLATES).format(med=med.med, dose=med.dose)
            send_sms(settings, body)
            log_message(conn, med.med_key, "out", body)
            set_next_followup_at(conn, med.med_key, next_followup_time(settings, now))
            followed_up.append(med.med_key)

    return followed_up
