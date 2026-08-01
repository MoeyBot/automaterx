import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response
from telnyx import Telnyx
from telnyx.lib.webhooks_ed25519 import WebhookVerificationError, unwrap_with_ed25519

from app.config import Settings, get_settings
from app.db import init_db
from app.nudge import run_nudge_check
from app.reply import handle_inbound_reply
from app.sms import send_sms

logging.basicConfig(level=logging.INFO)
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
    scheduler.start()
    logger.info(
        "Scheduler started, daily nudge check at %02d:00 %s",
        settings.nudge_hour_local,
        settings.timezone,
    )

    yield

    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)


def _verify_telnyx_webhook(settings: Settings, payload: bytes, headers):
    client = Telnyx(api_key=settings.telnyx_api_key, public_key=settings.telnyx_public_key)
    return unwrap_with_ed25519(client, payload, headers)


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

    reply_text = handle_inbound_reply(settings, body)
    send_sms(settings, reply_text)
    return Response(status_code=200)
