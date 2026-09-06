[README](README.md) · [Architecture](ARCHITECTURE.md) · [Workflow](WORKFLOW.md) · [Plan](PLAN.md) · [CLAUDE.md](CLAUDE.md)

# automaterx

A single-user SMS and voice agent for prescription refills. It reads a Google Sheet of your
medications, texts you when a refill date is coming up, keeps following up if you go quiet,
and uses Claude to understand your free-text reply — "yeah go ahead," "snooze a week," "just
filled it," "what dose is this again?" — and act on it accordingly. On your go-ahead, it
places a real outbound call to your doctor's office and texts you what happened.

Not a product — built for one person's own prescriptions. See [PLAN.md](PLAN.md) for the full
design rationale (why identity data lives outside the Sheet, why calls aren't auto-retried,
etc.), [ARCHITECTURE.md](ARCHITECTURE.md) for the component/data map, and
[WORKFLOW.md](WORKFLOW.md) for step-by-step walkthroughs of each flow.

## How it works

1. **Nudge.** Once a day, an in-process scheduler reads the Google Sheet, figures out which
   medications are running low, and texts you a reminder — skipping any medication that
   already has an open conversation (awaiting reply, snoozed, or mid-call).
2. **Follow up.** If you don't reply, a second scheduler job checks in every 30 minutes and
   sends a randomized follow-up text within a daily window, until either you reply or a
   day-cap is hit and it sends one final message and goes quiet.
3. **Reply.** You text back in plain English. Claude classifies the intent — request a
   refill, snooze the reminder, tell it the medication's already filled, ask a question, or
   "unclear" — using only the facts on that medication's Sheet row, and the app updates that
   medication's conversation state and replies. A "just filled it" reply always gets confirmed
   before anything is written back to the Sheet.
4. **Refill.** On confirmation, the app calls the prescriber's office with a Claude-driven
   voice agent (Telnyx Conversation Relay) and texts you the outcome.

## Stack

- **Python 3.12 + FastAPI**, one always-on process on Fly.io — the inbound SMS/voice webhooks
  and the scheduler share it, since the voice component needs a persistent websocket.
- **Google Sheets** (via `gspread`) as the medication list, hand-maintained.
- **Telnyx** for both SMS and voice (Conversation Relay).
- **Claude** (Anthropic API) for reply classification and in-call turns, gated to only the
  facts present on the medication record — no invented facts, no medical advice beyond that.
- **SQLite** for per-medication conversation state and a full message log.

## Project layout

```
app/
  main.py      FastAPI app: SMS webhook, voice websocket + status webhook, scheduler wiring
  nudge.py     daily scheduler job — decides who needs a reminder
  followup.py  periodic scheduler job — follow-up texts for nudges that went unanswered
  reply.py     inbound reply handling — resolves the medication, applies the classified intent
  intent.py    Claude classification: reply intent + fill-date confirmation intent
  voice.py     M2 voice: call placement, in-call turns, transcript summarization
  sheet.py     Google Sheet parsing (pure) + gspread client (loading, write-back)
  models.py    thread state machine + message log (SQLite)
  sms.py       Telnyx SMS sending
  db.py        SQLite schema + migrations
  config.py    Settings — the source of truth for required env vars
tests/         pytest suite; external services are monkeypatched, never hit live
Dockerfile, fly.toml   Fly.io deployment
PLAN.md          full design doc and milestone breakdown
ARCHITECTURE.md  component map, data model, security model
WORKFLOW.md      step-by-step walkthroughs of each flow
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

M1 (the SMS loop, including follow-ups and text-based fill confirmation) is complete and
proven end to end. M2 (voice) is implemented — one real call to a controlled number has
confirmed the dial → relay → token path — but the phone-tree and voicemail paths still need a
live test before it's trusted against a real prescriber's office. See CLAUDE.md's "What this
is" section for the current line on exactly what's proven versus still stubbed/unverified.
