"""Password hashing — Argon2id, tuned for this hardware, upgrade-aware.

argon2-cffi was already a dependency (the removed V2 login used it), so no new
package. Parameters follow the OWASP 2024 baseline (19 MiB / t=2 / p=1) rather
than argon2-cffi's heavier defaults: login runs on the same box as inference,
and a burst of logins must not steal hundreds of MB from the model's unified
memory. `verify()` reports when a stored hash predates the current parameters
so callers can transparently re-hash on the next successful login.
"""
from __future__ import annotations

import logging
from typing import Tuple

from argon2 import PasswordHasher
from argon2 import exceptions as argon2_exceptions
from argon2.low_level import Type

log = logging.getLogger(__name__)

_hasher = PasswordHasher(
    time_cost=2,
    memory_cost=19 * 1024,  # KiB → 19 MiB
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)

#: Sentinel prefix for accounts that CANNOT log in (the pre-bootstrap local
#: account, or an invited user who has not accepted yet). Argon2 hashes start
#: with "$argon2", so "!" can never collide with a real hash.
UNUSABLE = "!"

MIN_LENGTH = 10
MAX_LENGTH = 256  # argon2 is not bcrypt (no 72-byte cliff), but bound the work


def is_usable(stored: str) -> bool:
    return bool(stored) and not stored.startswith(UNUSABLE)


def validate_new_password(password: str) -> str | None:
    """None when acceptable, else a human-readable reason.

    Length is the only hard rule (NIST 800-63B: length beats composition
    rules). The trivial-password check exists because "password01" satisfies
    any length rule and appears in every credential-stuffing list.
    """
    if len(password) < MIN_LENGTH:
        return f"Use at least {MIN_LENGTH} characters."
    if len(password) > MAX_LENGTH:
        return f"Use at most {MAX_LENGTH} characters."
    lowered = password.lower()
    trivial = {"password", "techsara", "qwertyuiop", "1234567890", "salesforce"}
    if lowered.strip("0123456789!@#$%^&*.").lower() in trivial or lowered in trivial:
        return "That password is too guessable — pick something less common."
    return None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify(stored: str, candidate: str) -> Tuple[bool, bool]:
    """(matches, needs_rehash).

    Never raises on a malformed/unusable stored value — an unusable hash is an
    ordinary failed login, not a 500. needs_rehash is only meaningful when
    matches is True.
    """
    if not is_usable(stored):
        # Burn comparable time so "account exists but has no password" is not
        # distinguishable from "wrong password" by response timing.
        _burn()
        return False, False
    try:
        _hasher.verify(stored, candidate)
    except argon2_exceptions.VerifyMismatchError:
        return False, False
    except argon2_exceptions.InvalidHashError:
        log.warning("unreadable password hash encountered")
        return False, False
    return True, _hasher.check_needs_rehash(stored)


_BURN_HASH = _hasher.hash("timing-equalizer")


def _burn() -> None:
    try:
        _hasher.verify(_BURN_HASH, "wrong-on-purpose")
    except argon2_exceptions.VerifyMismatchError:
        pass
