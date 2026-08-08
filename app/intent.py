from datetime import date
from typing import Literal

from anthropic import Anthropic
from pydantic import BaseModel

from app.config import Settings
from app.models import Medication

MODEL = "claude-sonnet-5"

SYSTEM_PROMPT = """You are the reply-classifier for a prescription refill reminder system. \
The user was texted a nudge about a medication refill and has just replied. Your only job \
is to classify that reply by calling the classify_reply tool — never respond in plain text.

Classify into exactly one of:
- request_refill: the user wants the system to call the prescriber to request a refill \
  ("yes", "go ahead", "call them", "please do").
- snooze: the user wants to be reminded later rather than act now. Resolve whatever relative \
  language they used ("Friday", "give it a week", "next month") into an absolute ISO date using \
  today's date, provided below. Prefer the most natural reading of ambiguous phrasing.
- question: the user is asking about the medication rather than deciding on the refill. Answer \
  using ONLY the medication facts provided in the context below. Never invent a fact, dosage \
  detail, or medical opinion that isn't given to you. If the facts don't cover what they asked, \
  say so plainly rather than guessing.
- unclear: the reply doesn't clearly fit the above. Produce one short, specific clarifying \
  question to send back.

Do not give medical advice under any classification. You are a scheduling assistant, not a \
clinician."""

INTENT_TOOL = {
    "name": "classify_reply",
    "description": "Classify the user's SMS reply about a medication refill nudge.",
    "input_schema": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["request_refill", "snooze", "question", "unclear"],
            },
            "snooze_until": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD to resume nudging. Present only when kind == snooze.",
            },
            "answer": {
                "type": "string",
                "description": (
                    "When kind == question: the answer, using only the provided facts. "
                    "When kind == unclear: a short clarifying question to send back."
                ),
            },
        },
        "required": ["kind"],
    },
}


class Intent(BaseModel):
    kind: Literal["request_refill", "snooze", "question", "unclear"]
    snooze_until: date | None = None
    answer: str | None = None


def _build_context(med: Medication, history: list[dict], today: date) -> str:
    lines = [
        f"Today's date: {today.isoformat()}",
        "",
        "Medication facts (the only facts you may state in a question answer):",
        f"- Name: {med.med} {med.dose}",
        f"- Quantity per fill: {med.qty}",
        f"- Days supply: {med.days_supply}",
        f"- Last filled: {med.last_filled.isoformat()}",
        f"- Runs out: {med.runs_out.isoformat()}",
        f"- Prescriber: {med.prescriber}",
        f"- Status: {med.status}",
    ]
    if med.notes:
        lines.append(f"- Notes: {med.notes}")
    if history:
        lines.append("")
        lines.append("Recent message history (oldest first):")
        for m in history:
            who = "System" if m["direction"] == "out" else "User"
            lines.append(f"{who}: {m['body']}")
    return "\n".join(lines)


def classify_reply(
    settings: Settings,
    med: Medication,
    history: list[dict],
    reply_text: str,
    today: date,
) -> Intent:
    client = Anthropic(api_key=settings.anthropic_api_key)
    context = _build_context(med, history, today)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        tools=[INTENT_TOOL],
        tool_choice={"type": "tool", "name": "classify_reply"},
        messages=[{"role": "user", "content": f"{context}\n\nLatest reply: {reply_text}"}],
    )
    tool_use = next(b for b in resp.content if b.type == "tool_use")
    data = tool_use.input
    return Intent(
        kind=data["kind"],
        snooze_until=date.fromisoformat(data["snooze_until"]) if data.get("snooze_until") else None,
        answer=data.get("answer"),
    )
