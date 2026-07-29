"""Salesforce credentials — entirely local. No cloud calls (2026-07-28).

AWS Secrets Manager was removed at the owner's request: this platform runs on
one machine and reaching out to a cloud secret store to start a local sync was
both a dependency and a running cost. The credentials now come from the
environment only:

    SF_CLIENT_ID, SF_USERNAME, SF_LOGIN_URL
    plus ONE of:
      SF_CLIENT_SECRET     the connected app's Consumer Secret — OAuth 2.0
                           client-credentials grant. Simplest: no key, no
                           certificate, and the app runs as its configured
                           "Run As" user. SF_LOGIN_URL must be the org's My
                           Domain; Salesforce refuses this grant on
                           login.salesforce.com.
      SF_PRIVATE_KEY_FILE  path to the .pem/.key holding the JWT signing key
      SF_PRIVATE_KEY_B64   the same key, base64-encoded

The secret is tried first, then the key file, then the base64 key. Missing or
malformed credentials RAISE rather than falling back to anything, so a
misconfiguration is visible at startup instead of silently working through a
path nobody meant to use.

The JWT bearer flow signs with the RSA PRIVATE KEY that pairs with the
certificate uploaded to the connected app. A certificate fingerprint /
thumbprint (the 64-character hex string Salesforce shows next to the cert) is
NOT a key and cannot sign anything — passing one is a common mix-up, so it is
rejected with a message that says so rather than a vague parse error.

Secret VALUES are never logged and never appear in reprs or exception
messages — only key names do.
"""

from __future__ import annotations

import base64
import binascii
import os
import re
from dataclasses import dataclass
from pathlib import Path

ENV_KEYS = ("SF_CLIENT_ID", "SF_USERNAME", "SF_LOGIN_URL", "SF_PRIVATE_KEY_B64")
#: The same three identity vars, with the key supplied as a file path instead.
ENV_KEYS_FILE = ("SF_CLIENT_ID", "SF_USERNAME", "SF_LOGIN_URL", "SF_PRIVATE_KEY_FILE")

#: Where the signing key lives when no path is configured. The data volume,
#: not .env: an .env value leaks into `docker inspect`, shell history and
#: screenshots, and this file only ever needs to be readable by this service.
DEFAULT_KEY_PATH = "/data/sf_jwt_key.pem"

#: A cert thumbprint: 40 (SHA-1) or 64 (SHA-256) hex chars, nothing else.
_THUMBPRINT_RE = re.compile(r"^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$")


def _looks_like_a_thumbprint(value: str) -> bool:
    return bool(_THUMBPRINT_RE.match(value.strip().replace(":", "")))


def _check_pem(pem: bytes, source: str) -> bytes:
    """Fail loudly when the material is not actually a private key.

    Returns the bytes UNCHANGED — no stripping. Key material is handed to a
    crypto library verbatim; trimming it here would be an invisible edit to
    something this module has no business rewriting.
    """
    head = pem.lstrip()[:200]
    if b"PRIVATE KEY" not in head:
        if b"CERTIFICATE" in head:
            raise ValueError(
                f"{source} contains a CERTIFICATE, not a private key. The JWT "
                "flow signs with the private key that pairs with the "
                "certificate you uploaded to the connected app."
            )
        raise ValueError(
            f"{source} is not a PEM private key (expected a "
            "'-----BEGIN PRIVATE KEY-----' block)"
        )
    return pem


@dataclass
class SalesforceCredentials:
    client_id: str
    username: str
    login_url: str
    private_key_pem: bytes = b""
    #: Consumer Secret. When set, the client-credentials grant is used and no
    #: private key is needed at all.
    client_secret: str = ""

    def __repr__(self) -> str:  # never leak values via logging/tracebacks
        return "SalesforceCredentials(<redacted>)"

    __str__ = __repr__


def _identity_present(e) -> bool:
    return all(e.get(k) for k in ("SF_CLIENT_ID", "SF_USERNAME", "SF_LOGIN_URL"))


def _pem_from_file(path_value: str) -> bytes:
    """Read the private key named by SF_PRIVATE_KEY_FILE."""
    value = path_value.strip()
    if _looks_like_a_thumbprint(value):
        raise ValueError(
            "SF_PRIVATE_KEY_FILE looks like a certificate THUMBPRINT, not a "
            "file path. It must point at the .pem/.key file holding the "
            "PRIVATE KEY that pairs with the certificate on the connected app "
            "— a fingerprint cannot sign a JWT."
        )
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"SF_PRIVATE_KEY_FILE does not exist: {value}")
    return _check_pem(path.read_bytes(), "SF_PRIVATE_KEY_FILE")


def credentials_from_env(env: dict | None = None) -> SalesforceCredentials | None:
    """Build credentials from direct env vars; None when the key is missing.

    The key may be a FILE path (preferred — nothing secret lands in .env) or
    base64 in the environment. Identity vars without either return None so the
    AWS fallback still runs; a key that IS supplied but is broken raises, since
    silently falling back would hide the real misconfiguration.
    """
    e = os.environ if env is None else env
    if not _identity_present(e):
        return None

    secret = (e.get("SF_CLIENT_SECRET") or "").strip()
    if secret:
        # No key material involved: the app authenticates as itself.
        return SalesforceCredentials(
            client_id=str(e["SF_CLIENT_ID"]),
            username=str(e["SF_USERNAME"]),
            login_url=str(e["SF_LOGIN_URL"]).rstrip("/"),
            client_secret=secret,
        )

    key_file = (e.get("SF_PRIVATE_KEY_FILE") or "").strip()
    key_b64 = (e.get("SF_PRIVATE_KEY_B64") or "").strip()
    if key_file:
        pem = _pem_from_file(key_file)
    elif key_b64:
        try:
            pem = base64.b64decode(key_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("SF_PRIVATE_KEY_B64 is not valid base64") from exc
        pem = _check_pem(pem, "SF_PRIVATE_KEY_B64")
    elif Path(DEFAULT_KEY_PATH).is_file():
        pem = _check_pem(Path(DEFAULT_KEY_PATH).read_bytes(), DEFAULT_KEY_PATH)
    else:
        return None  # identity present but no key anywhere

    return SalesforceCredentials(
        client_id=str(e["SF_CLIENT_ID"]),
        username=str(e["SF_USERNAME"]),
        login_url=str(e["SF_LOGIN_URL"]).rstrip("/"),
        private_key_pem=pem,
    )


def fetch_sf_credentials(
    secret_name: str | None = None, region: str | None = None
) -> SalesforceCredentials:
    """Resolve Salesforce credentials from the environment.

    The arguments are ignored; they remain so the sync worker's call site (and
    anything else passing the old AWS settings) keeps working after Secrets
    Manager was removed.
    """
    del secret_name, region
    from_env = credentials_from_env()
    if from_env is not None:
        return from_env
    raise ValueError(
        "No Salesforce credentials: set SF_CLIENT_ID, SF_USERNAME and "
        "SF_LOGIN_URL, plus ONE of SF_CLIENT_SECRET (Consumer Secret — "
        "simplest, and SF_LOGIN_URL must then be your My Domain URL), "
        "SF_PRIVATE_KEY_FILE, or SF_PRIVATE_KEY_B64"
    )
