"""Images the conversation has already been shown (2026-09-18).

THE BUG THIS EXISTS FOR. A person uploads a photo of a note and asks what
number to call; the answer is right. They then ask, one turn later and with
nothing re-attached, "What was the invoice number again, and what day was the
meeting moved to?" — both answers are in the photo — and the assistant says
"I don't see the note you're referring to in our chat. Could you please paste
it or upload it here?" (audit, 2026-09-17). Worse, that turn routed to `rag`,
so a question about a photo could be answered out of unrelated workspace
documents.

WHY THE IMAGE WAS GONE. The composer sends `images` only on the turn the file
is attached to; the history the frontend resends is text with no marker that
an image was ever there; and `vision.history_turns` drops multimodal entries
on purpose. Documents already survive a turn (`_resolve_document_refs`) and
videos have both a pinned block and a follow-up test — images had neither.

WHAT THIS IS. The bytes of the conversation's most recent image turn, held in
this process, with a TTL, a per-conversation byte budget and a cap on how
many conversations are remembered at once. It is a cache, not a record: a
restart loses it and the next turn behaves exactly as it did before this
module existed. Nothing is written to disk and nothing is written to the
database, so no image outlives the conversation that produced it.

THE TTL RELEASES MEMORY, NOT ONLY RECALL (adversarial QA, 2026-09-18). An
expired entry used to be dropped only when ITS conversation asked again, so
the bytes stayed: 70 conversations, TTL passed, one more image -> 47 entries
and 62,666,884 characters still held. Every read and write now sweeps the
expired entries first (at most 64 of them, microseconds).

SCOPE IS THE VIEWER AND THE CONVERSATION. `chat`'s conversation key is
whatever the client sent (`request.conversation_id or scoped_session`), so
the key here is that value PLUS the viewer's id. Two accounts that happened
to send the same conversation id — or one that guessed another's — get
different entries, and image bytes never cross an account. A call with no
viewer stores and recalls nothing (repair round 2): it used to fall back to
the bare conversation id, so any two callers that forgot the viewer would
have shared one entry.

WHAT THE CALLER OWES THIS MODULE (hand-off to main.py and history.py, which
this track does not own): `note_turn` on every turn the word test is not
asked about (a document, URL, video, agent or research turn), so "that
chart" after an intervening PDF turn is not taken for the picture; and
`forget` when a conversation is deleted, so a deleted conversation's photo
cannot answer the same id again inside the TTL.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

log = logging.getLogger(__name__)

def _env_number(name: str, default: float) -> float:
    """Read a tuning from the environment; nobody on this programme owns
    config.py (see `video.screen.video_ocr_prompt` for the same choice)."""
    raw = (os.environ.get(name) or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def ttl_s() -> float:
    """How long an image stays available for follow-ups. Long enough for a
    real conversation about one picture, short enough that a browser tab left
    open overnight does not pin megabytes."""
    return _env_number("IMAGE_MEMORY_TTL_S", 2 * 60 * 60)


def max_conversations() -> int:
    return int(_env_number("IMAGE_MEMORY_CONVERSATIONS", 64))


def max_chars() -> int:
    """Base64 characters kept per conversation (~18 MB of image data at the
    default). A turn over the budget is remembered up to the images that
    fit — half a memory beats the process holding an unbounded one."""
    return int(_env_number("IMAGE_MEMORY_MAX_CHARS", 24_000_000))


def max_total_chars() -> int:
    """Base64 characters this process will hold across ALL conversations.

    The per-conversation cap alone is not a bound: 64 conversations at 24 M
    characters each is 1.5 GB of orchestrator memory, which is not a cache,
    it is an incident. The oldest conversations are dropped until the whole
    store fits inside this budget (~48 MB of image data by default).
    """
    return int(_env_number("IMAGE_MEMORY_TOTAL_CHARS", 64_000_000))


@dataclass
class _Remembered:
    images: List[str]
    #: The turn's question and answer, lowercased. It is what a later
    #: message is compared against when it names no picture at all.
    context: str = ""
    at: float = field(default_factory=time.monotonic)
    #: Text turns this conversation has sent since the image turn (every
    #: turn main.py asks `images_for_followup` about). "That chart" points
    #: at the picture only while the picture is the last thing shown.
    turns_after: int = 0


#: NOT named `_store`: `tests/test_exclusion_invariants.py` proves that the
#: shared web corpus's `_store` is reached from one function only, and it
#: matches on the bare NAME across app/ — a second `_store` anywhere in the
#: package breaks that proof.
_remembered_images: "OrderedDict[str, _Remembered]" = OrderedDict()


#: main.py's conversation key when the client sent no conversation id is
#: f"u{viewer}-{session_id}", and ChatRequest.session_id defaults to
#: "default". That key is shared by EVERY such request of the account, so
#: a photo sent in one of them answered "what else is written in the
#: photo?" asked in an unrelated one (measured, repair round 3). The
#: browser always sends a conversation id; a client that sends neither has
#: not said which conversation it is in, so nothing is remembered for it.
_SHARED_SESSION = "default"


def scope(conversation_id: Optional[str], user_id: "Optional[object]" = None) -> str:
    """The store's key: the viewer and the conversation, never one alone -
    '' (nothing stored, nothing recalled) when either is missing, or when
    the "conversation" is the account's shared default session."""
    if not conversation_id or user_id is None or str(user_id) == "":
        return ""
    if str(conversation_id) == f"u{user_id}-{_SHARED_SESSION}":
        return ""
    return f"u{user_id}:{conversation_id}"


