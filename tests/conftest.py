from datetime import date

import pytest

from app.config import Settings
from app.db import init_db
from app.sheet import parse_records

GOOD_ROW = {
    "med": "Lisinopril",
    "dose": "10mg",
    "qty": 90,
    "days_supply": 90,
    "last_filled": "2026-06-02",
    "prescriber": "Dr. Alicia Reyes",
    "prescriber_phone": "+15125551234",
    "phone_tree_hint": "",
    "lead_days": 7,
    "status": "active",
    "notes": "",
}


@pytest.fixture
def settings(tmp_path):
    db_path = tmp_path / "test.db"
    init_db(str(db_path))
    return Settings(
        owner_phone="+15125550000",
        patient_name="Test Patient",
        patient_dob="1990-01-01",
        pharmacy_name="Test Pharmacy",
        pharmacy_phone="+15125559999",
        twilio_account_sid="ACxxxx",
        twilio_auth_token="xxxx",
        twilio_from_number="+15125550001",
        anthropic_api_key="sk-xxxx",
        google_service_account_json="",
        google_sheet_id="",
        db_path=str(db_path),
        timezone="America/Chicago",
    )


@pytest.fixture
def one_med():
    return parse_records([GOOD_ROW])[0]
