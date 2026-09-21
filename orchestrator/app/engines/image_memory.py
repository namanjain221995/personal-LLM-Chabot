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

WHAT THIS IS. The bytes of the conversation's most recent image turn, with a
TTL, a per-conversation byte budget and a cap on how many conversations are
remembered at once — held in this process AND, since 2026-09-21, in one
`conversation_images` row (V41) so that a restart does not undo it.

IT USED TO BE A CACHE ONLY, AND THAT WAS THE DEFECT (completeness critic R6,
2026-09-21). This docstring said, of the process dict: "a restart loses it
and the next turn behaves exactly as it did before this module existed."
Every deploy, container restart and OOM kill therefore silently reverted the
fix for every conversation in flight, and what the person saw was the
original reported bug, word for word. Auto-deploy fires on every push to
main, so that was a daily event.

WHY THE BYTES AND NOT A DESCRIPTION. The obvious cheaper record is the one
video and documents keep: pin the transcript, not the material. It is the
right answer for them because text is what a video and a PDF ARE. It is the
wrong answer here for three measured reasons.
  * The follow-up route hands the remembered bytes to the vision engine
    (`main.py`, the `elif image_followup.images:` branch). A description
    answers "what does the third line say?" and cannot answer "what colour
    is the logo?", "how many people are in it?" or "is the stamp legible?".
    Swapping pixels for prose would have narrowed a working feature.
  * There is no description to store at Fast. `vision.run_vision_engine`
    skips the OCR pass below Think deliberately (measured 3.3 s of the 4.0 s
    to the first visible token). Storing one would mean a SECOND, speculative
    model pass on every single upload, to serve the minority of image turns
    that ever get a follow-up — and the live check this fix was measured on
    is a Fast turn.
  * A description is not cheaper in the sense that matters. It is still
    derived user content in the database, under the same TTL and the same
    deletion rules; it is only smaller. The honest way to spend less is the
    durable byte budget below, which keeps a picture the person's own
    browser already capped at 1600 px.

WHAT IS STORED, AND WHAT IS NOT. The row holds what the follow-up needs and
nothing else: the fitted image data URLs, the lowercased question+answer the
word test compares against, the turns-since counter, and `created_at`. No
filename, no upload id, no model output beyond the answer text that is
already in `messages`. It is a new copy of the person's picture, so it is
bounded (IMAGE_MEMORY_DB_CHARS), scoped by primary key to one viewer and one
conversation, deleted with the chat and with the account, and swept at the
TTL.

WHEN THE BYTES ARE GONE, SAY SO (the critic's option (b), kept as the
residual). A picture too large for the durable budget still writes its ROW,
so a later process knows a picture was there even though it cannot show it.
`followup()` then reports `unavailable`, and main.py answers "that picture is
no longer attached, please send it again" instead of answering as though
nothing had ever been attached. Note that (b) could never have been built
WITHOUT this row: a fresh process cannot tell "no picture was ever sent" from
"the picture is gone".

THE TTL RELEASES MEMORY, NOT ONLY RECALL (adversarial QA, 2026-09-18). An
expired entry used to be dropped only when ITS conversation asked again, so
the bytes stayed: 70 conversations, TTL passed, one more image -> 47 entries
and 62,666,884 characters still held. Every read and write now sweeps the
expired entries first (at most 64 of them, microseconds). The same rule
applies to the row: `hydrate` refuses and deletes an expired one on sight,
and `db.prune_conversation_images` runs on a bounded cadence from both the
read and the write path, so the TTL bounds STORAGE too.

SCOPE IS THE VIEWER AND THE CONVERSATION. `chat`'s conversation key is
whatever the client sent (`request.conversation_id or scoped_session`), so
the key here is that value PLUS the viewer's id. Two accounts that happened
to send the same conversation id — or one that guessed another's — get
different entries, and image bytes never cross an account. A call with no
viewer stores and recalls nothing (repair round 2): it used to fall back to
the bare conversation id, so any two callers that forgot the viewer would
have shared one entry.

