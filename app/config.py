from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Identity — never stored in the Google Sheet.
    owner_phone: str  # E.164, e.g. +15125551234 — the only number allowed to trigger actions
    patient_name: str
    patient_dob: str  # ISO date, e.g. 1985-03-14

    pharmacy_name: str
    pharmacy_phone: str
    pharmacy_address: str = ""

    twilio_account_sid: str
    twilio_auth_token: str
    twilio_from_number: str  # the app's Twilio number, E.164

    anthropic_api_key: str

    google_service_account_json: str = ""  # path to the service account key file
    google_sheet_id: str = ""

    timezone: str = "America/Chicago"
    nudge_hour_local: int = 9  # 24h, local to `timezone`

    db_path: str = "automaterx.db"

    max_calls_per_day: int = 5

    @property
    def db_file(self) -> Path:
        return Path(self.db_path)


@lru_cache
def get_settings() -> Settings:
    return Settings()
