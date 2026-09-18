"""LangGraph wiring: router → engine (spec §4/§8).

The graph carries an async `emit(event, data)` callback in its state; engines
push SSE-bound events through it while the graph runs. Engine modules are
imported lazily inside the nodes so importing app.graph stays light.
"""
from __future__ import annotations

import asyncio
import re
from typing import Awaitable, Callable, List, Optional, TypedDict

from langgraph.graph import END, StateGraph

Emit = Callable[[str, dict], Awaitable[None]]


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
_OBJECT_SCAN_LIMIT = 50


def _name_words(api: str, label: str) -> List[List[str]]:
    """An object's own names, as stemmed word sequences: its label, and its
    API name without the custom-object suffix ("Interview__c" -> interview)."""
    from .core.sf_dictionary import _stem

    split = [t for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", api) if t]
    if split and split[-1].lower() == "c" and api.endswith("__c"):
        split = split[:-1]
    names = [[_stem(w.lower()) for w in split], [_stem(w.lower()) for w in _WORD.findall(label or "")]]
    return [n for n in names if n]


def is_record_question(message: str) -> bool:
    """Does this Salesforce-mode message ask for a synced object's records?

    Pure apart from the cached org dictionary. True only when a data verb
    governs an object the question names outright (core/sf_dictionary: its
    whole label or API name, never a word shared with a field or with a setup
    object's label), the object is the head of its phrase, the question is
    anchored in this org's rows, and it is not advice. Precision over recall:
    a record question this misses still gets the assistant, told to say it
    will look the record up; a general question this caught would be answered
    by the SQL engine.
    """
    from . import fast_lane
    from .core import sf_dictionary
    from .core.schema_cache import _is_business_table

    text = message or ""
    if fast_lane.classify_pleasantry(text):
        return False
    strong = _STRONG_DATA_VERB.search(text) is not None
    weak_ends = [m.end() for m in _WEAK_DATA_VERB.finditer(text)]
    anchored = _ORG_ANCHOR.search(text) is not None
    if not (strong or weak_ends) or _ADVICE.search(text):
        return False
    if not (strong or anchored):
        return False
    # Not the default four: an org export lists setup objects named after the
    # same noun (FlowInterview, FlowInterviewLog...), and with enough matching
    # field labels four of them outrank Interview__c and push it out.
    objects = [
        o for o in sf_dictionary.relevant_objects(text, limit=_OBJECT_SCAN_LIMIT) if _is_business_table(o["api"])
    ]
    if not objects:
        return False
    tokens = list(_WORD.finditer(text))
    stems = [sf_dictionary._stem(t.group(0).lower()) for t in tokens]
    for obj in objects:
        for name in _name_words(obj["api"], obj.get("label", "")):
            for i in range(len(stems) - len(name) + 1):
                if stems[i : i + len(name)] != name:
                    continue
                last = i + len(name) - 1
                follower = tokens[last + 1].group(0).lower() if last + 1 < len(tokens) else ""
                if follower and follower not in _HEAD_FOLLOWERS:
                    continue
                if strong and (anchored or _proper_name(tokens, text, skip=range(i, last + 1))):
                    return True
                if anchored and any(
                    end <= tokens[i].start() and len(_WORD.findall(text[end : tokens[i].start()])) <= _WEAK_VERB_REACH
                    for end in weak_ends
                ):
                    return True
    return False


def _proper_name(tokens, text: str, *, skip) -> bool:
    """A capitalised word that does not start a sentence: "Priya", "Acme".
    Only strong verbs accept it as the anchor — "the tasks for Kubernetes"
    has one too."""
    for k, tok in enumerate(tokens):
        word = tok.group(0)
        if k in skip or k == 0 or not word[0].isupper() or word in ("I", "Salesforce"):
            continue
        before = text[: tok.start()].rstrip()
        if before and before[-1] in ".?!":
            continue
        return True
    return False


async def _chat_node(state: ChatState) -> dict:
    # V2 (V2-DESIGN §3a): salesforce-mode router class "chat" — the assistant
    # with the org's data available. A record question the router missed is
    # not chat: QA measured "Does the interview record for Priya exist and
    # when was it last updated?" forced here answering "I cannot access
    # Salesforce data" 3 of 3 runs. It goes to the engine that can look.
    # Off the event loop: the first call reads the org dictionary from disk
    # and every call scores its objects in Python.
    if await asyncio.to_thread(is_record_question, state["message"]):
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
