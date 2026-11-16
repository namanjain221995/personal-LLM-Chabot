"""Bearer tokens of the MCP server: generate, store only as SHA-256 hashes, verify. Standard library only.

A token is ``sfa_mcp_`` + 43 URL-safe characters (32 random bytes, 256 bits). The token file (``.private/mcp_token.json``,
mode 600) keeps, per token, a public id (``t-`` + 8 hex digits, the id the audit log names), a label, the SHA-256 hash
of the token and its creation time; never the token. A plain SHA-256 is enough here: a 256-bit random token cannot be
guessed from its hash, and every HTTP request is checked, so a slow hash (scrypt, as for the short page codes) would
only slow every MCP message down.

``new`` rewrites the file (every older token stops working at its next request) and prints the token once; ``--add``
keeps the tokens already in the file (one more client). It refuses to run when its output is not a terminal, so the
plain token never lands in a log, a pipe or an agent's transcript::

    python3 -I src/sf_assistant/server/auth.py new PATH [--label NAME] [--add]     # run.sh new-mcp-token
    ... new PATH --name ask --command new-ask-token                                # run.sh new-ask-token

``--name`` and ``--command`` name the service in the printed texts (default: the MCP server and ``new-mcp-token``), so
that following what a command prints never rotates another service's tokens.

The file is rewritten **in place** (same inode): the MCP container bind-mounts this one file, read-only, and a file
bind mount keeps the inode it was created with, so a rename-replace on the host would never reach the container.

:class:`TokenStore` is what the server uses: it re-reads the file at every check (a few hundred bytes; a rotation
needs no restart) and :meth:`TokenStore.verify` returns the id of the matching token, or None. When the file seen has
no link left (``st_nlink == 0``: on the host it was deleted or replaced by a new file, which the container's bind mount
cannot follow), the state is ``stale`` and every token is refused until the container restarts. This module imports
nothing outside the standard library (run.sh runs it with the host's python3), and nothing of sf_assistant.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading

__all__ = ["TOKEN_PREFIX", "new_token", "token_hash", "make_entry", "write_record", "write_tokens", "load_tokens",
           "TokenStore", "main"]

TOKEN_PREFIX = "sfa_mcp_"
TOKEN_BYTES = 32
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{20,200}$")   # anything else is refused before hashing
_ID_RE = re.compile(r"^t-[0-9a-f]{8}$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9 ._\-]{1,40}$")


def new_token() -> str:
    """'sfa_mcp_' + 43 URL-safe characters (256 random bits)."""
    return TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """SHA-256 hex digest of the token text."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def make_entry(token: str, label: str = "owner") -> dict:
    """The file entry of one token (its hash, never the token)."""
    if not _LABEL_RE.match(label or ""):
        raise ValueError("a label is 1-40 letters, digits, spaces, '.', '_' or '-'")
    return {"id": "t-" + secrets.token_hex(4), "label": label, "sha256": token_hash(token), "created_utc": _utc_now()}


def load_tokens(path: str) -> dict | None:
    """The token file's record, or None when the file is missing, empty or not a valid record."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    return _parse(raw) if len(raw) <= MAX_FILE_BYTES else None


def write_record(path: str, record: dict) -> None:
    """Write the token file ``path`` in place (mode 600, folder mode 700; the inode stays, see the module docstring).

    One ``pwrite`` of the whole text, then ``ftruncate`` and ``fsync``. A reader in between sees the old text or
    text that does not parse (refused: no token is valid for that one check), never a mix that parses. A symlink
    is refused (``O_NOFOLLOW``)."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    data = (json.dumps(record, indent=2) + "\n").encode("utf-8")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.pwrite(fd, data, 0)
        os.ftruncate(fd, len(data))
        os.fsync(fd)
    finally:
        os.close(fd)


def write_tokens(path: str, label: str = "owner", add: bool = False) -> tuple[str, dict]:
    """Write a new token to ``path`` (in place, :func:`write_record`); returns ``(token, its entry)``.

    ``add``: keep the valid tokens already in the file; otherwise the new token is the only one."""
    token = new_token()
    entry = make_entry(token, label)
    path = os.path.abspath(path)
    old = load_tokens(path) if add else None
    tokens = [t for t in (old or {}).get("tokens", []) if t.get("id") != entry["id"]] + [entry]
    write_record(path, {"version": 1, "kdf": "sha256", "updated_utc": _utc_now(), "tokens": tokens})
    return token, entry


