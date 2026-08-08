# automaterx

A single-user SMS (and eventually voice) agent for prescription refills. It reads a Google
Sheet of your medications, texts you when a refill date is coming up, and uses Claude to
understand your free-text reply — "yeah go ahead," "snooze a week," "what dose is this
again?" — and act on it accordingly.

Eventually (M2) it will place an actual outbound phone call to your doctor's office to
request the refill and text you the outcome — the voice vendor for that is still undecided.
For now (M1) that step is stubbed: the agent confirms what it *would* do instead of dialing
anyone.

Not a product — built for one person's own prescriptions. See [PLAN.md](PLAN.md) for the
full design rationale (why identity data lives outside the Sheet, why calls aren't
auto-retried, etc.) and [CLAUDE.md](CLAUDE.md) for a code-level architecture map.

## How it works

1. **Nudge.** Once a day, an in-process scheduler reads the Google Sheet, figures out which
   medications are running low, and texts you a reminder — skipping any medication that
   already has an open conversation (awaiting reply, snoozed, or mid-call).
2. **Reply.** You text back in plain English. Claude classifies the intent — request a
   refill, snooze the reminder, ask a question, or "unclear" — using only the facts on that
   medication's Sheet row, and the app updates that medication's conversation state and
   replies.
3. **Refill (M2, not yet live).** On confirmation, the app will call the prescriber's office
   with a Claude-driven voice agent and text you what happened.

## Stack

- **Python 3.12 + FastAPI**, one always-on process — the inbound SMS webhook and the
  scheduler share it, since the planned voice component needs a persistent websocket anyway.
- **Google Sheets** (via `gspread`) as the medication list, hand-maintained.
- **Telnyx** for SMS today; voice vendor for M2 not yet picked.
- **Claude** (Anthropic API) for reply classification, gated to only the facts present on
  the medication record — no invented facts, no medical advice beyond that.
- **SQLite** for per-medication conversation state and a full message log.

## Project layout

```
app/
  main.py     FastAPI app, inbound SMS webhook, Telnyx webhook signature validation
  nudge.py    daily scheduler job — decides who needs a reminder
  reply.py    inbound reply handling — resolves the active thread, applies the classified intent
  intent.py   Claude classification of free-text replies into refill/snooze/question/unclear
  sheet.py    Google Sheet parsing (pure) + gspread client (loading)
  models.py   thread state machine + message log (SQLite)
  sms.py      Telnyx SMS sending
  db.py       SQLite connection setup
  config.py   Settings — the source of truth for required env vars
tests/        pytest suite; external services are monkeypatched, never hit live
PLAN.md       full design doc and milestone breakdown
```

## Getting started

```bash
uv sync                  # install dependencies
cp .env.example .env     # fill in Telnyx/Anthropic/Google Sheets credentials
uv run pytest -q         # run the test suite (no .env required)
uv run uvicorn app.main:app --reload   # run the dev server
```

See [.env.example](.env.example) for the full list of required configuration and what each
variable is for.

## Status

Currently at **M1**: the SMS loop — nudge, reply, classify, state update — is fully wired
end to end. Voice (M2) hasn't started.