def remember(
    conversation_id: Optional[str],
    images: Sequence[str],
    *,
    question: str = "",
    answer: str = "",
    user_id: "Optional[object]" = None,
) -> None:
    """Keep this turn's images for the rest of the conversation.

    main.py calls this inline from the async /chat handler, so nothing slow
    may run here on the event loop: an image over the budget is downscaled
    in a worker thread and stored when that finishes (the previous picture
    is dropped at once - a follow-up in between gets what it got before this
    module existed). With no running loop (scripts, tests) it runs inline.
    """
    _sweep_expired()
    key = scope(conversation_id, user_id)
    if not key:
        return
    images = [img for img in (images or []) if (img or "").strip()]
    if not images:
        return
    context = f"{question}\n{answer}".lower()
    token = object()
    _latest[key] = token
    if sum(len(img) for img in images) <= max_chars():
        _keep(key, token, _fit(images), context)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _keep(key, token, _fit(images), context)
        return
    _remembered_images.pop(key, None)
    job = loop.run_in_executor(None, _fit, images)
    job.add_done_callback(lambda done: _keep_when_fitted(key, token, done, context))


#: The newest remember() per key. A downscale that finishes after a newer
#: image turn (or a forget) for the same conversation is thrown away.
_latest: "dict[str, object]" = {}


def _keep_when_fitted(key: str, token: object, done, context: str) -> None:
    if done.cancelled() or done.exception() is not None:
        if _latest.get(key) is token:
            _latest.pop(key, None)
        return
    _keep(key, token, done.result(), context)


def _keep(key: str, token: object, kept: List[str], context: str) -> None:
    if _latest.get(key) is not token:
        return
    _latest.pop(key, None)
    if not kept:
        return
    _remembered_images[key] = _Remembered(images=kept, context=context)
    _remembered_images.move_to_end(key)
    _evict()


def _fit(images: Sequence[str]) -> List[str]:
    """The images that fit the per-conversation budget, in order; one over
    the budget on its own is kept as a smaller copy."""
    kept: List[str] = []
    budget = max_chars()
    for img in images:
        if len(img) > budget:
            # A single image over the budget used to be dropped whole: a
            # 20 MB photo (27,962,028 characters against 24,000,000) was
            # not remembered at all, so the next question about it got the
            # original "I don't see the note" turn (QA, 2026-09-18). Keep a
            # copy at the composer's own 1600 px cap instead.
            img = _smaller_copy(img, budget) or ""
            if not img:
                break
        budget -= len(img)
        kept.append(img)
    return kept


