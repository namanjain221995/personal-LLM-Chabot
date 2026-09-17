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

SCOPE IS THE VIEWER AND THE CONVERSATION. `chat`'s conversation key is
whatever the client sent (`request.conversation_id or scoped_session`), so
the key here is that value PLUS the viewer's id. Two accounts that happened
to send the same conversation id — or one that guessed another's — get
different entries, and image bytes never cross an account.
"""
from __future__ import annotations

import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import List, Optional, Sequence


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


#: NOT named `_store`: `tests/test_exclusion_invariants.py` proves that the
#: shared web corpus's `_store` is reached from one function only, and it
#: matches on the bare NAME across app/ — a second `_store` anywhere in the
#: package breaks that proof.
_remembered_images: "OrderedDict[str, _Remembered]" = OrderedDict()


def scope(conversation_id: Optional[str], user_id: "Optional[object]" = None) -> str:
    """The store's key: the viewer and the conversation, never one alone."""
    if not conversation_id:
        return ""
    return f"u{user_id}:{conversation_id}" if user_id is not None else str(conversation_id)


def remember(
    conversation_id: Optional[str],
    images: Sequence[str],
    *,
    question: str = "",
    answer: str = "",
    user_id: "Optional[object]" = None,
) -> None:
    """Keep this turn's images for the rest of the conversation."""
    conversation_id = scope(conversation_id, user_id)
    if not conversation_id or not images:
        return
    kept: List[str] = []
    budget = max_chars()
    for img in images:
        if not (img or "").strip():
            continue
        if budget - len(img) < 0:
            break
        budget -= len(img)
        kept.append(img)
    if not kept:
        return
    _remembered_images[conversation_id] = _Remembered(
        images=kept, context=f"{question}\n{answer}".lower()
    )
    _remembered_images.move_to_end(conversation_id)
    _evict()


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
    _remembered_images.pop(scope(conversation_id, user_id), None)


def clear() -> None:
    """Test hook."""
    _remembered_images.clear()


# --- is this turn about the picture? ---------------------------------------
#
# Three tests, any of which is enough. They are deliberately cheap and
# deterministic: no model call decides whether a person's next sentence is
# still about their photo, because the wrong answer here is a 10-second
# detour, not a wrong fact.

#: (1) The message names a picture.
_NAMES_AN_IMAGE = re.compile(
    r"\b(image|images|photo|photos|photograph|picture|pictures|pic|pics|"
    r"screenshot|screen ?shot|scan|scanned|sign|poster|label|receipt|"
    r"invoice|note|whiteboard|slide|diagram|chart|graph|figure|drawing|"
    r"snapshot|frame|attachment|attached|uploaded|i sent|i shared|"
    r"on screen|in the shot)\b",
    re.I,
)

#: (2) The message continues the previous one instead of starting a subject.
#: "What was the invoice number AGAIN", "and what about the total?", "what
#: else does it say?" — the audit's own failing turn is in here.
_CONTINUES = re.compile(
    r"(^\s*(and|also|what about|how about|ok|okay)\b)|"
    r"\b(again|as well|too|the other one|the second one|the first one|"
    r"the last one|anything else|what else|also say|rest of it)\b",
    re.I,
)

#: (3) The message reuses the words of the image turn. Short and common
#: words carry no evidence, so they are not counted.
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
#: Two content words in common is the floor. One is coincidence — every
#: conversation says "number" eventually.
_OVERLAP_FLOOR = 2


def _content_words(text: str) -> List[str]:
    return [w for w in _WORD.findall((text or "").lower()) if w not in _STOPWORDS]


def _overlaps(message: str, context: str) -> bool:
    """Does the message reuse the image turn's vocabulary?

    Matching on a four-character prefix so "invoice" finds "invoiced" and
    "meetings" finds "meeting" — the audit's follow-up asked about an
    "invoice number" when the answer said "invoiced", and an exact-word test
    would have missed it.
    """
    if not context.strip():
        return False
    stems = {w[:4] for w in _content_words(context)}
    if not stems:
        return False
    hits = {w[:4] for w in _content_words(message) if w[:4] in stems}
    return len(hits) >= _OVERLAP_FLOOR


def is_about_the_image(
    conversation_id: Optional[str], message: str, user_id: "Optional[object]" = None
) -> bool:
    """Should this text-only turn be answered with the remembered image?"""
    entry = _remembered_images.get(scope(conversation_id, user_id))
    if entry is None or not recall(conversation_id, user_id):
        return False
    text = message or ""
    if not text.strip():
        return False
    if _NAMES_AN_IMAGE.search(text):
        return True
    if _CONTINUES.search(text):
        return True
    return _overlaps(text, entry.context)


def images_for_followup(
    conversation_id: Optional[str], message: str, user_id: "Optional[object]" = None
) -> List[str]:
    """The remembered images when this turn is about them, else []."""
    if not is_about_the_image(conversation_id, message, user_id):
        return []
    return recall(conversation_id, user_id)
