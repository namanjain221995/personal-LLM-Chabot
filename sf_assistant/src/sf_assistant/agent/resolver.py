"""The follow-up resolver of Salesforce conversation memory (design v2 of 2026-11-25, section 6; lane S2): it decides
how a question goes on from the parent answer's query state, and replaces the follow-up rewrite of the ask service
when ``SFA_MEMORY=on`` (``web/ask_mcp.py`` ``AskService._job`` calls :func:`resolve` where it calls ``rewrite`` today;
the page has no store and always passes ``state=None``). With ``off`` or ``shadow`` the rewrite runs exactly as today,
and shadow calls :func:`shadow`: the code steps only, no model call, for the log.

It holds no state between jobs and is pure apart from one model call through the existing LLM wrapper (``llm.chat``,
as ``agent/followup.py`` makes its call). The state is read with ``agent/query_state.py`` (lane S1): :func:`check
<sf_assistant.agent.query_state.check>` first, ``usable`` for "a usable plan", ``apply`` for every patch,
``terms_for`` for the terms of the run and ``plan_line`` for the words. The words code writes are in
``prompts/followup_resolve.md``, after its marker line (:data:`LINES`); the text before that line is the system prompt
of the call (:data:`FOLLOWUP_RESOLVE_SYSTEM`).

**A usable plan** (§6.2): the state holds a plan, ``gap`` is 0, the plan's age is at most ``max_age``
(``SFA_MEMORY_MAX_AGE``, default 2) and every stored field still resolves. The model may patch a plan whose age is
within ``max_age`` also when ``gap`` is above 0 or the age is 1 or 2, but such a patch never runs silently: it becomes
a confirm clarify ("Do you mean: <plan line>? 1. Yes 2. No, I'll ask in full"; C4, D10, D13). A model patch that
moves the period to words the person did not type confirms too (L5). A reply given at a gap (a confirm, an unclear
reply, small talk) hands the caller that plan at least one answer old, so the next patch of it confirms again (M8). A
plan older than ``max_age`` is not shown to the model: the question is read as text only.

**The order of steps; the first one that decides wins (C11):**

0. **Small talk** (prerequisite P6, ``agent/small_talk.py``): its fixed line; the state keeps the plan and drops the
   pending clarification.
1. **A pending clarification** (``state.pending``), used only when it is strict: ``gap`` is 0 and the last assistant
   turn is the clarifying question (it holds the pending's ``asked`` text; the contract has no strict key, so the
   caller may pass ``strict`` or it is read from the turns). A digit 1-4, an ordinal (first, second, pehla, doosra,
   ...) or a label (the reply equals a label, else a label's first word, case and spacing aside; an edit distance
   of 1 only for labels of 6 characters or more, so "no" never matches "ok" or "so", C12) picks a choice: its ops are
   applied (no confirm: the person just chose), or its question runs as a new question with the person's own
   question as the typed words. A reply of yes or acknowledgement words ("yes please", "ok", "sure", "theek hai") or
   of no words picks the choice of a confirm, and is read before step 0, so an "ok" to a confirm is a yes, not small
   talk. "No, I'll ask in full" gives the caller's unclear line and keeps the plan. A reply that is a complete
   question of its own goes on; anything else goes to the model with the choices in its context. With no pending in
   the state, a reply to a clarifying question of prerequisite P2 in the last turn is still read by
   ``agent.clarify.choice_reply``, as the rewrite reads it today.
2. **Code shortcuts** (``engine/followup_shortcuts.py``; a usable plan of age 0, no pending, ``SFA_MEMORY_SHORTCUTS``):
   display (the display words of ``agent/display.py``, stop words included: "without the chart" is ``chart:
   false``, C14), top N, another period, by month; English and Hinglish. Before step 3, since a shortcut leaves only
   filler, it never swallows a question with a subject.
3. **Stands alone**, decided by code with no model call, only when there is no plan the model may patch (none, or
   one older than ``max_age``) and the question is complete (``followup._complete``, not ``_fragment``), or when
   there is such a plan and ``followup._another_subject`` says the last question was never its subject ("and what is
   the leave policy?") and the rest asks something of its own (an org word, or an ask word with a record noun of its
   own: "and only for Pune" is a filter, "and what about candidates in Pune" and "and who had the most?" go on from
   the plan). After a gap or with a plan one or two answers old, a complete follow-up goes to the model, so the gap
   and age rules can confirm (M6). A
   first message (no turn, no plan) and an ask about what the answers can show ("can you do visualizations ?",
   ``display.generic_ask``) run as typed too, as with the rewrite today. "Only for North America", "show the top 10"
   and "Why is recruiter A performing lower than recruiter B?" are never decided here when a plan is usable.
4. **The model** (one call, :func:`build_messages`): the system prompt, the plan (its line and one line per call with
   its arguments), the summary, the pending choices, up to 6 pairs of turns and the question, within ``max_tokens``
   (``SFA_MEMORY_TOKENS``, default 12000, whatever the bridge sent; C32): a prompt that cannot be cut to fit is never
   sent, and step 5 answers (F4). It returns ``{"kind", "standalone", "ops"}``
   (:func:`parse_reply`; anything else is unclear). Code then checks it: ``new`` and ``about_answer`` need a
   ``standalone`` that passes the v6 rewrite's own reading of a reply (``followup.rewrite`` on the last 4 turns, the
   binding checks of ``_unbound`` included: the model gets 12 turns, the checks read 4); a ``patch`` goes through
   ``query_state.apply`` (fields, typed values, the filter cap, the term contracts with the rebuilt terms, the call
   cap, contact fields). A failed check asks (a clarify); a value the person did not type becomes a did-you-mean only
   when it is a picklist value or a record type of its field, never a name or a figure from an answer (F8).
5. **Fallback**: ``unclear`` with a plan is a clarify built from the plan (the model's readings as choices when they
   pass the checks); without a plan it is the caller's unclear line, as today.

The resolver never runs anything: it returns a :class:`Resolution` (what to run, or the reply to give), and the
caller (lane S3) runs it and builds the next state (``query_state.build``). It never raises: a fault of its own is
status ``failed`` with reason ``resolver_error``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import re
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable

from sf_assistant import prompts as _prompt_files
from sf_assistant.agent import clarify as _clarify
from sf_assistant.agent import display as _display
from sf_assistant.agent import followup as _followup
from sf_assistant.agent import query_state as qs
from sf_assistant.agent.checks import strip_thinking
from sf_assistant.agent.merge import _finish_reason
from sf_assistant.agent.record_session import is_local_endpoint
from sf_assistant.agent.small_talk import line as _talk_line
from sf_assistant.agent.small_talk import small_talk
from sf_assistant.config import LIMITS
from sf_assistant.engine import followup_shortcuts as _shortcuts
from sf_assistant.engine.contact import asks_for_contact
from sf_assistant.engine.term_contracts import Terms
from sf_assistant.knowledge.resolver import osa_distance

__all__ = ["FOLLOWUP_RESOLVE_TEXT", "FOLLOWUP_RESOLVE_SYSTEM", "LINES", "RESOLVE_MAX_TOKENS", "MAX_TURNS",
           "MAX_TURN_CHARS", "MAX_SUMMARY_CHARS", "MAX_TOKENS", "PLAN_TOKENS", "SUMMARY_TOKENS", "PAIRS_TOKENS",
           "MAX_AGE", "MAX_CALLS", "MAX_GAP", "ACTIONS", "REASONS", "MODEL_KINDS", "Resolution", "resolve", "shadow",
           "build_messages", "parse_reply", "choice_for", "render_clarify", "pending_from_clarify", "recent_turns",
           "estimate_tokens"]

FOLLOWUP_RESOLVE_TEXT = _prompt_files.text("followup_resolve")
#: The line that ends the system prompt in prompts/followup_resolve.md; what follows it never reaches the model.
_MARK = "\n==== lines code writes, never sent to the model ====\n"
FOLLOWUP_RESOLVE_SYSTEM, _LINES_TEXT = FOLLOWUP_RESOLVE_TEXT.split(_MARK, 1)
#: key -> the line code writes (prompts/followup_resolve.md, after the marker line)
LINES: dict[str, str] = dict(ln.split(": ", 1) for ln in _LINES_TEXT.split("\n") if ": " in ln)
#: The reply: one question of at most 1,000 characters and at most 3 operations (the rewrite's own limit).
RESOLVE_MAX_TOKENS = LIMITS.followup_max_tokens
#: The turns the model may read: 6 pairs (followup.MAX_TURNS stays 4 for the rewrite and the binding checks; C31).
MAX_TURNS = 12
MAX_TURN_CHARS = _followup.MAX_TURN_CHARS
#: The contract's cap on the summary (design §4.1).
MAX_SUMMARY_CHARS = 8000
#: The prompt cap (``SFA_MEMORY_TOKENS``) and the caps of its parts (§6.3).
MAX_TOKENS = 12000
PLAN_TOKENS, SUMMARY_TOKENS, PAIRS_TOKENS = 1000, 600, 6000
#: ``SFA_MEMORY_MAX_AGE`` and ``SFA_MEMORY_MAX_CALLS`` (the state core's defaults).
MAX_AGE, MAX_CALLS = qs.MAX_AGE, qs.MAX_CALLS
#: The largest gap the contract sends; an unreadable gap reads as one above it.
MAX_GAP = 6
#: What the caller does: ``reply`` (give ``text`` or the status line; run nothing), ``new`` (run ``question`` as a
#: question of its own), ``patch`` (run ``plan``, the patched plan, as seeded calls), ``about_answer`` (re-run the
#: parent ``plan`` unchanged and answer ``question`` about the last answer). ``model`` only from :func:`shadow`: the
#: code steps did not decide, so the model would be called.
ACTIONS = ("reply", "new", "patch", "about_answer", "model")
#: Fixed codes of what decided (never free text).
REASONS = ("small_talk", "clarify_choice", "ask_full", "shortcut", "new", "patch", "about_answer", "clarify",
           "unclear_rewrite", "call_failed", "not_local", "resolver_error", "model")
#: The kinds the model may reply.
MODEL_KINDS = ("new", "patch", "about_answer", "unclear")
_REPLY_KEYS = frozenset({"kind", "standalone", "ops"})
_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
#: Ordinals of a reply to a clarifying question: the clarifying question's own (agent/clarify.py), and a fourth.
_ORDINALS = {**_clarify._ORDINALS, "fourth": 4, "4th": 4, "four": 4, "chautha": 4, "chauthi": 4, "chauthe": 4}
#: Words that make the rest behind a lead ask something of its own (step 3, :meth:`_Job._another_subject`).
_ASK_WORDS = frozenset("""what whats who whom whose which how when where why kya kaun kitne kitna kitni kab kahan kyun
kyon""".split())
_YES_WORDS = frozenset(w.strip() for w in LINES["yes_words"].split(","))
#: Acknowledgement words: a yes to a confirm ("ok", "sure", "theek hai"; stage-1 review fix M5), small talk otherwise.
_ACK_WORDS = frozenset(w.strip() for w in LINES["ack_words"].split(","))
_NO_WORDS = frozenset(w.strip() for w in LINES["no_words"].split(","))
#: Courtesy words a yes or a no may carry ("yes please"; stage-1 review fix L4).
_COURTESY = frozenset(w.strip() for w in LINES["courtesy_words"].split(","))
#: Words that rank the records of the last answer ("and who had the most in Pune?"): such a question goes on from the
#: plan, so step 3 never decides it (stage-1 review fix M7).
_RANKING_WORDS = frozenset("""most least top highest lowest best worst maximum minimum fewest sabse""".split())
#: Words of a period phrase that say nothing of which period it is (the L5 check of a model's period).
_PERIOD_FRAME = frozenset("the for in of during on to a an".split())


def _say(key: str, plan: str = "", field: str = "", value: str = "") -> str:
    """A line of the prompt file, filled (one pass)."""
    return _prompt_files.fill(LINES[key], PLAN=plan, FIELD=field, VALUE=value)


def _norm(text: Any) -> str:
    return " ".join(str(text or "").casefold().split())


# ------------------------------------------------------------------------------------------------ the result
@dataclass(frozen=True)
class Resolution:
    """What the resolver decided for one question.

    ``action`` (:data:`ACTIONS`) says what the caller does; ``reason`` (:data:`REASONS`) and ``detail`` (a fixed word:
    which rule fired) are for the reply's ``reason`` and the log; ``step`` is the step of §6.2 that decided (0-5).

    - ``reply``: ``status`` ``answered`` with ``text`` (the small-talk line or a clarifying question), ``unclear``
      (the caller's unclear line: the person asks the full question) or ``failed``; ``state_kind`` is the answer
      kind for ``query_state.build`` (``small_talk``, ``clarify`` or ``unclear``) and ``pending`` the pending
      clarification a clarify stores (None for one without choices).
    - ``new``: run ``question``; ``terms`` are its terms (``terms_kind`` "new": the chain starts again).
    - ``patch``: run ``plan`` (``patched.plan``) through ``query_state.run_calls(patched, today)`` as seeded calls,
      with ``terms``; ``question`` is the checked standalone question or the plan line; ``plan_line`` is the
      "Read as:" line; ``patched.display_only`` means only ``show`` changed.
    - ``about_answer``: re-run the parent ``plan`` unchanged as seeded calls and answer ``question`` from them;
      ``earlier_answer`` is the last answer's text (at most 2,000 characters) to give the record agent as data.

    ``typed`` is what counts as the person's typed words of this run (``query_state.next_terms`` and the typed-value
    rule; for a picked choice, the question the clarification was for). ``checked`` is the parent state as read
    (``query_state.check``); every kind of answer builds the next state from it (a reply given at a gap holds its plan
    at least one answer old, M8). ``model_called`` and
    ``prompt_tokens`` (the estimated size of the resolver prompt; in shadow, the size it would have) are for the
    timeline. ``reply`` is the model's text behind a refusal, for the private log only."""

    action: str
    reason: str
    step: int
    status: str = "answered"
    text: str = ""
    question: str = ""
    typed: str = ""
    terms: Terms | None = None
    terms_kind: str = "patch"
    checked: qs.Checked = field(default_factory=lambda: qs.Checked(None))
    patched: qs.Patched | None = None
    plan: dict | None = None
    plan_line: str = ""
    pending: dict | None = None
    state_kind: str = ""
    ops: tuple[dict, ...] = ()
    op: str | None = None
    earlier_answer: str = ""
    detail: str = ""
    error: str = ""
    model_called: bool = False
    prompt_tokens: int = 0
    info: dict = field(default_factory=dict, compare=False)
    reply: str = field(default="", compare=False, repr=False)

    def log(self) -> dict:
        """The private log entry: fixed codes and numbers only (no question, no turn, no value, no answer text)."""
        out = {"action": self.action, "reason": self.reason, "step": self.step, "status": self.status,
               "detail": self.detail, "model": self.model_called, "prompt_tokens": self.prompt_tokens,
               "ops": [next(iter(o)) for o in self.ops], "pending": self.pending is not None,
               "state": self.checked.reason or ("ok" if self.checked.state else "none"),
               "missing": len(self.checked.missing)}
        out.update({k: v for k, v in self.info.items() if isinstance(v, (bool, int, str)) and k not in out})
        if self.error:
            out["error"] = self.error
        return out