#: Long edges tried, largest first, when an image does not fit the budget.
#: 1600 is `MAX_IMAGE_EDGE` in frontend/lib/images.ts: what a browser upload
#: already looks like.
_SHRINK_EDGES = (1600, 1024, 768, 512)

#: Pixels this module will decode to make a smaller copy, read from the
#: header before anything is decoded. The security review measured the
#: unbounded version on the worker (Pillow 12.3.0): a 13000 x 13000 1-bit
#: PNG padded to 25.4 M characters blocked for 2,410 ms at +1,567 MiB peak
#: RSS, and a 9400 x 9400 one - under PIL's own bomb warning - 1,621 ms and
#: +926 MiB, with five such images allowed per /chat body. 40 MP covers an
#: 8K screenshot (33.2 MP) and a 600-dpi A4 scan (34.8 MP). A JPEG may be
#: larger, up to the Files API's ceiling, because `draft` decodes it at a
#: quarter or an eighth of its size.
_MAX_DECODE_PIXELS = 40_000_000
_MAX_JPEG_PIXELS = 89_478_485


def _smaller_copy(image: str, budget: int) -> Optional[str]:
    """A downscaled data URL of `image` that fits `budget` characters, or None.

    Runs only for an image over the per-conversation budget - one the
    composer would itself have downscaled. The picture is decoded ONCE and
    reduced to 1600 px before any conversion (the first version converted
    the full-size image to RGB and then copied it at full size for every
    edge it tried). Measured on this box before the change: a 23 MB
    6000x4000 JPEG became a 1.0 M-character copy in 238 ms, a 20 MB PNG in
    143 ms.
    """
    import base64
    import binascii
    import io

    raw = re.sub(r"^data:image/[\w.+-]+;base64,", "", image.strip(), flags=re.I)
    try:
        payload = base64.b64decode(raw, validate=False)
        from PIL import Image

        with Image.open(io.BytesIO(payload)) as im:
            photo = (im.format or "").upper() == "JPEG"
            ceiling = _MAX_JPEG_PIXELS if photo else _MAX_DECODE_PIXELS
            if im.width * im.height > ceiling:
                log.debug("oversize image not downscaled: %dx%d is over the ceiling", im.width, im.height)
                return None
            im.draft("RGB", (_SHRINK_EDGES[0], _SHRINK_EDGES[0]))
            # Resampling needs a real mode: a 1-bit or palette picture
            # resized as it is would be point-sampled into noise.
            work = im if im.mode in ("RGB", "RGBA", "L", "LA") else im.convert("L" if im.mode == "1" else "RGB")
            work.thumbnail((_SHRINK_EDGES[0], _SHRINK_EDGES[0]))
            source = work.convert("RGB")
        for edge in _SHRINK_EDGES:
            copy = source if edge >= max(source.size) else source.resize(
                _fit_inside(source.size, edge), Image.Resampling.LANCZOS
            )
            # PNG for anything that was not already a lossy photo (sharp
            # text stays sharp), JPEG when PNG cannot fit - the frontend's
            # own rule.
            for fmt in (("JPEG",) if photo else ("PNG", "JPEG")):
                buf = io.BytesIO()
                copy.save(buf, format=fmt, **({"quality": 90} if fmt == "JPEG" else {}))
                url = f"data:image/{fmt.lower()};base64," + base64.b64encode(buf.getvalue()).decode()
                if len(url) <= budget:
                    return url
    except (binascii.Error, Exception) as exc:  # noqa: BLE001 — a cache, never a failed turn
        # Not an image PIL can open, or a decompression bomb: the turn has
        # already been answered, so the only cost is not remembering it.
        log.debug("oversize image could not be downscaled: %s", type(exc).__name__)
    return None


def _fit_inside(size: "tuple", edge: int) -> "tuple":
    w, h = size
    scale = edge / max(w, h)
    return max(1, round(w * scale)), max(1, round(h * scale))


def _sweep_expired() -> None:
    """Drop every expired entry, so the TTL bounds memory and not only recall."""
    limit = ttl_s()
    now = time.monotonic()
    for key in [k for k, e in _remembered_images.items() if now - e.at > limit]:
        _remembered_images.pop(key, None)


