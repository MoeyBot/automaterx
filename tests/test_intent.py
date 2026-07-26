from datetime import date

from app.intent import _build_context
from app.sheet import parse_records
from tests.conftest import GOOD_ROW


def test_build_context_includes_facts_and_history():
    med = parse_records([GOOD_ROW])[0]
    history = [{"direction": "out", "body": "Nudge text"}, {"direction": "in", "body": "not yet"}]
    ctx = _build_context(med, history, today=date(2026, 8, 20))

    assert "2026-08-20" in ctx
    assert "Lisinopril 10mg" in ctx
    assert "Runs out: 2026-08-31" in ctx
    assert "Dr. Alicia Reyes" in ctx
    assert "System: Nudge text" in ctx
    assert "User: not yet" in ctx


def test_build_context_without_history_omits_section():
    med = parse_records([GOOD_ROW])[0]
    ctx = _build_context(med, [], today=date(2026, 8, 20))
    assert "message history" not in ctx