# ------------------------------------------------------------------------------------------------ turns and size
def estimate_tokens(text: Any) -> int:
    """The token estimate of the platform's context code (orchestrator ``context.estimate_tokens``): 3 characters a
    token for ASCII text, 2 bytes a token for the rest, plus one. Both sides cap by the same measure."""
    text = str(text or "")
    if text.isascii():
        return int(len(text) / 3.0) + 1
    ascii_chars = len(text.encode("ascii", "ignore"))
    other = len(text.encode("utf-8", "surrogatepass")) - ascii_chars
    return int(ascii_chars / 3.0 + other / 2.0) + 1


_SYSTEM_TOKENS = estimate_tokens(FOLLOWUP_RESOLVE_SYSTEM)


def recent_turns(turns: Any, max_turns: int = MAX_TURNS) -> list[dict]:
    """The turns the model may read: roles user and assistant only, no empty one, each cut to
    :data:`MAX_TURN_CHARS` and scrubbed of chat control tokens (``followup.usable_turns``, with the resolver's own
    window of ``max_turns``)."""
    if not isinstance(turns, (list, tuple)):
        return []
    kept = []
    for t in turns:
        if isinstance(t, dict) and t.get("role") in ("user", "assistant") and isinstance(t.get("content"), str):
            content = _followup._scrubbed(t["content"][:MAX_TURN_CHARS])
            if content.strip():
                kept.append({"role": t["role"], "content": content})
    return kept[-max_turns:] if max_turns > 0 else []


