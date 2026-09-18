"""Explicit fact store (V10, 2026-08-21) — ChatGPT-style Memory.

When the user states something durable ("Sahil Patel is the CEO of TechSara",
"my name is Naman", "always answer in Hindi"), a background call to the small
router model extracts it as a short third-person fact and stores it in the
`user_facts` table. Every later assistant-mode request injects the user's
facts as a labelled system block, so the model "remembers" without any
fine-tuning — memory is retrieval, exactly as ChatGPT does it.

Extraction runs CONCURRENTLY with answer generation (it reads only the user's
message, not the answer), so it adds zero latency; its result rides out on
the final meta as `memory_updated` when it lands before the answer finishes.
Everything degrades to "no memory update" on failure — never a failed chat.

MEMORY INTEGRITY (2026-09-18). The extractor is a prompt, and until this
round whatever it returned was written to the table unchecked and then read
back to the model under "treat as true for this user". The platform sweep
found what that costs: a pasted CV overwrote an account's own name, email and
employer with a stranger's; "please forget that I'm vegetarian" was stored as
"The user is not vegetarian" and produced steakhouse recommendations; a
headcount moved from an old employer to a new one and was answered flatly;
and 39% of the live store was one-off task requests replayed as a to-do list.
Four rules now stand between the model and the table:

  1. Only the person's own words about themselves. A turn carrying an
     attachment writes nothing, and fenced/quoted material and pasted-length
     messages are not the person speaking (`own_words`).
  2. A "forget that" may only DELETE (`_FORGET_RE`, db.delete_user_fact); it
     can never add or rewrite, so an erasure request cannot leave a negated
     copy of the thing behind.
  3. Only what was stated: a fact may not contain a number or a name the
     message did not contain (`ungrounded_in`), and every row records where
     it came from (`source`, `source_excerpt` — db V40).
  4. A one-off task request is not a durable fact (`is_durable`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List, Optional

from . import db, llm
from .config import settings

log = logging.getLogger(__name__)

FACTS_HEADER = (
    "Durable facts this user has told you in past conversations (their saved "
    "memory — treat as true for this user unless they correct you; don't "
    "mention this list unless asked):"
)

# Bounds keep the block and the extractor prompt from growing without limit.
_FACT_MAX_CHARS = 300
_BLOCK_MAX_CHARS = 6000
_MESSAGE_MAX_CHARS = 4000
_MESSAGE_MIN_CHARS = 8

_EXTRACT_SYSTEM = """You maintain a user's long-term memory for a chat assistant.

