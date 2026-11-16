"""Tool-calling answer agent.

The agent answers one evaluation question using ONLY the MCP-style tools exposed by
:class:`okeval.tools.ToolRegistry`. The loop is the classic OpenAI tool-calling loop:

1. ``system`` = ``prompts.AGENT_SYSTEM``, ``user`` = the question.
2. Call the model with ``registry.openai_tools()``.
3. Execute every tool call through ``registry.call`` (which never raises), append the
   assistant turn and one ``{"role": "tool", ...}`` message per call.
4. Stop when the model replies with content and no tool calls, or when
   ``cfg.max_tool_steps`` model calls have been made; in that case one final call is forced
   with ``tools=None`` so the model must answer from what it has.

Every tool call is recorded (name, arguments, ok, chunk_ids, latency) so grading can check
tool usage and the report can compute knowledge coverage. Any exception while answering a
question is captured in :attr:`AgentResult.error`, so one bad question never kills a batch.

Each tool call and each loop rule that costs one more model call (the tools-first nudge, the forced answer) also
adds an entry to the timeline of the answer being made, when one is open (:mod:`sf_assistant.agent.timeline`;
sizes, names and flags only).

Spelling help (``SFA_RESOLVER=on``, docs/resolver_integration.md (1), (6)): :class:`QuestionSpelling` holds one
question's misspelt names and the knowledge agent's spelling block; :func:`sf_assistant.pipeline.question_spelling`
makes it and writes the answer's "Read as" line (the wiring layer: this module never reads record-session objects). A
tool call whose result lists the corrections of its arguments (``result["read_as"]``, a list) keeps them in its trace
record. Off, none of this runs.
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Iterable

from sf_assistant import prompts as _prompt_files   # the texts: prompts/<id>.md (REQUIRE_TOOLS / FORCE_ANSWER)
from sf_assistant.config import LIMITS
from sf_assistant.tools.knowledge_tools import spelling_section

from . import knowledge_prompts as prompts
from . import timeline

if TYPE_CHECKING:  # imported for type hints only; avoids import cycles at runtime
    from sf_assistant.agent.llm import LLM
    from sf_assistant.agent.question import Question
    from sf_assistant.config import Config
    from sf_assistant.tools.knowledge_tools import ToolRegistry

# Tool results larger than this are truncated before being sent back to the model, so one
# very broad tool call (e.g. list_fields on a wide object) cannot blow the context window.
MAX_TOOL_RESULT_CHARS = LIMITS.knowledge_tool_result_chars   # 16_000

# Message appended when the tool budget is exhausted and a final answer is forced.
# Message sent once when ``require_tools`` is set and the model answered without any tool call.
REQUIRE_TOOLS_MESSAGE = _prompt_files.text("knowledge_require_tools_message")

FORCE_ANSWER_MESSAGE = _prompt_files.text("knowledge_force_answer_message")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


@dataclass
class AgentResult:
    """Outcome of answering one question (see SPEC ``agent.py``)."""

    qid: str
    question: str
    answer: str
    tool_calls: list[dict] = field(default_factory=list)  # [{"name","arguments","ok","chunk_ids","latency_s"}]
    steps: int = 0                                         # number of LLM calls made
    usage: dict = field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0})
    latency_s: float = 0.0
    error: str | None = None

    def to_dict(self) -> dict:
        """Plain-dict form (JSON-serialisable) for export."""
        return asdict(self)


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------

def _strip_thinking(text: str | None) -> str:
    """Remove ``<think>…</think>`` blocks (Qwen reasoning) and surrounding whitespace."""
    if not text:
        return ""
    text = _THINK_RE.sub("", text)
    # A dangling "</think>" (opening tag consumed by the chat template) leaves reasoning
    # before it; keep only what follows.
    if "</think>" in text:
        text = text.split("</think>", 1)[1]
    return text.strip()


def _agent_system() -> str:
    """Current ``AGENT_SYSTEM`` text, honouring runtime edits to ``prompts.TEMPLATES``."""
    templates = getattr(prompts, "TEMPLATES", None)
    if isinstance(templates, dict) and "AGENT_SYSTEM" in templates:
        return templates["AGENT_SYSTEM"]
    return prompts.AGENT_SYSTEM


def _add_usage(total: dict, usage: dict | None) -> None:
    """Accumulate token usage, tolerating missing / ``None`` values."""
    if not usage:
        return
    for key in ("prompt_tokens", "completion_tokens"):
        try:
            total[key] = int(total.get(key, 0) or 0) + int(usage.get(key) or 0)
        except (TypeError, ValueError):
            pass


def _to_json(obj: Any) -> str:
    """Serialise a tool result for the ``tool`` message, truncating very large payloads."""
    try:
        text = json.dumps(obj, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = json.dumps({"ok": False, "error": "tool result was not JSON-serialisable", "repr": repr(obj)[:2000]})
    if len(text) > MAX_TOOL_RESULT_CHARS:
        text = text[:MAX_TOOL_RESULT_CHARS] + f'… [truncated {len(text) - MAX_TOOL_RESULT_CHARS} chars]'
    return text


def _validate_arguments(name: str, arguments: Any, registry: "ToolRegistry") -> str | None:
    """Return an error message when the model's tool call cannot be executed, else ``None``.

    Covers: unknown tool names, unparseable JSON (``{"_raw": ...}`` from ``LLM.chat``) and
    non-object arguments. Schema-level problems (missing / unknown keys) are left to
    ``registry.call``, which reports them as ``ok=False`` without raising.
    """
    if name not in registry.tools:
        known = ", ".join(sorted(registry.tools))
        return f"unknown tool '{name}'. Available tools: {known}"
    if not isinstance(arguments, dict):
        return f"arguments for '{name}' must be a JSON object, got {type(arguments).__name__}"
    if set(arguments) == {"_raw"}:
        raw = str(arguments["_raw"])[:300]
        return f"arguments for '{name}' were not valid JSON ({raw!r}); resend the call with a JSON object"
    return None


def _execute_tool(name: str, arguments: Any, registry: "ToolRegistry") -> tuple[dict, dict]:
    """Run one tool call. Returns ``(payload_for_model, trace_record)``; never raises."""
    t0 = time.perf_counter()
    problem = _validate_arguments(name, arguments, registry)
    if problem is not None:
        payload: dict = {"ok": False, "result": None, "error": problem, "chunk_ids": []}
    else:
        try:
            payload = registry.call(name, arguments)
        except Exception as exc:  # registry.call should never raise; be defensive anyway
            payload = {"ok": False, "result": None, "error": f"{type(exc).__name__}: {exc}", "chunk_ids": []}
        if not isinstance(payload, dict):
            payload = {"ok": True, "result": payload, "error": None, "chunk_ids": []}
    latency = time.perf_counter() - t0
    record = {
        "name": name,
        "arguments": arguments if isinstance(arguments, dict) else {"_raw": repr(arguments)},
        "ok": bool(payload.get("ok", False)),
        "chunk_ids": list(payload.get("chunk_ids") or []),
        "latency_s": round(latency, 4),
    }
    if not record["ok"] and payload.get("error"):
        record["error"] = str(payload["error"])
    result = payload.get("result")
    read_as = result.get("read_as") if record["ok"] and isinstance(result, dict) else None
    if isinstance(read_as, list) and read_as:   # the corrected arguments (spelling help on; a search's dict is not one)
        record["read_as"] = [dict(x) for x in read_as if isinstance(x, dict)]
    return payload, record


def _extends(old: Any, new: Any) -> bool:
    """``new`` holds everything ``old`` holds: equal, or every key of a dict ``old`` with a value that ``new``'s extends,
    or every item of a list ``old``."""
    if old == new:
        return True
    if isinstance(old, dict) and isinstance(new, dict):
        return all(k in new and _extends(v, new[k]) for k, v in old.items())
    if isinstance(old, list) and isinstance(new, list):
        return all(x in new for x in old)
    return False


def _note_tool(start: float, result: AgentResult, record: dict, text: str, registry: "ToolRegistry",
               refused: bool = False) -> None:
    """One tool entry on the answer's timeline (:mod:`sf_assistant.agent.timeline`; nothing when none is open) for
    ``record``, the last of ``result.tool_calls``: the round, the tool name (``<unknown>`` for a name the registry
    does not have), ``chars`` (the tool message sent back to the model), ``cut`` (longer than MAX_TOOL_RESULT_CHARS),
    ``ok``, ``error`` (``not_run``: refused before it ran, as invalid or, ``refused``, past the registry's call
    budget; ``failed``: the tool returned an error), and ``again_of`` (1-based, among this run's tool calls) with
    ``same`` for the latest earlier call of the same tool whose arguments this call repeats (``same`` true) or
    extends. The arguments are compared here and never written. Never raises."""
    try:
        name = record.get("name")
        args = record.get("arguments")
        entry: dict[str, Any] = {"round": result.steps, "name": name if name in registry.tools else "<unknown>",
                                 "chars": len(text), "cut": len(text) > MAX_TOOL_RESULT_CHARS, "ok": bool(record.get("ok"))}
        if not entry["ok"]:
            entry["error"] = "not_run" if refused or _validate_arguments(str(name or ""), args, registry) else "failed"
        calls = result.tool_calls[:-1] if result.tool_calls and result.tool_calls[-1] is record else result.tool_calls
        for i in range(len(calls) - 1, -1, -1):
            earlier = calls[i]
            if earlier.get("name") == name and _extends(earlier.get("arguments"), args):
                entry["again_of"], entry["same"] = i + 1, earlier.get("arguments") == args
                break
        timeline.add("tool", start, **entry)
    except Exception:  # noqa: BLE001 - logging never fails an answer
        pass


def _assistant_message(content: str | None, tool_calls: list[dict]) -> dict:
    """Build the OpenAI-format assistant turn that carries the model's tool calls."""
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": [
            {
                "id": tc["id"],
                "type": "function",
                "function": {
                    "name": tc["name"],
                    "arguments": json.dumps(tc.get("arguments") or {}, ensure_ascii=False, default=str),
                },
            }
            for tc in tool_calls
        ],
    }


