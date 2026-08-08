from telnyx import Telnyx

from app.config import Settings


def send_sms(settings: Settings, body: str, to: str | None = None) -> None:
    client = Telnyx(api_key=settings.telnyx_api_key)
    client.messages.send(
        text=body,
        from_=settings.telnyx_from_number,
        to=to or settings.owner_phone,
    )
