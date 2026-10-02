"""My files (2026-09-30): one read-only list of everything a person uploaded.

WHY. The owner asked "if any user uploads anything in our AI app, it is
stored, right, but it does not show on the user side". Stored, only partly —
and shown, nowhere. A person's uploads live in nine tables and three byte
stores, and until this module the only upload anyone could list for
themselves was a voice recording (/recordings). An admin could list a
member's uploads (authn/store.admin_user_uploads); the member could not.

WHAT IS LISTED — three sources in one keyset-paged statement:

    upload     an `uploads` row that finished ('ready'): a document, a
               spreadsheet or dataset, a video, or an audio file. The kind is
               the marker the writing rail left in `uploads.notes`
               ('document', 'video', or a dataset's own notes; pinned by
               tests/test_myfiles_api.py) and an audio file is a 'video' row
               whose name ends in an audio extension (video/api.py B12).
    text       a `documents` row with no ready upload behind it: a document that
               was sent inside the chat request (before 2026-09-02 every PDF
               travelled that way) or handed to the artifact studio. Only its
               extracted text was ever kept. Archive members
               ("x.zip/a.txt", "x.zip (archive contents)") fold under the
               archive's upload instead of listing one by one — and the
               composer sends an archive on the DOCUMENT rail, so once the
               archive itself is swept its manifest is the text it keeps.
    recording  a `voice_sessions` row whose audio is still stored.

Pictures are not listed: they were never stored on the account (the only copy
is the sending browser's IndexedDB, plus IMAGE_MEMORY_TTL_S of server memory
for follow-up questions), and the retention block says so.

WHOSE. The caller's, from the session and nothing else: there is no user id
parameter, and anything unknown in the query string is ignored. Uploads and
documents carry no owner column, so ownership is `conversations.user_id` — the
same join the admin list trusts — with the reserved `u<digits>-` conversation
shape excluded outright. That exclusion is a SECURITY requirement, not
tidiness: a legacy chat created as `u7-default` before 2026-09-13 names its
creator as owner, while user 7's bare /chat calls stored document text under
that very key (F034, audit of 2026-09-12). Joined naively, the creator would
see user 7's file names. uploads._refuse_reserved_conversation_key refuses the
same shape on every per-chat route.

NOTHING HERE READS OR DELETES A FILE. Downloads, previews and deletes go
through the routes that already exist and re-derive ownership on every call
(GET /uploads/{conversation}/{upload}/file, /uploads/{conversation}/document,
/audio/sessions/{id}/audio and DELETE /audio/sessions/{id}). This module only
says what exists and what can be done with it.

WHAT IS REALLY STORED ("availability"), decided per row the way the download
route decides it: the original bytes (`available`), only the text the chat
read (`text_only`), only a spreadsheet's profile (`summary_only`), a
recording still being made or transcribed (`processing`), or nothing
(`expired`). Uploaded originals are swept WORKSPACE_TTL_HOURS after upload
(24 h in production); a video's bytes also live in the analysis store while
any chat links them, which is why its Download keeps working after the sweep
(uploads.download_upload falls back to it since this change).

COST, measured on the private test database (PostgreSQL 18.6, production's
planner settings, 25 runs): the first page of a person with 2,000 uploads and
300 recordings is 5.6 ms p50 (8.2 p95) including the tunnel; at 20,000
uploads 45.7-65.0 ms. Production held 8 upload rows in total on 2026-09-30.
The keys are fetched first and only the page's rows are decorated (<0.5 ms
for 51 rows); availability is up to 100 stat() calls, 0.135 ms p50 for a
50-row page on the head's NVMe. When one person passes ~10,000 uploads or the
myfiles_list_seconds p95 passes 50 ms, add uploads.user_id with an index on
(user_id, created_at DESC, id DESC): 0.68-0.92 ms at every measured scale.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from . import db, metrics
from .auth import UserRow, require_user
from .config import settings

log = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["files"])

KINDS = ("document", "dataset", "video", "audio", "recording")
_UPLOAD_KINDS = frozenset({"document", "dataset", "video", "audio"})
SOURCES = ("upload", "text", "recording")
SORTS = ("newest", "oldest", "largest", "name")

#: The sidebar search's own bound (history.py /history/search): long enough
#: for any file name a person would type, short enough that a pasted
#: paragraph is refused rather than turned into a sequential ILIKE.
QUERY_MAX_CHARS = 100
LIMIT_DEFAULT = 50
LIMIT_MAX = 100

#: A recording has no file name. It is listed, searched and sorted by the
#: title the page shows for it, so typing "recording" finds them and A-Z puts
#: them under V rather than ahead of every file.
RECORDING_NAME = "Voice recording"

#: The largest cursor this route will decode. The name sort's key is the
#: first _NAME_KEY_CHARS characters of the lowered name, not the whole name:
#: a text row's name is whatever the client sent (ChatRequest.pdf_filename
#: has no bound) or an archive member's whole path, and a cursor carrying
#: 4,916 characters of one was refused by the next request (QA, 2026-09-30),
#: stalling "Name, A to Z" for good. 512 characters of JSON kept as UTF-8
#: (ensure_ascii=False; the default \uXXXX escapes made a CJK name six times
#: longer) keep a cursor under ~2.8 KB, far below this bound and Node's 16 KB
#: header limit, which answered 431 before the proxy ran.
_CURSOR_MAX_CHARS = 16_384
_NAME_KEY_CHARS = 512
#: Upload ids are uuid4().hex and text rows are a bigint; anything in this
#: alphabet is safe as a bound parameter, and anything outside it is not an
#: id this route ever minted.
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DIGITS_RE = re.compile(r"^[0-9]{1,19}$")
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_BIGINT_MAX = (1 << 63) - 1


def _audio_name_pattern() -> str:
    """POSIX regex for an audio file's name, from the video rail's own list,
    so an extension added there is an audio row here without an edit."""
    from .video.api import AUDIO_EXTENSIONS

    return r"\.(" + "|".join(re.escape(ext.lstrip(".")) for ext in AUDIO_EXTENSIONS) + r")$"


class BadRequest(ValueError):
    """A query this route refuses, with the sentence the person is shown."""


@dataclass(frozen=True)
class Filters:
    kinds: Tuple[str, ...] = ()
    q: Optional[str] = None
    since: Optional[datetime] = None
    until: Optional[datetime] = None
    min_bytes: Optional[int] = None
    max_bytes: Optional[int] = None

    @property
    def selected(self) -> frozenset:
        return frozenset(self.kinds or KINDS)


@dataclass(frozen=True)
class ListQuery:
    filters: Filters
    sort: str = "newest"
    limit: int = LIMIT_DEFAULT
    #: (sort key, source, item id) of the last row of the previous page.
    after: Optional[Tuple[Any, str, str]] = None


# ------------------------------------------------------------- parsing --


def _text(params: Mapping[str, Any], name: str) -> Optional[str]:
    value = params.get(name)
    if value is None:
        return None
    value = str(value).strip()
    if "\x00" in value:
        # PostgreSQL text cannot hold NUL: bound as a parameter it raised
        # psycopg.DataError, a 500 (QA, 2026-09-30).
        raise BadRequest(f"{name} must not contain a NUL character.")
    return value or None


def _instant(raw: str, name: str) -> datetime:
    # "+00:00" sent unencoded arrives as " 00:00" (a "+" in a query string is
    # a space); both spell the same instant, as in audio_api.list_sessions.
    text = raw.strip().replace(" ", "+")
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise BadRequest(f"{name} must be an ISO 8601 time.") from None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _count(raw: str, name: str) -> int:
    if not _DIGITS_RE.fullmatch(raw):
        raise BadRequest(f"{name} must be a whole number of bytes, 0 or more.")
    value = int(raw)
    if value > _BIGINT_MAX:
        raise BadRequest(f"{name} is too large.")
    return value


def parse_filters(params: Mapping[str, Any]) -> Filters:
    kinds: List[str] = []
    raw_kind = _text(params, "kind")
    if raw_kind:
        for part in raw_kind.split(","):
            part = part.strip()
            if not part:
                continue
            if part not in KINDS:
                raise BadRequest(f"kind must be one or more of {', '.join(KINDS)}.")
            if part not in kinds:
                kinds.append(part)
    q = _text(params, "q")
    if q is not None and len(q) > QUERY_MAX_CHARS:
        raise BadRequest(f"Search for at most {QUERY_MAX_CHARS} characters.")
    since = _instant(raw, "since") if (raw := _text(params, "since")) else None
    until = _instant(raw, "until") if (raw := _text(params, "until")) else None
    if since is not None and until is not None and since > until:
        raise BadRequest("since must not be later than until.")
    min_bytes = _count(raw, "min_bytes") if (raw := _text(params, "min_bytes")) else None
    max_bytes = _count(raw, "max_bytes") if (raw := _text(params, "max_bytes")) else None
    if min_bytes is not None and max_bytes is not None and min_bytes > max_bytes:
        raise BadRequest("min_bytes must not be larger than max_bytes.")
    return Filters(tuple(kinds), q, since, until, min_bytes, max_bytes)


def encode_cursor(sort: str, key: Any, source: str, item_id: str) -> str:
    if isinstance(key, datetime):
        key = key.astimezone(timezone.utc).isoformat()
    raw = json.dumps(
        {"v": 1, "sort": sort, "k": [key, source, item_id]}, separators=(",", ":"), ensure_ascii=False
    )
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def decode_cursor(text: str, sort: str) -> Tuple[Any, str, str]:
    """The keyset position a previous page handed out, typed strictly: every
    value lands in a bound parameter, and one minted under another sort is
    refused rather than read as a position in the wrong order."""
    invalid = BadRequest("The cursor is not valid. Load the list again.")
    if len(text) > _CURSOR_MAX_CHARS:
        raise invalid
    try:
        padded = text + "=" * (-len(text) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeError):
        raise invalid from None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise invalid
    if payload.get("sort") != sort:
        raise BadRequest("The cursor belongs to a different sort order. Load the list again.")
    position = payload.get("k")
    if not isinstance(position, list) or len(position) != 3:
        raise invalid
    key, source, item_id = position
    if source not in SOURCES or not isinstance(item_id, str) or not _ITEM_ID_RE.fullmatch(item_id):
        raise invalid
    if sort in ("newest", "oldest"):
        if not isinstance(key, str):
            raise invalid
        try:
            key = _instant(key, "cursor")
        except BadRequest:
            raise invalid from None
    elif sort == "largest":
        if isinstance(key, bool) or not isinstance(key, int) or not -1 <= key <= _BIGINT_MAX:
            raise invalid
    elif not isinstance(key, str) or len(key) > _NAME_KEY_CHARS or "\x00" in key:
        raise invalid
    return key, source, item_id


def parse_list_query(params: Mapping[str, Any]) -> ListQuery:
    filters = parse_filters(params)
    sort = _text(params, "sort") or "newest"
    if sort not in SORTS:
        raise BadRequest(f"sort must be one of {', '.join(SORTS)}.")
    raw_limit = _text(params, "limit")
    limit = LIMIT_DEFAULT
    if raw_limit is not None:
        if not _DIGITS_RE.fullmatch(raw_limit) or not 1 <= int(raw_limit) <= LIMIT_MAX:
            raise BadRequest(f"limit must be 1 to {LIMIT_MAX}.")
        limit = int(raw_limit)
    raw_cursor = _text(params, "cursor")
    after = decode_cursor(raw_cursor, sort) if raw_cursor else None
    return ListQuery(filters=filters, sort=sort, limit=limit, after=after)


# ---------------------------------------------------------------- SQL --
#
# Every fragment below is a constant; values only ever travel as bound
# parameters. The branches are included only when the kind filter can match
# them, so a "Voice recordings" filter never scans uploads at all — which is
# why EVERY branch names its own columns: a UNION takes its column names from
# its first branch, and under that filter the recordings branch is the only
# one (an unnamed branch there was a 500, found end to end on 2026-09-30).

_UPLOADS_BRANCH = """
    SELECT 'upload'::text AS source, u.id AS item_id, u.conversation_id, u.filename AS name,
           u.bytes::bigint AS bytes, u.created_at,
           CASE WHEN u.notes = 'document' THEN 'document'
                WHEN u.notes = 'video' AND lower(u.filename) ~ %(audio_re)s THEN 'audio'
                WHEN u.notes = 'video' THEN 'video'
                ELSE 'dataset' END AS kind
      FROM uploads u
      JOIN conversations c ON c.id = u.conversation_id
     WHERE c.user_id = %(uid)s AND u.status = 'ready' AND u.conversation_id !~ '^u[0-9]+-'"""

_UPLOADS_SEARCH = """
       AND (u.filename ILIKE %(pat)s ESCAPE '\\' OR c.title ILIKE %(pat)s ESCAPE '\\')"""

# A document's text is folded under its upload when the upload names it
# exactly, when it is a member of an uploaded archive ("x.zip/a.txt"), or when
# it is an archive's manifest ("x.zip (archive contents)"). Only under an
# upload that is itself listed ('ready'): folding it under a rejected or
# failed upload of the same name hid the text entirely (QA, 2026-09-30).
_TEXT_BRANCH = """
    SELECT 'text'::text AS source, d.id::text AS item_id, d.conversation_id, d.filename AS name,
           NULL::bigint AS bytes, d.created_at, 'document'::text AS kind
      FROM documents d
      JOIN conversations c ON c.id = d.conversation_id
     WHERE c.user_id = %(uid)s AND d.conversation_id !~ '^u[0-9]+-'
       AND NOT EXISTS (
             SELECT 1 FROM uploads u2
              WHERE u2.conversation_id = d.conversation_id AND u2.status = 'ready'
                AND u2.filename IN (d.filename, split_part(d.filename, '/', 1),
                                    regexp_replace(d.filename, ' \\(archive contents\\)$', '')))"""

_TEXT_SEARCH = """
       AND (d.filename ILIKE %(pat)s ESCAPE '\\' OR c.title ILIKE %(pat)s ESCAPE '\\')"""

_RECORDINGS_BRANCH = """
    SELECT 'recording'::text AS source, v.id AS item_id, NULL::text AS conversation_id,
           %(recording_name)s::text AS name, v.bytes_stored::bigint AS bytes, v.created_at,
           'recording'::text AS kind
      FROM voice_sessions v
     WHERE v.user_id = %(uid)s AND v.status <> 'cancelled' AND v.audio_deleted_at IS NULL"""

_RECORDINGS_SEARCH = """
       AND %(recording_name)s::text ILIKE %(pat)s ESCAPE '\\'"""

#: sort -> (key expression over the union's columns, direction, keyset operator).
#: The ORDER BY and the keyset comparison use the SAME expression, so the
#: bounded name key stays exact: names that agree on their first
#: _NAME_KEY_CHARS characters tie on it, and (source, item_id) orders them.
_SORT_SPEC = {
    "newest": ("created_at", "DESC", "<"),
    "oldest": ("created_at", "ASC", ">"),
    "largest": ("COALESCE(bytes, -1)", "DESC", "<"),
    "name": (f"left(lower(name), {_NAME_KEY_CHARS})", "ASC", ">"),
}

_DECORATE = """
SELECT p.source, p.item_id, p.conversation_id, c.title AS conversation_title, p.name, p.bytes,
       p.created_at, p.kind, p.sort_key,
       up.has_profile, CASE WHEN p.source = 'text' THEN p.name ELSE up.text_name END AS text_name,
       va.content_hash, COALESCE(va.status, vs.status) AS media_status,
       COALESCE(va.duration_ms::bigint, vs.audio_ms) AS media_ms
  FROM page p
  LEFT JOIN conversations c ON c.id = p.conversation_id
  LEFT JOIN LATERAL (
        SELECT (u.profile IS NOT NULL) AS has_profile,
               (SELECT d.filename FROM documents d
                 WHERE d.conversation_id = u.conversation_id
                   AND d.filename IN (u.filename, u.filename || ' (archive contents)')
                 ORDER BY d.filename = u.filename DESC
                 LIMIT 1) AS text_name
          FROM uploads u
         WHERE p.source = 'upload' AND u.id = p.item_id) up ON true
  LEFT JOIN LATERAL (
        SELECT a.content_hash, a.status, a.duration_ms
          FROM video_attachments l JOIN video_analyses a ON a.id = l.analysis_id
         WHERE p.source = 'upload' AND p.kind IN ('video', 'audio')
           AND l.conversation_id = p.conversation_id AND l.upload_id = p.item_id
         LIMIT 1) va ON true
  LEFT JOIN voice_sessions vs ON p.source = 'recording' AND vs.id = p.item_id
 ORDER BY p.sort_key {direction}, p.source {direction}, p.item_id {direction}"""


def _mine(filters: Filters, user_id: int) -> Tuple[str, Dict[str, Any], List[str]]:
    """The caller's rows as one UNION ALL (`mine`), the conditions the outer
    query applies to it, and every parameter both need. At least one branch
    always exists: every kind belongs to one."""
    params: Dict[str, Any] = {"uid": int(user_id)}
    selected = filters.selected
    search = filters.q is not None
    if search:
        params["pat"] = db.like_contains_pattern(filters.q)
    branches: List[str] = []
    if selected & _UPLOAD_KINDS:
        params["audio_re"] = _audio_name_pattern()
        branches.append(_UPLOADS_BRANCH + (_UPLOADS_SEARCH if search else ""))
    if "document" in selected:
        branches.append(_TEXT_BRANCH + (_TEXT_SEARCH if search else ""))
    if "recording" in selected:
        params["recording_name"] = RECORDING_NAME
        branches.append(_RECORDINGS_BRANCH + (_RECORDINGS_SEARCH if search else ""))

    where: List[str] = []
    if filters.kinds and selected != frozenset(KINDS):
        params["kinds"] = sorted(selected)
        where.append("kind = ANY(%(kinds)s::text[])")
    if filters.since is not None:
        params["since"] = filters.since
        where.append("created_at >= %(since)s")
    if filters.until is not None:
        params["until"] = filters.until
        where.append("created_at < %(until)s")
    if filters.min_bytes is not None:
        params["min_bytes"] = filters.min_bytes
        where.append("bytes >= %(min_bytes)s")
    if filters.max_bytes is not None:
        params["max_bytes"] = filters.max_bytes
        where.append("bytes < %(max_bytes)s")
    union = "\n    UNION ALL".join(branches)
    return f"WITH mine AS ({union}\n)", params, where


def list_statement(query: ListQuery, user_id: int) -> Tuple[str, Dict[str, Any]]:
    head, params, where = _mine(query.filters, user_id)
    key, direction, op = _SORT_SPEC[query.sort]
    if query.after is not None:
        params["after_key"], params["after_source"], params["after_id"] = query.after
        where.append(f"({key}, source, item_id) {op} (%(after_key)s, %(after_source)s, %(after_id)s)")
    params["lim"] = query.limit + 1  # one more than shown says whether a next page exists
    predicate = ("\n   WHERE " + "\n     AND ".join(where)) if where else ""
    sql = (
        f"{head},\npage AS (\n  SELECT source, item_id, conversation_id, name, bytes, created_at, kind,"
        f" {key} AS sort_key\n    FROM mine{predicate}\n"
        f"   ORDER BY {key} {direction}, source {direction}, item_id {direction}\n   LIMIT %(lim)s\n)"
        + _DECORATE.format(direction=direction)
    )
    return sql, params


def summary_statement(filters: Filters, user_id: int) -> Tuple[str, Dict[str, Any]]:
    # Counts are PER KIND, so a kind filter would only zero the other chips.
    head, params, where = _mine(Filters(q=filters.q, since=filters.since, until=filters.until,
                                        min_bytes=filters.min_bytes, max_bytes=filters.max_bytes),
                                user_id)
    predicate = ("\n WHERE " + "\n   AND ".join(where)) if where else ""
    sql = (
        f"{head}\nSELECT kind, count(*) AS n, COALESCE(sum(bytes), 0)::bigint AS total_bytes"
        f"\n  FROM mine{predicate}\n GROUP BY kind"
    )
    return sql, params


# --------------------------------------------------------- availability --


def _stored(conversation_id: str, upload_id: str, filename: str, subdir: str) -> bool:
    """The download route's own path resolution (core/upload_paths), so a
    row this page calls available is one that route will serve."""
    from .core.upload_paths import UploadPathError, resolve_upload_file

    try:
        return resolve_upload_file(
            settings.workspace_dir, conversation_id, upload_id, filename, subdir=subdir
        ).is_file()
    except (UploadPathError, OSError, ValueError):
        return False


def _in_video_store(content_hash: Optional[str]) -> bool:
    from .video import store

    if not content_hash:
        return False
    try:
        return store.source_path(content_hash) is not None
    except (OSError, ValueError):
        return False


def availability(row: Mapping[str, Any]) -> str:
    source, kind = row["source"], row["kind"]
    if source == "text":
        return "text_only"
    if source == "recording":
        # Decided from the row alone: the recording's files are the voice
        # store's business, and a stat per recording would tie this list to
        # wherever that store lives.
        return "processing" if row.get("media_status") in ("recording", "finishing") else "available"
    conv, upload_id, name = row["conversation_id"], row["item_id"], row["name"]
    if kind == "document":
        if _stored(conv, upload_id, name, "_original"):
            return "available"
        return "text_only" if row.get("text_name") else "expired"
    if kind == "dataset":
        # A single file is kept under extracted/<name>; an ARCHIVE's own bytes
        # are deleted the moment it is extracted (uploads._finalise_dataset),
        # so an archive is summary_only from its first minute.
        if _stored(conv, upload_id, name, "extracted") or _stored(conv, upload_id, name, "_original"):
            return "available"
        return "summary_only" if row.get("has_profile") else "expired"
    if _stored(conv, upload_id, name, "_original") or _in_video_store(row.get("content_hash")):
        return "available"
    return "expired"


def _iso(value: Any) -> Optional[str]:
    return value.astimezone(timezone.utc).isoformat() if isinstance(value, datetime) else None


def _item(row: Mapping[str, Any]) -> Dict[str, Any]:
    state = availability(row)
    source, kind = row["source"], row["kind"]
    preview: Optional[str] = None
    if source == "recording":
        preview = "audio" if state == "available" else None
    elif source == "text" or (kind == "document" and row.get("text_name")):
        preview = "text"
    elif kind == "dataset" and row.get("has_profile"):
        preview = "summary"
    downloadable = state == "available" and (
        source == "recording" or (source == "upload" and bool(_HEX32_RE.fullmatch(row["item_id"] or "")))
    )
    media = None
    if kind in ("video", "audio", "recording"):
        media_ms = row.get("media_ms")
        media = {
            "status": row.get("media_status"),
            "duration_ms": int(media_ms) if media_ms is not None else None,
        }
    conversation = None
    if row.get("conversation_id"):
        conversation = {"id": row["conversation_id"], "title": row.get("conversation_title") or ""}
    return {
        "id": f"{source}:{row['item_id']}",
        "source": source,
        "kind": kind,
        "name": row["name"],
        "bytes": int(row["bytes"]) if row.get("bytes") is not None else None,
        "created_at": _iso(row["created_at"]),
        "conversation": conversation,
        "availability": state,
        "media": media,
        "can": {"download": downloadable, "preview": preview, "delete": source == "recording"},
        # The documents row a text preview reads (GET /uploads/{conv}/document
        # ?name=): the file's own text, or an archive's "(archive contents)".
        "text_name": row.get("text_name") if preview == "text" else None,
    }


def retention() -> Dict[str, Any]:
    """What the page tells a person about how long each kind is kept — read
    from the deployment's settings, so the sentence stays true when a TTL
    changes."""
    from .engines import image_memory

    picture_hours = image_memory.ttl_s() / 3600.0
    return {
        "upload_hours": int(settings.workspace_ttl_hours),
        "recording_days": int(settings.voice_retention_days),
        "video_kept_with_chat": True,
        "video_grace_hours": int(settings.video_orphan_ttl_hours),
        "pictures": "browser_only",
        "picture_memory_hours": int(picture_hours) if picture_hours.is_integer() else round(picture_hours, 2),
    }


# ---------------------------------------------------------------- reads --


def list_files(user_id: int, query: ListQuery) -> Dict[str, Any]:
    """One page of the caller's files: ONE statement, then at most two
    stat() calls per row. Synchronous; the route runs it in a thread."""
    sql, params = list_statement(query, user_id)
    with db.read_connection() as con:
        rows = con.execute(sql, params).fetchall()
    more = len(rows) > query.limit
    rows = rows[: query.limit]
    next_cursor = None
    if more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(query.sort, last["sort_key"], last["source"], last["item_id"])
    return {
        "items": [_item(row) for row in rows],
        "next_cursor": next_cursor,
        "retention": retention(),
    }


def summarise(user_id: int, filters: Filters) -> Dict[str, Any]:
    sql, params = summary_statement(filters, user_id)
    with db.read_connection() as con:
        rows = con.execute(sql, params).fetchall()
    kinds = {kind: {"count": 0, "bytes": 0} for kind in KINDS}
    for row in rows:
        if row["kind"] in kinds:
            kinds[row["kind"]] = {"count": int(row["n"]), "bytes": int(row["total_bytes"])}
    return {
        "kinds": kinds,
        "total": {
            "count": sum(k["count"] for k in kinds.values()),
            "bytes": sum(k["bytes"] for k in kinds.values()),
        },
        "retention": retention(),
    }


# --------------------------------------------------------------- routes --


def _flat(status: int, reason: str, detail: str) -> JSONResponse:
    """audio_api's flat refusal: {"detail", "reason"} side by side."""
    return JSONResponse(status_code=status, content={"detail": detail, "reason": reason})


