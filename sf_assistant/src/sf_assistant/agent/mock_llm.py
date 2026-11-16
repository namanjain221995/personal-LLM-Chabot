"""Deterministic, offline stand-in for ``openai.OpenAI`` used by tests and the notebook dry run.

:class:`MockClient` implements just ``client.chat.completions.create(**kwargs)`` and returns
objects shaped like the OpenAI SDK's ``ChatCompletion`` (``choices[0].message.content``,
``choices[0].message.tool_calls[i].id / .function.name / .function.arguments`` and ``usage``), so
:class:`sf_assistant.agent.llm.LLM` works unchanged::

    from sf_assistant.agent.llm import LLM
    from sf_assistant.agent.mock_llm import MockClient
    llm = LLM(cfg, client=MockClient())

Behaviour (no randomness, no network):

* **Agent calls** (``tools`` passed, or a conversation that already contains tool results):
  step 1 calls ``search_knowledge`` with the user question; step 2 calls
  ``get_picklist_values`` (question mentions values/options/statuses) or ``get_field`` for the
  field named in the question or the best ``field:`` / ``picklist:`` chunk from the search; after
  two tool results (or when ``tools`` is None) it returns a final answer that echoes the tool
  results verbatim. Record-data questions (counts, averages, specific records) get the
  "not available from object knowledge — needs record data" refusal.
* **JSON calls** (``response_format`` json_object or a prompt that demands JSON): returns valid
  JSON for question generation (``{"questions": [...]}``), paraphrasing
  (``{"paraphrases": [...]}``) or judging (correctness/completeness/... computed from the
  expected facts actually present in the answer). Other JSON requests get ``{}``.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MockClient",
    "MockCompletion",
    "MockChoice",
    "MockMessage",
    "MockToolCall",
    "MockFunction",
    "MockUsage",
    "APIConnectionError",
]


# ------------------------------------------------------------------ SDK-shaped response objects
@dataclass
class MockFunction:
    name: str
    arguments: str  # JSON string, like the SDK


@dataclass
class MockToolCall:
    id: str
    function: MockFunction
    type: str = "function"


@dataclass
class MockMessage:
    content: str | None
    tool_calls: list[MockToolCall] | None = None
    role: str = "assistant"


@dataclass
class MockChoice:
    message: MockMessage
    index: int = 0
    finish_reason: str = "stop"


@dataclass
class MockUsage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class MockCompletion:
    id: str
    model: str
    choices: list[MockChoice]
    usage: MockUsage
    object: str = "chat.completion"


class APIConnectionError(Exception):
    """Same class name as the SDK's, so ``okeval.llm`` treats it as retryable."""


# ----------------------------------------------------------------------------- heuristics
_RECORD_DATA_RE = re.compile(
    r"\b(how many|number of (?:records|interviews)|count(?:s)? of|total number|average|"
    r"how much|which records?|list (?:all )?(?:the )?(?:records|interviews)|latest|most recent|"
    r"last week|last month|yesterday|today|this week|who owns|owner of|currently has|"
    r"specific record|INT-\d+)\b",
    re.IGNORECASE,
)
_PICKLIST_RE = re.compile(
    r"\b(values?|options?|picklist|allowed|possible|choices?|statuses|states|kinds of|types of)\b",
    re.IGNORECASE,
)
_API_NAME_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*__c)\b")
_STD_FIELD_RE = re.compile(
    r"\b(Name|CreatedDate|LastModifiedDate|CreatedById|LastModifiedById|OwnerId|Id)\b"
)
_REFUSAL_RE = re.compile(
    r"not (available|in|contained|present)|no information|does not (contain|include|specify)|"
    r"cannot (determine|answer)|needs record data",
    re.IGNORECASE,
)
_ECHO_LIMIT = 6000  # chars of each tool result echoed into the final answer