def _pairs(turns: list[dict]) -> list[list[dict]]:
    """The turns as pairs, oldest first: each user turn starts one, the assistant turns after it join it (an
    assistant turn before any user turn is a pair of its own). The window drops whole pairs, never half of one."""
    out: list[list[dict]] = []
    for t in turns:
        if t["role"] == "user" or not out:
            out.append([t])
        else:
            out[-1].append(t)
    return out


def _cut_tokens(text: str, tokens: int) -> str:
    """``text`` cut at a word boundary to at most ``tokens`` by :func:`estimate_tokens`."""
    if estimate_tokens(text) <= tokens:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= tokens:
            lo = mid
        else:
            hi = mid - 1
    head = text[:lo]
    return head.rsplit(" ", 1)[0] if " " in head else head


def _plan_lines(plan: dict | None, line: str) -> list[str]:
    """The plan in the call: its line, then one line per call (role, tool and the arguments as JSON)."""
    if not plan:
        return []
    out = [_followup._scrubbed(" ".join(str(line or "").split()))]
    calls = [c for c in plan.get("calls") or [] if isinstance(c, dict)]
    for c in sorted(calls, key=lambda c: c.get("role") != "main"):   # the main call first: a cut drops the sides
        args = _followup._scrubbed(json.dumps(c.get("args") or {}, ensure_ascii=False, sort_keys=True))
        out.append(f"{c.get('role', 'side')}: {c.get('tool', '')} {args}")
    return out


def build_messages(question: str, turns: Any, *, plan: dict | None = None, plan_line: str = "",
                   earlier: bool = False, summary: str = "", pending: dict | None = None,
                   max_tokens: int = MAX_TOKENS) -> tuple[list[dict], dict]:
    """The messages of the resolver call and what went into them (``info``: ``tokens``, the estimated size of the
    whole prompt; ``pairs`` and ``turns`` sent; ``dropped_pairs``; ``summary`` and ``plan`` sent; ``cut_turns`` and
    ``choices_dropped``, the cuts the cap made; ``over`` when it is still above ``max_tokens``).

    The parts, in the owner spec's order (§6.3): the system prompt; the plan (at most :data:`PLAN_TOKENS`; ``earlier``
    adds the line that it answered an earlier question); the summary (at most :data:`SUMMARY_TOKENS`); the pending
    choices; the turns (:func:`recent_turns`, at most :data:`PAIRS_TOKENS`, whole pairs dropped from the oldest end);
    the question. Then whole pairs are dropped from the oldest end until the prompt fits ``max_tokens`` (one pair is
    kept), then the summary; then the turns that are left are cut, the newest first, and then the pending choices go
    (stage-1 review fix F4). ``over`` is True only when the system prompt, the plan and the question alone are above
    ``max_tokens``: the caller then makes no call. The summary, the choices and the turns go as JSON, so no text in
    them can end its part, and every part is scrubbed of chat control tokens (the choices too: F7)."""
    plan_part = _plan_lines(plan, plan_line)
    while len(plan_part) > 2 and estimate_tokens("\n".join(plan_part)) > PLAN_TOKENS:
        plan_part.pop()   # the side calls go first; the main call is the second line
    plan_text = _cut_tokens("\n".join(plan_part), PLAN_TOKENS) if plan_part else ""
    if plan_text and earlier:
        plan_text += "\n" + LINES["plan_earlier"]
    summary_text = _followup._scrubbed(str(summary or "")[:MAX_SUMMARY_CHARS]).strip()
    summary_text = _cut_tokens(summary_text, SUMMARY_TOKENS) if summary_text else ""
    choices = [{"n": c.get("n"), "label": _followup._scrubbed(str(c.get("label") or "")),
                "question": _followup._scrubbed(str(c.get("question") or ""))}
               for c in (pending or {}).get("choices") or [] if isinstance(c, dict)]
    chunks = _pairs(recent_turns(turns))
    total_pairs = len(chunks)

    def flat() -> list[dict]:
        return [t for chunk in chunks for t in chunk]

    while len(chunks) > 1 and estimate_tokens(json.dumps(flat(), ensure_ascii=False, indent=0)) > PAIRS_TOKENS:
        chunks.pop(0)

    def user_message(with_summary: bool) -> str:
        parts = []
        if plan_text:
            parts.append(f"{LINES['head_plan']}\n{plan_text}")
        if with_summary and summary_text:
            parts.append(f"{LINES['head_summary']}\n{json.dumps(summary_text, ensure_ascii=False)}")
        if choices:
            parts.append(f"{LINES['head_pending']}\n{json.dumps(choices, ensure_ascii=False, indent=0)}")
        parts.append(f"{LINES['head_turns']}\n{json.dumps(flat(), ensure_ascii=False, indent=0)}")
        parts.append(f"{LINES['head_question']}\n{question}")
        return "\n\n".join(parts)

    def size(with_summary: bool) -> int:
        return _SYSTEM_TOKENS + estimate_tokens(user_message(with_summary))

    while len(chunks) > 1 and size(True) > max_tokens:
        chunks.pop(0)
    with_summary = bool(summary_text) and size(True) <= max_tokens
    cut_turns, choices_dropped = 0, False
    if size(with_summary) > max_tokens:
        kept_chunks, kept_choices = chunks, choices
        chunks, choices = [], []
        floor = size(False)   # the system prompt, the plan and the question: nothing of these is cut
        chunks, choices = kept_chunks, kept_choices
        if floor <= max_tokens:
            for _round in range(3):   # a cut by the estimate can leave a few tokens over (JSON escapes): again
                for t in reversed(flat()):
                    excess = size(False) - max_tokens
                    if excess <= 0:
                        break
                    if not t["content"]:
                        continue
                    keep = estimate_tokens(t["content"]) - excess - 4
                    t["content"] = _cut_tokens(t["content"], keep) if keep > 8 else ""
                    cut_turns += 1
                chunks = [kept for kept in ([t for t in chunk if t["content"]] for chunk in chunks) if kept]
                if size(False) <= max_tokens or not flat():
                    break
            if size(False) > max_tokens and choices:
                choices, choices_dropped = [], True
    tokens = size(with_summary)
    messages = [{"role": "system", "content": FOLLOWUP_RESOLVE_SYSTEM},
                {"role": "user", "content": user_message(with_summary)}]
    info = {"tokens": tokens, "pairs": len(chunks), "turns": len(flat()), "dropped_pairs": total_pairs - len(chunks),
            "summary": with_summary, "plan": bool(plan_text), "pending_choices": len(choices),
            "cut_turns": cut_turns, "choices_dropped": choices_dropped, "over": tokens > max_tokens}
    return messages, info