Given the user's newest message and their currently saved facts, decide what
to remember. A fact is a short, durable, third-person statement the user
STATED about themselves or about the world ("Sahil Patel is the CEO of
TechSara", "The user's name is Naman", "The user prefers answers in Hindi").

Store ONLY what the message says in so many words. Never infer, never
combine a saved fact with a new one, and never carry a detail (a number, a
name) from one subject to another: if the user changes employer, the old
employer's headcount is NOT the new employer's.

The message may quote or contain a document, a CV, an email or another
person's words. That material is not the user. Only first-person statements
the user makes about themselves become facts about them.

Do NOT store: questions, requests ("give me 200 practice questions"),
greetings, opinions about the current task, anything transient ("today",
"this file"), or anything already saved.

If a new statement contradicts or updates a saved fact, replace that fact.
When the user asks you to FORGET something, put that saved fact's id in
"remove" — never store a negated version of it.

Reply with ONLY a JSON object, no other text:
{"add": ["<new fact>", ...], "replace": [{"id": <saved fact id>, "fact": "<rewritten fact>"}, ...], "remove": [<saved fact id>, ...]}
Use {"add": [], "replace": [], "remove": []} when there is nothing to do."""

#: A message that asks the assistant to forget something. Such a message
#: never CREATES memory — the sweep found "please forget that I'm vegetarian"
#: stored as "The user is not vegetarian", so the erasure request itself
#: became a permanent record of the thing (and the assistant then recommended
#: steakhouses). Every add/replace is dropped for these messages; only a
#: delete may come out of one.
_FORGET_RE = re.compile(
    r"\b(?:forget|un-?remember|erase)\b"
    r"|\b(?:stop|don'?t|do not|no longer)\s+(?:remember|remembering|storing|saving)\b"
    r"|\b(?:delete|remove|drop|clear)\s+(?:that|this|the|my)?\s*(?:saved\s+)?"
    r"(?:memory|memories|fact|facts)\b",
    re.I,
)

#: …but "forget" is also ordinary English, and the id-less fallback below
#: DELETES the one saved fact whose content words a forget request matches.
#: QA reproduced two silent, irreversible deletions on 2026-09-18: "Don't
#: forget to add the unit tests" removed "The user always wants unit tests with
#: code", and "I always forget my password, any tips?" removed the password
#: manager fact. A reminder ("forget to …") and a confession ("I always forget
#: …") are not erasure requests.
_NOT_A_FORGET_RE = re.compile(
    r"\bforget\s+to\b"
    r"|\b(?:i|we)\s+(?:always|often|sometimes|usually|keep|kept|never)?\s*forget\b",
    re.I,
)

#: A one-off task request wearing a fact's clothes. 51 of the 132 rows in the
#: production store matched this shape ("The user is asking about all movie
#: names in the Spider-Man franchise", "The user wants 200 LeetCode
#: questions"), and they came back to the person as a to-do list when they
#: asked what the assistant remembered about them. The extractor prompt
#: already forbids them; the model ignores it, so the filter is code.
_TRANSIENT_FACT_RE = re.compile(
    r"^the user(?:'s)?\s+(?:is\s+|was\s+|has\s+|have\s+|had\s+)?"
    r"(?:currently\s+|now\s+|also\s+)?"
    r"(?:asking|asked|asks|request|requests|requested|requesting|want|wants|"
    r"wanted|need|needs|needed|looking|discussing|trying)\b",
    re.I,
)

#: …unless the same sentence states a STANDING preference, which is durable
#: however it is phrased. Two shapes count: an explicit standing word
#: ("always", "prefers"), and a wish about HOW the assistant should answer
#: ("The user wants responses in layman terms") — the production store holds
#: real ones of both kinds, and losing them would trade one regression for
#: another. A wish about WHAT to produce ("200 LeetCode questions", "an ATM
#: UI in Python") matches neither and stays out.
_DURABLE_PREFERENCE_RE = re.compile(
    r"\b(?:always|never|prefers?|preference|by default|from now on)\b"
    r"|\b(?:answers?|responses?|replies|explanations?|output|tone|style|"
    r"wording|format|formatting|language|units)\b"
    r"\s+(?:in|to be|as|with|without|free of|avoiding|using|written|formatted)\b",
    re.I,
)

#: Fenced blocks and quoted lines are material the person put in front of the
#: assistant, not the person speaking. They are cut out before the extractor
#: sees the message, so nothing inside them can become a fact and nothing
#: inside them can GROUND one either.
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_QUOTED_LINE_RE = re.compile(r"^\s*>.*$", re.M)

#: A pasted document announces itself in its layout long before it reaches the
#: length ceiling: an ALL-CAPS name banner, or a line carrying an email address.
#: QA stored a 214-character CV paste as the account's name, email and employer
#: on 2026-09-18 — the same defect as a 4,000-character one, under the ceiling.
_DOCUMENT_BANNER_RE = re.compile(
    r"^[A-Z][A-Z][A-Z .'\u2019-]{3,}$"
    r"|^[^\n]*[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\n]*$",
    re.M,
)

#: Grounding: a stored fact may only contain numbers and names the message
#: itself contains. `_NAME_RE` is deliberately crude — a capitalized word is
#: a name often enough, and the cost of a false positive is one fact not
#: saved, while the cost of a false negative is an invented one treated as
#: true forever.
_NUMBER_RE = re.compile(r"\d+")
_NAME_RE = re.compile(r"\b[A-Z][A-Za-z][A-Za-z'’-]+\b")
_NAME_STOPWORDS = frozenset(
    """The This That There These Those They Their Them User Users And But
    For Not With From Into Also When What Who Where Why How Has Have Had
    Does Did Will Would Should Could Every Always Never Prefers Prefer
    Wants Want Likes Like Uses Use Works Work Lives Live Needs Need""".split()
)

#: Words too common to identify WHICH saved fact a "forget that" points at.
_MATCH_STOPWORDS = frozenset(
    """please forget remember memory memories fact facts that this these
    those about from with your you mine thing things stuff anymore longer
    what when where which have here there stop don't dont delete remove
    drop clear stored saved again also just only more been very said told
    tell said know known sure okay date outdated wrong""".split()
)


def facts_block(facts: List[dict]) -> Optional[str]:
    """Render saved facts as the system block, or None when there are none."""
    if not facts:
        return None
    lines = [FACTS_HEADER]
    used = len(FACTS_HEADER)
    for f in facts:
        line = f"- {f['fact']}"
        if used + len(line) > _BLOCK_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else None


def parse_extraction(raw: str) -> dict:
    """The extractor's JSON, tolerantly parsed. {} when unusable."""
    if not raw:
        return {}
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    raw_add = data.get("add")
    add = [
        _flatten(f)
        for f in (raw_add if isinstance(raw_add, list) else [])
        if isinstance(f, str) and f.strip()
    ]
    raw_replace = data.get("replace")
    replace = []
    for item in raw_replace if isinstance(raw_replace, list) else []:
        if not isinstance(item, dict):
            continue
        fact = item.get("fact")
        try:
            fact_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if isinstance(fact, str) and fact.strip():
            replace.append({"id": fact_id, "fact": _flatten(fact)})
    raw_remove = data.get("remove")
    remove = []
    for item in raw_remove if isinstance(raw_remove, list) else []:
        try:
            remove.append(int(item))
        except (TypeError, ValueError):
            continue
    add = [f for f in add if is_durable(f)]
    replace = [item for item in replace if is_durable(item["fact"])]
    return {"add": add, "replace": replace, "remove": remove}


def is_durable(fact: str) -> bool:
    """False for a one-off task request dressed as a fact.

    "The user is asking about all movie names in the Spider-Man franchise" is
    what the person wanted once, not who they are; saved, it comes back as a
    to-do list the next time they ask what the assistant remembers. A standing
    preference stays, however it is phrased.
    """
    text = " ".join((fact or "").split())
    if not text:
        return False
    if _DURABLE_PREFERENCE_RE.search(text):
        return True
    return _TRANSIENT_FACT_RE.match(text) is None


def own_words(text: str) -> Optional[str]:
    """The part of a message that is the PERSON speaking, or None.

    Fenced blocks and quoted lines are material, not speech, and come out.
    What is left is the person's own words — unless it is longer than a
    self-disclosure can plausibly be, in which case the message is a pasted
    document (a CV, a contract, an email thread) and none of it is a fact
    about the person. The composer folds a paste inline with no marker
    (frontend/lib/pasted.ts), so length and layout are the only signals there
    are: a multi-line message with an ALL-CAPS name banner or an email line is
    a pasted document whatever its length.
    """
    body = _FENCE_RE.sub(" ", text or "")
    body = _QUOTED_LINE_RE.sub(" ", body)
    body = body.strip()
    if not body:
        return None
    if len(body) > settings.memory_self_disclosure_max_chars:
        return None
    if "\n" in body and _DOCUMENT_BANNER_RE.search(body):
        return None
    return body


def ungrounded_in(fact: str, *sources: Optional[str]) -> Optional[str]:
    """The first number or name in `fact` that no source contains, or None.

    The extractor is a prompt, and prompts invent. A person who says "I left
    Northwind Freight, I'm at Halcyon Rail now" has not said how big Halcyon
    Rail is — but the extractor rewrote the old employer's headcount onto the
    new one and the next chat answered "your engineering team at Halcyon Rail
    has 29 engineers". A fact may only carry numbers and names that were
    actually said.
    """
    return _ungrounded_number(fact, *sources) or _ungrounded_name(fact, *sources)


def _ungrounded_number(fact: str, *sources: Optional[str]) -> Optional[str]:
    hay = " ".join(s or "" for s in sources).lower()
    for number in _NUMBER_RE.findall(fact or ""):
        if number not in hay:
            return number
    return None


def _ungrounded_name(fact: str, *sources: Optional[str]) -> Optional[str]:
    hay = " ".join(s or "" for s in sources).lower()
    for match in _NAME_RE.finditer(fact or ""):
        token = match.group(0)
        if match.start() == 0 or token in _NAME_STOPWORDS:
            continue
        if token.lower() not in hay:
            return token
    return None


def _flatten(text: str) -> str:
    """One whitespace-normalized line, capped. A fact with embedded newlines
    would escape its bullet in facts_block and read as fresh top-level system
    lines — a durable prompt-injection channel for text the extractor was fed.
    Same normalization memory_api applies to manual adds."""
    return " ".join((text or "").split())[:_FACT_MAX_CHARS]


def _normalized(text: str) -> str:
    return " ".join((text or "").lower().split()).rstrip(".")


def _content_words(text: str) -> set:
    return {
        w
        for w in re.findall(r"[a-z][a-z'’-]{3,}", (text or "").lower())
        if w not in _MATCH_STOPWORDS
    }


def _fact_named_by(text: str, existing: List[dict]) -> Optional[int]:
    """The id of the ONE saved fact a "forget that" names, or None.

    The model is asked for the id and usually gives one; when it does not,
    the request is still an erasure, and "forget that I'm vegetarian" points
    at the row that says vegetarian. Exactly one match, or nothing happens —
    deleting the wrong memory is worse than deleting none.
    """
    words = _content_words(text)
    if not words:
        return None
    hits = [f["id"] for f in existing if words & _content_words(f["fact"])]
    return hits[0] if len(hits) == 1 else None


async def remember_after_route(
    gate: "asyncio.Future[bool]",
    user_id: int,
    user_text: str,
    conversation_id: Optional[str],
    *,
    attachments: bool = False,
    complete=None,
) -> List[dict]:
    """`remember_from_message`, held until the turn knows its route.

    The chat turn starts extraction the moment it has the message, before
    it knows whether the message is a request for a FILE. A request for a
    file is not a fact about the person ("make me a PDF of the audit" is a
    task), yet the extractor is a prompt and prompts are not guarantees —
    so an artifact turn must never reach the model with it, let alone the
    `user_facts` table (CONTRACT-2 §8). Cancelling the task is not enough:
    the extractor's first awaits are thread hops and it often FINISHES
    before the artifact intent is decided. The turn therefore resolves
    `gate` once the route is known — True to extract as before (still
    concurrent with the answer, which starts after the same decision),
    False to do nothing — and every path out of the turn resolves it, so
    the task never waits forever."""
    if not await gate:
        return []
    return await remember_from_message(
        user_id,
        user_text,
        conversation_id,
        attachments=attachments,
        complete=complete,
    )


async def remember_from_message(
    user_id: int,
    user_text: str,
    conversation_id: Optional[str],
    *,
    attachments: bool = False,
    complete=None,
) -> List[dict]:
    """Extract and store durable facts from one user message.

    Returns the facts that were added, rewritten or deleted (empty when
    none); a deleted one carries `"deleted": True`. `complete` defaults to
    llm.router_chat_completion — injectable for tests.

    WHAT MAY BECOME A FACT (2026-09-18). Only the person's own words about
    themselves. A turn that carries an ATTACHMENT is a turn about a document,
    so it writes no memory at all, and a message long enough to be a pasted
    document is treated the same way (`own_words`). What the extractor then
    proposes is checked rather than trusted: nothing transient (`is_durable`),
    no number or name the message did not contain (`ungrounded_in`), and a
    message that asks to FORGET something may only delete.
    """
    if not settings.fact_extraction_enabled:
        return []
    # A document the person uploaded is third-party content: it is material
    # for the turn, never a statement the person made about themselves.
    if attachments:
        return []
    text = own_words(user_text)
    if not text or len(text) < _MESSAGE_MIN_CHARS:
        return []
    forget_request = bool(_FORGET_RE.search(text)) and not _NOT_A_FORGET_RE.search(
        text
    )
    try:
        existing = await db.run_in_thread(
            db.list_user_facts, user_id, settings.memory_max_facts
        )
        saved_lines = "\n".join(
            f"[{f['id']}] {f['fact']}" for f in existing
        ) or "(none yet)"
        if complete is None:
            complete = llm.router_chat_completion
        raw = await complete(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Saved facts:\n{saved_lines}\n\n"
                        f"User's newest message:\n{text[:_MESSAGE_MAX_CHARS]}"
                    ),
                },
            ],
            max_tokens=400,
        )
        ops = parse_extraction(raw)
        if not ops:
            return []
        by_id = {f["id"]: f for f in existing}
        known = {_normalized(f["fact"]): f["id"] for f in existing}
        stored: List[dict] = []
        added = 0
        # A delete happens because the PERSON asked for one. The extractor
        # may point at the row, but an unprompted "remove" on an ordinary
        # message would let a prompt quietly drop somebody's memory.
        removals = (
            [i for i in ops.get("remove", []) if i in by_id]
            if forget_request
            else []
        )
        if forget_request:
            # An erasure request never writes memory. Left to itself the
            # extractor answers "please forget that I'm vegetarian" with a
            # REPLACE — "The user is not vegetarian" — which is the erased
            # thing, kept forever and stated as the person's own words.
            ops["add"] = []
            ops["replace"] = []
            if not removals:
                target = _fact_named_by(text, existing)
                if target is not None:
                    removals = [target]
        for fact_id in removals:
            deleted = await db.run_in_thread(db.delete_user_fact, user_id, fact_id)
            if deleted:
                row = dict(by_id[fact_id])
                row["deleted"] = True
                stored.append(row)
                known.pop(_normalized(row["fact"]), None)
        for item in ops.get("replace", []):
            row = by_id.get(item["id"])
            if row is None:  # an id this user does not own, or already gone
                continue
            # A rewrite may re-use the names already in the fact it rewrites,
            # but every NUMBER must come from this message — carrying the old
            # employer's headcount onto the new employer is exactly the
            # invention this guards.
            missing = _ungrounded_number(item["fact"], text) or _ungrounded_name(
                item["fact"], text, row["fact"]
            )
            if missing is not None:
                log.info("fact rewrite dropped: %r not in the message", missing)
                continue
            updated = await db.run_in_thread(
                db.update_user_fact,
                user_id,
                item["id"],
                item["fact"],
                source="stated",
                source_excerpt=text,
            )
            if updated:
                known[_normalized(item["fact"])] = updated["id"]
                stored.append(updated)
        for fact in ops.get("add", []):
            if _normalized(fact) in known:  # extractor re-suggested a saved fact
                continue
            missing = ungrounded_in(fact, text)
            if missing is not None:
                log.info("fact dropped: %r not in the message", missing)
                continue
            # Replaces rewrite existing rows; only genuine adds consume slots.
            if len(existing) + added >= settings.memory_max_facts:
                break
            created = await db.run_in_thread(
                db.add_user_fact,
                user_id,
                fact,
                conversation_id,
                source="stated",
                source_excerpt=text,
            )
            known[_normalized(fact)] = created["id"]
            stored.append(created)
            added += 1
        return stored
    except Exception:
        log.warning("fact extraction failed", exc_info=True)
        return []
