"""Toggle-off isolation: with the Salesforce toggle OFF, no Salesforce content.

Owner order (2026-11-25): with the toggle off there is strictly no Salesforce.
No Salesforce records, no earlier Salesforce answer and no company people
detail may reach a toggle-off answer. Routing already kept every toggle-off
turn away from the Salesforce engines; the breach was CONTEXT: the history the
browser sends, the rolling summary, folded chunks, cross-chat recall, the
chat's artifacts and the in-process session memory all carried earlier
Salesforce answers into the assistant-mode prompt.

This module is the one place the guard is defined, so every read path asks the
same question:

* The predicate. An assistant row is a Salesforce answer when its meta says so
  (`mode`, `engine`, a `salesforce_*` key) or when a server-written row of its
  generation asked for Salesforce: the `query_traces` row (written after the
  feature gate, so it says what the turn really ran as) or, when there is no
  trace, the `chat_requests` row. Neither can be rewritten by a client PUT,
  and they catch an answer stopped before its one final meta. A chat is
  Salesforce-HOLDING when it has such an answer, or a server row says one of
  its turns asked for Salesforce (an answer stopped or not yet stored, review
  F1).
* `ChatState` / `prepare`, the per-chat state: which stored answers are
  Salesforce answers, which questions were asked in Salesforce mode, which
  assistant-mode answers are CLEAN (explicit `mode='assistant'`, and either
  before the chat's first Salesforce turn or stamped `sf_isolation` by the
  guard). Everything else is TAINTED: written from a prompt that may have
  held a Salesforce answer.
* `redact`, the history rewrite. Hidden turns become placeholders, so roles,
  turn counts and positions stay as they were. A selected-context quote is
  kept only when it is the text of a clean answer or of the person's own
  question (an allow-list, review F3).
* `visible_text` / `is_placeholder`, which keep placeholders out of every web
  query, retrieval query and file.
* `needs_salesforce`, the conservative "needs company records" detector with
  its fixed replies, decided before any model call of the turn.
* `salesforce_chats`, the user's Salesforce-holding chat ids, cached in this
  process for the cross-chat reads (review R2).

Read-time only: nothing stored is deleted or rewritten.
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from . import metrics
from .config import settings

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

USER_PLACEHOLDER = (
    "(A question asked with the Salesforce toggle on. It is not shown while the toggle is off.)"
)
SF_ANSWER_PLACEHOLDER = (
    "(An answer from Salesforce records. It is not shown while the Salesforce toggle is off. "
    "Do not guess what it said.)"
)
TAINTED_PLACEHOLDER = (
    "(An earlier answer in this chat that may repeat Salesforce records. It is not shown "
    "while the Salesforce toggle is off.)"
)
#: Replaces a selected-context quote that is not the text of a clean answer
#: or of the person's own question; the follow-up under it is kept.
QUOTE_PLACEHOLDER = (
    "(A quote from an earlier answer that may hold Salesforce records. It is not shown while "
    "the Salesforce toggle is off.)"
)
#: Whole-turn placeholders: `is_placeholder` compares against exactly these.
PLACEHOLDERS: Tuple[str, ...] = (USER_PLACEHOLDER, SF_ANSWER_PLACEHOLDER, TAINTED_PLACEHOLDER)

STATUS_LINE = "Earlier parts of this chat that used Salesforce are not used while the toggle is off."
#: Said instead when the chat's stored rows could not be read (review R6):
#: nothing is known to be from Salesforce, so the line does not say so.
STATUS_LINE_UNCHECKED = (
    "Earlier answers in this chat could not be checked just now and are not used on this turn."
)

REPLY_NEEDS_SALESFORCE = (
    "That needs your company's Salesforce records, and I don't use them while the "
    "Salesforce toggle is off. Turn on the Salesforce toggle in the composer and ask again."
)
#: D and E. Worded for both a Salesforce answer and a toggle-off answer
#: written after one (review data F6): both "may hold Salesforce records".
REPLY_HIDDEN = (
    "That refers to an earlier part of this chat that may hold Salesforce records, and I can't "
    "see it while the Salesforce toggle is off. Turn on the Salesforce toggle to continue with it."
)
#: D and E for a member with no Salesforce toggle (review R5): no offer of a
#: toggle they do not have.
REPLY_HIDDEN_NO_TOGGLE = (
    "That refers to an earlier part of this chat that may hold Salesforce records, and I can't "
    "use it here. Please ask your question in full."
)
#: A quote that could not be checked because the chat's rows could not be
#: read (review R6): no claim about where it came from.
REPLY_UNCHECKED = (
    "I can't check that quoted text against this chat just now, so I haven't used it. "
    "Please try again in a moment, or ask without the quote."
)
FIXED_REPLIES: Tuple[str, ...] = (REPLY_NEEDS_SALESFORCE, REPLY_HIDDEN, REPLY_HIDDEN_NO_TOGGLE, REPLY_UNCHECKED)
#: The replies a short follow-up (rule D) still refers through: they are said
#: only about a hidden part of a Salesforce-holding chat. A follow-up on
#: REPLY_NEEDS_SALESFORCE is a new question (review R4).
_HIDDEN_REPLIES: Tuple[str, ...] = (REPLY_HIDDEN, REPLY_HIDDEN_NO_TOGGLE)

#: Keys of an answer's meta that only the Salesforce engines write.
SF_META_KEYS: Tuple[str, ...] = ("salesforce_mode", "salesforce_sources", "salesforce_scope")

#: 6-token shingles for the quote check, capped per chat.
SHINGLE = 6
MAX_SHINGLES = 50_000
#: Share of a quote's shingles that must be found in clean text to keep it.
_CLEAN_SHARE = 0.8
#: Shingles searched in the joined text once the shingle set is capped.
_SAMPLE = 16

# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------

_ALIAS_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

#: The meta half as ONE jsonpath test: the meta is read (and decompressed)
#: once per row instead of once per key (review R2/R7: 0.65 ms instead of
#: 3-6 ms over the largest clean chat's 43 answers).
SF_META_JSONPATH = (
    '$ ? (@.mode == "salesforce" || @.engine == "sf_assistant"'
    + "".join(f" || exists(@.{k})" for k in SF_META_KEYS)
    + ")"
)


def sf_meta_sql(alias: str) -> str:
    """SQL that is TRUE when the meta of `messages` row `alias` says it is a
    Salesforce answer (no joins). Never NULL."""
    if not _ALIAS_RE.match(alias):
        raise ValueError(f"bad SQL alias {alias!r}")
    return f"COALESCE(jsonb_path_exists({alias}.meta, '{SF_META_JSONPATH}'), false)"


def sf_answer_sql(alias: str) -> str:
    """SQL that is TRUE when the `messages` row `alias` is a Salesforce answer.

    Never NULL (wrapped in COALESCE), so `NOT (...)` keeps a row with no meta.
    Takes no parameters. The server-written rows are matched on the row's
    `generation_id` (a trace's id IS its generation id): the trace first,
    because it is written after the feature gate (a member without Salesforce
    who sent mode=salesforce was answered in assistant mode, review R12); the
    request only for a generation with no trace.
    """
    if not _ALIAS_RE.match(alias):
        raise ValueError(f"bad SQL alias {alias!r}")
    a = alias
    return (
        f"COALESCE({a}.role = 'assistant' AND ({sf_meta_sql(a)}"
        f" OR ({a}.generation_id IS NOT NULL AND CASE"
        f" WHEN EXISTS (SELECT 1 FROM query_traces sfi_t WHERE sfi_t.trace_id = {a}.generation_id)"
        f" THEN EXISTS (SELECT 1 FROM query_traces sfi_t2 WHERE sfi_t2.trace_id = {a}.generation_id"
        "             AND sfi_t2.requested_mode = 'salesforce')"
        f" ELSE EXISTS (SELECT 1 FROM chat_requests sfi_r WHERE sfi_r.generation_id = {a}.generation_id"
        "             AND sfi_r.request->>'mode' = 'salesforce') END)"
        "), false)"
    )


def meta_says_salesforce(meta: Optional[Mapping]) -> bool:
    """The meta half of the predicate, in Python (a meta dict in hand)."""
    if not isinstance(meta, Mapping):
        return False
    return (
        meta.get("mode") == "salesforce"
        or meta.get("engine") == "sf_assistant"
        or any(k in meta for k in SF_META_KEYS)
    )


def server_says_salesforce(generation_id: Optional[str], traces: Mapping, requests: Mapping) -> bool:
    """The server-row half of the predicate, in Python: `traces` maps a trace
    id (= generation id) to its requested mode, `requests` a generation id to
    its request's mode."""
    if not generation_id:
        return False
    if generation_id in traces:
        return traces[generation_id] == "salesforce"
    return requests.get(generation_id) == "salesforce"


def enabled() -> bool:
    return bool(getattr(settings, "toggle_off_isolation", True))


def frozen_holding_chats() -> frozenset:
    """Conversation ids the owner froze as Salesforce-holding although no row
    says so (TOGGLE_OFF_FROZEN_HOLDING_CHATS, data review F3). Such a chat
    reads like one that holds Salesforce only by a server row: every
    unstamped answer is hidden on a toggle-off turn, and the chat is left
    out of cross-chat recall and of new vectors. Empty while isolation is
    off. Read-time only: no stored row changes."""
    if not enabled():
        return frozenset()
    return frozenset(str(i) for i in (getattr(settings, "toggle_off_frozen_holding_chats", None) or ()))


# ---------------------------------------------------------------------------
# The user's Salesforce-holding chats, cached in this process (review R2)
# ---------------------------------------------------------------------------

#: The cross-chat reads (vector candidates, keyword recall) and the embedder
#: leave these chats out. One statement per user per TTL instead of a NOT
#: EXISTS re-scan per candidate row. The set only grows when a Salesforce
#: request is accepted or a row whose meta says Salesforce is stored, and
#: both call `forget_user` (main.py, db.add_message, db.replace_messages); it
#: shrinks on a delete, where a stale entry only leaves out more. The
#: orchestrator runs one process (Dockerfile CMD: one uvicorn worker).
_SF_CHATS_TTL_S = 300.0
_SF_CHATS_MAX_USERS = 512
_sf_chats_lock = threading.Lock()
_sf_chats: Dict[int, Tuple[float, int, frozenset]] = {}
_sf_chats_gen: Dict[int, int] = {}


def salesforce_chats(user_id: int) -> frozenset:
    """The ids of this user's conversations that hold Salesforce. Blocking
    (a DB read on a miss): call it from a worker thread."""
    uid = int(user_id)
    now = time.monotonic()
    with _sf_chats_lock:
        gen = _sf_chats_gen.get(uid, 0)
        hit = _sf_chats.get(uid)
        if hit is not None and hit[0] > now and hit[1] == gen:
            return hit[2]
    from . import db

    ids = frozenset(db.salesforce_chat_ids(uid))
    with _sf_chats_lock:
        if _sf_chats_gen.get(uid, 0) == gen:  # nothing changed while reading
            if len(_sf_chats) >= _SF_CHATS_MAX_USERS:
                _sf_chats.pop(next(iter(_sf_chats)), None)
            _sf_chats[uid] = (time.monotonic() + _SF_CHATS_TTL_S, gen, ids)
    return ids


def forget_user(user_id: Optional[int]) -> None:
    """A chat of this user may have become Salesforce-holding: read the set
    again next time. Cheap; safe to call from any thread."""
    if user_id is None:
        return
    uid = int(user_id)
    with _sf_chats_lock:
        _sf_chats_gen[uid] = _sf_chats_gen.get(uid, 0) + 1
        _sf_chats.pop(uid, None)


def note_salesforce_chat(user_id: Optional[int], conversation_id: Optional[str]) -> None:
    """This chat now holds Salesforce: drop the user's cached set unless it
    already lists the chat (an answer in a chat known to hold Salesforce
    changes nothing). Also drops a set being read right now."""
    if user_id is None:
        return
    uid = int(user_id)
    with _sf_chats_lock:
        hit = _sf_chats.get(uid)
        if hit is not None and conversation_id and str(conversation_id) in hit[2]:
            return
        _sf_chats_gen[uid] = _sf_chats_gen.get(uid, 0) + 1
        _sf_chats.pop(uid, None)


def forget_all() -> None:
    """Drop every cached set (tests, a flag flip)."""
    with _sf_chats_lock:
        for uid in list(_sf_chats_gen):
            _sf_chats_gen[uid] += 1
        _sf_chats.clear()


# ---------------------------------------------------------------------------
# Per-chat state
# ---------------------------------------------------------------------------


def _key(content: object) -> str:
    """The hash a history turn and a stored row are matched on: whitespace
    differences (a browser re-render, a trailing newline) do not count."""
    text = content if isinstance(content, str) else _flatten(content)
    return hashlib.sha1(" ".join(text.split()).encode("utf-8")).hexdigest()


def answer_key(content: object) -> str:
    """The public name of `_key`, for the Salesforce conversation memory
    (design 2026-11-25 §2.3): `sf_query_state` stores and matches its answer,
    parent and question hashes with this one helper, so the memory and this
    guard can never disagree about which turn a row belongs to. It adds no
    behaviour; it calls `_key`."""
    return _key(content)


def _flatten(content: object) -> str:
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


_TOKEN_RE = re.compile(r"[a-z0-9]+")
#: What the browser does not render into a selection: a link's target, an
#: HTML tag, a citation marker, a list or quote marker at a line start
#: (review F3: numbered lists and record links broke every shingle).
_RENDER_STRIP_RE = re.compile(
    r"\]\([^)\s]*\)|<[^>\n]{1,200}>|\[\d{1,3}\]|(?m:^[ \t]*(?:\d{1,3}[.)]|[-*+>])[ \t]+)"
)


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall(_RENDER_STRIP_RE.sub(" ", text or "").lower())


@dataclass
class ChatState:
    """What one conversation's stored rows say about Salesforce."""

    has_salesforce: bool = False
    #: Hashes of Salesforce answers (every branch).
    sf_keys: set = field(default_factory=set)
    #: Hashes of CLEAN assistant-mode answers (see the module docstring).
    clean_keys: set = field(default_factory=set)
    #: Hashes a Salesforce answer and a non-Salesforce answer both have: the
    #: answer is hidden, its question is not (review data F5).
    ambiguous_keys: set = field(default_factory=set)
    #: Hashes of questions asked in Salesforce mode and never outside it
    #: (review data F2: an empty or missing answer hid nothing next to it).
    sf_question_keys: set = field(default_factory=set)
    #: The texts of every hidden answer and of every clean answer and the
    #: person's own questions, for the quote check (indexed on first use).
    hidden_texts: List[str] = field(default_factory=list)
    clean_texts: List[str] = field(default_factory=list)
    #: No guarded toggle-off answer since the last Salesforce turn: this is
    #: the first toggle-off turn after it, so the status line is said.
    announce: bool = False
    #: The stored rows could not be read; `has_salesforce` is then the
    #: fail-closed choice made by `prepare`.
    read_failed: bool = False
    sf_answers: int = 0
    tainted_answers: int = 0
    _index: Optional[dict] = field(default=None, repr=False)

    # -- the quote index, built only when a quote has to be checked (R8) --

    def quote_index(self) -> dict:
        if self._index is None:
            self._index = {
                "hidden": _ShingleSet(self.hidden_texts),
                "clean": _ShingleSet(self.clean_texts),
            }
        return self._index


class _ShingleSet:
    """6-token shingles of some texts (capped), and their joined tokens for a
    short quote and for shingles past the cap."""

    def __init__(self, texts: Sequence[str]) -> None:
        self.shingles: set = set()
        self.capped = False
        joined: List[str] = []
        for text in texts:
            tokens = _tokens(text)
            joined.append(" ".join(tokens))
            if self.capped:
                continue
            for i in range(0, max(0, len(tokens) - SHINGLE + 1)):
                if len(self.shingles) >= MAX_SHINGLES:
                    self.capped = True
                    break
                self.shingles.add(" ".join(tokens[i : i + SHINGLE]))
        self.joined = " | " + " | ".join(joined) + " | "

    def has_phrase(self, phrase: str) -> bool:
        return f" {phrase} " in self.joined

    def share(self, shingles: Sequence[str]) -> float:
        """The share of `shingles` found here. Past the cap, a sample of at
        most `_SAMPLE` shingles is also searched in the joined text, so the
        cost stays a few scans however long the quote (review R8). A missed
        shingle only makes a quote look less clean, and an unclean quote is
        kept out."""
        if not shingles:
            return 0.0
        if not self.capped:
            return sum(1 for s in shingles if s in self.shingles) / len(shingles)
        step = max(1, len(shingles) // _SAMPLE)
        sample = list(shingles[::step])[:_SAMPLE]
        return sum(1 for s in sample if s in self.shingles or self.has_phrase(s)) / len(sample)


def build_state(rows: Iterable[Mapping]) -> ChatState:
    """ChatState from `db.salesforce_chat_state` rows. Pure.

    A row has `id`, `content`, `is_sf`, `mode`, `stamped`, and optionally
    `role` (default assistant), `is_sf_question` (a user row asked in
    Salesforce mode) and `holding` (a server row says this chat asked for
    Salesforce, review F1)."""
    ordered = sorted((dict(r) for r in rows), key=lambda r: int(r.get("id") or 0))
    answers = [r for r in ordered if (r.get("role") or "assistant") == "assistant"]
    questions = [r for r in ordered if r.get("role") == "user"]
    sf_ids = [int(r["id"]) for r in answers if r.get("is_sf")]
    sfq_ids = [int(r["id"]) for r in questions if r.get("is_sf_question")]
    holding = bool(sf_ids or sfq_ids or any(r.get("holding") for r in ordered))
    if not holding:
        return ChatState()
    marks = sf_ids + sfq_ids
    # A chat that holds Salesforce only by a server row (the answer stopped
    # and the question not stored yet): no stored row is known to come
    # before it, so only a stamped answer is clean (fail-closed).
    first_sf = min(marks) if marks else 0
    last_sf = max(marks) if marks else 0
    state = ChatState(has_salesforce=True)
    nonsf_keys: set = set()
    for r in answers:
        content = r.get("content")
        text = content if isinstance(content, str) else _flatten(content)
        if not text.strip():
            continue
        key = _key(text)
        if r.get("is_sf"):
            state.sf_keys.add(key)
            state.sf_answers += 1
            state.hidden_texts.append(text)
            continue
        nonsf_keys.add(key)
        if r.get("mode") == "assistant" and (int(r["id"]) < first_sf or r.get("stamped")):
            state.clean_keys.add(key)
            state.clean_texts.append(text)
        else:
            state.tainted_answers += 1
            state.hidden_texts.append(text)
    # A clean row that says exactly what a Salesforce answer said is not clean.
    state.ambiguous_keys = state.sf_keys & nonsf_keys
    state.clean_keys -= state.sf_keys
    own_question_keys: set = set()
    sf_question_keys: set = set()
    for r in questions:
        content = r.get("content")
        text = content if isinstance(content, str) else _flatten(content)
        if not text.strip():
            continue
        if r.get("is_sf_question"):
            sf_question_keys.add(_key(text))
        else:
            own_question_keys.add(_key(text))
            # Only the person's own words: a quote folded into a stored
            # question is not clean text, or quoting it again would pass.
            parsed = split_selected_context(text)
            state.clean_texts.append(parsed[1] if parsed is not None else text)
    # The same words asked in both modes are the person's own toggle-off words.
    state.sf_question_keys = sf_question_keys - own_question_keys
    state.announce = not any(
        r.get("stamped") and r.get("mode") == "assistant" and not r.get("is_sf") and int(r["id"]) > last_sf
        for r in answers
    )
    return state


# ---------------------------------------------------------------------------
# Selected-context quotes (frontend/lib/selectedContext.ts foldSelectedContext)
# ---------------------------------------------------------------------------

_QUOTE_HEAD_RE = re.compile(r"^Selected context from a previous (?:assistant|user) message:[ \t]*\n")
_FOLLOW_UP = "User follow-up:"


def split_selected_context(text: object) -> Optional[Tuple[str, str]]:
    """(quoted text, follow-up) of a turn the browser folded a quote into,
    or None for any other turn."""
    if not isinstance(text, str):
        return None
    head = _QUOTE_HEAD_RE.match(text)
    if head is None:
        return None
    rest = text[head.end():]
    quote_lines: List[str] = []
    follow = ""
    lines = rest.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(">"):
            quote_lines.append(line[2:] if line.startswith("> ") else line[1:])
        elif line.strip() == _FOLLOW_UP:
            follow = "\n".join(lines[i + 1 :]).strip()
            break
        i += 1
    quote = "\n".join(quote_lines).replace("[…excerpt truncated]", "").strip()
    return quote, follow


def _quote_hidden(quote: str, state: Optional[ChatState]) -> bool:
    """Must this quote be kept out? In a Salesforce-holding chat, yes unless
    it is the text of a clean answer or of the person's own question (an
    allow-list, review F3: the person selects RENDERED text, so a deny-list
    over stored Markdown missed numbered lists and record links). Outside
    such a chat a quote is the person's own chat and stays."""
    if state is None or not state.has_salesforce:
        return False
    tokens = _tokens(quote)
    if not tokens:
        return False
    if state.read_failed:
        return True  # cannot be checked: removed
    index = state.quote_index()
    hidden, clean = index["hidden"], index["clean"]
    phrase = " ".join(tokens)
    if len(tokens) < SHINGLE:
        if hidden.has_phrase(phrase):
            return True
        return not clean.has_phrase(phrase)
    shingles = [" ".join(tokens[i : i + SHINGLE]) for i in range(len(tokens) - SHINGLE + 1)]
    if hidden.share(shingles) > 0:
        return True
    if clean.has_phrase(phrase):
        return False
    return clean.share(shingles) < _CLEAN_SHARE


def quote_check(text: object, state: Optional[ChatState]) -> bool:
    """Does this text carry a selected-context quote that must be kept out?"""
    parsed = split_selected_context(text)
    if parsed is None:
        return False
    return _quote_hidden(parsed[0], state)


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


@dataclass
class Hidden:
    """What `redact` hid on one turn."""

    count: int = 0
    sf: int = 0
    tainted: int = 0
    users: int = 0
    quotes_removed: int = 0
    last_answer_hidden: bool = False


def redact(history: List[dict], state: Optional[ChatState]) -> Tuple[List[dict], Hidden]:
    """The history with every hidden turn replaced by its placeholder. Pure.

    Fast path: a chat that holds no Salesforce comes back unchanged, as the
    SAME object. Otherwise an assistant turn is hidden when it matches a
    Salesforce answer, or when it matches no clean row (a tainted or
    mode-less answer, or a turn the server cannot match, such as one not
    stored yet). A user turn is hidden when the server knows it as a
    Salesforce-mode question, and the user turn directly before a hidden
    Salesforce answer is hidden too, unless that answer's text is also a
    toggle-off answer's. The user turn before a tainted answer is the
    person's own toggle-off words and stays. A selected-context quote that
    is not clean text is replaced and the follow-up kept. System blocks are
    untouched.
    """
    hidden = Hidden()
    if state is None or not state.has_salesforce:
        return history, hidden
    out = [dict(m) if isinstance(m, dict) else m for m in history]
    last_assistant: Optional[int] = None
    hidden_assistant: set = set()

    def hide_user(i: int) -> None:
        if isinstance(out[i], dict) and out[i].get("role") == "user" and out[i].get("content") != USER_PLACEHOLDER:
            out[i]["content"] = USER_PLACEHOLDER
            hidden.users += 1

    for i, m in enumerate(history):
        if not isinstance(m, dict):
            continue
        if m.get("role") == "user":
            if state.sf_question_keys and _key(m.get("content")) in state.sf_question_keys:
                hide_user(i)
            continue
        if m.get("role") != "assistant":
            continue
        last_assistant = i
        key = _key(m.get("content"))
        if key in state.sf_keys:
            out[i]["content"] = SF_ANSWER_PLACEHOLDER
            hidden.sf += 1
            hidden_assistant.add(i)
            if i > 0 and key not in state.ambiguous_keys:
                hide_user(i - 1)
        elif key not in state.clean_keys:
            out[i]["content"] = TAINTED_PLACEHOLDER
            hidden.tainted += 1
            hidden_assistant.add(i)
    for i, m in enumerate(out):
        if not isinstance(m, dict) or m.get("role") != "user" or m.get("content") == USER_PLACEHOLDER:
            continue
        parsed = split_selected_context(m.get("content"))
        if parsed is None or not _quote_hidden(parsed[0], state):
            continue
        _quote, follow = parsed
        m["content"] = f"{QUOTE_PLACEHOLDER}\n\n{_FOLLOW_UP}\n\n{follow}" if follow else QUOTE_PLACEHOLDER
        hidden.quotes_removed += 1
    hidden.count = hidden.sf + hidden.tainted + hidden.users + hidden.quotes_removed
    hidden.last_answer_hidden = last_assistant is not None and last_assistant in hidden_assistant
    return out, hidden


@dataclass
class Prepared:
    """One toggle-off turn's isolation: the state, the redacted history,
    what was hidden, and whether the current text quotes kept-out text."""

    state: ChatState
    history: List[dict]
    hidden: Hidden
    quote_hidden: bool


def _prepare_sync(rows_fn, history: List[dict], text: object) -> Prepared:
    state = build_state(rows_fn())
    new_history, hidden = redact(history, state)
    return Prepared(state, new_history, hidden, quote_check(text, state))


_ABANDON_KW: Optional[dict] = None


def _abandon_kw() -> dict:
    """anyio's keyword for "stop waiting on cancel" (4.1+: abandon_on_cancel)."""
    global _ABANDON_KW
    if _ABANDON_KW is None:
        import anyio.to_thread

        params = inspect.signature(anyio.to_thread.run_sync).parameters
        _ABANDON_KW = {"abandon_on_cancel": True} if "abandon_on_cancel" in params else {"cancellable": True}
    return _ABANDON_KW


async def prepare(
    user_id: int,
    conversation_id: str,
    history: List[dict],
    text: object,
    *,
    fail_closed: bool,
) -> Prepared:
    """The chat's state, the redacted history and the current-text quote
    check, worked out in ONE worker thread (review R8: building hashes and
    shingles of a long Salesforce chat is CPU the event loop must not run).

    A read that fails or outlasts TOGGLE_OFF_STATE_TIMEOUT_S (review R6)
    counts the chat as Salesforce-holding only when `fail_closed` (the
    principal may use Salesforce): closing context for everyone during a
    database fault would break every chat to guard a case that is close to
    empty (design C16)."""
    from . import db, db_metrics

    import anyio.to_thread

    def rows():
        return db.salesforce_chat_state(int(user_id), str(conversation_id))

    work = db_metrics.wrap_thread_call(lambda: _prepare_sync(rows, history, text))
    timeout = float(getattr(settings, "toggle_off_state_timeout_s", 2.0) or 0)
    try:
        call = anyio.to_thread.run_sync(work, **_abandon_kw())
        return await (asyncio.wait_for(call, timeout) if timeout > 0 else call)
    except Exception as exc:  # noqa: BLE001 — says what it did in the log
        log.warning(
            "cannot read whether this chat holds Salesforce answers (%s); %s",
            type(exc).__name__,
            "hiding every earlier assistant turn this turn" if fail_closed else "context stays as sent",
            exc_info=not isinstance(exc, asyncio.TimeoutError),
        )
        metrics.inc(
            "toggle_off_state_read_failed_total",
            "toggle-off turns whose Salesforce state could not be read",
            closed="yes" if fail_closed else "no",
        )
        state = ChatState(has_salesforce=bool(fail_closed), read_failed=True, announce=bool(fail_closed))
        new_history, hidden = redact(history, state)
        return Prepared(state, new_history, hidden, quote_check(text, state))


async def load_state(user_id: int, conversation_id: str, *, fail_closed: bool) -> ChatState:
    """The chat's state alone (see `prepare`)."""
    return (await prepare(user_id, conversation_id, [], "", fail_closed=fail_closed)).state


def record(hidden: Hidden) -> None:
    """toggle_off_hidden_turns_total{kind}, once per hidden turn."""
    for kind, n in (("sf", hidden.sf + hidden.users), ("tainted", hidden.tainted), ("quote", hidden.quotes_removed)):
        for _ in range(n):
            metrics.inc("toggle_off_hidden_turns_total", "turns hidden from a toggle-off prompt", kind=kind)


def is_placeholder(content: object) -> bool:
    """Is this whole turn one of the three placeholders?"""
    return isinstance(content, str) and content.strip() in PLACEHOLDERS


def visible_text(content: object) -> str:
    """The turn's text with every placeholder removed: '' for a placeholder
    turn, the follow-up alone for a turn whose quote was removed, the text
    unchanged otherwise. What a web query, a retrieval query or a file may
    read of a turn (design rule 5)."""
    if not isinstance(content, str):
        return content  # type: ignore[return-value]
    stripped = content.strip()
    if stripped in PLACEHOLDERS or stripped == QUOTE_PLACEHOLDER:
        return ""
    if stripped.startswith(QUOTE_PLACEHOLDER):
        rest = stripped[len(QUOTE_PLACEHOLDER):].strip()
        if rest.startswith(_FOLLOW_UP):
            rest = rest[len(_FOLLOW_UP):].strip()
        return rest
    return content


def without_placeholders(turns: Sequence[dict]) -> List[dict]:
    """`turns` minus placeholder turns, quote placeholders stripped."""
    out: List[dict] = []
    for m in turns:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, str):
            out.append(m)
            continue
        text = visible_text(content)
        if not text.strip():
            continue
        out.append(m if text is content else {**m, "content": text})
    return out


# ---------------------------------------------------------------------------
# The "needs company records" detector (G7)
# ---------------------------------------------------------------------------

_W = re.IGNORECASE

_URL_RE = re.compile(r"\bhttps?://|\bwww\.", _W)
_FILE_WORD_RE = re.compile(
    r"\b(?:files?|sheets?|spreadsheets?|excel|csv|documents?|pdfs?|attach(?:ed|ment|ments)?|"
    r"upload(?:ed)?|pasted|below|this table|resume|cv|job description)\b",
    _W,
)
#: A follow-up that points at a file of the person's own, not at the hidden
#: answer: "summarise this file", "read the uploaded sheet" (review R11).
_FILE_REF_RE = re.compile(
    r"\b(?:this|that|the|my|these|those|attached|uploaded|pasted)\s+(?:uploaded\s+|attached\s+|pasted\s+)?"
    r"(?:files?|sheets?|spreadsheets?|documents?|docs?|pdfs?|cvs?|resumes?|uploads?|attachments?|"
    r"images?|photos?|pictures?|screenshots?|csvs?|workbooks?|decks?|slides?)\b"
    r"|\b(?:uploaded|attached|pasted)\b",
    _W,
)
_RECORD_NOUN_RE = re.compile(
    r"\b(?:candidates?|placements?|placed|interviews?|submissions?|recruiters?|employees?|"
    r"consultants?|invoices?|timesheets?|requisitions?|bench|joinings?|onboardings?|"
    r"offer letters?|payroll)\b",
    _W,
)
_MONTHS = (
    r"january|february|march|april|may|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_SHAPE_RE = re.compile(
    r"\b(?:how many|count|number of|list|show|give me|total|"
    r"(?:this|last|previous|current) (?:week|month|quarter|year)|today|yesterday|"
    rf"in (?:{_MONTHS})|20\d\d)\b",
    _W,
)
#: What makes a records shape a question about THE PERSON'S OWN company
#: (review R1: "how many employees does Google have?" is a public fact): the
#: organisation in the first person, or a recent window of time.
_ORG_ANCHOR_RE = re.compile(
    r"\b(?:we|our|ours|(?-i:us)|my team|my company|the company|in (?:the|our) (?:system|crm|database|records)|"
    r"internally|company-wide)\b",
    _W,
)
_TIME_ANCHOR_RE = re.compile(
    r"\b(?:(?:this|last|previous|current|past) (?:week|month|quarter|year|fy|financial year)|today|"
    rf"yesterday|so far|mtd|ytd|qtd|in (?:{_MONTHS}))\b",
    _W,
)
#: Code, data work and the person's own life are never a records lookup (R1).
_CODE_RE = re.compile(
    r"\b(?:sql|query|queries|pandas|dataframe|python|javascript|typescript|java|class|classes|model|"
    r"models|schema|regex|formula|script|code|function|django|flask|vba|api|json|orm|soql|apex|dax|"
    r"power bi|tableau|database design)\b",
    _W,
)
_OWN_LIFE_RE = re.compile(
    r"\b(?:i have|i've got|i've|i got|i(?:'m| am) an?|my (?:interview|interviews|offer|offers|joining|"
    r"onboarding|placement|resume|cv|salary|appraisal|notice period))\b",
    _W,
)
_HOW_TO_RE = re.compile(
    r"\b(?:questions?|tips|how (?:do|can|to|should)|should i|write|draft|template|example|"
    r"sample|ideas|best practices?|explain|meaning|definition|checklist|plan|steps|kpis?|reasons)\b",
    _W,
)
_SOURCE_RE = re.compile(r"\b(?:in|from) (?:our |the )?(?:salesforce|crm)\b|\bsalesforce (?:data|records)\b", _W)
#: Salesforce the product: admin, developer and comparison questions (R1).
_PRODUCT_RE = re.compile(
    r"\b(?:apex|triggers?|soql|sosl|apis?|integrat\w*|migrat\w*|connectors?|sync\w*|lightning|"
    r"visualforce|flows?|admin\w*|developers?|configur\w*|set ?up|install\w*|vs|versus|compar\w*|"
    r"difference|data cloud|marketing cloud|service cloud|einstein|certification|trailhead|export\w*|"
    r"import\w*|hubspot|dynamics|zoho|snowflake)\b",
    _W,
)
_DETAIL = (
    r"(?:profile|details?|contact(?: details| number| info(?:rmation)?)?|"
    r"phone(?: number| no\.?)?|mobile(?: number| no\.?)?|e-?mail(?: address| id)?|address|salary|"
    r"joining date|designation|manager|reports to)"
)
_ROLE = r"(?:employees?|candidates?|recruiters?|consultants?|colleagues?|team members?|staff)"
_GENERIC_FOLLOWER = (
    r"(?:benefits|rights|law|act|polic(?:y|ies)|engagement|retention|handbook|evaluation|appraisal|"
    r"training|experience|management|relations|satisfaction|template|form|referral|provident|stock|"
    r"options?|plan|plans|portal|scheme)"
)
#: A person's detail by company role, of OUR/THIS person or a NAMED one
#: (review R1: "salary for a consultant in Bangalore" is a market question).
_PERSON_DETAIL_RE = re.compile(
    rf"\b{_DETAIL}\s+(?:of|for)\s+"
    rf"(?:(?:our|my|this|that|these|those)\s+(?:[\w-]+\s+)?{_ROLE}\b(?!\s+{_GENERIC_FOLLOWER}\b)"
    rf"|(?:the\s+)?{_ROLE}\s+(?-i:[A-Z][a-z]+)\b)",
    _W,
)
_CHART_EXPORT_RE = re.compile(
    r"\b(?:chart|plot|graph|pie|bar|visuali[sz]e|export|excel|table|pdf|word file|docx|spreadsheet)\b",
    _W,
)
_REFERENCE_RE = re.compile(r"\b(?:this|that|these|those|it|them|above|previous|same|earlier)\b", _W)
_PERSON_PRONOUN_RE = re.compile(r"\b(?:him|her|his|their|them)\b", _W)
_OPENER_RE = re.compile(r"^\W*(?:and|what about|how about|also|now)\b", _W)


@dataclass(frozen=True)
class Block:
    """A toggle-off turn answered with a fixed reply, and why."""

    rule: str
    guard: str
    reply: str

    def meta(self) -> dict:
        return {"route": "chat", "toggle_off_guard": self.guard, "rule": self.rule}


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _rules() -> str:
    from .config import _detector_rules

    return _detector_rules(str(getattr(settings, "toggle_off_detector_rules", "ABCD") or ""))


def refers_to_a_file(text: str) -> bool:
    """Does this turn name a file (its own, or one already in the chat)?"""
    return bool(_FILE_WORD_RE.search(text or "") or _FILE_REF_RE.search(text or ""))


def needs_salesforce(
    text: str,
    *,
    hidden: Optional[Hidden] = None,
    last_assistant: str = "",
    attachments: bool = False,
    own_material: bool = False,
    quote_hidden: bool = False,
    rules: Optional[str] = None,
    has_toggle: bool = True,
    unchecked: bool = False,
) -> Optional[Block]:
    """The fixed reply this toggle-off turn gets, or None to answer it. Pure.

    E: the current text quotes kept-out text. Always on, whatever
    TOGGLE_OFF_DETECTOR_RULES says (review F2): it is a fact about the text,
    not a guess. When the chat's rows could not be read (`unchecked`) the
    reply says only that the quote could not be checked (R6).

    A, B and C answer "that needs your company's records" for a lookup of
    the person's own company's records, a question that takes Salesforce or
    the CRM as its source, and a person's details asked for by company role.
    They need a Salesforce toggle to offer (`has_toggle`, R5), and step aside
    for code, a how-to, the person's own life, a product question about
    Salesforce, and anything the person brought: an attachment, a paste, a
    link, a file word, or files of their own already in this chat
    (`own_material`).

    D says the earlier answer is hidden: a short follow-up on a hidden
    answer, or on a fixed reply that said so. Never after a records reply
    (R4), never when the rows could not be read, and never for a turn that
    points at a file (R11).

    Every pattern is word-bounded; the detector is conservative on purpose,
    because a missed records question can no longer leak anything — the
    context reads have removed every source.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    if quote_hidden:
        if unchecked:
            return Block("E", "unchecked_quote", REPLY_UNCHECKED)
        return Block("E", "hidden_quote", REPLY_HIDDEN if has_toggle else REPLY_HIDDEN_NO_TOGGLE)
    from .config import _detector_rules

    allowed = _rules() if rules is None else _detector_rules(str(rules))
    if not allowed:
        return None
    parsed = split_selected_context(text)
    ask = (parsed[1] if parsed is not None else text).strip()
    if not ask:
        return None
    from .core import pasted as _pasted

    own_turn = bool(attachments or _pasted.is_paste(ask) or _URL_RE.search(ask))
    after_hidden = not unchecked and (
        bool(hidden is not None and hidden.last_answer_hidden)
        or (last_assistant or "").strip() in _HIDDEN_REPLIES
    )
    if "D" in allowed and after_hidden and not own_turn and not _FILE_REF_RE.search(ask):
        words = _words(ask)
        if (
            (_CHART_EXPORT_RE.search(ask) and _REFERENCE_RE.search(ask))
            or (words <= 8 and (_REFERENCE_RE.search(ask) or _PERSON_PRONOUN_RE.search(ask)))
            or (words <= 6 and _OPENER_RE.search(ask))
        ):
            return Block("D", "hidden_followup", REPLY_HIDDEN if has_toggle else REPLY_HIDDEN_NO_TOGGLE)
    if not has_toggle or own_turn or own_material or _FILE_WORD_RE.search(ask):
        return None
    if _HOW_TO_RE.search(ask) or _CODE_RE.search(ask) or _OWN_LIFE_RE.search(ask):
        return None
    # C first: "phone number of the employee Ravi" has A's shape too, and the
    # rule the metric names should be the one that describes it.
    if "C" in allowed and _PERSON_DETAIL_RE.search(ask):
        return Block("C", "needs_salesforce", REPLY_NEEDS_SALESFORCE)
    if (
        "A" in allowed
        and _RECORD_NOUN_RE.search(ask)
        and _SHAPE_RE.search(ask)
        and (_ORG_ANCHOR_RE.search(ask) or _TIME_ANCHOR_RE.search(ask))
    ):
        return Block("A", "needs_salesforce", REPLY_NEEDS_SALESFORCE)
    if (
        "B" in allowed
        and _SOURCE_RE.search(ask)
        and (_RECORD_NOUN_RE.search(ask) or _ORG_ANCHOR_RE.search(ask))
        and not _PRODUCT_RE.search(ask)
    ):
        return Block("B", "needs_salesforce", REPLY_NEEDS_SALESFORCE)
    return None


def record_block(block: Block) -> None:
    metrics.inc(
        "toggle_off_needs_salesforce_total",
        "toggle-off turns answered with a fixed reply instead of a model",
        rule=block.rule,
    )
