"""Helpers shared by the voice archive store's tests (no fixtures here)."""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Dict, Optional

HERE = Path(__file__).resolve().parent
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

import server  # noqa: E402,F401

#: Deliberately low-entropy: secret scanners flag random-looking literals,
#: and this one opens nothing.
TOKEN = "voice-store-test-token-" + "a" * 40
OTHER_TOKEN = "voice-store-test-token-" + "b" * 40
AUTH = {"authorization": f"Bearer {TOKEN}"}
UID = "7"
SID = "0123456789abcdef0123456789abcdef"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def put_headers(data: bytes, **extra: str) -> Dict[str, str]:
    return {**AUTH, "x-content-sha256": sha(data), "x-recording-type": "audio/webm", **extra}


def object_url(uid: str = UID, sid: str = SID, name: str = "source.webm") -> str:
    return f"/v1/recordings/{uid}/{sid}/{name}"


def put(client, data: bytes, *, uid: str = UID, sid: str = SID, name: str = "source.webm",
        headers: Optional[Dict[str, str]] = None):
    return client.put(object_url(uid, sid, name), content=data, headers=headers or put_headers(data))


def files_under(root: Path) -> list:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def leftovers(root: Path) -> list:
    return [p for p in files_under(root) if os.path.basename(p).startswith((".incoming-", ".manifest-"))]
