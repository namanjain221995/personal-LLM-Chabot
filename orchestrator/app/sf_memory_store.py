"""The Salesforce conversation-memory store: table `sf_query_state` (V45).

WHAT IT HOLDS. One row per sf_assistant answer the person saw (design
2026-11-25 §2). The row carries the service's `state` for follow-ups after
that answer, the code-written `plan_line`, the rolling Salesforce summary of
the path up to that answer, and three hashes that tie the row to one place in
the chat tree with no frontend change:

    answer_sha1    the answer itself
    parent_sha1    the assistant turn before it on the path ('' for the first)
    question_sha1  the user turn it replies to (tells identical siblings
                   apart after an edit, design C1)

Every hash is the toggle-off G1 normalisation (sha1 of
`" ".join(text.split())`). This module never computes one from text except in
`delete_removed_answers`, and there the caller hands in the one shared helper,
so the definition lives in a single place.

WHAT IT DOES NOT DO. It never reads inside `state` (the bridge keeps it
opaque; sf_assistant owns its shape) and it never decides which turn of a
path is the parent: the walk-back with gaps and transparent turns belongs to
`sf_memory.py`. This module is the four row operations that walk-back and the
write hook need:

    insert                  one immutable row per answer; a duplicate
                            generation writes nothing and returns None
    fetch_candidates/pick   the one indexed read per turn, then the choice
    lookup                  both of those for a single answer
    update_summary          compare-and-set, so a late fold never overwrites
                            a newer one
    delete_removed_answers  the truncate rule: only rows of answers that no
                            longer exist in the chat (design C26)

WHO MAY SEE A ROW. Every read and every delete is filtered by `user_id` AND
`conversation_id`, on top of the chat ownership the routes already check. An
insert only lands in a chat the user owns. No function here returns another
person's row, and no route returns state, `plan_line` or summary at all.

PRIVACY OF THE LOGS. State, summary, plan lines and turn text are Salesforce
data and are never logged: log lines carry ids, sizes and an SQLSTATE only. A
database error is re-raised as `StoreError` with its context suppressed,
because a psycopg error's DETAIL can quote the failing row's values.

SIZES ARE CUT IN CODE FIRST. `plan_line` is cut to 600 characters and the
summary to 8,000 before any write; the table's CHECKs are a backstop only
(design C30). A state larger than 32 KB is written as NULL.

THE TABLE. Migration V45 in db.py creates it, from the exact text of design
§2.2. `DDL` below is that same object (`db._MIGRATION_V45`), so only one copy
of the text exists; db.py does not import this module (an import cycle, and
design §3.6 limits who imports the store).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

from psycopg.types.json import Jsonb

from . import db
from .sse import strict_json

log = logging.getLogger(__name__)

TABLE = "sf_query_state"

#: The V45 DDL. db.py is the authority: this is the very text the migration
#: runs (design 2026-11-25 §2.2, verbatim), never a second copy to keep in
#: step.
DDL = db._MIGRATION_V45

#: The caps the table's CHECKs back up. Code cuts to these before writing.
PLAN_LINE_MAX_CHARS = 600
SUMMARY_MAX_CHARS = 8000
#: A state whose stored JSON text is larger than this (UTF-8 bytes) is written
#: as NULL: the row still exists, so the walk-back finds the answer, but it
#: carries no plan.
STATE_MAX_BYTES = 32 * 1024
#: Rows one read may return (design §3.1). The walk-back asks for at most 13
#: answer hashes; 50 rows leaves room for repeated fixed lines.
FETCH_LIMIT = 50

#: Longest id-like text accepted (conversation, generation, workspace,
#: access profile). These are ids, never content.
_ID_MAX_CHARS = 255
_SHA1_RE = re.compile(r"[0-9a-f]{40}")

#: Every column a read or an insert hands back. `state` and `summary` are in
#: it because the walk-back and the fold need them; they never reach a log.
_COLUMNS = (
    "id, conversation_id, user_id, workspace_id, access_profile, generation_id, "
    "answer_sha1, parent_sha1, question_sha1, contract, state, plan_line, "
    "summary, summary_pairs, path_pairs, created_at, summary_at, expires_at"
)


class StoreError(Exception):
    """A store operation failed. The message names the operation and an
    SQLSTATE or reason only, never a value, so it is safe to log with
    `exc_info`. The caller treats it as "no memory this turn"."""

    def __init__(self, op: str, reason: str) -> None:
        super().__init__(f"{TABLE} {op} failed ({reason})")
        self.op = op
        self.reason = reason


class InvalidArgument(StoreError, ValueError):
    """An argument has the wrong type or shape: a bug in the caller. Raised
    before the database is touched; the message names the argument, never
    its value (a mix-up could put answer text where a hash belongs)."""

    def __init__(self, op: str, name: str, rule: str) -> None:
        super().__init__(op, f"invalid {name}: {rule}")
        self.name = name


# ------------------------------------------------------------- arguments --


def _user_id(op: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidArgument(op, "user_id", "a positive integer")
    return value


def _ident(op: str, name: str, value: Any, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value) or len(value) > _ID_MAX_CHARS:
        rule = "a non-empty string" if required else "a string"
        raise InvalidArgument(op, name, f"{rule} of at most {_ID_MAX_CHARS} characters")
    return value


def _sha1(op: str, name: str, value: Any, *, allow_empty: bool) -> str:
    if isinstance(value, str) and (_SHA1_RE.fullmatch(value) or (allow_empty and value == "")):
        return value
    rule = "a lowercase sha1 hex digest" + (" or ''" if allow_empty else "")
    raise InvalidArgument(op, name, rule)


def _count(op: str, name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 2**31 - 1:
        raise InvalidArgument(op, name, "a non-negative integer")
    return value


def _profile(op: str, value: Any) -> Optional[str]:
    """None for "no access profile" (today's only value); '' means the same,
    so an empty string never makes a row look like another profile's."""
    if value is None or value == "":
        return None
    return _ident(op, "access_profile", value)


def _timestamp(op: str, name: str, value: Any) -> Optional[datetime]:
    """None, an aware datetime, or the ISO-8601 text db.py hands out."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            raise InvalidArgument(op, name, "an ISO-8601 timestamp") from None
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise InvalidArgument(op, name, "a timezone-aware datetime")
    return value


def _cut(value: Any, limit: int) -> str:
    """Text safe for a `text` column and its CHECK: NULs removed, lone
    surrogates replaced, then cut to `limit` characters (PostgreSQL's
    `length` counts characters, as Python's `len` does)."""
    text = db._text(str(value or "")) or ""
    text = text.encode("utf-8", "replace").decode("utf-8")
    return text[:limit]


def _without_nul(value: Any) -> Any:
    """`value` with every NUL code point removed from its strings and keys.

    jsonb refuses NUL. The house helper `db._dumps_jsonb` removes the
    six-character escape `\\u0000` from the serialised TEXT, and there it
    cannot tell an escaped NUL from a typed backslash followed by "u0000":
    the text it leaves is then invalid JSON (the row is lost) or a different
    value ("C:\\u0000temp" comes back as "C:<TAB>emp"). Removing the code
    point from the object before it is serialised leaves the text exact.

    A value JSON cannot hold natively becomes its `str()`, which is what
    `strict_json`'s `default=str` would write, so the NUL removal covers it
    too. Other keys (int, float, bool, None) are left for json to convert;
    any other key type makes the state unstorable, as before."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {
            (_without_nul(key) if isinstance(key, str) else key): _without_nul(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_without_nul(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _without_nul(str(value))


def _state_param(op: str, conversation_id: str, generation_id: str, state: Any):
    """`state` as a jsonb parameter, or None when there is none, it is not an
    object, it cannot be stored as JSON, or it is over STATE_MAX_BYTES. The
    second value is the stored size in bytes (0 for NULL), for the log.

    NUL code points are removed from the object first (jsonb cannot hold
    them, see `_without_nul`); everything else is stored as the service
    returned it, and the size is measured on the exact text stored."""
    if state is None:
        return None, 0
    if not isinstance(state, dict):
        log.warning(
            "%s %s: state is not an object, stored NULL: conversation=%s generation=%s",
            TABLE, op, conversation_id, generation_id,
        )
        return None, 0
    try:
        clean = _without_nul(state)
        text = strict_json(clean)
        size = len(text.encode("utf-8"))
    except (TypeError, ValueError, RecursionError):  # UnicodeEncodeError is a ValueError
        log.warning(
            "%s %s: state is not storable JSON, stored NULL: conversation=%s generation=%s",
            TABLE, op, conversation_id, generation_id,
        )
        return None, 0
    if size > STATE_MAX_BYTES:
        log.warning(
            "%s %s: state over the cap, stored NULL: conversation=%s generation=%s "
            "bytes=%d cap=%d",
            TABLE, op, conversation_id, generation_id, size, STATE_MAX_BYTES,
        )
        return None, 0
    return Jsonb(clean, dumps=lambda _obj: text), size


def _failed(op: str, exc: BaseException, **ids: Any) -> StoreError:
    """Log one safe line for a failed operation and build the error to raise.

    Only the exception's class and SQLSTATE are kept: psycopg's message and
    DETAIL can quote the failing row, which holds state and summary text."""
    sqlstate = getattr(exc, "sqlstate", None) or type(exc).__name__
    shown = " ".join(f"{key}={value}" for key, value in ids.items())
    log.warning("%s %s failed: %s sqlstate=%s", TABLE, op, shown, sqlstate)
    return StoreError(op, f"sqlstate {sqlstate}")


# ----------------------------------------------------------------- write --


def insert(
    *,
    user_id: int,
    conversation_id: str,
    generation_id: str,
    answer_sha1: str,
    contract: int,
    parent_sha1: str = "",
    question_sha1: str = "",
    workspace_id: Optional[str] = "",
    access_profile: Optional[str] = None,
    state: Optional[dict] = None,
    plan_line: str = "",
    summary: str = "",
    summary_pairs: int = 0,
    summary_at: Any = None,
    path_pairs: int = 0,
    expires_at: Any = None,
) -> Optional[Dict[str, Any]]:
    """Write the row of one answer and return it, or None when nothing was
    written.

    None means one of two harmless things: this generation already has a row
    in this chat (`UNIQUE (conversation_id, generation_id)`: a second tab, a
    retried hook), or the chat is missing or not this user's. Only a returned
    row may spawn a summary fold (design C27).

    `summary`, `summary_pairs` and `summary_at` are the PARENT row's, copied
    so the new row starts from that branch's summary (design §11.3).
    `plan_line` and `summary` are cut to their caps here; a state over
    STATE_MAX_BYTES, or one that is not an object, is written as NULL.

    Raises InvalidArgument for a malformed argument and StoreError for a
    database failure; the write hook logs, counts and carries on."""
    op = "insert"
    uid = _user_id(op, user_id)
    conv = _ident(op, "conversation_id", conversation_id)
    gen = _ident(op, "generation_id", generation_id)
    answer = _sha1(op, "answer_sha1", answer_sha1, allow_empty=False)
    parent = _sha1(op, "parent_sha1", parent_sha1, allow_empty=True)
    question = _sha1(op, "question_sha1", question_sha1, allow_empty=True)
    if isinstance(contract, bool) or not isinstance(contract, int) or not 1 <= contract <= 32767:
        raise InvalidArgument(op, "contract", "an integer from 1 to 32767")
    workspace = _ident(op, "workspace_id", workspace_id, required=False)
    profile = _profile(op, access_profile)
    folded = _count(op, "summary_pairs", summary_pairs)
    pairs = _count(op, "path_pairs", path_pairs)
    folded_at = _timestamp(op, "summary_at", summary_at)
    expires = _timestamp(op, "expires_at", expires_at)
    line = _cut(plan_line, PLAN_LINE_MAX_CHARS)
    text = _cut(summary, SUMMARY_MAX_CHARS)
    state_param, state_bytes = _state_param(op, conv, gen, state)

    # INSERT ... SELECT so the ownership check and the write are one
    # statement: a chat that is not this user's (or no longer exists) gives
    # zero rows rather than a row filed under the wrong person.
    sql = (
        f"INSERT INTO {TABLE} (conversation_id, user_id, workspace_id, access_profile, "
        "generation_id, answer_sha1, parent_sha1, question_sha1, contract, state, "
        "plan_line, summary, summary_pairs, path_pairs, summary_at, expires_at) "
        "SELECT %(conv)s::text, %(uid)s::integer, %(workspace)s::text, %(profile)s::text, "
        "%(gen)s::text, %(answer)s::text, %(parent)s::text, %(question)s::text, "
        "%(contract)s::smallint, %(state)s::jsonb, %(line)s::text, %(summary)s::text, "
        "%(folded)s::integer, %(pairs)s::integer, %(folded_at)s::timestamptz, "
        "%(expires)s::timestamptz "
        "WHERE EXISTS (SELECT 1 FROM conversations c "
        "              WHERE c.id = %(conv)s::text AND c.user_id = %(uid)s::integer) "
        "ON CONFLICT ON CONSTRAINT sf_query_state_generation DO NOTHING "
        f"RETURNING {_COLUMNS}"
    )
    params = {
        "conv": conv, "uid": uid, "workspace": workspace, "profile": profile,
        "gen": gen, "answer": answer, "parent": parent, "question": question,
        "contract": contract, "state": state_param, "line": line, "summary": text,
        "folded": folded, "pairs": pairs, "folded_at": folded_at, "expires": expires,
    }
    try:
        with db.connection() as con:
            row = con.execute(sql, params).fetchone()
    except Exception as exc:  # noqa: BLE001 — memory never fails a turn
        raise _failed(op, exc, conversation=conv, user=uid, generation=gen) from None
    if row is None:
        log.debug(
            "%s insert wrote nothing (duplicate generation or chat not owned): "
            "conversation=%s user=%s generation=%s",
            TABLE, conv, uid, gen,
        )
        return None
    log.debug(
        "%s insert: id=%s conversation=%s user=%s generation=%s state_bytes=%d "
        "plan_line_chars=%d summary_chars=%d path_pairs=%d",
        TABLE, row["id"], conv, uid, gen, state_bytes, len(line), len(text), pairs,
    )
    return dict(row)


# ------------------------------------------------------------------ read --


def fetch_candidates(
    user_id: int,
    conversation_id: str,
    answer_sha1s: Iterable[str],
    *,
    access_profile: Optional[str] = None,
    limit: int = FETCH_LIMIT,
) -> List[Dict[str, Any]]:
    """The live rows of this user's chat whose answer is one of
    `answer_sha1s`, newest first (`created_at DESC, id DESC`), at most `limit`.

    The one indexed read per turn of design §3.1. Always filtered by
    `user_id` and `conversation_id`, and only while the chat itself belongs
    to that user (the same rule the insert applies, so a row filed under
    anyone else is unreachable even if a route forgot its ownership check).
    Expired rows (`expires_at <= now()` on the database clock) and rows
    whose `access_profile` is distinct from the asker's are never fetched.
    The walk-back then calls `pick` per turn on the result."""
    op = "lookup"
    uid = _user_id(op, user_id)
    conv = _ident(op, "conversation_id", conversation_id)
    profile = _profile(op, access_profile)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise InvalidArgument(op, "limit", "an integer from 1 to 500")
    shas: List[str] = []
    for value in answer_sha1s:
        sha = _sha1(op, "answer_sha1s", value, allow_empty=False)
        if sha not in shas:
            shas.append(sha)
    if not shas:
        return []
    try:
        with db.read_connection() as con:
            rows = con.execute(
                f"SELECT {_COLUMNS} FROM {TABLE} s "
                "WHERE s.conversation_id = %s AND s.user_id = %s "
                "  AND EXISTS (SELECT 1 FROM conversations c "
                "              WHERE c.id = s.conversation_id AND c.user_id = s.user_id) "
                "  AND answer_sha1 = ANY(%s::text[]) "
                "  AND access_profile IS NOT DISTINCT FROM %s::text "
                "  AND (expires_at IS NULL OR expires_at > now()) "
                "ORDER BY created_at DESC, id DESC LIMIT %s",
                (conv, uid, shas, profile, limit),
            ).fetchall()
    except Exception as exc:  # noqa: BLE001
        raise _failed(op, exc, conversation=conv, user=uid, hashes=len(shas)) from None
    return [dict(row) for row in rows]


def pick(
    rows: Sequence[Dict[str, Any]],
    *,
    answer_sha1: str,
    parent_sha1: str = "",
    question_sha1: str = "",
    access_profile: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The row of one assistant turn among `rows` (newest first, as
    `fetch_candidates` returns them), or None. Pure: no database.

    A candidate has this turn's answer hash, the hash of the assistant turn
    before it on the path as its parent ('' when this is the first assistant
    turn: an exact match, never a wildcard) and the asker's access profile.
    Among the candidates, the newest whose question hash is the user turn's
    wins, else the newest candidate (design C1). The returned copy carries
    `matched`: "question" or "newest"."""
    candidates = [
        row for row in rows
        if row.get("answer_sha1") == answer_sha1
        and (row.get("parent_sha1") or "") == (parent_sha1 or "")
        and (row.get("access_profile") or None) == (access_profile or None)
    ]
    if not candidates:
        return None
    for row in candidates:
        if (row.get("question_sha1") or "") == (question_sha1 or ""):
            return {**row, "matched": "question"}
    return {**candidates[0], "matched": "newest"}


def lookup(
    user_id: int,
    conversation_id: str,
    *,
    answer_sha1: str,
    parent_sha1: str = "",
    question_sha1: str = "",
    access_profile: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The row of one answer of this user's chat, or None: `fetch_candidates`
    for that one hash, then `pick`."""
    op = "lookup"
    _sha1(op, "parent_sha1", parent_sha1, allow_empty=True)
    _sha1(op, "question_sha1", question_sha1, allow_empty=True)
    rows = fetch_candidates(
        user_id, conversation_id, [answer_sha1], access_profile=access_profile
    )
    return pick(
        rows,
        answer_sha1=answer_sha1,
        parent_sha1=parent_sha1,
        question_sha1=question_sha1,
        access_profile=access_profile,
    )


# --------------------------------------------------------------- summary --


def update_summary(
    conversation_id: str,
    row_id: int,
    *,
    expected_pairs: int,
    summary: str,
    summary_pairs: int,
    user_id: Optional[int] = None,
) -> bool:
    """Compare-and-set the rolling summary of one row; True when it was
    written.

    The row must still hold `expected_pairs` folded pairs, so a late fold
    never overwrites a newer one (design §11.1). The new count must be
    greater than the expected one and no greater than the row's
    `path_pairs`: a fold only moves forward, and never past the path. The
    summary is cut to SUMMARY_MAX_CHARS first; `summary_at` is set on the
    database clock. Filtered by `conversation_id` (and `user_id` when
    given) on top of the row id, and, as every read is, only while the chat
    belongs to the row's user, so a row filed under anyone else is never
    written. The fold names the user whenever its caller gave one (review
    DS-5). Raises StoreError on a database failure."""
    op = "update_summary"
    conv = _ident(op, "conversation_id", conversation_id)
    if isinstance(row_id, bool) or not isinstance(row_id, int) or row_id < 1:
        raise InvalidArgument(op, "row_id", "a positive integer")
    old = _count(op, "expected_pairs", expected_pairs)
    new = _count(op, "summary_pairs", summary_pairs)
    uid = None if user_id is None else _user_id(op, user_id)
    if new <= old:
        log.debug(
            "%s summary not moved forward: id=%s conversation=%s pairs=%d->%d",
            TABLE, row_id, conv, old, new,
        )
        return False
    text = _cut(summary, SUMMARY_MAX_CHARS)
    try:
        with db.connection() as con:
            cur = con.execute(
                f"UPDATE {TABLE} s SET summary = %(summary)s, summary_pairs = %(new)s, "
                "summary_at = now() "
                "WHERE s.id = %(id)s AND s.conversation_id = %(conv)s "
                "  AND s.summary_pairs = %(old)s AND %(new)s <= s.path_pairs "
                "  AND (%(uid)s::integer IS NULL OR s.user_id = %(uid)s::integer) "
                "  AND EXISTS (SELECT 1 FROM conversations c "
                "              WHERE c.id = s.conversation_id AND c.user_id = s.user_id)",
                {"summary": text, "new": new, "id": row_id, "conv": conv, "old": old, "uid": uid},
            )
            written = cur.rowcount == 1
    except Exception as exc:  # noqa: BLE001
        raise _failed(op, exc, id=row_id, conversation=conv) from None
    log.debug(
        "%s summary %s: id=%s conversation=%s pairs=%d->%d chars=%d",
        TABLE, "written" if written else "skipped (row moved on)", row_id, conv, old, new,
        len(text),
    )
    return written


# -------------------------------------------------------------- truncate --


def delete_removed_answers(
    user_id: int,
    conversation_id: str,
    *,
    answer_key: Callable[[str], str],
) -> int:
    """After a truncate, delete the rows of answers that are gone; return how
    many were deleted (design §3.4, C26).

    A row stays while some assistant message still in the chat hashes to its
    `answer_sha1` under `answer_key` (the shared G1 helper, passed in so this
    module holds no second definition). Rows of kept answers stay, with their
    plans and summaries.

    The chat row is locked FOR UPDATE first, as `db.truncate_messages` does:
    a concurrent answer write (its foreign-key check needs a KEY SHARE lock
    on the same row) waits until this finishes, so a row whose message is
    being stored right now is never judged against a stale message list. A
    chat that is missing or not this user's deletes nothing and returns 0.
    Raises StoreError on a database failure."""
    op = "truncate"
    uid = _user_id(op, user_id)
    conv = _ident(op, "conversation_id", conversation_id)
    if not callable(answer_key):
        raise InvalidArgument(op, "answer_key", "the shared answer hash function")
    try:
        with db.connection() as con:
            owned = con.execute(
                "SELECT 1 FROM conversations WHERE id = %s AND user_id = %s FOR UPDATE",
                (conv, uid),
            ).fetchone()
            if not owned:
                return 0
            kept = sorted({
                answer_key(row["content"])
                for row in con.execute(
                    "SELECT content FROM messages "
                    "WHERE conversation_id = %s AND role = 'assistant'",
                    (conv,),
                ).fetchall()
            })
            cur = con.execute(
                f"DELETE FROM {TABLE} "
                "WHERE conversation_id = %s AND user_id = %s "
                "  AND NOT (answer_sha1 = ANY(%s::text[]))",
                (conv, uid, kept),
            )
            deleted = max(0, cur.rowcount)
    except Exception as exc:  # noqa: BLE001
        raise _failed(op, exc, conversation=conv, user=uid) from None
    log.info(
        "%s truncate: conversation=%s user=%s kept_answers=%d deleted_rows=%d",
        TABLE, conv, uid, len(kept), deleted,
    )
    return deleted
