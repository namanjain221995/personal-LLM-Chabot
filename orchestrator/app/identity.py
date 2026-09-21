"""Who the model is assisting — safe, server-derived, request-scoped.

main.py binds this from the authenticated Principal at the top of each chat
worker task; prompt builders append `identity_line()` to their system prompt.
A ContextVar so it rides the asyncio task without threading a parameter
through every engine signature.

Only ever the display name, email, workspace name and a name the person is on
record as having given — never roles, session ids, or anything
security-relevant: prompts are user-visible surfaces (reasoning traces quote
them), so nothing an attacker could use belongs here.

WHOSE NAME (2026-09-21). Production answered "What is my name?" with a
stranger's. The account's memory held rows written on 2026-09-16 out of a
pasted interview-simulation prompt — one of them a name fact for the
candidate in that prompt — and the saved-facts block states every stored row
as the person's own, so the model had two system blocks naming two people and
no rule for choosing. The account is the one name the server can VERIFY: it
was typed at sign-up and it is what the session cookie resolves to. A saved
fact may only name the person when its V40 provenance says the person is its
source — `user_facts.source` 'stated' (the extractor read their own message)
or 'manual' (they typed it into the memory panel). NULL predates V40, its
origin is unknown, and an unknown origin is exactly what this defect was made
of.

PRECEDENCE, highest first:
  1. the account's display name (main.py falls back to the username) —
     verified, and typed by the person or their administrator;
  2. a saved name fact whose source is 'stated' or 'manual' — their own words;
  3. the local part of the account's e-mail — a login handle, not a name, so
     it is offered as one and never asserted;
  4. nothing at all: say so plainly and ask.
2 outranks 3 because an address like `test1@gmail.com` yields "test1", and
calling somebody by their login handle when they have told you their name is
a worse answer than either name the server holds. In every branch the one
thing that can never happen is a name taken out of a document.
"""
from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from typing import Iterable, Mapping, Optional, Sequence, Tuple

from .db import fact_source_is_trusted

log = logging.getLogger(__name__)

_current: ContextVar[str] = ContextVar("techsara_identity", default="")

#: A name is a name, never a paragraph. A display name and a saved fact are
#: both user-written text landing in a system prompt, so the value that gets
#: quoted is flattened to one line and cut here; anything longer is a payload,
#: not somebody's name.
_NAME_MAX_CHARS = 80

#: The shapes a stored name fact is written in — the extractor's own template
#: ("The user's name is Naman", facts._EXTRACT_SYSTEM) and the first person a
#: hand-typed memory-panel entry tends to use.
_NAME_PATTERNS = (
    re.compile(
        r"^(?:the\s+)?(?:user(?:'s|’s)?|my)\s+name\s+is\s+(?P<name>[^.;,\n]+)",
        re.I,
    ),
    re.compile(
        r"^(?:the\s+)?(?:user|i)\s+(?:is\s+(?:called|named)|goe?s\s+by)\s+"
        r"(?P<name>[^.;,\n]+)",
        re.I,
    ),
)

#: The conduct half of the line, unchanged since the line existed. The
#: no-name variants drop the "address them by name" half: there is no name to
#: address them by, and an instruction to use one is how a model talks itself
#: into inventing one.
_CONDUCT = (
    " Address them naturally by name when it helps; never reveal "
    "information about other workspace members."
)
_CONDUCT_NAMELESS = " Never reveal information about other workspace members."


def _flat(value: object) -> str:
    """One line, whitespace collapsed, cut to _NAME_MAX_CHARS."""
    return " ".join(str(value or "").split())[:_NAME_MAX_CHARS]


def name_from_fact(fact: str) -> str:
    """The name a saved fact states, or "" when it states none. Pure."""
    text = " ".join((fact or "").split())
    for pattern in _NAME_PATTERNS:
        match = pattern.match(text)
        if match:
            return _flat(match.group("name")).strip("\"'“”")
    return ""


def trusted_name(facts: Optional[Iterable[Mapping]]) -> Tuple[str, str]:
    """(name, source_excerpt) from the first fact the PERSON is on record as
    the source of; ("", "") when no saved fact qualifies.

    The provenance test is repeated here even though `db.trusted_user_facts`
    already applies it in SQL, because this is the function that decides
    whether a stranger gets to name somebody: it must be safe when handed any
    list of facts at all.
    """
    for fact in facts or ():
        if not fact_source_is_trusted(fact.get("source")):
            continue
        name = name_from_fact(str(fact.get("fact") or ""))
        if name:
            return name, _flat(fact.get("source_excerpt"))
    return "", ""


