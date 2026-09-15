"""Per-request sampling and Fast length caps for chat answers.

Pure policy: no llm import, no database, no network. The environment is read
once, at import, with safe parsing — a value this module does not recognise
falls back to today's behaviour and is logged, never raised.

WHAT THE ENGINE DOES WITHOUT US. The main model is served by vLLM with no
`--generation-config` flag, so vLLM applies the checkpoint's
generation_config.json to every field a request leaves out. The head's startup
log says so ("Default vLLM sampling parameters have been overridden by the
model's `generation_config.json`: {'temperature': 1.0, 'top_k': 20,
'top_p': 0.95}"). Fast has always sent `temperature` alone, so what it really
samples with is temperature 0.6, top_p 0.95, top_k 20, min_p 0 and no
presence penalty. That is the LEGACY profile, and it stays the default.

WHY LEGACY STAYS (answer-quality design, 2026-09-14, critic verdict b). The
paired eval of the Qwen3.6 instruct profile showed no gain beyond noise (8/10
vs 7/10 on one sample), its presence_penalty 1.5 arm looped on a prompt the
baseline did not, and it never saw code, markdown tables, mermaid, JSON or long
Hindi/Gujarati — exactly the output that depends on reusing tokens ('|',
'-->', indentation, braces, JSON keys, script-specific subwords). A presence
penalty charges every token already generated, once, so it pushes against all
of them. The Qwen instruct profile is therefore only reachable through
ANSWER_SAMPLING_PROFILE=qwen_instruct, with presence_penalty 0; the 1.5 penalty
additionally needs ANSWER_PROSE_PRESENCE_PENALTY=1.5 AND a prose-shaped ask AND
no non-Latin script anywhere in the message or recent history. 1.0 is never
used (it is not a value that was evaluated), and no seed is ever sent, so
regenerate keeps resampling.

ROUTED THINKING (a pass the adaptive-thinking policy decides to run) uses the
Qwen thinking-general profile: temperature 1.0, top_p 0.95, top_k 20, min_p 0,
presence_penalty 1.5 — the measured think_b1024 arm; temperature 0.3 left 2 of
3 runs with no answer at all. Its forced-closure ANSWER uses the same profile
with presence_penalty 0 for structured asks: vLLM applies the penalty to
GENERATED tokens only, so reasoning carried in the prompt does not count, but
a table or a diagram written in the answer does.

KEY PLACEMENT (`place_sampling`). temperature, top_p and presence_penalty are
OpenAI parameters and go top-level. top_k, min_p and repetition_penalty are
vLLM extensions and go into `extra_body`, beside chat_template_kwargs — and
only for a backend that accepts vLLM extensions; anywhere else they are
dropped, because an unknown body field is a 400 on a strict server.

FAST LENGTH CAPS (`fast_caps`). One call of 8,000 tokens for every shape; the
logical total is the system output ceiling (MAX_LOGICAL_OUTPUT_TOKENS, 1,000,000)
for every shape, continued across segments until the model says it is done.
Owner decision 2026-09-15: a Fast answer must not stop at 8,000 or 64,000
tokens (a 50-problem coding answer was cut at 8,000). Runaway repetition is
stopped by the answer loop guard (core/answer_guard.py), not by a length cap.
ANSWER_FAST_PROSE_TOTAL_TOKENS=8000 still puts the one-call prose cap back.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, MutableMapping, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

PROFILE_LEGACY = "legacy"
PROFILE_QWEN_INSTRUCT = "qwen_instruct"
PROFILES = (PROFILE_LEGACY, PROFILE_QWEN_INSTRUCT)

SHAPE_PROSE = "prose"
SHAPE_STRUCTURED = "structured"
SHAPE_LONGFORM = "longform"
SHAPES = (SHAPE_PROSE, SHAPE_STRUCTURED, SHAPE_LONGFORM)

#: What engines/chat.py has always sent. Kept here as the one named source so
#: a plan that carries no temperature falls back to exactly today's number.
LEGACY_FAST_TEMPERATURE = 0.6
LEGACY_THINK_TEMPERATURE = 0.3

#: The only presence penalty ever sent. 1.0 is deliberately not accepted.
PROSE_PRESENCE_PENALTY = 1.5

#: Qwen3.6 model card, instruct (thinking-off) mode. presence_penalty is NOT
#: part of it here: see the module docstring.
QWEN_INSTRUCT_SAMPLING: Mapping[str, float] = {
    "temperature": 0.7,
    "top_p": 0.8,
    "top_k": 20,
    "min_p": 0.0,
}

#: Qwen3.6 model card, thinking mode for general tasks (measured: think_b1024).
THINKING_SAMPLING: Mapping[str, float] = {
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": PROSE_PRESENCE_PENALTY,
}

#: Sent as top-level OpenAI parameters.
TOP_LEVEL_KEYS = frozenset({"temperature", "top_p", "presence_penalty"})
#: vLLM sampling extensions: sent inside extra_body.
EXTRA_BODY_KEYS = frozenset({"top_k", "min_p", "repetition_penalty"})
_ALLOWED_KEYS = TOP_LEVEL_KEYS | EXTRA_BODY_KEYS

#: Fast caps (tokens). Segment = one call; total = the logical answer.
FAST_SEGMENT_MAX_TOKENS = 8000


def _system_output_ceiling() -> int:
    """MAX_LOGICAL_OUTPUT_TOKENS as config.py reads it (default 1,000,000)."""
    raw = (os.environ.get("MAX_LOGICAL_OUTPUT_TOKENS") or "").strip()
    try:
        value = int(raw) if raw else 1_000_000
    except ValueError:
        value = 1_000_000
    return value if value > 0 else 1_000_000


FAST_PROSE_TOTAL_TOKENS_DEFAULT = _system_output_ceiling()
FAST_EXTENDED_TOTAL_TOKENS = FAST_PROSE_TOTAL_TOKENS_DEFAULT
#: The smallest prose total above one segment that is accepted. A total a
#: little larger than one call is the pathological value config.py documents
#: (8,192 over an 8,000 call: a notice, no extra answer), so anything between
#: one segment and one segment plus a real second one falls back.
_PROSE_TOTAL_MIN_ABOVE_SEGMENT = 2 * FAST_SEGMENT_MAX_TOKENS


def _env_choice(name: str, allowed: Sequence[str], default: str) -> str:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in allowed:
        return raw
    log.warning("%s=%r is not one of %s; using %r", name, raw, "/".join(allowed), default)
    return default


def _env_presence(name: str) -> float:
    """0.0 unless the variable is exactly the evaluated value 1.5."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if value == PROSE_PRESENCE_PENALTY:
        return PROSE_PRESENCE_PENALTY
    if value != 0.0:
        log.warning("%s=%r ignored: only 0 or %s may be sent", name, raw, PROSE_PRESENCE_PENALTY)
    return 0.0


