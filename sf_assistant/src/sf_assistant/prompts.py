"""The prompt files ``prompts/<id>.md``: load, check and cache them (stdlib and :mod:`sf_assistant.config` only).

Every text the models get from a prompt constant lives in one file under :data:`PROMPTS_DIR` (``SFA_PROMPTS_DIR``,
default ``<HOME>/prompts``). A file is a front matter block and the exact text::

    ---
    id: records_other_lookup_message           # = the file stem
    version: 1                                 # integer; raise it when the text changes
    name: RECORDS_OTHER_LOOKUP_MESSAGE         # the Python name the text is bound to
    module: sf_assistant.agent.record_prompts  # the module that binds it
    source: "notebooks/object_knowledge_eval/okrec/prompts.py RECORDS_OTHER_LOOKUP_MESSAGE"
    placeholders: "<<FIELD>> <<FITS>> <<OBJECT>> <<OTHERS>> <<ROLE>>"
    ---
    Your count used <<FIELD>> (<<ROLE>>) and found 0, ...

The text is everything after the closing ``---`` line, minus exactly one final newline (so leading blank lines and
spaces are kept). Values are plain words or JSON strings (a JSON string is a valid YAML scalar, so markdown viewers
show the header as front matter). ``placeholders`` lists every ``{name}`` and ``<<NAME>>`` token of the text; the
loader refuses a file whose tokens differ, whose id is not its file name, whose version is not an integer, or that
holds a carriage return or a byte order mark.

The modules that use the texts bind them once, at import, to the same Python names as before
(``agent/knowledge_prompts.py``, ``agent/record_prompts.py``, ``agent/knowledge_agent.py``), so call sites do not
change. :func:`load` reads each file once per process.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from sf_assistant import config

__all__ = ["PROMPTS_DIR", "Prompt", "PromptError", "parse", "load", "text", "ids", "all_prompts", "versions",
           "fill", "TOKEN_RE"]

PROMPTS_DIR: Path = config.PROMPTS_DIR

#: ``{name}`` (filled by ``knowledge_prompts.render`` and ``combine_user_message``) or ``<<NAME>>`` (``str.replace``).
TOKEN_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}|<<[A-Z][A-Z0-9_]*>>")
_ANGLE_RE = re.compile(r"<<([A-Z][A-Z0-9_]*)>>")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_KEYS = ("id", "version", "name", "module", "source", "placeholders", "fill")
_REQUIRED = ("id", "version")


class PromptError(ValueError):
    """A prompt file is missing or malformed (the message names the file)."""


@dataclass(frozen=True)
class Prompt:
    """One prompt file: its id, version, exact text, placeholder tokens and the other header values."""

    id: str
    version: int
    text: str
    placeholders: tuple[str, ...] = ()
    meta: dict = field(default_factory=dict, compare=False)   # name, module, source, fill (as written)


def _value(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith('"'):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"bad quoted value {raw[:40]!r}: {exc}") from None
        if not isinstance(value, str):
            raise ValueError(f"quoted value is not a string: {raw[:40]!r}")
        return value
    return raw


def parse(data: str, stem: str) -> Prompt:
    """Parse the content of ``<stem>.md``. Raises :class:`PromptError`."""
    where = f"prompts/{stem}.md"
    if data.startswith("﻿"):
        raise PromptError(f"{where}: starts with a byte order mark")
    if "\r" in data:
        raise PromptError(f"{where}: holds a carriage return (LF line ends only)")
    if not data.startswith("---\n"):
        raise PromptError(f"{where}: must start with a '---' front matter line")
    end = data.find("\n---\n", 3)
    if end < 0:
        raise PromptError(f"{where}: no closing '---' line after the front matter")
    head, body = data[4:end], data[end + len("\n---\n"):]
    if not body.endswith("\n"):
        raise PromptError(f"{where}: must end with one newline after the text")
    body = body[:-1]
    meta: dict[str, str] = {}
    for line in head.split("\n"):
        key, sep, raw = line.partition(":")
        key = key.strip()
        if not sep or key not in _KEYS:
            raise PromptError(f"{where}: unknown front matter line {line[:60]!r} (keys: {', '.join(_KEYS)})")
        if key in meta:
            raise PromptError(f"{where}: '{key}' given twice")
        try:
            meta[key] = _value(raw)
        except ValueError as exc:
            raise PromptError(f"{where}: {key}: {exc}") from None
    for key in _REQUIRED:
        if not meta.get(key):
            raise PromptError(f"{where}: front matter needs '{key}'")
    if meta["id"] != stem or not _ID_RE.match(stem):
        raise PromptError(f"{where}: id '{meta['id']}' must equal the file name (lower case, digits, '_')")
    if not re.fullmatch(r"[1-9][0-9]*", meta["version"]):
        raise PromptError(f"{where}: version must be a positive integer, not {meta['version']!r}")
    declared = tuple(meta.get("placeholders", "").split())
    found = sorted(set(TOKEN_RE.findall(body)))
    if sorted(set(declared)) != found or len(set(declared)) != len(declared):
        raise PromptError(f"{where}: placeholders {list(declared)} do not match the text's tokens {found}")
    rest = {k: v for k, v in meta.items() if k not in ("id", "version", "placeholders")}
    return Prompt(id=stem, version=int(meta["version"]), text=body, placeholders=declared, meta=rest)


_cache: dict[str, Prompt] = {}
_lock = threading.Lock()


def load(prompt_id: str) -> Prompt:
    """The prompt ``prompt_id`` (read from :data:`PROMPTS_DIR` on first use, then cached)."""
    with _lock:
        hit = _cache.get(prompt_id)
        if hit is not None:
            return hit
        if not _ID_RE.match(str(prompt_id)):
            raise PromptError(f"bad prompt id {prompt_id!r}")
        path = PROMPTS_DIR / f"{prompt_id}.md"
        try:
            data = path.read_bytes().decode("utf-8")
        except FileNotFoundError:
            if not PROMPTS_DIR.is_dir():   # e.g. a plain (non-editable) install: HOME is not the sf_assistant folder
                raise PromptError(
                    f"prompts/{prompt_id}.md not found: the prompt folder {PROMPTS_DIR} does not exist. The prompt "
                    "files, the knowledge files and .private/ stay in the sf_assistant folder, outside the installed "
                    "package: install it editable (pip install -e) or set SFA_HOME to the sf_assistant folder "
                    "(SFA_PROMPTS_DIR for the prompts alone)") from None
            raise PromptError(f"prompts/{prompt_id}.md not found in {PROMPTS_DIR}") from None
        except UnicodeDecodeError as exc:
            raise PromptError(f"prompts/{prompt_id}.md is not UTF-8: {exc}") from None
        prompt = parse(data, prompt_id)
        _cache[prompt_id] = prompt
        return prompt


def text(prompt_id: str) -> str:
    """The exact text of prompt ``prompt_id``."""
    return load(prompt_id).text


def ids() -> list[str]:
    """Every prompt id in :data:`PROMPTS_DIR`, sorted."""
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))


def all_prompts() -> dict[str, Prompt]:
    """Every prompt file, by id (sorted)."""
    return {pid: load(pid) for pid in ids()}


def versions() -> dict[str, int]:
    """``{id: version}`` of every prompt file (for answer metadata)."""
    return {pid: p.version for pid, p in all_prompts().items()}


def fill(template: str, **values: str) -> str:
    """Replace every ``<<NAME>>`` token of ``template`` with ``values[NAME]`` in one pass (a value is never filled
    again). Raises KeyError when a token has no value."""
    missing = sorted({m for m in _ANGLE_RE.findall(template) if m not in values})
    if missing:
        raise KeyError(f"no value for {', '.join('<<' + m + '>>' for m in missing)}")
    return _ANGLE_RE.sub(lambda m: str(values[m.group(1)]), template)