def usable_facts(facts: Iterable[Mapping]) -> list:
    """The saved facts a prompt may state as true of THIS person.

    A row that NAMES them is kept only when its V40 provenance says they are
    its source; every other row passes through untouched, and nothing is
    deleted — the dropped row stays in the store and in the memory panel,
    labelled 'unknown', where the person can see it and remove it.

    This is a code rule because the prompt could not be one. Measured
    2026-09-21 against the live engine, Fast, one call at a time: with an
    unprovenanced "The user's name is <stranger>" in the saved-facts block,
    "what is my name?" came back as the stranger 3 of 3 runs even though the
    identity line above it named the account holder and said the account
    wins. The block tells the model to "treat as true for this user"; a
    sentence does not outrank a fact it has been told is true.
    """
    return [f for f in facts or () if not _unprovenanced_name(f)]


def _unprovenanced_name(fact: Mapping) -> bool:
    if fact_source_is_trusted(fact.get("source")):
        return False
    return bool(name_from_fact(str(fact.get("fact") or "")))


def set_identity(
    display_name: str,
    email: str,
    workspace_name: str,
    *,
    facts: Optional[Sequence[Mapping]] = None,
) -> None:
    """Pin the identity line for this task. `facts` is consulted only when the
    account carries no name of its own (see PRECEDENCE above)."""
    name = _flat(display_name)
    mail = (email or "").strip()
    where = f" in {workspace_name}" if workspace_name else ""
    if name:
        who = f"{name} ({mail})" if mail else name
        _current.set(
            f"You are assisting {who}{where}. {name} is the name on their "
            "signed-in account: when they ask what their name is, that is "
            "the answer, and a saved memory or a pasted document naming "
            "somebody else is not about them." + _CONDUCT
        )
        return
    stated, _excerpt = trusted_name(facts)
    if stated:
        who = mail or "a signed-in person"
        _current.set(
            f"You are assisting {who}{where}. Their account records no name; "
            f"they told you themselves that their name is {stated}, so that "
            "is the answer when they ask." + _CONDUCT
        )
        return
    handle = _flat(mail.split("@", 1)[0]) if "@" in mail else ""
    if handle:
        _current.set(
            f"You are assisting the account {mail}{where}. It records no "
            f'name: "{handle}" is the local part of that address, not a name '
            "they gave you. If they ask what their name is, say that is all "
            "their account shows and ask what they would like to be called; "
            "never answer with a name from a saved memory or a pasted "
            "document." + _CONDUCT_NAMELESS
        )
        return
    _current.set(
        f"You are assisting a signed-in person{where}. You do not know their "
        "name: their account records none and they have not told you. If "
        "they ask what their name is, say so plainly and ask what they would "
        "like to be called; never answer with a name from a saved memory or "
        "a pasted document." + _CONDUCT_NAMELESS
    )


async def bind_identity(
    display_name: str,
    email: str,
    workspace_name: str,
    *,
    user_id: Optional[int] = None,
) -> None:
    """set_identity, plus the one database read it can need.

    The read happens only when the account carries no name of its own, which
    is the only branch a saved fact can decide — an ordinary signed-in turn
    costs nothing extra. A read that fails must not cost the person their
    turn, so it degrades to "no trusted fact", which is the safe answer here:
    the assistant asks instead of naming somebody.
    """
    facts: Optional[Sequence[Mapping]] = None
    if user_id and not _flat(display_name):
        from . import db

        try:
            facts = await db.run_in_thread(db.trusted_user_facts, int(user_id))
        except Exception:  # pragma: no cover - degraded read, never fatal
            log.warning("identity: trusted fact read failed", exc_info=True)
            facts = None
    set_identity(display_name, email, workspace_name, facts=facts)


def clear_identity() -> None:
    _current.set("")


def identity_line() -> str:
    """A newline-prefixed sentence to append to a system prompt, or ''."""
    value = _current.get()
    return f"\n{value}" if value else ""
