"""LangGraph wiring: router → engine (spec §4/§8).

The graph carries an async `emit(event, data)` callback in its state; engines
push SSE-bound events through it while the graph runs. Engine modules are
imported lazily inside the nodes so importing app.graph stays light.
"""
from __future__ import annotations

import asyncio
import bisect
import itertools
import logging
import re
from typing import Awaitable, Callable, List, Optional, Tuple, TypedDict

from langgraph.graph import END, StateGraph

Emit = Callable[[str, dict], Awaitable[None]]

logger = logging.getLogger(__name__)


class ChatState(TypedDict, total=False):
    message: str
    session_id: str
    image_base64: Optional[str]
    history: List[dict]
    route: str
    answer: str
    emit: Emit
    # V2 (V2-DESIGN §3a): model picker + reasoning effort for the chat route.
    model_choice: str
    effort: str


async def _router_node(state: ChatState) -> dict:
    from .engines.router import route_request

    # §10: meta is emitted exactly ONCE per turn — the engine's single final
    # meta (which carries `route`). The router must NOT emit an early meta:
    # the frontend replaces meta wholesale on every meta event, so a second
    # emit without `route` would clobber it.
    route = await route_request(
        state["message"],
        bool(state.get("image_base64")),
        state.get("history") or (),
    )
    return {"route": route}


async def _sql_node(state: ChatState) -> dict:
    from .engines.sql import run_sql_engine

    answer = await run_sql_engine(state["message"], state.get("history", []), state["emit"])
    return {"answer": answer}


async def _rag_node(state: ChatState) -> dict:
    from .engines.rag import run_rag_engine

    answer = await run_rag_engine(state["message"], state.get("history", []), state["emit"])
    return {"answer": answer}


async def _vision_node(state: ChatState) -> dict:
    from .engines.vision import run_vision_engine

    answer = await run_vision_engine(
        state["message"], state.get("image_base64"), state.get("history", []), state["emit"]
    )
    return {"answer": answer}


async def _report_node(state: ChatState) -> dict:
    from .engines.report import run_report_engine

    answer = await run_report_engine(state["message"], state.get("history", []), state["emit"])
    return {"answer": answer}


