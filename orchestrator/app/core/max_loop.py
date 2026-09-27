"""Max, as a loop a person can watch: plan, draft, a check code does,
a fenced critique, one guarded revision.

WHAT MAX WAS. engines/chat.py's Max branch was the only thing separating Max
from Think in chat, and it was not a loop: `best_of.generate_candidates`
produced three whole NON-streaming completions concurrently, a thinking-off
guided-JSON judge picked one from `_JUDGE_ANSWER_CHARS = 4000` characters of
each — about a seventh of a report-sized answer — and the winner was
re-emitted in 200-character slices as if it were streaming. No plan, no
independent read of the draft against the person's requirements, no
revision, and not one step event. The owner's complaint that Max "does not
feel like anything is thinking" was a correct reading of the code.

THE CASTING IS HONEST. "The other LLMs" on this box are an 8B router and a
0.6B embedder. Arbitration between the 35B main model and an 8B router
lowers answer quality, and llm.py's `router_chat_completion` says so in
writing: router calls are classification, never a person's answer. So the
agents are ROLES — planner, writer, critic, reviser — played by the model we
already have, in four separate calls with four different system prompts.
The router's only job in this design is the model-proposer half of
`contract.extract`, over the person's own prompt, and it never sees the
plan, the draft or the critique. The embedder and the reranker return
scores; dressing them as agents in the timeline would be a latency cost and
a lie about what is happening.

THE PHASES, and what each one may cost.

  1. PLAN     one call, thinking OFF (measured: see `_plan`), of at most
              PLAN_MAX_TOKENS. The
              person's requirements restated as a writing plan. A plan that
              fails is not fatal: the step is marked failed and the draft
              writes without one.
  2. DRAFT    continuation.stream_long_completion, exactly as the non-Max
              path does, so the draft STREAMS. This is the single biggest
              felt change — Max used to be a long silence before the first
              character.
  3. CHECK    core/contract.check. PURE PYTHON, ZERO model calls. What can
              be counted is counted here and never asked of a model.
  4. CRITIQUE one call, thinking FORCED OFF, over the draft and ONLY the
              contract items a counter could not decide. Skipped entirely
              when there are none — a critic asked to re-derive what code
              already decided is a call spent on nothing.
  5. REVISE   one call, only when a must is unmet, guarded so it cannot
              make the answer worse.

WHY THE CRITIC RUNS WITH THINKING OFF — measured, not preferred. Thinking
off, over this platform's own published document with a ten-item checklist:
prompt 2,325 tokens, completion 956, TTFT 0.267 s, total 9.36 s, and the
verdict named four defects that are all true. The same critique with
thinking ON: 2,902 reasoning tokens, TTFT 36.4 s, and the verdict CUT OFF at
its 3,000-token cap with nothing usable — json_completion's thinking-pool
trap, which its own docstring documents. There is deliberately no setting
that turns it back on.

WHY THE REVISION APPENDS RATHER THAN REPLACES. The design this track was
given says "revise: one call, whole answer". The SSE protocol has no event
that replaces text already streamed — `sse.ts` parses token, reasoning,
status, step, research, meta, done and error, and the assistant message is
built by appending token deltas — and the frontend is out of this track's
scope. A whole-answer revision would therefore either show the person two
answers one after the other, or store an answer different from the one they
watched being written (the repository deliberately avoids that: see
chat._say_what_was_left_out, which emits its sentence AND returns it so the
stored message and the streamed one are the same text). So the reviser is
given the whole draft and the unmet requirements, and writes the missing
material as a continuation of the same document. What is streamed and what
is stored stay identical. `_worse` below is still the faithful plain-text
port of compose.py's guard, applied to the finished answer against the
draft, so the day a replace event exists the guard is already correct.

NOTHING HERE REACHES FAST OR THINK. The critique and the revision are extra
model calls, and Fast's budget is the owner's line. engines/chat.py gates
this module behind `effort == "max"`; `contract.check` is pure Python and
would be safe at any effort, but nothing at Fast or Think consumes it, so
nothing at Fast or Think runs it.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from . import contract as C

log = logging.getLogger(__name__)

Emit = Callable[[str, dict], Awaitable[None]]
OnDelta = Callable[[str, str], Awaitable[None]]

#: The plan is a page of structure, not prose, and it runs with thinking
#: off, so this is the whole call: no reasoning shares the pool.
PLAN_MAX_TOKENS = 700

#: The critic's verdict. The measured run above produced 956 completion
#: tokens for ten items; 1,200 leaves room without inviting an essay.
CRITIQUE_MAX_TOKENS = 1_200

#: How much of the draft each later prompt reads. At the engine's measured
#: prefill rate (0.105 ms per prompt token) 60,000 characters is well under
#: two seconds, and a report that overruns it is truncated visibly rather
#: than silently — `_clip` says so in the prompt.
CRITIC_DRAFT_CHARS = 60_000

#: compose.py's CORRECTION_KEEP_FRACTION, ported to plain text.
CORRECTION_KEEP_FRACTION = 0.9

#: A revision has to earn its call: below this many words it is noise.
MIN_REVISION_WORDS = 20

#: The shape that takes the loop rather than best-of-N. Best-of-N is
#: genuinely the better shape for a short ask, whose whole candidate fits
#: inside the judge's 4,000-character window; it is kept, switchable through
#: EXTRA_HIGH_SAMPLES, and routed to by shape.
LOOP_MIN_SECTIONS = 2
LOOP_MIN_ELEMENTS = 3

STEP_PLAN = "Reading the request"
STEP_DRAFT = "Writing the answer"
STEP_CHECK = "Checking it against your request"
STEP_CRITIQUE = "Reviewing what is missing"
STEP_REVISE = "Revising what did not match"

_PLACEHOLDER_RE = re.compile(
    r"\b(?:TODO|TBD|FIXME|lorem ipsum|placeholder|XXXX?)\b|\[(?:insert|your|add)\b", re.I
)


def wants_loop(contract: C.Contract) -> bool:
    """Is this ask the shape the loop is for? Decided in code, from the
    contract's own counts, with no model call and no wording heuristic."""
    sections = sum(1 for i in contract.items if i.kind == "section")
    elements = sum(1 for i in contract.items if i.kind == "element")
    return sections >= LOOP_MIN_SECTIONS or elements >= LOOP_MIN_ELEMENTS


