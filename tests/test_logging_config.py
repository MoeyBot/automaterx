import json
import logging

from app.logging_config import JsonFormatter


def _format(record: logging.LogRecord) -> dict:
    return json.loads(JsonFormatter().format(record))


def _make_record(msg: str, *args, level=logging.INFO, exc_info=None, extra=None) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=args,
        exc_info=exc_info,
    )
    for key, value in (extra or {}).items():
        setattr(record, key, value)
    return record


def test_basic_fields_present():
    record = _make_record("hello %s", "world")
    payload = _format(record)
    assert payload["message"] == "hello world"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert "timestamp" in payload


def test_extra_fields_are_included():
    record = _make_record("placed call", extra={"med_key": "lisinopril_10mg", "call_control_id": "ccid123"})
    payload = _format(record)
    assert payload["med_key"] == "lisinopril_10mg"
    assert payload["call_control_id"] == "ccid123"


def test_reserved_attributes_are_not_leaked_as_extra_fields():
    record = _make_record("hello")
    payload = _format(record)
    # These are LogRecord internals, not caller-supplied structured fields.
    assert "msg" not in payload
    assert "args" not in payload
    assert "pathname" not in payload
    assert "lineno" not in payload


def test_exception_info_is_included():
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        record = _make_record("failed", exc_info=sys.exc_info())
    payload = _format(record)
    assert "ValueError: boom" in payload["exc_info"]


def test_output_is_a_single_json_line():
    record = _make_record("no newlines here")
    rendered = JsonFormatter().format(record)
    assert "\n" not in rendered
    json.loads(rendered)  # must parse as a single JSON object
