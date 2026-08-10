"""App-state SQLite store (V2-DESIGN §3c): users, conversations, messages.

Stdlib sqlite3 at APP_DB_PATH (/data/app.sqlite3 in compose), WAL mode. This
is APP state only — the DuckDB/LanceDB-only rule applies to the ANALYTICS
data plane; history/auth use SQLite per the v1 spec's planned "SQLite swap".

Connections are short-lived (one per operation) — simple and correct for a
LAN tool; WAL keeps readers and writers from blocking each other. Nothing
here touches the database at import time.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import settings

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0,
    archived   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    meta            TEXT,
    created_at      TEXT NOT NULL,
    -- Idempotency key for one model generation. Several clients can be
    -- attached to the same detached generation (a second browser joining a
    -- running answer); each would otherwise persist its own copy of the same
    -- reply. Appends carrying a generation_id already present in the
    -- conversation are no-ops.
    generation_id   TEXT
);
-- Phase A: one rolling summary per conversation. Its own table, not a column
-- on `conversations`, so writing a summary never touches the row the sidebar
-- orders by (updated_at).
CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    summary         TEXT NOT NULL,
    -- COUNT of leading messages already folded in. A count (not a row id)
    -- because a request's turns arrive from the client, which has no server
    -- ids; it makes folding idempotent — only messages beyond it are ever
    -- folded, so a crash mid-compact can neither double-fold nor skip.
    covers_through  INTEGER NOT NULL,
    token_estimate  INTEGER NOT NULL,
    updated_at      TEXT NOT NULL
);
-- Phase B: embedded chunks of FOLDED turns, so detail the summary dropped can
-- still be retrieved. Scoped per conversation — the WHERE clause is what
-- enforces session isolation.
CREATE TABLE IF NOT EXISTS conversation_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,
    role            TEXT NOT NULL,
    text            TEXT NOT NULL,
    embedding       BLOB NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE(conversation_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_conversation_chunks_conv
    ON conversation_chunks(conversation_id, ordinal);
-- Phase 4: uploaded datasets. The PROFILE lives here, not the bytes, so a
-- conversation keeps answering questions after the workspace TTL has swept
-- the extracted files away.
CREATE TABLE IF NOT EXISTS uploads (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    filename        TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    status          TEXT NOT NULL,
    profile         TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_conversation
    ON uploads(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_user
    ON conversations(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);
-- Phase 2: pages fetched for a conversation, so follow-up questions about the
-- same URL are answered from stored content without re-fetching.
CREATE TABLE IF NOT EXISTS url_documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    text            TEXT NOT NULL,
    fetched_at      TEXT NOT NULL,
    UNIQUE(conversation_id, url)
);
CREATE INDEX IF NOT EXISTS idx_url_documents_conv
    ON url_documents(conversation_id, id);
-- 2026-08-07: full text of uploaded documents (PDF/DOCX/plain), stored per
-- conversation so EVERY later turn can reference them — the file itself was
-- only ever sent on the turn it was attached, and the model forgot it after.
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    filename        TEXT NOT NULL,
    text            TEXT NOT NULL,
    total_pages     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    UNIQUE(conversation_id, filename)
);
CREATE INDEX IF NOT EXISTS idx_documents_conv
    ON documents(conversation_id, id);
-- Phase 3: cloned+indexed GitHub repos and their code chunks (per conversation).
CREATE TABLE IF NOT EXISTS repos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    repo_key        TEXT NOT NULL,
    url             TEXT NOT NULL,
    sha             TEXT NOT NULL,
    cloned_at       TEXT NOT NULL,
    UNIQUE(conversation_id, repo_key)
);
CREATE TABLE IF NOT EXISTS repo_chunks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    repo_key        TEXT NOT NULL,
    path            TEXT NOT NULL,
    start_line      INTEGER NOT NULL,
    end_line        INTEGER NOT NULL,
    text            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_repo_chunks_conv
    ON repo_chunks(conversation_id, repo_key, id);
"""


