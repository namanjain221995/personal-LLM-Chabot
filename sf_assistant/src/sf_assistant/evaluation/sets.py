"""Evaluation sets: ``sets.yaml`` (paths only), the question files and expected answers in the private folder.

* :func:`load_sets` reads ``sets.yaml`` (next to this module, or another file) with :func:`parse_yaml`, a reader for
  the small YAML subset the file uses (block mappings, flow lists ``[a, b]`` and maps ``{a: 1}``, plain and quoted
  scalars, comments). The package pins no YAML library, and the file needs nothing more.
* :func:`load_items` reads one set: every question of its question file as an :class:`EvalItem` with its id, its tag
  line and its expected entry (matched by id or by exact question text).
* :func:`plan` picks the (item, mode) pairs of a run from ``--set``, ``--mode`` and ``--level``.

The question files and expected files hold real names: they are read from ``private_dir`` (default
:data:`sf_assistant.config.PRIVATE_DIR`) and never copied into tracked files. A path under ``questions/`` is read
from ``questions_dir`` instead (default :data:`sf_assistant.config.QUESTIONS_DIR`, ``SFA_QUESTIONS_DIR``, which is
``.private/questions`` unless set; with another ``private_dir`` given, its ``questions/`` folder).
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sf_assistant import config

SETS_FILE = Path(__file__).with_name("sets.yaml")
MODES = ("merged", "records")
LEVELS = ("quick", "standard", "full")
GRADERS = ("lookup", "golden", "people", "control", "route")

__all__ = ["SETS_FILE", "MODES", "LEVELS", "GRADERS", "SetSpec", "SetsConfig", "EvalItem", "parse_yaml", "load_sets",
           "load_items", "read_question_file", "sample_indices", "plan"]


# ------------------------------------------------------------------------------------------------- YAML subset
class YamlError(ValueError):
    """``sets.yaml`` is not in the supported subset (with the line number)."""


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _split_flow(text: str) -> list[str]:
    parts, depth, quote, cur = [], 0, None, []
    for ch in text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def _scalar(text: str, lineno: int) -> Any:
    t = text.strip()
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(p, lineno) for p in _split_flow(t[1:-1])]
    if t.startswith("{") and t.endswith("}"):
        out = {}
        for p in _split_flow(t[1:-1]):
            k, sep, v = p.partition(":")
            if not sep:
                raise YamlError(f"line {lineno}: {p!r} in a flow map has no ':'")
            out[str(_scalar(k, lineno))] = _scalar(v, lineno)
        return out
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        return t[1:-1]
    if t in ("", "~", "null"):
        return None
    if t in ("true", "false"):
        return t == "true"
    if re.fullmatch(r"[-+]?\d+", t):
        return int(t)
    if re.fullmatch(r"[-+]?\d+\.\d+", t):
        return float(t)
    return t


def parse_yaml(text: str) -> Any:
    """Parse the YAML subset of ``sets.yaml``: nested block mappings (two or more spaces per level), block lists
    (``- scalar``), flow lists and maps, plain / quoted scalars (int, float, true / false, null) and ``#`` comments."""
    lines = []
    for n, raw in enumerate(text.splitlines(), 1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise YamlError(f"line {n}: tabs in the indentation")
        s = _strip_comment(raw)
        if s.strip():
            lines.append((n, len(s) - len(s.lstrip(" ")), s.strip()))

    def block(i: int, indent: int) -> tuple[Any, int]:
        if i >= len(lines):
            return None, i
        if lines[i][2].startswith("- ") or lines[i][2] == "-":
            out_list = []
            while i < len(lines) and lines[i][1] == indent and (lines[i][2].startswith("- ") or lines[i][2] == "-"):
                out_list.append(_scalar(lines[i][2][1:], lines[i][0]))
                i += 1
            return out_list, i
        out: dict[str, Any] = {}
        while i < len(lines) and lines[i][1] == indent:
            n, _, s = lines[i]
            key, sep, rest = s.partition(":")
            if not sep or (rest and not rest.startswith(" ")):
                raise YamlError(f"line {n}: expected 'key: value', got {s!r}")
            key = str(_scalar(key, n))
            if key in out:
                raise YamlError(f"line {n}: duplicate key {key!r}")
            i += 1
            if rest.strip():
                out[key] = _scalar(rest, n)
            elif i < len(lines) and lines[i][1] > indent:
                out[key], i = block(i, lines[i][1])
            else:
                out[key] = None
        if i < len(lines) and lines[i][1] > indent:
            raise YamlError(f"line {lines[i][0]}: unexpected indentation")
        return out, i

    if not lines:
        return {}
    value, i = block(0, lines[0][1])
    if i != len(lines):
        raise YamlError(f"line {lines[i][0]}: unexpected indentation")
    return value


# ------------------------------------------------------------------------------------------------- the sets file
@dataclass(frozen=True)
class SetSpec:
    """One set of ``sets.yaml``."""

    name: str
    questions: str
    grader: str
    id_prefix: str
    modes: tuple[str, ...]
    full: tuple[str, ...]
    quick: int
    expected: str | None = None


@dataclass(frozen=True)
class SetsConfig:
    """``sets.yaml``: the sets, the levels, ``today`` and the folders."""

    sets: dict[str, SetSpec]
    order: tuple[str, ...]
    levels: dict[str, dict]
    today: str | None
    results_dir: str
    private_dir: Path
    home: Path
    source: Path
    questions_dir: Path | None = None

    def path(self, rel: str) -> Path:
        """A set file: ``rel`` under the private folder; ``questions/<file>`` under :attr:`questions_dir`."""
        parts = Path(rel).parts
        if self.questions_dir is not None and parts and parts[0] == "questions":
            return self.questions_dir.joinpath(*parts[1:])
        return self.private_dir / rel

    def results_path(self) -> Path:
        p = Path(self.results_dir)
        return p if p.is_absolute() else self.home / p


def _tuple(value: Any, what: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"sets.yaml: {what} must be a list of names")
    return tuple(value)


def load_sets(path: str | os.PathLike | None = None, private_dir: str | os.PathLike | None = None,
              home: str | os.PathLike | None = None, questions_dir: str | os.PathLike | None = None) -> SetsConfig:
    """Read ``sets.yaml`` (default: the one next to this module). ``private_dir`` / ``home`` default to
    :data:`sf_assistant.config.PRIVATE_DIR` / :data:`sf_assistant.config.HOME`; ``questions_dir`` to
    :data:`sf_assistant.config.QUESTIONS_DIR`, or ``<private_dir>/questions`` when ``private_dir`` is given."""
    src = Path(path) if path else SETS_FILE
    data = parse_yaml(src.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("sets"), dict):
        raise ValueError(f"{src}: no 'sets' mapping")
    sets: dict[str, SetSpec] = {}
    for name, d in data["sets"].items():
        if not isinstance(d, dict) or not d.get("questions") or d.get("grader") not in GRADERS:
            raise ValueError(f"{src}: set {name!r} needs 'questions' and a grader in {GRADERS}")
        modes = _tuple(d.get("modes") or ["merged"], f"{name}.modes")
        full = _tuple(d.get("full") or list(modes), f"{name}.full")
        bad = [m for m in modes + full if m not in MODES] + [m for m in full if m not in modes]
        if bad:
            raise ValueError(f"{src}: set {name!r}: unknown or unsupported modes {bad}")
        for rel in (d["questions"], d.get("expected")):
            if rel is not None and (os.path.isabs(str(rel)) or ".." in Path(str(rel)).parts):
                raise ValueError(f"{src}: set {name!r}: {rel!r} must be a path inside the private folder")
        sets[name] = SetSpec(name=name, questions=str(d["questions"]), grader=d["grader"],
                             id_prefix=str(d.get("id_prefix") or name[:1].upper()), modes=modes, full=full,
                             quick=int(d.get("quick") or 0), expected=d.get("expected"))
    order = _tuple(data.get("order") or list(sets), "order")
    if set(order) != set(sets):
        raise ValueError(f"{src}: 'order' must list every set once")
    levels = data.get("levels") or {}
    if set(levels) != set(LEVELS):
        raise ValueError(f"{src}: 'levels' must define {LEVELS}")
    today = data.get("today")
    return SetsConfig(sets=sets, order=order, levels=levels, today=None if today is None else str(today),
                      results_dir=str(data.get("results_dir") or ".private/runs/eval"),
                      private_dir=Path(private_dir) if private_dir else config.PRIVATE_DIR,
                      home=Path(home) if home else config.HOME, source=src,
                      questions_dir=Path(questions_dir) if questions_dir else (
                          Path(private_dir) / "questions" if private_dir else config.QUESTIONS_DIR))


# ------------------------------------------------------------------------------------------------- questions
@dataclass
class EvalItem:
    """One question of a set: ``id`` (P01, Q07, G03 ...), its 1-based position ``n`` in the file, the text, the tag line
    before it (``tag``, '' when there is none) and its expected entry (None when the set has no expected file or the
    question has no entry)."""

    set: str
    id: str
    n: int
    question: str
    grader: str
    tag: str = ""
    expected: dict | None = None
    meta: dict = field(default_factory=dict)


_TAG_RE = re.compile(r"^#\s*\[([A-Za-z]*)(\d+)\b")


def read_question_file(path: str | os.PathLike) -> list[tuple[str, str]]:
    """``(question, tag line)`` for every question line (non-empty, not '#'); the tag is the comment line right before
    it ('' when the line before is no comment)."""
    out: list[tuple[str, str]] = []
    prev = ""
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                prev = ""
                continue
            if line.startswith("#"):
                prev = line
                continue
            out.append((line, prev))
            prev = ""
    return out


def _expected_entries(spec: SetSpec, data: Any) -> tuple[dict[str, dict], dict[str, dict]]:
    """(by id, by question text) of an expected file."""
    if spec.grader in ("people", "control") and isinstance(data, dict):
        entries = list(data.get("questions") or []) + list(data.get("controls") or [])
        return ({str(e["id"]): e for e in entries if e.get("id")}, {e["q"]: e for e in entries if e.get("q")})
    if isinstance(data, list):
        key = "question" if spec.grader == "golden" else "q"
        by_text = {e[key]: e for e in data if isinstance(e, dict) and e.get(key)}
        by_id = {f"{spec.id_prefix}{int(e['n']):02d}": e for e in data if isinstance(e, dict) and e.get("n") is not None}
        return by_id, by_text
    return {}, {}


def load_items(spec: SetSpec, cfg: SetsConfig) -> list[EvalItem]:
    """Every question of ``spec`` with its expected entry. Raises FileNotFoundError when a set file is missing."""
    questions = read_question_file(cfg.path(spec.questions))
    by_id: dict[str, dict] = {}
    by_text: dict[str, dict] = {}
    meta_all: dict = {}
    if spec.expected:
        with open(cfg.path(spec.expected), encoding="utf-8") as fh:
            data = json.load(fh)
        by_id, by_text = _expected_entries(spec, data)
        if isinstance(data, dict) and data.get("today"):
            meta_all["today"] = data["today"]
    items: list[EvalItem] = []
    seen: set[str] = set()
    for n, (text, tag) in enumerate(questions, 1):
        m = _TAG_RE.match(tag)
        if m and m.group(1):
            qid = m.group(1).upper() + m.group(2)
        elif m:
            qid = f"{spec.id_prefix}{int(m.group(2)):02d}"
        else:
            qid = f"{spec.id_prefix}{n:02d}"
        if qid in seen:
            raise ValueError(f"{spec.questions}: question id {qid} twice")
        seen.add(qid)
        # people / controls carry their id in the tag line; the lookup and golden files are matched by exact text
        first, second = (by_id.get(qid), by_text.get(text)) if spec.grader in ("people", "control") else \
            (by_text.get(text), by_id.get(qid))
        entry = first if first is not None else second
        items.append(EvalItem(set=spec.name, id=qid, n=n, question=text, grader=spec.grader, tag=tag, expected=entry,
                              meta=dict(meta_all)))
    return items


def sample_indices(total: int, k: int) -> list[int]:
    """``k`` evenly spaced 0-based indices of ``total`` items (all of them when ``k >= total``): a fixed sample."""
    if k <= 0:
        return []
    if k >= total:
        return list(range(total))
    return [i * total // k for i in range(k)]


def plan(cfg: SetsConfig, set_name: str = "all", mode: str | None = None, level: str = "standard"
         ) -> list[tuple[EvalItem, str]]:
    """The (item, mode) pairs of a run, set by set in ``cfg.order`` and, within a set, mode by mode.

    ``level`` picks the questions and the default modes (``sets.yaml`` ``levels``); ``mode`` (merged / records / both)
    replaces the level's modes, limited to the modes each set supports (a set without the mode is left out)."""
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r} (one of {', '.join(LEVELS)})")
    if mode not in (None, "merged", "records", "both"):
        raise ValueError(f"unknown mode {mode!r} (merged, records or both)")
    names = list(cfg.order) if set_name == "all" else [set_name]
    unknown = [s for s in names if s not in cfg.sets]
    if unknown:
        raise ValueError(f"unknown set {unknown[0]!r} (one of {', '.join(cfg.order)}, all)")
    lv = cfg.levels[level]
    out: list[tuple[EvalItem, str]] = []
    for name in names:
        spec = cfg.sets[name]
        if mode is None:
            modes = spec.full if lv.get("modes") == "full" else tuple(m for m in _tuple(lv.get("modes"), "levels")
                                                                   if m in spec.modes)
        else:
            want = MODES if mode == "both" else (mode,)
            modes = tuple(m for m in want if m in spec.modes)
        items = load_items(spec, cfg)
        if lv.get("sample"):
            items = [items[i] for i in sample_indices(len(items), spec.quick)]
        for m in modes:
            out.extend((it, m) for it in items)
    return out
