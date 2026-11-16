"""The lean knowledge step for record questions (RECORDS_SPEC §21.19; no duckdb import).

In merged mode (``ask.py`` with records on), when the route call says the question needs record data (RECORDS),
the record agent gives every number and the merge needs from the knowledge agent only what the objects, fields
and values mean. The knowledge agent then runs lean: its own system prompt
(:data:`okrec.prompts.KNOWLEDGE_LEAN_SYSTEM`), a few read tools (:data:`LEAN_TOOLS`), at most
:data:`LEAN_MAX_TOOL_CALLS` tool calls and :data:`LEAN_MAX_MODEL_CALLS` model calls, and short replies. Its reply
is a short facts block for the merge. Code enforces the budget (:class:`LeanRegistry`, :class:`LeanLLM`) around the
unchanged ``okeval.agent.answer_question``. Metadata-only questions (NO_RECORDS), an unclear route, records-only
mode, ``--no-records`` and the notebook keep the full knowledge agent (or none) as before.
"""

from __future__ import annotations

import dataclasses
import json
import re
from typing import TYPE_CHECKING, Any

from sf_assistant.agent.knowledge_agent import AgentResult, answer_question
from sf_assistant.config import LIMITS

from .record_prompts import (KNOWLEDGE_LEAN_ANSWER_MESSAGE, KNOWLEDGE_LEAN_BUDGET_ERROR, lean_budget_error,
                      lean_require_tools_message, render_lean_system)
from .record_session import QUOTED_SPAN_RE, numbers_in, strip_thinking, text_tool_call

if TYPE_CHECKING:  # type hints only
    from sf_assistant.agent.question import Question
    from sf_assistant.config import Config

__all__ = ["LEAN_MAX_TOOL_CALLS", "LEAN_MAX_MODEL_CALLS", "LEAN_MAX_TOKENS", "LEAN_SEARCH_K", "LEAN_TOOLS",
           "LeanRegistry", "LeanLLM", "lean_facts", "lean_knowledge_answer", "is_budget_refusal", "text_tool_calls",
           "NO_TOOL_RESULT_ERROR"]

#: Knowledge tool calls that run in the lean step; later calls get :func:`okrec.prompts.lean_budget_error`.
LEAN_MAX_TOOL_CALLS = LIMITS.lean_max_tool_calls     # 3
#: Model calls of the lean step; the last one offers no tools, so the model must answer.
LEAN_MAX_MODEL_CALLS = LIMITS.lean_max_model_calls   # 3
#: ``max_tokens`` of each lean model call (a facts block is 2 to 5 short bullets).
LEAN_MAX_TOKENS = LIMITS.lean_max_tokens             # 600
#: Results per search call (``k``; the full agent's default is 8).
LEAN_SEARCH_K = 5
#: The tools the lean step offers, in this order, when the registry has them (search_all_knowledge only with several
#: knowledge files). Relationship, automation and provenance tools are left out: the lookup role is in get_field's
#: ``reference``, and the merge does not need flows.
LEAN_TOOLS = ("search_all_knowledge", "search_knowledge", "search_fields", "get_field", "get_picklist_values")
_SEARCH_TOOLS = frozenset({"search_all_knowledge", "search_knowledge", "search_fields"})
_BUDGET_LEAD = KNOWLEDGE_LEAN_BUDGET_ERROR.split("(", 1)[0].strip()   # "tool budget used up"
_TOOL_CALL_TEXT_RE = re.compile(r"<tool_call>.*?(?:</tool_call>|$)", re.DOTALL | re.IGNORECASE)
_TOOL_CALL_TAG_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.DOTALL | re.IGNORECASE)
_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)
#: Noise clauses of a facts block (what the knowledge does not hold; the merge needs none of it), in three forms:
#: the knowledge as the subject ("the knowledge base does not contain specific record data or counts", "the knowledge
#: for `Session__c` does not provide counts"); a clause with "it" or no subject after one that names the knowledge
#: ("..., but it does not provide specific counts", "..., but does not provide a breakdown"; only counts, numbers,
#: breakdowns and totals, never "data", which a field caveat says); and "no caveat is documented". A field as the
#: subject is a caveat and stays: "`X__c` does not have data", "Per the knowledge, the Evaluator lookup does not hold
#: data in practice", "the knowledge says `X__c` does not hold records".
_NEG = r"(?:does\s+not|doesn't|do\s+not|don't|cannot|can't|can\s+not)"
_HOLD = r"(?:contain|provide|include|have|hold|give|store|list|show|record|track)"
_KNOWLEDGE_NOISE_RE = re.compile(
    r"\b(?:knowledge(?:\s+base)?|documentation)(?:\s+(?:for|on|about|of)\s+(?:the\s+)?\S+(?:\s+object)?)?"
    r"(?:\s+(?:itself|here|provided|retrieved|files?))?\s+" + _NEG + r"\s+" + _HOLD + r"\s+(?:[\w-]+\s+){0,4}?"
    r"(?:record\s+data|records?|counts?|numbers?|data|breakdown|how\s+many|totals?|statistics)\b", re.IGNORECASE)