# V3-DESIGN §1: columns added to `conversations` after V2 shipped. The live DB
# holds the owner's real conversations, so the only permitted migration step is
# ALTER TABLE ... ADD COLUMN, and only when the column is missing — never a
# DROP, never a CREATE ... AS SELECT rewrite, never a row update.
_ADDED_CONVERSATION_COLUMNS = (
    ("pinned", "INTEGER NOT NULL DEFAULT 0"),
    ("archived", "INTEGER NOT NULL DEFAULT 0"),
)

_ADDED_MESSAGE_COLUMNS = (("generation_id", "TEXT"),)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def migrate(con: sqlite3.Connection) -> None:
    """Bring an existing database up to the current schema, additively.

    Idempotent by construction: every column is added only when
    PRAGMA table_info says it is absent, so running this against an
    already-migrated DB (or twice in a row) is a no-op. Existing rows keep
    their data and pick up the DEFAULT 0 for the new flags.
    """
    columns = {row["name"] for row in con.execute("PRAGMA table_info(conversations)")}
    if not columns:  # table absent (fresh DB before _SCHEMA ran) — nothing to alter
        return
    for name, declaration in _ADDED_CONVERSATION_COLUMNS:
        if name not in columns:
            con.execute(f"ALTER TABLE conversations ADD COLUMN {name} {declaration}")
    message_columns = {row["name"] for row in con.execute("PRAGMA table_info(messages)")}
    if message_columns:
        for name, declaration in _ADDED_MESSAGE_COLUMNS:
            if name not in message_columns:
                con.execute(f"ALTER TABLE messages ADD COLUMN {name} {declaration}")

        # Enforce one-message-per-generation in the DATABASE, not in Python.
        # Two clients attached to the same answer append at the same moment,
        # so an application-level "select then insert" check loses the race and
        # stores the reply twice. A unique index makes the second insert fail,
        # which add_message turns into a no-op.
        #
        # Any duplicates already written by that race have to go first, or the
        # index cannot be built; the earliest row of each pair is kept.
        con.execute(
            "DELETE FROM messages WHERE generation_id IS NOT NULL AND id NOT IN ("
            "  SELECT MIN(id) FROM messages WHERE generation_id IS NOT NULL"
            "  GROUP BY conversation_id, generation_id"
            ")"
        )
        con.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_generation "
            "ON messages(conversation_id, generation_id) "
            "WHERE generation_id IS NOT NULL"
        )
    con.commit()


def connect() -> sqlite3.Connection:
    """Open the app DB (creating the file + schema on first use), WAL mode."""
    path = Path(settings.app_db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(_SCHEMA)  # CREATE ... IF NOT EXISTS: new databases only
    migrate(con)  # existing databases: additive ALTERs for anything missing
    return con


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username: str, password_hash: str) -> int:
    """Insert a user; raises sqlite3.IntegrityError on a duplicate username
    (usernames are UNIQUE COLLATE NOCASE)."""
    with closing(connect()) as con, con:
        cur = con.execute(
            "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
            (username, password_hash, utcnow()),
        )
        return int(cur.lastrowid)


def get_user_by_username(username: str) -> Optional[sqlite3.Row]:
    with closing(connect()) as con:
        return con.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()


def get_user_by_id(user_id: int) -> Optional[sqlite3.Row]:
    with closing(connect()) as con:
        return con.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# ---------------------------------------------------------------------------
# Conversations + messages — every accessor is scoped to a user_id (§3c):
# a conversation owned by someone else is indistinguishable from a missing
# one (the caller turns None/False into 404).
# ---------------------------------------------------------------------------

def _conversation_dict(row: sqlite3.Row) -> dict:
    """Row → JSON-friendly dict; the SQLite 0/1 flags surface as booleans."""
    out = dict(row)
    for flag in ("pinned", "archived"):
        if flag in out:
            out[flag] = bool(out[flag])
    return out


def list_conversations(user_id: int, archived: bool = False) -> List[dict]:
    """The user's conversations, active by default (V3-DESIGN §1).

    `archived=True` returns the archived ones instead — the two sets are
    disjoint, so the sidebar's Archived section is a separate fetch. Pinned
    conversations float to the top, then most-recent activity.
    """
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT id, title, updated_at, pinned, archived FROM conversations "
            "WHERE user_id = ? AND archived = ? "
            "ORDER BY pinned DESC, updated_at DESC, rowid DESC",
            (user_id, 1 if archived else 0),
        ).fetchall()
    return [_conversation_dict(r) for r in rows]


