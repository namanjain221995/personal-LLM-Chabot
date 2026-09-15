"""The one classifier call the intent gate may make — context-aware, bounded,
and told what NOT to call a file.

    rules: none + a file word + no negative shape
        -> classify(text, last_answer_head=…, has_artifacts=…, upload_names=…)
        -> IntentVerdict{action, formats, target, style_request, chart_request, confidence}

WHY CONTEXT. The pre-AS3 classifier saw the message alone. "give it in docs"
means nothing without the audit answer above it, and "make it a docx" means
something else right after a file card. The prompt carries at most 500
characters of the substantial answer, whether the last turn was a file card,
the titles of the conversation's files and the names of the attached files.

WHY NEGATIVE EXAMPLES. Measured on 56 rule misses the old text-only prompt
said `wants_file` 56 times out of 56: a yes-bias. Six authored negatives
(upload QA, how-to, praise, code) sit in the prompt, the acceptance threshold
is 0.75, and a create/export verdict on a message the lexicon's negative
pass flags is refused whatever its confidence.

BOUNDS. Thinking off, 220 output tokens, 2.5 s at Fast and 5 s otherwise
(`asyncio.timeout`, not `wait_for`: CI Python 3.11 swallows a same-pass
cancel inside `wait_for`). Skipped — the rules' answer stands — when the
feature flag is off or the engine is saturated with live chat generations.
Every outcome is counted in artifact_intent_llm_total{result}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .. import metrics
from ..config import settings
from . import lexicon as LX

log = logging.getLogger(__name__)

ACTIONS = ("create", "export", "convert", "edit", "none")
TARGETS = ("previous_answer", "upload", "artifact", "conversation", "none")
FORMATS = ("pdf", "docx", "xlsx", "csv", "pptx", "png", "svg")
#: A verdict below this is the rules' answer.
ACCEPT_CONFIDENCE = 0.75
MAX_TOKENS = 220
#: Live chat generations (not waiting on a job) at or above which the
#: classifier is skipped: the person is better served by the rules' answer
#: now than by a queued classification.
SATURATED_GENERATIONS = 6
_HEAD_CHARS = 500


@dataclass
class IntentVerdict:
    action: str = "none"
    formats: List[str] = field(default_factory=list)
    target: str = "none"
    style_request: bool = False
    chart_request: bool = False
    confidence: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "formats": {"type": "array", "maxItems": 5, "items": {"type": "string", "enum": list(FORMATS)}},
        "target": {"type": "string", "enum": list(TARGETS)},
        "style_request": {"type": "boolean"},
        "chart_request": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    },
    "required": ["action", "formats", "target", "style_request", "chart_request", "confidence"],
}

_SYSTEM = (
    "You label ONE chat message for a workspace assistant that can create downloadable files (Word DOCX, PDF, Excel XLSX, "
    "CSV, PowerPoint PPTX, PNG/SVG charts) and edit files it already made. Decide whether the person is asking the "
    "assistant to PRODUCE or CHANGE a file now. Messages may be in English, Hindi, Gujarati, Hinglish or Gujlish, with typos "
    "(\"dox\" = docx, \"exel\" = excel, \"pdf bana do\" = make a pdf).\n"
    "action: create (a new file on a topic or from an attached file), export (the assistant's previous answer as a file), "
    "convert (an existing file of this conversation in another format), edit (change an existing file: content, colours, "
    "fonts, orientation, columns, undo), none (anything else).\n"
    "A file format named as the SOURCE is not a request: questions about an attached file, how-to questions, format "
    "comparisons, praise or complaints about a file, and requests for code or formulas are all action none. When unsure, "
    "answer none with low confidence.\n"
    "Examples:\n"
    "\"summarize this pdf\" (a PDF is attached) -> none\n"
    "\"how do I convert word to pdf on windows\" -> none\n"
    "\"excel mein vlookup kaise lagate hain\" -> none\n"
    "\"the report looks great, thanks\" -> none\n"
    "\"write python code that creates a docx\" -> none\n"
    "\"what does the excel say about march\" (an Excel file is attached) -> none\n"
    "\"give it in docs, a classy format\" (after a long answer) -> export, formats [docx], target previous_answer\n"
    "\"make the headings dark blue\" (a file exists) -> edit, style_request true, target artifact\n"
    "Answer with JSON only."
)

#: Metric label values are a closed set per metric (app/metrics.py maps
#: anything else to "other"); the AS3 metrics declare theirs here, beside
#: the code that emits them.
_METRIC_LABELS = {
    "artifact_intent_llm_total": {"result": {"accepted", "rejected_low_conf", "rejected_negative", "said_none", "timeout", "error",
                                             "skipped_busy", "skipped_disabled"}},
    "artifact_intent_llm_seconds": {"effort": {"fast", "think", "max", ""}},
    "artifact_denial_rerouted_total": {"engine": {"chat", "agent", "dataset", "vision"}},
    "artifact_denial_seen_total": {"engine": {"chat", "agent", "dataset", "vision"}},
    "artifact_sf_fallthrough_total": {"route": {"chat", "clarify"}},
}
for _name, _labels in _METRIC_LABELS.items():
    metrics._ALLOWED_BY_METRIC.setdefault(_name, {}).update(_labels)  # noqa: SLF001 — declared next to the emitter

#: Tests inject a probe; production reads main's live generations lazily.
_saturation_probe: Optional[Callable[[], bool]] = None


def set_saturation_probe(fn: Optional[Callable[[], bool]]) -> None:
    global _saturation_probe
    _saturation_probe = fn


def _saturated() -> bool:
    if _saturation_probe is not None:
        try:
            return bool(_saturation_probe())
        except Exception:  # noqa: BLE001 — advisory
            return False
    main = sys.modules.get(__name__.rsplit(".", 2)[0] + ".main")
    live = getattr(main, "_live_generations", None)
    if not isinstance(live, dict):
        return False
    try:
        busy = sum(1 for g in list(live.values())
                   if not getattr(g, "done", False) and not getattr(g, "waiting_on_job", False) and not getattr(g, "waiting_on_video", False))
    except Exception:  # noqa: BLE001
        return False
    return busy >= SATURATED_GENERATIONS


def timeout_for(effort: str) -> float:
    if (effort or "fast") == "fast":
        return float(settings.artifact_intent_llm_timeout_fast_s)
    return float(settings.artifact_intent_llm_timeout_s)


def build_messages(text: str, *, last_answer_head: str = "", last_turn_is_artifact: bool = False, has_artifacts: bool = False,
                   artifact_titles: Sequence[str] = (), upload_names: Sequence[str] = ()) -> List[dict]:
    ctx: List[str] = []
    if last_turn_is_artifact:
        ctx.append("The assistant's last turn was a FILE CARD (a file was just made).")
    elif last_answer_head:
        ctx.append("The assistant's previous answer begins:\n<<<\n" + last_answer_head[:_HEAD_CHARS] + "\n>>>")
    else:
        ctx.append("There is no earlier assistant answer worth exporting.")
    titles = [str(t)[:80] for t in artifact_titles if t][:6]
    if has_artifacts and titles:
        ctx.append("Files already made in this conversation: " + "; ".join(titles))
    elif has_artifacts:
        ctx.append("Files already exist in this conversation.")
    names = [str(n)[:80] for n in upload_names if n][:6]
    if names:
        ctx.append("Files attached to THIS message: " + "; ".join(names))
    user = "\n".join(ctx) + "\n\nMessage:\n<<<\n" + (text or "")[:2000] + "\n>>>"
    return [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}]


def parse_verdict(raw: Any) -> Optional[IntentVerdict]:
    """A model reply (dict or JSON text) → a validated verdict, or None."""
    obj = raw
    if isinstance(raw, str):
        try:
            from ..core.sf_intel.planner import extract_json_object  # the extractor compose.py uses
        except Exception:  # noqa: BLE001
            extract_json_object = None  # type: ignore[assignment]
        try:
            obj = extract_json_object(raw) if extract_json_object else json.loads(raw)
        except Exception:  # noqa: BLE001
            obj = None
    if not isinstance(obj, dict):
        return None
    action = str(obj.get("action") or "none")
    if action not in ACTIONS:
        return None
    try:
        confidence = float(obj.get("confidence", 0.0))
    except (TypeError, ValueError):
        return None
    if not 0.0 <= confidence <= 1.0:
        return None
    formats = [str(f) for f in (obj.get("formats") or []) if str(f) in FORMATS][:5]
    target = str(obj.get("target") or "none")
    return IntentVerdict(action=action, formats=list(dict.fromkeys(formats)), target=target if target in TARGETS else "none",
                         style_request=bool(obj.get("style_request")), chart_request=bool(obj.get("chart_request")),
                         confidence=confidence)


def _upload_formats(names: Sequence[str]) -> List[str]:
    out: List[str] = []
    for n in names:
        ext = str(n).rsplit(".", 1)[-1].lower() if "." in str(n) else ""
        fmt = {"xls": "xlsx", "doc": "docx", "markdown": "md"}.get(ext, ext)
        if fmt and fmt not in out:
            out.append(fmt)
    return out


async def classify(
    text: str,
    *,
    last_answer_head: str = "",
    last_turn_is_artifact: bool = False,
    has_artifacts: bool = False,
    artifact_titles: Sequence[str] = (),
    upload_names: Sequence[str] = (),
    upload_formats: Sequence[str] = (),
    effort: str = "fast",
    completion: Optional[Callable[..., Any]] = None,
) -> Optional[IntentVerdict]:
    """ONE strict-JSON call. Returns the ACCEPTED verdict (confidence ≥ 0.75
    and not a create/export on a negative-shaped message), else None. Never
    raises. `completion` replaces llm.json_completion in tests."""
    if not getattr(settings, "artifact_intent_llm_enabled", True):
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="skipped_disabled")
        return None
    if _saturated():
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="skipped_busy")
        return None
    messages = build_messages(text, last_answer_head=last_answer_head, last_turn_is_artifact=last_turn_is_artifact,
                              has_artifacts=has_artifacts, artifact_titles=artifact_titles, upload_names=upload_names)
    if completion is None:
        from .. import llm

        completion = llm.json_completion
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout_for(effort)):
            raw = await completion(messages, json_schema=SCHEMA, schema_name="artifact_intent", temperature=0.0,
                                   max_tokens=MAX_TOKENS, thinking=False)
    except TimeoutError:
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="timeout")
        return None
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — an outage is the rules' answer
        log.info("artifact intent classifier failed: %s", type(exc).__name__)
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="error")
        return None
    finally:
        metrics.observe("artifact_intent_llm_seconds", time.perf_counter() - started, "intent-gate classifier latency", effort=str(effort or ""))
    verdict = parse_verdict(raw)
    if verdict is None:
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="error")
        return None
    if verdict.action == "none":
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="said_none")
        return None
    if verdict.confidence < ACCEPT_CONFIDENCE:
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="rejected_low_conf")
        return None
    formats = list(upload_formats or ()) or _upload_formats(upload_names)
    if verdict.action in ("create", "export") and LX.negative_shape(text or "", formats) is not None:
        metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="rejected_negative")
        return None
    metrics.inc("artifact_intent_llm_total", "intent-gate classifier outcomes", result="accepted")
    return verdict


def make_hook(*, last_answer_head: str = "", last_turn_is_artifact: bool = False, has_artifacts: bool = False,
              artifact_titles: Sequence[str] = (), upload_names: Sequence[str] = (), upload_formats: Sequence[str] = (),
              effort: str = "fast"):
    """A hook for intent.decide_with_hook that carries this turn's context."""

    async def hook(text: str, **_kw: Any) -> Optional[IntentVerdict]:
        return await classify(text, last_answer_head=last_answer_head, last_turn_is_artifact=last_turn_is_artifact,
                              has_artifacts=has_artifacts, artifact_titles=artifact_titles, upload_names=upload_names,
                              upload_formats=upload_formats, effort=effort)

    return hook


__all__ = ["IntentVerdict", "SCHEMA", "ACCEPT_CONFIDENCE", "classify", "make_hook", "parse_verdict", "build_messages",
           "set_saturation_probe", "timeout_for"]
