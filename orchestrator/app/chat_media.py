"""Chat media: every picture sent in a chat, stored on this server for the life
of the chat (V44, 2026-10-02; docs/chat-media/CONTRACT.md).

THE DEFECT. A photo sent from a phone showed above the person's message on
the phone. The same chat opened on a desktop, same account, showed the
question and the answer and no photo. The picture's only lasting copy was the
sending browser's IndexedDB: the bytes reached this server inline in the /chat
body, `main._request_snapshot` strips them, and `engines/image_memory.py`
keeps a chat's latest picture for two hours for the MODEL's follow-ups, never
for display. Production chat 4ab7ac45 had a vision answer and no bytes
anywhere: no uploads row, no conversation_images row, no file.

WHAT THIS MODULE IS. The store, its routes and its housekeeping:
  * POST /chat-media/{conversation}   store up to MAX_FILES pictures (upload
                                      route; a browser backfilling the photos
                                      it still holds from before V44 uses it);
  * GET  /chat-media/{conversation}   the viewer's pictures in that chat;
  * GET  /chat-media/{conversation}/{attachment}?size=thumb|full
                                      the bytes, privately cacheable forever;
  * `schedule_inline_store`           /chat's hook: every inline picture of a
                                      turn is stored behind the turn;
  * `load_refs`                       /chat's `image_refs`: a regenerate, edit
                                      or retry on a device without the bytes;
  * `latest_turn_images`              image_memory's fallback after its TTL;
  * `erase_conversation`, `reap_once` deletion and its backstop.

WHAT IS STORED, AND WHERE. A V44 `chat_media` row per picture (owner, chat,
the composer's attachment id, sha256, type, size, dimensions) and the bytes as
FILES under CHAT_MEDIA_DIR/<user_id>/<conversation_id>/<media_id>/:
    full.<ext>   the picture exactly as it arrived (jpg, png, webp or gif)
    thumb.webp   512 px on the long edge, EXIF orientation applied, made only
                 for a picture over 512 px or 200 KiB; the chat bubble asks
                 for this one and never pays for a 10 MiB original
Directories 0700, files 0600, outside WORKSPACE_DIR so the 24 h sweep and the
20 GB quota never touch them.

THE ATTACHMENT ID IS THE REFERENCE. The composer mints it when a picture is
chosen and the browser writes it into the user message's `meta.images`; the
display URL is derived from it and never stored. The server never writes that
reference into a message itself: the history PUT replaces meta whole, last
writer wins, so the next push from any tab would erase it.

THE FIRST WRITE WINS. Files go to a temporary name, are fsynced and renamed,
the directory is fsynced, and only THEN is the row inserted. UNIQUE (user,
chat, attachment) decides a race: the loser removes its files and gets the
first write back unchanged. The bytes behind a URL therefore never change,
which is what makes `Cache-Control: immutable` safe, and a retried send, a
second tab's backfill, and /chat and the upload route storing the same picture
all end as one row.

ONLY VERIFIED RASTERS. The magic bytes AND a Pillow decode must agree on JPEG,
PNG, WebP or GIF, and the decode must reach the end of the data (a truncated
file fails there). SVG, HTML renamed .png, HEIC, BMP and TIFF are refused: the
bytes route serves INLINE, and a raster a browser cannot read as a document is
the only thing safe to serve that way (nosniff and a sandbox CSP as well).

OWNERSHIP. Every read carries the viewer: the row's user_id must be the
viewer, and the chat must be the viewer's or have no row yet (a brand-new chat
whose first history push has not landed). Anything else, the F034 reserved
`u<digits>-` shape included, is one 404 with one body, so neither a chat id
nor an attachment id is an oracle.

WHAT IT COSTS A SEND: nothing on the loop. /chat stores in a background task,
every decode, hash and write runs in a worker thread, and a failure is logged
and counted, never a chat error. Pillow's decompression-bomb guard stays on;
a decode is bounded in pixels (MAX_STORE_PIXELS) and in concurrency (the
DECODE_WORKERS pool), so a burst of tiny-on-the-wire, huge-in-memory pictures
queues instead of exhausting the head node's memory.

DELETION. A deleted chat's rows go in its delete transaction (db._SIDE_TABLES),
its directory right after (history.py, best effort), and the reaper removes
what is left: rows whose chat or account is gone, and directories no row
names, both only past CHAT_MEDIA_ORPHAN_GRACE_H, because /chat stores a
picture BEFORE the browser's first history push creates the chat's row.
Nothing here promises an erasure time.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import functools
import hashlib
import io
import logging
import os
import re
import shutil
import stat as stat_mode
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from . import db, metrics
from .auth import UserRow, require_user
from .config import settings

router = APIRouter(prefix="/chat-media", tags=["chat-media"])

log = logging.getLogger(__name__)

#: The ceilings per picture and per request. MAX_IMAGE_BYTES applies to the
#: bytes actually sent, which the composer has already shrunk to 1600 px
#: (frontend/components/Composer.tsx): the ORIGINAL photo may be any size.
#: MAX_FILES is main.MAX_IMAGES, the technical ceiling of pictures one
#: message may carry: no limit in the app since 2026-10-03 (5, then 20,
#: before; docs/chat-media/LIMITS.md), 999 being vLLM's per-prompt maximum.
#: What bounds one POST is its bytes (BATCH_BUDGET_BYTES under the 64 MiB
#: body cap), not its count. Spelled out, not imported: main imports this
#: module.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILES = 999
#: Picture bytes one request may carry inline: the browser's batch budget for
#: a POST here, and for the inline images of one /chat body (as base64
#: characters there). A send over it goes by reference (`image_refs`). Well
#: under Cloudflare's 100 MB request wall; main's 64 MiB body cap for this
#: route is this plus the framing of MAX_FILES parts (a test pins that).
BATCH_BUDGET_BYTES = 48 * 1024 * 1024

#: The chat bubble's rendition: long edge, the size past which one is made,
#: and its WebP quality.
THUMB_EDGE = 512
THUMB_OVER_BYTES = 200 * 1024
THUMB_QUALITY = 80

#: The closed vocabularies of the V44 CHECK and of app/metrics.py, which
#: tests/test_chat_media_api.py pins to these.
SOURCES = ("chat", "upload", "backfill")
WRITE_RESULTS = ("stored", "duplicate", "unsupported", "too_large", "no_space", "error", "unlinked")
SIZES = ("thumb", "full")
READ_RESULTS = ("ok", "not_modified", "not_found", "missing")

#: The headers of every byte response (CONTRACT §4.3). Private because the
#: picture is the person's; immutable because a URL's bytes never change (the
#: first write wins). nosniff and the sandbox CSP because the orchestrator's
#: port is reachable without the frontend that would otherwise add them.
CACHE_CONTROL = "private, max-age=31536000, immutable"
CONTENT_SECURITY_POLICY = "default-src 'none'; sandbox"

#: The composer's id for an attachment: crypto.randomUUID(), the backfill's
#: `bf-<32 hex>`, or the server's own `ix-<intent>-<index>` (below).
ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
#: A send intent the server names pictures by: the composer's newIntentId(),
#: crypto.randomUUID() without its dashes. `ix-<intent>-<index>` is then 37
#: characters, inside ATTACHMENT_ID_RE.
_MINT_INTENT_RE = re.compile(r"^[0-9a-f]{32}$")
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: F034's reserved bare-call key, `u<user id>-<session id>`. The same
#: expression lives in main.py, uploads.py and history.py; spelled out again
#: for the reason they give (main imports this module). If one changes,
#: change all four.
_SYNTHETIC_CONV_KEY_RE = re.compile(r"^u\d+-")
_MEDIA_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: Pillow format -> (the row's mime, the stored file's extension).
_FORMATS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
    "GIF": ("image/gif", "gif"),
}
_EXT_BY_MIME = {mime: ext for mime, ext in _FORMATS.values()}

#: Pixels this store will decode, read from the header before anything is
#: decoded; above it a picture is refused as `unsupported`. One ceiling for
#: every format, JPEG included: `draft` does not bound a PROGRESSIVE JPEG's
#: coefficient buffer. The security review (2026-10-02) measured the old
#: ceilings (40 MP, 89 MP for JPEG) at +601 MiB peak for ONE 38-byte lossless
#: WebP of 16383x2440, +505 MiB for a 1 MiB progressive 9450x9450 JPEG, and
#: +9.5 GiB for 16 at once. The composer sends at most 1600 px on the long
#: edge (2.6 MP); 16 MP still takes a 12 MP phone photo whose downscale failed.
MAX_STORE_PIXELS = 16_000_000

#: Decodes (verification, thumbnail, image_memory's fit of a stored original)
#: run on their own small pool, so a burst of pictures queues instead of
#: decoding at once: peak memory is DECODE_WORKERS decodes, and the default
#: executor every other `asyncio.to_thread` shares is never filled with them.
DECODE_WORKERS = 2
_decode_executor: Optional[concurrent.futures.ThreadPoolExecutor] = None
_decode_executor_lock = threading.Lock()

#: An inline data URL's prefix (what the composer sends), or none.
_DATA_URL_PREFIX = re.compile(r"^data:[\w.+/-]*;base64,", re.I)

#: The form parser's bounds: MAX_FILES pictures, and beside them as many ids
#: and a source, with the slack the five-picture form had (16 fields for 6).
#: Every extra part is another spool file or another string in memory, and
#: the 64 MiB body cap bounds them all: 1,010 fields of at most 4 KiB is
#: under 4 MiB, and the files are the batch's own bytes.
_FORM_MAX_FIELDS = MAX_FILES + 11
_FORM_MAX_FIELD_BYTES = 4 * 1024

#: The reaper's first pass waits this long after start-up, so a deploy's
#: first minutes are spent serving, not walking directories.
_REAP_FIRST_DELAY_S = 300.0
#: Rows the reaper deletes per pass, so one pass is one bounded statement.
_REAP_ROW_BATCH = 1000

_COLUMNS = (
    "media_id, user_id, conversation_id, attachment_id, sha256, mime, bytes, "
    "width, height, has_thumb, source, created_at"
)


# ---------------------------------------------------------------- refusals --


class Refused(Exception):
    """A picture this store will not keep. `result` is its metric word."""

    def __init__(self, result: str) -> None:
        super().__init__(result)
        self.result = result


def _error(status: int, code: str, detail: str) -> JSONResponse:
    """The flat body every refusal of these routes has: {code, detail}."""
    return JSONResponse(status_code=status, content={"code": code, "detail": detail})


def _not_found() -> JSONResponse:
    # One answer for a chat that is someone else's, a reserved key, a
    # malformed id and a picture that never existed.
    return _error(404, "not_found", "No such picture.")


def _valid_conversation(conversation_id: Optional[str]) -> bool:
    return bool(
        conversation_id
        and _CONVERSATION_ID_RE.fullmatch(conversation_id)
        and not _SYNTHETIC_CONV_KEY_RE.match(conversation_id)
    )


# ------------------------------------------------------------------- files --


def media_root() -> str:
    return settings.chat_media_dir


def conversation_dir(user_id: int, conversation_id: str) -> str:
    """<root>/<user>/<conversation>. Only ever built from a validated id, so
    nothing a client sends becomes a path component it chose."""
    if not _CONVERSATION_ID_RE.fullmatch(conversation_id or ""):
        raise ValueError("conversation id must be 1-64 characters of [A-Za-z0-9_-]")
    return os.path.join(media_root(), str(int(user_id)), conversation_id)


def media_dir(user_id: int, conversation_id: str, media_id: str) -> str:
    if not _MEDIA_ID_RE.fullmatch(media_id or ""):
        raise ValueError("media id must be 32 hex characters")
    return os.path.join(conversation_dir(user_id, conversation_id), media_id)


def file_path(row: Dict[str, Any], size: str = "full") -> str:
    """The file a request for `size` is served from. A picture with no
    thumbnail is its own thumbnail (CONTRACT §3)."""
    directory = media_dir(row["user_id"], row["conversation_id"], row["media_id"])
    if size == "thumb" and row["has_thumb"]:
        return os.path.join(directory, "thumb.webp")
    return os.path.join(directory, f"full.{_EXT_BY_MIME[row['mime']]}")


def _private_dir(path: str) -> None:
    """mkdir with 0700 whatever the umask; an existing one keeps its mode."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)


