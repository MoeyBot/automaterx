from datetime import date

import pytest

from app.sheet import SheetValidationError, parse_records
from tests.conftest import GOOD_ROW


def test_parse_valid_row():
    meds = parse_records([GOOD_ROW])
    assert len(meds) == 1
    med = meds[0]
    assert med.med_key == "lisinopril_10mg"
    assert med.runs_out == date(2026, 8, 31)
    assert med.nudge_date == date(2026, 8, 24)


def test_is_due():
    med = parse_records([GOOD_ROW])[0]
    assert not med.is_due(date(2026, 8, 1))
    assert med.is_due(date(2026, 8, 24))
    assert med.is_due(date(2026, 9, 1))


def test_paused_never_due():
    row = {**GOOD_ROW, "status": "paused"}
    med = parse_records([row])[0]
    assert not med.is_due(date(2030, 1, 1))


def test_missing_column_raises_with_row_number():
    bad = dict(GOOD_ROW)
    del bad["prescriber_phone"]
    with pytest.raises(SheetValidationError) as exc:
        parse_records([bad])
    assert "row 2" in str(exc.value)


def test_bad_status_value_raises():
    row = {**GOOD_ROW, "status": "on vacation"}
    with pytest.raises(SheetValidationError):
        parse_records([row])


def test_default_lead_days_when_blank():
    row = {**GOOD_ROW, "lead_days": ""}
    med = parse_records([row])[0]
    assert med.lead_days == 7
