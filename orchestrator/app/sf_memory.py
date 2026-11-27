"""Salesforce conversation memory: the platform half (design 2026-11-25 §3).

WHAT IT DOES. sf_assistant returns a `state` and a code-written `plan_line`
with each answer (contract v2). This module keeps them for the next turn,
with no frontend change:

    lookup   before the call: find the row of the answer this turn replies
             to, by walking back over the path the browser sent (§3.1)
    window   the recent turns the service gets: at most
             SF_ASSISTANT_MEMORY_PAIRS whole exchanges within
             SF_ASSISTANT_MEMORY_TOKENS, and never fewer turns than the v1
             call sends (§3.2)
    record   what the bridge puts on `Outcome.sf_memory` (§3.3)
    write    after the answer is stored: one row, then the summary fold
             when a Salesforce pair has left the window (§11.1)

It never reads inside `state`, with the one exception the design names
(§3.5): whether a stored state holds a pending clarification, read as the
presence of its top-level `pending` object and nothing else. The state is
sent back to the service exactly as stored.

THE BRANCH KEY. Every turn is hashed with `sf_isolation.answer_key`, the one
helper the toggle-off guard also uses. An assistant turn's row is the one
with its answer hash, the hash of the assistant turn before it on the path
as its parent ('' only for the first assistant turn of the path), and,
among identical siblings, the hash of the user turn before it (C1). The
parent is computed the same way when a row is written and when it is
looked up, transparent turns included, so a row written after a "thanks"
is found after that same "thanks".

TRANSPARENT TURNS (C3). A turn with no row whose user turn is a polite word
(`is_conversational`), or whose answer is one of the bridge's own lines that
read no data or one of the toggle-off guard's fixed replies, is stepped over
without counting as a gap. Any turn in between, transparent or not, ends a
pending clarification (`strict`).

WHO MAY SEE IT. Only the bridge and the main.py write hook import this
module, and only on a Salesforce-mode turn with SF_ASSISTANT_MEMORY on. The
store filters every read by user and chat. Nothing here reaches meta, the
browser or a log line: logs and the `sf_memory_events_total` metric carry
fixed codes, counts and sizes only.

NEVER FAILS A TURN. A read that fails gives no memory for this turn
(`LookupFailed`, already logged and counted), and the bridge then asks
exactly as it does with the switch off (v1). A write that fails is logged and
counted; the next lookup walks back over the turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import db, metrics, sf_isolation, sf_memory_store
from .config import settings
from .context import estimate_tokens

log = logging.getLogger(__name__)

#: The assistant turns of the path whose rows one lookup reads: the walk-back
#: (WALK_TURNS) plus one, so the fold can see the pairs just outside a full
#: window (§3.1 "the last 13 assistant turns").
FETCH_TURNS = 13
#: How far back the walk looks for the answer this turn replies to.
WALK_TURNS = 12
#: The largest gap the contract sends (contract v2 `gap`, 0-6). The walk stops
#: once more non-transparent answers than this lie between.
MAX_GAP = 6
#: Each turn sent to the service is cut to this, as v1 cuts it (contract v2
#: `recent_turns[].content`).
TURN_MAX_CHARS = 2000
#: The contract's own caps on what the bridge sends.
SUMMARY_MAX_CHARS = 8000
MAX_PAIRS = 6
#: contract v2 `recent_turns` takes at most this many turns.
MAX_TURNS = 2 * MAX_PAIRS
#: The turns the v1 call sends (`sfk_bridge._ASK_MAX_TURNS`): the window never
#: holds fewer, so a service in shadow or off, which reads the last four turns
#: it is sent, reads exactly what it reads with the switch off (design C31).
V1_TURNS = 4
#: The memory contract this bridge speaks.
CONTRACT = 2

#: `sf_memory_events_total{event}`: a closed set, so a typo never mints a
#: series. Lookups by outcome, writes by result, the fold, and the service
#: that answered a v2 call with the v1 refusal (§4.4).
EVENTS = (
    "lookup_found", "lookup_transparent", "lookup_gap", "lookup_none", "lookup_error",
    "lookup_skipped", "write_inserted", "write_duplicate", "write_error",
    "fold_spawned", "fold_error", "contract_mismatch",
)


def count(event: str) -> None:
    """Count one memory event (`EVENTS`); an unknown name counts as "other"."""
    metrics.inc(
        "sf_memory_events_total",
        "Salesforce conversation memory events (app/sf_memory.py).",
        event=event if event in EVENTS else "other",
    )


def enabled() -> bool:
    """SF_ASSISTANT_MEMORY (design §15.1, default false). Only a real True
    turns memory on: with anything else the bridge makes today's v1 call and
    nothing here reads or writes the database."""
    return getattr(settings, "sf_assistant_memory", False) is True


def key(text: Any) -> str:
    """The branch hash of one turn: `sf_isolation.answer_key`, the toggle-off
    guard's own helper, so the memory and the guard never disagree."""
    return sf_isolation.answer_key(text)