def _evict() -> None:
    """Drop the least recently used conversations until the store fits."""
    limit = max_conversations()
    budget = max_total_chars()
    while len(_remembered_images) > limit:
        _remembered_images.popitem(last=False)
    while len(_remembered_images) > 1 and _total_chars() > budget:
        _remembered_images.popitem(last=False)


def _total_chars() -> int:
    return sum(len(i) for entry in _remembered_images.values() for i in entry.images)


def recall(conversation_id: Optional[str], user_id: "Optional[object]" = None) -> List[str]:
    """The conversation's remembered images, or [] once they have expired."""
    _sweep_expired()
    conversation_id = scope(conversation_id, user_id)
    if not conversation_id:
        return []
    entry = _remembered_images.get(conversation_id)
    if entry is None:
        return []
    if time.monotonic() - entry.at > ttl_s():
        _remembered_images.pop(conversation_id, None)
        return []
    _remembered_images.move_to_end(conversation_id)
    return list(entry.images)


def forget(conversation_id: Optional[str], user_id: "Optional[object]" = None) -> None:
    """Drop the conversation's picture now (and any downscale still running
    for it). history.py's delete should call this; see the module docstring."""
    key = scope(conversation_id, user_id)
    _remembered_images.pop(key, None)
    _latest.pop(key, None)


def note_turn(conversation_id: Optional[str], user_id: "Optional[object]" = None) -> None:
    """Count a turn the word test was not asked about (a document, URL,
    video, agent or research turn). After it, the picture is no longer the
    last thing shown, so "that chart" may be the PDF's. main.py should call
    this wherever it skips `images_for_followup`; see the module docstring."""
    entry = _remembered_images.get(scope(conversation_id, user_id))
    if entry is not None:
        entry.turns_after += 1


def clear() -> None:
    """Test hook."""
    _remembered_images.clear()
    _latest.clear()


# --- is this turn about the picture? ---------------------------------------
#
# Deliberately cheap and deterministic: no model call decides whether a
# person's next sentence is still about their photo.
#
# CONSERVATIVE, MEASURED (adversarial QA, 2026-09-18). The first version
# fired on any noun that CAN name a picture ("note", "sign", "chart",
# "attached"), on any continuation word ("and", "ok", "too", "again"), and
# on any two words shared with the image turn. After an image turn, 9 of 10
# turns that were NOT about it fired - "ok thanks!", "Please note that I'm
# away tomorrow...", "Sign the email as Priya...", "Summarise the attached
# PDF", "Make a bar chart of monthly revenue from the sales file", and in a
# dataset conversation "What is the total revenue by region?" - and because
# main.py's image branch sits above the dataset, search, agent and chat
# branches, every one of them was answered from the stale photo instead of
# where it went before. A wrong fire costs a wrong answer (and puts the
# picture - third-party content that can carry injected text - back in
# front of the model); a missed one costs only what the conversation had
# before this module existed (the previous answer is still in the history).
# So a turn fires only on EVIDENCE that it points at the picture:
#
#   (1) it names THE picture: "the photo", "this screenshot", "image 2" -
#       a pointing word before the noun, and not a topic ("the best photo
#       editing app", "a screenshot on Ubuntu", "resize images in Python",
#       "the photos in the PDF" all fired in round 2's reviews);
#   (2) it points back at a thing the image turn talked about: "the note",
#       "the dashboard" - the same word, or its plural (a four-letter prefix
#       made "the list" match "listed", "the chart" "charge", "the bill"
#       "billion" and "the form" "format"); or at a thing only ever looked
#       at ("the sign") with a reading verb ("read the sign again") or as a
#       place ("the address on the receipt?");
#   (3) right after the picture, a distal demonstrative ("that chart"), "it"
#       as the picture ("what's the date on it?", "transcribe it"), or a
#       question about its lines ("what does the second line say?", "anything
#       else written on it?"). "This letter" / "this list" is left out: with nothing
#       attached, "this" points at what the turn itself carries ("Sort this
#       list alphabetically: banana, apple, cherry");
#   (4) it asks again for something the image turn said: "what was the
#       invoice number again?";
#   (5) it asks about a place IN the picture: "and the total at the
#       bottom?" (but not "the bottom OF the ocean").
#
# Everything stands down when the turn names another source it points at
# ("the PDF", "the dataset", "the sales file", "the email"); (2)-(5) also
# when it names one at all, and when the turn carries its own material (a
# pasted letter after a line break, a list after a colon). A turn longer
# than a follow-up question is material, decided without reading it (a
# 10 MB paste took 3.1-4.7 s on the event loop in round 2's review). Nothing
# fires for a request to MAKE a picture, or for a figure of speech ("the big
# picture").