def _text(content: Any) -> str:
    """Message content as text (handles None and multi-part content lists)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    return str(content)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict or attribute from an object (messages may be either)."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _between(text: str, tag: str) -> str | None:
    """Return the text inside ``<tag>...</tag>`` (first occurrence), stripped."""
    m = re.search(rf"<{tag}>\s*(.*?)\s*</{tag}>", text, re.DOTALL)
    return m.group(1) if m else None


def _tokens(text: str) -> int:
    """Rough token estimate (4 chars per token) for usage accounting."""
    return max(1, len(text) // 4)


def _humanize(api_name: str) -> str:
    """``Final_Descion__c`` -> ``final descion`` (used by mock paraphrases)."""
    base = re.sub(r"__c$", "", api_name)
    base = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base).replace("_", " ")
    return re.sub(r"\s+", " ", base).strip().lower()


# --------------------------------------------------------------------------------- client
class _Completions:
    def __init__(self, owner: "MockClient") -> None:
        self._owner = owner

    def create(self, **kwargs: Any) -> MockCompletion:
        return self._owner._create(**kwargs)


class _Chat:
    def __init__(self, owner: "MockClient") -> None:
        self.completions = _Completions(owner)


class MockClient:
    """Deterministic fake of ``openai.OpenAI`` (only ``chat.completions.create``).

    Args:
        fail_first: raise :class:`APIConnectionError` on the first N requests (to test retries).

    Attributes:
        requests: every kwargs dict received, in order (thread-safe append).
    """

    def __init__(self, fail_first: int = 0) -> None:
        self.chat = _Chat(self)
        self.requests: list[dict] = []
        self._fail_remaining = fail_first
        self._lock = threading.Lock()

    # ------------------------------------------------------------------------ dispatch
    def _create(self, **kwargs: Any) -> MockCompletion:
        with self._lock:
            self.requests.append(kwargs)
            if self._fail_remaining > 0:
                self._fail_remaining -= 1
                raise APIConnectionError("mock transient connection failure")

        messages: list[Any] = list(kwargs.get("messages") or [])
        tools: list[dict] | None = kwargs.get("tools")
        model = str(kwargs.get("model") or "mock-model")
        all_text = "\n".join(_text(_get(m, "content")) for m in messages)

        rf = kwargs.get("response_format") or {}
        wants_json = (isinstance(rf, dict) and rf.get("type") in {"json_object", "json_schema"}) or (
            not tools and re.search(r"\bJSON\b", all_text) is not None and not self._is_agent(messages)
        )

        if tools or self._is_agent(messages):
            content, tool_calls = self._agent_step(messages, tools)
        elif wants_json:
            content, tool_calls = json.dumps(self._json_reply(messages, all_text)), None
        else:
            content, tool_calls = "Mock response.", None

        message = MockMessage(content=content, tool_calls=tool_calls or None)
        finish = "tool_calls" if tool_calls else "stop"
        digest = hashlib.sha1(all_text.encode("utf-8")).hexdigest()[:12]
        completion_text = (content or "") + "".join(tc.function.arguments for tc in tool_calls or [])
        return MockCompletion(
            id=f"mockcmpl-{digest}",
            model=model,
            choices=[MockChoice(message=message, finish_reason=finish)],
            usage=MockUsage(prompt_tokens=_tokens(all_text), completion_tokens=_tokens(completion_text)),
        )

    @staticmethod
    def _is_agent(messages: list[Any]) -> bool:
        """True when the conversation already contains tool calls or tool results."""
        return any(_get(m, "role") == "tool" or _get(m, "tool_calls") for m in messages)

    # --------------------------------------------------------------------------- agent
    def _agent_step(
        self, messages: list[Any], tools: list[dict] | None
    ) -> tuple[str | None, list[MockToolCall] | None]:
        """Decide the next agent move: a tool call (steps 1-2) or the final answer."""
        # Only consider the current turn: messages after the last user message.
        last_user = max((i for i, m in enumerate(messages) if _get(m, "role") == "user"), default=-1)
        question = _text(_get(messages[last_user], "content")) if last_user >= 0 else ""
        turn = messages[last_user + 1 :]
        tool_results = [m for m in turn if _get(m, "role") == "tool"]
        available = {
            _get(_get(t, "function", {}), "name") for t in (tools or []) if isinstance(t, dict)
        }

        if tools and len(tool_results) == 0:
            name = "search_knowledge" if "search_knowledge" in available else sorted(available)[0]
            args = {"query": question, "k": 5} if name == "search_knowledge" else {}
            return None, [self._tool_call(name, args, step=1, question=question)]

        if tools and len(tool_results) == 1:
            call = self._second_call(question, _text(_get(tool_results[0], "content")), available)
            if call is not None:
                return None, [call]

        return self._final_answer(question, turn), None

    def _second_call(self, question: str, search_content: str, available: set) -> MockToolCall | None:
        """Pick get_picklist_values / get_field / describe_object from keywords + search hits."""
        chunk_ids: list[str] = []
        try:
            payload = json.loads(search_content)
            chunk_ids = [str(c) for c in payload.get("chunk_ids") or []]
            if not chunk_ids and isinstance(payload.get("result"), list):
                chunk_ids = [str(r.get("id")) for r in payload["result"] if isinstance(r, dict)]
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

        wants_picklist = bool(_PICKLIST_RE.search(question))
        hit_fields = [c.split(":", 1)[1] for c in chunk_ids if c.startswith(("field:", "picklist:"))]
        hit_picklists = [c.split(":", 1)[1] for c in chunk_ids if c.startswith("picklist:")]
        explicit = _API_NAME_RE.findall(question) + _STD_FIELD_RE.findall(question)
        # Prefer a field named in the question that the search confirmed, then any named field,
        # then the best search hit.
        confirmed = [f for f in explicit if f in hit_fields]
        target = (confirmed or explicit or (hit_picklists if wants_picklist else []) or hit_fields or [None])[0]

        if target is None:
            if "describe_object" in available:
                return self._tool_call("describe_object", {}, step=2, question=question)
            return None
        if wants_picklist and "get_picklist_values" in available:
            return self._tool_call("get_picklist_values", {"field": target}, step=2, question=question)
        if "get_field" in available:
            return self._tool_call("get_field", {"name": target}, step=2, question=question)
        return None

    @staticmethod
    def _tool_call(name: str, args: dict, *, step: int, question: str) -> MockToolCall:
        digest = hashlib.sha1(f"{question}|{step}|{name}".encode("utf-8")).hexdigest()[:10]
        return MockToolCall(
            id=f"call_{step}_{digest}",
            function=MockFunction(name=name, arguments=json.dumps(args, ensure_ascii=False)),
        )

    @staticmethod
    def _final_answer(question: str, turn: list[Any]) -> str:
        """Echo every tool result of this turn, prefixed by a refusal when appropriate."""
        names: dict[str, str] = {}
        for m in turn:
            for tc in _get(m, "tool_calls") or []:
                fn = _get(tc, "function", {})
                names[str(_get(tc, "id"))] = str(_get(fn, "name"))
        results = [m for m in turn if _get(m, "role") == "tool"]

        any_ok = False
        lines: list[str] = []
        for m in results:
            content = _text(_get(m, "content"))
            try:
                any_ok = any_ok or bool(json.loads(content).get("ok"))
            except (json.JSONDecodeError, AttributeError, TypeError):
                any_ok = True  # non-JSON tool output: treat as usable text
            name = names.get(str(_get(m, "tool_call_id")), "tool")
            lines.append(f"- `{name}` returned: {content[:_ECHO_LIMIT]}")

        if _RECORD_DATA_RE.search(question):
            head = ("This is not available from object knowledge — needs record data. "
                    "The object knowledge describes metadata only.")
        elif not results:
            head = "The object knowledge does not contain this information (no tools were used)."
        elif not any_ok:
            head = "The object knowledge does not contain this information."
        else:
            head = "Based on the object knowledge (mock answer echoing tool results):"
        return head + ("\n" + "\n".join(lines) if lines else "")

    # ---------------------------------------------------------------------------- JSON
    def _json_reply(self, messages: list[Any], all_text: str) -> dict:
        """Route a JSON-mode request to the judge / question-gen / paraphrase generator."""
        user = next(
            (_text(_get(m, "content")) for m in reversed(messages) if _get(m, "role") == "user"), ""
        )
        if "<answer>" in user and "correctness" in all_text:
            return self._judge(user)
        if '"paraphrases"' in all_text:
            return self._paraphrase(user)
        if '"questions"' in all_text:
            return self._questions(user)
        return {}

    @staticmethod
    def _judge(user: str) -> dict:
        """Score by the fraction of expected facts present in the answer (case-insensitive)."""
        answer = _between(user, "answer") or ""
        answerable = (_between(user, "answerable") or "true").strip().lower() != "false"
        facts_raw = _between(user, "expected_facts") or "[]"
        try:
            facts = [str(f) for f in json.loads(facts_raw)]
        except json.JSONDecodeError:
            facts = [f.strip("-• ").strip() for f in facts_raw.splitlines() if f.strip()]

        if not answerable:
            refused = bool(_REFUSAL_RE.search(answer))
            score = 2 if refused else 0
            return {
                "correctness": score, "completeness": score, "groundedness": 2 if refused else 1,
                "hallucination": False, "missing_knowledge": False,
                "notes": "" if refused else "Did not state that the information is unavailable.",
            }

        norm = re.sub(r"\s+", " ", answer.lower())
        missing = [f for f in facts if re.sub(r"\s+", " ", f.lower()) not in norm]
        recall = 1.0 if not facts else (len(facts) - len(missing)) / len(facts)
        score = 2 if recall >= 0.8 else 1 if recall >= 0.5 else 0
        return {
            "correctness": score,
            "completeness": score,
            "groundedness": 2,
            "hallucination": False,
            "missing_knowledge": bool(facts) and recall < 0.5,
            "notes": "" if not missing else f"Missing: {', '.join(missing[:5])}",
        }

    @staticmethod
    def _questions(user: str) -> dict:
        """Build N simple questions for the requested category from API names in the excerpts."""
        m_cat = re.search(r"for category `([^`]+)`", user) or re.search(r"Category:\s*(\S+)", user)
        category = m_cat.group(1) if m_cat else "field_meaning"
        m_n = re.search(r"exactly (\d+) questions", user)
        n = int(m_n.group(1)) if m_n else 3
        m_obj = re.search(r"Object:\s*(\S+)", user)
        obj = m_obj.group(1) if m_obj else "the object"

        if category.startswith("unanswerable"):
            canned = [
                f"How many {obj} records were created last week?",
                f"Which profiles have edit access to {obj}?",
                f"What is the page layout for {obj}?",
                f"What is the average score across all {obj} records?",
                f"Who owns the most recent {obj} record?",
            ]
            items = [
                {"question": canned[i % len(canned)] + ("" if i < len(canned) else f" (variant {i})"),
                 "category": category, "expected_facts": [], "expected_tools": ["search_knowledge"],
                 "answerable": False}
                for i in range(n)
            ]
            return {"questions": items}

        excerpts = _between(user, "knowledge_excerpts") or user
        names: list[str] = []
        for name in _API_NAME_RE.findall(excerpts):
            if name not in names and name != obj:
                names.append(name)
        if not names:
            names = [obj]
        items = []
        for i in range(n):
            api = names[i % len(names)]
            suffix = "" if i < len(names) else f" (variant {i // len(names) + 1})"
            items.append({
                "question": f"What does the object knowledge say about {api}?{suffix}",
                "category": category,
                "expected_facts": [api],
                "expected_tools": ["get_field", "search_knowledge"],
                "answerable": True,
            })
        return {"questions": items}

    @staticmethod
    def _paraphrase(user: str) -> dict:
        """Deterministic rewrites with API names replaced by humanised words."""
        question = (_between(user, "question") or "").strip() or "this question"
        m_n = re.search(r"in exactly (\d+) different ways", user)
        n = int(m_n.group(1)) if m_n else 2
        plain = _API_NAME_RE.sub(lambda m: _humanize(m.group(1)), question)
        plain = plain.rstrip("?").strip()
        lowered = plain[:1].lower() + plain[1:]
        frames = [
            "In plain terms, {q}?",
            "Could you tell me: {q}?",
            "Quick question about interviews: {q}?",
            "I need to know this for a report: {q}?",
            "Help me understand: {q}?",
        ]
        out = [frames[i % len(frames)].format(q=lowered) + ("" if i < len(frames) else f" ({i})")
               for i in range(n)]
        return {"paraphrases": out}