def _limits() -> Tuple[int, int]:
    """(pairs, tokens) of the window, held to the contract whatever a test
    or an operator set: 1-6 pairs, at least one token."""
    pairs = getattr(settings, "sf_assistant_memory_pairs", MAX_PAIRS)
    tokens = getattr(settings, "sf_assistant_memory_tokens", 6000)
    pairs = pairs if isinstance(pairs, int) and not isinstance(pairs, bool) else MAX_PAIRS
    tokens = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else 6000
    return min(MAX_PAIRS, max(1, pairs)), max(1, tokens)


# ------------------------------------------------------------------ path --


@dataclass
class Turn:
    """One assistant turn of the path, with the user turn just before it
    (None when the turn before it is not a user turn) and its three hashes.
    `row` is this path's row of that answer, when one was found.

    `earlier` are the user turns before `user` that no answer followed (a
    send that failed or was stopped before any text), oldest first. They are
    never hashed, so the branch key is as it was; the window sends them with
    this answer, as the v1 call sends every turn."""

    user: Optional[str]
    assistant: str
    answer_sha1: str
    parent_sha1: str
    question_sha1: str
    row: Optional[Dict[str, Any]] = None
    earlier: List[str] = field(default_factory=list)


def _turns(history: Any) -> List[Mapping[str, Any]]:
    """The user and assistant turns with text, as `sfk_bridge._recent_turns`
    keeps them: the same filter at write and at read keeps the parent hash
    the same."""
    return [
        turn for turn in (history or ())
        if isinstance(turn, Mapping)
        and turn.get("role") in ("user", "assistant")
        and isinstance(turn.get("content"), str)
    ]


def path_turns(history: Any, limit: int = FETCH_TURNS) -> List[Turn]:
    """The last `limit` assistant turns of the path, oldest first, each with
    its hashes. The parent of the oldest one is still the hash of the
    assistant turn before it on the whole path, so a row's parent never
    depends on how many turns are kept here."""
    turns = _turns(history)
    at = [index for index, turn in enumerate(turns) if turn["role"] == "assistant"]
    kept = at[-limit:] if limit > 0 else []
    first = len(at) - len(kept)
    parent = key(turns[at[first - 1]]["content"]) if first > 0 else ""
    out: List[Turn] = []
    for index in kept:
        before = turns[index - 1] if index > 0 else None
        user = before["content"] if before is not None and before["role"] == "user" else None
        earlier: List[str] = []
        if user is not None:
            back = index - 2
            while back >= 0 and turns[back]["role"] == "user":
                earlier.insert(0, turns[back]["content"])
                back -= 1
        answer = key(turns[index]["content"])
        out.append(Turn(
            user=user,
            assistant=turns[index]["content"],
            answer_sha1=answer,
            parent_sha1=parent,
            question_sha1=key(user) if user is not None else "",
            earlier=earlier,
        ))
        parent = answer
    return out


def unanswered(history: Any) -> List[str]:
    """The user turns after the last assistant turn of the path (every user
    turn when there is none), oldest first: questions no answer followed,
    because the send failed or was stopped before any text. The v1 call
    sends them, so the window does too."""
    turns = _turns(history)
    last = max((index for index, turn in enumerate(turns) if turn["role"] == "assistant"), default=-1)
    return [turn["content"] for turn in turns[last + 1:]]