# ------------------------------------------------------------------------------------------------ the reply
def parse_reply(text: Any) -> dict | None:
    """The model's reply as ``{"kind", "standalone", "ops"}``, or None: one JSON object (a code fence around it is
    allowed, nothing else), with the keys kind (:data:`MODEL_KINDS`), standalone (a string of one line, at most 1,000
    characters; may be missing) and ops (a list; may be missing), and no other key. The ops are checked later by
    ``query_state.normalize_ops``."""
    if not isinstance(text, str) or not text.strip():
        return None
    body = text.strip()
    m = _FENCE_RE.match(body)
    if m:
        body = m.group(1)
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or not set(obj) <= _REPLY_KEYS or obj.get("kind") not in MODEL_KINDS:
        return None
    standalone = obj.get("standalone", "")
    ops = obj.get("ops", [])
    if standalone is None:
        standalone = ""
    if ops is None:
        ops = []
    if not isinstance(standalone, str) or not isinstance(ops, list):
        return None
    standalone = standalone.strip()
    if len(standalone) > _followup.MAX_QUESTION_CHARS or "\n" in standalone or "\r" in standalone:
        return None
    return {"kind": obj["kind"], "standalone": standalone, "ops": ops}


class _Echo:
    """A stand-in for the model that gives back one text, so that ``followup.rewrite`` reads a ``standalone`` exactly
    as it reads its own call's reply (the v6 rules: the echo, the respelling, the output rule, the contact rule and
    ``_unbound`` on the last 4 turns). It sends nothing anywhere."""

    cfg = SimpleNamespace(base_url="http://127.0.0.1/v1")

    def __init__(self, text: str) -> None:
        self.text = text

    def chat(self, messages: list[dict], **kwargs: Any) -> dict:
        return {"content": self.text, "finish_reason": "stop"}


# ------------------------------------------------------------------------------------------------ pending replies
def _is_no(choice: dict) -> bool:
    return _norm(choice.get("label")) == _norm(LINES["no_label"]) and not choice.get("ops")


def _is_confirm(choices: list[dict]) -> bool:
    return (len(choices) == 2 and _norm(choices[0].get("label")) == _norm(LINES["yes_label"])
            and _is_no(choices[1]))


def _confirm_word(words: list[str]) -> int | None:
    """1 when a reply to a confirm is made only of yes and acknowledgement words ("yes", "ok", "sure, go ahead",
    "haan ji", "theek hai"), 2 when only of no words ("no", "nahi"), courtesy words aside ("yes please"); else None
    (stage-1 review fixes M5 and L4). The lists are lines of prompts/followup_resolve.md."""
    rest = [w for w in words if w not in _COURTESY]
    if not rest or len(rest) > 6:
        return None

    def covered(phrases: frozenset[str]) -> bool:
        i = 0
        while i < len(rest):
            step = next((k for k in (3, 2, 1) if i + k <= len(rest) and " ".join(rest[i:i + k]) in phrases), 0)
            if not step:
                return False
            i += step
        return True

    if covered(_YES_WORDS | _ACK_WORDS):
        return 1
    if covered(_NO_WORDS):
        return 2
    return None


def choice_for(reply: Any, pending: Any) -> dict | None:
    """The choice of ``pending`` (a checked pending clarification) that ``reply`` picks, or None (C12): a number 1-4
    (alone, or with option, choice, number, the ... one, wala), a number word or an ordinal (first, 2nd, doosra,
    fourth ...), "the last one"; for a confirm a reply of yes or acknowledgement words or of no words
    (:func:`_confirm_word`); else a label, in this order: the reply equals one label; else it equals one label's first
    word; else, for a label of 6 characters or more, it is one edit from one (case, punctuation and spacing aside).
    Never an edit distance on a shorter label, and a reply that fits two labels at the same step picks none (an exact
    label wins over another label's first word: stage-1 review fix L3)."""
    try:
        choices = [c for c in (pending or {}).get("choices") or [] if isinstance(c, dict)]
        if not choices:
            return None
        words = _clarify._normal(reply).split()
        if not words:
            return None
        rest = [w for w in words if w not in _clarify._FILL_WORDS]
        n = None
        if len(rest) == 1 and len(words) <= 5:
            w = rest[0].lstrip("#")
            if w.isdigit():
                n = int(w)
            elif w in _ORDINALS:
                n = _ORDINALS[w]
            elif w in _clarify._LAST:
                n = len(choices)
        elif not rest and "one" in words and "the" not in words and len(words) <= 3:
            n = 1   # "one", "option one" ("the one" names none)
        if n is not None:
            return choices[n - 1] if 1 <= n <= len(choices) else None
        typed = " ".join(words)
        if _is_confirm(choices):
            got = _confirm_word(words)
            if got is not None:
                return choices[got - 1]
        labels = [(c, _clarify._normal(c.get("label"))) for c in choices]
        labels = [(c, label) for c, label in labels if label]
        for fits in (lambda label: typed == label, lambda label: typed == label.split()[0],
                     lambda label: len(label) >= 6 and osa_distance(typed, label, 1) <= 1):
            hits = [c for c, label in labels if fits(label)]
            if hits:
                return hits[0] if len(hits) == 1 else None
        return None
    except Exception:  # noqa: BLE001 - no choice: the question goes on
        return None