def create_conversation(user_id: int, conversation_id: str, title: str) -> dict:
    """Insert a conversation; raises sqlite3.IntegrityError when the id exists."""
    now = utcnow()
    with closing(connect()) as con, con:
        con.execute(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (conversation_id, user_id, title, now, now),
        )
    return {
        "id": conversation_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "pinned": False,
        "archived": False,
    }


def get_conversation(user_id: int, conversation_id: str) -> Optional[dict]:
    with closing(connect()) as con:
        row = con.execute(
            "SELECT id, title, created_at, updated_at, pinned, archived "
            "FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
    return _conversation_dict(row) if row else None


def update_conversation(
    user_id: int,
    conversation_id: str,
    title: Optional[str] = None,
    pinned: Optional[bool] = None,
    archived: Optional[bool] = None,
) -> Optional[dict]:
    """Update any subset of {title, pinned, archived}; None means "leave alone".

    Only a rename counts as activity: pinning or archiving sets the flag and
    nothing else, so a chat keeps its place in the updated_at ordering when it
    comes back out of the archive (V3-DESIGN §1). Returns None when the
    conversation is missing or owned by someone else.
    """
    assignments: List[str] = []
    values: List[object] = []
    if title is not None:
        assignments += ["title = ?", "updated_at = ?"]
        values += [title, utcnow()]
    if pinned is not None:
        assignments.append("pinned = ?")
        values.append(1 if pinned else 0)
    if archived is not None:
        assignments.append("archived = ?")
        values.append(1 if archived else 0)
    if not assignments:  # nothing to change — just an ownership-scoped read
        return get_conversation(user_id, conversation_id)
    with closing(connect()) as con, con:
        cur = con.execute(
            f"UPDATE conversations SET {', '.join(assignments)} "
            "WHERE id = ? AND user_id = ?",
            (*values, conversation_id, user_id),
        )
        if cur.rowcount == 0:
            return None
    return get_conversation(user_id, conversation_id)


def delete_conversation(user_id: int, conversation_id: str) -> bool:
    with closing(connect()) as con, con:
        cur = con.execute(
            "DELETE FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        )
        return cur.rowcount > 0


def list_messages(conversation_id: str) -> List[dict]:
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT role, content, meta FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    out: List[dict] = []
    for r in rows:
        meta = json.loads(r["meta"]) if r["meta"] else None
        out.append({"role": r["role"], "content": r["content"], "meta": meta})
    return out


def conversation_owner(conversation_id: str) -> Optional[int]:
    """user_id owning this conversation, or None when no such row exists.

    Used to authorize the per-conversation stores (url_documents, repo_chunks,
    live generations) that are keyed by conversation id alone.
    """
    with closing(connect()) as con:
        row = con.execute(
            "SELECT user_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
    return int(row["user_id"]) if row else None


class MessageCountWouldShrink(Exception):
    """A sync tried to replace a thread with a SHORTER one.

    The client's offline sync used to reconcile a diverged tail by deleting
    the conversation and recreating it from its local copy. When that local
    copy was empty or stale (a chat listed by the server but never opened in
    this browser, or one evicted by a localStorage quota purge) the rebuild
    destroyed every earlier turn. Shrinking is never a legitimate sync
    outcome — only an explicit DELETE of the whole conversation is.
    """

    def __init__(self, existing: int, incoming: int) -> None:
        super().__init__(
            f"refusing to replace {existing} messages with {incoming}"
        )
        self.existing = existing
        self.incoming = incoming


# ---------------------------------------------------------------------------
# Phase A/B: rolling summary + folded-turn chunks
# ---------------------------------------------------------------------------


def get_summary(conversation_id: str) -> Optional[dict]:
    with closing(connect()) as con:
        row = con.execute(
            "SELECT summary, covers_through, token_estimate, updated_at "
            "FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "summary": row["summary"],
        "covers_through": int(row["covers_through"]),
        "token_estimate": int(row["token_estimate"]),
        "updated_at": row["updated_at"],
    }


def save_summary(
    conversation_id: str, summary: str, covers_through: int, token_estimate: int
) -> None:
    with closing(connect()) as con, con:
        con.execute(
            "INSERT INTO conversation_summaries "
            "(conversation_id, summary, covers_through, token_estimate, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id) DO UPDATE SET "
            "summary = excluded.summary, covers_through = excluded.covers_through, "
            "token_estimate = excluded.token_estimate, updated_at = excluded.updated_at",
            (conversation_id, summary, covers_through, token_estimate, utcnow()),
        )


def clear_summary(conversation_id: str) -> None:
    """Drop the summary and folded chunks for a conversation.

    Used when the thread is truncated (a confirmed regenerate of an older
    answer): the summary describes turns that no longer exist, so keeping it
    would let the model assert things the user deliberately removed.
    """
    with closing(connect()) as con, con:
        con.execute(
            "DELETE FROM conversation_summaries WHERE conversation_id = ?",
            (conversation_id,),
        )
        con.execute(
            "DELETE FROM conversation_chunks WHERE conversation_id = ?",
            (conversation_id,),
        )


def add_conversation_chunks(conversation_id: str, chunks: List[dict]) -> None:
    """Store embedded chunks of folded turns (ordinal = index in the thread)."""
    if not chunks:
        return
    now = utcnow()
    with closing(connect()) as con, con:
        for c in chunks:
            con.execute(
                "INSERT OR REPLACE INTO conversation_chunks "
                "(conversation_id, ordinal, role, text, embedding, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    int(c["ordinal"]),
                    c["role"],
                    c["text"],
                    c["embedding"],
                    now,
                ),
            )


def get_conversation_chunks(conversation_id: str) -> List[dict]:
    """Every folded chunk for ONE conversation — the isolation boundary."""
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT ordinal, role, text, embedding FROM conversation_chunks "
            "WHERE conversation_id = ? ORDER BY ordinal",
            (conversation_id,),
        ).fetchall()
    return [
        {
            "ordinal": int(r["ordinal"]),
            "role": r["role"],
            "text": r["text"],
            "embedding": r["embedding"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Phase 4: uploaded datasets (profile stored, bytes expire separately)
# ---------------------------------------------------------------------------


def save_upload(
    upload_id: str,
    conversation_id: str,
    filename: str,
    size: int,
    status: str,
    profile: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    with closing(connect()) as con, con:
        con.execute(
            "INSERT INTO uploads (id, conversation_id, filename, bytes, status, "
            "profile, notes, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET status = excluded.status, "
            "profile = excluded.profile, notes = excluded.notes",
            (
                upload_id,
                conversation_id,
                filename,
                size,
                status,
                profile,
                notes,
                utcnow(),
            ),
        )


def get_uploads(conversation_id: str) -> List[dict]:
    """Uploads for ONE conversation — the isolation boundary."""
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT id, filename, bytes, status, profile, notes, created_at "
            "FROM uploads WHERE conversation_id = ? ORDER BY created_at",
            (conversation_id,),
        ).fetchall()
    out = []
    for r in rows:
        out.append(
            {
                "id": r["id"],
                "filename": r["filename"],
                "bytes": int(r["bytes"]),
                "status": r["status"],
                "profile": json.loads(r["profile"]) if r["profile"] else None,
                "notes": r["notes"],
                "created_at": r["created_at"],
            }
        )
    return out


def get_upload(upload_id: str) -> Optional[dict]:
    with closing(connect()) as con:
        row = con.execute(
            "SELECT id, conversation_id, filename, bytes, status, profile, notes "
            "FROM uploads WHERE id = ?",
            (upload_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "filename": row["filename"],
        "bytes": int(row["bytes"]),
        "status": row["status"],
        "profile": json.loads(row["profile"]) if row["profile"] else None,
        "notes": row["notes"],
    }


class ConversationChanged(Exception):
    """The stored thread is not the one the caller was looking at."""

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(f"expected {expected} messages, found {actual}")
        self.expected = expected
        self.actual = actual


def truncate_messages(
    user_id: int, conversation_id: str, keep: int, expected_total: int
) -> Optional[dict]:
    """Drop every message after the first `keep` — the ONE sanctioned shrink.

    This exists so a user-confirmed "regenerate an older answer" (which really
    does discard the turns after that point) does not need the sync path to be
    able to shrink. The separation is the safety property: the sync path can
    never reduce a thread, and this endpoint can only ever delete a tail — it
    cannot write content, so a bug here cannot rewrite history, only shorten
    it, and only when the caller proves it knows the current length.

    `expected_total` is optimistic concurrency: if another tab appended turns
    since the caller last looked, this raises instead of destroying them.
    """
    with closing(connect()) as con, con:
        owned = con.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if not owned:
            return None
        actual = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["n"]
        )
        if actual != expected_total:
            raise ConversationChanged(expected_total, actual)
        if keep >= actual:
            return {"id": conversation_id, "count": actual}
        con.execute(
            "DELETE FROM messages WHERE conversation_id = ? AND id NOT IN ("
            "  SELECT id FROM messages WHERE conversation_id = ? ORDER BY id LIMIT ?"
            ")",
            (conversation_id, conversation_id, keep),
        )
        con.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (utcnow(), conversation_id),
        )
    return {"id": conversation_id, "count": keep}


def replace_messages(
    user_id: int, conversation_id: str, messages: List[dict]
) -> Optional[dict]:
    """Atomically replace a conversation's messages, never reducing the count.

    Returns None when the conversation is missing or owned by someone else,
    and raises MessageCountWouldShrink when the incoming thread is shorter
    than what is stored. The delete+insert runs in ONE transaction, so a
    failure cannot leave the conversation empty.
    """
    now = utcnow()
    with closing(connect()) as con, con:
        owned = con.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if not owned:
            return None
        existing = int(
            con.execute(
                "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()["n"]
        )
        if len(messages) < existing:
            raise MessageCountWouldShrink(existing, len(messages))
        con.execute(
            "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,)
        )
        seen_generations = set()
        for m in messages:
            meta = m.get("meta")
            gen_id = (meta or {}).get("generation_id")
            # The unique index would reject a repeated key; a client sending
            # one twice is a bug on its side, not a reason to 500 — keep the
            # first and store the rest unkeyed.
            if gen_id:
                if gen_id in seen_generations:
                    gen_id = None
                else:
                    seen_generations.add(gen_id)
            con.execute(
                "INSERT INTO messages (conversation_id, role, content, meta, created_at, "
                "generation_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    m.get("role", ""),
                    m.get("content", ""),
                    json.dumps(meta, ensure_ascii=False, default=str)
                    if meta is not None
                    else None,
                    now,
                    str(gen_id) if gen_id else None,
                ),
            )
        con.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?",
            (now, conversation_id),
        )
    return {"id": conversation_id, "count": len(messages)}


def add_message(
    user_id: int,
    conversation_id: str,
    role: str,
    content: str,
    meta: Optional[dict] = None,
) -> Optional[dict]:
    """Append a message to a conversation the user owns; bumps updated_at.

    Returns None when the conversation is missing or owned by someone else.

    IDEMPOTENT per generation: when `meta.generation_id` is present and a
    message from that same generation already exists here, the existing row is
    returned untouched. Two clients attached to one detached generation (a
    second browser opening a running answer) both finalize and both push —
    without this the same reply is stored twice.
    """
    now = utcnow()
    generation_id = (meta or {}).get("generation_id")
    with closing(connect()) as con, con:
        owned = con.execute(
            "SELECT 1 FROM conversations WHERE id = ? AND user_id = ?",
            (conversation_id, user_id),
        ).fetchone()
        if not owned:
            return None
        def _existing_generation_row():
            row = con.execute(
                "SELECT id, role, content, meta, created_at FROM messages "
                "WHERE conversation_id = ? AND generation_id = ?",
                (conversation_id, str(generation_id)),
            ).fetchone()
            if row is None:
                return None
            return {
                "id": int(row["id"]),
                "role": row["role"],
                "content": row["content"],
                "meta": json.loads(row["meta"]) if row["meta"] else None,
                "created_at": row["created_at"],
                "deduplicated": True,
            }

        try:
            cur = con.execute(
                "INSERT INTO messages (conversation_id, role, content, meta, created_at, "
                "generation_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    conversation_id,
                    role,
                    content,
                    json.dumps(meta, ensure_ascii=False, default=str)
                    if meta is not None
                    else None,
                    now,
                    str(generation_id) if generation_id else None,
                ),
            )
        except sqlite3.IntegrityError:
            # The unique index rejected it: another client stored this exact
            # generation first. Hand back the row that won — no duplicate.
            if generation_id:
                duplicate = _existing_generation_row()
                if duplicate is not None:
                    return duplicate
            raise
        con.execute(
            "UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id)
        )
        message_id = int(cur.lastrowid)
    return {"id": message_id, "role": role, "content": content, "meta": meta, "created_at": now}


# ---------------------------------------------------------------------------
# Phase 2: fetched-page storage (per conversation, so follow-ups don't refetch)
# ---------------------------------------------------------------------------

def save_url_document(
    conversation_id: str, url: str, title: str, text: str
) -> None:
    """Store (or refresh) an extracted page for a conversation."""
    with closing(connect()) as con, con:
        con.execute(
            "INSERT INTO url_documents (conversation_id, url, title, text, fetched_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id, url) DO UPDATE SET "
            "title=excluded.title, text=excluded.text, fetched_at=excluded.fetched_at",
            (conversation_id, url, title, text, utcnow()),
        )


def get_url_documents(conversation_id: str) -> List[dict]:
    """All pages stored for a conversation, oldest first."""
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT url, title, text FROM url_documents "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return [{"url": r["url"], "title": r["title"], "text": r["text"]} for r in rows]


def get_url_document_urls(conversation_id: str) -> set:
    """The set of URLs already fetched for a conversation (dupe check)."""
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT url FROM url_documents WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
    return {r["url"] for r in rows}


def save_document(
    conversation_id: str, filename: str, text: str, total_pages: int = 0
) -> None:
    """Store (or refresh) an uploaded document's extracted text."""
    with closing(connect()) as con, con:
        con.execute(
            "INSERT INTO documents (conversation_id, filename, text, total_pages, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id, filename) DO UPDATE SET "
            "text=excluded.text, total_pages=excluded.total_pages, "
            "created_at=excluded.created_at",
            (conversation_id, filename, text, total_pages, utcnow()),
        )


def get_documents(conversation_id: str) -> List[dict]:
    """All documents stored for a conversation, oldest first."""
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT filename, text, total_pages FROM documents "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return [
        {"filename": r["filename"], "text": r["text"], "total_pages": r["total_pages"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Phase 3: repo + code-chunk storage (per conversation)
# ---------------------------------------------------------------------------

def save_repo(conversation_id: str, repo_key: str, url: str, sha: str) -> None:
    with closing(connect()) as con, con:
        con.execute(
            "INSERT INTO repos (conversation_id, repo_key, url, sha, cloned_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(conversation_id, repo_key) DO UPDATE SET "
            "url=excluded.url, sha=excluded.sha, cloned_at=excluded.cloned_at",
            (conversation_id, repo_key, url, sha, utcnow()),
        )


def get_repo(conversation_id: str, repo_key: str) -> Optional[dict]:
    with closing(connect()) as con:
        r = con.execute(
            "SELECT repo_key, url, sha FROM repos "
            "WHERE conversation_id = ? AND repo_key = ?",
            (conversation_id, repo_key),
        ).fetchone()
    return dict(r) if r else None


def get_repo_keys(conversation_id: str) -> List[str]:
    with closing(connect()) as con:
        rows = con.execute(
            "SELECT repo_key FROM repos WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        ).fetchall()
    return [r["repo_key"] for r in rows]


def replace_repo_chunks(
    conversation_id: str, repo_key: str, chunks: List[dict]
) -> None:
    """Store a repo's code chunks, replacing any previous set for that repo."""
    with closing(connect()) as con, con:
        con.execute(
            "DELETE FROM repo_chunks WHERE conversation_id = ? AND repo_key = ?",
            (conversation_id, repo_key),
        )
        con.executemany(
            "INSERT INTO repo_chunks "
            "(conversation_id, repo_key, path, start_line, end_line, text) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (conversation_id, repo_key, c["path"], c["start_line"], c["end_line"], c["text"])
                for c in chunks
            ],
        )


def search_repo_chunks(
    conversation_id: str, keywords: List[str], limit: int = 12
) -> List[dict]:
    """Code chunks in the conversation's repos matching any keyword, ranked by
    number of matching keywords (path matches weighted). Returns
    [{path, start_line, end_line, text}]."""
    if not keywords:
        return []
    patterns = [like_contains_pattern(k) for k in keywords]
    # score = matches in text + 2x matches in path (path names are strong signal)
    score_terms = " + ".join(
        "(CASE WHEN text LIKE ? ESCAPE '\\' THEN 1 ELSE 0 END) + "
        "(CASE WHEN path LIKE ? ESCAPE '\\' THEN 2 ELSE 0 END)"
        for _ in keywords
    )
    like_any = " OR ".join(
        "text LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\'" for _ in keywords
    )
    # Doc files (README/HISTORY/…) often mention a term in prose; for "where is
    # X handled" the real source file should win, so penalize docs a little.
    doc_penalty = (
        "(CASE WHEN lower(path) LIKE '%.md' OR lower(path) LIKE '%.rst' "
        "OR lower(path) LIKE '%.txt' OR lower(path) LIKE '%.markdown' "
        "THEN 2 ELSE 0 END)"
    )
    sql = (
        f"SELECT path, start_line, end_line, text, "
        f"(({score_terms}) - {doc_penalty}) AS score "
        "FROM repo_chunks WHERE conversation_id = ? "
        f"AND ({like_any}) ORDER BY score DESC, id LIMIT ?"
    )
    # Bind in the ORDER the ? placeholders appear in the SQL text:
    # score_terms (SELECT) → conversation_id (WHERE) → like_any (AND) → limit.
    params: List[object] = []
    for p in patterns:
        params += [p, p]  # score_terms
    params.append(conversation_id)
    for p in patterns:
        params += [p, p]  # like_any
    params.append(limit)
    with closing(connect()) as con:
        rows = con.execute(sql, params).fetchall()
    return [
        {
            "path": r["path"],
            "start_line": r["start_line"],
            "end_line": r["end_line"],
            "text": r["text"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Search (V4-DESIGN §2) — one row per conversation, matched on the title or on
# any message body, scoped to the owner like every other accessor here.
# ---------------------------------------------------------------------------

SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 100  # hard cap: a bigger ?limit is clamped, never an error

_LIKE_ESCAPE = "\\"
_SNIPPET_WIDTH = 120
_ELLIPSIS = "…"


def like_contains_pattern(needle: str) -> str:
    """`needle` as a literal LIKE "contains" pattern.

    The user's text is data, not syntax: `%`, `_` and the escape character
    itself are escaped so searching for "50%" or "q_1" matches those literal
    characters instead of turning into a wildcard. Every statement using this
    pattern MUST pair it with the matching ESCAPE clause (see _SEARCH_SQL) —
    without one, SQLite treats the backslashes as ordinary characters and the
    escaping silently becomes corruption.
    """
    escaped = (
        needle.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", _LIKE_ESCAPE + "%")
        .replace("_", _LIKE_ESCAPE + "_")
    )
    return f"%{escaped}%"


def snippet_window(content: str, needle: str, width: int = _SNIPPET_WIDTH) -> str:
    """A ~`width`-character window of `content` centered on the first
    case-insensitive occurrence of `needle`.

    Short messages come back whole; a trimmed end gets an ellipsis so the
    palette can show that the message continues. When the hit sits near the
    tail the window is slid back so it stays `width` wide instead of running
    short. `needle` not being found is not an error (SQLite's LIKE and Python's
    casefolding can disagree on exotic characters) — the head of the message is
    a reasonable snippet in that case.
    """
    if len(content) <= width:
        return content
    hit = content.lower().find(needle.lower())
    center = 0 if hit < 0 else hit + len(needle) // 2
    start = max(0, center - width // 2)
    end = min(len(content), start + width)
    start = max(0, end - width)  # re-widen when the hit sits near the tail
    snippet = content[start:end]
    if start > 0:
        snippet = _ELLIPSIS + snippet
    if end < len(content):
        snippet = snippet + _ELLIPSIS
    return snippet


# `:pattern` is reused for the title test, the existence test and the snippet
# lookup. ESCAPE '\' is what makes like_contains_pattern's escaping real.
# Archived conversations are deliberately NOT filtered out — they come back
# flagged so the palette can label them (§2).
_SEARCH_SQL = """
SELECT c.id, c.title, c.updated_at, c.pinned, c.archived,
       (SELECT m.content
          FROM messages m
         WHERE m.conversation_id = c.id
           AND m.content LIKE :pattern ESCAPE '\\'
         ORDER BY m.id
         LIMIT 1) AS match_content
  FROM conversations c
 WHERE c.user_id = :user_id
   AND (c.title LIKE :pattern ESCAPE '\\'
        OR EXISTS (SELECT 1
                     FROM messages m2
                    WHERE m2.conversation_id = c.id
                      AND m2.content LIKE :pattern ESCAPE '\\'))
 ORDER BY c.pinned DESC, c.updated_at DESC, c.rowid DESC
 LIMIT :limit
"""


_RECALL_SNIPPET_CHARS = 240


def recall_conversations(
    user_id: int,
    keywords: List[str],
    exclude_conversation_id: Optional[str],
    limit: int = 3,
) -> List[dict]:
    """Cross-chat memory: the user's OTHER conversations whose messages mention
    any of `keywords`, ranked by how many messages match. Returns
    [{id, title, snippet}] — a compact recall context for the model. Scoped to
    the owner like every accessor here; the current conversation is excluded.
    """
    if not keywords:
        return []
    patterns = [like_contains_pattern(k) for k in keywords]
    like_any = " OR ".join("m.content LIKE ? ESCAPE '\\'" for _ in keywords)
    rank_sql = (
        "SELECT c.id, c.title, COUNT(m.id) AS hits "
        "FROM conversations c JOIN messages m ON m.conversation_id = c.id "
        f"WHERE c.user_id = ? AND c.id != ? AND ({like_any}) "
        "GROUP BY c.id ORDER BY hits DESC, c.updated_at DESC LIMIT ?"
    )
    snip_sql = (
        "SELECT m.content FROM messages m "
        f"WHERE m.conversation_id = ? AND ({like_any}) ORDER BY m.id LIMIT 1"
    )
    out: List[dict] = []
    with closing(connect()) as con:
        rows = con.execute(
            rank_sql,
            (user_id, exclude_conversation_id or "", *patterns, limit),
        ).fetchall()
        for r in rows:
            srow = con.execute(snip_sql, (r["id"], *patterns)).fetchone()
            content = (srow["content"] if srow else "") or ""
            snippet = content.strip().replace("\n", " ")
            if len(snippet) > _RECALL_SNIPPET_CHARS:
                snippet = snippet[:_RECALL_SNIPPET_CHARS] + "…"
            out.append({"id": r["id"], "title": r["title"], "snippet": snippet})
    return out


def search_conversations(
    user_id: int, query: str, limit: int = SEARCH_LIMIT_DEFAULT
) -> List[dict]:
    """The user's conversations matching `query` in the title or any message.

    Returns the documented row shape: {id, title, updated_at, pinned, archived,
    snippet, matched_in}. `matched_in` is "message" whenever some message body
    matched (the snippet then windows the FIRST such message) and "title" only
    when the title alone matched — so `snippet is None` exactly when
    `matched_in == "title"`, which is the invariant the palette renders on.
    """
    query = query.strip()
    if not query:  # empty/whitespace query is a no-op, not an error (§2)
        return []
    limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    with closing(connect()) as con:
        rows = con.execute(
            _SEARCH_SQL,
            {
                "pattern": like_contains_pattern(query),
                "user_id": user_id,
                "limit": limit,
            },
        ).fetchall()
    out: List[dict] = []
    for row in rows:
        match_content = row["match_content"]
        out.append(
            {
                "id": row["id"],
                "title": row["title"],
                "updated_at": row["updated_at"],
                "pinned": bool(row["pinned"]),
                "archived": bool(row["archived"]),
                "snippet": (
                    snippet_window(match_content, query)
                    if match_content is not None
                    else None
                ),
                "matched_in": "message" if match_content is not None else "title",
            }
        )
    return out