WHAT THE CALLER OWES THIS MODULE (hand-off to main.py and history.py):
`hydrate` (awaited — it reads the database) before the word test, so a
process that has just started can still see the conversation's picture;
`note_turn` on every turn the word test is not asked about (a document, URL,
video, agent or research turn), so "that chart" after an intervening PDF turn
is not taken for the picture; and `forget` when a conversation is deleted, so
a deleted conversation's photo cannot answer the same id again inside the
TTL.
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


def max_db_chars() -> int:
    """Base64 characters written to `conversation_images` per conversation.

    Smaller than the in-process budget on purpose: RAM eviction and DISK
    retention are different problems. This is a new copy of the person's
    picture, so the durable one is the one a browser already produces —
    `MAX_IMAGE_EDGE` in frontend/lib/images.ts is 1600 px, and five such
    uploads (the per-turn maximum) are comfortably under 8 M characters
    (~6 MB). A picture above this budget is stored as the same 1600 px copy
    `_smaller_copy` already makes for the in-process budget; when even that
    cannot be produced the ROW is still written with no images, and the
    follow-up says the picture is no longer attached rather than pretending
    there never was one.
    """
    return int(_env_number("IMAGE_MEMORY_DB_CHARS", 8_000_000))


def durable_enabled() -> bool:
    """Is the V41 row written and read at all?

    An env flag and not a settings field for the same reason the numbers
    above are: nobody on this programme owns config.py. Off is the old
    process-only cache, which is what the tests of the word test want and
    what a deployment with no database (scripts, the bare engine harness)
    gets for free — every durable call already fails soft.
    """
    raw = (os.environ.get("IMAGE_MEMORY_DURABLE") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def prune_interval_s() -> float:
    """How often ONE process will sweep expired rows. The sweep is a range
    delete on an index; running it on every turn would still be a statement
    per turn for nothing, and running it never is how a TTL stops releasing
    storage."""
    return _env_number("IMAGE_MEMORY_PRUNE_INTERVAL_S", 60.0)


#: What the person is told when this conversation's picture is past what the
#: durable budget could hold. Fixed words, no model call: the app is
#: reporting its own state, and a model asked to report it would embroider.
UNAVAILABLE_NOTICE = (
    "That picture is no longer attached to this conversation, so I can't read "
    "it again. Please send it once more and ask your question with it."
)


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


# --- the durable half (V41 `conversation_images`) --------------------------
#
# Everything below fails soft, by design. This module answers a follow-up
# question a little better than the app did before it existed; a database
# that is slow, missing or mid-migration must cost exactly that improvement
# and never a turn. So every durable call is wrapped, logs at debug and
# returns, and the process cache carries on alone — which is precisely the
# behaviour this file had until 2026-09-21.


def _durable_identity(
    conversation_id: "Optional[str]", user_id: "Optional[object]"
) -> "Optional[tuple]":
    """(user_id as int, conversation_id) for the row, or None.

    The same rule as `scope`, plus the one the FOREIGN KEY adds: the row
    hangs off `users(id)`, so a viewer that is not an integer id (a script, a
    test using a label) has a process cache and no row. `scope` is still what
    keys the process dict — the two must agree on whether this call has an
    identity at all, which is why this starts from `scope`.
    """
    if not scope(conversation_id, user_id):
        return None
    try:
        return int(user_id), str(conversation_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


#: The newest durable write per key, so a write that reaches its thread after
#: a NEWER image turn's write cannot put the older picture back. Same shape
#: and same reason as `_latest` for the process cache.
_durable_latest: "dict[str, object]" = {}

#: `time.monotonic()` of this process's last expired-row sweep.
_last_prune: float = 0.0


def _db():
    """The database module, imported here and not at module import: `app.db`
    opens a connection pool and the engines are imported by tooling that has
    no database at all."""
    from .. import db

    return db


#: ONE thread, for every durable write this module makes.
#:
#: Not the default executor, and not one thread per call: these writes have to
#: happen IN ORDER. main.py's `note_turn` runs on an image turn just before
#: `remember` replaces the entry, and `forget` can arrive from the delete
#: route while a write for the same conversation is still queued. On a shared
#: pool those land in whatever order threads are scheduled, and the two
#: outcomes are a picture whose turns-since counter says it is already stale,
#: and — the one that matters — a deleted conversation's photo written back
#: after the delete. A single worker makes the queue FIFO, and it also caps
#: what this module can take from the connection pool at one connection.
_writer = None


def _writer_pool():
    global _writer
    if _writer is None:
        from concurrent.futures import ThreadPoolExecutor

        _writer = ThreadPoolExecutor(max_workers=1, thread_name_prefix="image-memory")
    return _writer


def _run_durable(fn, *args) -> None:
    """Queue a durable write on the writer thread; wait for it off the loop.

    The /chat handler calls `remember`, `note_turn` and `followup` inline from
    `async def`, and psycopg is synchronous: a commit on the loop is a commit
    every other in-flight SSE stream waits for. So with a running loop this
    returns at once and the write happens behind the turn.

    With no running loop — history.py's sync delete route, scripts, tests —
    it waits, which keeps both guarantees that callers rely on: after
    `forget(...)` returns the row is gone, and after `remember(...)` returns
    the row is there.

    Never called FROM the writer thread (that would wait on a queue only this
    thread can drain): `hydrate` reads through `asyncio.to_thread`, not here.
    """
    future = _writer_pool().submit(fn, *args)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        future.result()


def _prune_expired_rows() -> None:
    """Sweep rows past the TTL, at most once every `prune_interval_s`.

    Called from the write path and from `hydrate`, so any activity at all
    releases the storage of conversations that have gone quiet. Rate-limited
    because otherwise it is one DELETE statement per turn to find nothing.
    """
    global _last_prune
    now = time.monotonic()
    if now - _last_prune < prune_interval_s():
        return
    _last_prune = now
    try:
        gone = _db().prune_conversation_images(ttl_s())
        if gone:
            log.debug("image memory: pruned %d expired picture row(s)", gone)
    except Exception as exc:  # noqa: BLE001 — never a failed turn
        log.debug("image memory: prune failed: %s", type(exc).__name__)


def _persist(ident: "Optional[tuple]", kept: List[str], context: str) -> None:
    """Write this turn's picture to its row (see `_persist_now`)."""
    if ident is None or not durable_enabled():
        return
    key = f"u{ident[0]}:{ident[1]}"
    token = object()
    _durable_latest[key] = token
    _run_durable(_persist_now, ident, kept, context, key, token)


def _persist_now(
    ident: tuple, kept: List[str], context: str, key: str, token: object
) -> None:
    """The durable write itself, in a worker thread.

    `kept` already fits the in-process budget; the durable budget is smaller
    (see `max_db_chars`), so a picture above it is reduced HERE, in this
    thread, by the same `_smaller_copy` the in-process budget uses — measured
    143-238 ms, which is why it may not happen on the loop.

    A picture that cannot be reduced at all writes the row with NO images
    rather than no row: that is what lets a later process say "the picture is
    no longer attached" instead of answering as if none had been sent.
    """
    if _durable_latest.get(key) is not token:
        return  # a newer image turn for this conversation already won
    try:
        stored = kept if sum(map(len, kept)) <= max_db_chars() else _fit(kept, max_db_chars())
        _db().save_conversation_image(ident[0], ident[1], stored, context)
        if not stored:
            log.info(
                "image memory: picture too large for the durable budget; the "
                "next turn will ask for it again"
            )
    except Exception as exc:  # noqa: BLE001 — never a failed turn
        log.debug("image memory: could not store the picture: %s", type(exc).__name__)
    finally:
        if _durable_latest.get(key) is token:
            _durable_latest.pop(key, None)
        _prune_expired_rows()


def _forget_durable(ident: "Optional[tuple]") -> None:
    """Delete the row, and cancel a write for it that has not run yet.

    The cancellation is the half that matters: without it, a `remember`
    queued microseconds before a conversation was deleted would write the
    photo back AFTER the delete, and the next process would hydrate a
    deleted conversation's picture.
    """
    if ident is None or not durable_enabled():
        return
    _durable_latest.pop(f"u{ident[0]}:{ident[1]}", None)

    def _write() -> None:
        try:
            _db().delete_conversation_image(ident[0], ident[1])
        except Exception as exc:  # noqa: BLE001
            log.debug("image memory: could not forget the picture: %s", type(exc).__name__)

    _run_durable(_write)


def _note_durable_turn(ident: "Optional[tuple]", turns_after: int) -> None:
    """Persist the 0 -> non-zero step of the turns-since counter.

    ONLY that step. The counter feeds one decision — `just_shown`, which asks
    whether the picture is still the last thing the chat was shown — and
    every value above zero answers it the same way, so writing each later
    turn would be a statement per turn that changes nothing. Without this
    step, a restart would hand a hydrated entry `turns_after = 0` and "that
    chart" would fire ten turns after the photo, which is the wrong-fire the
    2026-09-18 adversarial round spent itself on.
    """
    if ident is None or not durable_enabled():
        return

    def _write() -> None:
        try:
            _db().touch_conversation_image_turns(ident[0], ident[1], turns_after)
        except Exception as exc:  # noqa: BLE001
            log.debug("image memory: could not count the turn: %s", type(exc).__name__)

    _run_durable(_write)


def _hydrate_read(ident: tuple) -> "Optional[dict]":
    """The row, or None — the DATABASE half of `hydrate`, in a worker thread.

    Reads and deletes only; `_remembered_images` is never touched from here.
    The store is mutated by `remember` and `_keep` on the event loop, and a
    second writer in a thread would race them: `_evict`'s read-modify-write
    loop and `move_to_end` on a key another thread has just dropped are a
    KeyError out of a live turn, for a cache.
    """
    try:
        row = _db().get_conversation_image(ident[0], ident[1])
    except Exception as exc:  # noqa: BLE001
        log.debug("image memory: could not read the picture: %s", type(exc).__name__)
        return None
    finally:
        _prune_expired_rows()
    if row is None:
        return None
    if row["age_s"] > ttl_s():
        # Expired: never served, and gone now. The TTL is measured from
        # `created_at` by the database, because a monotonic clock does not
        # survive the restart this row exists for.
        _forget_durable(ident)
        return None
    return row


async def hydrate(
    conversation_id: "Optional[str]", user_id: "Optional[object]" = None
) -> None:
    """Load this conversation's picture back into the process, if it has one.

    main.py awaits this before the word test. It is one primary-key read and
    it happens only when this process does not already hold the conversation,
    so a chat that stays on one orchestrator pays it once — on the first turn
    after a deploy, which is exactly the turn that used to lose the picture.

    Deliberately NOT folded into `recall`: `recall` is called from inside the
    word test and from `images_for_followup`, both synchronous and both on
    the event loop, and a database read belongs on neither.
    """
    if not durable_enabled():
        return
    _sweep_expired()
    key = scope(conversation_id, user_id)
    if not key or key in _remembered_images:
        return
    ident = _durable_identity(conversation_id, user_id)
    if ident is None:
        return
    row = await asyncio.to_thread(_hydrate_read, ident)
    if row is None or key in _remembered_images:
        # `key in ...` again: an image turn for this conversation may have
        # landed while the read was in flight, and it is the newer picture.
        return
    _remembered_images[key] = _Remembered(
        images=list(row["images"]),
        context=row["context"],
        # The age the DATABASE measured, translated into this process's
        # monotonic clock, so `_sweep_expired` and `recall` keep working on
        # a hydrated entry exactly as on a local one.
        at=time.monotonic() - row["age_s"],
        turns_after=row["turns_after"],
    )
    _remembered_images.move_to_end(key)
    _evict()


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

    The durable row (V41) is written from `_keep`, in a worker thread too:
    the process cache is what the NEXT request in this process reads, and it
    is written first and synchronously, so nothing waits on the database to
    get the behaviour this module already had.
    """
    _sweep_expired()
    key = scope(conversation_id, user_id)
    if not key:
        return
    images = [img for img in (images or []) if (img or "").strip()]
    if not images:
        return
    context = f"{question}\n{answer}".lower()
    ident = _durable_identity(conversation_id, user_id)
    token = object()
    _latest[key] = token
    if sum(len(img) for img in images) <= max_chars():
        _keep(key, token, _fit(images), context, ident)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        _keep(key, token, _fit(images), context, ident)
        return
    _remembered_images.pop(key, None)
    # BOTH halves drop the previous picture at once, not just this process's.
    # Otherwise, for the 143-238 ms the downscale takes, the conversation
    # holds no picture here and its OLD one in the database — and a restart
    # inside that window would hydrate the picture the person has just
    # replaced. The new row lands behind this delete (one writer thread).
    _forget_durable(ident)
    job = loop.run_in_executor(None, _fit, images)
    job.add_done_callback(lambda done: _keep_when_fitted(key, token, done, context, ident))


#: The newest remember() per key. A downscale that finishes after a newer
#: image turn (or a forget) for the same conversation is thrown away.
_latest: "dict[str, object]" = {}


def _keep_when_fitted(
    key: str, token: object, done, context: str, ident: "Optional[tuple]" = None
) -> None:
    if done.cancelled() or done.exception() is not None:
        if _latest.get(key) is token:
            _latest.pop(key, None)
        return
    _keep(key, token, done.result(), context, ident)


def _keep(
    key: str,
    token: object,
    kept: List[str],
    context: str,
    ident: "Optional[tuple]" = None,
) -> None:
    if _latest.get(key) is not token:
        return
    _latest.pop(key, None)
    if not kept:
        return
    _remembered_images[key] = _Remembered(images=kept, context=context)
    _remembered_images.move_to_end(key)
    _evict()
    _persist(ident, kept, context)


def _fit(images: Sequence[str], budget: "Optional[int]" = None) -> List[str]:
    """The images that fit a byte budget, in order; one over the budget on
    its own is kept as a smaller copy.

    `budget` defaults to the per-conversation process budget; the durable
    write passes the smaller `max_db_chars()` (see `_persist_now`).
    """
    kept: List[str] = []
    budget = max_chars() if budget is None else budget
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

        with Image.open(io.BytesIO(payload), formats=_IMAGE_FORMATS) as im:
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
    """Drop the least recently used conversations until the store fits.

    This process's memory bound only: the ROW stays, and `hydrate` brings an
    evicted conversation's picture back on its next turn. Eviction is this
    process saying it cannot hold everything at once, not the conversation
    saying it is finished with its photo — that is `forget` and the TTL.
    """
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
    """Drop the conversation's picture now — both halves (and any downscale
    still running for it). history.py's delete calls this; see the module
    docstring. The row goes too, or deleting a chat would only hide its photo
    until the next restart hydrated it back."""
    key = scope(conversation_id, user_id)
    _remembered_images.pop(key, None)
    _latest.pop(key, None)
    _forget_durable(_durable_identity(conversation_id, user_id))


def note_turn(conversation_id: Optional[str], user_id: "Optional[object]" = None) -> None:
    """Count a turn the word test was not asked about (a document, URL,
    video, agent or research turn). After it, the picture is no longer the
    last thing shown, so "that chart" may be the PDF's. main.py should call
    this wherever it skips `images_for_followup`; see the module docstring."""
    entry = _remembered_images.get(scope(conversation_id, user_id))
    if entry is None:
        return
    entry.turns_after += 1
    if entry.turns_after == 1:
        _note_durable_turn(_durable_identity(conversation_id, user_id), 1)


def clear() -> None:
    """Test hook: forget every picture, in BOTH halves.

    Still means "nothing was ever remembered", because that is what the
    tests of the word test use it for — and since V41 that has to reach the
    rows as well, or a cleared test would hydrate the previous one's photo.

    A RESTART is a different event and has no hook: it is the two dicts below
    being empty and the rows still there, which is what
    tests/test_image_memory_restart.py reproduces directly.
    """
    _remembered_images.clear()
    _latest.clear()
    _durable_latest.clear()
    if not durable_enabled():
        return
    try:
        _db().prune_conversation_images(0.0)
    except Exception as exc:  # noqa: BLE001
        log.debug("image memory: could not clear the rows: %s", type(exc).__name__)


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
# where it went before. (It does NOT sit above everything: Salesforce
# Intelligence Mode and Artifact Studio are decided before the chain and can
# return without reaching it. The Salesforce gate now excludes a turn this
# module claims - `Followup.about_the_picture`, 2026-09-21 - because
# `sf_outcome.handled` answers straight away.) A wrong fire costs a wrong
# answer (and puts the
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
#: Formats a chat picture may be; ICO/CUR decode their embedded PNG inside
#: Image.open, before the pixel ceiling can run (review 2026-09-19).
_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP", "GIF", "BMP", "TIFF")
_TALKED_ABOUT_THINGS = (
    "note|notes|invoice|invoices|bill|ticket|table|tables|chart|charts|graph|"
    "graphs|figure|figures|diagram|diagrams|drawing|form|page|map|card|menu|"
    "dashboard|letter|list|board|meter|row|rows|column|columns|legend|axis"
)
#: "the best chart TYPE for sign-ups" asks about charts in general, not the
#: picture (review 2026-09-19: it went chat -> vision after a chart turn).
_NOT_A_TOPIC = (
    r"(?!\s+(?:types?|kinds?|formats?|templates?|styles?|tools?|apps?|software|"
    r"librar(?:y|ies))\b)"
)
_BACK_REFERENCE = re.compile(
    r"\b(?:the|that|this|those|these)\s+(?:[\w'-]+\s+){0,2}?"
    r"(" + _SEEN_THINGS + "|" + _TALKED_ABOUT_THINGS + r")\b" + _NOT_A_TOPIC,
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
    r"(" + _SEEN_THINGS + "|" + _TALKED_ABOUT_THINGS + r")\b" + _NOT_A_TOPIC,
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


@dataclass
class Followup:
    """What a text-only turn should do about the conversation's picture.

    Three outcomes, and main.py needs all three kept apart:
      * `images` — this turn is about the picture and here it is;
      * `unavailable` — this turn is about the picture and the picture is
        gone (it was over the durable budget, so only its row survived the
        restart). The turn is answered by saying so, which is the critic's
        option (b) and the only case left where it applies;
      * neither — this turn is not about a picture at all, and routes exactly
        as it did before this module existed.
    """

    images: List[str] = field(default_factory=list)
    unavailable: bool = False

    @property
    def about_the_picture(self) -> bool:
        """The word test fired: this turn belongs to the image route, whether
        or not the bytes are still there. What every gate ABOVE the answer
        chain has to check, so that a turn about a photo is not claimed by
        something that cannot see one."""
        return bool(self.images) or self.unavailable


def followup(
    conversation_id: Optional[str], message: str, user_id: "Optional[object]" = None
) -> Followup:
    """The picture this text-only turn should be answered with, if any.

    `hydrate` must have been awaited first for a conversation this process
    has not seen; everything here is in-memory and safe on the event loop.
    """
    key = scope(conversation_id, user_id)
    entry = _remembered_images.get(key)
    if entry is None:
        return Followup()
    images = recall(conversation_id, user_id)
    if key not in _remembered_images:
        return Followup()  # recall found it expired and dropped it
    text = message or ""
    just_shown = entry.turns_after == 0
    # Every turn main.py asks about counts, fired or not: after one more
    # text turn "that chart" may be a chart the assistant made in between.
    # (Turns main.py does not ask about are counted by `note_turn`.)
    entry.turns_after += 1
    if entry.turns_after == 1:
        _note_durable_turn(_durable_identity(conversation_id, user_id), 1)
    if not _points_at_the_picture(text, entry.context, just_shown=just_shown):
        return Followup()
    return Followup(images=images, unavailable=not images)


def is_about_the_image(
    conversation_id: Optional[str], message: str, user_id: "Optional[object]" = None
) -> bool:
    """Should this text-only turn be answered with the remembered image?"""
    return bool(followup(conversation_id, message, user_id).images)


def images_for_followup(
    conversation_id: Optional[str], message: str, user_id: "Optional[object]" = None
) -> List[str]:
    """The remembered images when this turn is about them, else []."""
    return followup(conversation_id, message, user_id).images