# ------------------------------------------------------------- the prompts --

_PLANNER_SYSTEM = (
    "You are planning a piece of writing that another writer will produce "
    "from your plan. Do not write the piece. Return a short plan: the "
    "sections in the order the request gives them, and for each one a line "
    "naming what it must contain and which of tables, code blocks, numbered "
    "steps, warnings, notes or recommendations belong in it. Name nothing "
    "the request did not ask for."
)

_CRITIC_SYSTEM = (
    "You are checking a finished draft against a list of requirements. You "
    "are not rewriting it and not praising it. For each requirement say "
    "whether the draft meets it, and QUOTE the words of the draft that "
    "decide it — the quote must be copied from the draft exactly. A "
    "requirement you cannot find evidence for in the draft is missing. "
    "Respond with JSON only."
)

_REVISER_SYSTEM = (
    "You are completing a document that is already written and already shown "
    "to the reader. Everything above is final and must not be repeated, "
    "restated or summarised. Write ONLY the material the requirements below "
    "say is still missing, as a continuation in the same Markdown style and "
    "the same voice, starting at a heading. Any section you add carries at "
    "least two full paragraphs of its own body, like every other section. If "
    "nothing is genuinely missing, answer with the single word NOTHING."
)

_CRITIQUE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "findings": {
            "type": "array",
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "requirement": {"type": "string", "maxLength": 120},
                    "verdict": {"type": "string", "enum": ["met", "missing"]},
                    "evidence": {"type": "string", "maxLength": 300},
                    "fix": {"type": "string", "maxLength": 200},
                },
                "required": ["requirement", "verdict", "evidence"],
            },
        }
    },
    "required": ["findings"],
}


