"""Access codes of the web page: generate, store only as hashes, verify. Standard library only.

Two codes: the access code (role ``friend``) and the admin code (role ``admin``). Each is 20 random
characters from a 32-symbol alphabet (100 bits), shown once by ``new`` and stored only as a scrypt hash
with its own salt in ``codes.json`` (mode 600). Every ``new`` also writes a new ``generation``: the
server re-reads the file when it changes and drops every session signed in under an older generation.

    python3 codes.py new PATH      # run.sh new-codes: write PATH, print the two codes once (a terminal only)
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

ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/I: easy to read out and type on a phone
GROUPS, GROUP_LEN = 5, 4
MAX_CODE_CHARS = 64                           # longer input is refused before hashing
SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1, "dklen": 32}
ROLES = ("friend", "admin")


def new_code() -> str:
    """'K7QM-2XW9-RT4P-HN3C-8VZA'."""
    return "-".join("".join(secrets.choice(ALPHABET) for _ in range(GROUP_LEN)) for _ in range(GROUPS))


def normalize(code: object) -> str:
    """Upper case, spaces and dashes removed: 'k7qm 2xw9-...' and 'K7QM-2XW9-...' are the same code."""
    return re.sub(r"[\s\-]+", "", str(code or "")).upper()


def _hash(code: str, salt: bytes, params: dict) -> bytes:
    return hashlib.scrypt(normalize(code).encode("utf-8"), salt=salt, n=int(params["n"]), r=int(params["r"]),
                          p=int(params["p"]), dklen=int(params["dklen"]), maxmem=64 * 1024 * 1024)


def make_record(friend: str, admin: str) -> dict:
    """The codes.json content for two plain codes (hashes only)."""
    if normalize(friend) == normalize(admin):
        raise ValueError("the access code and the admin code must differ")
    entries = {}
    for role, code in (("friend", friend), ("admin", admin)):
        salt = secrets.token_bytes(16)
        entries[role] = {"salt": salt.hex(), "hash": _hash(code, salt, SCRYPT).hex()}
    return {"version": 1, "generation": secrets.token_hex(8),
            "created_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "kdf": "scrypt", "params": dict(SCRYPT), "codes": entries}


def write_codes(path: str) -> tuple[str, str]:
    """Write new codes to ``path`` atomically (mode 600); returns ``(access code, admin code)``."""
    friend, admin = new_code(), new_code()
    while normalize(admin) == normalize(friend):
        admin = new_code()
    record = make_record(friend, admin)
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return friend, admin


def load_codes(path: str) -> dict | None:
    """The codes.json record, or None when the file is missing or not a valid record."""
    try:
        with open(path, encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return None
    try:
        if record.get("kdf") != "scrypt" or not record.get("generation"):
            return None
        for role in ROLES:
            entry = record["codes"][role]
            bytes.fromhex(entry["salt"]), bytes.fromhex(entry["hash"])
        params = record["params"]
        if not all(isinstance(params.get(k), int) for k in ("n", "r", "p", "dklen")):
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return record


def verify(record: dict, code: object) -> str | None:
    """The role whose code ``code`` is, or None. Both hashes are always computed (same time for every input)."""
    text = normalize(code)
    if not text or len(text) > MAX_CODE_CHARS:
        return None
    found = None
    for role in ROLES:
        entry = record["codes"][role]
        digest = _hash(text, bytes.fromhex(entry["salt"]), record["params"])
        if hmac.compare_digest(digest, bytes.fromhex(entry["hash"])):
            found = role
    return found


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "new":
        print("usage: codes.py new PATH", file=sys.stderr)
        return 2
    if not sys.stdout.isatty():   # checked before anything is written: the old codes keep working
        print("refusing to print new access codes: the output is not a terminal (a pipe, a file, a log or an "
              "agent's transcript would keep the plain codes). Run ./run.sh new-codes yourself, in your own "
              "terminal.", file=sys.stderr)
        return 2
    friend, admin = write_codes(argv[1])
    print("New codes written (only their hashes are stored in " + os.path.abspath(argv[1]) + ").")
    print("Every session signed in with the old codes is signed out at its next request.\n")
    print(f"  access code (friend): {friend}")
    print(f"  admin code  (owner) : {admin}\n")
    print("They are shown only now. Give the access code to your friend in person or by a private message.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