_ELIDED_NOISE_RE = re.compile(
    r"^\s*(?:(?:but|and|yet|although|though|whereas)\s+)?(?:it\s+|this\s+|they\s+)?" + _NEG + r"\s+" + _HOLD +
    r"\s+(?:[\w-]+\s+){0,4}?(?:record\s+data|counts?|numbers?|breakdown|how\s+many|totals?|statistics)\b",
    re.IGNORECASE)
_NO_CAVEAT_RE = re.compile(r"\bno\s+(?:specific\s+)?caveats?\s+(?:is|are)\s+(?:documented|noted|stated)\b",
                           re.IGNORECASE)
_NAMES_KNOWLEDGE_RE = re.compile(r"\bknowledge\b|\bdocumentation\b", re.IGNORECASE)
_API_IN_TICKS_RE = re.compile(r"`[A-Za-z][\w.]*`")
#: A clause of a facts block that holds a record count the knowledge quotes from the preprod snapshot ("filled on
#: 1,200 of 3,000 records in the preprod snapshot", "in the preprod snapshot of 2026-10-13, there were 9 sessions";
#: §21.19): the record agent gives every number, so lean_facts drops it. Digits in quoted values, API names and ISO
#: dates (:data:`_COUNT_MASK_RE`) are not counts. A count is "N of M", "N % of", or a number with the snapshot or
#: preprod named, or a number above 12 with filled, populated, blank or empty.
_COUNT_MASK_RE = re.compile(QUOTED_SPAN_RE.pattern + r"|\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?)?\b")
_N_OF_M_RE = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?\s*%?\s+of\s+(?:the\s+|all\s+)?\d|\d\s*%\s+of\b|"
                        r"\bper\s*cent\s+of\b", re.IGNORECASE)
_SNAPSHOT_RE = re.compile(r"\b(?:snapshot|pre-?prod(?:uction)?|sandbox)\b", re.IGNORECASE)
_FILL_RE = re.compile(r"\b(?:filled|populated|non-?blank|blank|empty)\b", re.IGNORECASE)
_PAREN_RE = re.compile(r"\s*\(([^()\n]*)\)")
#: Clause ends inside a facts line: a sentence end, a semicolon, or a comma before "but", "whereas", "although",
#: "though" or "yet".
_CLAUSE_SPLIT_RE = re.compile(r"((?<=[.!?])\s+|;\s+|,\s+(?=(?:but|whereas|although|though|yet)\b))", re.IGNORECASE)
_BULLET_RE = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")
_LEAD_CONJ_RE = re.compile(r"^(?:but|whereas|although|though|yet|and)\s+", re.IGNORECASE)
#: ``AgentResult.error`` when no tool call of the lean step returned a result: the reply came from memory, and the
#: knowledge rules allow facts from tool results only, so the reply is dropped.
NO_TOOL_RESULT_ERROR = "the lean knowledge step got no tool result; its reply is not used"