# --------------------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------------------

def answer_question(
    q: "Question",
    llm: "LLM",
    registry: "ToolRegistry",
    cfg: "Config",
    *,
    system: str | None = None,
    require_tools: bool = False,
    require_tools_message: str | None = None,
    force_answer_message: str | None = None,
    check_answer: Callable[[str], str | None] | None = None,
    max_checks: int = 1,
) -> AgentResult:
    """Answer one question with the tool-calling loop. Never raises.

    Args:
        q: The question to answer (only ``qid`` and ``question`` are used).
        llm: Chat client (``LLM.chat`` contract from SPEC).
        registry: MCP-style tool registry over the object knowledge.
        cfg: Run configuration; ``cfg.max_tool_steps`` bounds the number of tool rounds.
        system: System prompt to use instead of ``AGENT_SYSTEM`` (``None`` keeps the default).
        require_tools: When the model answers before making any tool call, discard that
            answer and ask it once to look the knowledge up first. Off for evaluation runs.
        require_tools_message: Text of that one nudge (``None`` keeps ``REQUIRE_TOOLS_MESSAGE``).
        force_answer_message: Text sent when the tool budget is exhausted (``None`` keeps
            ``FORCE_ANSWER_MESSAGE``). Both defaults keep evaluation runs unchanged.
        check_answer: Called once with the first final answer while tool rounds remain; a
            returned text is sent back as a user message and the loop goes on (``None``, the
            default, accepts every answer, as evaluation runs do).
        max_checks: How many final answers ``check_answer`` sees at most (default 1: only the
            first). A later answer is checked only after the earlier check sent a message back.

    Returns:
        :class:`AgentResult`. On failure ``error`` is set and ``answer`` holds whatever
        partial answer existed (usually ``""``); the tool trace up to the failure is kept.
    """
    result = AgentResult(qid=q.qid, question=q.question, answer="")
    t_start = time.perf_counter()
    messages: list[dict] = [
        {"role": "system", "content": system or _agent_system()},
        {"role": "user", "content": q.question},
    ]
    max_steps = max(1, int(getattr(cfg, "max_tool_steps", 8) or 8))
    try:
        tools = registry.openai_tools()
        answered = False
        nudged = False
        checks = 0
        while result.steps < max_steps:
            resp = llm.chat(messages, tools=tools)
            result.steps += 1
            _add_usage(result.usage, resp.get("usage"))
            tool_calls = list(resp.get("tool_calls") or [])
            content = resp.get("content")

            if not tool_calls and require_tools and not result.tool_calls and not nudged:
                nudged = True
                timeline.add("check", time.perf_counter(), name="tools_first", nudge=True)
                messages.append({"role": "assistant", "content": _strip_thinking(content)})
                messages.append({"role": "user", "content": require_tools_message or REQUIRE_TOOLS_MESSAGE})
                continue

            if not tool_calls and check_answer is not None and checks < max(1, int(max_checks or 1)) \
                    and result.steps < max_steps:
                checks += 1
                draft = _strip_thinking(content)
                try:
                    retry = check_answer(draft)
                except Exception:  # a failing check must not cost the answer
                    retry = None
                if retry:
                    messages.append({"role": "assistant", "content": draft})
                    messages.append({"role": "user", "content": retry})
                    continue

            if not tool_calls:
                result.answer = _strip_thinking(content)
                answered = True
                break

            # Give every call a stable id (some servers omit it) so tool messages pair up.
            for tc in tool_calls:
                if not tc.get("id"):
                    tc["id"] = f"call_{uuid.uuid4().hex[:12]}"
            messages.append(_assistant_message(content, tool_calls))
            for tc in tool_calls:
                t_tool, refused = time.perf_counter(), getattr(registry, "refused", None)   # a budget's count
                payload, record = _execute_tool(str(tc.get("name") or ""), tc.get("arguments"), registry)
                result.tool_calls.append(record)
                text = _to_json(payload)
                _note_tool(t_tool, result, record, text, registry, refused != getattr(registry, "refused", None))
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": text})

        if not answered:
            # Budget exhausted while the model still wanted tools: force a final answer.
            timeline.add("check", time.perf_counter(), name="step_cap", nudge=True)
            messages.append({"role": "user", "content": force_answer_message or FORCE_ANSWER_MESSAGE})
            resp = llm.chat(messages, tools=None)
            result.steps += 1
            _add_usage(result.usage, resp.get("usage"))
            result.answer = _strip_thinking(resp.get("content"))
            if not (result.answer or "").strip() and resp.get("tool_calls"):
                # The model ignored tools=None and asked for tools again: retry the forced
                # call once before giving up, so a single stray tool call is not a failure.
                timeline.add("check", time.perf_counter(), name="step_cap", nudge=True)
                resp = llm.chat(messages, tools=None)
                result.steps += 1
                _add_usage(result.usage, resp.get("usage"))
                result.answer = _strip_thinking(resp.get("content"))
                if not (result.answer or "").strip() and resp.get("tool_calls"):
                    result.error = "model requested tools after the tool budget was exhausted"

        # An empty final answer is a model/infrastructure failure, not a wrong answer:
        # surface it in ``error`` so run summaries count it.
        if not (result.answer or "").strip() and result.error is None:
            result.error = "empty final answer"
    except Exception as exc:  # one failure must never kill a batch
        result.error = f"{type(exc).__name__}: {exc}"
    finally:
        result.latency_s = round(time.perf_counter() - t_start, 3)
    return result


