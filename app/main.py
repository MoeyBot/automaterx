import logging
from contextlib import asynccontextmanager

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import FastAPI, Request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse

from app.config import get_settings
from app.db import init_db
from app.nudge import run_nudge_check
from app.reply import handle_inbound_reply

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


def _request_url(request: Request) -> str:
    """Reconstructs the externally-visible URL for Twilio signature validation.

    Fly.io (and most PaaS) terminate TLS at the edge and forward plain HTTP to the
    container, so request.url reports "http://" even though Twilio signed "https://".
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    return str(request.url.replace(scheme=proto))


async def _validate_twilio_request(request: Request, form: dict) -> bool:
    settings = get_settings()
    validator = RequestValidator(settings.twilio_auth_token)
    signature = request.headers.get("X-Twilio-Signature", "")
    return validator.validate(_request_url(request), form, signature)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/sms/inbound")
async def sms_inbound(request: Request):
    form = dict(await request.form())

    if not await _validate_twilio_request(request, form):
        logger.warning("Rejected inbound SMS webhook with invalid Twilio signature")
        return Response(status_code=403)

    settings = get_settings()
    from_number = form.get("From", "")
    body = form.get("Body", "")

    twiml = MessagingResponse()
    if from_number != settings.owner_phone:
        logger.warning("Rejected SMS from non-owner number %s", from_number)
        # Deliberately silent: no TwiML <Message>, so an unknown sender gets no reply
        # confirming this number is live and hooked up to anything.
        return Response(content=str(twiml), media_type="application/xml")

    reply_text = handle_inbound_reply(settings, body)
    twiml.message(reply_text)
    return Response(content=str(twiml), media_type="application/xml")