def _numbered(asked: str, pending: dict | None) -> str:
    """The clarifying question as the person reads it: the question, then one numbered line per choice."""
    if not pending:
        return asked
    return asked + "\n\n" + "\n".join(f"{c['n']}. {c['label']}" for c in pending["choices"])


def _make_pending(asked: str, for_question: str, choices: Iterable[dict]) -> dict | None:
    """``query_state.make_pending`` with the asked text, the labels and the questions scrubbed of chat control tokens
    (they are stored, and the next turn's prompt shows them: stage-1 review fix F7)."""
    return qs.make_pending(_followup._scrubbed(str(asked or "")), for_question,
                           [dict(c, label=_followup._scrubbed(str(c.get("label") or "")),
                                 question=_followup._scrubbed(str(c.get("question") or ""))) for c in choices])


def pending_from_clarify(text: Any, for_question: Any) -> dict | None:
    """A pending clarification (``query_state.make_pending``) from a clarifying question of prerequisite P2
    (``agent/clarify.py``: a lead, numbered choices, a closing line), so that the caller can store its choices
    (design §6.6) and step 1 reads the reply: each choice's text is its label and its question. None when the text
    holds no such question. Never raises."""
    try:
        text = str(text or "")
        choices = _clarify._choices(text)
        if not choices:
            return None
        lead = next((ln.strip() for ln in text.splitlines()
                     if _clarify.TEXTS["lead"] in ln or _clarify.TEXTS["lead_no_object"] in ln), "")
        return _make_pending(lead, str(for_question or ""), [{"label": c, "question": c} for c in choices])
    except Exception:  # noqa: BLE001 - no pending: the next turn reads the text as before
        return None


def render_clarify(clarify: qs.Clarify, plan_line: str = "") -> str:
    """The words of a check that failed (``query_state.Clarify``): the line of its code, filled with the plan line,
    the field and the value (a term reject names its contract, never the model-facing contract text)."""
    code = clarify.code
    key = f"clarify_{code}_{clarify.value}" if code == "term_reject" else f"clarify_{code}"
    if key not in LINES:
        key = "clarify_bad_call"
    value = " ".join(str(clarify.value or "").split())[:80]
    return _say(key, plan=plan_line, field=" ".join(str(clarify.field or "").split())[:120], value=value)