def answer_all(
    questions: Iterable["Question"],
    llm: "LLM",
    registry: "ToolRegistry",
    cfg: "Config",
    *,
    workers: int = 4,
    progress: bool = True,
) -> list[AgentResult]:
    """Answer many questions concurrently; results are returned in input order.

    Args:
        questions: Questions to answer.
        llm, registry, cfg: As for :func:`answer_question`.
        workers: Thread-pool size (``1`` runs sequentially in the calling thread).
        progress: Print a one-line progress update every 10 completed questions and at the end.
    """
    qs = list(questions)
    total = len(qs)
    results: list[AgentResult | None] = [None] * total
    lock = threading.Lock()
    state = {"done": 0, "errors": 0}
    t0 = time.perf_counter()

    def _report(r: AgentResult) -> None:
        with lock:
            state["done"] += 1
            state["errors"] += int(r.error is not None)
            done = state["done"]
            if progress and (done % 10 == 0 or done == total):
                print(f"[agent] {done}/{total} answered, {state['errors']} errors, "
                      f"{time.perf_counter() - t0:.1f}s elapsed", flush=True)

    def _run(i: int) -> None:
        r = answer_question(qs[i], llm, registry, cfg)
        results[i] = r
        _report(r)

    if workers <= 1 or total <= 1:
        for i in range(total):
            _run(i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # list() re-raises anything unexpected; answer_question itself never raises.
            list(pool.map(_run, range(total)))
    return [r for r in results if r is not None]


# --------------------------------------------------------------------------------------
# spelling help (docs/resolver_integration.md (1) and (6))
# --------------------------------------------------------------------------------------

@dataclass
class QuestionSpelling:
    """The spelling help of one question (made by :func:`sf_assistant.pipeline.question_spelling`, which also writes
    the answer's "Read as" line: what reads the record session lives in the wiring layer, never here).

    ``knowledge``: the misspelt names the knowledge agent is told about (:attr:`block`; metadata only: no person
    name, no stored value). ``records``: those the record agent's hints list (``RecordTools.object_hints``: stored
    values too, no person name; [] without a record session). Both are ``knowledge_tools.SpellingSpan`` lists from
    one ``spell_correct`` of the question."""

    question: str
    knowledge: list = field(default_factory=list)
    records: list = field(default_factory=list)

    @property
    def block(self) -> str:
        """The knowledge agent's spelling block (``prompts/spelling_hints.md``), or '' when it has no span."""
        return spelling_section(self.knowledge)

    def knowledge_question(self, q: "Question") -> "Question":
        """``q`` for the knowledge step: its question followed by :attr:`block` (``q`` itself without one). The
        caller keeps ``q``, so the user's own words stay in the outcome and the JSONL."""
        block = self.block
        return dataclasses.replace(q, question=f"{q.question}\n\n{block}") if block else q

    def rows(self) -> list[dict]:
        """JSON-safe rows for the JSONL sidecar (typed words, decisions, spellings): the record agent's spans when a
        record session is open, else the knowledge agent's."""
        return [s.as_dict() for s in (self.records or self.knowledge)]
