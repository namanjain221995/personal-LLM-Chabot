"""Router engine (spec §8, vLLM design; V2-DESIGN §3a adds a 5th class).

Qwen3-4B-Instruct-2507 on the vLLM router endpoint (OpenAI-compatible) with
a strict prompt + few-shots; must output exactly
{"route": "sql|rag|vision|report|chat"}. An attached image forces the vision
route before any model call. Unparseable output → fall back to classifying
with gpt-oss-120b; if that also fails, default to "rag".
"""
from __future__ import annotations

import json
import re
from typing import List, Sequence, Optional

from .. import llm

ROUTES = ("sql", "rag", "vision", "report", "chat")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)
_ROUTE_RE = re.compile(r'"route"\s*:\s*"(sql|rag|vision|report|chat)"', re.I)

_SYSTEM = (
    "You are the request router for the TechSara Local AI Analysis Platform.\n"
    "Classify the user's request into exactly one route:\n"
    '- "sql": questions answered by querying structured Salesforce tables '
    "(counts, sums, rankings, filters, trends, top-N) — AND any request for "
    "the RECORDS of a named person or thing: their training, interviews, "
    "invoices, sessions, status, history, dates. If the answer is fields on "
    "records, it is sql, however the question is worded.\n"
    '- "rag": questions about the free TEXT inside records — what someone '
    "said, wrote or complained about, in notes, emails, case comments and "
    "descriptions. Choose rag only when the answer is prose a human typed, "
    "never merely because a person is named.\n"
    '- "vision": the user attached an image (screenshot, invoice, contract, '
    "photo) to analyze.\n"
    '- "report": the user wants a multi-section document/report generated '
    "(docx/pdf), typically with several analyses and charts.\n"
    '- "chat": greetings, small talk, thanks, introductions, or questions '
    "about who you are — nothing that needs Salesforce data.\n"
    "Respond with ONLY this JSON object and nothing else: "
    '{"route": "sql|rag|vision|report|chat"}'
)

# Few-shots per spec §8, updated for the V2 "chat" class (V2-DESIGN §3a).
FEW_SHOTS = [
    ("Show total pipeline value by stage for open opportunities", "sql"),
    ("What concerns did Acme raise about renewal in recent case comments?", "rag"),
    ("Here is a photo of an invoice — what is the total amount due?", "vision"),
    ("Create a Q3 sales performance report with charts and email highlights", "report"),
    ("Top 10 accounts by closed-won revenue last quarter", "sql"),
    # A named person plus "details" read as narrative and went to rag, which
    # text-searched record bodies and answered "no training details found"
    # about a candidate with five enrolments.
    ("give me details for Rakshith Bodakuntla's training", "sql"),
    ("show me everything about Priya Sharma", "sql"),
    ("what did the client say in their feedback about Priya Sharma?", "rag"),
    ("hello my name is X", "chat"),
    ("Thanks, that was helpful! Who are you exactly?", "chat"),
]


def parse_route(text: object) -> Optional[str]:
    """Parse a route from model output. Returns one of ROUTES, or None.

    Handles: plain JSON, code-fenced JSON, <think> preambles, surrounding
    prose (regex fallback), and case-insensitive route values. Anything else
    (garbage, unknown routes, wrong keys) → None.
    """
    if not text or not isinstance(text, str):
        return None
    t = _THINK_RE.sub("", text).strip()
    m = _FENCE_RE.search(t)
    if m:
        t = m.group(1).strip()
    # Strict path: valid JSON object with a "route" key.
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(t[start : end + 1])
            if isinstance(obj, dict):
                route = str(obj.get("route", "")).strip().lower()
                if route in ROUTES:
                    return route
        except (json.JSONDecodeError, ValueError):
            pass
    # Lenient path: find the route key/value anywhere in the text.
    m = _ROUTE_RE.search(text)
    if m:
        return m.group(1).lower()
    return None


def _messages(message: str) -> List[dict]:
    msgs: List[dict] = [{"role": "system", "content": _SYSTEM}]
    for question, route in FEW_SHOTS:
        msgs.append({"role": "user", "content": question})
        msgs.append({"role": "assistant", "content": json.dumps({"route": route})})
    msgs.append({"role": "user", "content": message})
    return msgs


async def route_request(
    message: str, has_image: bool = False, history: Sequence[dict] = ()
) -> str:
    """Pick the engine route for a user message.

    `history` matters for SHORT follow-ups. Classified alone, "just tell me if
    it exists, yes or no?" looks like small talk and lands on "chat" — which
    then answers with a canned apology instead of querying the data the
    previous turn was about. The last user turn is prepended so the follow-up
    inherits its subject.
    """
    if has_image:
        return "vision"  # an attached image forces vision (§8)

    previous = [m for m in history if m.get("role") == "user"]
    if previous and len(message.split()) <= 12:
        earlier = str(previous[-1].get("content", ""))[:400]
        if earlier:
            message = f"(earlier question: {earlier})\nFollow-up: {message}"

    # Primary: ROUTER_MODEL on the vLLM router endpoint — temperature 0,
    # small max_tokens; the strict-JSON prompt + parse below do the rest.
    try:
        raw = await llm.router_chat_completion(
            _messages(message), temperature=0.0, max_tokens=200
        )
        route = parse_route(raw)
        if route:
            return route
    except Exception:
        pass

    # Fallback: classify with gpt-oss-120b (§8).
    try:
        raw = await llm.chat_completion(_messages(message), temperature=0.0, max_tokens=50)
        route = parse_route(raw)
        if route:
            return route
    except Exception:
        pass

    return "rag"
