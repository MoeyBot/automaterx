from datetime import date

import gspread
from google.oauth2.service_account import Credentials

from app.config import Settings
from app.models import Medication, slugify

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

REQUIRED_COLUMNS = {
    "med",
    "dose",
    "qty",
    "days_supply",
    "last_filled",
    "prescriber",
    "prescriber_phone",
}


class SheetValidationError(Exception):
    def __init__(self, row_errors: list[str]):
        self.row_errors = row_errors
        super().__init__("; ".join(row_errors))


def _row_to_medication(row: dict) -> Medication:
    return Medication(
        med=str(row["med"]).strip(),
        dose=str(row["dose"]).strip(),
        qty=int(row["qty"]),
        days_supply=int(row["days_supply"]),
        last_filled=row["last_filled"],
        prescriber=str(row["prescriber"]).strip(),
        prescriber_phone=str(row["prescriber_phone"]).strip(),
        phone_tree_hint=str(row.get("phone_tree_hint", "") or ""),
        lead_days=int(row["lead_days"]) if row.get("lead_days") not in ("", None) else 7,
        status=str(row.get("status") or "active"),
        notes=str(row.get("notes", "") or ""),
    )


def parse_records(records: list[dict]) -> list[Medication]:
    """Pure transform from raw sheet rows to validated Medications.

    Kept separate from the gspread client so it can be unit tested without
    live Google credentials.
    """
    meds: list[Medication] = []
    errors: list[str] = []
    for i, row in enumerate(records, start=2):  # row 1 is the header
        missing = REQUIRED_COLUMNS - row.keys()
        if missing:
            errors.append(f"row {i}: missing column(s) {sorted(missing)}")
            continue
        try:
            meds.append(_row_to_medication(row))
        except Exception as e:
            errors.append(f"row {i} ({row.get('med', '?')}): {e}")
    if errors:
        raise SheetValidationError(errors)
    return meds


def _worksheet(settings: Settings):
    creds = Credentials.from_service_account_file(settings.google_service_account_json, scopes=SCOPES)
    gc = gspread.authorize(creds)
    return gc.open_by_key(settings.google_sheet_id).sheet1


def load_medications(settings: Settings) -> list[Medication]:
    ws = _worksheet(settings)
    # numericise_ignore=["all"]: gspread otherwise auto-converts anything that looks numeric,
    # including phone numbers like "+17082978600" — int("+17082978600") succeeds and silently
    # drops the leading "+". Every field here is cast explicitly in _row_to_medication anyway.
    return parse_records(ws.get_all_records(numericise_ignore=["all"]))


def mark_filled(settings: Settings, med_key: str, filled_on: date) -> None:
    ws = _worksheet(settings)
    records = ws.get_all_records(numericise_ignore=["all"])
    headers = ws.row_values(1)
    col = headers.index("last_filled") + 1
    for i, row in enumerate(records, start=2):
        if slugify(str(row["med"]), str(row["dose"])) == med_key:
            ws.update_cell(i, col, filled_on.isoformat())
            return
    raise ValueError(f"medication {med_key!r} not found in sheet for write-back")