# --------------------------------------------------------------- the steps --


class _Steps:
    """The timeline. One `emit("step", …)` frame per transition, ids counted
    from 1, and an open step ALWAYS closed — deep_research.py leaves its
    open step in a variable for exactly this reason, because a phase that
    raises otherwise leaves a spinner running in the UI forever."""

    def __init__(self, emit: Emit) -> None:
        self._emit = emit
        self._next = 0
        self._open: Optional[tuple] = None
        self.finished: List[dict] = []

    async def open(self, title: str) -> int:
        await self.close_open("interrupted")
        self._next += 1
        self._open = (self._next, title)
        await self._emit("step", {"id": self._next, "title": title, "status": "running"})
        return self._next

    async def done(self, detail: str = "") -> None:
        await self._close("done", detail)

    async def failed(self, detail: str = "") -> None:
        await self._close("failed", detail)

    async def close_open(self, detail: str = "") -> None:
        if self._open is not None:
            await self._close("failed", detail)

    async def _close(self, status: str, detail: str) -> None:
        if self._open is None:
            return
        step_id, title = self._open
        self._open = None
        frame = {"id": step_id, "title": title, "status": status}
        if detail:
            frame["detail"] = detail[:200]
        self.finished.append(dict(frame))
        await self._emit("step", frame)


# ---------------------------------------------------------------- the loop --


@dataclass
class LoopResult:
    text: str = ""
    plan: str = ""
    report: Optional[C.Report] = None
    revised: bool = False
    refused: str = ""
    steps: List[dict] = field(default_factory=list)
    model_calls: int = 0
    #: The draft's continuation shape. The loop branch emits its own meta,
    #: so without this a draft that ran out of budget would go unreported
    #: on a Max turn while the ordinary path reports it.
    draft: Optional[dict] = None

    def as_meta(self) -> dict:
        meta: Dict[str, Any] = {"phases": self.model_calls, "revised": self.revised}
        if self.report is not None:
            meta["unmet"] = len(self.report.failed_musts())
        if self.refused:
            meta["revision_refused"] = self.refused
        if self.draft is not None:
            meta["draft"] = self.draft
        return meta