async def _serve(view: str, work) -> Any:
    started = time.monotonic()
    result = "ok"
    try:
        return await db.run_in_thread(work)
    except BadRequest as exc:
        result = "bad_request"
        return _flat(400, "bad_request", str(exc))
    except Exception:
        result = "error"
        raise
    finally:
        elapsed = time.monotonic() - started
        metrics.observe(
            "myfiles_list_seconds", elapsed, "My files: time to answer one list or summary request.", view=view
        )
        metrics.inc("myfiles_list_total", "My files: list and summary requests by outcome.", view=view, result=result)
        # Counts and timing only: a file name is the person's content.
        log.debug("my files %s: %s in %.1f ms", view, result, elapsed * 1000)


@router.get("/mine")
async def my_files(request: Request, user: UserRow = Depends(require_user)) -> Any:
    """The caller's files, newest first by default, keyset-paged. No feature
    gate: a person must always be able to find what is stored about them, the
    rule /audio/sessions follows (voice security review, item 7)."""
    user_id = int(user["id"])
    params = dict(request.query_params)
    return await _serve("list", lambda: list_files(user_id, parse_list_query(params)))


@router.get("/mine/summary")
async def my_files_summary(request: Request, user: UserRow = Depends(require_user)) -> Any:
    """Counts and bytes per kind under the same filters (kind and paging
    aside): the filter chips' numbers and the page's header line."""
    user_id = int(user["id"])
    params = dict(request.query_params)
    return await _serve("summary", lambda: summarise(user_id, parse_filters(params)))
