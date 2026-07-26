# automaterx — Project Plan

An SMS agent that watches your prescription refill dates, texts you before you run out,
lets you answer in plain English, and — on your go-ahead — places a real phone call to
your doctor's office to request the refill, then texts you what happened.

Single user (you). Not a product, not multi-tenant, no HIPAA covered-entity obligations —
but it handles your own PHI, so it's built like it matters.

---

## 1. Decisions made

| Area | Decision |
|---|---|
| Rx source of truth | A Google Sheet you maintain by hand |
| Notification channel | Twilio Programmable Messaging (SMS) |
| Reply handling | Claude parses free-text intent: **refill**, **snooze**, **question** |
| Doctor contact | Real outbound voice call with an AI agent |
| Voice stack | Twilio **ConversationRelay** (Twilio owns STT/TTS/barge-in; Claude owns what to say) |
| Runtime | Python 3.12 + FastAPI, one always-on container on Fly.io |
| Identity data | Name / DOB / pharmacy in encrypted app secrets — **never** in the Sheet |
| Call failures | One attempt. Voicemail fallback. Always text you the outcome + transcript. |
| Sequencing | M1 = SMS loop end to end. M2 = voice. |

**Model choice:** Claude Sonnet 5 (`claude-sonnet-5`) for both the SMS intent parsing and the
in-call turns. On a live call, time-to-first-token is the thing you feel — Sonnet is the right
point on that curve, and neither task is reasoning-hard.

---

## 2. Architecture

```
                    ┌──────────────────┐
   Google Sheet ───▶│  APScheduler     │  daily 09:00 America/<your TZ>
   (meds, dates)    │  refill check    │
                    └────────┬─────────┘
                             │ due soon?
                             ▼
                    ┌──────────────────┐        ┌─────────────┐
                    │  FastAPI app     │───────▶│   Twilio    │──▶ your phone (SMS)
                    │  (Fly.io)        │◀───────│  Messaging  │◀── your reply
                    │                  │        └─────────────┘
                    │  SQLite (volume) │
                    │  - thread state  │        ┌─────────────┐
                    │  - call records  │───────▶│ Twilio Voice│──▶ doctor's office
                    │  - msg log       │◀ ws ──▶│ Conversation│
                    └────────┬─────────┘        │   Relay     │
                             │                  └─────────────┘
                             ▼
                      Claude Sonnet 5
                   (intent parse · call turns · outcome summary)
```

One process. The websocket for live call audio is why this is a container and not a Lambda.

### Endpoints

| Route | Purpose |
|---|---|
| `POST /sms/inbound` | Twilio inbound SMS webhook |
| `POST /voice/twiml` | Returns `<Connect><ConversationRelay url="wss://…">` for the outbound call |
| `WS /voice/relay` | Live call loop — receives `prompt`/`dtmf`, sends `text`/`sendDigits`/`end` |
| `POST /voice/status` | Call status callback → triggers outcome summary + SMS |
| `GET /health` | Fly health check |

---

## 3. Data model

### Google Sheet — one row per medication

| Column | Example | Notes |
|---|---|---|
| `med` | Lisinopril | |
| `dose` | 10mg | |
| `qty` | 90 | tablets dispensed |
| `days_supply` | 90 | |
| `last_filled` | 2026-06-02 | ISO date; app writes this back on a confirmed refill |
| `prescriber` | Dr. Alicia Reyes | |
| `prescriber_phone` | +15125551234 | E.164 |
| `phone_tree_hint` | `w2w1` | optional; known DTMF path to the refill line |
| `lead_days` | 7 | how early to nudge; default 7 |
| `status` | active | `active` / `paused` |
| `notes` | | free text, passed to the voice agent as context |

Runs-out date = `last_filled + days_supply`. Nudge fires at `runs_out - lead_days`.

Read via `gspread` with a Google service account; share the sheet with the service account
email. Read-only except for the `last_filled` write-back.

### SQLite (Fly volume at `/data`)

- **`threads`** — one open conversation per med: `med_key`, `state`, `snooze_until`, `last_nudged_at`
- **`calls`** — `med_key`, `twilio_call_sid`, `started_at`, `ended_at`, `outcome`, `transcript` (JSON), `summary`
- **`messages`** — every SMS in and out, for context and for debugging what the model saw

### Thread state machine

```
IDLE ──nudge sent──▶ AWAITING_REPLY ──"go ahead"──▶ CALL_QUEUED ──▶ CALLING
  ▲                       │                                            │
  │                       └──"remind me Friday"──▶ SNOOZED ──┐         │
  │                                                          │         ▼
  └──────────────────────────────────────────────────────────┴── DONE / FAILED
```

A "question" reply does not change state — you can ask things mid-thread without
losing the pending decision.

---

## 4. The SMS loop

**Outbound nudge:**
> Lisinopril 10mg runs out in 7 days (Sep 1). Want me to call Dr. Reyes for a refill?

**Inbound handling:**
1. Validate the `X-Twilio-Signature` header. Reject anything that fails.
2. Reject any sender that isn't `OWNER_PHONE`. Non-negotiable — this number can trigger phone
   calls that disclose your DOB, so it answers to exactly one handset.
3. Load the open thread + recent message history.
4. Claude classifies into a structured intent:
   - `request_refill` → queue the call
   - `snooze{until: date}` → resolves relative language ("Friday", "give it a week") against your timezone
   - `question` → answer from Sheet data only
   - `unclear` → ask one clarifying question
5. Reply.

Only your reply triggers a call. The system never dials on its own initiative.

---

## 5. The voice agent