async def run(
    message: str,
    history: Sequence[dict],
    messages: Sequence[dict],
    emit: Emit,
    *,
    mode: str = "assistant",
    model_choice: str = "smart",
    grounding: str = "",
    contract: Optional[C.Contract] = None,
    on_delta: Optional[OnDelta] = None,
    temperature: float = 0.3,
    segment_max_tokens: Optional[int] = None,
    total_max_tokens: Optional[int] = None,
    deadline_s: Optional[float] = None,
    target_words: Optional[int] = None,
    meta: Optional[dict] = None,
) -> str:
    """One Max turn as five visible phases. Returns the answer's full text.

    `history` is carried because the design names it; the prompt it would
    build is already in `messages`, which is why this module does not
    rebuild one.

    `messages` is the prompt engines/chat.py already assembled for this turn
    (persona, identity, grounding, fenced history, the person's message);
    this module never rebuilds it, which is what keeps the context-assembly
    goldens out of its way. `on_delta` is that engine's own delta sink — the
    loop guard and the rewrite shaper live there and cover the draft and the
    revision alike — and everything forwarded through it is teed here, so
    the check reads exactly the text that was sent.
    """
    from .. import continuation
    from ..continuity import LeaseLost, QueuedForRecovery

    steps = _Steps(emit)
    result = LoopResult()
    pieces: List[str] = []
    sink = _tee(on_delta or _sink(emit), pieces)
    brief = C.requirements_brief(contract) if contract is not None else ""

    try:
        # 1 --------------------------------------------------------- PLAN --
        await steps.open(STEP_PLAN)
        try:
            result.plan = await _plan(message, grounding, mode, brief)
            result.model_calls += 1
            await steps.done(_plan_detail(contract, result.plan))
        except (QueuedForRecovery, LeaseLost):
            # Not a failed plan: the turn is parked for the main model, or
            # its row was taken over by another resumer. Nothing may stand
            # in, and the draft would only park again (best_of.py §8.3).
            raise
        except Exception as exc:  # noqa: BLE001 — a plan is an optimisation
            log.info("max loop: plan unavailable (%s)", type(exc).__name__)
            result.plan = ""
            await steps.failed("the plan could not be written; writing directly")

        # 2 -------------------------------------------------------- DRAFT --
        await steps.open(STEP_DRAFT)
        long = await continuation.stream_long_completion(
            _with_plan(messages, result.plan, brief),
            on_delta=sink,
            model_choice=model_choice,
            effort="max",
            temperature=temperature,
            segment_max_tokens=segment_max_tokens,
            total_max_tokens=total_max_tokens,
            deadline_s=deadline_s,
            target_words=target_words,
        )
        result.model_calls += max(1, int(getattr(long, "segment_count", 1) or 1))
        if getattr(long, "segment_count", 1) > 1 or getattr(long, "truncated", False):
            result.draft = long.as_meta()
        result.text = "".join(pieces)
        await steps.done(f"{len(result.text.split()):,} words")

        # 3 -------------------------------------------------------- CHECK --
        await steps.open(STEP_CHECK)
        report = C.check(contract, result.text) if contract is not None else None
        result.report = report
        await steps.done(report.detail() if report is not None else "no requirements to check")
        if report is None:
            return result.text

        # 4 ----------------------------------------------------- CRITIQUE --
        findings: List[dict] = []
        undecided = report.undecided()
        if undecided:
            await steps.open(STEP_CRITIQUE)
            try:
                findings = await _critique(result.text, undecided)
                result.model_calls += 1
                missing = [f for f in findings if f["verdict"] == "missing"]
                await steps.done(
                    f"{len(missing)} of {len(undecided)} open point"
                    f"{'s' if len(undecided) != 1 else ''} still missing"
                )
            except (QueuedForRecovery, LeaseLost):
                raise
            except Exception as exc:  # noqa: BLE001 — the counted check stands
                log.info("max loop: critique unavailable (%s)", type(exc).__name__)
                await steps.failed("the review could not be completed")

        # 5 ------------------------------------------------------- REVISE --
        unmet = _unmet(report, findings)
        if not unmet:
            return result.text
        await steps.open(STEP_REVISE)
        try:
            addition = await _revise(
                result.text, unmet, continuation,
                model_choice=model_choice, temperature=temperature,
                segment_max_tokens=segment_max_tokens,
                total_max_tokens=total_max_tokens, deadline_s=deadline_s,
            )
        except (QueuedForRecovery, LeaseLost):
            raise
        except Exception as exc:  # noqa: BLE001 — the draft is already the answer
            log.info("max loop: revision unavailable (%s)", type(exc).__name__)
            await steps.failed("the revision could not be written; the answer stands as drafted")
            return result.text
        result.model_calls += 1
        # THE REVISION IS BUFFERED, NOT STREAMED, and that is the guard's
        # whole point: nothing can un-send a delta, so a pass that may be
        # REFUSED has to be judged before any of it reaches the person.
        candidate = result.text + addition
        refusal = _worse(result.text, candidate) or _thin(addition)
        if refusal:
            result.refused = refusal
            await steps.failed(f"kept the draft: {refusal}")
            return result.text
        # THE GUARD CAN FIRE ON THE REVISION'S OWN TEXT, AND IT MUST NOT COST
        # THE PERSON THEIR ANSWER. `sink` is the engine's delta sink, and it
        # raises continuation.StopGeneration when the loop guard's verdict
        # lands (engines/chat._loop_out). On every other path that exception
        # is raised INSIDE stream_long_completion, which catches it and ends
        # the stream cleanly. Here the revision is buffered and the sink is
        # called directly, so nothing was catching it: it escaped `run`,
        # escaped run_chat_engine, and reached main.py's terminal error
        # handler, which turned a finished answer into a failed generation and
        # an `error` frame. Measured on this branch before this block, with a
        # revision that repeats one sentence forty times.
        #
        # The draft is already the answer. What the guard held back is held
        # back, the step says so, and the engine's own post-loop path still
        # reports the verdict on the meta.
        try:
            for start in range(0, len(addition), 400):
                await sink("token", addition[start : start + 400])
        except continuation.StopGeneration:
            result.text = "".join(pieces)
            await steps.failed("the revision began repeating itself; it stops there")
            return result.text
        result.text = "".join(pieces)
        result.revised = True
        await steps.done(
            f"added {len(addition.split()):,} words for {len(unmet)} "
            f"requirement{'s' if len(unmet) != 1 else ''}"
        )
        return result.text
    finally:
        await steps.close_open("the turn ended before this step finished")
        result.steps = steps.finished
        if meta is not None:
            meta["steps"] = list(steps.finished)
            meta["max_loop"] = result.as_meta()