#: A follow-up question about a picture is a sentence or two; a longer turn
#: is the person's own material. Every on-image follow-up in this module's
#: tests is under 120 characters.
_MAX_FOLLOWUP_CHARS = 1000

#: A word that points at one particular thing already in the conversation.
_POINTER = r"(?:the|this|that|these|those|my|our|your|his|her|their|its|both)"

#: (1) Words that only ever mean a picture...
_PICTURE_NOUN = (
    r"(?:images?|photos?|photographs?|pictures?|pics?|screenshots?|screen ?shots?|snapshots?)"
)
#: ...named as THE picture, and not as a topic ("photo editing app").
_NAMES_A_PICTURE = re.compile(
    r"\b" + _POINTER + r"\s+(?:[\w'-]+\s+){0,2}?" + _PICTURE_NOUN + r"\b"
    r"(?!\s+(?:editing|editor|editors|processing|tool|tools|app|apps|application|software|"
    r"library|libraries|format|formats|file ?types?|size|sizes|quality|recognition|"
    r"generation|generator|generators|compression|resolution|upload|uploads|limit|limits|"
    r"gallery|viewer|storage|backup|backups|settings|shortcut|folder|folders)\b)"
    r"|\b" + _PICTURE_NOUN + r"\s+(?:\d|one|two|three|four|five)\b",
    re.I,
)
#: ...except in a figure of speech.
_FIGURATIVE = re.compile(
    r"\b(big|bigger|whole|full|overall|clear|clearer)\s+picture\b|"
    r"\bget the picture\b|\bpicture (this|that|yourself|it)\b",
    re.I,
)
#: ...or when the turn asks for a NEW picture: "generate a picture of a cat".
_MAKES_A_PICTURE = re.compile(
    r"\b(generate|create|make|draw|design|render|paint|sketch|produce|imagine)\b"
    r"(?:\s+[\w'-]+){0,3}?\s+(an?|some|new|another)\s+(?:[\w'-]+\s+){0,2}?"
    r"(images?|photos?|pictures?|pics?|diagrams?|charts?|graphs?|drawings?|"
    r"illustrations?|logos?|icons?)\b",
    re.I,
)

#: (2) Things a photo shows that are only ever LOOKED AT: with a reading
#: verb, "the sign" is the picture even when the image turn never said so.
_SEEN_THINGS = (
    "sign|signs|label|labels|receipt|receipts|whiteboard|poster|posters|slide|"
    "slides|handwriting|plate|sticker|banner|scan|scans|screen|display"
)
#: Things that are also words for data or text: they point at the picture
#: only when the image turn talked about them ("the table" in a dataset
#: conversation is the dataset's).
_TALKED_ABOUT_THINGS = (
    "note|notes|invoice|invoices|bill|ticket|table|tables|chart|charts|graph|"
    "graphs|figure|figures|diagram|diagrams|drawing|form|page|map|card|menu|"
    "dashboard|letter|list|board|meter|row|rows|column|columns|legend|axis"
)
_BACK_REFERENCE = re.compile(
    r"\b(?:the|that|this|those|these)\s+(?:[\w'-]+\s+){0,2}?"
    r"(" + _SEEN_THINGS + "|" + _TALKED_ABOUT_THINGS + r")\b",
    re.I,
)
_SEEN_RE = re.compile(r"^(" + _SEEN_THINGS + r")$", re.I)
#: A place on a thing that is only ever looked at: "the address on the
#: receipt?" (4810da0 sent it to the picture; round 1's fix did not). Not
#: "screen" or "display", which are also a computer's.
_ON_A_SEEN_THING = re.compile(
    r"\b(?:on|in|at|from|of)\s+(?:the|that|this|your|my)\s+(?:[\w'-]+\s+){0,2}?"
    r"(receipts?|signs?|labels?|whiteboard|posters?|sticker|banner|scans?|handwriting|plate)\b",
    re.I,
)
_READING_VERB = re.compile(
    r"\b(read|reads|say|says|said|written|write|show|shows|showing|shown|see|"
    r"visible|zoom|look|looks|legible|printed)\b",
    re.I,
)