class LeanRegistry:
    """A knowledge tool registry (``okeval`` ``ToolRegistry`` or ``MultiToolRegistry``) cut down to
    :data:`LEAN_TOOLS` and to ``max_calls`` calls that run. Same duck-typed surface ``answer_question`` uses
    (``tools``, ``openai_tools()``, ``call()``); ``call`` never raises."""

    def __init__(self, registry: Any, max_calls: int = LEAN_MAX_TOOL_CALLS, names: tuple[str, ...] = LEAN_TOOLS,
                 search_k: int = LEAN_SEARCH_K) -> None:
        self.registry = registry
        self.max_calls = max(0, int(max_calls))
        self.search_k = max(1, int(search_k))
        available = dict(getattr(registry, "tools", {}) or {})
        kept = [n for n in names if n in available]
        self.tools = {n: available[n] for n in (kept or list(available))}  # no lean tool at all: every tool
        self.calls = 0      # calls that ran
        self.refused = 0    # calls past the budget (not run)

    def openai_tools(self) -> list[dict]:
        return [t for t in self.registry.openai_tools() if (t.get("function") or {}).get("name") in self.tools]

    def list_tools(self) -> list[dict]:
        return [t for t in self.registry.list_tools() if t.get("name") in self.tools]

    def call(self, name: str, arguments: dict | None = None) -> dict:
        if name not in self.tools:
            return {"ok": False, "result": None, "chunk_ids": [],
                    "error": f"unknown tool '{name}'; available: {sorted(self.tools)}"}
        if self.calls >= self.max_calls:
            self.refused += 1
            return {"ok": False, "result": None, "chunk_ids": [], "error": lean_budget_error(self.max_calls)}
        self.calls += 1
        args = dict(arguments or {})
        if name in _SEARCH_TOOLS:
            try:
                k = int(args.get("k") or self.search_k)
            except (TypeError, ValueError):
                k = self.search_k
            args["k"] = max(1, min(k, self.search_k))
        try:
            return self.registry.call(name, args)
        except Exception as exc:  # noqa: BLE001 - registries never raise; be defensive anyway
            return {"ok": False, "result": None, "chunk_ids": [], "error": f"{type(exc).__name__}: {exc}"}


class LeanLLM:
    """Chat client wrapper for the lean step: at most ``max_calls`` model calls, each with at most ``max_tokens``.

    The last call offers no tools, adds :data:`~okrec.prompts.KNOWLEDGE_LEAN_ANSWER_MESSAGE` to the request (not to
    the agent's history) and drops any tool call from the reply, so ``answer_question`` takes its text as the final
    answer. On the other calls, a reply that only writes tool calls as text (:func:`text_tool_calls`) becomes those
    calls. A call past the budget is not sent and returns an empty reply. Other attributes pass through.
    """

    def __init__(self, llm: Any, max_calls: int = LEAN_MAX_MODEL_CALLS, max_tokens: int = LEAN_MAX_TOKENS) -> None:
        self.llm = llm
        self.max_calls = max(1, int(max_calls))
        self.max_tokens = max(1, int(max_tokens))
        self.calls = 0
        self.refused = 0
        self.recovered = 0   # replies whose tool calls were written as text (text_tool_calls)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.llm, name)

    def chat(self, messages: list[dict], tools: list[dict] | None = None, **kwargs: Any) -> dict:
        if self.calls >= self.max_calls:
            self.refused += 1
            return {"content": "", "tool_calls": [], "usage": {}}
        self.calls += 1
        last = self.calls >= self.max_calls
        if last:
            if tools:
                messages = list(messages) + [{"role": "user", "content": KNOWLEDGE_LEAN_ANSWER_MESSAGE}]
            tools = None
        asked = kwargs.get("max_tokens")
        kwargs["max_tokens"] = min(int(asked), self.max_tokens) if asked else self.max_tokens
        resp = dict(self.llm.chat(messages, tools=tools, **kwargs) or {})
        if last:
            resp["tool_calls"] = []
        elif tools and not resp.get("tool_calls"):   # tool calls written as text become real calls
            calls = text_tool_calls(resp.get("content"), tools)
            if calls:
                self.recovered += 1
                resp["content"] = None
                resp["tool_calls"] = [{"id": f"lean-text-{self.recovered}-{i}", "name": n, "arguments": a}
                                      for i, (n, a) in enumerate(calls)]
        return resp