# ------------------------------------------------------------------------------------------------ one job
class _Job:
    """One question: the inputs as read, and the steps of §6.2. Lives for one call of :func:`resolve`."""

    def __init__(self, question: Any, turns: Any, state: Any, gap: Any, summary: Any, *, view: Any, today: Any,
                 profile: Any, strict: Any, max_calls: int, max_age: int, max_tokens: int, shortcuts: bool,
                 charts_on: bool, other_record_types: Iterable[str]) -> None:
        self.question = question if isinstance(question, str) else ""
        self.turns = list(turns) if isinstance(turns, (list, tuple)) else []
        self.recent = recent_turns(self.turns)
        self.last4 = _followup.usable_turns(self.turns)
        self.summary = summary if isinstance(summary, str) else ""
        self.view, self.max_age, self.max_tokens = view, max_age, max_tokens
        self.shortcuts, self.charts_on = bool(shortcuts), bool(charts_on)
        self.other_record_types = tuple(other_record_types or ())
        if not isinstance(today, dt.date):   # the business date, from the caller: never this machine's clock (N3)
            raise TypeError("today must be the business date (engine.periods.today_in)")
        self.today = today
        self.max_calls = max_calls
        if isinstance(gap, int) and not isinstance(gap, bool) and gap >= 0:
            self.gap = gap
        else:
            self.gap = 0 if state is None else MAX_GAP + 1   # an unreadable gap is never "the last answer"
        self.checked = qs.check(state, profile=profile if isinstance(profile, str) else None, view=view,
                                max_calls=max_calls)
        self.plan = self.checked.plan
        self.age = self.plan["age"] if self.plan else None
        #: the model may patch it; the gap and the age decide whether the patch asks first
        self.plan_ok = self.plan is not None and self.age <= max_age
        self.usable = qs.usable(self.checked, self.gap, max_age)
        self.fresh = self.usable and self.age == 0
        self.label = getattr(view, "label", None) if view is not None else None
        self.line = qs.plan_line(self.plan, today=self.today, label=self.label) if self.plan else ""
        self.stored_terms = self.plan["terms"] if self.plan else None
        self.pending = self.checked.pending if self._strict(strict) else None
        self.info: dict[str, Any] = {"gap": self.gap, "age": self.age if self.age is not None else -1,
                                     "plan": self.plan is not None, "usable": self.usable,
                                     "pending_strict": self.pending is not None}

    # -- the inputs
    def _strict(self, strict: Any) -> bool:
        pending = self.checked.pending
        if not pending or self.gap != 0:
            return False
        if strict is not None:
            return bool(strict)
        last = next((t.get("content") for t in reversed(self.turns) if isinstance(t, dict)
                     and t.get("role") == "assistant" and isinstance(t.get("content"), str)), "")
        asked = _norm(pending.get("asked"))
        return bool(asked) and asked in _norm(last)

    def _complete(self, text: str) -> bool:
        return _followup._complete(text) and not _followup._fragment(text)

    def _another_subject(self, text: str) -> bool:
        """``followup._another_subject``, narrowed (C11): a question on how the org works, or one behind a lead that
        asks something of its own (an ask word, :data:`_ASK_WORDS`) about a subject it names itself (a record noun,
        ``followup._THING_NOUNS``). "and what is the leave policy?" and "and how many recruiters joined last
        month?" still run as typed. "and only for Pune" (``followup._complete`` reads a name and one more word as
        complete), "and what about candidates in Pune" (what about, how about: §6.5's own ambiguous case) and "and
        who had the most in Pune?" (a ranking word, of the plan's records) go to the model (stage-1 review fix
        M7)."""
        if not _followup._another_subject(text, self.last4):
            return False
        words = _followup._words(text)
        have = set(words)
        if have & _followup._ORG_WORDS:
            return True
        if any(pair in _followup._FRAGMENT_PAIRS for pair in zip(words, words[1:])) or have & _RANKING_WORDS:
            return False
        return bool(have & _ASK_WORDS) and bool(have & _followup._THING_NOUNS)

    def _own_question(self, text: str) -> bool:
        """A reply to a clarifying question that is a question of its own: complete, or of another subject."""
        return self._complete(text) or self._another_subject(text)

    def _read(self, standalone: Any) -> _followup.Rewrite:
        """The v6 rewrite's reading of ``standalone`` as the reply to the typed question (last 4 turns)."""
        if not isinstance(standalone, str) or not standalone.strip():
            return _followup.Rewrite(_followup.UNCLEAR, reason="no_standalone")
        return _followup.rewrite(self.question, self.turns, _Echo(standalone))

    def _terms(self, typed: str, standalone: str, kind: str) -> Terms:
        return qs.terms_for(self.stored_terms, typed, standalone, kind, self.other_record_types)

    def _line(self, plan: dict | None, terms: Terms | None = None) -> str:
        """The plan line of a patched plan, with the scope of the terms it runs with (stage-1 review fix M2)."""
        return qs.plan_line(plan, today=self.today, label=self.label, terms=terms) if plan else ""

    def _field_label(self, obj: Any, path: Any) -> str:
        """The words of a field for a choice: its label in the catalog view, else its API name."""
        try:
            got = self.label(obj, path) if callable(self.label) else None
        except Exception:  # noqa: BLE001 - the API name then
            got = None
        return str(got or path or "")

    def _out(self, action: str, reason: str, step: int, **kw: Any) -> Resolution:
        kw.setdefault("plan_line", self.line)
        return Resolution(action, reason, step, checked=self._carried(action), info=dict(self.info), **kw)

    def _carried(self, action: str) -> qs.Checked:
        """The parent state the caller builds the next one from. A reply given at a gap (a confirm, an unclear reply,
        small talk) carries the plan from before the gap: its row becomes the next parent at gap 0, so the plan is
        kept at least one answer old, and the next patch of it confirms again instead of running silently (D10;
        stage-1 review fix M8)."""
        plan = self.checked.plan
        if action != "reply" or self.gap <= 0 or not plan or plan.get("age", 0) >= 1:
            return self.checked
        state = dict(self.checked.state, plan=dict(plan, age=1))
        return dataclasses.replace(self.checked, state=state)

    # -- the outcomes
    def _reply(self, step: int, reason: str, *, status: str = "answered", text: str = "", state_kind: str = "",
               pending: dict | None = None, detail: str = "", error: str = "") -> Resolution:
        return self._out("reply", reason, step, status=status, text=text, state_kind=state_kind, pending=pending,
                         detail=detail, error=error)

    def _clarify(self, step: int, asked: str, choices: list[dict] | None = None, detail: str = "",
                 ops: Iterable[dict] = ()) -> Resolution:
        asked = _followup._scrubbed(asked)
        pending = _make_pending(asked, self.question, choices) if choices else None
        return dataclasses.replace(self._reply(step, "clarify", text=_numbered(asked, pending), state_kind="clarify",
                                               pending=pending, detail=detail), ops=tuple(ops))

    def _unclear(self, step: int, detail: str) -> Resolution:
        return self._reply(step, "unclear_rewrite", status="unclear", state_kind="unclear", detail=detail)

    def _new(self, step: int, reason: str, question: str, *, typed: str | None = None,
             detail: str = "") -> Resolution:
        typed = self.question if typed is None else typed
        return self._out("new", reason, step, question=question, typed=typed,
                         terms=self._terms(typed, question, "new"), terms_kind="new", detail=detail)

    def _no_choice(self) -> dict:
        return {"label": LINES["no_label"], "question": LINES["no_label"], "ops": []}

    def _confirm(self, step: int, canon: list[dict], line: str, detail: str) -> Resolution:
        """The confirm clarify, "Do you mean: <plan line>? 1. Yes 2. No, I'll ask in full" (``detail``: confirm_gap,
        confirm_age or confirm_period)."""
        choices = [{"label": LINES["yes_label"], "question": line, "ops": canon}, self._no_choice()]
        return self._clarify(step, _say("confirm", plan=line), choices, detail=detail, ops=canon)

    def _patch(self, step: int, reason: str, ops: Any, *, standalone: str = "", typed: str | None = None,
               pending_text: str = "", contact_ok: bool | None = None, confirm: str | None = None,
               model: bool = False, detail: str = "") -> Resolution:
        """A patch of the plan: ``query_state.apply`` and its checks; ``confirm`` (the confirm's detail) asks first."""
        typed = self.question if typed is None else typed
        canon = qs.normalize_ops(list(ops) if isinstance(ops, (list, tuple)) else ops)
        if not canon:
            return self._fallback(None, "bad_ops") if model else self._clarify(
                step, render_clarify(qs.Clarify("bad_ops"), self.line), detail="check_bad_ops")
        if any("new_question" in o for o in canon):
            return self._model_new(step, standalone, detail="op_new_question")
        if self.checked.missing:
            return self._clarify(step, _say("field_removed", plan=self.line, field=self.checked.missing[0]),
                                 detail="field_removed")
        terms = self._terms(typed, standalone, "patch")
        patched = qs.apply(self.plan, canon, typed=typed, view=self.view, today=self.today, terms=terms,
                           pending_text=pending_text, contact_ok=contact_ok, max_calls=self.max_calls)
        if patched.clarify is not None:
            return self._failed_check(step, patched.clarify, canon, typed, pending_text, terms, model)
        line = self._line(patched.plan, terms)
        if confirm:
            return self._confirm(step, canon, line, confirm)
        # the record agent's question: the model's whole question when it passes the rewrite's reading and is more
        # than the typed words again ("Only for North America" alone names no query); else the plan line
        ok = self._read(standalone) if standalone and model else None
        whole = (ok is not None and ok.status == _followup.OK and bool(ok.question)
                 and _followup._words(ok.question) != _followup._words(self.question))
        question = ok.question if whole else line
        return self._out("patch", reason, step, question=question, typed=typed, terms=terms, terms_kind="patch",
                         patched=patched, plan=patched.plan, plan_line=line, ops=tuple(canon),
                         op=next(iter(canon[0])), detail=detail)

    def _failed_check(self, step: int, c: qs.Clarify, canon: list[dict], typed: str, pending_text: str,
                      terms: Terms, model: bool) -> Resolution:
        """A check of a patch failed: a did-you-mean for a value the model chose and the person did not type, the
        links to choose from for ``which_link``, else the code's clarifying line."""
        if c.code == "value_not_typed" and model and self._schema_values(canon, typed, pending_text):
            values = [str(v) for o in canon for p in o.values() for v in (
                p.get("values") or ([p["value"]] if "value" in p else []))]
            again = qs.apply(self.plan, canon, typed=typed, view=self.view, today=self.today, terms=terms,
                             pending_text="\n".join([pending_text, *values]), max_calls=self.max_calls)
            if again.plan is not None:
                line = self._line(again.plan, terms)
                asked = _say("did_you_mean", plan=line, field=" ".join(c.field.split())[:120],
                             value=" ".join(c.value.split())[:80])
                choices = [{"label": LINES["yes_label"], "question": line, "ops": canon}, self._no_choice()]
                return self._clarify(step, asked, choices, detail="did_you_mean", ops=canon)
        if c.code == "which_link" and c.choices:
            choices = []
            for ch in c.choices:
                alt = [o for o in ch.get("ops") or [] if isinstance(o, dict) and "follow_set" in o]
                if not alt:
                    continue
                ops = [alt[0] if "follow_set" in o else o for o in canon]
                got = qs.apply(self.plan, ops, typed=typed, view=self.view, today=self.today, terms=terms,
                               pending_text=pending_text, max_calls=self.max_calls)
                if got.plan is not None:
                    link = alt[0]["follow_set"]
                    choices.append({"label": self._field_label(link.get("object"), link.get("via_field")),
                                    "question": self._line(got.plan, terms), "ops": ops})
            if choices:
                choices = choices[:qs.MAX_CHOICES - 1] + [self._no_choice()]
                return self._clarify(step, render_clarify(c, self.line), choices, detail="which_link", ops=canon)
        return self._clarify(step, render_clarify(c, self.line), detail=f"check_{c.code}", ops=canon)

    def _schema_values(self, canon: list[dict], typed: str, pending_text: str) -> bool:
        """Every value of the model's filter and drill operations that the person did not type is schema data of its
        field (a picklist value, a record type: ``query_state.schema_value``), so a did-you-mean may show it. A value
        found only in an answer (a name, a figure) is never offered as a choice; the clarify then only asks which value
        is meant (stage-1 review fix F8)."""
        main = next((c for c in (self.plan or {}).get("calls") or [] if c.get("role") == "main"), None)
        if main is None:
            return False
        obj = main["args"].get("object")
        for o in canon:
            name, p = next(iter(o.items()))
            if name == "add_filter":
                field_path, vals = p.get("field"), p.get("values") or ([p["value"]] if "value" in p else [])
            elif name == "drill" and "value" in p:
                field_path, vals = main["args"].get("group_by"), [p["value"]]
            else:
                continue
            for v in vals:
                if qs.typed_value(v, typed, pending_text):
                    continue
                if not qs.schema_value(self.view, obj, field_path, v):
                    return False
        return True

    def _model_new(self, step: int, standalone: str, detail: str) -> Resolution:
        got = self._read(standalone)
        if got.status == _followup.OK and got.question:
            return self._new(step, "new", got.question, detail=detail)
        return self._fallback(None, f"rewrite_{got.reason}")

    def _choice(self, choice: dict) -> Resolution:
        if _is_no(choice):
            return self._reply(1, "ask_full", status="unclear", state_kind="unclear", detail="ask_full")
        if choice.get("ops"):
            return self._patch(1, "clarify_choice", choice["ops"], standalone=choice["question"],
                               typed=self.pending.get("for") or self.question, pending_text=choice["question"],
                               contact_ok=asks_for_contact(self.question), detail="choice_ops")
        # the person typed the question the clarification was for, not the choice's words (which may be a model's
        # reading): those set the terms and the next state's typed words (stage-1 review fix F8)
        return self._new(1, "clarify_choice", choice["question"], typed=self.pending.get("for") or self.question,
                         detail="choice")

    def _fallback(self, reply: dict | None, detail: str) -> Resolution:
        """Step 5: with a plan the model saw, a clarify built from the plan (its readings as choices when they pass
        the checks); without one, the caller's unclear line."""
        if not self.plan_ok:
            return self._unclear(5, detail)
        choices: list[dict] = []
        if reply and reply.get("ops") and not self.checked.missing:
            canon = qs.normalize_ops(reply["ops"])
            if canon and not any("new_question" in o for o in canon):
                terms = self._terms(self.question, reply.get("standalone") or "", "patch")
                got = qs.apply(self.plan, canon, typed=self.question, view=self.view, today=self.today, terms=terms,
                               max_calls=self.max_calls)
                if got.plan is not None:
                    line = self._line(got.plan, terms)
                    choices.append({"label": line, "question": line, "ops": canon})
        if reply and reply.get("standalone"):
            got_q = self._read(reply["standalone"])
            if got_q.status == _followup.OK and got_q.question and all(
                    _norm(c["question"]) != _norm(got_q.question) for c in choices):
                choices.append({"label": got_q.question, "question": got_q.question, "ops": []})
        if choices:
            return self._clarify(5, _say("unclear_choices", plan=self.line), choices + [self._no_choice()],
                                 detail="unclear_readings")
        if self.checked.missing:
            return self._clarify(5, _say("field_removed", plan=self.line, field=self.checked.missing[0]),
                                 detail="field_removed")
        return self._clarify(5, _say("unclear_plan", plan=self.line), detail=detail or "unclear_plan")

    def _shortcut(self) -> _shortcuts.Shortcut | None:
        """Step 2: the display words read by ``agent/display.py`` (the one list), the rest by the shortcut grammar."""
        q = self.question
        d = _display.split(q)
        if _display.generic_ask(d):
            return None
        chart = False if "chart" in d.refused else (True if d.chart else None)
        other = bool(d.detail or d.file or d.breakdown)
        rest = d.search if (d.words or d.refused) else q
        if "chart" in d.refused:   # a refusal of the chart grammar keeps its words in the search text
            rest = _display._STOP.sub(" ", _display.fold(rest))
        kind = None
        if chart is True and self.charts_on:
            from sf_assistant.agent.charts import named_type   # only with charts on (SFA_CHARTS)

            kind = named_type(_display.fold(q))
            kind = kind if kind in qs.CHART_TYPES else None
        shown = {"chart": chart, "type": kind, "table": d.table, "other": other} \
            if (chart is not None or d.table or other) else None
        return _shortcuts.match(rest, today=self.today, display=shown)

    # -- the steps
    def run(self, llm: Any, call: bool) -> Resolution:
        q = self.question
        if self.pending is not None and _is_confirm(self.pending["choices"]):
            # "ok", "sure", "theek hai" answer a confirm: a yes, read before the small talk would end the pending
            # (stage-1 review fix M5); any other reply to it takes the steps below
            picked = choice_for(q, self.pending)
            if picked is not None:
                return self._choice(picked)
        talk = small_talk(q)
        if talk:
            return self._reply(0, "small_talk", text=_talk_line(talk), state_kind="small_talk", detail=talk)
        with_choices = False
        if self.pending is not None:
            picked = choice_for(q, self.pending)
            if picked is not None:
                return self._choice(picked)
            with_choices = not self._own_question(q)
        else:
            chosen = _clarify.choice_reply(q, self.last4)   # P2c: a clarifying question whose choices were not stored
            if chosen is not None:
                return self._new(1, "clarify_choice", chosen, typed=chosen, detail="p2_choice")
        if not with_choices:
            if self.shortcuts and self.fresh and self.pending is None:
                sc = self._shortcut()
                if sc is not None:
                    return self._patch(2, "shortcut", list(sc.ops), detail="+".join(sc.parts))
            if not self.recent and not self.plan_ok:
                return self._new(3, "new", q, detail="no_context")
            if _display.generic_ask(_display.split(q)):
                return self._new(3, "new", q, detail="display_only")
            # a plan the model may still patch (after a gap, or one or two answers old) is context: a follow-up of
            # it is never run as typed with no model call, so the gap and age rules can confirm (§6.5, §14, D10,
            # D13; stage-1 review fix M6). Only with no such plan does a complete question run as typed
            if not self.plan_ok and self._complete(q):
                return self._new(3, "new", q, detail="complete")
            if self.plan_ok and self._another_subject(q):
                return self._new(3, "new", q, detail="another_subject")
        return self._model(llm, call, self.pending if with_choices else None)

    def _model(self, llm: Any, call: bool, pending: dict | None) -> Resolution:
        plan = self.plan if self.plan_ok else None
        messages, info = build_messages(self.question, self.turns, plan=plan, plan_line=self.line,
                                        earlier=bool(plan) and (self.gap > 0 or self.age > 0), summary=self.summary,
                                        pending=pending, max_tokens=self.max_tokens)
        self.info.update({"pairs": info["pairs"], "dropped_pairs": info["dropped_pairs"],
                          "summary_sent": info["summary"], "cut_turns": info["cut_turns"], "over": info["over"]})
        tokens = info["tokens"]
        if info["over"]:   # SFA_MEMORY_TOKENS holds: no prompt above it reaches the shared server (C32, F4)
            return dataclasses.replace(self._fallback(None, "over_cap"), prompt_tokens=tokens)
        if not call:
            return dataclasses.replace(self._out("model", "model", 4, detail="would_call"), prompt_tokens=tokens)
        if not is_local_endpoint(getattr(getattr(llm, "cfg", None), "base_url", "")):
            return dataclasses.replace(self._reply(4, "not_local", status="failed", detail="not_local"),
                                       prompt_tokens=tokens)
        try:
            resp = llm.chat(messages, json_mode=True, max_tokens=RESOLVE_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001 - the typed words are never sent on after a failed call
            return dataclasses.replace(self._reply(4, "call_failed", status="failed", detail="call_failed",
                                                   error=type(exc).__name__), model_called=True, prompt_tokens=tokens)
        content = resp.get("content") if isinstance(resp, dict) else None
        text = strip_thinking(content if isinstance(content, str) else "")
        cut = _finish_reason(resp) == "length"
        reply = None if cut else parse_reply(text)
        out = self._decide(reply, "cut" if cut else "bad_reply")
        return dataclasses.replace(out, model_called=True, prompt_tokens=tokens,
                                   reply=text[:MAX_TURN_CHARS] if out.action == "reply" else "")

    def _decide(self, reply: dict | None, why: str) -> Resolution:
        """Step 4's code checks on the model's reply."""
        if reply is None:
            return self._fallback(None, why)
        kind, standalone, ops = reply["kind"], reply["standalone"], reply["ops"]
        if kind == "new":
            return self._model_new(4, standalone, detail="model_new")
        if kind == "about_answer":
            got = self._read(standalone)
            if got.status != _followup.OK or not got.question:
                return self._fallback(reply, f"rewrite_{got.reason}")
            if not self.fresh:   # the plan did not answer the last answer: a question of its own
                return self._new(4, "new", got.question, detail="about_not_fresh")
            return self._out("about_answer", "about_answer", 4, question=got.question, typed=self.question,
                             terms=self._terms(self.question, got.question, "patch"), terms_kind="patch",
                             plan=self.plan, earlier_answer=self._last_answer(), detail="model_about")
        if kind == "patch":
            if not self.plan_ok:
                return self._fallback(reply, "patch_without_plan")
            if not ops:
                return self._fallback(reply, "bad_ops")
            confirm = ("confirm_gap" if self.gap > 0 else "confirm_age" if self.age > 0
                       else "confirm_period" if self._untyped_period(ops) else None)
            return self._patch(4, "patch", ops, standalone=standalone, confirm=confirm, model=True,
                               detail="model_patch")
        return self._fallback(reply, "model_unclear")

    def _untyped_period(self, ops: Any) -> bool:
        """A model's set_period or compare_period whose phrase the typed question does not say: it shares no word
        with it ("same for last year" says "year"; "previous month" says "month"), and the period the typed words make
        (Hinglish mapped: "pichle mahine" is "last month") is another. Such a patch confirms first, since the person
        never asked for that period (stage-1 review fix L5)."""
        canon = qs.normalize_ops(list(ops) if isinstance(ops, (list, tuple)) else ops) or []
        typed = set(_shortcuts.words(self.question))
        reading = None
        for o in canon:
            name, p = next(iter(o.items()))
            if name not in ("set_period", "compare_period"):
                continue
            if (set(_shortcuts.words(p["phrase"])) - _PERIOD_FRAME) & typed:
                continue
            if reading is None:
                reading = _shortcuts.period_phrase(self.question, today=self.today) or ""
            if _norm(reading) != _norm(p["phrase"]):
                return True
        return False

    def _last_answer(self) -> str:
        return next((t["content"] for t in reversed(self.recent) if t["role"] == "assistant"), "")


# ------------------------------------------------------------------------------------------------ entry points
def resolve(question: str, turns: Any, state: Any, gap: Any, summary: Any, llm: Any, *, today: dt.date,
            view: Any = None, profile: str | None = None, strict: bool | None = None,
            max_calls: int = MAX_CALLS, max_age: int = MAX_AGE, max_tokens: int = MAX_TOKENS,
            shortcuts: bool = True, charts_on: bool = True, other_record_types: Iterable[str] = ()) -> Resolution:
    """How ``question`` goes on from the parent answer's ``state`` (see the module text). Never raises.

    ``turns``: the contract's ``recent_turns`` (up to 12); ``gap``: the contract's gap (0-6); ``summary``: the
    Salesforce summary (data); ``llm``: the pipeline's LLM (one ``chat`` call at most, only to an endpoint on this
    machine). ``view``: ``query_state.PlanView`` over the record tools' catalog (without it no field resolves and
    every patch asks); ``today``: the business date (``engine.periods.today_in``), required: the resolver never
    reads this machine's clock (a value that is no date gives ``resolver_error``);
    ``profile``: the request's ``access_profile`` (a state built under another one is not used); ``strict``: whether
    the parent answer is the very last assistant turn (None: read from the turns). ``max_calls``, ``max_age``,
    ``max_tokens`` and ``shortcuts``: ``SFA_MEMORY_MAX_CALLS``, ``SFA_MEMORY_MAX_AGE``, ``SFA_MEMORY_TOKENS`` and
    ``SFA_MEMORY_SHORTCUTS`` (``src/config.py`` reads them; these are their defaults). ``charts_on``: ``SFA_CHARTS``
    (with it off no chart type is read). ``other_record_types``: the Account record type names the term contracts
    read (``term_contracts.detect``)."""
    try:
        job = _Job(question, turns, state, gap, summary, view=view, today=today, profile=profile, strict=strict,
                   max_calls=max_calls, max_age=max_age, max_tokens=max_tokens, shortcuts=shortcuts,
                   charts_on=charts_on, other_record_types=other_record_types)
        return job.run(llm, call=True)
    except Exception as exc:  # noqa: BLE001 - the caller then falls back to the rewrite of today
        return Resolution("reply", "resolver_error", 5, status="failed", detail="resolver_error",
                          error=type(exc).__name__)


def shadow(question: str, turns: Any, state: Any, gap: Any, summary: Any, *, today: dt.date, view: Any = None,
           profile: str | None = None, strict: bool | None = None,
           max_calls: int = MAX_CALLS, max_age: int = MAX_AGE, max_tokens: int = MAX_TOKENS,
           shortcuts: bool = True, charts_on: bool = True, other_record_types: Iterable[str] = ()) -> Resolution:
    """What the code steps of :func:`resolve` would decide, with no model call (``SFA_MEMORY=shadow``, C31): the
    decision of steps 0-3, or action ``model`` with ``prompt_tokens``, the size the resolver prompt would have. For the
    log only: the answer path stays today's. Never raises."""
    try:
        job = _Job(question, turns, state, gap, summary, view=view, today=today, profile=profile, strict=strict,
                   max_calls=max_calls, max_age=max_age, max_tokens=max_tokens, shortcuts=shortcuts,
                   charts_on=charts_on, other_record_types=other_record_types)
        return job.run(None, call=False)
    except Exception as exc:  # noqa: BLE001 - shadow never changes an answer
        return Resolution("reply", "resolver_error", 5, status="failed", detail="resolver_error",
                          error=type(exc).__name__)
