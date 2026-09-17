"""Vision engine (spec §8, vLLM design).

The MAIN model answers about images on this deployment — it is multimodal and
`VISION_BASE_URL == OPENAI_BASE_URL` (the separate Qwen3-VL router model is
deliberately not used here: measured 2026-08-28 it was slower to the first
token and refused the extraction outright). Images travel as OpenAI
multimodal content: [{"type": "text", ...}, {"type": "image_url",
"image_url": {"url": "data:image/png;base64,<b64>"}}].

Since 2026-08-29 the engine answers the user's question directly, in prose or
Markdown, and emits a fenced JSON block of structured fields ONLY when the
message asks for extraction (see `_SYSTEM` and `extraction_hint`) and the
image is an invoice or a contract. Before that the prompt told the model to
lead with that JSON for every document-ish image, which it also did for
screenshots. The conversation so far is passed through `history_turns`, and
the OCR pre-pass runs at Think/Max only. The final meta is the §10 contract
shape: {"route": "vision"}.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from typing import Awaitable, Callable, List, Optional, Sequence

from .. import llm
from ..config import settings
from . import recent_turns

Emit = Callable[[str, dict], Awaitable[None]]

#: Effort this engine runs at when a caller does not name one. "think" is
#: exactly what this route did before 2026-08-28, so every caller that is not
#: updated (graph.py's `_vision_node`, bare API calls) keeps today's
#: behaviour and nothing regresses.
DEFAULT_EFFORT = "think"

#: Completion ceiling asked of the vision model. Deliberately generous; see
#: `vision_max_tokens` for why frugal is the wrong instinct on this route.
VISION_ANSWER_TOKENS = 8000

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)
_DATA_URL_RE = re.compile(r"^data:image/[\w.+-]+;base64,", re.I)

_SYSTEM = (
    "You are TechSara's visual assistant. The user attached one or more "
    "images; answer their question about them directly, the way an expert "
    "colleague looking at the same screen would.\n"
    "- Lead with the answer, in clear Markdown: short headings, numbered "
    "steps for procedures, tables for tabular data. Do not restate the "
    "question and do not describe the image unless that is what was asked.\n"
    "- Ground every claim in what is visible: quote text, labels, numbers and "
    "UI elements exactly as they appear. Say plainly when something is cut "
    "off, blurry or not visible. Never invent values.\n"
    "- NEVER COMPLETE A NUMBER YOU CANNOT READ — this rule outranks every "
    "other instruction here. Before you write any number, code, extension, "
    "amount, date, serial or identifier, check it character by character in "
    "the image. Write only the characters you can actually make out and put "
    "a question mark where a character is not legible (for example "
    "\"ext. 447?\"), then say plainly which characters are unreadable and "
    "what would make them readable (more light, closer, sharper). A question "
    "mark is never to be replaced by a digit because of context, a similar "
    "document, what such a line usually says, a plausible pattern, or an OCR "
    "transcript. A hedged guess (\"it appears to be 4472\", \"it likely "
    "reads…\") is a WRONG answer, not a careful one: the person will dial it "
    "or pay it. The same holds for a whole line you cannot see: say it is "
    "unreadable rather than what it probably says.\n"
    "- Before claiming a maximum, minimum, \"worst\", \"best\" or \"largest\", "
    "check the claim against every value you read and state the test you "
    "used — a superlative your own listed numbers contradict is an error.\n"
    "- Use the conversation so far and any notes about the user to tailor the "
    "answer: names, repositories, tools, paths and files already mentioned "
    "are context — reuse them verbatim instead of placeholders.\n"
    "- Be practical: when asked how to do something shown on screen, give the "
    "exact clicks or commands for what is on screen.\n"
    "- Structured extraction ONLY on request. If the user asks to extract "
    "data or fields, or asks for JSON, and the image is an invoice or a "
    "contract, start with one fenced ```json block — invoice keys: vendor, "
    "invoice_number, invoice_date, due_date, currency, subtotal, tax, total, "
    "line_items (array of {description, quantity, unit_price, amount}); "
    "contract keys: parties (array), effective_date, end_date, term, "
    "contract_value, governing_law, key_obligations (array); null for "
    "anything not visible — then a short summary. In every other case do "
    "not output JSON."
)
# --- AS3 intent-capability BEGIN --- (the file capability line, engines/capability.py)
from .capability import capability_suffix as _as3_capability_suffix  # noqa: E402

_AS3_CAPABILITY = _as3_capability_suffix()

_SYSTEM = _SYSTEM + _AS3_CAPABILITY
# --- AS3 intent-capability END ---

# Words that mean "give me the data", not "answer my question". Deterministic
# so the JSON-or-prose decision does not rest on the model's mood: the hint
# below is appended to the user text only when one of these is present.
_EXTRACT_RE = re.compile(
    r"\b(extract|json|fields?|line items?|all (the )?data|structured|"
    r"parse|key[- ]?value|table of|invoice (data|details|fields)|"
    r"contract (data|details|terms))\b",
    re.I,
)
_EXTRACT_HINT = (
    "\n\n(If this is an invoice or a contract, lead with the fenced json "
    "block described in your instructions; otherwise answer in Markdown.)"
)


def extraction_hint(message: str) -> str:
    """The extraction hint when the message asks for data, else ''."""
    return _EXTRACT_HINT if _EXTRACT_RE.search(message or "") else ""


def history_turns(history: Sequence[dict], n: Optional[int] = None) -> List[dict]:
    """Text turns (and pinned system blocks) the vision call should carry.

    Until 2026-08-29 the engine sent ``[system, user]`` only, so a follow-up
    about an image — or a question whose answer depends on what the user
    said two turns ago — lost the whole conversation. Multimodal entries
    (list content) are dropped: re-sending earlier images would multiply the
    prompt for no gain, and main.py stores answers as plain text anyway.
    """
    turns = recent_turns(history, n or settings.chat_history_turns)
    return [m for m in turns if isinstance(m.get("content"), str) and m.get("role")]


def to_data_url(image_base64: str) -> str:
    """Return a data: URL for the image; frontend may send raw base64 or a
    full data URL — both are accepted."""
    raw = image_base64.strip()
    if _DATA_URL_RE.match(raw):
        return raw
    return f"data:image/png;base64,{raw}"


def build_user_content(
    message: str, images: "str | Sequence[str]"
) -> List[dict]:
    """OpenAI multimodal content parts: text + one image_url per image
    (2026-08-05: the composer sends up to 5). A bare string is accepted for
    the single-image callers that predate the list form."""
    imgs = [images] if isinstance(images, str) else list(images)
    return [
        {
            "type": "text",
            "text": message
            or (
                "Describe this image."
                if len(imgs) <= 1
                else "Describe these images."
            ),
        },
        *(
            {"type": "image_url", "image_url": {"url": to_data_url(i)}}
            for i in imgs
        ),
    ]


def extract_json_block(text: str) -> Optional[dict]:
    m = _JSON_BLOCK_RE.search(text or "")
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def vision_max_tokens(max_tokens: Optional[int] = None) -> int:
    """Completion ceiling for one vision call, clamped to what the vision
    endpoint says it will serve (`VISION_OUTPUT_LIMIT`, 8192 here).

    WHY THE BUDGET MATTERS MORE HERE THAN ANYWHERE ELSE: reasoning and answer
    are drawn from ONE `max_tokens` pool, and an image prompt makes the model
    think long before it writes anything. Measured 2026-08-28 on three
    1280x800 screenshots: with thinking ON and max_tokens=700 the stream
    produced ZERO content chunks — 26-28s of reasoning, whole budget gone, no
    answer at all. The same three images with thinking OFF answered in
    2.3-2.9s. So a small ceiling here does not make the route cheaper, it
    makes it return nothing.

    `llm.stream_chat_events` already protects the thinking-ON case (it floors
    the request at MAX_OUTPUT_TOKENS whenever thinking is on), which makes
    this value the ceiling the FAST path actually runs under. It has to be
    big enough for a full multi-image extraction to finish, hence generous.

    Clamping keeps the request coherent with the declared capability: a
    deployment that lowers VISION_OUTPUT_LIMIT is never asked for more than
    it serves, and a caller that wants less (or the request-level budget a
    future caller passes in) is honoured as-is.
    """
    want = max_tokens if max_tokens and max_tokens > 0 else VISION_ANSWER_TOKENS
    limit = settings.vision_capabilities.output_limit or 0
    return min(want, limit) if limit > 0 else want


#: Conversations where the OCR sidecar has already missed its deadline once.
#: THE PASS RUNS BEFORE THE MAIN MODEL CAN START, so a miss is 10 s of a
#: person watching "Reading the text in the image…" for a transcript that is
#: then thrown away. Measured 2026-09-17 against the live sidecar: a 1280x800
#: UI screenshot needs 11.6 s and a matplotlib chart 25.4 s, both past
#: OCR_VISION_DEADLINE_S (10.0) — and a chat full of screenshots pays that
#: every single turn. One miss benches the pass for the rest of THAT
#: conversation; the pixels answered those images perfectly well without it
#: (the Fast path, which never runs the pass, read the same images correctly
#: in 0.8-1.1 s). Bounded and in-process on purpose: it is a latency hint,
#: losing it on a restart costs one slow turn and nothing else.
_OCR_BENCH_LIMIT = 512
_ocr_benched: "OrderedDict[str, bool]" = OrderedDict()


def ocr_is_benched(conversation_id: Optional[str]) -> bool:
    """Has the OCR pass already failed to beat its deadline here?"""
    return bool(conversation_id) and conversation_id in _ocr_benched


def _bench_ocr_if_it_missed_the_deadline(
    conversation_id: Optional[str], reads: Sequence
) -> None:
    """Bench the pass for this conversation after a deadline miss.

    Only a DEADLINE miss benches it. A sidecar that is down fails instantly
    and costs the turn nothing, so it keeps its chance to come back.
    """
    if not conversation_id:
        return
    if not any(
        getattr(r, "status", "") == "failed" and "deadline" in (getattr(r, "error", "") or "")
        for r in reads
    ):
        return
    _ocr_benched[conversation_id] = True
    _ocr_benched.move_to_end(conversation_id)
    while len(_ocr_benched) > _OCR_BENCH_LIMIT:
        _ocr_benched.popitem(last=False)


def forget_ocr_bench(conversation_id: Optional[str]) -> None:
    """Test/operational hook: give this conversation the pass back."""
    _ocr_benched.pop(conversation_id or "", None)


async def run_vision_engine(
    message: str,
    images: "Optional[str | Sequence[str]]",
    history: Sequence[dict],
    emit: Emit,
    *,
    effort: str = DEFAULT_EFFORT,
    max_tokens: Optional[int] = None,
    conversation_id: Optional[str] = None,
) -> str:
    """Answer about attached image(s) at the effort the caller asked for.

    `effort` accepts any wire value `llm.normalize_effort` understands and
    means the same thing it means on the text route: fast -> thinking OFF,
    think/max -> thinking ON. It is NOT a knob invented here — the whole
    mechanism is `stream_chat_events` turning the effort into the chat
    template's `enable_thinking`; this engine's only job is to stop
    overriding it.

    `conversation_id` is only for the OCR pre-pass: it is how a conversation
    whose images the sidecar cannot read inside its deadline stops paying
    that deadline on every later turn. Nothing else reads it, and a caller
    that has no conversation (the bare API, graph.py) may leave it None.
    """
    imgs = [images] if isinstance(images, str) else list(images or [])
    if not imgs:
        raise ValueError("the vision engine requires an attached image")

    level = llm.normalize_effort(effort)
    user_content = build_user_content(message + extraction_hint(message), imgs)

    # Is the picture legible at all? Measured from the pixels, in
    # milliseconds, before anything is sent (engines/image_quality.py). The
    # prompt rule alone left the audit's dark sign answered "ext. 4472" in
    # two runs of six; a picture whose contrast is 8 of 255 is one the model
    # cannot tell a reading from an expectation on, so the app says so.
    # Silent for an ordinary image, and never a gate.
    from .image_quality import legibility_note

    note = legibility_note(imgs)
    if note:
        user_content.append({"type": "text", "text": note})

    # Unlimited-OCR pass (2026-08-06): screenshots, invoices and photographed
    # documents get a dedicated OCR transcript alongside the pixels — the OCR
    # model reads dense text/tables the general VLM misses. Photos with no
    # text produce an empty transcript and add nothing.
    #
    # 2026-08-29: Fast skips it. The pass runs BEFORE the main model can
    # start and measured ~3.3 s of the 4.0 s to the first visible token on a
    # 1280x800 screenshot (0.66 s straight to vLLM); the main model reads
    # screenshots at that resolution itself. Think/Max keep the transcript —
    # that is where dense scans and small print earn it.
    from .ocr import evidence_block, image_ocr_prompt, read_images

    if settings.ocr_enabled and level != "fast" and not ocr_is_benched(conversation_id):
        # 2026-09-03: bounded. Measured on a text-dense 1280x800 screenshot
        # at Think: 47 s before the first visible token, all of it the OCR
        # sidecar decoding a long transcript the main model did not need to
        # read the screenshot. The transcript is capped and time-boxed; when
        # it misses the deadline the answer proceeds from the pixels.
        #
        # 2026-09-18, three changes, all from the same measurement (see
        # `ocr.image_ocr_prompt`):
        #   * the prompt is the one that was measured to WORK on chat images,
        #     not the document default that prefixed "ovi…" to every read and
        #     invented a date on the dark sign;
        #   * `read_images`, not `ocr_images` — the flat view forwards a
        #     DEGENERATE read's text verbatim, so a loop reached the prompt as
        #     if it were the picture's text. Only `ok` reads are appended now;
        #   * the block says whose output it is and that it is not a reading.
        await emit("status", {"text": "Reading the text in the image…"})
        reads = await read_images(
            imgs,
            prompt=image_ocr_prompt(),
            max_output_tokens=settings.ocr_vision_max_tokens,
            deadline_s=settings.ocr_vision_deadline_s,
        )
        _bench_ocr_if_it_missed_the_deadline(conversation_id, reads)
        block = evidence_block(reads, "image")
        if block:
            user_content.append({"type": "text", "text": block})

    messages: List[dict] = [
        {"role": "system", "content": _SYSTEM},
        *history_turns(history),
        {"role": "user", "content": user_content},
    ]

    parts: List[str] = []
    # Reasoning-aware stream (the vision model IS the thinking main model):
    # surface thinking in the panel and give room to finish the answer.
    #
    # Until 2026-08-28 this call hard-coded effort="medium" — an alias for
    # "think" — so every image upload ran the main model (then the 27B) with thinking ON no matter
    # which of Fast/Think/Max the user picked in the composer. Measured on
    # three 1280x800 screenshots: thinking on -> 26-28s to the first visible
    # token and no answer inside the budget; thinking off -> 2.3-2.9s and a
    # complete answer. Passing the request's effort through is the entire
    # fix; `stream_chat_events` already maps it to `enable_thinking`.
    #
    # model_choice stays "smart" on purpose: the main model (Qwen3.6-35B-A3B since 2026-08-29; the 27B before) is the vision model on
    # this deployment (VISION_BASE_URL == OPENAI_BASE_URL). The dedicated 8B
    # VL model is NOT a fallback — measured 2026-08-28 it was slower to first
    # token AND refused the extraction outright.
    async for kind, delta in llm.stream_chat_events(
        messages,
        model_choice="smart",
        effort=level,
        max_tokens=vision_max_tokens(max_tokens),
    ):
        await emit(kind, {"text": delta})
        if kind == "token":
            parts.append(delta)
    answer = "".join(parts)

    # §10: the single final meta carries only contract keys. The structured
    # invoice/contract fields already reach the user inside the streamed
    # answer's fenced ```json block; there is no `extracted` meta key in the
    # contract, so nothing else is emitted.
    await emit("meta", {"route": "vision"})
    return answer