# ---------------------------------------------------------------- window --


def _cut(text: Optional[str]) -> Optional[str]:
    return None if text is None else text[:TURN_MAX_CHARS]


def _group(pair: Mapping[str, Any]) -> List[Dict[str, str]]:
    """The turns of one pair as sent, oldest first, each cut to 2,000
    characters: the user turns no answer followed (`earlier`), the user turn,
    then the answer. The last pair of a path whose newest question was never
    answered has no answer (`assistant` None)."""
    out = [
        {"role": "user", "content": text[:TURN_MAX_CHARS]}
        for text in pair.get("earlier") or ()
        if isinstance(text, str)
    ]
    if isinstance(pair.get("user"), str):
        out.append({"role": "user", "content": pair["user"][:TURN_MAX_CHARS]})
    if isinstance(pair.get("assistant"), str):
        out.append({"role": "assistant", "content": pair["assistant"][:TURN_MAX_CHARS]})
    return out


def _pair_tokens(pair: Mapping[str, Any]) -> int:
    return sum(estimate_tokens(turn["content"]) for turn in _group(pair))


def window(pairs: Sequence[Mapping[str, Any]]) -> Tuple[list, list]:
    """(kept, left out), both oldest first (§3.2).

    A pair is one answer with every user turn since the answer before it
    (the question it answers, and any question no answer followed), or the
    user turns after the last answer. Kept: the newest whole pairs that fit
    SF_ASSISTANT_MEMORY_PAIRS, SF_ASSISTANT_MEMORY_TOKENS and the contract's
    12 turns. Pairs are dropped from the oldest end, never half a pair, and
    the first pair that does not fit ends the window (an older, shorter one
    is not taken in its place). Tokens are counted on the text as sent: each
    turn cut to 2,000 characters, measured with `context.estimate_tokens`.

    THE FLOOR. Whatever the budget, the window keeps the newest whole pairs
    that hold the V1_TURNS turns the v1 call sends. A service in shadow or
    off reads the last four turns it is sent, so turning the platform switch
    on never changes what it reads (design C31, §15.2 step 4); the service's
    own cap applies again when its memory is on (C32)."""
    max_pairs, max_tokens = _limits()
    pairs = list(pairs)
    taken = turns = tokens = 0
    for pair in reversed(pairs):
        size = len(_group(pair))
        cost = _pair_tokens(pair)
        if taken >= max_pairs or turns + size > MAX_TURNS or tokens + cost > max_tokens:
            break
        taken += 1
        turns += size
        tokens += cost
    while taken < len(pairs) and turns < V1_TURNS:
        turns += len(_group(pairs[len(pairs) - 1 - taken]))
        taken += 1
    split = len(pairs) - taken
    return pairs[split:], pairs[:split]


