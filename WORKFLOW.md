[README](README.md) · [Architecture](ARCHITECTURE.md) · [Workflow](WORKFLOW.md) · [Plan](PLAN.md) · [CLAUDE.md](CLAUDE.md)

# Workflow

Step-by-step walkthroughs of what actually happens for each thing this app does. For the
component map and data model these workflows move through, see
[ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Nudge → follow-up

```mermaid
sequenceDiagram
    participant Sched as APScheduler (daily)
    participant App
    participant Sheet as Google Sheet
    participant DB as SQLite
    participant Owner

    Sched->>App: run_nudge_check
    App->>Sheet: load_medications (fresh read)
    App->>DB: check each due medication's thread
    Note over App,DB: skip if already AWAITING_REPLY / SNOOZED (not yet due) / CALLING
    App->>Owner: SMS nudge
    App->>DB: mark_nudged → state=AWAITING_REPLY, schedules next_followup_at
```

If nothing comes back, a second scheduler job (`run_followup_check`, every 30 minutes) picks up
where the nudge left off:

```mermaid
sequenceDiagram
    participant Sched as APScheduler (30 min)
    participant App
    participant DB as SQLite
    participant Owner

    Sched->>App: run_followup_check
    App->>DB: threads AWAITING_REPLY with next_followup_at due
    alt within FOLLOWUP_CAP_DAYS of the original nudge
        alt inside the send window
            App->>Owner: varied follow-up text
            App->>DB: reschedule next_followup_at (4-8h random offset, re-clamped to the window)
        else outside the window
            App->>DB: reschedule to next window open, no text sent
        end
    else cap reached
        App->>Owner: one final "I'll stop checking in" text
        App->>DB: followup_final_sent = 1 (no more follow-ups for this thread)
    end
```

The day-cap counts from `first_nudged_at` — the *original* nudge — not from the last follow-up,
so it can't be reset indefinitely by repeated follow-ups.

## 2. Reply → classify → act

Every inbound SMS goes through the same entry point (`app/reply.py:
handle_inbound_reply`), regardless of which of the branches below it ends up in.

```mermaid
flowchart TD
    In["Inbound SMS"] --> Verify{"Ed25519 signature valid\nAND sender == OWNER_PHONE?"}
    Verify -- no --> Silent["200, no reply sent"]
    Verify -- yes --> Resolve["Resolve medication:\nname mentioned in text? else active thread"]
    Resolve -- none found --> NoThread["'No open reminder to match that to yet'"]
    Resolve --> Pending{"thread.state ==\nAWAITING_FILL_CONFIRMATION?"}
    Pending -- yes --> Confirm["classify_fill_confirmation"]
    Pending -- no --> Decide["classify_reply"]
    Decide --> RR["request_refill\n→ place the call"]
    Decide --> SZ["snooze\n→ set_snooze"]
    Decide --> MF["mark_filled\n→ set_pending_fill,\nask to confirm the date"]
    Decide --> Q["question\n→ answer from Sheet facts only"]
    Decide --> U["unclear\n→ ask a clarifying question"]
```

Medication resolution tries a **name match** first (so an unprompted text like "just filled the
lisinopril" works with no open thread), and only falls back to `get_active_thread` — the thread
`AWAITING_REPLY`/`AWAITING_FILL_CONFIRMATION`, or else whichever was touched most recently — when
the text doesn't name exactly one medication.

A `question` or `unclear` reply while `AWAITING_REPLY` doesn't change state, but it does push
the follow-up clock forward from that moment — the owner engaged, so the next automated
follow-up shouldn't fire as if they'd gone silent.

## 3. Confirming a fill date

`mark_filled` never writes to the Sheet on the first message — it always proposes a date and
waits for a second reply:

```mermaid
sequenceDiagram
    participant Owner
    participant App
    participant Sheet as Google Sheet

    Owner->>App: "just filled the lisinopril"
    App->>App: classify_reply → mark_filled(filled_on?)
    App->>Owner: "Mark Lisinopril 10mg filled on 2026-09-05? Reply yes, or give me the right date."
    Note over App: thread.state = AWAITING_FILL_CONFIRMATION

    alt confirmed
        Owner->>App: "yes"
        App->>Sheet: mark_filled(med_key, date)
        App->>Owner: "Marked ... as filled on 2026-09-05."
        Note over App: state → IDLE
    else corrected
        Owner->>App: "no, the 3rd"
        App->>Owner: "Mark ... filled on 2026-09-03? Reply yes to confirm."
        Note over App: stays AWAITING_FILL_CONFIRMATION with the new date
    else cancelled
        Owner->>App: "nevermind"
        App->>Owner: "Okay, I won't update it."
        Note over App: state → IDLE, nothing written
    end
```

This is the one place a misclassification can't silently corrupt the Sheet — nothing is written
until the owner explicitly confirms a specific date.

## 4. Refill call (M2)

Only reachable via a `request_refill` reply — the system never dials on its own initiative.

```mermaid
sequenceDiagram
    participant App
    participant Telnyx as Telnyx Conversation Relay
    participant Office as Prescriber's office
    participant Owner

    App->>Telnyx: calls.dial(conversation_relay_config, AMD=premium)
    Telnyx->>Office: places the call
    App->>App: insert `calls` row, sign relay token (rid + med_key)
    Telnyx-->>App: WS /voice/relay connects (setup frame)

    alt AMD detects a machine
        App->>Telnyx: read fixed VOICEMAIL_SCRIPT, end call
    else human answers
        loop each turn
            Telnyx->>App: prompt / dtmf frame
            App->>App: next_call_action (fact-sheet-gated Claude call)
            App->>Telnyx: speak / sendDigits / end
        end
    end

    Telnyx-->>App: call ends (relay closes, or /voice/status call.hangup)
    App->>App: summarize_call → {outcome, detail}
    alt sent_to_pharmacy
        App->>Sheet: write last_filled
    end
    App->>Owner: SMS with the outcome
```

The agent's system prompt is built from a **closed fact sheet** (patient name/DOB, this one
medication, prescriber, pharmacy) with an explicit instruction to say "I don't have that, I'll
have him call you back" rather than improvise anything outside it — the same fact-gating
principle `classify_reply` uses for text replies. One attempt, no auto-retry: if it fails, the
owner finds out within minutes and can just say "try again."
