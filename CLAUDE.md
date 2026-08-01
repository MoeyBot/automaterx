# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-user SMS (and eventually voice) agent: it reads a Google Sheet of prescriptions,
texts the owner when a refill date is approaching, classifies their free-text reply with
Claude, and (in M2) will place an outbound call to request the refill — the voice vendor is
still TBD (see PLAN.md §5, §9 risk 5). See [PLAN.md](PLAN.md) for the full design, milestone
breakdown, and rationale behind key decisions (why identity data lives in env secrets and never
the Sheet, why calls aren't retried automatically, etc.) — read it before making architectural
changes.

Currently at M1: the SMS loop is fully wired end to end. The `request_refill` intent is
stubbed — it confirms what it *would* do instead of placing a real call — so voice (M2) can be
built on a proven decision path.

## Commands

```bash
uv sync                  # install/update dependencies (uv manages the venv automatically)
uv run pytest -q         # run the full test suite
uv run pytest tests/test_reply.py -q          # run one test file
uv run pytest tests/test_reply.py::test_snooze_updates_thread -q  # run one test
uv run uvicorn app.main:app --reload          # run the dev server (needs a filled-in .env)
uv run ruff check .      # lint
uv run ruff format .     # format
```

CI ([.github/workflows/ci.yml](.github/workflows/ci.yml)) runs `ruff check`, `ruff format --check`,
and the test suite on every push/PR to `main`. It installs with `uv sync --locked`, so commit
`uv.lock` alongside any dependency change. Tests never need `.env` — `tests/conftest.py`'s
`settings` fixture constructs `Settings` directly with explicit values, which is also why CI
doesn't need any secrets configured.

Config comes from environment variables (or a local `.env`, gitignored) — see
[.env.example](.env.example) for the full list and what each is for. `app/config.py`'s
`Settings` is the source of truth for what's required; it will fail fast at startup if a
required var is missing.

## Architecture

One FastAPI process (`app/main.py`) hosting both the inbound webhook and an in-process
APScheduler job — this is intentional (see PLAN.md §2): a later voice component needs a
persistent websocket, which rules out serverless, so there's no reason to split the scheduler
out either.

**Data flow for a nudge:**
`app/nudge.py: run_nudge_check` (cron, daily at `NUDGE_HOUR_LOCAL`) → `app/sheet.py:
load_medications` → for each `Medication` due, checks its `Thread` row in SQLite to avoid
re-nudging something already awaiting reply/snoozed/calling → sends SMS via `app/sms.py` →
`app/models.py: mark_nudged` flips the thread to `AWAITING_REPLY`.

**Data flow for a reply:**
Telnyx POSTs a `message.received` webhook to `/sms/inbound` (`app/main.py`) → the Ed25519
signature is verified against `TELNYX_PUBLIC_KEY` → sender must equal `OWNER_PHONE` exactly, or
the handler just returns 200 without sending anything back (no confirmation that the number is
live) → `app/reply.py: handle_inbound_reply` re-reads the Sheet fresh (not cached), resolves
`get_active_thread` (the thread `AWAITING_REPLY`, or else whatever thread was most recently
touched — so a bare question still resolves to the right medication) → `app/intent.py:
classify_reply` calls Claude with a forced tool call (`tool_choice`) to classify into
`request_refill` / `snooze` / `question` / `unclear` → the corresponding branch in
`handle_inbound_reply` mutates thread state and builds the reply text → `handle_inbound_reply`
returns plain text; `app/main.py` is the only place that sends it back out, via `app/sms.py:
send_sms` (Telnyx webhooks are fire-and-forget notifications, not request/response, so the
reply is always a separate outbound API call rather than something returned in the webhook
response). Every inbound and outbound body is logged to the `messages` table via `log_message`.

**Two things load fresh every request rather than being cached:** the Sheet (so an edit takes
effect on the next text, not the next deploy) and the thread lookup (so state changes from a
prior message are visible immediately). Don't introduce caching here without checking that
tradeoff against PLAN.md's reasoning.

**The Sheet boundary (`app/sheet.py`):** `parse_records` is a pure function from raw
`dict` rows to validated `Medication` objects, deliberately separated from the gspread client
(`load_medications`) so sheet-shape tests don't need live Google credentials — this is why
`tests/test_sheet.py` never touches `gspread`. A bad row anywhere in the sheet raises
`SheetValidationError` with *all* row errors collected (not just the first), and callers
(`nudge.py`, `reply.py`) catch it and text the owner about the problem rather than silently
skip the broken row or crash the scheduler.

**Thread state machine** (`app/models.py`, table `threads`, one row per `med_key`): `IDLE` →
`AWAITING_REPLY` → `SNOOZED` / `CALL_QUEUED` → ... — see PLAN.md §3 for the full diagram. Note
`med_key` is a slug of `med + dose` (`slugify`), not a Sheet row number, since row numbers shift
when the sheet is edited.

**Identity data lives only in env config, never the Sheet** (`Settings.patient_name`,
`patient_dob`, `pharmacy_*`) — the Sheet holds medication/scheduling facts only. This split is
deliberate (PLAN.md §6): the owner may share the Sheet or lose control of it without exposing
DOB alongside it.

**Claude's role is intentionally narrow and fact-gated:** `app/intent.py`'s system prompt
builds its context (`_build_context`) from *only* the fields on the `Medication` object, and
explicitly instructs the model not to invent facts or give medical advice beyond that context.
When extending what the model can say (e.g., in the M2 voice agent), preserve this pattern — a
closed fact sheet plus an explicit instruction to refuse rather than improvise — rather than
loosening it for convenience.

## Testing conventions

- `tests/conftest.py` provides a `settings` fixture (writes a fresh SQLite file per test via
  `tmp_path`) and a `one_med` fixture (one valid parsed `Medication`).
- External calls are never hit in tests: `app.sms.send_sms` (Telnyx), `app.intent.classify_reply`
  (Anthropic), and `app.sheet.load_medications` (gspread) are monkeypatched at the module level
  in the test file that needs them (see `tests/test_reply.py`, `tests/test_main.py`) rather than
  mocked via a fixture — follow that pattern for new tests in the same area.
- `app/main.py`'s dependencies (`get_settings`, `_verify_telnyx_webhook`, `handle_inbound_reply`,
  `send_sms`) are patched directly on `main_mod` for the same reason.