#: Data verbs that ask about one record's existence or state.
_STRONG_DATA_VERB = re.compile(r"\b(?:exists?|last (?:updated|modified)|status of)\b", re.I)
#: Data verbs that also open ordinary requests ("list ten interview
#: questions", "show me a flowchart"): they count only right before the
#: object they govern.
_WEAK_DATA_VERB = re.compile(r"\b(?:how many|count|show|list)\b", re.I)
_WEAK_VERB_REACH = 4
#: The question is about THIS org's rows, not the idea of a lead or a task.
#: A bare period ("this year") is not an anchor: "list 5 events for team
#: building this year" asks for ideas.
_ORG_ANCHOR = re.compile(
    r"\b(?:records?|we|our|us|today|yesterday|since|open|closed|pending|scheduled|"
    r"won|lost|active|created|in (?:salesforce|the org|the crm))\b",
    re.I,
)
#: Advice about records, not a request for them.
_ADVICE = re.compile(
    r"\b(?:should|typical(?:ly)?|usually|ideal|best practices?|recommend\w*|advice|tips?|"
    r"examples?|templates?|explain|how (?:to|do i|do you|should))\b",
    re.I,
)
#: Words that may follow an object's name when the object is the thing asked
#: about. Anything else makes the name a modifier: "interview questions",
#: "task list", "case study", "lead generation".
_HEAD_FOLLOWERS = frozenset(
    "record records id ids status for of with about in on at from by that which where whose who "
    "named called and or is are was were has have had did do does exist exists existed created "
    "updated modified closed opened open owned assigned scheduled last this today yesterday since "
    "between before after per we i you they there still".split()
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*")
#: Longest ask this check reads, in words. The record questions it exists
#: for run 7 to 17 words (the ones in tests/test_salesforce_chat_redispatch.py);
#: a longer ask is a brief or a note, which the router has already judged.
#: It is also what bounds the work: reading the whole message, the check
#: measured 10.19 s on 64,000 chars and had not finished 50,424 chars after
#: 120 s (QA), in a thread that /chat/stop cannot cancel.
_ASK_MAX_WORDS = 40
#: Verbs asking for a piece of writing, or for a change to given text. The
#: SQL engine answers with rows: it cannot write the email, the translation or
#: the Apex class. QA measured 8 of 8 such asks sent to it once they named a
#: record ("Write an Apex test class that asserts an Account record exists
#: after insert.").
_WORK_VERB = re.compile(
    r"(?:write|draft|compose|translate|rewrite|rephrase|reword|paraphrase|proofread|edit|fix|correct|"
    r"polish|tidy|format|reformat|summari[sz]e|condense|shorten|expand|simplify|make|create|generate|"
    r"turn|convert|improve|review|critique|brainstorm|outline|design|draw|sketch|code|implement|build|"
    r"prepare|reply|respond|imagine|pretend|describe)\b",
    re.I,
)
#: What may stand in front of a request's verb: "Hi, can you please write".
_REQUEST_LEAD = re.compile(
    r"(?:(?:hi|hey|hello|ok|okay|so|and|also|now|please|pls|kindly|just|can|could|would|will|you)\b[\s,!.]*"
    r"|i (?:need|want|would like|'d like) (?:you )?to\b\s*|help me\b\s*|let's\b\s*)*",
    re.I,
)
_SENTENCE_BREAK = re.compile(r"(?<=[.?!;])\s+")


#: How much of the chosen line the_ask reads. The quote patterns below
#: backtrack within one line, so the line is bounded before they run; a
#: request is a sentence or two, and the composer has no paste limit.
_ASK_SCAN_CHARS = 2000
#: A line that opens like a person asking: a question word, an auxiliary, a
#: politeness lead, or an imperative. Pasted notes, emails and rulebooks open
#: with a name or a noun ("Priya said", "Hi team,", "Candidates should").
_REQUEST_OPENER = re.compile(
    r"^[\W_]*(?:(?:hi|hey|hello|ok|okay|so|and|also|now)\b[\s,!.]*)*"
    r"(?:how|what|what's|whats|when|where|which|who|whose|why|is|are|was|were|do|does|did|can|could|"
    r"would|will|should|has|have|had|may|please|pls|kindly|i need|i want|i would like|i'd like|help|let's|"
    r"write|draft|compose|translate|rewrite|rephrase|reword|paraphrase|proofread|edit|fix|correct|polish|"
    r"tidy|format|summari[sz]e|condense|shorten|expand|simplify|explain|describe|make|create|generate|"
    r"turn|convert|improve|review|give|show|list|tell|find|get|count|check|look|pull|compare|analy[sz]e|"
    r"suggest|recommend|outline|plan|draw|build|prepare|reply|respond|read|extract)\b",
    re.IGNORECASE,
)
#: Quoted spans are material the person is showing, not asking: a straight
#: or curly double-quoted span, a curly single-quoted one, a straight single
#: quote only at word edges ("Priya's" is an apostrophe), and inline code.
_QUOTED_SPAN = re.compile(
    r'"[^"]*"|“[^”]*”|‘[^’]*’|(?<![\w\'])\'[^\'\n]*\'(?!\w)|`[^`]*`'
)
#: "Summarize this for me: <what they pasted>" — a colon that is followed by
#: space or ends the line introduces material. "10:30" and "https://" do not.
_LEAD_COLON = re.compile(r":(?=\s|$)")


def the_ask(message: str) -> str:
    """The person's own request inside a message, without the material they
    handed over with it.

    The composer folds a paste into the message with no marker, and the old
    composer put pasted blocks IN FRONT of the typed instruction; so the
    words a message carries are not all the person's. is_record_question
    reads only the person's: QA measured 'Summarize this for me:\\n\\nCase
    00012345: Status of the case updated to Closed ...' going to the SQL
    engine when it read the whole message. (core/pasted.own_words is the same
    idea for pastes of 300+ chars; a record question's paste is often a
    one-line email, so it cannot be the cut here.)

    The ask is ONE line: a single-line message is its own ask; otherwise the
    first line when it opens like a request, else the last line when that
    does, else nothing. Within it, quoted spans go, and so does everything
    from a colon that introduces material. Precision over recall on purpose:
    an empty ask means "no record question", which is what the chat class
    decided before it read the message at all.
    """
    text = (message or "").strip()
    if not text:
        return ""
    first_break = text.find("\n")
    if first_break < 0:
        line = text
    else:
        first = text[:first_break].strip()
        last = text[text.rfind("\n") + 1 :].strip()
        if _REQUEST_OPENER.match(first[:_ASK_SCAN_CHARS]):
            line = first
        elif _REQUEST_OPENER.match(last[:_ASK_SCAN_CHARS]):
            line = last
        else:
            return ""
    line = _QUOTED_SPAN.sub(" ", line[:_ASK_SCAN_CHARS])
    lead = _LEAD_COLON.search(line)
    if lead:
        line = line[: lead.start()]
    return " ".join(line.split())


def _name_words(api: str, label: str) -> List[List[str]]:
    """An object's own names, as stemmed word sequences: its label, and its
    API name without the custom-object suffix ("Interview__c" -> interview)."""
    from .core.sf_dictionary import _stem

    split = [t for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", api) if t]
    if split and split[-1].lower() == "c" and api.endswith("__c"):
        split = split[:-1]
    names = [[_stem(w.lower()) for w in split], [_stem(w.lower()) for w in _WORD.findall(label or "")]]
    return [n for n in names if n]


def _synced_objects() -> List[Tuple[str, str]]:
    """(api, label) of every business object the SQL engine can answer from.

    The org dictionary's objects when one is loaded, otherwise the
    warehouse's own tables — the list the SQL engine grounds on. Production
    has no dictionary (QA, 2026-09-18: /data/sf_dictionary.json absent,
    "objects 0 fields 0"), so reading the dictionary alone, this check never
    fired there. Only names and labels are read: scoring fields
    (sf_dictionary.relevant_objects) raised KeyError on an entry without a
    'label' or 'fields' and cost the person the turn.
    """
    from .core import sf_dictionary
    from .core.schema_cache import _is_business_table

    objects = sf_dictionary.load().get("objects")
    if objects:
        pairs = [(o.get("api"), o.get("label")) for o in objects.values() if isinstance(o, dict)]
    else:
        pairs = [(table, None) for table in _warehouse_tables()]
    return [(api, str(label or "")) for api, label in pairs if isinstance(api, str) and api and _is_business_table(api)]


def _warehouse_tables() -> List[str]:
    import os

    from .config import settings
    from .core.schema_cache import schema_cache

    path = settings.duckdb_path
    if not os.path.exists(path):
        return []
    return list(schema_cache.get(path))


def _is_work_ask(ask: str) -> bool:
    """Does a sentence of the ask open with a verb asking for writing work?"""
    for sentence in _SENTENCE_BREAK.split(ask):
        sentence = sentence.lstrip(" \t\"'*#>-")
        lead = _REQUEST_LEAD.match(sentence)
        if _WORK_VERB.match(sentence, lead.end() if lead else 0):
            return True
    return False


def is_record_question(message: str) -> bool:
    """Does this Salesforce-mode message ask for a synced object's records?

    Pure apart from the cached org dictionary (or, without one, the cached
    warehouse schema). Reads only the person's own ask (engines/chat.the_ask):
    text they pasted or quoted is data and never chooses the engine. True only
    when the ask is short, is not a request for writing work or for advice, a
    data verb governs an object it names outright (the object's whole label or
    API name), the object is the head of its phrase, and the ask is anchored
    in this org's rows. Precision over recall: a record question this misses
    still gets the assistant, told to say it will look the record up; a
    general question or a piece of writing this caught would be answered by
    the SQL engine.
    """
    from . import fast_lane
    from .core.sf_dictionary import _stem

    if fast_lane.classify_pleasantry(message or ""):
        return False
    ask = the_ask(message)
    tokens = list(itertools.islice(_WORD.finditer(ask), _ASK_MAX_WORDS + 1))
    if not tokens or len(tokens) > _ASK_MAX_WORDS:
        return False
    strong = _STRONG_DATA_VERB.search(ask) is not None
    weak_ends = [m.end() for m in _WEAK_DATA_VERB.finditer(ask)]
    anchored = _ORG_ANCHOR.search(ask) is not None
    if not (strong or weak_ends) or _ADVICE.search(ask) or _is_work_ask(ask):
        return False
    if not (strong or anchored):
        return False
    objects = _synced_objects()
    if not objects:
        return False
    stems = [_stem(t.group(0).lower()) for t in tokens]
    # Computed once per ask, not once per candidate object: per candidate,
    # a prefix copy per capitalised word and a findall per weak verb made the
    # check quadratic-to-cubic in the message length (QA, security review).
    proper = [k for k in range(len(tokens)) if _is_proper_name(ask, tokens, k)]
    after_weak = [bisect.bisect_left([t.start() for t in tokens], end) for end in weak_ends]
    for api, label in objects:
        for name in _name_words(api, label):
            for i in range(len(stems) - len(name) + 1):
                if stems[i : i + len(name)] != name:
                    continue
                last = i + len(name) - 1
                follower = tokens[last + 1].group(0).lower() if last + 1 < len(tokens) else ""
                if follower and follower not in _HEAD_FOLLOWERS:
                    continue
                if strong and (anchored or any(k < i or k > last for k in proper)):
                    return True
                if anchored and any(0 <= i - j <= _WEAK_VERB_REACH for j in after_weak):
                    return True
    return False


def _is_proper_name(text: str, tokens, k: int) -> bool:
    """Token k is a capitalised word that does not start a sentence: "Priya",
    "Acme". Only strong verbs accept it as the anchor — "the tasks for
    Kubernetes" has one too."""
    word = tokens[k].group(0)
    if k == 0 or not word[0].isupper() or word in ("I", "Salesforce"):
        return False
    gap = text[tokens[k - 1].end() : tokens[k].start()].rstrip()
    return not (gap and gap[-1] in ".?!")


async def _chat_node(state: ChatState) -> dict:
    # V2 (V2-DESIGN §3a): salesforce-mode router class "chat" — the assistant
    # with the org's data available. A record question the router missed is
    # not chat: QA measured "Does the interview record for Priya exist and
    # when was it last updated?" forced here answering "I cannot access
    # Salesforce data" 3 of 3 runs. It goes to the engine that can look.
    # Off the event loop: the first call reads the org dictionary (or the
    # warehouse schema) from disk.
    try:
        record = await asyncio.to_thread(is_record_question, state["message"])
    except Exception:  # noqa: BLE001 — the dispatch is an extra; the answer is not
        # At 4810da0 this node never read the dictionary, so a malformed one
        # could not cost the person the turn; QA measured it costing it.
        logger.warning("chat node: record-question check failed, answering on the chat class", exc_info=True)
        record = False
    if record:
        from .engines.sql import run_sql_engine

        answer = await run_sql_engine(state["message"], state.get("history", []), state["emit"])
        return {"answer": answer, "route": "sql"}

    from .engines.chat import run_chat_engine

    answer = await run_chat_engine(
        state["message"],
        state.get("history", []),
        state["emit"],
        mode="salesforce",
        model_choice=state.get("model_choice", "smart"),
        effort=state.get("effort", "medium"),
    )
    return {"answer": answer}


def build_graph():
    graph = StateGraph(ChatState)
    graph.add_node("router", _router_node)
    graph.add_node("sql", _sql_node)
    graph.add_node("rag", _rag_node)
    graph.add_node("vision", _vision_node)
    graph.add_node("report", _report_node)
    graph.add_node("chat", _chat_node)

    graph.set_entry_point("router")
    graph.add_conditional_edges(
        "router",
        lambda state: state["route"],
        {"sql": "sql", "rag": "rag", "vision": "vision", "report": "report", "chat": "chat"},
    )
    for node in ("sql", "rag", "vision", "report", "chat"):
        graph.add_edge(node, END)
    return graph.compile()


_compiled = None


def get_graph():
    global _compiled
    if _compiled is None:
        _compiled = build_graph()
    return _compiled
