"""The two containers share one LanceDB directory; they must share one library.

/data/lancedb is written by the sync-worker and read by the orchestrator;
/data/lancedb-web is written and read by the orchestrator. A Lance file
written by one lancedb/pyarrow version and read by another is a format
question nobody wants to answer during an incident, so both requirement
files pin the same EXACT versions and this test is what keeps them equal.
On 2026-09-03 the ranged specs had drifted to pyarrow 25.0.1 (orchestrator)
versus 21.0.0 (sync-worker) without anyone noticing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_FILES = {
    "orchestrator": _ROOT / "orchestrator" / "requirements.txt",
    "sync-worker": _ROOT / "sync-worker" / "requirements.txt",
}
_SHARED = ("lancedb", "pyarrow")

_REQ = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[[^\]]*\])?\s*(===|==|>=|<=|~=|!=|<|>)\s*([^\s;,#]+)"
)
_RELEASE = re.compile(r"^\d+(\.\d+)+$")


def _pins(path: Path) -> dict:
    """{normalised name: (operator, version, raw line)} for every requirement."""
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQ.match(line)
        if not match:
            continue
        name = match.group(1).lower().replace("_", "-")
        out[name] = (match.group(3), match.group(4), line)
    return out


@pytest.mark.parametrize("name", _SHARED)
def test_shared_lancedb_library_is_pinned_exactly_in_both_files(name):
    for service, path in _FILES.items():
        pins = _pins(path)
        assert name in pins, f"{service}: {name} is missing from {path}"
        op, version, line = pins[name]
        assert op == "==", f"{service}: {name} must be an exact pin, got {line!r}"
        assert _RELEASE.match(version), f"{service}: {name} pin {version!r} is not a release version"


@pytest.mark.parametrize("name", _SHARED)
def test_shared_lancedb_library_versions_match_across_containers(name):
    versions = {service: _pins(path)[name][1] for service, path in _FILES.items()}
    assert len(set(versions.values())) == 1, (
        f"{name} differs between the containers that share /data/lancedb: {versions}"
    )
