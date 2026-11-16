"""The MCP server's audit log and per-client call limit (standard library only).

Audit log: one JSON line per tool call, resource read and prompt get, appended to ``.private/mcp_log.jsonl``
(``SFA_MCP_LOG``; mode 600). Nothing here rotates,
prunes or rewrites it. A line holds what was called, by which token, how it went and how long it took; never the
arguments (they can name people) and never a result value::

    {"ts": "2026-10-20T05:15:43.123Z", "client": "t-1a2b3c4d", "transport": "http", "method": "tools/call",
     "tool": "count_records", "args_hmac": "9f2c...", "rows": 1, "ms": 41.7, "status": "ok", "error_code": null,
     "refused": null}

``client`` is the token id (``auth.TokenStore``), ``"stdio"`` for the local stdio server. ``tool``: the tool, resource
URI or prompt name when the server has it; any other name the client sent is logged as ``<unknown:...>`` with the
first 12 hex digits of its HMAC, never as sent. ``args_hmac``: HMAC-SHA256 of the arguments as canonical JSON, keyed
with the install's secret key (``.private/mcp_audit_key``, :func:`load_key`): equal arguments give equal values
within one install, but a value cannot be checked against a list of names without the key (a plain SHA-256 of a
person's name can). The first lines of the log (the deploy checks of 2026-10-20, before the keyed hash) carry
``args_sha256`` (a plain SHA-256) instead.
``rows``: the rows the record tool's SQL statements returned (None for the knowledge tools, resources and prompts).
``status``: ``ok``, ``error`` (the tool refused or failed; ``error_code`` is the record tools' code when there is one),
``rate_limited``, ``records_off`` or ``unknown``. ``refused``: on a ``rate_limited`` line, the calls of that client
the limit refused since its previous ``rate_limited`` line (one line per client and window at most, so a looping
client cannot grow the log faster than its allowance); None on every other line. Every text value is cut to
:data:`MAX_FIELD_CHARS`.

:class:`RateLimiter` keeps every client to ``limit`` calls per ``window_s`` seconds (tool calls: default 30 per minute,
the approved design's budget, ``SFA_MCP_RATE_PER_MIN``, 0 turns it off; resource reads and prompt gets have their own
limiter of 4 times that).
"""
from __future__ import annotations

import collections
import datetime as dt
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from typing import Any

__all__ = ["AuditLog", "args_hash", "load_key", "utc_stamp", "RateLimiter", "MAX_FIELD_CHARS", "KEY_BYTES"]

#: a text value of an audit line is cut to this many characters (names the client sent cannot grow the log)
MAX_FIELD_CHARS = 200
#: the audit key: 32 random bytes, stored as 64 hex digits
KEY_BYTES = 32


def utc_stamp() -> str:
    """'2026-10-20T05:15:43.123Z'."""
    now = dt.datetime.now(dt.timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def args_hash(args: Any, key: bytes) -> str:
    """HMAC-SHA256 hex of the arguments as canonical JSON (sorted keys), keyed with ``key``: equal arguments give
    equal values under one key."""
    text = json.dumps(args if args is not None else {}, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)
    return hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()


def load_key(path: str, create: bool = True) -> tuple[bytes, str]:
    """``(key, how)`` of the audit key file ``path`` (64 hex digits): ``how`` is ``"file"``, ``"created"`` (it was
    missing and ``create``: written now, mode 600) or ``"ephemeral"`` (it could not be read or written: a random key
    of this process only, so its values match no other process's). Never raises."""
    try:
        with open(path, encoding="ascii") as fh:
            text = fh.read(4 * KEY_BYTES).strip()
        key = bytes.fromhex(text)
        if len(key) >= KEY_BYTES:
            return key, "file"
    except FileNotFoundError:
        if create:
            key = secrets.token_bytes(KEY_BYTES)
            try:
                os.makedirs(os.path.dirname(os.path.abspath(path)), mode=0o700, exist_ok=True)
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="ascii") as fh:
                    fh.write(key.hex() + "\n")
                return key, "created"
            except FileExistsError:   # written by another process meanwhile: read that one
                return load_key(path, create=False)
            except OSError:
                pass
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    return secrets.token_bytes(KEY_BYTES), "ephemeral"


def _cap(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS - 3] + "..."
    return value


class AuditLog:
    """Append-only JSONL file. Each line is written with one ``write`` call on an ``O_APPEND`` descriptor.

    ``key``: the HMAC key of :meth:`hash_args` (None: a random key of this instance)."""

    def __init__(self, path: str, key: bytes | None = None) -> None:
        self.path = os.path.abspath(str(path))
        self.key = key if key is not None else secrets.token_bytes(KEY_BYTES)
        self._lock = threading.Lock()
        self.failures = 0

    def hash_args(self, args: Any) -> str:
        return args_hash(args, self.key)

    def name_tag(self, name: Any) -> str:
        """``<unknown:...>`` for a name the server does not have: the first 12 hex digits of its HMAC."""
        return f"<unknown:{args_hash(str(name), self.key)[:12]}>"

    def _open(self) -> int:
        return os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)

    def check(self) -> None:
        """Raise OSError when the log cannot be appended to (the server does not start without its audit log)."""
        os.close(self._open())

    def write(self, record: dict) -> bool:
        """Append one line; False (and a line on stderr) when it could not be written."""
        record = {k: _cap(v) for k, v in record.items()}
        line = (json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with self._lock:
            try:
                fd = self._open()
                try:
                    os.write(fd, line)
                finally:
                    os.close(fd)
                return True
            except OSError as exc:
                self.failures += 1
                print(f"sf-assistant-mcp: audit log {self.path} not written: {type(exc).__name__}: {exc}",
                      file=sys.stderr, flush=True)
                return False


class RateLimiter:
    """At most ``limit`` calls per client in any ``window_s`` seconds (``limit`` 0: no limit)."""

    def __init__(self, limit: int = 30, window_s: float = 60.0, clock=time.monotonic) -> None:
        self.limit = max(0, int(limit))
        self.window_s = float(window_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._calls: dict[str, collections.deque] = {}

    def now(self) -> float:
        """The limiter's clock (``time.monotonic`` unless a test gives another)."""
        return self._clock()

    def allow(self, client: str) -> tuple[bool, float]:
        """``(True, 0)`` and the call is counted, or ``(False, seconds until the next call is allowed)``."""
        if self.limit <= 0:
            return True, 0.0
        now = self._clock()
        with self._lock:
            calls = self._calls.setdefault(client, collections.deque())
            while calls and now - calls[0] >= self.window_s:
                calls.popleft()
            if len(calls) >= self.limit:
                return False, round(self.window_s - (now - calls[0]), 1)
            calls.append(now)
            return True, 0.0
