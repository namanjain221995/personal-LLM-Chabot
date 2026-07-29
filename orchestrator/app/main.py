"""FastAPI entrypoint (spec §8/§10 + V2-DESIGN §1-§3): POST /chat (SSE),
GET /reports, GET /reports/{filename}, GET /health, /auth/*, /history/*."""
from __future__ import annotations

import asyncio
import contextlib
import mimetypes
import uuid
from typing import AsyncIterator, List, Literal, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, model_validator

from . import context, db, llm
from .auth import router as auth_router
from .config import settings
from .core.report_paths import ReportPathError, list_reports, resolve_report_file
from .graph import get_graph
from .health import check_dependencies
from .history import router as history_router
from .uploads import router as uploads_router
from .memory import memory
from .sse import sse_event

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    """Apply the database schema/migrations BEFORE serving any request.

    connect() migrates lazily, so a broken migration used to surface on the
    first user request that happened to touch app.sqlite3 — long after the
    deploy looked healthy. Doing it here makes a bad migration fail startup,
    which is what a container healthcheck can actually catch.
    """
    with contextlib.closing(db.connect()):
        pass
    yield


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
        """Replay buffered events, then stream live ones until the end."""
        self.subscribers += 1
        try:
            index = 0
            while True:
                async with self.cond:
                    while index >= len(self.events) and not self.done:
                        await self.cond.wait()
                    if index >= len(self.events):
                        break  # done and fully drained
                    event, data = self.events[index]
                    index += 1
                yield sse_event(event, data)
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
        with contextlib.suppress(Exception):
            db.add_message(
                gen.user_id,
                gen.conversation_id,
                "assistant",
                gen.answer,
                gen.final_meta,
            )


class ChatMessage(BaseModel):
    role: str
    content: str = ""


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
    # --- V2 optional fields (defaults preserve v1 behavior) ---
    conversation_id: Optional[str] = None
    mode: Literal["salesforce", "assistant"] = "salesforce"
    model: Literal["smart", "fast"] = "smart"
    effort: Literal["fast", "low", "medium", "high"] = "medium"
    agent: bool = False
    # V8: an uploaded PDF (base64, optionally a data: URL) + its filename.
    pdf: Optional[str] = None
    pdf_filename: Optional[str] = None
    # Phase 1: web search — "off" (never), "on" (force), "auto" (model decides).
    web_search: Literal["off", "auto", "on"] = "off"

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
    def image_data(self) -> Optional[str]:
        return self.image_base64 or self.image

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
    def _require_input(self) -> "ChatRequest":
        if not self.text and not self.image_data and not self.pdf_data:
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
    text = request.text or "Analyze the attached image."

    def meta_extras(route: Optional[str]) -> dict:
        """V2 §2: meta gains mode / model (served model id) / effort — merged
        in centrally so the engines keep emitting their v1 shapes untouched.

        meta is trust metadata, so model/effort must describe what actually
        served the answer, keyed on the engine's own `route`. The picker and
        effort apply only where the design routes them: the chat engine (§3a)
        and agent synthesis (§3b, smart-pinned). The data engines stay pinned
        to the main model at its default effort per spec §8, and the vision
        route is served by the vision model, which has no effort knob.
        """
        extras: dict = {"mode": request.mode}
        if route == "vision":
            extras["model"] = settings.vision_model  # no effort key: N/A
        elif route == "agent":
            extras["model"] = llm.served_model_id("smart")
            extras["effort"] = request.effort  # applied to synthesis (§3b)
        elif route in ("sql", "rag", "report"):
            extras["model"] = llm.served_model_id("smart")
            extras["effort"] = "medium"  # gpt-oss default; picker not applied
        else:  # "chat": assistant mode or the salesforce chat class (§3a)
            extras["model"] = llm.served_model_id(request.model)
            extras["effort"] = request.effort
        return extras

    # V9: within-chat memory comes from the messages the frontend sends (robust,
    # survives restarts); the in-process dict is only a fallback for bare API
    # calls. Cross-chat memory: for a signed-in user, look through their OTHER
    # conversations for relevant context and prepend it as a system note.
    from .auth import current_user
    from .memory_recall import recall_block

    signed_in = current_user(http_request)

    conv_key_outer = request.conversation_id or request.session_id

    # The per-conversation stores below (url_documents, repo_chunks) and the
    # live-generation registry are keyed by conversation id ALONE. Without
    # this check, anyone who guessed an id could pull another account's
    # fetched pages and indexed source code into their own prompt. A
    # conversation with no row (a bare API call) has no owner to violate.
    # If the DB is unreachable this raises; the stores it guards are read
    # through the same connection, so they fail too and nothing can leak.
    try:
        conv_owner = db.conversation_owner(conv_key_outer)
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
            gen.final_meta = data
        await gen.publish(event, data)

    async def worker() -> None:
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
                from .engines.orchestrate import decide, describe

                auto_plan = await decide(request.text, history, request.effort)
                label = describe(auto_plan)
                if label:
                    await emit("status", {"text": f"{label}…"})

            want_agent = request.agent or bool(auto_plan and auto_plan.agent)
            if auto_plan is not None and (auto_plan.agent or auto_plan.search):
                orchestration_state.update(
                    {"agent": auto_plan.agent, "search": auto_plan.search}
                )

            want_search = False
            if (
                settings.search_enabled
                and request.web_search != "off"
                and not request.pdf_data
                and not request.image_data
                and request.text
                # In Salesforce mode only an EXPLICIT "on" searches the web;
                # auto-detection would quietly answer CRM questions from the
                # internet. Turning the toggle off is how you ask the web.
                and (auto_web_search_allowed or request.web_search == "on")
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
            if signed_in is not None and request.text:
                block = recall_block(
                    int(signed_in["id"]), request.text, request.conversation_id
                )
                if block:
                    history = [{"role": "system", "content": block}, *history]

            conv_key = request.conversation_id or request.session_id

            # Phase 3: GitHub repo analysis. A repo URL → clone/index/overview;
            # a follow-up when a repo is already indexed → code Q&A.
            github_ref = None
            repo_followup = False
            if settings.repo_analysis_enabled and request.text and not request.pdf_data and not request.image_data:
                from .core.repo import detect_github

                github_ref = detect_github(request.text)
                if github_ref is None:
                    try:
                        from . import db as _dbr

                        repo_followup = bool(_dbr.get_repo_keys(conv_key))
                    except Exception:
                        repo_followup = False

            # Phase 2: URL analysis. Pasted links → fetch+read; a follow-up with
            # no new link but pages already read this chat → inject their
            # relevant content (no re-fetch). GitHub URLs are handled by Phase 3
            # instead, so skip them here.
            url_list: list = []
            if (
                settings.url_analysis_enabled
                and request.text
                and not request.pdf_data
                and not request.image_data
                and github_ref is None
            ):
                from . import db as _db
                from .core.urls import extract_urls, select_relevant

                url_list = extract_urls(request.text, limit=settings.url_max_pages)
                if not url_list:
                    try:
                        stored = _db.get_url_documents(conv_key)
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
                    dataset_ready = bool(db.get_uploads(conv_key))
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

            if request.pdf_data:
                # V8: a PDF → render pages to images + text → multimodal model.
                from .engines.document import run_pdf_engine

                answer = await run_pdf_engine(
                    text, request.pdf_data, request.pdf_filename, history, emit
                )
            elif request.image_data:
                # An attached image ALWAYS goes to the vision engine — text-only
                # engines (chat/agent/sql/rag) cannot see it, and silently
                # answering "I can't view images" is worse than routing here.
                from .engines.vision import run_vision_engine

                answer = await run_vision_engine(
                    text, request.image_data, history, emit
                )
            elif github_ref is not None or repo_followup:
                # Phase 3: a GitHub repo URL → clone/index/overview; or a
                # follow-up question about a repo already indexed → code Q&A.
                from .engines.repo import run_repo_engine

                answer = await run_repo_engine(
                    text, github_ref, conv_key, history, emit
                )
            elif url_list:
                # Phase 2: the user pasted link(s) → read them and answer.
                from .engines.url import run_url_engine

                answer = await run_url_engine(
                    text, url_list, conv_key, history, emit
                )
            elif want_agent:
                # V2 §3b: agent (deep-task) engine. Checked BEFORE plain search
                # because the agent can search inside its own plan ("web"
                # steps): a request that needs both planning and the web gets
                # both, instead of losing the plan to a one-shot search.
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
                )
            elif want_search:
                # Phase 1: web search — cited answer from fetched sources.
                from .engines.search import run_search_engine

                answer = await run_search_engine(text, history, emit, request.effort)
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


