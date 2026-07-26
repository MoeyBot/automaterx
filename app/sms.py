from twilio.rest import Client

from app.config import Settings


def send_sms(settings: Settings, body: str, to: str | None = None) -> None:
    client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    client.messages.create(
        body=body,
        from_=settings.twilio_from_number,
        to=to or settings.owner_phone,
    )
