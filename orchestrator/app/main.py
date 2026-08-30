"""FastAPI entrypoint (spec §8/§10 + V2-DESIGN §1-§3): POST /chat (SSE),
GET /reports, GET /reports/{filename}, GET /health, /auth/*, /history/*."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
import uuid
from typing import AsyncIterator, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, model_validator

from . import context, db, llm
from .auth import router as auth_router
from .config import settings

# App-module logging was silently dropped: uvicorn configures only its own
# loggers, and with no root handler every app `log.info/warning/error` —
# the generation-usage telemetry, best-of-N losers, and the wall-clock hang
# guard's LOUD error — went nowhere. One root handler, level via LOG_LEVEL
# (default INFO), added only when nothing else configured logging first.
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
from .core.report_paths import ReportPathError, list_reports, resolve_report_file
from .graph import get_graph
from .health import check_dependencies
from .history import foldable_counts, router as history_router
from .memory_api import router as memory_router
from .uploads import router as uploads_router
from .memory import memory
from .sse import HEARTBEAT_SECONDS, sse_comment, sse_event

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Open the connection pool and apply migrations BEFORE serving a request.

    The SQLite layer migrated lazily on every connection, so a broken migration
    used to surface on the first user request — long after the deploy looked
    healthy. Doing it here makes a bad migration fail startup, which is what a
    container healthcheck can actually catch. It is also now the ONLY place the
    schema is applied: accessors no longer replay the DDL, which is where most
    of the old per-operation cost went.

    Run in a thread: pool startup dials PostgreSQL, and blocking the loop
    during startup would stall the readiness probe alongside it.

    `wait_for_database` comes first because after a reboot the Docker daemon
    starts every container simultaneously and ignores `depends_on` — so this
    process can, and does, come up before its own database. Waiting turns a
    crash-restart loop into one clean start.
    """
    await db.run_in_thread(db.wait_for_database)
    await db.run_in_thread(db.init_schema)
    try:
        yield
    finally:
        await db.run_in_thread(db.close_pool)


app = FastAPI(title="TechSara Orchestrator", version="0.2.0", lifespan=lifespan)

# Local platform: ONLY the local Next.js frontend origins are allowed. A
# wildcard here would let any web page the user visits cross-origin read
# /reports and drive /chat against the synced Salesforce data (§1/§12).
# V2: allow_credentials so the ts_session cookie flows on /auth + /history.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# V2 (V2-DESIGN §3c): /auth + /history are the account boundary; /chat and
# /reports* remain auth-free.
app.include_router(auth_router)
app.include_router(history_router)
app.include_router(uploads_router)
app.include_router(memory_router)


class LiveGeneration:
    """A model generation DETACHED from the HTTP request that started it.

    ChatGPT-style lifecycle: the worker task runs to completion even if the
    browser tab reloads or navigates away. Every SSE event is kept in an
    in-memory buffer, so any number of readers can `follow()` — the original
    POST /chat response, and later GET /chat/attach/{id} re-connections, which
    replay the buffer from the start and then stream live.

    If the generation finishes while NOBODY is attached, the answer is
    persisted server-side into the conversation (when the requester was
    signed in), so it is waiting in history after a reload. When a reader IS
    attached, the frontend persists as usual and the server stays out of it —
    that ordering avoids duplicate assistant messages.
    """

    def __init__(self, conversation_id: Optional[str], user_id: Optional[int]):
        self.conversation_id = conversation_id
        self.user_id = user_id
        # Idempotency key for this answer. Every attached client receives it in
        # the final meta and sends it back when persisting, so a reply watched
        # by two browsers is still stored exactly once (db.add_message).
        self.generation_id = uuid.uuid4().hex
        self.events: List[tuple] = []
        self.done = False
        self.cond = asyncio.Condition()
        self.subscribers = 0
        self.task: Optional[asyncio.Task] = None
        self.answer = ""
        self.final_meta: Optional[dict] = None
        self.cancelled = False
        self.failed = False

    async def publish(self, event: str, data: dict) -> None:
        async with self.cond:
            self.events.append((event, data))
            self.cond.notify_all()

    async def finish(self) -> None:
        async with self.cond:
            self.done = True
            self.cond.notify_all()

    async def follow(self) -> AsyncIterator[str]:
        """Replay buffered events, then stream live ones until the end.

        Planning, retrieval and a long thinking pass all produce NO events for
        minutes at a time. Waiting on the condition without a bound made the
        response body go completely silent for that whole stretch, and every
        idle-timeout in the path treats silence as a dead peer — Node/undici
        in the Next.js proxy cut the stream at 300s (UND_ERR_BODY_TIMEOUT) and
        the user was told the orchestrator was unreachable while the model was
        still working. Bounding the wait lets us emit an SSE comment instead:
        it carries no event, so the contract is untouched, but it proves the
        connection is alive. The frame is yielded OUTSIDE the condition's lock
        — holding it across a yield would block publish() for as long as the
        consumer takes to drain.
        """
        self.subscribers += 1
        try:
            index = 0
            while True:
                frame: Optional[str] = None
                async with self.cond:
                    while index >= len(self.events) and not self.done:
                        try:
                            await asyncio.wait_for(
                                self.cond.wait(), HEARTBEAT_SECONDS
                            )
                        except asyncio.TimeoutError:
                            break  # idle — fall through and send a keep-alive
                    if index < len(self.events):
                        event, data = self.events[index]
                        index += 1
                        frame = sse_event(event, data)
                    elif self.done:
                        break  # done and fully drained
                yield frame if frame is not None else sse_comment()
        finally:
            self.subscribers -= 1


# One live generation per conversation key. Finished generations are removed
# immediately (attach on a finished one 404s and the client loads history).
_live_generations: dict = {}


# Detached background-compaction tasks. Held so the event loop keeps a strong
# reference (an unreferenced task can be garbage-collected mid-run).
_background_tasks: set = set()