def turns_for_service(pairs: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """The window's pairs as contract `recent_turns`, oldest first: the turns
    of each pair as `{role, content}`, each cut to 2,000 characters, at most
    the contract's 12 (the oldest go first, which only a run of unanswered
    questions longer than that can need)."""
    out: List[Dict[str, str]] = []
    for pair in pairs:
        out.extend(_group(pair))
    return out[-MAX_TURNS:]


# ---------------------------------------------------------------- lookup --


def _has_pending(state: Any) -> bool:
    """Does a stored state hold a pending clarification? The one look inside a
    state this module takes (design §3.5): whether its top-level `pending`
    is a non-empty object. Nothing in it is read."""
    if not isinstance(state, Mapping):
        return False
    pending = state.get("pending")
    return isinstance(pending, Mapping) and bool(pending)


def _code(value: Any) -> str:
    """A reply's fixed code (`kind`, `reason`), or '' for anything else."""
    if not isinstance(value, str):
        return ""
    value = value.strip().lower()
    return value if value and len(value) <= 32 and value.replace("_", "").isalpha() else ""


@dataclass
class Memory:
    """What one lookup found for this turn.

    `turns` are the last FETCH_TURNS assistant turns of the path, each with
    its row when it has one on this path. `parent` is the row of the answer
    this turn replies to, or None; `gap` the answers with no row between it
    and this turn (transparent ones not counted); `strict` whether it is the
    very last assistant turn. `question` and `question_sha1` are this turn's
    question as received; `parent_sha1` is the hash of the last assistant
    turn of the path, the parent of the answer this turn writes. `trailing`
    are the path's user turns after its last answer (`unanswered`)."""

    question: str
    question_sha1: str
    parent_sha1: str
    turns: List[Turn] = field(default_factory=list)
    parent: Optional[Dict[str, Any]] = None
    matched: Optional[Turn] = None
    gap: int = 0
    strict: bool = False
    #: Hashes of the bridge's own lines whose rows carry their parent's state
    #: (the retry line): their pending is no question this turn answers.
    carried: frozenset = frozenset()
    trailing: List[str] = field(default_factory=list)

    @property
    def outcome(self) -> str:
        """found | transparent | gap | none: the trace and metric code."""
        if self.parent is None:
            return "none"
        if self.gap:
            return "gap"
        return "found" if self.strict else "transparent"

    @property
    def pending(self) -> bool:
        """Is this turn the reply to a pending clarification? Only when the
        row is the very last assistant turn (`strict`), its answer is the
        service's own (not the bridge's retry line, which carries a copy of
        its parent's state), and its state holds a pending question (§3.5).
        Then the bridge's conversational rules let a "yes" or an "ok" through
        to the service."""
        return (
            self.parent is not None
            and self.strict
            and self.matched is not None
            and self.matched.answer_sha1 not in self.carried
            and _has_pending(self.parent.get("state"))
        )

    def pairs(self) -> List[Dict[str, Any]]:
        """The path's recent pairs, oldest first, as the window and the fold
        read them: the texts (cut as sent; `earlier` holds the questions no
        answer followed), whether the answer has a row on this path (a
        Salesforce pair, §11.2), and that row's plan line. The path's
        unanswered questions after its last answer are a last pair with no
        answer. `kind` and `clarify` of an earlier answer are not kept
        anywhere the bridge may read (open point 14: never from inside
        `state`), so they are ''."""
        pairs = [
            {
                "earlier": [_cut(text) for text in turn.earlier],
                "user": _cut(turn.user),
                "assistant": _cut(turn.assistant),
                "salesforce": turn.row is not None,
                "plan_line": str((turn.row or {}).get("plan_line") or ""),
                "kind": "",
                "clarify": "",
            }
            for turn in self.turns
        ]
        if self.trailing:
            pairs.append({
                "earlier": [_cut(text) for text in self.trailing[:-1]],
                "user": _cut(self.trailing[-1]),
                "assistant": None,
                "salesforce": False,
                "plan_line": "",
                "kind": "",
                "clarify": "",
            })
        return pairs

    def recent_turns(self) -> List[Dict[str, str]]:
        """The contract `recent_turns` of this call: the window over the path."""
        kept, _left_out = window(self.pairs())
        return turns_for_service(kept)

    def arguments(self) -> Dict[str, Any]:
        """The contract-v2 keys of this call, beside `question`,
        `recent_turns` and `request_id`: the parent row's state as stored,
        the gap, its summary, and the reserved access profile (null today,
        C17). With no parent row: no state, gap 0, no summary."""
        parent = self.parent or {}
        state = parent.get("state")
        summary = parent.get("summary")
        return {
            "contract": CONTRACT,
            "state": state if isinstance(state, dict) else None,
            "gap": self.gap if self.parent is not None else 0,
            "summary": summary[:SUMMARY_MAX_CHARS] if isinstance(summary, str) else "",
            "access_profile": None,
        }

    def path_pairs(self) -> int:
        """Salesforce pairs on the path through the answer this turn writes
        (C5): the parent row's count plus one, or, with no parent row, the
        rows found on this path plus one. Never from how much history the
        client sent."""
        if self.parent is not None:
            parent_pairs = self.parent.get("path_pairs")
            if isinstance(parent_pairs, int) and not isinstance(parent_pairs, bool) and parent_pairs >= 0:
                return parent_pairs + 1
        return 1 + sum(1 for turn in self.turns if turn.row is not None)

    def _record(self, *, contract: int, state: Any, plan_line: str, reason: str, kind: str,
                clarify: str) -> Dict[str, Any]:
        parent = self.parent or {}
        summary = parent.get("summary")
        folded = parent.get("summary_pairs")
        return {
            "contract": contract,
            "state": state if isinstance(state, dict) else None,
            "plan_line": plan_line,
            "reason": reason,
            "kind": kind,
            "clarify": clarify,
            "question": self.question[:TURN_MAX_CHARS],
            "question_sha1": self.question_sha1,
            "parent_sha1": self.parent_sha1,
            "summary": summary if isinstance(summary, str) else "",
            "summary_pairs": folded if isinstance(folded, int) and not isinstance(folded, bool) else 0,
            "summary_at": parent.get("summary_at"),
            "path_pairs": self.path_pairs(),
            "pairs": self.pairs(),
        }

    def record(self, reply: Mapping[str, Any], answer: str) -> Optional[Dict[str, Any]]:
        """`Outcome.sf_memory` for the service's reply, or None when the
        reply carries no `contract: 2` (a v1 service, or one answering as
        v1): no row then. `reply` holds the keys read BEFORE the bridge's
        fixed-line reset (C2). The clarify text of a clarifying answer is
        the answer as shown; the fold keeps only its question part."""
        contract = reply.get("contract")
        if type(contract) is not int or contract != CONTRACT:
            return None
        plan_line = reply.get("plan_line")
        reason = _code(reply.get("reason"))
        return self._record(
            contract=CONTRACT,
            state=reply.get("state"),
            plan_line=plan_line if isinstance(plan_line, str) else "",
            reason=reason,
            kind=_code(reply.get("kind")),
            clarify=answer if reason == "clarify" and isinstance(answer, str) else "",
        )

    def carry(self) -> Optional[Dict[str, Any]]:
        """`Outcome.sf_memory` for the bridge's retry line (§3.3): a copy of
        the parent row's state and plan line, so the plan survives a failed
        turn. Only when that row is the answer this turn replies to (gap 0):
        a copy would otherwise make an earlier plan look like the last one.
        None without such a row."""
        if self.parent is None or self.gap != 0:
            return None
        contract = self.parent.get("contract")
        if type(contract) is not int or not 1 <= contract <= 32767:
            return None
        plan_line = self.parent.get("plan_line")
        return self._record(
            contract=contract,
            state=self.parent.get("state"),
            plan_line=plan_line if isinstance(plan_line, str) else "",
            reason="",
            kind="",
            clarify="",
        )

    def trace(self) -> Dict[str, Any]:
        """The trace event's details: codes and sizes only (§13)."""
        state = (self.parent or {}).get("state")
        summary = (self.parent or {}).get("summary")
        return {
            "outcome": self.outcome,
            "gap": self.gap,
            "strict": self.strict,
            "pending": self.pending,
            "turns_read": len(self.turns),
            "rows_on_path": sum(1 for turn in self.turns if turn.row is not None),
            "state_keys": len(state) if isinstance(state, dict) else 0,
            "summary_chars": len(summary) if isinstance(summary, str) else 0,
        }


def _transparent(turn: Turn, own: frozenset) -> bool:
    """A turn that did nothing with data (C3): one of the bridge's own lines
    or a toggle-off fixed reply as its answer, or a polite word as its
    question."""
    if turn.answer_sha1 in own:
        return True
    if turn.user is None:
        return False
    from .engines.sf_intel import is_conversational

    return is_conversational(turn.user)


def walk(memory: Memory, own: frozenset) -> Memory:
    """Find the parent row among `memory.turns` (rows already picked per
    turn), newest first, at most WALK_TURNS turns, and set `parent`,
    `matched`, `gap` and `strict` (§3.1). Pure."""
    gap = 0
    strict = True
    for turn in reversed(memory.turns[-WALK_TURNS:]):
        if turn.row is not None:
            memory.parent, memory.matched, memory.gap, memory.strict = turn.row, turn, gap, strict
            return memory
        strict = False
        if _transparent(turn, own):
            continue
        gap += 1
        if gap > MAX_GAP:
            break
    memory.parent, memory.matched, memory.gap, memory.strict = None, None, 0, False
    return memory


def _user_id(principal: Any) -> Optional[int]:
    value = getattr(principal, "user_id", None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class LookupFailed(Exception):
    """The memory read failed. It is already logged and counted
    (`lookup_error`) with codes only, and carries no text of its own: the
    bridge asks as v1 for this turn and records the trace outcome `error`
    (design §13, §14)."""


async def lookup(
    principal: Any,
    request: Any,
    *,
    transparent: Sequence[str] = (),
    carried: Sequence[str] = (),
) -> Optional[Memory]:
    """The memory of this turn, or None when there is none to use (the
    switch is off, or the asker or the chat is unknown). Raises
    `LookupFailed` when the read failed (then the bridge asks as v1, §14).

    ONE indexed read (`fetch_candidates`) for the last FETCH_TURNS assistant
    turns of the path the client sent, then a pick per turn with its exact
    parent and question hash (`pick`), then the walk-back. Only server-side
    rows of this user's own chat are read; nothing is taken from the client
    but the path. `transparent` are the bridge's own lines that read no data
    (the toggle-off guard's fixed replies are added here); `carried` its
    lines whose rows copy their parent's state."""
    if not enabled():
        return None
    user_id = _user_id(principal)
    conversation_id = getattr(request, "conversation_id", None)
    if user_id is None or not isinstance(conversation_id, str) or not conversation_id:
        count("lookup_skipped")
        return None
    history = getattr(request, "history_messages", None) or ()
    question = getattr(request, "text", "") or ""
    turns = path_turns(history)
    memory = Memory(
        question=question,
        question_sha1=key(question),
        parent_sha1=turns[-1].answer_sha1 if turns else "",
        turns=turns,
        carried=frozenset(key(line) for line in carried),
        trailing=unanswered(history),
    )
    try:
        rows = await db.run_in_thread(
            sf_memory_store.fetch_candidates,
            user_id,
            conversation_id,
            [turn.answer_sha1 for turn in turns],
            access_profile=None,
        )
    except Exception as exc:  # noqa: BLE001 — a read that fails costs memory, never the turn
        _failed("lookup", exc, conversation_id)
        # `from None`: the cause may quote a row (a CHECK's DETAIL); nothing
        # may carry it further than `_failed`'s codes.
        raise LookupFailed() from None
    for turn in turns:
        turn.row = sf_memory_store.pick(
            rows,
            answer_sha1=turn.answer_sha1,
            parent_sha1=turn.parent_sha1,
            question_sha1=turn.question_sha1,
            access_profile=None,
        )
    own = frozenset(key(line) for line in (*transparent, *sf_isolation.FIXED_REPLIES))
    walk(memory, own)
    count(f"lookup_{memory.outcome}")
    log.debug(
        "sf_memory: lookup conversation=%s outcome=%s gap=%d strict=%s rows=%d",
        conversation_id, memory.outcome, memory.gap, memory.strict, len(rows),
    )
    return memory


# ----------------------------------------------------------------- write --


def _failed(op: str, exc: BaseException, conversation_id: Any) -> None:
    """One safe log line and a count. A StoreError's text names the operation
    and an SQLSTATE only; any other exception is named by its class."""
    reason = str(exc) if isinstance(exc, sf_memory_store.StoreError) else type(exc).__name__
    log.warning("sf_memory: %s failed: conversation=%s %s", op, conversation_id, reason)
    count(f"{op}_error")


def next_pairs(record: Mapping[str, Any], row: Mapping[str, Any], answer: str) -> list:
    """The pairs the NEXT call's window is built from once this answer is on
    the path, oldest first: this turn's pairs, then the new answer with its
    question. The questions no answer followed at the end of this turn's
    path join the new answer's pair (`earlier`), as they will in the next
    call's path."""
    pairs = list(record.get("pairs") or ())
    earlier: List[str] = []
    if pairs and pairs[-1].get("assistant") is None:
        last = pairs.pop()
        earlier = [*(last.get("earlier") or ()), *([last["user"]] if isinstance(last.get("user"), str) else [])]
    new = {
        "earlier": earlier,
        "user": _cut(record.get("question") or ""),
        "assistant": _cut(answer),
        "salesforce": True,
        "plan_line": str(row.get("plan_line") or ""),
        "kind": record.get("kind") or "",
        "clarify": record.get("clarify") or "",
    }
    return [*pairs, new]


def fold_pairs(record: Mapping[str, Any], row: Mapping[str, Any], answer: str) -> list:
    """The pairs the summary fold of a new row takes (§11.1), oldest first,
    or [] when no fold is due.

    Counted over the window THROUGH THE NEW ANSWER, the one the next call
    will send, so a pair that has just left it is folded now and never sits
    a turn in neither. Only Salesforce pairs (answers with a row on this
    path) count and are folded: the question the answer replied to and that
    row's plan line (never answer text, never anything from inside `state`,
    and never a question no answer followed). The new answer is always in
    that window (its floor), so its own kind and clarify text, kept in the
    record, reach no fold: once its pair leaves a later window it is an
    earlier pair, with '' for both (open point 14)."""
    from . import sf_summary

    kept, left_out = window(next_pairs(record, row, answer))
    outside = [
        {
            "question": pair.get("user") or "",
            "plan_line": pair.get("plan_line") or "",
            "kind": pair.get("kind") or "",
            "clarify": pair.get("clarify") or "",
        }
        for pair in left_out
        if pair.get("salesforce")
    ]
    return sf_summary.pairs_due(
        outside,
        path_pairs=row["path_pairs"],
        summary_pairs=row["summary_pairs"],
        window_pairs=sum(1 for pair in kept if pair.get("salesforce")),
    )


def _spawn_fold(record: Mapping[str, Any], row: Mapping[str, Any], answer: str) -> None:
    """Start the summary fold of a row the insert returned (C27), when
    SF_ASSISTANT_SUMMARY is on and a Salesforce pair has left the window.
    The row's owner is named to the fold (review DS-5). Never raises."""
    from . import sf_summary

    try:
        if not sf_summary.enabled():
            return
        due = fold_pairs(record, row, answer)
        if not due:
            return
        task = sf_summary.spawn_fold(
            row["conversation_id"],
            row["id"],
            row["summary"],
            row["summary_pairs"],
            due,
            user_id=row["user_id"],
        )
        if task is not None:
            count("fold_spawned")
    except Exception as exc:  # noqa: BLE001 — the answer is stored; the next row catches up
        log.warning("sf_memory: fold not started: row=%s %s", row.get("id"), type(exc).__name__)
        count("fold_error")


async def write(
    record: Any,
    *,
    user_id: Any,
    conversation_id: Any,
    generation_id: Any,
    answer: Any,
    workspace_id: Any = "",
) -> Optional[Dict[str, Any]]:
    """Write the row of one stored answer and return it, or None when nothing
    was written (§3.3). Never raises.

    `record` is the bridge's `Outcome.sf_memory`; `answer` is the text as
    stored (with any tail main.py added), so its hash is the one the browser
    sends back. The insert writes nothing for a generation that already has a
    row or a chat that is not this user's; only a row it returns may start a
    summary fold. A failure is logged and counted, and the next lookup walks
    back over this turn (a gap, never a wrong plan)."""
    if not enabled() or not isinstance(record, Mapping) or not isinstance(answer, str) or not answer:
        return None
    try:
        row = await db.run_in_thread(
            sf_memory_store.insert,
            user_id=user_id,
            conversation_id=conversation_id,
            generation_id=generation_id,
            answer_sha1=key(answer),
            contract=record["contract"],
            parent_sha1=record["parent_sha1"],
            question_sha1=record["question_sha1"],
            workspace_id=workspace_id if isinstance(workspace_id, str) else "",
            access_profile=None,
            state=record["state"],
            plan_line=record["plan_line"],
            summary=record["summary"],
            summary_pairs=record["summary_pairs"],
            summary_at=record["summary_at"],
            path_pairs=record["path_pairs"],
        )
    except Exception as exc:  # noqa: BLE001 — memory never fails a turn
        _failed("write", exc, conversation_id)
        return None
    if row is None:
        count("write_duplicate")
        return None
    count("write_inserted")
    _spawn_fold(record, row, answer)
    return row