def _env_prose_total(name: str) -> int:
    """The Fast prose total: the system output ceiling unless set to 8,000 (one
    call) or to a value of at least two segments. The knob lets an operator put
    the one-call prose cap back with an env change and no code change."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return FAST_PROSE_TOTAL_TOKENS_DEFAULT
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if value == FAST_SEGMENT_MAX_TOKENS or value >= _PROSE_TOTAL_MIN_ABOVE_SEGMENT:
        return value
    log.warning(
        "%s=%r ignored: use %d (one call) or at least %d",
        name, raw, FAST_SEGMENT_MAX_TOKENS, _PROSE_TOTAL_MIN_ABOVE_SEGMENT,
    )
    return FAST_PROSE_TOTAL_TOKENS_DEFAULT


#: Read once at import (see module docstring).
SAMPLING_PROFILE: str = _env_choice("ANSWER_SAMPLING_PROFILE", PROFILES, PROFILE_LEGACY)
PROSE_PRESENCE: float = _env_presence("ANSWER_PROSE_PRESENCE_PENALTY")
FAST_PROSE_TOTAL_TOKENS: int = _env_prose_total("ANSWER_FAST_PROSE_TOTAL_TOKENS")


# ---------------------------------------------------------------------------
# Script detection
# ---------------------------------------------------------------------------

#: Anything outside Basic Latin, Latin-1, Latin Extended-A/B, IPA and Latin
#: Extended Additional. Candidates only: a match still has to be a LETTER, so
#: punctuation, arrows, box drawing and emoji never count as a script.
_NON_LATIN_CANDIDATE = re.compile(r"[^\x00-\u02AF\u1E00-\u1EFF]")
#: How much text is scanned. A 1M-token history is not read end to end to
#: decide a sampling knob; the last message and recent turns are what the
#: answer will be written in.
_SCRIPT_SCAN_CHARS = 20_000
_SCRIPT_SCAN_TURNS = 6


def has_non_latin_letters(texts: Iterable[str]) -> bool:
    budget = _SCRIPT_SCAN_CHARS
    for text in texts:
        if budget <= 0:
            break
        chunk = (text or "")[-budget:]
        budget -= len(chunk)
        for match in _NON_LATIN_CANDIDATE.finditer(chunk):
            if match.group(0).isalpha():
                return True
    return False


def _history_texts(history: Optional[Sequence[Mapping]]) -> list:
    out = []
    for turn in reversed(list(history or [])[-_SCRIPT_SCAN_TURNS:]):
        content = turn.get("content") if isinstance(turn, Mapping) else None
        if isinstance(content, str):
            out.append(content)
    return out


# ---------------------------------------------------------------------------
# Shape (length caps and the presence-penalty gate)
# ---------------------------------------------------------------------------

_SHAPE_SCAN_CHARS = 4_000
_FENCE = re.compile(r"```|~~~")
_STRUCTURED = re.compile(
    r"\b(?:tables?|tabular|json|csv|tsv|yaml|xml|mermaid|diagrams?|flow\s?charts?|"
    r"sequence\s+diagram|er\s+diagram|gantt|spreadsheet)\b",
    re.IGNORECASE,
)
_LONGFORM = re.compile(
    r"\b(?:write|implement|script|function|class|full|complete|detailed|essay|report|document)\b",
    re.IGNORECASE,
)
_LIST_OF_N = re.compile(r"\b(?:list|table)\s+of\s+(\d{1,7})\b", re.IGNORECASE)
_TRANSLATE = re.compile(r"\btranslat(?:e|ion|ing)\b", re.IGNORECASE)
#: Long-form asks the first lexicon missed (verifier probes, 2026-09-15):
#: "in depth", "comprehensive guide", "a 5000-word story", "draft", and the
#: Hinglish words for essay/write/in detail. `\b` is not used on the Indic
#: words below: Python's `\b` treats Devanagari/Gujarati vowel signs as
#: non-word characters, so it would split those words in the middle.
_LONGFORM_EXTRA = re.compile(
    r"\b(?:in[-\s]depth|comprehensive|exhaustive|thorough(?:ly)?|in\s+detail|step[-\s]by[-\s]step|"
    r"guide|tutorial|chapters?|story|article|draft|nibandh|likh(?:o|iye|ein|en|na)|vistar(?:\s+se)?)\b",
    re.IGNORECASE,
)
_LONGFORM_INDIC = re.compile(
    "निबंध|लेख|विस्तार|विस्तृत|कहानी|लिखि|लिखो|लिखें|"
    "નિબંધ|લેખ|વિગતે|વિગતવાર|વિસ્તાર|વાર્તા|લખો|લખી"
)
#: An explicit size: "2000 words", "३००० शब्दों", "2000 શબ્દોનો", "80 questions".
#: `\d` matches Devanagari and Gujarati digits too, and int() reads them.
_SIZE_WORDS = re.compile(r"(\d{2,7})\s*[-\s]?\s*(?:words?|शब्द|શબ્દ)", re.IGNORECASE)
_SIZE_ITEMS = re.compile(
    r"(\d{2,7})\s+(?:[a-z-]+\s+){0,3}?(?:items|questions|examples|names|ideas|idioms|words|lines|rows|entries|"
    r"points|tips|facts|quotes|sentences|paragraphs|pages|steps|recipes|jokes|prompts|titles)\b",
    re.IGNORECASE,
)
#: Target-language mentions for the presence-penalty gate: a Latin-script ask
#: whose ANSWER is in another script ("translate into Hindi") must not get it.
_TARGET_LANGUAGE = re.compile(
    r"\b(?:translat\w*|hindi|gujarati|marathi|bengali|bangla|tamil|telugu|kannada|malayalam|punjabi|urdu|"
    r"odia|oriya|sanskrit|nepali|arabic|persian|farsi|hebrew|russian|ukrainian|greek|chinese|mandarin|"
    r"cantonese|japanese|korean|thai|devanagari)\b",
    re.IGNORECASE,
)


def shape_for(message: str, *, mode: str = "assistant") -> str:
    """'structured', 'longform' or 'prose' for the length caps and the
    presence-penalty gate. Structured wins over long-form: a 60-row table is
    both, and the structured reading is the conservative one."""
    if mode == "salesforce":
        return SHAPE_STRUCTURED
    text = message or ""
    head = text[-_SHAPE_SCAN_CHARS:]
    if _STRUCTURED.search(head):
        return SHAPE_STRUCTURED
    if _FENCE.search(text[-_SHAPE_SCAN_CHARS * 5:]):
        return SHAPE_LONGFORM
    if _LONGFORM.search(head):
        return SHAPE_LONGFORM
    for match in _LIST_OF_N.finditer(head):
        if int(match.group(1)) >= 50:
            return SHAPE_LONGFORM
    if len(text) > 2000 and _TRANSLATE.search(head):
        return SHAPE_LONGFORM
    if _LONGFORM_EXTRA.search(head) or _LONGFORM_INDIC.search(head):
        return SHAPE_LONGFORM
    for match in _SIZE_WORDS.finditer(head):
        if int(match.group(1)) >= 800:
            return SHAPE_LONGFORM
    for match in _SIZE_ITEMS.finditer(head):
        if int(match.group(1)) >= 50:
            return SHAPE_LONGFORM
    return SHAPE_PROSE


def fast_caps(shape: str, *, dense_script: bool = False) -> Tuple[int, int]:
    """(segment_max_tokens, total_max_tokens) for a Fast answer of this shape.

    `dense_script`: the ask or the conversation is written in a non-Latin
    script. The one-call prose total does not apply there: measured live, a
    Gujarati answer costs ~3.8 tokens a word and Hindi ~2 (English ~1.3), so
    8,000 tokens is only ~2,000 Gujarati words and an ordinary long Gujarati
    answer would be cut at a third of the English length.
    """
    if shape in (SHAPE_STRUCTURED, SHAPE_LONGFORM) or dense_script:
        return FAST_SEGMENT_MAX_TOKENS, max(FAST_EXTENDED_TOTAL_TOKENS, FAST_PROSE_TOTAL_TOKENS)
    return FAST_SEGMENT_MAX_TOKENS, FAST_PROSE_TOTAL_TOKENS


# ---------------------------------------------------------------------------
# Sampling builders
# ---------------------------------------------------------------------------


def thinking_off_sampling(
    *,
    shape: str,
    message: str = "",
    history: Optional[Sequence[Mapping]] = None,
    model_choice: str = "smart",
    profile: Optional[str] = None,
    prose_presence: Optional[float] = None,
) -> dict:
    """The keys to send for a thinking-off Fast answer.

    - model_choice other than 'smart': {} — the caller's temperature is sent
      alone, exactly as today.
    - legacy (default): {'temperature': 0.6}.
    - qwen_instruct: the Qwen instruct profile; presence_penalty 1.5 only for
      prose with no non-Latin letters, and only when enabled.
    """
    profile = SAMPLING_PROFILE if profile is None else profile
    presence = PROSE_PRESENCE if prose_presence is None else prose_presence
    if model_choice != "smart":
        return {}
    if profile != PROFILE_QWEN_INSTRUCT:
        return {"temperature": LEGACY_FAST_TEMPERATURE}
    out = dict(QWEN_INSTRUCT_SAMPLING)
    if (
        presence == PROSE_PRESENCE_PENALTY
        and shape == SHAPE_PROSE
        and not _TARGET_LANGUAGE.search((message or "")[-_SHAPE_SCAN_CHARS:])
        and not has_non_latin_letters([message or "", *_history_texts(history)])
    ):
        out["presence_penalty"] = PROSE_PRESENCE_PENALTY
    return out


def routed_thinking_sampling() -> dict:
    """Sampling for a routed (bounded) thinking pass."""
    return dict(THINKING_SAMPLING)


def closure_sampling(shape: str) -> dict:
    """Sampling for the forced-closure answer after a bounded thinking pass."""
    out = dict(THINKING_SAMPLING)
    if shape == SHAPE_STRUCTURED:
        out.pop("presence_penalty", None)
    return out


def validate_sampling(sampling: Mapping[str, object]) -> dict:
    """A clean copy, or ValueError for a key this layer never sends (a seed,
    frequency_penalty, a typo). Values must be real numbers."""
    out = {}
    for key, value in (sampling or {}).items():
        if key not in _ALLOWED_KEYS:
            raise ValueError(f"sampling key {key!r} is not sent by the answer layer")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"sampling value for {key!r} must be a finite number, got {value!r}")
        out[key] = value
    return out


def place_sampling(
    request: MutableMapping[str, object],
    sampling: Mapping[str, object],
    *,
    vllm_extensions: bool,
) -> None:
    """Write `sampling` into a chat-completions request in place.

    Top-level keys overwrite the request's own (the plan is authoritative for
    the call it was built for). Extension keys are merged into
    request['extra_body'] next to whatever is there (chat_template_kwargs,
    continue_final_message); a request with no extra_body gets one. With
    `vllm_extensions` False they are dropped.
    """
    clean = validate_sampling(sampling)
    extensions = {}
    for key, value in clean.items():
        if key in TOP_LEVEL_KEYS:
            request[key] = value
        else:
            extensions[key] = value
    if extensions and vllm_extensions:
        body = dict(request.get("extra_body") or {})
        body.update(extensions)
        request["extra_body"] = body
    elif extensions:
        log.debug("dropping vLLM sampling extensions %s: backend does not accept them", sorted(extensions))


# ---------------------------------------------------------------------------
# The Fast decision engines/chat.py consumes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FastSampling:
    """The sampling and length slice of an answer plan.

    Duck-typed with the adaptive-thinking AnswerPlan: llm.stream_chat_events
    reads only `.sampling` and `.enable_thinking` from whatever it is given as
    `answer_plan`, so an integrator can pass either object.
    """

    sampling: Mapping[str, float] = field(default_factory=dict)
    enable_thinking: Optional[bool] = None
    shape: str = SHAPE_PROSE
    segment_max_tokens: int = FAST_SEGMENT_MAX_TOKENS
    total_max_tokens: int = FAST_PROSE_TOTAL_TOKENS
    profile: str = PROFILE_LEGACY


def fast_sampling_for(
    message: str,
    history: Optional[Sequence[Mapping]],
    *,
    mode: str,
    effort: str,
    model_choice: str,
    profile: Optional[str] = None,
    prose_presence: Optional[float] = None,
) -> Optional[FastSampling]:
    """The Fast sampling/length decision, or None to keep today's values.

    `effort` must already be normalized ('low' → 'fast'). Only assistant and
    Salesforce chat at Fast get a decision; think/max and every other mode get
    None and are untouched.
    """
    if effort != "fast" or mode not in ("assistant", "salesforce"):
        return None
    shape = shape_for(message, mode=mode)
    dense = has_non_latin_letters([message or "", *_history_texts(history)])
    segment, total = fast_caps(shape, dense_script=dense)
    resolved = SAMPLING_PROFILE if profile is None else profile
    return FastSampling(
        sampling=thinking_off_sampling(
            shape=shape,
            message=message,
            history=history,
            model_choice=model_choice,
            profile=resolved,
            prose_presence=prose_presence,
        ),
        enable_thinking=None,
        shape=shape,
        segment_max_tokens=segment,
        total_max_tokens=total,
        profile=resolved if model_choice == "smart" else PROFILE_LEGACY,
    )