def _spawn_background_compaction(
    conv_key: str, history: list, *, base_url: str, model: str
) -> None:
    """Start a compaction that outlives this request's stream."""
    from . import compaction

    task = asyncio.create_task(
        compaction.maybe_background_compact(
            conv_key, history, base_url=base_url, model=model
        )
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _finalize_generation(conv_key: str, gen: LiveGeneration) -> None:
    """Mark done, unregister, and persist the answer if nobody received it."""
    await gen.finish()
    if _live_generations.get(conv_key) is gen:
        _live_generations.pop(conv_key, None)
    if (
        not gen.cancelled
        and not gen.failed
        and gen.subscribers == 0
        and gen.user_id is not None
        and gen.conversation_id
        and gen.answer
    ):
        # This is the ONLY copy of the answer: nobody was attached when it
        # finished, so no client will persist it. A bare `suppress(Exception)`
        # here loses the user's reply without a trace — and gives no way to
        # tell "nothing to save" apart from "the save failed". Still
        # best-effort (a storage failure must not take the process down), but
        # audible.
        try:
            stored = await db.run_in_thread(
                db.add_message,
                gen.user_id,
                gen.conversation_id,
                "assistant",
                gen.answer,
                gen.final_meta,
            )
            if stored is None:
                logging.getLogger(__name__).warning(
                    "detached answer for conversation %s was not stored: no such "
                    "conversation for user %s",
                    gen.conversation_id,
                    gen.user_id,
                )
        except Exception as exc:  # noqa: BLE001 — best-effort, but never silent
            logging.getLogger(__name__).warning(
                "failed to persist the detached answer for conversation %s: %s: %s",
                gen.conversation_id,
                type(exc).__name__,
                exc,
            )
    elif gen.answer and gen.conversation_id and not gen.cancelled and not gen.failed:
        logging.getLogger(__name__).debug(
            "not persisting %s server-side: subscribers=%s user_id=%s",
            gen.conversation_id,
            gen.subscribers,
            gen.user_id,
        )


class ChatMessage(BaseModel):
    role: str
    content: str = ""


# Composer multi-upload cap (2026-08-05): base64 images ride in the JSON chat
# body, so five 10 MB uploads ≈ 67 MB of payload — a deliberate ceiling, not
# an arbitrary one.
MAX_IMAGES = 5


class ChatRequest(BaseModel):
    """Chat request per spec §8 + V2-DESIGN §1:
    {messages, session_id, image?, conversation_id?, mode?, model?, effort?, agent?}.

    The flat {message, image_base64} shape the Next.js proxy sends is also
    accepted; `message` wins when both are present.
    """

    messages: Optional[List[ChatMessage]] = None
    message: Optional[str] = None
    session_id: str = "default"
    image: Optional[str] = None
    image_base64: Optional[str] = None
    # 2026-08-05: up to MAX_IMAGES images in one turn (composer multi-upload).
    # `image`/`image_base64` remain the single-image back-compat spelling.
    images: Optional[List[str]] = None
    # --- V2 optional fields (defaults preserve v1 behavior) ---
    conversation_id: Optional[str] = None
    mode: Literal["salesforce", "assistant"] = "salesforce"
    # The composer's "Live Salesforce" toggle: answer straight from the org
    # (any object/field this integration user can read) instead of the synced
    # copy. Only meaningful in salesforce mode; ignored elsewhere.
    sf_live: bool = False
    model: Literal["smart", "fast"] = "smart"
    effort: Literal[
        "fast", "think", "max", "low", "medium", "high", "extra_high"
    ] = "think"
    agent: bool = False
    # Deep Research (2026-08-30): the iterative mode — plan, search, read,
    # find the gaps, search again, then write a cited report. Explicit only:
    # it costs minutes and the whole search budget, so nothing infers it.
    deep_research: bool = False
    # V8: an uploaded PDF (base64, optionally a data: URL) + its filename.
    pdf: Optional[str] = None
    pdf_filename: Optional[str] = None
    # Phase 1: web search — "off" (never), "on" (force), "auto" (model decides).
    web_search: Literal["off", "auto", "on"] = "off"
    # Salesforce Intelligence Mode: the answer to a clarifying question this
    # conversation is waiting on. Present → the ORIGINAL request resumes with
    # this answer folded in, instead of `message` being treated as a new one.
    # Absent → an ordinary send, which may still be READ as an answer when a
    # question is pending (engines/sf_intel.py decides, not the client).
    clarification: Optional[dict] = None

    @property
    def pdf_data(self) -> Optional[str]:
        return self.pdf

    @property
    def text(self) -> str:
        if self.message and self.message.strip():
            return self.message.strip()
        for m in reversed(self.messages or []):
            if m.role == "user" and m.content.strip():
                return m.content.strip()
        return ""

    @property
    def images_data(self) -> List[str]:
        """Every attached image, list form — `images` wins over the single
        back-compat fields, which become a one-element list."""
        imgs = [i for i in (self.images or []) if i and i.strip()]
        if imgs:
            return imgs
        single = self.image_base64 or self.image
        return [single] if single else []

    @property
    def image_data(self) -> Optional[str]:
        """First image or None — the truthiness gate the routing checks use."""
        data = self.images_data
        return data[0] if data else None

    @property
    def history_messages(self) -> List[dict]:
        """Prior turns of THIS conversation, from the messages the frontend
        sends — the authoritative within-chat memory (survives restarts, unlike
        the in-process dict). The trailing user turn is the current `text`, so
        it is dropped here."""
        out = [
            {"role": m.role, "content": m.content}
            for m in (self.messages or [])
            if m.content and m.content.strip()
        ]
        if out and out[-1]["role"] == "user":
            out.pop()
        return out

    @model_validator(mode="after")
    def _canonical_effort(self) -> "ChatRequest":
        # Legacy wire values (low/medium/high/extra_high) normalize to the
        # 3-level ladder HERE, once — every engine and the trust metadata
        # (meta.effort) see only fast|think|max.
        self.effort = llm.normalize_effort(self.effort)
        return self

    @model_validator(mode="after")
    def _require_input(self) -> "ChatRequest":
        if len(self.images_data) > MAX_IMAGES:
            raise ValueError(f"at most {MAX_IMAGES} images per message")
        if (
            not self.text
            and not self.image_data
            and not self.pdf_data
            # Answering a clarification by clicking "Skip" carries no text of
            # its own; the request it resumes is what supplies the question.
            and not self.clarification
        ):
            raise ValueError(
                "provide a non-empty message/messages, an image, or a PDF"
            )
        return self


@app.get("/health")
async def health() -> dict:
    """§8: /health checks the model servers and DuckDB — under the all-vLLM
    override, the four vLLM services plus the warehouse. Overall status is
    "degraded" (never a static ok) when any dependency check fails, with
    per-dependency detail in `checks`."""
    report = await check_dependencies()
    return {
        "status": report["status"],
        "service": "orchestrator",
        "version": app.version,
        "checks": report["checks"],
        # Additive (2026-08-11): the VERIFIED effective context length, the
        # request budget derived from it, and the serving flags the app
        # believes are set. `status` is untouched — a window mismatch is a
        # configuration fact to surface, not a dependency outage.
        "context": report.get("context", {}),
    }


@app.get("/reports")
async def reports_index() -> dict:
    return {"reports": list_reports(settings.reports_dir)}


@app.get("/reports/{filename}")
async def get_report(filename: str) -> FileResponse:
    try:
        path = resolve_report_file(settings.reports_dir, filename)
    except ReportPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="report not found")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return FileResponse(path, filename=filename, media_type=media_type)