def text_tool_calls(text: str | None, tools: list[dict] | None) -> list[tuple[str, dict]]:
    """Tool calls a reply writes as text, else []. The reply must be nothing but the calls: one or more
    ``<tool_call>`` blocks, or JSON (optionally in a ```json fence) holding one call or a list of calls, each
    ``{"name" | "tool": <offered tool>, "arguments" | "args" | "parameters": {...}}``; one bare call is also read
    as :func:`okrec.session.text_tool_call` reads it. Qwen wrote such a list instead of calling the tools in the
    timed runs (§21.19)."""
    single = text_tool_call(text, tools)
    if single is not None:
        return [single]
    offered = {t["function"]["name"] for t in tools or []
               if isinstance(t, dict) and isinstance(t.get("function"), dict) and t["function"].get("name")}
    body = strip_thinking(text)
    if not body or not offered:
        return []
    tags = _TOOL_CALL_TAG_RE.findall(body)
    if tags and not _TOOL_CALL_TAG_RE.sub("", body).strip():
        raw = tags
    else:
        m = _JSON_FENCE_RE.match(body)
        raw = [m.group(1) if m else body]
    items: list = []
    for chunk in raw:
        try:
            obj = json.loads(chunk)
        except ValueError:
            return []
        items.extend(obj if isinstance(obj, list) else [obj])
    calls: list[tuple[str, dict]] = []
    for it in items:
        if not isinstance(it, dict):
            return []
        name = next((it[k] for k in ("name", "tool", "function") if isinstance(it.get(k), str)), None)
        args = next((it[k] for k in ("arguments", "args", "parameters") if k in it), {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                return []
        if name not in offered or not isinstance(args, dict):
            return []
        calls.append((name, args))
    return calls


def _is_count_clause(text: str) -> bool:
    """True when ``text`` holds a record count the knowledge quotes (:data:`_N_OF_M_RE`, :data:`_SNAPSHOT_RE`,
    :data:`_FILL_RE`)."""
    masked = _COUNT_MASK_RE.sub(" ", text or "")
    nums = numbers_in(masked)
    if not nums:
        return False
    if _N_OF_M_RE.search(masked) or _SNAPSHOT_RE.search(masked):
        return True
    return bool(_FILL_RE.search(masked)) and any(float(n) > 12 for n in nums)


def _is_noise_clause(text: str, names_knowledge: bool) -> bool:
    """True for a clause about what the knowledge does not hold (:data:`_KNOWLEDGE_NOISE_RE`; with
    ``names_knowledge``, the line names the knowledge, also :data:`_ELIDED_NOISE_RE`; :data:`_NO_CAVEAT_RE`)."""
    return bool(_KNOWLEDGE_NOISE_RE.search(text) or _NO_CAVEAT_RE.search(text)
                or (names_knowledge and _ELIDED_NOISE_RE.search(text)))


def _facts_line(line: str) -> str | None:
    """One facts line without its noise and count clauses, or None when nothing is left. A line with a noise clause
    and no API name in backticks is dropped whole (its other clauses are the same talk: "... record data or counts
    for X; it only documents the schema"); otherwise clauses are dropped one by one, and so is a parenthesis that
    holds a count ("mostly blank (150 of 400 filled in preprod)")."""
    m = _BULLET_RE.match(line)
    prefix = m.group(0) if m else ""
    body = line[len(prefix):]
    trimmed = _PAREN_RE.sub(lambda p: "" if _is_count_clause(p.group(1)) else p.group(0), body)
    parts = _CLAUSE_SPLIT_RE.split(trimmed)
    clauses, seps = parts[0::2], [""] + parts[1::2]
    names_knowledge = bool(_NAMES_KNOWLEDGE_RE.search(trimmed))
    noise = [_is_noise_clause(c, names_knowledge) for c in clauses]
    if any(noise) and not _API_IN_TICKS_RE.search(trimmed):
        return None
    keep = [not n and not _is_count_clause(c) and bool(c.strip()) for c, n in zip(clauses, noise)]
    if all(keep) and trimmed == body:
        return line
    if not any(keep):
        return None
    out = ""
    for i, (sep, clause, k) in enumerate(zip(seps, clauses, keep)):
        if not k:
            continue
        if not out:
            if i:   # a later clause opens the line now: no leading "but", a capital first letter
                clause = _LEAD_CONJ_RE.sub("", clause.lstrip())
                clause = clause[:1].upper() + clause[1:]
            out = clause
        else:
            out += sep + clause
    out = out.rstrip(" ,;:")
    if body.rstrip().endswith((".", "!", "?")) and not out.endswith((".", "!", "?")):
        out += "."
    return (prefix + out) if out.strip() else None


def lean_facts(text: str | None) -> str:
    """The lean reply as the facts block for the merge: without tool calls written as text (a reply to the last,
    tool-free call can still hold ``<tool_call>`` tags), without what the knowledge does not hold ("the knowledge base
    does not contain specific record data or counts", "no caveat is documented"), without record counts the knowledge
    quotes from the preprod snapshot (:func:`_facts_line`, clause by clause) and without blank-line runs."""
    t = _TOOL_CALL_TEXT_RE.sub("", text or "")
    lines = []
    for ln in t.split("\n"):
        kept = _facts_line(ln) if ln.strip() else ln
        if kept is not None:
            lines.append(kept)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _json_only(text: str) -> bool:
    """True when ``text`` (thinking removed, optionally in a ```json fence) is one JSON object or list: no facts block,
    e.g. a tool call with a malformed name written as the last reply."""
    body = strip_thinking(text)
    m = _JSON_FENCE_RE.match(body)
    body = m.group(1) if m else body
    if not body.startswith(("{", "[")):
        return False
    try:
        return isinstance(json.loads(body), (dict, list))
    except ValueError:
        return False


def is_budget_refusal(call: dict) -> bool:
    """True for a lean-step tool call (an ``AgentResult.tool_calls`` entry) that the budget refused."""
    return not call.get("ok") and str(call.get("error") or "").startswith(_BUDGET_LEAD)


def lean_knowledge_answer(q: "Question", llm: Any, registry: Any, cfg: "Config", *,
                          max_tool_calls: int = LEAN_MAX_TOOL_CALLS, max_model_calls: int = LEAN_MAX_MODEL_CALLS,
                          max_tokens: int = LEAN_MAX_TOKENS) -> AgentResult:
    """Run the lean knowledge step for one record question. Never raises (``answer_question`` does not).

    ``answer_question`` gets the lean system prompt, :class:`LeanRegistry` over ``registry``, :class:`LeanLLM` over
    ``llm``, ``cfg`` with ``max_tool_steps = max_model_calls`` and ``require_tools=True`` (a reply without a tool
    call is sent back once with :func:`~okrec.prompts.lean_require_tools_message`; it counts as one of the model
    calls). The answer is :func:`lean_facts` of the reply. It is dropped (``answer`` "", ``error`` set; the merge then
    shows the record answer, as for any empty knowledge answer) when no tool call returned a result
    (:data:`NO_TOOL_RESULT_ERROR`: the reply came from memory), when it is empty, when it only writes tool calls or
    is only JSON, or when cleaning it fails (``error`` names the exception). The prompt names the search tool the
    step offers (:func:`~okrec.prompts.render_lean_system`).
    """
    tools = LeanRegistry(registry, max_tool_calls)
    client = LeanLLM(llm, max_model_calls, max_tokens)
    r = answer_question(q, client, tools, dataclasses.replace(cfg, max_tool_steps=max(1, int(max_model_calls))),
                        system=render_lean_system(max_tool_calls, tuple(tools.tools)), require_tools=True,
                        require_tools_message=lean_require_tools_message(max_tool_calls),
                        force_answer_message=KNOWLEDGE_LEAN_ANSWER_MESSAGE)
    if not any(c.get("ok") for c in r.tool_calls):
        r.answer, r.error = "", r.error or NO_TOOL_RESULT_ERROR   # a reply from memory is not used
        return r
    try:
        answer = lean_facts(r.answer)
        if text_tool_calls(answer, tools.openai_tools()) or _json_only(answer):   # tool calls or JSON, no facts
            answer = ""
    except Exception as exc:  # noqa: BLE001 - this step never raises; the merge then shows the record answer
        r.answer, r.error = "", f"{type(exc).__name__}: {exc}"[:300]
        return r
    r.answer = answer
    if not r.answer and r.error is None:
        r.error = "empty final answer"
    return r