class StopRequest(BaseModel):
    conversation_id: Optional[str] = None
    session_id: str = "default"


def _viewer_id(http_request: Request) -> Optional[int]:
    """The signed-in user's id, or None for an unauthenticated caller."""
    from .auth import current_user

    user = current_user(http_request)
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
    if not _owns(gen, _viewer_id(http_request)):
        return {"stopped": False}  # not yours — indistinguishable from absent
    gen.task.cancel()
    return {"stopped": True}


@app.get("/chat/active")
async def chat_active(http_request: Request) -> dict:
    """Conversation keys with a generation still running — the sidebar polls
    this to show a ChatGPT-style spinner next to busy chats. Scoped to the
    caller: another account's conversation ids are never disclosed."""
    viewer = _viewer_id(http_request)
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

    user = current_user(http_request)
    if user is None:
        raise HTTPException(status_code=401, detail="sign in required")
    owner = db.conversation_owner(body.conversation_id)
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
            for m in db.list_messages(body.conversation_id)
        ]
    result = await compaction.compact(body.conversation_id, history, force=True)
    if result is None:
        return {"compacted": False, "reason": "nothing older to summarize"}
    return {
        "compacted": True,
        "folded_turns": result["folded"],
        "covers_through": result["covers_through"],
    }


@app.get("/chat/attach/{conversation_id}")
async def chat_attach(
    conversation_id: str, http_request: Request
) -> StreamingResponse:
    """Re-join a running generation after a reload: replays every buffered
    event (so the partial answer rebuilds instantly) and then streams live.
    404 once it has finished — the answer is in history at that point."""
    gen = _live_generations.get(conversation_id)
    if gen is None or gen.done or not _owns(gen, _viewer_id(http_request)):
        raise HTTPException(status_code=404, detail="no active generation")
    return StreamingResponse(
        gen.follow(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