@app.post("/chat")
async def chat(request: ChatRequest, http_request: Request) -> StreamingResponse:
    """Stream SSE events (§10 + V2-DESIGN §2/§3a/§3b).

    - agent=true → agent engine (plan/execute/synthesize, any mode)
    - mode="assistant" → plain streamed completion, router + data engines
      bypassed entirely
    - mode="salesforce" (default) → v1 router → engine graph (now with the
      5th "chat" class)

    Event order: reasoning/step/token deltas → ONE meta (carrying `route` +
    engine keys, plus the V2 mode/model/effort keys) → done. On failure a
    terminal `error` event is sent instead of `done`.

    The generation itself is DETACHED (LiveGeneration): closing the response
    does not cancel it — use POST /chat/stop for that, GET /chat/attach/{id}
    to re-join it, and GET /chat/active to see what is still running.
    """
    # Image-only sends still need a text instruction for the vision engine.
    # Gated on there actually BEING an image: a Skip click carries no text of
    # its own (`_require_input` permits that), and without the gate the
    # placeholder became the request — so answering a clarifying question by
    # skipping it sent "Analyze the attached image." to the Salesforce planner
    # as the thing the user wanted to know.
    text = request.text or ("Analyze the attached image." if request.image_data else "")

    def meta_extras(route: Optional[str]) -> dict:
        """V2 §2: meta gains mode / model (served model id) / effort — merged
        in centrally so the engines keep emitting their v1 shapes untouched.

        meta is trust metadata, so model/effort must describe what actually
        served the answer, keyed on the engine's own `route`. The picker and
        effort apply where the design routes them: the chat engine (§3a),
        agent synthesis (§3b, smart-pinned) and — since 2026-08-28 — the
        vision route. The data engines stay pinned to the main model at its
        default effort per spec §8.
        """
        extras: dict = {"mode": request.mode}
        if route == "vision":
            # 2026-08-28: this used to say "no effort key: N/A", on the
            # belief that the vision model had no effort knob. It does — the
            # route streams through `llm.stream_chat_events`, which turns
            # effort into the chat template's `enable_thinking` — and it was
            # silently pinned to think, which is why Fast on an image spent
            # its whole budget reasoning. run_vision_engine now runs at
            # request.effort, so meta reports the level that actually served
            # the answer instead of hiding it from the UI and the history.
            extras["model"] = settings.vision_model
            extras["effort"] = request.effort
        elif route == "agent":
            extras["model"] = llm.served_model_id("smart")
            extras["effort"] = request.effort  # applied to synthesis (§3b)
        elif route in ("sql", "rag", "report"):
            extras["model"] = llm.served_model_id("smart")
            extras["effort"] = "think"  # engine default; picker not applied
        else:  # "chat": assistant mode or the salesforce chat class (§3a)
            extras["model"] = llm.served_model_id(request.model)
            extras["effort"] = request.effort
        return extras

    # V9: within-chat memory comes from the messages the frontend sends (robust,
    # survives restarts); the in-process dict is only a fallback for bare API
    # calls. Cross-chat memory: for a signed-in user, look through their OTHER
    # conversations for relevant context and prepend it as a system note.
    from .auth import current_user
    from . import facts, memory_semantic
    from .memory_recall import recall_block

    signed_in = await db.run_in_thread(current_user, http_request)

    conv_key_outer = request.conversation_id or request.session_id

    # The per-conversation stores below (url_documents, repo_chunks) and the
    # live-generation registry are keyed by conversation id ALONE. Without
    # this check, anyone who guessed an id could pull another account's
    # fetched pages and indexed source code into their own prompt. A
    # conversation with no row (a bare API call) has no owner to violate.
    # If the DB is unreachable this raises; the stores it guards are read
    # through the same connection, so they fail too and nothing can leak.
    try:
        conv_owner = await db.run_in_thread(db.conversation_owner, conv_key_outer)
    except Exception:
        conv_owner = None
    viewer = int(signed_in["id"]) if signed_in is not None else None
    if conv_owner is not None and conv_owner != viewer:
        raise HTTPException(status_code=404, detail="conversation not found")

    # A new send for a conversation that is still generating replaces the old
    # generation — the user's newest message wins.
    previous = _live_generations.get(conv_key_outer)
    if previous is not None and not previous.done and previous.task is not None:
        previous.task.cancel()

    gen = LiveGeneration(
        request.conversation_id,
        int(signed_in["id"]) if signed_in is not None else None,
    )
    _live_generations[conv_key_outer] = gen

    # Filled in by the compaction pass; rides out on the final meta so the
    # context meter shows this session's real usage.
    context_state: dict = {}
    # What the auto-orchestration decided, surfaced on the final meta.
    orchestration_state: dict = {}
    # Facts the background extractor saved from THIS message; surfaced on the
    # final meta as `memory_updated` (the ChatGPT-style chip). Extraction runs
    # concurrently with generation, so by meta time it has almost always
    # finished; when it hasn't, the facts still persist — only the chip is
    # skipped for this turn.
    memory_state: dict = {}
    # Salesforce Intelligence Mode extras (assumptions, resolved scope, the
    # final phase) merged into whichever engine's meta ends up being emitted.
    salesforce_state: dict = {}

    async def emit(event: str, data: dict) -> None:
        if event == "meta":
            data = {
                **data,
                **meta_extras(data.get("route")),
                "generation_id": gen.generation_id,
            }
            # If anything had to be removed to fit the window, say so rather
            # than silently answering from a shortened prompt.
            trimmed = context.get_trim_notice()
            if trimmed:
                data["input_trimmed"] = trimmed
            if context_state:
                data["context"] = dict(context_state)
            if orchestration_state:
                data["auto"] = dict(orchestration_state)
            if memory_state.get("facts"):
                data["memory_updated"] = list(memory_state["facts"])
            # Merged rather than overwritten: the engine that answered owns
            # `route`, `data` and `chart`, and the Salesforce planner only ever
            # ADDS provenance and assumptions on top of them.
            for key, value in salesforce_state.items():
                data.setdefault(key, value)
            gen.final_meta = data
        await gen.publish(event, data)

    async def worker() -> None:
        # `text` is rebound when the user answers a clarifying question, so it
        # must be declared here — a `nonlocal` further down would come after
        # the reads above it and fail to compile.
        nonlocal text
        # Per-request state: this task owns its own trim record.
        context.reset_trim_notice()
        try:
            history = request.history_messages or memory.history(request.session_id)
            # Phase 1: decide whether to run web search (never for attachments).
            # AUTO-ORCHESTRATION (2026-07-28): with no Agent toggle in the UI,
            # one cheap non-thinking call decides whether this request deserves
            # agent steps and/or web search. An explicit user choice always
            # wins; effort "low" opts out entirely.
            # Salesforce mode means "answer from MY data". The AGENT is still
            # allowed there — it is how a question reaches a live Salesforce
            # lookup — but automatic WEB SEARCH is not: search is checked
            # before the route chain, so a classifier that fancied the web
            # hijacked the request and the Salesforce router never saw it.
            # Live, "what problems do customers describe in their support
            # cases?" came back with web articles about IT ticketing instead
            # of this org's cases.
            auto_web_search_allowed = request.mode == "assistant"

            auto_plan = None
            if (
                request.text
                and not request.pdf_data
                and not request.image_data
                and not request.agent
            ):
                from .engines.orchestrate import decide

                auto_plan = await decide(request.text, history, request.effort)

            want_agent = request.agent or bool(auto_plan and auto_plan.agent)

            want_search = False
            if (
                settings.search_enabled
                and request.web_search != "off"
                and not request.pdf_data
                and not request.image_data
                and request.text
                # Salesforce mode NEVER searches the web — at any effort
                # level, and even if the client sends web_search="on" (owner
                # request 2026-08-05; until then an explicit "on" was an
                # escape hatch). The composer hides the web-search option in
                # that mode, and this gate makes the promise hold for ANY
                # client, not just the current UI. Turning the Salesforce
                # toggle off is how you ask the web.
                and auto_web_search_allowed
            ):
                from .engines.search import rate_ok, should_search

                user_key = str(signed_in["id"]) if signed_in is not None else "anon"
                if not rate_ok(user_key):
                    await emit(
                        "status",
                        {"text": "Search rate limit reached — answering from model knowledge."},
                    )
                elif request.web_search == "on":
                    want_search = True
                elif auto_plan is not None:
                    # The orchestration call already judged this request.
                    want_search = auto_plan.search
                else:  # "auto"
                    want_search = await should_search(request.text)

            # Deep Research is EXPLICIT-only and needs the web: without a
            # search provider there is nothing to research, so the request
            # degrades to the ordinary engines rather than pretending. It is
            # computed here, with the other web gates, because the pre-passes
            # below all have to know about it — a research question that
            # happens to quote a URL must not be diverted into the
            # single-page reader, and one that says "index" must not be read
            # as a crawl request.
            deep_research_on = bool(
                request.deep_research
                and settings.deep_research_enabled
                and settings.search_enabled
                and request.text
                and not request.pdf_data
                and not request.image_data
                and auto_web_search_allowed
            )
            if request.deep_research and not deep_research_on:
                await emit(
                    "status",
                    {"text": "Deep Research is unavailable here — answering normally."},
                )

            # Announce and record the auto-decision only AFTER the gates
            # above, so the status line and meta.auto describe what will
            # actually run. Before this reorder the label came straight from
            # the classifier's raw wish: Salesforce mode showed "searching
            # the web…" while the gate silently blocked the search, directly
            # contradicting the composer's "no web search" promise (owner
            # report 2026-08-06). Same held for SEARCH_ENABLED=false and the
            # rate limit.
            if auto_plan is not None:
                from .engines.orchestrate import Plan, describe

                effective = Plan(
                    agent=auto_plan.agent,
                    search=bool(auto_plan.search and want_search),
                )
                label = describe(effective)
                if label:
                    await emit("status", {"text": f"{label}…"})
                if effective.agent or effective.search:
                    orchestration_state.update(
                        {"agent": effective.agent, "search": effective.search}
                    )

            if signed_in is not None and request.text:
                user_id = int(signed_in["id"])
                if request.mode == "assistant":
                    # V10 cross-chat memory — normal chat only, by request:
                    # Salesforce mode answers from CRM data, not chat memory.
                    # 1. Kick fact extraction off CONCURRENTLY with the
                    #    answer (it reads only the user's message); the strong
                    #    ref keeps it alive past this request if need be.
                    if settings.fact_extraction_enabled:
                        fact_task = asyncio.create_task(
                            facts.remember_from_message(
                                user_id, request.text, request.conversation_id
                            )
                        )
                        _background_tasks.add(fact_task)

                        def _facts_done(task) -> None:
                            _background_tasks.discard(task)
                            try:
                                saved = task.result()
                            except BaseException:
                                # CancelledError is a BaseException; a bare
                                # Exception clause would let it escape the
                                # event loop's callback handler.
                                return
                            if saved:
                                memory_state["facts"] = [
                                    f["fact"] for f in saved
                                ]

                        fact_task.add_done_callback(_facts_done)
                    # 2. Saved facts, injected verbatim (their memory).
                    saved_facts = await db.run_in_thread(
                        db.list_user_facts, user_id, settings.memory_max_facts
                    )
                    facts_text = facts.facts_block(saved_facts)
                    if facts_text:
                        history = [
                            {"role": "system", "content": facts_text},
                            *history,
                        ]
                    # 3. Semantic + keyword recall over other conversations,
                    #    merged into one block.
                    block = await memory_semantic.cross_chat_block(
                        user_id, request.text, request.conversation_id
                    )
                else:
                    # Salesforce mode keeps the original keyword-only recall.
                    block = await db.run_in_thread(
                        recall_block,
                        user_id,
                        request.text,
                        request.conversation_id,
                    )
                if block:
                    history = [{"role": "system", "content": block}, *history]

            conv_key = request.conversation_id or request.session_id

            # Phase 3: GitHub repo analysis. A repo URL → clone/index/overview;
            # a follow-up when a repo is already indexed → code Q&A.
            github_ref = None
            repo_followup = False
            if settings.repo_analysis_enabled and not deep_research_on and request.text and not request.pdf_data and not request.image_data:
                from .core.repo import detect_github

                from .core.urls import extract_urls as _extract, links_are_the_request

                github_ref = detect_github(request.text)
                # A GitHub link INSIDE a pasted document is a citation, not an
                # instruction to clone and index a repository. Same test as the
                # URL engine below, and the consequence of getting it wrong is
                # larger here: a clone, an index, and an answer about source
                # code nobody asked about.
                if github_ref is not None and not links_are_the_request(
                    request.text, _extract(request.text, limit=settings.url_max_pages)
                ):
                    github_ref = None
                if github_ref is None:
                    try:
                        from . import db as _dbr

                        repo_followup = bool(
                            await db.run_in_thread(_dbr.get_repo_keys, conv_key)
                        )
                    except Exception:
                        repo_followup = False

            # Phase 3.5 (2026-08-30): a whole-SITE crawl. "index/crawl this
            # site <url>" walks every in-scope page into the web store — the
            # repo engine's shape for websites. Detected here so Phase 2 does
            # not swallow the URL as a single pasted page, and so a follow-up
            # question in a conversation that crawled a site can answer from
            # the stored copy.
            crawl_url = None
            crawl_site_hits: list = []
            crawl_site_host = ""
            if (
                settings.web_crawl_enabled
                and not deep_research_on
                and request.text
                and not request.pdf_data
                and not request.image_data
                and github_ref is None
            ):
                from .engines.crawl import (
                    _URL_RE,
                    detect_crawl,
                    detect_resume,
                    site_hits_for,
                )

                crawl_url = detect_crawl(request.text)
                if (
                    crawl_url is None
                    and request.conversation_id
                    and detect_resume(request.text)
                ):
                    # "Continue crawling" names no URL — the capped-crawl
                    # message advertises the phrase, so it must actually
                    # route here: it means the newest crawl in THIS
                    # conversation (review round, 2026-08-30).
                    try:
                        sites = await db.run_in_thread(
                            db.get_conversation_crawl_sites, conv_key
                        )
                        if sites:
                            crawl_url = sites[0]["root_url"]
                    except Exception:
                        crawl_url = None
                if (
                    crawl_url is None
                    and request.conversation_id
                    # Explicit wishes outrank the stored copy: the forced
                    # web pill means LIVE search, a pasted URL means THAT
                    # page, the agent toggle means a plan, and fresh-intent
                    # wording ("latest", "today") should never be answered
                    # from a crawl snapshot (review round, 2026-08-30).
                    # Skipping also skips this pre-pass's embed round trip.
                    and request.web_search != "on"
                    and not request.agent
                    and not _URL_RE.search(request.text)
                ):
                    from .engines.search import _FRESH_RE

                    if not _FRESH_RE.search(request.text):
                        try:
                            # Follow-up: does a crawled site in this
                            # conversation hold relevant material? Cheap (one
                            # embed + a scoped flat scan, ~60 ms) and decisive
                            # — no hits above the relevance floor means normal
                            # routing proceeds.
                            crawl_site_hits, crawl_site_host = await site_hits_for(
                                conv_key, request.text
                            )
                        except Exception:
                            crawl_site_hits = []

            # Phase 2: URL analysis. Pasted links → fetch+read; a follow-up with
            # no new link but pages already read this chat → inject their
            # relevant content (no re-fetch). GitHub URLs are handled by Phase 3
            # instead, so skip them here.
            url_list: list = []
            if (
                settings.url_analysis_enabled
                and not deep_research_on
                and request.text
                and not request.pdf_data
                and not request.image_data
                and github_ref is None
                and crawl_url is None
            ):
                from . import db as _db
                from .core.urls import (
                    extract_urls,
                    links_are_the_request,
                    select_relevant,
                )

                url_list = extract_urls(request.text, limit=settings.url_max_pages)
                # …but only when the links ARE the request. A 30,599-character
                # paste that happens to contain URLs is a document to read, not
                # a list of pages to fetch — and routing it here discarded the
                # paste entirely and answered "I couldn't read any of those
                # links" (owner report, 2026-08-11).
                if url_list and not links_are_the_request(request.text, url_list):
                    logging.getLogger(__name__).info(
                        "ignoring %d incidental URL(s) in a %d-character message",
                        len(url_list),
                        len(request.text),
                    )
                    url_list = []
                if not url_list:
                    try:
                        stored = await db.run_in_thread(
                            _db.get_url_documents, conv_key
                        )
                    except Exception:
                        stored = []  # best-effort — never break chat on a DB hiccup
                    if stored:
                        blocks = [
                            f'[{i}] {d["title"]} ({d["url"]})\n'
                            + select_relevant(d["text"], request.text, 6000)
                            for i, d in enumerate(stored, start=1)
                        ]
                        history = [
                            {
                                "role": "system",
                                "content": "Pages the user shared earlier in this "
                                "chat (reference them if relevant):\n"
                                + "\n\n".join(blocks),
                            },
                            *history,
                        ]

            # 2026-08-07: documents uploaded earlier in this conversation are
            # remembered the same way stored pages are — question-relevant
            # excerpts ride as a pinned system block on EVERY later turn, so
            # "what did that PDF say about X?" works ten turns later, in any
            # mode, whatever engine answers.
            if request.text and not request.pdf_data and not request.image_data:
                from .core.urls import select_relevant as _doc_select

                try:
                    stored_docs = await db.run_in_thread(db.get_documents, conv_key)
                except Exception:
                    stored_docs = []  # best-effort
                if stored_docs:
                    doc_blocks = [
                        f'[{i}] {d["filename"]}'
                        + (f' ({d["total_pages"]} pages)' if d["total_pages"] else "")
                        + "\n" + _doc_select(d["text"], request.text, 8000)
                        for i, d in enumerate(stored_docs, start=1)
                    ]
                    history = [
                        {
                            "role": "system",
                            "content": "Documents the user uploaded earlier in "
                            "this chat — the full files were read and stored; "
                            "these are the sections most relevant to the "
                            "current question (reference them if relevant):\n"
                            + "\n\n".join(doc_blocks),
                        },
                        *history,
                    ]

            # Phase A/B: assemble THIS session's context — rolling summary +
            # retrieved folded chunks + recent turns — compacting first if the
            # request would otherwise overflow the window. Scoped entirely to
            # conv_key, so no other session's content can enter the prompt.
            # Phase 4: does this conversation have datasets to answer from?
            dataset_ready = False
            if (
                settings.dataset_uploads_enabled
                and request.text
                and not request.pdf_data
                and not request.image_data
                and github_ref is None
                and not url_list
            ):
                try:
                    dataset_ready = bool(
                        await db.run_in_thread(db.get_uploads, conv_key)
                    )
                except Exception:
                    dataset_ready = False

            # The FULL transcript stays the reference for compaction: fold
            # boundaries count turns in the whole thread, so measuring against
            # the already-compacted prompt would mis-count them.
            full_history = list(history)
            if signed_in is not None and request.conversation_id:
                from . import compaction, recall

                base_url, _key, model_id = llm.resolve_model_choice(request.model)
                retrieved = await recall.retrieve_block(conv_key, text)
                history, info = await compaction.prepare(
                    conv_key,
                    full_history,
                    text,
                    base_url=base_url,
                    model=model_id,
                    emit=emit,
                    retrieved=retrieved,
                )
                context_state.update(info)

            # SALESFORCE INTELLIGENCE MODE (2026-08-11).
            #
            # The Salesforce pill is no longer a retrieval filter. Before any
            # engine runs, the request is resolved against this conversation
            # ("what about EMEA?" keeps the previous object, period and owner
            # scope), and ONE targeted question is asked only when a missing
            # detail would materially change the answer. A question that IS
            # asked pauses the intent; answering it resumes the SAME request
            # rather than starting a new one.
            #
            # It is skipped for attachments (a document has its own engine), for
            # the explicit Live toggle (already a scoped instruction), for the
            # agent/URL/repo/dataset routes (each owns its own pipeline), and
            # for text that already carries a legacy "(Clarified:" resolution.
            #
            # A CLARIFICATION ANSWER OWNS THE TURN. When `clarification` is
            # present the user is finishing a request this server started, so
            # the escalation gates below do not apply to it — auto-orchestration
            # deciding "this deserves agent steps" must not swallow the answer
            # and turn it back into a standalone question. Found on a live run
            # (2026-08-11): the second half of a resumed request was routed to
            # the agent engine and the resume was silently lost.
            sf_outcome = None
            # ONE clarification implementation, two planners. Intelligence Mode
            # on → the model plans and may ask; off → the deterministic
            # detectors in core/clarify.py ask, through the SAME persisted,
            # resumable, loop-guarded card. The previous arrangement ran a
            # second implementation here whose card could not be resumed, did
            # not survive a reload, and re-asked its own question forever.
            clarification_available = (
                settings.salesforce_intelligence_enabled
                or settings.clarify_before_answering
            )
            answering_clarification = bool(
                request.clarification
                and request.mode == "salesforce"
                and clarification_available
            )
            # NOTE ON `want_agent`: it is deliberately NOT a gate here.
            # Resolving a request against the conversation, and asking about a
            # genuinely ambiguous detail, are ROUTING decisions; running the
            # request as multi-step agent work is an EXECUTION STRATEGY. When
            # the auto-orchestration classifier gated this block, a long
            # analytical Salesforce question ("training details for slot 128 …
            # how many cleared and failed and what is the ratio") skipped the
            # planner entirely and was answered by the agent — with neither the
            # clarification gate nor the deterministic figures. Which
            # clarification card a user saw then depended on an unrelated
            # classifier. Owner report, 2026-08-11.
            #
            # The engine still hands the turn back (`handled=False`) whenever it
            # is not the right answerer, and the agent then runs BELOW with the
            # RESOLVED request rather than the ambiguous one.
            if answering_clarification or (
                request.mode == "salesforce"
                and clarification_available
                and request.text
                and not (request.pdf_data or request.image_data)
                and not request.sf_live
                and github_ref is None
                and not repo_followup
                and not url_list
                and not dataset_ready
                and "(Clarified:" not in text
            ):
                from .core.sf_intel.models import ClarificationResponse
                from .engines import sf_intel

                answer_to_pending = None
                malformed = False
                if request.clarification:
                    try:
                        answer_to_pending = ClarificationResponse.model_validate(
                            request.clarification
                        )
                    except Exception as exc:  # noqa: BLE001
                        # A malformed response is the client's bug, not the
                        # user's. Saying so is better than silently re-reading
                        # their click as a brand-new question, which is what
                        # happened before: the engine fell through to the topic
                        # classifier, an option label on its own read as a
                        # change of subject, and the pending question — and the
                        # request behind it — were cancelled.
                        logging.getLogger(__name__).info(
                            "rejecting a malformed clarification response: %s",
                            str(exc)[:200],
                        )
                        malformed = True

                if malformed:
                    answer = (
                        "I could not read that answer, so nothing has changed "
                        "and your question is still open — pick an option "
                        "again, or just tell me what you meant."
                    )
                    await emit("token", {"text": answer})
                    await emit(
                        "meta", {"route": "clarify", "salesforce_mode": "intelligence"}
                    )
                    sf_outcome = sf_intel.Outcome(handled=True, answer=answer)
                else:
                    sf_outcome = await sf_intel.run(
                        text,
                        history,
                        emit,
                        conversation_id=conv_key,
                        effort=request.effort,
                        model_choice=request.model,
                        clarification_response=answer_to_pending,
                        source_enabled=True,
                        use_planner=settings.salesforce_intelligence_enabled,
                    )
                    if sf_outcome.meta_extras:
                        salesforce_state.update(sf_outcome.meta_extras)
                    if not sf_outcome.handled and sf_outcome.resolved_text:
                        # Resumed or context-resolved: the engines below must
                        # see the RESOLVED request, never the ambiguous one.
                        text = sf_outcome.resolved_text
            else:
                # NOTHING in this turn can answer or re-ask a question this
                # conversation is waiting on: the source was switched off, or
                # the turn belongs to the document, vision, repo, URL or
                # dataset pipeline, or it is an explicit live lookup. A
                # question left open here is not merely stale — the partial
                # unique index allows one pending clarification per
                # conversation, so it silently blocks every future question
                # until something cancels it.
                from .core.sf_intel import state as sf_intel_state

                with contextlib.suppress(Exception):
                    await sf_intel_state.cancel_pending(conv_key)

            if sf_outcome is not None and sf_outcome.handled:
                # Salesforce Intelligence Mode answered, or asked a question and
                # is now waiting. Either way it already emitted its tokens and
                # its single meta; there is nothing left for the chain below.
                answer = sf_outcome.answer
            elif request.pdf_data:
                # V8 → 2026-08-07: any document (PDF/DOCX/plain) — the WHOLE
                # file is read and remembered for this conversation.
                from .engines.document import run_pdf_engine

                answer = await run_pdf_engine(
                    text,
                    request.pdf_data,
                    request.pdf_filename,
                    history,
                    emit,
                    conversation_id=conv_key,
                    # Same contract as the image route: the document engine
                    # runs at the level the composer picked, so the effort
                    # meta_extras reports for route="vision" is the truth for
                    # documents too (2026-08-29).
                    effort=request.effort,
                )
            elif request.image_data:
                # An attached image ALWAYS goes to the vision engine — text-only
                # engines (chat/agent/sql/rag) cannot see it, and silently
                # answering "I can't view images" is worse than routing here.
                from .engines.vision import run_vision_engine

                answer = await run_vision_engine(
                    text,
                    request.images_data,
                    history,
                    emit,
                    # Honour the composer's Fast/Think/Max (already
                    # normalized to fast|think|max by ChatRequest). Before
                    # 2026-08-28 the engine ignored it and always thought,
                    # so "Fast" on an image was the slowest path in the app.
                    effort=request.effort,
                )
            elif github_ref is not None or repo_followup:
                # Phase 3: a GitHub repo URL → clone/index/overview; or a
                # follow-up question about a repo already indexed → code Q&A.
                from .engines.repo import run_repo_engine

                answer = await run_repo_engine(
                    text, github_ref, conv_key, history, emit
                )
            elif crawl_url is not None:
                # Phase 3.5: crawl the whole site into the web store.
                from .engines.crawl import run_crawl_engine

                answer = await run_crawl_engine(
                    text, crawl_url, conv_key, history, emit
                )
            elif crawl_site_hits:
                # Follow-up about a crawled site → answer from the stored
                # copy, cited and dated. Only reached when the scoped
                # retrieval found chunks above the relevance floor, so an
                # unrelated question in the same conversation routes normally.
                from .engines.crawl import run_site_qa_engine

                answer = await run_site_qa_engine(
                    text,
                    crawl_site_hits,
                    crawl_site_host,
                    history,
                    emit,
                    effort=request.effort,
                )
            elif url_list:
                # Phase 2: the user pasted link(s) → read them and answer.
                from .engines.url import run_url_engine

                answer = await run_url_engine(
                    text, url_list, conv_key, history, emit
                )
            elif deep_research_on:
                # Deep Research: the iterative research loop. It sits ABOVE
                # the agent engine because orchestrate.decide() classifies
                # exactly this multi-part phrasing as agent=true (and at
                # effort "max" with search it FORCES it), so any lower and
                # the pill would be silently eaten by the planner.
                from .engines.deep_research import run_deep_research_engine

                answer = await run_deep_research_engine(
                    text,
                    history,
                    emit,
                    effort=request.effort,
                    conversation_id=conv_key,
                    user_id=int(signed_in["id"]) if signed_in is not None else None,
                )
            elif want_agent and (request.agent or not dataset_ready):
                # V2 §3b: agent (deep-task) engine. Checked BEFORE plain search
                # because the agent can search inside its own plan ("web"
                # steps): a request that needs both planning and the web gets
                # both, instead of losing the plan to a one-shot search.
                #
                # A conversation with uploaded datasets keeps its turns unless
                # the user forced the agent: the auto classifier judges only
                # the phrasing ("read this file and give me insights" sounds
                # like a task), never sees that a dataset exists, and the
                # agent cannot read datasets — so letting its wish outrank
                # dataset_ready answered dataset questions with "no file came
                # through" (found 2026-08-21).
                #
                # The Salesforce toggle gates Salesforce access — off → the
                # agent works only from the conversation context (shared
                # URLs/docs) + general knowledge. `web` carries the same gate
                # for the internet.
                from .engines.agent import run_agent_engine

                answer = await run_agent_engine(
                    text,
                    history,
                    emit,
                    effort=request.effort,
                    salesforce=(request.mode != "assistant"),
                    web=want_search,
                    # The pill and the auto classifier collapse into one
                    # boolean by here, but they are different promises: auto
                    # lets the planner judge, FORCED means at least one step
                    # actually searches. Without this the agent answered a
                    # current-events question from training memory under a
                    # trust line saying searches go to the internet.
                    web_forced=(request.web_search == "on"),
                    user_id=int(signed_in["id"]) if signed_in is not None else None,
                    conversation_id=conv_key,
                )
            elif want_search and (request.web_search == "on" or not dataset_ready):
                # Phase 1: web search — cited answer from fetched sources.
                # Auto-decided search yields to uploaded datasets for the same
                # reason as the agent above; an explicit "on" still wins.
                from .engines.search import run_search_engine

                answer = await run_search_engine(
                    text,
                    history,
                    emit,
                    request.effort,
                    user_id=int(signed_in["id"]) if signed_in is not None else None,
                    conversation_id=conv_key,
                )
            elif dataset_ready:
                # Phase 4: this conversation has uploaded datasets — answer
                # from their stored PROFILES (never the files themselves).
                from .engines.dataset import run_dataset_engine

                answer = await run_dataset_engine(
                    text,
                    conv_key,
                    history,
                    emit,
                    model_choice=request.model,
                    effort=request.effort,
                )
            elif request.mode == "assistant":
                # V2 §3a: SKIP the router and data engines entirely.
                from .engines.chat import run_chat_engine

                answer = await run_chat_engine(
                    text,
                    history,
                    emit,
                    mode="assistant",
                    model_choice=request.model,
                    effort=request.effort,
                )
            elif request.sf_live:
                # "Live Salesforce" toggle: skip the router — every text
                # answer queries the org directly (schema questions included;
                # the engine's live branch handles both).
                from .engines.sql import run_sql_engine

                answer = await run_sql_engine(
                    text, history, emit, force_live=True
                )
            else:
                state = await get_graph().ainvoke(
                    {
                        "message": text,
                        "session_id": request.session_id,
                        "image_base64": request.image_data,
                        "history": history,
                        "emit": emit,
                        "model_choice": request.model,
                        "effort": request.effort,
                    }
                )
                answer = state.get("answer") or ""
            gen.answer = answer
            memory.add_exchange(request.session_id, text, answer)
            await gen.publish("done", {"session_id": request.session_id})

            # Background compaction: fold early so the next turn almost never
            # needs a synchronous one. Deliberately NOT awaited — the SSE
            # stream stays open until this worker returns, so awaiting it here
            # would make the user wait for the very thing that is supposed to
            # be invisible.
            if signed_in is not None and request.conversation_id:
                from . import compaction

                base_url, _key, model_id = llm.resolve_model_choice(request.model)
                _spawn_background_compaction(
                    conv_key,
                    [
                        *full_history,
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": answer},
                    ],
                    base_url=base_url,
                    model=model_id,
                )
        except asyncio.CancelledError:
            gen.cancelled = True  # /chat/stop or replaced by a newer send
        except Exception as exc:  # terminal error event (§10)
            gen.failed = True
            await gen.publish("error", {"message": str(exc)})
        finally:
            # shield: finishing the bookkeeping must survive the cancellation
            # that may still be propagating through this task.
            with contextlib.suppress(Exception):
                await asyncio.shield(_finalize_generation(conv_key_outer, gen))

    gen.task = asyncio.create_task(worker())

    return StreamingResponse(
        gen.follow(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/chat/salesforce/{conversation_id}")
async def salesforce_context(
    conversation_id: str, http_request: Request
) -> dict:
    """Restore Salesforce Intelligence state for a conversation.

    This is what makes a clarification card survive a reload: the browser asks
    for the pending question on mount and rebuilds the card from the server's
    copy, rather than from whatever the tab happened to have in memory. It also
    supplies the starter card's options, filtered to what this connection can
    actually reach.

    Scoped to the conversation's owner, like /chat itself — a conversation whose
    id someone guessed must not disclose what it is asking about.
    """
    viewer = await _viewer_id(http_request)
    try:
        owner = await db.run_in_thread(db.conversation_owner, conversation_id)
    except Exception:
        owner = None
    if owner is not None and owner != viewer:
        raise HTTPException(status_code=404, detail="conversation not found")

    from .engines import sf_intel

    if not settings.salesforce_intelligence_enabled:
        return {"enabled": False, "options": [], "pending_clarification": None}
    try:
        return await sf_intel.starter_options(conversation_id)
    except Exception as exc:  # noqa: BLE001 — a starter card is never fatal
        logging.getLogger(__name__).info(
            "salesforce context unavailable for %s: %s", conversation_id, exc
        )
        return {"enabled": True, "options": [], "pending_clarification": None}


class SalesforceCancelRequest(BaseModel):
    conversation_id: str


@app.post("/chat/salesforce/cancel")
async def salesforce_cancel(
    body: SalesforceCancelRequest, http_request: Request
) -> dict:
    """Cancel a pending clarification.

    Called when the Salesforce source is switched off with a question on screen.
    Deterministic by design: the card disappears because the server says it is
    cancelled, not because the client stopped drawing it — otherwise the next
    Salesforce turn in that chat would resume a question the user had visibly
    dismissed.
    """
    viewer = await _viewer_id(http_request)
    try:
        owner = await db.run_in_thread(db.conversation_owner, body.conversation_id)
    except Exception:
        owner = None
    if owner is not None and owner != viewer:
        raise HTTPException(status_code=404, detail="conversation not found")

    from .core.sf_intel import state as sf_intel_state

    cancelled = await sf_intel_state.cancel_pending(body.conversation_id)
    return {"cancelled": cancelled}


class StopRequest(BaseModel):
    conversation_id: Optional[str] = None
    session_id: str = "default"


async def _viewer_id(http_request: Request) -> Optional[int]:
    """The signed-in user's id, or None for an unauthenticated caller.

    Async because resolving the local account is a database round trip
    (auth.local_user -> db.get_user_by_id); running it inline would block the
    event loop on the SSE attach/stop hot paths.
    """
    from .auth import current_user

    user = await db.run_in_thread(current_user, http_request)
    return int(user["id"]) if user is not None else None


def _owns(gen: "LiveGeneration", viewer: Optional[int]) -> bool:
    """A generation is only visible to the identity that started it.

    Without this, /chat/attach would stream ANOTHER user's answer to anyone
    who guessed a conversation id, and /chat/stop would let them cancel it.
    Anonymous generations (direct API calls, no cookie) belong to the
    anonymous caller — a signed-in user cannot reach them either.
    """
    return gen.user_id == viewer


@app.post("/chat/stop")
async def chat_stop(body: StopRequest, http_request: Request) -> dict:
    """Cancel a running generation. Closing the SSE stream no longer stops
    the model (generations are detached), so the Stop button calls this."""
    gen = _live_generations.get(body.conversation_id or body.session_id)
    if gen is None or gen.done or gen.task is None:
        return {"stopped": False}
    if not _owns(gen, await _viewer_id(http_request)):
        return {"stopped": False}  # not yours — indistinguishable from absent
    gen.task.cancel()
    return {"stopped": True}


@app.get("/chat/active")
async def chat_active(http_request: Request) -> dict:
    """Conversation keys with a generation still running — the sidebar polls
    this to show a ChatGPT-style spinner next to busy chats. Scoped to the
    caller: another account's conversation ids are never disclosed."""
    viewer = await _viewer_id(http_request)
    return {
        "active": [
            k
            for k, g in _live_generations.items()
            if not g.done and _owns(g, viewer)
        ]
    }


class CompactRequest(BaseModel):
    conversation_id: str
    messages: Optional[List[ChatMessage]] = None


@app.post("/chat/compact")
async def chat_compact(body: CompactRequest, http_request: Request) -> dict:
    """Compact a conversation on demand ("Compact now" in the meter popover).

    Folds everything except the most recent turns, regardless of how full the
    window currently is.
    """
    from . import compaction
    from .auth import current_user

    user = await db.run_in_thread(current_user, http_request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    owner = await db.run_in_thread(db.conversation_owner, body.conversation_id)
    if owner is None or owner != int(user["id"]):
        raise HTTPException(status_code=404, detail="conversation not found")

    history = [
        {"role": m.role, "content": m.content}
        for m in (body.messages or [])
        if m.content and m.content.strip()
    ]
    if not history:
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in await db.run_in_thread(db.list_messages, body.conversation_id)
            # Same rule as the `body.messages` branch above and as
            # `ChatRequest.history_messages`: a blank turn has nothing to
            # summarize, and `covers_through` counts blank-filtered turns.
            if m["content"] and m["content"].strip()
        ]
    result = await compaction.compact(body.conversation_id, history, force=True)
    # What is STILL foldable afterwards, from the same helper the summary
    # endpoint serves — so the popover can update without a refetch and can
    # never disagree with what this button would do on the next press.
    counts = await db.run_in_thread(foldable_counts, body.conversation_id, history)
    if result is None:
        return {
            "compacted": False,
            "reason": "nothing older to summarize",
            **counts,
        }
    return {
        "compacted": True,
        "folded_turns": result["folded"],
        "covers_through": result["covers_through"],
        **counts,
    }


@app.get("/chat/attach/{conversation_id}")
async def chat_attach(
    conversation_id: str, http_request: Request
) -> StreamingResponse:
    """Re-join a running generation after a reload: replays every buffered
    event (so the partial answer rebuilds instantly) and then streams live.
    404 once it has finished — the answer is in history at that point."""
    gen = _live_generations.get(conversation_id)
    if gen is None or gen.done or not _owns(gen, await _viewer_id(http_request)):
        raise HTTPException(status_code=404, detail="no active generation")
    return StreamingResponse(
        gen.follow(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
