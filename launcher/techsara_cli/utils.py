"""Small, security-sensitive launcher utilities using only the standard library."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import TechSaraError

GIB = 1024**3
MIB = 1024**2

_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_EXPORT_PREFIX = re.compile(r"^export\s+")
_ENV_INLINE_COMMENT = re.compile(r"\s#")
# What a writer may emit unquoted. Deliberately conservative: quoting is free,
# and a value that escapes this set becomes shell code when someone sources it.
_ENV_BARE_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+,=-]+$")
# What a shell actually reads back as itself. Wider than the writer's set by a
# `#`, which only starts a comment at the start of a word, so `value#tight`
# survives a `.` unharmed and must not be reported as a problem.
_ENV_SHELL_SAFE_VALUE = re.compile(r"^[A-Za-z0-9_./:@%+,=#-]+$")
_ENV_ESCAPES = {
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "f": "\f",
    "v": "\v",
    "b": "\b",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "$": "$",
}
_PROFILE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def run_command(
    args: Sequence[str],
    *,
    timeout: float = 15.0,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a fixed argv without a shell and with bounded output/time."""
    try:
        return subprocess.run(
            [str(a) for a in args],
            cwd=str(cwd) if cwd else None,
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=check,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        if check:
            raise TechSaraError(f"command failed: {args[0]} ({type(exc).__name__})") from exc
        return subprocess.CompletedProcess(list(args), 127, "", f"{type(exc).__name__}: {exc}")


def atomic_write_text(path: Path, value: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_json(path: Path, value: Any, mode: int = 0o644) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n", mode)


def load_json(path: Path, default: Any = None) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def load_yaml_json(path: Path) -> Any:
    """Load JSON-compatible YAML without adding a bootstrap dependency.

    JSON is a strict subset of YAML 1.2. The repository's declarative `.yaml`
    manifests intentionally use that subset so the launcher stays standalone.
    """
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _env_value(raw: str) -> str:
    """Resolve one dotenv right-hand side the way Compose's loader does.

    Single quotes are literal, double quotes honour backslash escapes, and an
    unquoted value ends at the first whitespace-preceded `#`. Text after a
    closing quote is a comment. This mirrors compose-go, which is the parser
    that actually decides what the containers receive.
    """
    value = raw.lstrip()
    if not value:
        return ""
    quote = value[0]
    if quote in {"'", '"'}:
        out: list[str] = []
        index = 1
        while index < len(value):
            char = value[index]
            if quote == '"' and char == "\\" and index + 1 < len(value):
                nxt = value[index + 1]
                out.append(_ENV_ESCAPES.get(nxt, "\\" + nxt))
                index += 2
                continue
            if char == quote:
                return "".join(out)
            out.append(char)
            index += 1
        # Unterminated quote. Compose rejects the whole file; keep the literal
        # text so callers still see something and let check_env_file() flag it.
        return value
    comment = _ENV_INLINE_COMMENT.search(value)
    if comment is not None:
        value = value[: comment.start()]
    return value.rstrip()


def parse_env_file(path: Path) -> dict[str, str]:
    """Parse dotenv assignments the way Compose does, without a shell.

    Deliberately NOT `sh -c '. file'`: a dotenv file is data, and sourcing it
    makes a value containing a space, a parenthesis or a quote into shell code.
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return values
    if text.startswith("\ufeff"):  # UTF-8 BOM, else it glues itself to the first key
        text = text[1:]
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = _ENV_EXPORT_PREFIX.sub("", key.strip(), count=1)
        if not _ENV_KEY.fullmatch(key):
            continue
        values[key] = _env_value(value)
    return values


def quote_env_value(value: str) -> str:
    """Render a value so both Compose and a shell read back exactly `value`.

    Single quotes are preferred: inside them nothing is expanded or escaped.
    A value that itself contains a single quote falls back to double quotes,
    where the expanding characters have to be escaped.
    """
    if _ENV_BARE_VALUE.fullmatch(value):
        return value
    if "'" not in value:
        return f"'{value}'"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def render_env(values: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for key in sorted(values):
        if not _ENV_KEY.fullmatch(key):
            raise ValueError(f"invalid environment key: {key!r}")
        value = str(values[key])
        if any(ch in value for ch in "\r\n\x00"):
            raise ValueError(f"invalid newline in environment value for {key}")
        lines.append(f"{key}={quote_env_value(value)}")
    return "\n".join(lines) + "\n"


def check_env_file(path: Path) -> list[tuple[int, str, str]]:
    """Report lines a POSIX shell would mis-parse, as (line number, key, reason).

    Values are never returned or logged — only the key and the shape of the
    problem — because these files hold credentials.
    """
    problems: list[tuple[int, str, str]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return problems
    if text.startswith("\ufeff"):
        problems.append((1, "<file>", "starts with a UTF-8 BOM, which glues itself to the first key"))
        text = text[1:]
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = _ENV_EXPORT_PREFIX.sub("", key.strip(), count=1)
        if not _ENV_KEY.fullmatch(key):
            problems.append((number, "<malformed>", "left-hand side is not a valid environment key"))
            continue
        value = value.strip()
        if not value:
            continue
        quote = value[0]
        if quote in {"'", '"'}:
            if _env_value(value) == value:
                problems.append((number, key, f"unterminated {quote} quote; Compose refuses the whole file"))
            continue
        if _ENV_SHELL_SAFE_VALUE.fullmatch(value):
            continue
        reasons = []
        if any(ch in value for ch in " \t"):
            reasons.append("whitespace")
        if any(ch in value for ch in "()"):
            reasons.append("parentheses (a shell syntax error)")
        if any(ch in value for ch in "\"'"):
            reasons.append("quote characters (a shell would strip them)")
        if any(ch in value for ch in "$`"):
            reasons.append("expansion characters")
        if any(ch in value for ch in "*?[]{}~!|&;<>\\"):
            reasons.append("glob or control characters")
        problems.append(
            (number, key, "unquoted value contains " + ", ".join(reasons or ["shell-significant characters"]))
        )
    return problems


def validate_profile_name(value: str) -> str:
    if not _PROFILE.fullmatch(value):
        raise ValueError(f"invalid profile name: {value!r}")
    return value


def validate_model_id(value: str) -> str:
    if not _MODEL_ID.fullmatch(value):
        raise ValueError(f"invalid Hugging Face model id: {value!r}")
    return value


def validate_revision(value: str) -> str:
    if not _REVISION.fullmatch(value):
        raise ValueError(f"model revision must be a full 40-character commit: {value!r}")
    return value


def slug_model(model_id: str) -> str:
    validate_model_id(model_id)
    return model_id.replace("/", "--")


def secure_token(bytes_count: int = 32) -> str:
    return secrets.token_hex(bytes_count)


def redact(text: str, secret_values: Iterable[str]) -> str:
    safe = text
    for value in secret_values:
        if value and len(value) >= 4:
            safe = safe.replace(value, "[REDACTED]")
    return safe


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_with_resume(url: str, destination: Path, *, timeout: float = 30.0) -> Path:
    """Resume an HTTP download into `destination.part`, then atomically publish."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    request = urllib.request.Request(url, headers={"User-Agent": "TechSara-bootstrap/1"})
    if offset:
        request.add_header("Range", f"bytes={offset}-")
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and partial.exists():
            os.replace(partial, destination)
            return destination
        raise TechSaraError(f"download failed with HTTP {exc.code}: {url}") from exc
    status = getattr(response, "status", 200)
    mode = "ab" if offset and status == 206 else "wb"
    with response, partial.open(mode) as handle:
        while True:
            chunk = response.read(MIB)
            if not chunk:
                break
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(partial, destination)
    return destination


def verified_download(url: str, destination: Path, sha256: str) -> Path:
    if destination.exists() and sha256_file(destination) == sha256:
        return destination
    download_with_resume(url, destination)
    actual = sha256_file(destination)
    if actual != sha256:
        destination.unlink(missing_ok=True)
        raise TechSaraError(f"checksum mismatch for {destination.name}")
    return destination


def safe_extract_tar(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            target = (destination / member.name).resolve()
            if root != target and root not in target.parents:
                raise TechSaraError(f"unsafe archive member: {member.name}")
            if member.issym() or member.islnk() or member.isdev():
                raise TechSaraError(f"unsupported archive member: {member.name}")
        tar.extractall(destination, filter="data")


class FileLock(AbstractContextManager["FileLock"]):
    """Cross-platform O_EXCL lock with conservative stale-lock recovery."""

    def __init__(self, path: Path, *, timeout: float = 30.0, stale_after: float = 6 * 3600) -> None:
        self.path = path
        self.timeout = timeout
        self.stale_after = stale_after
        self._owned = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"pid": os.getpid(), "created": time.time()}, handle)
                self._owned = True
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > self.stale_after:
                    owner_alive = False
                    try:
                        payload = json.loads(self.path.read_text(encoding="utf-8"))
                        pid = payload.get("pid")
                        if type(pid) is int and pid > 0:
                            try:
                                os.kill(pid, 0)
                                owner_alive = True
                            except PermissionError:
                                owner_alive = True
                            except (ProcessLookupError, OSError):
                                owner_alive = False
                    except (OSError, json.JSONDecodeError, AttributeError):
                        owner_alive = False
                    if not owner_alive:
                        self.path.unlink(missing_ok=True)
                        continue
                if time.monotonic() >= deadline:
                    raise TechSaraError(f"another TechSara operation holds {self.path.name}")
                time.sleep(0.2)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._owned:
            self.path.unlink(missing_ok=True)
            self._owned = False


def disk_free(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free