MAX_FILE_BYTES = 1 << 20   # a token file is a few hundred bytes; anything bigger is not read


def _parse(raw: bytes) -> dict | None:
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    try:
        if record.get("version") != 1 or record.get("kdf") != "sha256" or not isinstance(record.get("tokens"), list):
            return None
        for entry in record["tokens"]:
            if not (_ID_RE.match(entry["id"]) and _HASH_RE.match(entry["sha256"])):
                return None
    except (KeyError, TypeError, AttributeError):
        return None
    return record


class TokenStore:
    """The token file as the server reads it: read again at every check (by content, not by its stat: an in-place
    rewrite within one clock tick keeps the same mtime and, often, the same size)."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(str(path))
        self._lock = threading.Lock()
        self._raw: bytes | None = None
        self._state = "missing"
        self._hashes: list[tuple[str, bytes]] = []   # (token id, sha256 digest)

    def _refresh(self) -> None:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            self._raw, self._state, self._hashes = None, "missing", []
            return
        try:
            st = os.fstat(fd)
            raw = os.read(fd, MAX_FILE_BYTES + 1) if st.st_nlink else b""
        finally:
            os.close(fd)
        if not st.st_nlink:   # deleted or replaced on the host: this bind mount still shows the old file
            self._raw, self._state, self._hashes = None, "stale", []
            return
        if raw == self._raw:
            return
        record = _parse(raw) if raw.strip() and len(raw) <= MAX_FILE_BYTES else None
        self._hashes = [(t["id"], bytes.fromhex(t["sha256"])) for t in (record or {}).get("tokens", [])]
        self._state = "ok" if self._hashes else ("missing" if not raw.strip() else "invalid")
        self._raw = raw

    def state(self) -> str:
        """``"ok"`` (at least one token), ``"missing"`` (no file, or an empty one: no token set up yet), ``"invalid"``
        (no valid token in it) or ``"stale"`` (the file was replaced on the host; restart the container)."""
        with self._lock:
            self._refresh()
            return self._state

    def count(self) -> int:
        with self._lock:
            self._refresh()
            return len(self._hashes)

    def verify(self, token: object) -> str | None:
        """The id of the token ``token`` is, or None. Every stored hash is compared (same work for any input)."""
        text = str(token or "")
        if not _TOKEN_RE.match(text):
            return None
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        with self._lock:
            self._refresh()
            hashes = list(self._hashes)
        found = None
        for tid, stored in hashes:
            if hmac.compare_digest(digest, stored):
                found = tid
        return found


def main(argv: list[str]) -> int:
    usage = "usage: auth.py new PATH [--label NAME] [--add] [--name WORD] [--command NAME]"
    if len(argv) < 2 or argv[0] != "new":
        print(usage, file=sys.stderr)
        return 2
    path, rest, label, add, name, command = argv[1], argv[2:], "owner", False, "MCP", "new-mcp-token"
    while rest:
        if rest[0] == "--add":
            add, rest = True, rest[1:]
        elif rest[0] == "--label" and len(rest) > 1:
            label, rest = rest[1], rest[2:]
        elif rest[0] == "--name" and len(rest) > 1:
            name, rest = rest[1], rest[2:]
        elif rest[0] == "--command" and len(rest) > 1:
            command, rest = rest[1], rest[2:]
        else:
            print(usage, file=sys.stderr)
            return 2
    if not sys.stdout.isatty():
        print(f"refusing to print a new {name} token: the output is not a terminal (a pipe, a file, a log or an "
              f"agent's transcript would keep the plain token). Run ./run.sh {command} yourself, in your own terminal.",
              file=sys.stderr)
        return 2
    try:
        token, entry = write_tokens(path, label=label, add=add)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"New {name} token written (only its SHA-256 hash is stored in {os.path.abspath(path)}).")
    print("The tokens already in the file are kept." if add else
          f"Every older {name} token stops working at its next request; the server needs no restart.")
    note = "; the audit log names this id" if name == "MCP" else ""
    print(f"\n  token id : {entry['id']}  (label {entry['label']}{note})")
    print(f"  token    : {token}\n")
    print(f"It is shown only now. Put it in the {name} client's configuration (Authorization: Bearer <token>), and")
    print("nowhere else: not in a file in the repository, a chat or a ticket.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