#: (3) "that chart": a distal demonstrative points back at what was just
#: shown. Measured live at Fast (2026-09-18): after a dashboard screenshot
#: in a dataset conversation, "Which region is smallest in that chart?"
#: went to the dataset engine, which answered that the profile "does not
#: show a chart" - the answer about the dashboard never said "chart".
_DEMONSTRATIVE = re.compile(
    r"\b(?:that|those)\s+(?:[\w'-]+\s+){0,2}?"
    r"(" + _SEEN_THINGS + "|" + _TALKED_ABOUT_THINGS + r")\b",
    re.I,
)
#: "Is there anything else written on it?" / "What else does it say?"
_ELSE_ON_IT = re.compile(
    r"\b(?:anything|something|what|nothing)\s+else\b[^.?!\n]{0,40}"
    r"\b(?:written|printed|says?|said|shown|shows?|visible|mentioned|on it|in it)\b"
    r"|\b(?:written|printed)\s+(?:on|in)\s+(?:it|there)\b"
    r"|\bdoes\s+it\s+(?:say|show|mention|read|list)\b",
    re.I,
)

#: "What's the date on it?", "Can you transcribe it word for word?",
#: "Translate the whole thing into French." - right after the picture, "it"
#: and "the whole thing" are the picture (rvq1's natural follow-ups).
_IT_IS_THE_PICTURE = re.compile(
    r"\b(?:on|in)\s+it\s*\?"
    r"|\b(?:transcribe|translate|proofread|read)\s+(?:it|all of it|the whole thing|everything)\b",
    re.I,
)
#: "What does the second line say?" - a line of the picture's text.
_A_LINE_OF_IT = re.compile(
    r"\bthe\s+(?:first|second|third|fourth|fifth|last|top|bottom|next|other)\s+"
    r"(?:line|lines|row|word|words|entry|item|number|name|paragraph)\b",
    re.I,
)

#: (4) "...again", in a question or with a reading verb.
_AGAIN = re.compile(r"\bagain\b", re.I)
_QUESTION_START = re.compile(
    r"^\s*(what|what's|whats|which|who|whom|whose|where|when|why|how|is|are|was|"
    r"were|does|do|did|can|could|would|will|should)\b",
    re.I,
)

#: (5) A place in the picture - never "the bottom OF something".
_PLACE_IN_PICTURE = re.compile(
    r"\b(?:at|in|on|near|along)\s+the\s+(?:very\s+)?(top|bottom|left|right|corner|"
    r"background|foreground|middle|centre|center|edge|margin|header|footer)"
    r"(?:[\s-]+(?:left|right|corner|edge|half|part))?\b(?!\s+of\b)",
    re.I,
)

#: The turn names a source that is not the picture...
_OTHER_SOURCE_NOUN = (
    r"(?:pdfs?|docx?|documents?|files?|dataset|data ?set|csv|spreadsheets?|excel|"
    r"xlsx|sheets?|database|website|web ?page|url|link|videos?|repo|repository|"
    r"github|e-?mails?|article|report)"
)
_OTHER_SOURCE = re.compile(r"\b" + _OTHER_SOURCE_NOUN + r"\b", re.I)
#: ...and points at it: "in the PDF", "the sales file", "my report" - not a
#: format to make ("put the text from this photo into a Word document").
_POINTS_AT_ANOTHER_SOURCE = re.compile(
    r"\b" + _POINTER + r"\s+(?:[\w'-]+\s+){0,2}?" + _OTHER_SOURCE_NOUN + r"\b", re.I
)