def _sink(emit: Emit) -> OnDelta:
    """The delta sink when the caller supplies none — plain pass-through."""

    async def _out(kind: str, text: str) -> None:
        await emit("reasoning" if kind == "reasoning" else "token", {"text": text})

    return _out


def _plan_detail(contract: Optional[C.Contract], plan: str) -> str:
    if contract is not None and contract.sections:
        return f"{len(contract.sections)} sections to cover"
    return f"{len(plan.split()):,} words of plan" if plan else "no plan"


def _with_plan(messages: Sequence[dict], plan: str, brief: str = "") -> List[dict]:
    """The turn's prompt with the counted brief and the plan as one system
    block.

    `llm.normalize_system` folds every system block into one at index 0 in
    order, so the engine's persona still leads and this reads as later
    context — the same shape compaction and search already use. The
    person's own message stays last.

    THE BRIEF GOES IN WHETHER OR NOT THE PLAN CALL WORKED. It is code's
    reading of the person's own request, and it is the half of this loop
    that closes the original defect; the plan is the model's reading and is
    an optimisation on top of it.
    """
    parts = []
    if brief.strip():
        parts.append(brief.strip())
    if plan.strip():
        parts.append(
            "YOUR PLAN FOR THIS ANSWER (you wrote it a moment ago; follow it "
            "and do not mention it):\n" + plan.strip()
        )
    if not parts:
        return list(messages)
    block = {"role": "system", "content": "\n\n".join(parts)}
    body = list(messages)
    for index in range(len(body) - 1, -1, -1):
        if body[index].get("role") == "user":
            return body[:index] + [block] + body[index:]
    return body + [block]


async def _plan(message: str, grounding: str, mode: str, brief: str = "") -> str:
    """One call, THINKING OFF, PLAN_MAX_TOKENS of answer.

    It always runs on the main model: engines/chat.py gates this whole
    branch on `model_choice == "smart"` and llm.chat_completion sends to
    settings.llm_model, so there is no model to choose here.

    THE DESIGN SAID THINKING ON. Measured on the owner's own prompt, live,
    GPU idle before each call, same messages, same 700-token answer ceiling:

      thinking ON  (effort=max)   71.4 s, 4,046 words of reasoning,
                                  7,317 completion tokens, a 511-word plan
                                  that opened with a "Writer Directive"
                                  preamble instead of the sections
      thinking OFF                 6.9 s, 700 completion tokens, a 421-word
                                  plan that enumerated all fifteen sections
                                  with the elements each one needs

    Ten times the wall clock for a plan that was not better, and the whole
    71 seconds lands BEFORE the draft's first token — it is the difference
    between an answer that starts at about second 7 and one that starts at
    two minutes. It is the critic's lesson again: reasoning earns its cost
    on a judgement, not on restating a structure that code has already
    extracted. The contract did the extraction; this call only arranges it.
    """
    from .. import llm

    lines = [message.strip()[: C.PROPOSER_INPUT_CHARS * 4]]
    if brief:
        lines.append("\n" + brief)
    if mode != "assistant":
        lines.append("\nThis organisation's Salesforce data is available to the writer.")
    if grounding.strip():
        lines.append("\nEvidence the writer already holds:\n" + grounding.strip()[:4000])
    text = await llm.chat_completion(
        [{"role": "system", "content": _PLANNER_SYSTEM}, {"role": "user", "content": "\n".join(lines)}],
        temperature=0.2,
        max_tokens=PLAN_MAX_TOKENS,
        thinking=False,
    )
    return (text or "").strip()