### Call setup
Outbound call via the REST API with `machineDetection="DetectMessageEnd"`, pointing at
`/voice/twiml`, which returns `<Connect><ConversationRelay url="wss://…/voice/relay" welcomeGreeting="…">`.

### The fact sheet — the core safety mechanism
Before the call, the app assembles a **closed set of facts** the agent is permitted to state:

```
Patient: <full name>, DOB <date>, callback <phone>
Medication: Lisinopril 10mg, 90 tablets, last filled 2026-06-02
Prescriber: Dr. Alicia Reyes
Pharmacy: <name>, <address>, <phone>
```

The system prompt's hardest rule: **if asked anything not on this list — other medications,
symptoms, conditions, insurance, appointment history — the agent says
"I don't have that, I'll have him call you back" and does not guess.** An improvised answer
about your medical history is the one genuinely damaging failure mode here, and it's
prevented by construction rather than by hoping the model behaves.

### Disclosure
Every call opens with an explicit statement that this is an automated assistant calling on
your behalf. This is both the honest thing and the practical thing — offices hang up on
robots that pretend otherwise, and some states require it.

### Recording
Off by default. Two-party consent states cover the office too, and you don't need the audio —
you get the transcript from ConversationRelay for free.

### Phone trees
ConversationRelay delivers transcribed IVR audio as `prompt` messages; the agent responds with
`sendDigits` (`digits` accepts `0-9`, `w`, `#`, `*` — `w` is a ~0.5s pause). `phone_tree_hint`
in the Sheet lets you hard-code a known path once you've learned it, skipping the guesswork.

### Voicemail
If AMD reports a machine, the agent skips the conversational flow and reads a fixed voicemail
script: who, which med, which pharmacy, callback number.

### After the call
`/voice/status` fires → Claude summarizes the transcript into
`{outcome: sent_to_pharmacy | refused | needs_appointment | voicemail | unclear, detail}` →
you get an SMS with the outcome plus the two or three quotes that determine it. On a confirmed
refill, `last_filled` is written back to the Sheet.

Per your call: **one attempt, no auto-retry.** If it fails you'll know within minutes and can
say "try again" — which just re-queues it.

---

## 6. Security

- Twilio signature validation on `/sms/inbound`, `/voice/twiml`, `/voice/status`
- Websocket authenticated by a signed one-time token in the `wss://` URL, bound to the call SID
- Single-number allowlist for anything that causes an action
- Secrets via `fly secrets` (`OWNER_PHONE`, `PATIENT_NAME`, `PATIENT_DOB`, `PHARMACY_*`,
  `TWILIO_*`, `ANTHROPIC_API_KEY`, `GOOGLE_SA_JSON`) — never committed, never in the Sheet
- DOB and med names redacted from application logs; full detail only in the SQLite `calls` row
- A hard cap on outbound calls per day, so a bug can't dial a doctor's office forty times

---

## 7. Milestones

### M1 — SMS loop (target: usable in a couple of evenings)
1. FastAPI skeleton, Fly app + volume, health check
2. `gspread` reader + `runs_out` computation + local test fixture
3. APScheduler daily job → nudge SMS via Twilio
4. `/sms/inbound` with signature validation + sender allowlist
5. Claude intent parser → `request_refill` / `snooze` / `question` / `unclear`
6. Snooze persistence; Q&A answered from Sheet data
7. `request_refill` **stubs the call**: "Would call Dr. Reyes at +1512…" — proves the whole
   decision path before a single real dial

**Done when:** you get a real text, reply "not yet, remind me Friday", and it does.

### M2 — voice
1. Outbound call + `/voice/twiml` returning ConversationRelay TwiML
2. `/voice/relay` websocket: handle `setup`/`prompt`/`dtmf`/`interrupt`/`error`,
   send `text`/`sendDigits`/`end`
3. Fact-sheet system prompt + the refuse-to-improvise rule
4. AMD → voicemail script branch
5. `/voice/status` → transcript summary → outcome SMS → Sheet write-back
6. **Test against a phone number you control before any real office.** Record a fake IVR on a
   second Twilio number and make the agent navigate it.

### M3 — hardening
Structured logging, call-volume cap, Sheet schema validation with a clear error SMS on bad rows,
graceful behavior when the Sheet is unreachable.

---

## 8. Costs

| Item | Rough |
|---|---|
| Fly.io shared-cpu-1x + small volume | ~$3–5/mo |
| Twilio phone number | ~$1.15/mo |
| SMS | ~$0.0079 each — pennies/mo at this volume |
| Voice + ConversationRelay | per-minute voice plus a ConversationRelay per-minute rate — **check current pricing**, it dominates everything else here |
| Claude Sonnet 5 tokens | negligible at this volume |

Realistically a few dollars a month plus whatever the calls cost.

---

## 9. Open risks

1. **Offices may refuse to deal with an automated caller.** The single biggest unknown, and it's
   social, not technical. If it turns out most offices won't engage, the fix is the bridge
   pattern: the agent handles the tree and the hold, then conferences you in when a human picks
   up. Worth keeping in the back pocket.
2. **Phone trees vary wildly** and the first calls to any given office will likely fail. The
   `phone_tree_hint` column exists so a failure only has to happen once per office.
3. **No retry** means a call placed at 5:59pm just fails. Mitigated by scheduling calls during
   business hours only.
4. **DOB in environment secrets** is a real if small exposure — anyone with Fly access to the app
   has it. Acceptable for a personal tool; would not be for anything shared.
5. **ConversationRelay is Twilio-proprietary.** If it's ever deprecated, the escape hatch is raw
   Media Streams with Deepgram + a TTS provider — same architecture, more plumbing.

---

## 10. First step

`git init`, FastAPI skeleton, Twilio number provisioned, and a Sheet with one row in it.
