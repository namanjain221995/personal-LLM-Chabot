"""LangGraph wiring: router → engine (spec §4/§8).

The graph carries an async `emit(event, data)` callback in its state; engines
push SSE-bound events through it while the graph runs. Engine modules are
imported lazily inside the nodes so importing app.graph stays light.
"""
from __future__ import annotations

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


async def _chat_node(state: ChatState) -> dict:
    # V2 (V2-DESIGN §3a): salesforce-mode router class "chat" — brief friendly
    # reply from the selected model, no data engines touched.
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