def _clip(text: str) -> str:
    if len(text) <= CRITIC_DRAFT_CHARS:
        return text
    return text[:CRITIC_DRAFT_CHARS] + "\n\n[the draft continues beyond what you were shown]"


async def _critique(draft: str, undecided: Sequence[C.ItemResult]) -> List[dict]:
    """One call, thinking FORCED OFF, over the draft and the open points.

    See the module docstring for the two measurements that decided the flag:
    thinking off, 9.36 s and four true defects; thinking on, TTFT 36.4 s and
    a verdict cut off mid-sentence at its cap. There is no setting.

    Every finding must quote the draft. A finding whose evidence string is
    not in the draft is DROPPED rather than acted on — it is cheaper for a
    model to restate a checklist than to read a draft, and a confabulated
    agreement drives a revision that fixes nothing.
    """
    from .. import llm

    items = C.fenced_items([_as_item(r) for r in undecided])
    raw = await llm.json_completion(
        [
            {"role": "system", "content": _CRITIC_SYSTEM},
            {"role": "user", "content": f"REQUIREMENTS STILL OPEN:\n{items}\n\nDRAFT:\n{_clip(draft)}"},
        ],
        json_schema=_CRITIQUE_SCHEMA,
        schema_name="critique",
        temperature=0.0,
        max_tokens=CRITIQUE_MAX_TOKENS,
        thinking=False,
    )
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return []
    obj = json.loads(raw[start : end + 1])
    return _keep_evidenced(obj.get("findings") or [], draft)


def _as_item(result: C.ItemResult) -> C.ContractItem:
    """An ItemResult as the item the fence will carry: its label, nothing
    else. The critic never receives an item's surrounding free text."""
    return C.ContractItem(result.item_id, "free", result.label, True, must=result.must, source="model")


def _normalise(text: str) -> str:
    return " ".join((text or "").split()).lower()


def _keep_evidenced(findings: Sequence[Any], draft: str) -> List[dict]:
    """Findings whose evidence really is in the draft, normalised for
    whitespace. A "missing" verdict quotes the place the gap is; a "met"
    verdict quotes what meets it. Either way the quote has to exist."""
    body = _normalise(draft)
    out: List[dict] = []
    for raw in findings:
        if not isinstance(raw, dict):
            continue
        verdict = str(raw.get("verdict") or "").lower()
        if verdict not in ("met", "missing"):
            continue
        evidence = _normalise(str(raw.get("evidence") or ""))
        if not evidence or evidence not in body:
            log.info("max loop: dropped a critique finding whose evidence is not in the draft")
            continue
        out.append({
            "requirement": C.sanitise_phrase(raw.get("requirement")),
            "verdict": verdict,
            "evidence": str(raw.get("evidence") or "")[:300],
            "fix": C.sanitise_phrase(raw.get("fix")),
        })
    return out


#: What an APPEND can honestly fix. The revision writes new material at the
#: end of a document that has already been shown, so it can add a section
#: that is missing and it can add an element the answer carries NONE of. It
#: cannot put a subheading inside section 7, and it cannot thicken section 4
#: — chasing those produces an appendix that scores nothing and drags the
#: per-section average down, which is exactly what the first live run of
#: this loop did (it appended a sixteenth section of bulleted warnings).
#: Those gaps are REPORTED in the step detail and left to the next draft.
_APPENDABLE_ELEMENTS = frozenset(
    {"table", "code_block", "numbered_list", "bullet_list", "warning", "note", "recommendation"}
)