def _make_media_dir(user_id: int, conversation_id: str, media_id: str) -> str:
    """Every level 0700: os.makedirs gives the intermediate ones the umask."""
    target = media_dir(user_id, conversation_id, media_id)
    root = media_root()
    os.makedirs(root, mode=0o700, exist_ok=True)
    for level in (
        os.path.join(root, str(int(user_id))),
        conversation_dir(user_id, conversation_id),
        target,
    ):
        _private_dir(level)
    return target


def _write_file(directory: str, name: str, data: bytes) -> None:
    """Write `name` in `directory` atomically: a temporary name, fsync, rename.
    A crash leaves the temporary file, which no row names and the reaper
    removes; it never leaves a half-written `name`."""
    final = os.path.join(directory, name)
    tmp = os.path.join(directory, f".{name}.{uuid.uuid4().hex[:8]}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        _unlink_quietly(tmp)
        raise
    os.close(fd)
    os.replace(tmp, final)


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _unlink_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def free_bytes() -> Optional[int]:
    """Free space on the filesystem that holds CHAT_MEDIA_DIR (its nearest
    existing ancestor before the first write), or None when it cannot be
    measured."""
    probe = media_root()
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        return int(shutil.disk_usage(probe or "/").free)
    except OSError:
        return None


def has_room() -> bool:
    """Above the CHAT_MEDIA_MIN_FREE_GIB floor. Unmeasurable counts as room:
    the write itself then fails loudly, which is better than refusing every
    picture because statvfs did."""
    free = free_bytes()
    floor = int(float(settings.chat_media_min_free_gib) * 1024 ** 3)
    return free is None or free >= floor


# ------------------------------------------------------------- inspection --


def _decode_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _decode_executor
    with _decode_executor_lock:
        if _decode_executor is None:
            _decode_executor = concurrent.futures.ThreadPoolExecutor(
                max_workers=DECODE_WORKERS, thread_name_prefix="chat-media-decode"
            )
        return _decode_executor


async def run_decode(fn, *args):
    """Await `fn(*args)` on the decode pool (see DECODE_WORKERS). For any
    call that decodes a picture: `inspect`, a background store, image_memory's
    read of a stored original."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_decode_pool(), functools.partial(fn, *args))


@dataclass(frozen=True)
class Picture:
    """A verified raster, ready to store."""

    mime: str
    ext: str
    #: As the picture DISPLAYS: EXIF orientations 5-8 swap the two, which is
    #: what a browser does with the full image, so the bubble that reserves
    #: width x height does not jump when it loads.
    width: int
    height: int
    sha256: str
    thumb: Optional[bytes]


def _sniff(data: bytes) -> Optional[str]:
    """The format the first bytes claim, or None for anything else."""
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return None


#: The last chunk of every PNG, with its CRC (which is constant: IEND is empty).
_PNG_IEND = b"IEND\xaeB`\x82"


def _gif_reaches_trailer(data: bytes) -> bool:
    """Walk a GIF's blocks to its trailer (0x3B). A cut file runs out first.
    Every block is length-prefixed, so this is ~40,000 steps for 10 MiB."""
    if len(data) < 13:
        return False
    pos = 13
    flags = data[10]
    if flags & 0x80:  # a global colour table follows the screen descriptor
        pos += 3 * (2 << (flags & 0x07))
    while pos < len(data):
        block = data[pos]
        if block == 0x3B:
            return True
        if block == 0x21:  # extension: its label, then data sub-blocks
            pos += 2
        elif block == 0x2C:  # image: 10-byte descriptor, local table, LZW code size
            if pos + 10 > len(data):
                return False
            local = data[pos + 9]
            pos += 10
            if local & 0x80:
                pos += 3 * (2 << (local & 0x07))
            pos += 1
        else:
            return False
        while True:  # sub-blocks, up to the zero-length terminator
            if pos >= len(data):
                return False
            size = data[pos]
            pos += 1 + size
            if size == 0:
                break
    return False


#: In a JPEG's entropy-coded data every FF is followed by 00 (a stuffed byte),
#: a restart marker D0-D7, or another FF (fill before a marker). Anything else
#: is the next marker.
_JPEG_NEXT_MARKER = re.compile(rb"\xff[^\x00\xd0-\xd7\xff]")


def _jpeg_reaches_eoi(data: bytes) -> bool:
    """Walk a JPEG's marker segments from SOI to the EOI that ends the
    PRIMARY picture. A cut file runs out first. Bytes after that EOI are not
    looked at: an MPO's further pictures, a phone's motion-photo MP4 or
    vendor trailer, all of which browsers ignore (an `rfind` over the whole
    buffer used to find an FF DA inside such a trailer and refuse the photo).
    Every segment is length-prefixed, and each scan's data is skipped by one
    regex search, so this is a few hundred steps for 10 MiB."""
    n = len(data)
    pos = 2  # after SOI
    while pos + 1 < n:
        if data[pos] != 0xFF:
            # Junk between segments, which Pillow skips too.
            pos = data.find(b"\xff", pos)
            if pos < 0:
                return False
            continue
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker == 0xD9:  # EOI
            return True
        if marker in (0x00, 0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # no length
            pos += 2
            continue
        if pos + 4 > n:
            return False
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        if length < 2:
            return False
        pos += 2 + length
        if marker == 0xDA:  # start of scan: its data runs to the next marker
            following = _JPEG_NEXT_MARKER.search(data, pos)
            if following is None:
                return False
            pos = following.start()
    return False


def _ends_whole(fmt: str, data: bytes) -> bool:
    """Does the data END the way a whole file of this format ends?

    Judged from the file's own structure, never by Pillow alone: Pillow's
    truncation check is the process-wide switch ImageFile.LOAD_TRUNCATED_IMAGES,
    and WeasyPrint turns it ON when it is imported (weasyprint/images.py), which
    the artifact renderer does in this same process. With it on (measured
    2026-10-02, Pillow 12.3), a JPEG cut to a third and a GIF cut anywhere
    decoded "successfully", and a PNG cut inside its last chunk did too.
    Flipping the switch back around our own decode would race the renderer's
    threads, so the check does not touch it.

      JPEG  the segment walk reaches the EOI (FF D9) of the primary picture
            (`_jpeg_reaches_eoi`). EOI cannot occur inside entropy-coded
            data, where every FF is followed by 00 or a restart marker, so a
            scan cut short never reaches one.
      PNG   the IEND chunk, CRC included.
      GIF   the block walk reaches the trailer.
      WebP  libwebp decodes the whole buffer itself and refuses a short one
            whatever the switch says (measured); its RIFF size is checked
            anyway, because it costs nothing.
    """
    if fmt == "JPEG":
        return _jpeg_reaches_eoi(data)
    if fmt == "PNG":
        return data.rfind(_PNG_IEND) > 8
    if fmt == "GIF":
        return _gif_reaches_trailer(data)
    if fmt == "WEBP":
        return len(data) >= 8 + int.from_bytes(data[4:8], "little")
    return False


def _orientation(image) -> int:
    try:
        return int(image.getexif().get(0x0112, 1) or 1)
    except Exception:  # noqa: BLE001 — a broken EXIF block is no orientation
        return 1


def _thumbnail(image) -> Optional[bytes]:
    """512 px WebP of an already decoded picture: EXIF orientation applied,
    alpha kept, the first frame of a GIF. None when it cannot be made; the
    picture is still stored and `size=thumb` then serves the full file."""
    try:
        from PIL import Image, ImageOps

        frame = ImageOps.exif_transpose(image)
        if frame.mode in ("I;16", "I;16B", "I;16L", "I;16N", "I"):
            # 16-bit greyscale (PNG): a straight RGB convert clips every value
            # over 255, so a near-black picture thumbnailed as pure white.
            frame = frame.convert("I").point(lambda v: v * (1 / 256)).convert("L")
        if frame.mode in ("RGBA", "LA", "PA") or (
            frame.mode == "P" and "transparency" in frame.info
        ):
            frame = frame.convert("RGBA")
        elif frame.mode != "RGB":
            frame = frame.convert("RGB")
        frame.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        frame.save(out, format="WEBP", quality=THUMB_QUALITY)
        return out.getvalue()
    except Exception as exc:  # noqa: BLE001
        log.warning("chat media: thumbnail not made: %s", type(exc).__name__)
        return None


def inspect(data: bytes) -> Picture:
    """Verify `data` is a JPEG, PNG, WebP or GIF and measure it, or raise
    Refused. CPU work: call it through `run_decode` (the bounded pool).

    Four agreements, not one. The magic bytes name a format; the data must
    end as a whole file of that format ends (`_ends_whole`, which is where a
    truncated file fails whatever Pillow's process-wide switch says); Pillow,
    allowed to open ONLY that format, must agree and pass `verify()`; and a
    real decode must succeed. The thumbnail is made from that same decode, so
    a large picture is decoded once.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise Refused("too_large")
    fmt = _sniff(data)
    if fmt is None or not _ends_whole(fmt, data):
        raise Refused("unsupported")
    from PIL import Image

    # Pillow's JPEG opener answers MPO for a JPEG whose MPF names more than
    # one picture (many camera and phone photos); browsers show its first
    # picture, which is the one decoded here, so it is stored as a JPEG.
    allowed = ("JPEG", "MPO") if fmt == "JPEG" else (fmt,)
    try:
        with Image.open(io.BytesIO(data), formats=(fmt,)) as image:
            # The header's size is judged before anything is verified or
            # decoded (MAX_STORE_PIXELS).
            width, height = image.size
            if (
                (image.format or "").upper() not in allowed
                or width < 1
                or height < 1
                or width * height > MAX_STORE_PIXELS
            ):
                raise Refused("unsupported")
            image.verify()
        with Image.open(io.BytesIO(data), formats=(fmt,)) as image:
            if (image.format or "").upper() not in allowed or image.size != (width, height):
                raise Refused("unsupported")
            orientation = _orientation(image)
            wants_thumb = max(width, height) > THUMB_EDGE or len(data) > THUMB_OVER_BYTES
            if fmt == "JPEG" and wants_thumb:
                # Decode at 1/2..1/8 scale, never below the thumbnail's edge:
                # measured 140 ms for a 24 MP photo against a full decode's
                # ~0.5 s. Truncation is still detected; every scan is read.
                image.draft("RGB", (THUMB_EDGE, THUMB_EDGE))
            image.load()
            thumb = _thumbnail(image) if wants_thumb else None
    except Refused:
        raise
    except Exception as exc:  # noqa: BLE001 — every decoder failure is "not a picture we keep"
        log.debug("chat media: not a verifiable %s: %s", fmt, type(exc).__name__)
        raise Refused("unsupported") from None
    if orientation in (5, 6, 7, 8):
        width, height = height, width
    mime, ext = _FORMATS[fmt]
    return Picture(
        mime=mime,
        ext=ext,
        width=int(width),
        height=int(height),
        sha256=hashlib.sha256(data).hexdigest(),
        thumb=thumb,
    )


def decode_inline(value: str) -> bytes:
    """The bytes of one inline /chat image (a data URL or bare base64), or
    Refused. The length is judged BEFORE decoding, so an oversized body never
    allocates its decoded copy."""
    raw = (value or "").strip()
    prefix = _DATA_URL_PREFIX.match(raw)
    if prefix:
        raw = raw[prefix.end():]
    if len(raw) > (MAX_IMAGE_BYTES + 2) // 3 * 4 + 64:
        raise Refused("too_large")
    try:
        return base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError):
        raise Refused("unsupported") from None


# -------------------------------------------------------------------- rows --


def _row_dict(row: Any) -> Dict[str, Any]:
    out = dict(row)
    out["user_id"] = int(out["user_id"])
    out["bytes"] = int(out["bytes"])
    return out


def get_row(user_id: int, conversation_id: str, attachment_id: str) -> Optional[Dict[str, Any]]:
    """The viewer's picture by its attachment id, or None. There is
    deliberately no read by conversation or attachment alone."""
    with db.read_connection() as con:
        row = con.execute(
            f"SELECT {_COLUMNS} FROM chat_media "
            "WHERE user_id = %s AND conversation_id = %s AND attachment_id = %s",
            (int(user_id), conversation_id, attachment_id),
        ).fetchone()
    return _row_dict(row) if row else None


def rows_by_attachment(
    user_id: int, conversation_id: str, attachment_ids: Sequence[str]
) -> Dict[str, Dict[str, Any]]:
    if not attachment_ids:
        return {}
    with db.read_connection() as con:
        rows = con.execute(
            f"SELECT {_COLUMNS} FROM chat_media "
            "WHERE user_id = %s AND conversation_id = %s AND attachment_id = ANY(%s)",
            (int(user_id), conversation_id, list(attachment_ids)),
        ).fetchall()
    return {r["attachment_id"]: _row_dict(r) for r in rows}


def list_rows(user_id: int, conversation_id: str) -> List[Dict[str, Any]]:
    with db.read_connection() as con:
        rows = con.execute(
            f"SELECT {_COLUMNS} FROM chat_media "
            "WHERE user_id = %s AND conversation_id = %s ORDER BY created_at, media_id",
            (int(user_id), conversation_id),
        ).fetchall()
    return [_row_dict(r) for r in rows]


def _insert_row(
    media_id: str,
    user_id: int,
    conversation_id: str,
    attachment_id: str,
    picture: Picture,
    size: int,
    source: str,
) -> Optional[Dict[str, Any]]:
    """The new row, or None when (user, chat, attachment) already had one —
    the first write wins, and the caller removes its own files."""
    with db.connection() as con:
        row = con.execute(
            "INSERT INTO chat_media (media_id, user_id, conversation_id, attachment_id, "
            "sha256, mime, bytes, width, height, has_thumb, source) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT chat_media_owner_attachment DO NOTHING "
            f"RETURNING {_COLUMNS}",
            (
                media_id,
                int(user_id),
                conversation_id,
                attachment_id,
                picture.sha256,
                picture.mime,
                int(size),
                picture.width,
                picture.height,
                picture.thumb is not None,
                source,
            ),
        ).fetchone()
    return _row_dict(row) if row else None


def _may_see_chat(user_id: int, conversation_id: str) -> bool:
    """The chat is the viewer's, or nobody's yet (a brand-new chat whose
    first history push has not created its row). Rows are scoped to the
    viewer on top of this, so an unowned chat shows each person only theirs."""
    if not _valid_conversation(conversation_id):
        return False
    owner = db.conversation_owner(conversation_id)
    return owner is None or owner == int(user_id)


def lookup(user_id: int, conversation_id: str, attachment_id: str) -> Optional[Dict[str, Any]]:
    """The row a byte route may serve to this viewer, or None for every kind
    of no. Two point reads; call it from a worker thread."""
    if not ATTACHMENT_ID_RE.fullmatch(attachment_id or ""):
        return None
    if not _may_see_chat(user_id, conversation_id):
        return None
    return get_row(user_id, conversation_id, attachment_id)


# ------------------------------------------------------------------- store --


def _commit(
    user_id: int,
    conversation_id: str,
    attachment_id: str,
    data: bytes,
    picture: Picture,
    source: str,
) -> Tuple[bool, Dict[str, Any]]:
    """Files first, then the row (see the module docstring). Returns
    (created, row): created False when another write won."""
    media_id = uuid.uuid4().hex
    directory = _make_media_dir(user_id, conversation_id, media_id)
    try:
        _write_file(directory, f"full.{picture.ext}", data)
        if picture.thumb is not None:
            _write_file(directory, "thumb.webp", picture.thumb)
        _fsync_dir(directory)
        _fsync_dir(os.path.dirname(directory))
        row = _insert_row(media_id, user_id, conversation_id, attachment_id, picture, len(data), source)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    if row is not None:
        return True, row
    shutil.rmtree(directory, ignore_errors=True)
    existing = get_row(user_id, conversation_id, attachment_id)
    if existing is None:
        # The winner's row went between the conflict and this read (the chat
        # was deleted in that instant). Nothing is stored; say so.
        raise RuntimeError("chat media row vanished after a conflict")
    return False, existing


def _file_present(row: Dict[str, Any]) -> bool:
    """Is the row's full file on disk? One stat."""
    try:
        return stat_mode.S_ISREG(os.stat(file_path(row, "full")).st_mode)
    except (OSError, ValueError, KeyError):
        return False


def _heal(row: Dict[str, Any], data: bytes) -> bool:
    """Write a row's files again from a retry of the SAME bytes, when the
    files are gone (a restored volume, a manual cleanup, a chat erase that
    raced the store). Only the same bytes: the sha256 is the ETag and the
    URL is cached as immutable, so different bytes under the same id stay
    refused (the row keeps answering 410). True when the files were written.
    Never raises; decodes, so it runs where `inspect` runs."""
    try:
        if hashlib.sha256(data).hexdigest() != row["sha256"] or not has_room():
            return False
        picture = inspect(data)
        full = file_path(row, "full")
        directory = _make_media_dir(row["user_id"], row["conversation_id"], row["media_id"])
        _write_file(directory, os.path.basename(full), data)
        if row["has_thumb"] and picture.thumb is not None:
            _write_file(directory, "thumb.webp", picture.thumb)
        _fsync_dir(directory)
        _fsync_dir(os.path.dirname(directory))
    except Refused:
        return False
    except Exception:  # noqa: BLE001 — a heal is a retry's bonus, never its failure
        log.warning("chat media: could not restore a stored picture's files", exc_info=True)
        return False
    log.info("chat media: restored the files of a stored picture from a retry")
    return True


def _count_write(source: str, result: str) -> None:
    metrics.inc(
        "chat_media_writes_total",
        "chat media: pictures written, by who sent them and how the write ended",
        source=source,
        result=result,
    )


def store_bytes(
    user_id: int,
    conversation_id: str,
    attachment_id: str,
    data: bytes,
    source: str,
    *,
    looked_up: bool = False,
) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Store one picture end to end and count it: (result, row). The row is
    the stored one for `stored` and `duplicate`, None for a refusal.

    The existing row is looked for FIRST (unless the caller just did,
    `looked_up`): a retry of a send or a second tab's backfill is the common
    duplicate, and it should cost one indexed read, not a decode."""
    if not looked_up:
        existing = get_row(user_id, conversation_id, attachment_id)
        if existing is not None:
            result = "duplicate" if _file_present(existing) or not _heal(existing, data) else "stored"
            _count_write(source, result)
            return result, existing
    started = time.monotonic()
    try:
        if len(data) > MAX_IMAGE_BYTES:
            raise Refused("too_large")
        if not has_room():
            raise Refused("no_space")
        picture = inspect(data)
        created, row = _commit(user_id, conversation_id, attachment_id, data, picture, source)
    except Refused as refused:
        _count_write(source, refused.result)
        return refused.result, None
    result = "stored" if created else "duplicate"
    _count_write(source, result)
    if created:
        metrics.observe(
            "chat_media_write_seconds",
            time.monotonic() - started,
            "chat media: seconds to verify and durably store one picture",
            source=source,
        )
    return result, row


def _store_inline_one(user_id: int, conversation_id: str, attachment_id: str, value: str) -> str:
    """One inline /chat picture, in a worker thread. Never raises."""
    try:
        existing = get_row(user_id, conversation_id, attachment_id)
        if existing is not None and _file_present(existing):
            _count_write("chat", "duplicate")
            return "duplicate"
        try:
            data = decode_inline(value)
        except Refused as refused:
            _count_write("chat", refused.result)
            return refused.result
        if existing is not None:
            result = "stored" if _heal(existing, data) else "duplicate"
            _count_write("chat", result)
            return result
        result, _row = store_bytes(
            user_id, conversation_id, attachment_id, data, "chat", looked_up=True
        )
        if result not in ("stored", "duplicate"):
            log.info(
                "chat media: an inline picture of a chat turn was not stored (%s)", result
            )
        return result
    except Exception:  # noqa: BLE001 — storage never fails a chat
        log.warning("chat media: storing an inline picture failed", exc_info=True)
        _count_write("chat", "error")
        return "error"


#: Strong references to the background stores in flight: the loop keeps only
#: a weak one, and a task collected mid-way would drop the rest of its turn.
_inflight: "set[asyncio.Task]" = set()


async def _store_inline(user_id: int, conversation_id: str, pairs: List[Tuple[str, str]]) -> None:
    try:
        for attachment_id, value in pairs:
            await run_decode(_store_inline_one, user_id, conversation_id, attachment_id, value)
    except asyncio.CancelledError:
        raise  # shutdown: what was not stored is the next send's, or a backfill's
    except Exception:  # noqa: BLE001 — the executor itself refused (shutting down)
        log.warning("chat media: the background store stopped early", exc_info=True)


def minted_attachment_id(intent_id: str, index: int) -> str:
    """The id the server gives the `index`-th inline picture of a send that
    came with no usable `image_ids` (docs/chat-media/STORE-ALWAYS.md §1)."""
    return f"ix-{intent_id}-{index}"


def _minted_ids(
    images: Sequence[str], intent_id: Optional[str], by_reference: bool
) -> Optional[List[str]]:
    """Ids for a turn whose pictures came without ids that fit them: one per
    picture, from the send intent and the picture's place in the send, or
    None when there is nothing to name them by (counted `unlinked`).

    A page loaded before V44 sends the bytes and no ids, and never stores them
    itself (production, 2026-10-03: a tab opened before the deploy). The
    browser keeps the intent on the user message (`meta.intent.id`), so every
    device can find these pictures from the message alone. Without the
    browser's own intent there is no such link: the one /chat mints when none
    is sent is never the browser's. A turn that sends `image_refs` is a
    regenerate, edit or retry of pictures already stored under the ids its
    message names; it is never given a second set.
    """
    if not by_reference and intent_id and _MINT_INTENT_RE.fullmatch(intent_id):
        return [minted_attachment_id(intent_id, i) for i in range(len(images))]
    unlinked = sum(1 for value in images if value and value.strip())
    for _ in range(unlinked):
        _count_write("chat", "unlinked")
    if unlinked:
        log.info("chat media: %d inline picture(s) with no ids to store them under", unlinked)
    return None


def schedule_inline_store(
    user_id: int,
    conversation_id: Optional[str],
    images: Sequence[str],
    attachment_ids: Optional[Sequence[str]],
    *,
    intent_id: Optional[str] = None,
    by_reference: bool = False,
) -> Optional["asyncio.Task"]:
    """/chat's hook (CONTRACT §5): store this turn's inline pictures BEHIND
    the turn. Returns at once; the task never raises and nothing waits on it.

    `images[i]` is the picture `attachment_ids[i]` names. When the ids are
    absent or their count does not match, which id belongs to which picture
    is unknown, and a picture filed under the wrong id would show on the
    wrong message of every other device, forever (the first write wins); so
    the server names them itself from the browser's send intent
    (`_minted_ids`), or, with none, stores nothing and counts it. Never a 4xx.
    A bare call (no conversation id) stores nothing: its key is the account's
    shared `u<id>-<session>`, which is not a chat anybody can open.
    """
    if not images or not _valid_conversation(conversation_id):
        return None
    if not attachment_ids or len(images) != len(attachment_ids):
        if attachment_ids:
            log.info(
                "chat media: %d inline picture(s) but %d image_ids",
                len(images),
                len(attachment_ids),
            )
        attachment_ids = _minted_ids(images, intent_id, by_reference)
        if attachment_ids is None:
            return None
    pairs = [
        (str(aid), value)
        for aid, value in zip(attachment_ids, images)
        if aid and value and value.strip()
    ]
    if not pairs:
        return None
    task = asyncio.get_running_loop().create_task(
        _store_inline(int(user_id), str(conversation_id), pairs), name="chat-media-store"
    )
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return task


# -------------------------------------------------------------------- read --


def read_full(row: Dict[str, Any]) -> Optional[bytes]:
    """The stored picture's bytes, or None when its file is gone."""
    try:
        with open(file_path(row, "full"), "rb") as fh:
            return fh.read(MAX_IMAGE_BYTES + 1)
    except (OSError, ValueError, KeyError):
        return None


def _data_url(row: Dict[str, Any], data: bytes) -> str:
    return f"data:{row['mime']};base64," + base64.b64encode(data).decode("ascii")


#: Data-URL characters of stored originals one /chat turn reads into memory
#: as they are; past it every further picture is read as a model-sized copy
#: (engines/vision.FIT_EDGES' smallest edge, tens of KB). A message may carry
#: any number of pictures (2026-10-03), and 999 originals of up to 10 MiB
#: would otherwise be 13 GB of base64 in the orchestrator. Twice the inline
#: budget: every turn the browser could have sent inline loads unchanged.
REFS_FULL_CHARS = 2 * BATCH_BUDGET_BYTES


def _load_refs(
    user_id: int,
    conversation_id: str,
    attachment_ids: Sequence[str],
    max_chars: Optional[int] = None,
    full_chars: Optional[int] = None,
) -> Tuple[List[str], List[str]]:
    """(data URLs in order, ids that could not be loaded). With `max_chars`
    (image_memory's fallback) it stops reading once the loaded data URLs
    reach that many characters: the caller keeps only what fits its budget,
    so many 10 MiB originals are never all in memory at once. With
    `full_chars` (/chat) it reads on past that point, each further picture as
    a model-sized copy (`REFS_FULL_CHARS`); one that will not shrink is read
    as it is."""
    rows = rows_by_attachment(user_id, conversation_id, attachment_ids)
    loaded: List[str] = []
    missing: List[str] = []
    held = 0
    for attachment_id in attachment_ids:
        if max_chars is not None and held >= max_chars:
            break
        row = rows.get(attachment_id)
        data = read_full(row) if row is not None else None
        if data is None:
            if attachment_id not in missing:
                missing.append(attachment_id)
            continue
        small = None
        if full_chars is not None and held >= full_chars:
            from .engines.vision import FIT_EDGES, shrink_picture

            small = shrink_picture(data, FIT_EDGES[-1])
        loaded.append(small or _data_url(row, data))
        held += len(loaded[-1])
    return loaded, missing


async def load_refs(
    user_id: int, conversation_id: Optional[str], attachment_ids: Sequence[str]
) -> Tuple[List[str], List[str]]:
    """/chat's `image_refs` (CONTRACT §5): (data URLs in the order asked, the
    ids that could not be loaded). The FULL file, never the thumbnail: the
    model reads the picture the person sent. Scoped to the viewer; /chat has
    already settled that the chat is theirs."""
    ids = [str(a) for a in attachment_ids or []]
    if not ids:
        return [], []
    if not _valid_conversation(conversation_id):
        return [], list(dict.fromkeys(ids))
    return await asyncio.to_thread(
        _load_refs, int(user_id), str(conversation_id), ids, None, REFS_FULL_CHARS
    )


def _meta_attachment_ids(images: Any) -> List[str]:
    """The attachment ids a message's `meta.images` names, in order: valid,
    unique, at most MAX_FILES. The browser writes this list, so it is read as
    untrusted input."""
    out: List[str] = []
    for entry in images if isinstance(images, list) else []:
        aid = entry.get("attachment_id") if isinstance(entry, dict) else None
        if isinstance(aid, str) and ATTACHMENT_ID_RE.fullmatch(aid) and aid not in out:
            out.append(aid)
        if len(out) >= MAX_FILES:
            break
    return out


def _turn_attachment_ids(images: Any, intent_id: Any) -> List[str]:
    """The pictures a stored user message names: its `meta.images`, or, for
    a message without them, the ones the server stored under its send intent
    (`ix-<intent>-<index>`, STORE-ALWAYS.md §1), in send order. An index with
    no row is simply missing when they are loaded."""
    ids = _meta_attachment_ids(images)
    if ids or not (isinstance(intent_id, str) and _MINT_INTENT_RE.fullmatch(intent_id)):
        return ids
    return [minted_attachment_id(intent_id, i) for i in range(MAX_FILES)]


#: A user message `m` (of conversation `c`) that names stored pictures: by
#: its `meta.images`, or, with none, by the viewer's `ix-` rows under its
#: send intent: a photo from a page that wrote no `meta.images` (a tab loaded
#: before V44), which the server stored under ids it named itself.
_PICTURE_TURN_SQL = (
    "((jsonb_typeof(m.meta -> 'images') = 'array' AND (m.meta -> 'images') -> 0 IS NOT NULL) "
    " OR ((m.meta -> 'intent' ->> 'id') ~ '^[0-9a-f]{32}$' "
    "     AND EXISTS (SELECT 1 FROM chat_media x "
    "                  WHERE x.user_id = c.user_id AND x.conversation_id = m.conversation_id "
    "                    AND starts_with(x.attachment_id, 'ix-' || (m.meta -> 'intent' ->> 'id') || '-'))))"
)

#: Picture turns compared with the path the browser sent, newest first.
_VISIBLE_CANDIDATES = 20


def _visible_path(visible: Sequence[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """The turns the browser sent as (role, stripped content), without empty
    ones (the browser drops those itself) and without the trailing user turn,
    which is the question being asked now."""
    path = [
        (str(role or ""), str(content or "").strip())
        for role, content in visible
    ]
    path = [(role, content) for role, content in path if role in ("user", "assistant") and content]
    if path and path[-1][0] == "user":
        path.pop()
    return path


def _place_on_path(
    question: str, answers: Sequence[str], path: List[Tuple[str, str]]
) -> Optional[Tuple[int, str]]:
    """Where a stored picture turn sits on the visible path: (index of the
    last path turn that is it, its answer as the path shows it), or None.

    A turn with words is matched by them. The browser folds pasted blocks and
    a quoted excerpt in FRONT of the words (lib/selectedContext.ts
    foldTurnForModel, joined by a blank line), so a path turn that ends with
    them after a blank line is the same turn. A photo with no words is not on
    the path at all (the browser drops empty turns), so it is matched by its
    answer: one of the assistant messages stored under it."""
    if question:
        for i in range(len(path) - 1, -1, -1):
            role, content = path[i]
            if role == "user" and (content == question or content.endswith("\n\n" + question)):
                following = path[i + 1] if i + 1 < len(path) else ("", "")
                return i, following[1] if following[0] == "assistant" else ""
        return None
    wanted = {a.strip() for a in answers if isinstance(a, str) and a.strip()}
    for i in range(len(path) - 1, -1, -1):
        role, content = path[i]
        if role == "assistant" and content in wanted:
            return i, content
    return None


def _latest_visible_turn_images(
    user_id: int,
    conversation_id: str,
    visible: Sequence[Tuple[str, str]],
    max_chars: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """`latest_turn_images` restricted to the path the browser sent.

    The stored list is flat and append-only (lib/branching.ts): an edit adds
    a sibling and keeps every edited-away version, so "the newest picture
    message" may be on a branch nobody sees any more. Only a picture turn
    that is on the path the person is looking at may be handed to the model.
    Its answers are the assistant messages stored under it: by `meta.branch`
    parent when it has one, and the physically next message (a message
    without branch fields is a child of whatever precedes it)."""
    path = _visible_path(visible)
    if not path:
        return None
    with db.read_connection() as con:
        rows = con.execute(
            "SELECT m.content, m.meta -> 'images' AS images, m.meta -> 'intent' ->> 'id' AS intent_id, "
            "(SELECT coalesce(jsonb_agg(a.content), '[]'::jsonb) FROM messages a "
            "  WHERE btrim(coalesce(m.content, '')) = '' "
            "    AND a.conversation_id = m.conversation_id AND a.id > m.id "
            "    AND a.role = 'assistant' "
            "    AND (a.id = (SELECT n.id FROM messages n "
            "                  WHERE n.conversation_id = m.conversation_id AND n.id > m.id "
            "                  ORDER BY n.id LIMIT 1) "
            "         OR (m.meta -> 'branch' ->> 'self' IS NOT NULL "
            "             AND a.meta -> 'branch' ->> 'parent' = m.meta -> 'branch' ->> 'self'))"
            ") AS answers "
            "FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "WHERE m.conversation_id = %s AND c.user_id = %s AND m.role = 'user' "
            f"  AND {_PICTURE_TURN_SQL} "
            "  AND EXISTS (SELECT 1 FROM chat_media s "
            "               WHERE s.conversation_id = %s AND s.user_id = %s) "
            "ORDER BY m.id DESC LIMIT %s",
            (conversation_id, user_id, conversation_id, user_id, _VISIBLE_CANDIDATES),
        ).fetchall()
    for row in rows:
        question = (row["content"] or "").strip()
        placed = _place_on_path(question, row["answers"] or [], path)
        if placed is None:
            continue
        index, answer = placed
        # The newest picture on the path is the one "the photo" means; if its
        # files are gone, an older one would be the wrong picture.
        loaded, _missing = _load_refs(
            user_id,
            conversation_id,
            _turn_attachment_ids(row["images"], row["intent_id"]),
            max_chars,
        )
        if not loaded:
            return None
        return {
            "images": loaded,
            "context": f"{row['content'] or ''}\n{answer}".lower(),
            "turns_after": sum(1 for role, _ in path[index + 1:] if role == "user"),
        }
    return None


def latest_turn_images(
    user_id: int,
    conversation_id: str,
    visible: Optional[Sequence[Tuple[str, str]]] = None,
    max_chars: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """image_memory's store fallback (CONTRACT §6): the newest USER message
    of this chat whose `meta.images` is non-empty, or, with none, whose send
    intent names the viewer's `ix-` rows (`_PICTURE_TURN_SQL`), with its
    stored pictures.

    {"images": [data URL, ...], "context": question + "\\n" + answer
    (lowercased, as image_memory.remember stores it), "turns_after": user
    messages after it}, or None. One statement that finds nothing at once for
    a chat with no stored picture: the EXISTS is evaluated once, before any
    message is read.

    `visible` is the path the browser sent with the turn (/chat `messages`,
    as (role, content)). When given, only a picture turn ON that path counts,
    and `turns_after` is counted on it (`_latest_visible_turn_images`): a
    photo on a branch the person edited away is never read into a turn. None
    (a caller that sent no history) keeps the stored order alone.

    `turns_after` leaves out a trailing UNANSWERED user message: that is the
    turn being asked now, when the browser's history push beat /chat to the
    database, and counting it would make an immediate follow-up look one turn
    late ("that chart" would stop meaning the picture).

    `max_chars` stops reading pictures once that many data-URL characters
    are loaded (see `_load_refs`); the turn's first pictures come first.
    """
    if not _valid_conversation(conversation_id):
        return None
    if visible is not None:
        return _latest_visible_turn_images(int(user_id), conversation_id, visible, max_chars)
    with db.read_connection() as con:
        row = con.execute(
            "SELECT t.content, t.images, t.intent_id, "
            "(SELECT jsonb_build_object('role', n.role, 'content', n.content) "
            "   FROM messages n WHERE n.conversation_id = t.conversation_id AND n.id > t.id "
            "  ORDER BY n.id LIMIT 1) AS next_message, "
            "(SELECT count(*) FROM messages u WHERE u.conversation_id = t.conversation_id "
            "   AND u.id > t.id AND u.role = 'user') AS users_after, "
            "(SELECT l.role FROM messages l WHERE l.conversation_id = t.conversation_id "
            "  ORDER BY l.id DESC LIMIT 1) AS last_role "
            "FROM (SELECT m.id, m.conversation_id, m.content, m.meta -> 'images' AS images, "
            "             m.meta -> 'intent' ->> 'id' AS intent_id "
            "        FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            "       WHERE m.conversation_id = %s AND c.user_id = %s AND m.role = 'user' "
            f"        AND {_PICTURE_TURN_SQL} "
            "         AND EXISTS (SELECT 1 FROM chat_media s "
            "                      WHERE s.conversation_id = %s AND s.user_id = %s) "
            "       ORDER BY m.id DESC LIMIT 1) t",
            (conversation_id, int(user_id), conversation_id, int(user_id)),
        ).fetchone()
    if row is None:
        return None
    ids = _turn_attachment_ids(row["images"], row["intent_id"])
    loaded, _missing = _load_refs(int(user_id), conversation_id, ids, max_chars)
    if not loaded:
        return None
    following = row["next_message"] or {}
    answer = following.get("content") if following.get("role") == "assistant" else ""
    users_after = int(row["users_after"] or 0)
    if users_after and row["last_role"] == "user":
        users_after -= 1
    return {
        "images": loaded,
        "context": f"{row['content'] or ''}\n{answer or ''}".lower(),
        "turns_after": users_after,
    }


def _etag_matches(header: Optional[str], etag: str) -> bool:
    if not header:
        return False
    for part in header.split(","):
        tag = part.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def _count_read(size: str, result: str) -> None:
    metrics.inc(
        "chat_media_reads_total",
        "chat media: byte reads, by rendition and outcome",
        size=size,
        result=result,
    )


async def media_response(row: Dict[str, Any], size: str, if_none_match: Optional[str]):
    """The bytes of one picture with the headers of CONTRACT §4.3, a 304 when
    the browser already holds them, or 410 when the row outlived its file.
    Shared by the owner's route and the audited admin route. Streamed from
    disk (FileResponse), never read into memory."""
    thumb = size == "thumb" and bool(row["has_thumb"])
    # The tag names the BYTES: a thumbnail request for a picture too small to
    # have one is served the full file, under the full file's tag.
    etag = f'"{row["sha256"]}-t"' if thumb else f'"{row["sha256"]}"'
    ext = "webp" if thumb else _EXT_BY_MIME.get(row["mime"], "bin")
    headers = {
        "Cache-Control": CACHE_CONTROL,
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
        "Content-Disposition": f'inline; filename="image.{ext}"',
    }
    if _etag_matches(if_none_match, etag):
        _count_read(size, "not_modified")
        return Response(status_code=304, headers=headers)
    try:
        path = file_path(row, size)
        stat = await asyncio.to_thread(os.stat, path)
        present = stat_mode.S_ISREG(stat.st_mode)
    except (OSError, ValueError, KeyError):
        present = False
    if not present:
        _count_read(size, "missing")
        return _error(410, "media_missing", "This picture is no longer stored on the server.")
    _count_read(size, "ok")
    return FileResponse(
        path,
        media_type="image/webp" if thumb else row["mime"],
        headers=headers,
        stat_result=stat,
    )


def _list_item(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "attachment_id": row["attachment_id"],
        "media_id": row["media_id"],
        "mime": row["mime"],
        "width": row["width"],
        "height": row["height"],
        "bytes": row["bytes"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def _stored_item(row: Dict[str, Any], created: bool) -> Dict[str, Any]:
    return {
        "attachment_id": row["attachment_id"],
        "media_id": row["media_id"],
        "mime": row["mime"],
        "width": row["width"],
        "height": row["height"],
        "bytes": row["bytes"],
        "sha256": row["sha256"],
        "created": bool(created),
    }


# ------------------------------------------------------------------ routes --


async def _require_attachments(request: Request) -> None:
    """The ATTACHMENTS feature gate (V17), as the upload rail applies it:
    bytes land here, so this is a hard 403 and not /chat's downgrade.
    Imported late: uploads imports the engines, which import this module's
    neighbours."""
    from .uploads import require_attachments

    await require_attachments(request)


async def _read_form(request: Request):
    """The multipart body, parsed only after the caller is known (the
    upload rail's rule, uploads._read_form): no declared File()/Form()
    parameters, so FastAPI leaves the stream alone until here."""
    try:
        return await request.form(
            max_files=MAX_FILES,
            max_fields=_FORM_MAX_FIELDS,
            max_part_size=_FORM_MAX_FIELD_BYTES,
        )
    except ClientDisconnect:
        raise HTTPException(status_code=408, detail="The connection closed before the upload finished.")
    except StarletteHTTPException as exc:
        # Starlette's own 400s: more than MAX_FILES pictures, too many fields.
        return _error(400, "bad_request", str(exc.detail))
    except Exception:  # noqa: BLE001 — multipart.MultiPartException and kin
        return _error(400, "bad_request", "The upload was not a valid form.")


@router.post("/{conversation_id}")
async def upload_media(
    conversation_id: str,
    request: Request,
    user: UserRow = Depends(require_user),
    _attachments: None = Depends(_require_attachments),
):
    """Store up to MAX_FILES pictures for this chat (CONTRACT §4.1).

    Repeated `file` parts and, in the same order, repeated `attachment_id`
    text parts; `source` is `upload` (default) or `backfill`. Every picture
    is checked before ANY is stored, so a refused request stores nothing,
    and an attachment id that already has a row is answered with that row
    (`created: false`) without its bytes being looked at again, unless the
    row's file is gone: then the very same bytes write it back (`_heal`).
    """
    viewer = int(user["id"])
    if not _valid_conversation(conversation_id):
        return _not_found()
    if not await db.run_in_thread(_may_see_chat, viewer, conversation_id):
        return _not_found()
    form = await _read_form(request)
    if isinstance(form, Response):
        return form
    try:
        return await _upload(viewer, conversation_id, form)
    finally:
        await form.close()


async def _upload(viewer: int, conversation_id: str, form) -> Any:
    files = form.getlist("file")
    ids = form.getlist("attachment_id")
    source = form.get("source") or "upload"
    if source not in ("upload", "backfill"):
        return _error(400, "bad_request", "source must be upload or backfill.")
    if not files or any(not isinstance(f, UploadFile) for f in files):
        return _error(400, "bad_request", f"Send one to {MAX_FILES} pictures as `file` parts.")
    if len(files) > MAX_FILES:
        return _error(400, "bad_request", f"At most {MAX_FILES} pictures per request.")
    if len(ids) != len(files):
        return _error(400, "bad_request", "Send one attachment_id per file, in the same order.")
    if any(not isinstance(a, str) or not ATTACHMENT_ID_RE.fullmatch(a) for a in ids):
        return _error(400, "bad_request", "Each attachment_id is 8-64 characters of [A-Za-z0-9_-].")
    if len(set(ids)) != len(ids):
        return _error(400, "bad_request", "Each attachment_id may appear once.")

    existing = await db.run_in_thread(rows_by_attachment, viewer, conversation_id, ids)
    # A stored row whose file is gone is healed by a retry of the same bytes
    # (`_heal`); every other existing row is answered without its bytes read.
    heal: Dict[str, bytes] = {}
    pending: Dict[str, bytes] = {}
    for attachment_id, upload in zip(ids, files):
        if attachment_id in existing:
            if not await asyncio.to_thread(_file_present, existing[attachment_id]):
                data = await upload.read(MAX_IMAGE_BYTES + 1)
                if len(data) <= MAX_IMAGE_BYTES:
                    heal[attachment_id] = data
            continue
        data = await upload.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            _count_write(source, "too_large")
            return _error(413, "too_large", "A picture may be at most 10 MiB.")
        pending[attachment_id] = data

    started = time.monotonic()
    checked: Dict[str, Picture] = {}
    for attachment_id, data in pending.items():
        try:
            checked[attachment_id] = await run_decode(inspect, data)
        except Refused as refused:
            _count_write(source, refused.result)
            return _error(415, "unsupported_type", "Only JPEG, PNG, WebP and GIF pictures are stored.")
    if pending and not await asyncio.to_thread(has_room):
        _count_write(source, "no_space")
        return _error(507, "insufficient_storage", "The server has no space left for pictures.")

    items: List[Dict[str, Any]] = []
    for attachment_id in ids:
        if attachment_id in existing:
            healed = attachment_id in heal and await run_decode(
                _heal, existing[attachment_id], heal[attachment_id]
            )
            _count_write(source, "stored" if healed else "duplicate")
            items.append(_stored_item(existing[attachment_id], created=False))
            continue
        try:
            created, row = await asyncio.to_thread(
                _commit,
                viewer,
                conversation_id,
                attachment_id,
                pending[attachment_id],
                checked[attachment_id],
                source,
            )
        except Exception:  # noqa: BLE001 — a disk or database failure is ours, never a 4xx
            log.warning("chat media: storing an uploaded picture failed", exc_info=True)
            _count_write(source, "error")
            return _error(500, "store_failed", "The picture could not be saved. Try again.")
        _count_write(source, "stored" if created else "duplicate")
        items.append(_stored_item(row, created=created))
    if checked:
        metrics.observe(
            "chat_media_write_seconds",
            (time.monotonic() - started) / len(checked),
            "chat media: seconds to verify and durably store one picture",
            source=source,
        )
    return {"items": items}


def _list_for(viewer: int, conversation_id: str) -> Optional[List[Dict[str, Any]]]:
    if not _may_see_chat(viewer, conversation_id):
        return None
    return list_rows(viewer, conversation_id)


@router.get("/{conversation_id}")
async def list_media(conversation_id: str, user: UserRow = Depends(require_user)):
    """The viewer's pictures in this chat (CONTRACT §4.2), oldest first."""
    rows = await db.run_in_thread(_list_for, int(user["id"]), conversation_id)
    if rows is None:
        return _not_found()
    return {"items": [_list_item(r) for r in rows]}


@router.get("/{conversation_id}/{attachment_id}")
async def get_media(
    conversation_id: str,
    attachment_id: str,
    request: Request,
    size: str = "full",
    user: UserRow = Depends(require_user),
):
    """One picture's bytes (CONTRACT §4.3): `size=thumb` for the chat bubble,
    `full` (the default) for the preview and the model."""
    if size not in SIZES:
        return _error(400, "bad_request", "size must be thumb or full.")
    row = await db.run_in_thread(lookup, int(user["id"]), conversation_id, attachment_id)
    if row is None:
        _count_read(size, "not_found")
        return _not_found()
    return await media_response(row, size, request.headers.get("if-none-match"))


# ------------------------------------------------------- deletion, reaping --


def _count_erase(result: str) -> None:
    metrics.inc(
        "chat_media_erase_total",
        "chat deletion: stored bytes removed at once, by store",
        store="media",
        result=result,
    )


def erase_conversation(conversation_id: str, user_id: int) -> bool:
    """Remove <CHAT_MEDIA_DIR>/<user_id>/<conversation_id> now (CONTRACT §8).

    Called by history.py after the chat's rows are gone, off the event loop.
    Best effort: a failure is logged and counted, returns False, and the
    reaper removes the directory on a later pass (no row names it any more).
    Only the deleter's own directory: a colliding id's pictures under another
    account are that account's, and the reaper decides about them.
    """
    try:
        path = conversation_dir(int(user_id), conversation_id)
    except (TypeError, ValueError):
        return False
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("chat media: could not erase a deleted chat's pictures", exc_info=True)
        _count_erase("error")
        return False
    _count_erase("ok")
    return True


def _newest_mtime(path: str) -> float:
    """The newest mtime of a directory and its direct entries: a directory's
    own mtime moves only when an entry is added or removed, not when a file
    in it is rewritten."""
    newest = 0.0
    try:
        newest = os.stat(path).st_mtime
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
                except OSError:
                    continue
    except OSError:
        pass
    return newest


def _remove_tree(path: str) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        log.warning("chat media: the reaper could not remove a directory", exc_info=True)
        return False


def _rmdir_if_empty(path: str) -> None:
    try:
        os.rmdir(path)
    except OSError:
        pass  # not empty, already gone, or not ours: all fine


def _count_reaped(kind: str, n: int) -> None:
    for _ in range(n):
        metrics.inc("chat_media_reaped_total", "chat media: orphans the reaper removed", kind=kind)


def _reap_rows(grace_s: float) -> int:
    """Delete rows past the grace whose chat is not their owner's — gone, or
    claimed by someone else after they were stored — and their directories."""
    with db.connection() as con:
        gone = con.execute(
            "DELETE FROM chat_media WHERE media_id IN ("
            "  SELECT m.media_id FROM chat_media m"
            "   WHERE m.created_at < now() - make_interval(secs => %s)"
            "     AND NOT EXISTS (SELECT 1 FROM conversations c"
            "                      WHERE c.id = m.conversation_id AND c.user_id = m.user_id)"
            "   LIMIT %s"
            ") RETURNING user_id, conversation_id, media_id",
            (float(grace_s), _REAP_ROW_BATCH),
        ).fetchall()
    for row in gone:
        try:
            _remove_tree(media_dir(int(row["user_id"]), row["conversation_id"], row["media_id"]))
        except ValueError:
            continue
    _count_reaped("row", len(gone))
    return len(gone)


def _known_media(user_id: int) -> set:
    with db.read_connection() as con:
        rows = con.execute(
            "SELECT media_id FROM chat_media WHERE user_id = %s", (int(user_id),)
        ).fetchall()
    return {r["media_id"] for r in rows}


def _reap_dirs(grace_s: float) -> int:
    """Remove media directories no row names, past the grace: a deleted chat
    whose erase failed, a deleted account (the rows cascaded), a crash
    between the files and the row. Names this module did not make are never
    touched."""
    root = media_root()
    if not os.path.isdir(root):
        return 0
    removed = 0
    now = time.time()
    with os.scandir(root) as users:
        user_dirs = [e for e in users if e.is_dir(follow_symlinks=False) and e.name.isdigit()]
    for user_entry in user_dirs:
        known = _known_media(int(user_entry.name))
        try:
            with os.scandir(user_entry.path) as chats:
                chat_dirs = [
                    e for e in chats
                    if e.is_dir(follow_symlinks=False) and _CONVERSATION_ID_RE.fullmatch(e.name)
                ]
        except OSError:
            continue
        for chat_entry in chat_dirs:
            try:
                with os.scandir(chat_entry.path) as media:
                    media_dirs = [
                        e for e in media
                        if e.is_dir(follow_symlinks=False) and _MEDIA_ID_RE.fullmatch(e.name)
                    ]
            except OSError:
                continue
            for media_entry in media_dirs:
                if media_entry.name in known:
                    continue
                if now - _newest_mtime(media_entry.path) < grace_s:
                    continue
                if _remove_tree(media_entry.path):
                    removed += 1
            if now - _newest_mtime(chat_entry.path) >= grace_s:
                _rmdir_if_empty(chat_entry.path)
        if now - _newest_mtime(user_entry.path) >= grace_s:
            _rmdir_if_empty(user_entry.path)
    _count_reaped("dir", removed)
    return removed


def reap_once() -> Dict[str, int]:
    """One reaper pass (CONTRACT §8), in a worker thread: rows first, so the
    directories they named are counted as rows and not again as orphans."""
    grace_s = float(settings.chat_media_orphan_grace_h) * 3600.0
    rows = _reap_rows(grace_s)
    dirs = _reap_dirs(grace_s)
    if rows or dirs:
        log.info("chat media: reaped %d orphan row(s) and %d orphan directory(ies)", rows, dirs)
    return {"rows": rows, "dirs": dirs}


async def reap_loop() -> None:
    """main.py's lifespan runs this until shutdown cancels it: one pass at
    most every CHAT_MEDIA_REAP_INTERVAL_S in this process. Nothing but
    cancellation leaves it; a failed pass is logged and the next one runs."""
    await asyncio.sleep(_REAP_FIRST_DELAY_S)
    while True:
        try:
            await asyncio.to_thread(reap_once)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("chat media: reaper pass failed", exc_info=True)
        await asyncio.sleep(float(settings.chat_media_reap_interval_s))