def _carries_material(text: str) -> bool:
    """Did the person paste what they are talking about? A line break, or a
    colon, followed by three or more words that are not a question."""
    for sep in ("\n", ":"):
        head, found, rest = text.strip().partition(sep)
        if found and "?" not in rest and len(rest.split()) >= 3:
            return True
    return False


#: Short and common words carry no evidence of what a turn is about.
_STOPWORDS = frozenset(
    """about after again against all also and any are because been before
    being between both but can cant come could did does doing dont down
    each few for from further had has have having her here hers him his how
    into its itself just like made make many may more most much must need
    not now off once only other our out over own please same she should
    some such than that the their them then there these they this those
    through too under until very was way were what when where which while
    who whom why will with would you your yours""".split()
)
_WORD = re.compile(r"[a-z][a-z0-9'-]{3,}")


def _content_words(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS]


def _stems(text: str) -> set:
    """Four-character prefixes, so "invoice" finds "invoiced" - the audit's
    follow-up asked for an "invoice number" when the answer said
    "invoiced". Used only with "again" (4), where the turn ALSO asks to
    hear something repeated."""
    return {w[:4] for w in _content_words(text)}


def _said(noun: str, words: set) -> bool:
    """The image turn used this noun, or its plural or singular."""
    noun = noun.lower()
    return bool({noun, noun + "s", noun + "es", noun[:-1] if noun.endswith("s") else noun} & words)


def _is_a_question(text: str) -> bool:
    return "?" in text or bool(_QUESTION_START.search(text))


def _points_at_the_picture(text: str, context: str, *, just_shown: bool = False) -> bool:
    """The shapes of evidence above, for one message. `just_shown`: the
    image turn is the conversation's previous turn, so "that chart" can
    only mean the chart in the picture."""
    if len(text) > _MAX_FOLLOWUP_CHARS or not text.strip():
        return False
    if _FIGURATIVE.search(text) or _MAKES_A_PICTURE.search(text):
        return False
    if _POINTS_AT_ANOTHER_SOURCE.search(text):
        return False
    if _NAMES_A_PICTURE.search(text):
        return True
    if _OTHER_SOURCE.search(text) or _carries_material(text):
        return False
    said_before = set(_content_words(context))
    for match in _BACK_REFERENCE.finditer(text):
        noun = match.group(1)
        if _said(noun, said_before):
            return True
        if _SEEN_RE.match(noun) and _READING_VERB.search(text):
            return True
    if _ON_A_SEEN_THING.search(text) and _is_a_question(text):
        return True
    if just_shown and (
        _DEMONSTRATIVE.search(text)
        or _IT_IS_THE_PICTURE.search(text)
        or ((_ELSE_ON_IT.search(text) or _A_LINE_OF_IT.search(text)) and _is_a_question(text))
    ):
        return True
    if _AGAIN.search(text) and (_is_a_question(text) or _READING_VERB.search(text)):
        if _stems(text) & _stems(context):
            return True
    if _PLACE_IN_PICTURE.search(text) and _is_a_question(text):
        return True
    return False


def is_about_the_image(
    conversation_id: Optional[str], message: str, user_id: "Optional[object]" = None
) -> bool:
    """Should this text-only turn be answered with the remembered image?"""
    entry = _remembered_images.get(scope(conversation_id, user_id))
    if entry is None or not recall(conversation_id, user_id):
        return False
    text = message or ""
    just_shown = entry.turns_after == 0
    # Every turn main.py asks about counts, fired or not: after one more
    # text turn "that chart" may be a chart the assistant made in between.
    # (Turns main.py does not ask about are counted by `note_turn`.)
    entry.turns_after += 1
    return _points_at_the_picture(text, entry.context, just_shown=just_shown)


def images_for_followup(
    conversation_id: Optional[str], message: str, user_id: "Optional[object]" = None
) -> List[str]:
    """The remembered images when this turn is about them, else []."""
    if not is_about_the_image(conversation_id, message, user_id):
        return []
    return recall(conversation_id, user_id)