def _unmet(report: C.Report, findings: Sequence[dict]) -> List[str]:
    """What the revision is for: the musts a counter found unmet AND that an
    append can fix, plus the open points the critic confirmed missing.

    `unsupported` never appears — the medium cannot carry it, and revising
    forever would not change that.
    """
    out: List[str] = []
    for result in report.failed_musts():
        if _is_appendable(result, report):
            out.append(result.label)
    for finding in findings:
        if finding["verdict"] == "missing" and finding["requirement"]:
            out.append(finding["requirement"])
    seen: set = set()
    unique: List[str] = []
    for label in out:
        key = label.lower()
        if key not in seen:
            seen.add(key)
            unique.append(label)
    return unique


def _is_appendable(result: C.ItemResult, report: C.Report) -> bool:
    if result.label.startswith("a section on"):
        return True
    for target in _APPENDABLE_ELEMENTS:
        if C._ELEMENT_LABELS[target] in result.label:
            # Short of the floor but present: the answer already uses the
            # element, and one more of it bolted on the end is padding.
            return report.observed.count_of(target) == 0
    return False


async def _revise(
    draft: str,
    unmet: Sequence[str],
    continuation: Any,
    *,
    model_choice: str,
    temperature: float,
    segment_max_tokens: Optional[int],
    total_max_tokens: Optional[int],
    deadline_s: Optional[float],
) -> str:
    """One call: the material the check found missing, as a continuation of
    the draft. BUFFERED, never streamed — see the module docstring. Nothing
    can un-send a delta, so a pass the guard may refuse must be complete
    before any of it reaches the person."""
    items = C.fenced_items(
        [C.ContractItem("", "free", label, True, must=True, source="rule") for label in unmet]
    )
    pieces: List[str] = []

    async def _collect(kind: str, text: str) -> None:
        if kind != "reasoning":
            pieces.append(text)

    await continuation.stream_long_completion(
        [
            {"role": "system", "content": _REVISER_SYSTEM},
            {"role": "user", "content": f"STILL MISSING:\n{items}\n\nTHE DOCUMENT SO FAR:\n{_clip(draft)}"},
        ],
        on_delta=_collect,
        model_choice=model_choice,
        effort="max",
        temperature=temperature,
        segment_max_tokens=segment_max_tokens,
        total_max_tokens=total_max_tokens,
        deadline_s=deadline_s,
    )
    text = "".join(pieces).strip()
    if _normalise(text) in ("", "nothing"):
        return ""
    return "\n\n" + text


def _tee(sink: OnDelta, pieces: List[str]) -> OnDelta:
    """The caller's delta sink, with every ANSWER delta recorded. The check
    then reads exactly the text that was sent, not a second reconstruction
    of it."""

    async def _out(kind: str, text: str) -> None:
        if kind != "reasoning":
            pieces.append(text)
        await sink(kind, text)

    return _out


def _thin(addition: str) -> str:
    """The refusal reason when a revision is too small to be worth showing,
    and the empty string when it is substantial enough."""
    return "" if len(addition.split()) >= MIN_REVISION_WORDS else "the revision added nothing"


def _worse(before: str, after: str) -> str:
    """Why `after` is a worse answer than `before`, or "".

    compose.py's `_worse` / CORRECTION_KEEP_FRACTION judgement, ported to
    plain text: a correction that dropped most of the content, introduced
    placeholders, or emptied the answer is refused and the draft stands.
    """
    if not after.strip():
        return "emptied the answer"
    before_words, after_words = len(before.split()), len(after.split())
    if before_words >= 40 and after_words < before_words * CORRECTION_KEEP_FRACTION:
        return "dropped most of the answer"
    if _PLACEHOLDER_RE.search(after) and not _PLACEHOLDER_RE.search(before):
        return "replaced content with placeholders"
    return ""


__all__ = ["run", "wants_loop", "LoopResult", "PLAN_MAX_TOKENS", "CRITIQUE_MAX_TOKENS"]
