"""Check the rendered files against what was asked, repair once, say what is unmet.

WHERE IT RUNS. pipeline._run_stages, after render -> validate -> preview ->
visual QA and before PUBLISH, on the final candidate files. Skipped for a
restore (a byte copy of a version that was checked when it was made) and
while a stage deferred. It never fails a job: every exception inside is
caught, reported as outcome=error, and the job publishes what it has.

THE LOOP (bounded, one repair):

  1. checklist   requirements.build — the rule extractor plus, at Think/Max
                 and when chat is idle, one proposer call that never sees
                 the spec or the plan.
  2. observe     inspect_files.inspect over every file the validate stage
                 listed — the bytes the person downloads (asyncio.to_thread).
  3. evaluate    per item: pass | fail | unverifiable | contested. A
                 property no file shows is UNVERIFIABLE, never a pass.
                 Faithfulness: >= 99 % of the source heading lines and 100 %
                 of its table cells in the file text. Preservation: the text
                 of every section/sheet/slide whose spec did not change is
                 equal to the parent version's FILE text. Chart values: the
                 numbers in the native chart equal the values recomputed
                 from the bound tables (chart_data.recompute_matches when
                 the charts track is present) or the spec's series.
  4. repair      only when a must-item failed, ARTIFACT_SELFCHECK_REPAIR is
                 on and the remaining budget covers a re-render (measured
                 from this job's own render/validate/preview times):
                   style/layout  -> edits.ops_for_style / ops_for_layout
                                    applied by code (0 model calls; allowed
                                    at Fast); orientation has a built-in
                                    fallback when the edits track is absent
                   format        -> the missing format added to the render
                   chart values  -> chart_data.resolve_spec (0 calls); no
                                    model ever repairs a number
                   content       -> Think/Max only: compose.revise on a
                                    create, compose.revise_section on an
                                    edit (never a whole-document rewrite of
                                    an edit)
                 The revision renders BESIDE the good files through
                 pipeline._try_revision, everything is re-inspected, and the
                 repair is ACCEPTED only if the set of failing must-items
                 strictly shrinks, no previously passing item fails, and on
                 an edit preservation still holds. Otherwise the pre-repair
                 files are restored byte for byte.
  5. report      selfcheck.json in the version dir (published, never a
                 download, never in the zip) and progress['selfcheck'];
                 unmet must-items and contested readings become the job's
                 warnings, so the version is completed_with_warnings and the
                 sentence names them ("I read 'blue headings' as all
                 headings").

BUDGETS. ARTIFACT_SELFCHECK_BUDGET_{FAST,THINK,MAX}_S (20/60/90 s) include
the re-render; model calls: Fast 0, Think/Max <= 1 checklist + <= 1 repair,
skipped when pipeline's busy probe says a person is waiting.

THE HEADLINE METRIC is the false-satisfied rate — a sentence claiming a
requirement the file does not meet — measured by scripts/
as3_selfcheck_score.py with a reader written separately from this module.
`false_claim_guard` lists what the answer may and may not claim.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from .. import metrics
from ..config import settings
from . import inspect_files as I
from . import requirements as RQ
from . import store
from . import types as T

log = logging.getLogger(__name__)

SELFCHECK_NAME = store.SELFCHECK_NAME
CHECK_STAGE = "check"
CHECK_TITLE = "Checking the files against the request"

RESULTS = ("pass", "fail", "unverifiable", "contested")

#: font requested -> metric-compatible / container substitutes a produced
#: file may carry instead (style guide §2). A substitute is a pass with a
#: note, and the note reaches the answer sentence.
FONT_SUBSTITUTES: Dict[str, Tuple[str, ...]] = {
    "calibri": ("carlito",),
    "cambria": ("caladea",),
    "arial": ("liberation sans", "arimo", "helvetica"),
    "helvetica": ("liberation sans", "arimo", "arial"),
    "times new roman": ("liberation serif", "tinos", "times"),
    "courier new": ("liberation mono", "cousine", "courier"),
    "georgia": ("gelasio",),
    "segoe ui": ("selawik", "open sans", "noto sans"),
}

#: (ctx: pipeline.ComposeContext, spec, issues) -> revised spec or None.
ContentRepairer = Callable[[Any, Any, List[dict]], Awaitable[Any]]
_content_repairer: Optional[ContentRepairer] = None


def set_content_repairer(fn: Optional[ContentRepairer]) -> None:
    global _content_repairer
    _content_repairer = fn


# ------------------------------------------------------------- results --


@dataclass
class ItemResult:
    item: RQ.ChecklistItem
    result: str
    evidence: List[str] = field(default_factory=list)
    by_format: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {**self.item.to_dict(), "result": self.result, "evidence": [e[:200] for e in self.evidence[:4]], "by_format": dict(self.by_format)}


@dataclass
class SelfcheckReport:
    items: List[dict] = field(default_factory=list)
    passed: int = 0
    failed: int = 0
    repaired: int = 0
    unverifiable: int = 0
    contested: int = 0
    unmet: List[str] = field(default_factory=list)
    false_claim_guard: Dict[str, List[str]] = field(default_factory=dict)
    model_calls: int = 0
    seconds: float = 0.0
    outcome: str = "clean"  # clean | repaired | unmet | contested | error | skipped_budget | skipped_busy
    repair: Dict[str, Any] = field(default_factory=dict)
    checklist: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "items": self.items, "passed": self.passed, "failed": self.failed, "repaired": self.repaired,
            "unverifiable": self.unverifiable, "contested": self.contested, "unmet": list(self.unmet),
            "false_claim_guard": dict(self.false_claim_guard), "model_calls": self.model_calls,
            "seconds": round(self.seconds, 2), "outcome": self.outcome, "repair": dict(self.repair),
            "checklist": {k: v for k, v in self.checklist.items() if k != "items"}, "notes": list(self.notes)[:10],
        }


# ----------------------------------------------------------- comparison --


def _hex_close(a: Any, b: Any, tol: int = 2) -> bool:
    ha, hb = I.norm_hex(a), I.norm_hex(b)
    if not ha or not hb:
        return False
    return all(abs(int(ha[i:i + 2], 16) - int(hb[i:i + 2], 16)) <= tol for i in (1, 3, 5))


def font_matches(observed: Any, expected: Any) -> Tuple[bool, str]:
    """(matches, note). Exact family, or a declared substitute (with note).

    A PDF or chart image is drawn HERE, so a proprietary family the server
    cannot have (Georgia, Calibri) is met by the family the renderers map it
    to (render.theme.resolve_font over style.FontFace.pdf_candidates): its
    metric twin always, and its documented fallback or the generic family
    only when that is what this server's mapping resolves to — never an
    arbitrary other font. Text in a script the requested Latin family cannot
    draw (Devanagari, Gujarati ...) is met by that script's installed font.
    The note says which font was used."""
    o = _font_key(observed)
    e = _font_key(expected)
    if not o or not e:
        return False, ""
    if e == o or e in o or o.replace(" ", "") == e.replace(" ", ""):
        return True, ""
    for sub in FONT_SUBSTITUTES.get(e, ()):
        if sub in o:
            return True, f"{expected} was set in its substitute {observed}"
    try:
        from . import style as ST
        from .render import theme

        face = ST.font_face(expected)
        if face is None:
            return False, ""
        if _font_key(face.name) == o or _font_key(face.office_name) == o:
            return True, ""
        if _font_key(face.metric_substitute) == o:
            kind = "metric-compatible equivalent" if face.metric_compatible else "substitute"
            return True, f"{face.name} was set in its {kind} {face.metric_substitute}"
        chosen = theme.resolve_font(face)
        if chosen.family and _font_key(chosen.family) == o and chosen.kind in ("fallback", "generic"):
            return True, f"{chosen.sentence()}, because neither {face.name} nor {face.metric_substitute} is installed on this server"
        for script, families in theme.SCRIPT_FONTS.items():
            # "Noto Serif Devanagari", "Lohit Gujarati": named for the script
            # they draw; the Latin requested family cannot draw that text.
            if script not in ("Arabic", "Hebrew", "Emoji") and script.casefold() in o or any(_font_key(f) == o for f in families if f not in ("DejaVu Sans",)):
                return True, f"{script} text was set in {observed}, which draws that script"
    except Exception:  # noqa: BLE001 - the check never fails on a font lookup
        log.debug("font equivalence lookup failed", exc_info=True)
    return False, ""


def _font_key(name: Any) -> str:
    return " ".join(I.norm_text(name).replace("-", " ").split())


def _hls(hx: str) -> Tuple[float, float, float]:
    import colorsys

    h = hx.lstrip("#")
    return colorsys.rgb_to_hls(int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


def colour_matches(observed: Any, expected: Any, locator: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    """A typed hex is held exactly (±2 per channel for PDF float rounding).
    A NAMED colour is held to its hue family and shade class: hue within
    25°, a chromatic observation for a chromatic name (an achromatic one
    for black/white/grey, lightness within 0.25), 'dark …' at lightness
    <= 0.40 and 'light …' at >= 0.70. The note names the hex the file has
    when it is not the palette value."""
    loc = locator or {}
    ho, he = I.norm_hex(observed), I.norm_hex(expected)
    if not ho or not he:
        return False, ""
    if not loc.get("color_name"):
        return _hex_close(ho, he), ""
    if _hex_close(ho, he):
        return True, ""
    h1, l1, s1 = _hls(ho)
    h2, l2, s2 = _hls(he)
    if s2 < 0.12 or l2 in (0.0, 1.0):
        ok = (s1 < 0.15 or l1 > 0.95 or l1 < 0.08) and abs(l1 - l2) <= 0.25
    else:
        diff = abs(h1 - h2) * 360.0
        diff = min(diff, 360.0 - diff)
        ok = s1 >= 0.12 and 0.05 < l1 < 0.97 and diff <= 25.0
    shade = loc.get("shade") or ""
    if ok and shade == "dark":
        ok = l1 <= 0.40
    elif ok and shade == "light":
        ok = l1 >= 0.70
    elif ok and s2 >= 0.12 and l2 not in (0.0, 1.0):
        # "blue headings" is not met by the house navy #0A1D37 (lightness
        # 0.13, reads as black) — verifier 2026-09-15.
        ok = 0.15 <= l1 <= 0.90
    return ok, (f"{loc.get('color_name')} is {ho} in the file" if ok else "")


def _value_matches(prop: str, observed: Any, expected: Any, locator: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
    if prop in ("color", "background", "series_color"):
        return colour_matches(observed, expected, locator)
    if prop == "font_family":
        return font_matches(observed, expected)
    if prop == "size_pt":
        try:
            return abs(float(observed) - float(expected)) <= 0.6, ""
        except (TypeError, ValueError):
            return False, ""
    if isinstance(expected, bool):
        return bool(observed) is expected, ""
    return I.norm_text(observed) == I.norm_text(expected), ""


def _target_matches(item_target: str, obs_target: str) -> bool:
    if item_target == obs_target:
        return True
    if item_target == "heading":
        return obs_target in ("heading1", "heading2", "heading3")
    if item_target.startswith("column:") and obs_target.startswith("column:"):
        a, b = item_target[7:], obs_target[7:]
        return a == b or (len(a) >= 3 and (a in b.split() or b.startswith(a)))
    if item_target.startswith("cell_range:") and obs_target.startswith("cell:"):
        return _in_range(obs_target[5:], item_target[len("cell_range:"):])
    return False


def _col_num(letters: str) -> int:
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def _in_range(coord: str, a1: str) -> bool:
    m = re.fullmatch(r"([A-Z]{1,3})(\d+)", coord.upper())
    r = re.fullmatch(r"([A-Z]{1,3})(\d+):([A-Z]{1,3})(\d+)", a1.upper())
    if not m or not r:
        return False
    c, row = _col_num(m.group(1)), int(m.group(2))
    c1, r1, c2, r2 = _col_num(r.group(1)), int(r.group(2)), _col_num(r.group(3)), int(r.group(4))
    return min(c1, c2) <= c <= max(c1, c2) and min(r1, r2) <= row <= max(r1, r2)


def _combine(by_format: Dict[str, str]) -> str:
    values = set(by_format.values())
    if "fail" in values:
        return "fail"
    if "pass" in values:
        return "pass"
    return "unverifiable"


def _obs(observations: Sequence[I.Observation], target: str, prop: str) -> List[I.Observation]:
    return [o for o in observations if o.target == target and o.property == prop]


def _formats_present(observations: Sequence[I.Observation]) -> List[str]:
    return sorted({o.format for o in observations if o.target == "file" and o.property == "format"})


# -------------------------------------------------------------- evaluate --


@dataclass
class EvalContext:
    spec: Any = None
    source_structure: Dict[str, List[str]] = field(default_factory=dict)
    parent_spec: Any = None
    parent_observations: Optional[List[I.Observation]] = None
    touched: Optional[Set[str]] = None
    expected_chart_values: Optional[List[dict]] = None
    chart_values_ok: Optional[Tuple[bool, List[str]]] = None
    instruction: str = ""


def _section_name_matches(wanted: str, actual: str) -> bool:
    """'Risks' names the heading 'Risks', '2. Risks', 'Key Risks and
    Mitigations' (its words, in order); 'Risk' also names 'Risks'."""
    w = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[ivxlc]+\.)\s+", "", I.norm_text(wanted))
    a = re.sub(r"^\s*(?:\d+(?:\.\d+)*\.?|[ivxlc]+\.)\s+", "", I.norm_text(actual))
    if not w or not a:
        return False
    return w == a or f" {w} " in f" {a} " or (len(w) >= 4 and any(tok.startswith(w) for tok in a.split()))


def _in_scope(item: RQ.ChecklistItem, o: I.Observation) -> Optional[bool]:
    """None when the observation cannot say which section it is in."""
    loc = item.locator
    if item.target == "paragraph" and loc.get("paragraphs_only") and o.locator.get("list"):
        return False
    if loc.get("section_index") is not None:
        if o.locator.get("section_index") is None:
            return None
        return int(o.locator["section_index"]) == int(loc["section_index"])
    if "sections" not in o.locator:
        return None
    return any(_section_name_matches(str(loc["section"]), s) for s in (o.locator.get("sections") or []))


def _eval_style(item: RQ.ChecklistItem, observations: Sequence[I.Observation]) -> ItemResult:
    res = ItemResult(item, "unverifiable")
    by_fmt: Dict[str, List[I.Observation]] = {}
    scoped = bool(item.locator.get("section") or item.locator.get("section_index") is not None)
    for o in observations:
        if o.property == item.property and _target_matches(item.target, o.target):
            by_fmt.setdefault(o.format, []).append(o)
    for fmt, found in list(by_fmt.items()):
        if scoped:
            # A style scoped to one section is judged on that section only
            # (never as the whole document's body text), and a file whose
            # reader cannot place text in sections says nothing about it.
            verdicts = [(o, _in_scope(item, o)) for o in found]
            if all(v is None for _, v in verdicts):
                res.by_format[fmt] = "unverifiable"
                continue
            found = [o for o, v in verdicts if v]
            if not found:
                res.by_format[fmt] = "unverifiable"
                where = item.locator.get("section") or f"section {item.locator.get('section_index')}"
                res.evidence.append(f"{fmt}: no {item.target.replace('paragraph', 'body text')} found under {where!r}")
                continue
        if item.locator.get("which") == "first":
            found = found[:1]
        verifiable = [o for o in found if o.verifiable and o.value is not None]
        if not verifiable:
            res.by_format[fmt] = "unverifiable"
            continue
        good = 0
        notes: List[str] = []
        for o in verifiable:
            ok, note = _value_matches(item.property, o.value, item.expected, item.locator)
            good += ok
            if note:
                notes.append(note)
        share = good / len(verifiable)
        # Body text is judged by proportion (a lede or a caption in its own
        # size is not a failure of "body 12pt"); named elements must all match.
        threshold = 0.9 if item.target in ("paragraph",) else 1.0
        res.by_format[fmt] = "pass" if share >= threshold else "fail"
        if res.by_format[fmt] == "fail":
            seen = sorted({str(o.value) for o in verifiable if not _value_matches(item.property, o.value, item.expected, item.locator)[0]})[:3]
            res.evidence.append(f"{fmt}: {len(verifiable) - good} of {len(verifiable)} show {', '.join(seen)}")
        # The note naming the font the file uses outranks a script note.
        res.evidence.extend(sorted(set(notes), key=lambda n: (" text was set in " in n, n))[:1])
    res.result = _combine(res.by_format)
    return res


def _eval_layout(item: RQ.ChecklistItem, observations: Sequence[I.Observation]) -> ItemResult:
    res = ItemResult(item, "unverifiable")
    prop = item.property
    if prop == "margins":
        for o in _obs(observations, "page", "margins_mm"):
            m = o.value or {}
            if item.expected == "narrow":
                ok = max(m.values()) <= 13.5
            else:
                ok = min(m.get("left", 0), m.get("right", 0)) >= 24.0
            res.by_format[o.format] = "fail" if res.by_format.get(o.format) == "fail" or not ok else "pass"
            if not ok:
                res.evidence.append(f"{o.format}: margins {m}")
    elif prop == "page_numbers":
        for o in _obs(observations, "page", "page_numbers"):
            if not o.verifiable or o.value is None:
                res.by_format.setdefault(o.format, "unverifiable")
                continue
            res.by_format[o.format] = "pass" if o.value else "fail"
            if not o.value:
                res.evidence.append(f"{o.format}: no page number field or text")
    else:
        for o in _obs(observations, "page", prop):
            ok = I.norm_text(o.value) == I.norm_text(item.expected)
            res.by_format[o.format] = "fail" if res.by_format.get(o.format) == "fail" or not ok else "pass"
            if not ok:
                res.evidence.append(f"{o.format}: {prop} {o.value}")
    res.result = _combine(res.by_format)
    return res


def _eval_format(item: RQ.ChecklistItem, observations: Sequence[I.Observation]) -> ItemResult:
    fmt = str(item.expected)
    unreadable = [o for o in observations if o.target == "file" and o.property == "readable" and o.format == fmt]
    present = fmt in _formats_present(observations)
    if unreadable:
        # The validate stage already reopened every file with the format's
        # own library; a file only THIS reader cannot parse is not evidence
        # that it was not delivered — it is evidence nothing could be checked.
        return ItemResult(item, "unverifiable", [f"the checker could not open the {fmt} file"], {fmt: "unverifiable"})
    return ItemResult(item, "pass" if present else "fail", [] if present else [f"no {fmt} file was produced"], {fmt: "pass" if present else "fail"})


_CHART_FAMILY = {"histogram": ("histogram", "bar"), "bar": ("bar",), "stacked_bar": ("stacked_bar",), "donut": ("donut",), "pie": ("pie",),
                 "combo": ("bar", "line", "combo")}


def _eval_chart(item: RQ.ChecklistItem, observations: Sequence[I.Observation], ectx: EvalContext) -> ItemResult:
    res = ItemResult(item, "unverifiable")
    prop = item.property
    if prop == "values_match":
        if ectx.chart_values_ok is not None:
            ok, diffs = ectx.chart_values_ok
            return ItemResult(item, "pass" if ok else "fail", list(diffs)[:3], {})
        return ItemResult(item, "unverifiable", ["no native chart values to compare"], {})
    if prop == "series_color":
        for o in observations:
            if o.target != "chart":
                continue
            if o.property == "series_colors" and o.verifiable:
                ok = any((colour_matches(c, item.expected, item.locator)[0] or _hex_close(c, item.expected, tol=6)) for c in (o.value or []) if c)
                res.by_format[o.format] = "pass" if ok or res.by_format.get(o.format) == "pass" else "fail"
                if not ok:
                    res.evidence.append(f"{o.format}: series colours {o.value}")
            elif o.property == "dominant_colors" and o.value:
                # Anti-aliased PNG pixels: a named hue family, or a typed hex
                # within 28 per channel.
                ok = any((colour_matches(c, item.expected, item.locator)[0] if item.locator.get("color_name") else _hex_close(c, item.expected, tol=28)) for c in o.value)
                res.by_format[o.format] = "pass" if ok or res.by_format.get(o.format) == "pass" else "fail"
                if not ok:
                    res.evidence.append(f"{o.format}: chart image colours {o.value[:4]}")
        res.result = _combine(res.by_format)
        return res
    family = _CHART_FAMILY.get(str(item.expected), (str(item.expected),))
    for o in _obs(observations, "chart", prop if prop != "type" else "type"):
        if not o.verifiable:
            continue
        if prop == "type":
            ok = str(o.value) in family
        elif prop == "title":
            ok = I.norm_text(item.expected) in I.norm_text(o.value)
        else:
            ok, _ = _value_matches(prop, o.value, item.expected)
        # Several charts in one file: the requirement is met by any of them.
        res.by_format[o.format] = "pass" if ok or res.by_format.get(o.format) == "pass" else "fail"
        if not ok:
            res.evidence.append(f"{o.format}: chart {prop} {o.value}")
    res.result = _combine(res.by_format)
    if res.result == "pass" and str(item.expected) == "histogram":
        res.evidence.append("the histogram is a column chart over computed bins")
    return res


def _heading_texts(observations: Sequence[I.Observation]) -> Dict[str, List[str]]:
    out: Dict[str, List[str]] = {}
    for o in observations:
        if o.target == "document" and o.property == "headings":
            out.setdefault(o.format, []).extend(str(v) for v in (o.value or []))
    return out


_STOP = frozenset({"a", "an", "the", "and", "of", "for", "to", "in", "on", "with", "&"})


def _stems(text: str) -> Set[str]:
    out: Set[str] = set()
    for w in re.findall(r"[\w&]+", text or ""):
        w = w.lower()
        if w in _STOP or w.isdigit():
            continue
        for suffix in ("ies", "es", "s", "ing", "ed"):
            if len(w) > len(suffix) + 2 and w.endswith(suffix):
                w = w[: -len(suffix)] + ("y" if suffix == "ies" else "")
                break
        out.add(w)
    return out


def _eval_content(item: RQ.ChecklistItem, observations: Sequence[I.Observation]) -> ItemResult:
    name = item.target.split(":", 1)[-1]
    res = ItemResult(item, "unverifiable")
    for fmt, heads in _heading_texts(observations).items():
        norms = [I.norm_text(h) for h in heads]
        want = _stems(name)
        # "Risks" is met by "2. Risk Assessment": every content word of the
        # requested name, stemmed, among the heading's words.
        ok = any(name in h or (len(h) >= 3 and h in name) or (want and want <= _stems(h))
                 or difflib.SequenceMatcher(None, name, h).ratio() >= 0.8 for h in norms)
        res.by_format[fmt] = "pass" if ok else "fail"
        if not ok:
            res.evidence.append(f"{fmt}: no heading like '{name}'")
    res.result = _combine(res.by_format)
    return res


def _eval_data(item: RQ.ChecklistItem, observations: Sequence[I.Observation]) -> ItemResult:
    res = ItemResult(item, "unverifiable")
    if item.property == "row_count":
        counts: Dict[str, List[int]] = {}
        for o in _obs(observations, "sheet", "row_count"):
            counts.setdefault(o.format, []).append(int(o.value or 0))
        for fmt, values in counts.items():
            ok = int(item.expected) in values
            res.by_format[fmt] = "pass" if ok else "fail"
            if not ok:
                res.evidence.append(f"{fmt}: {values} rows")
    else:
        name = item.target.split(":", 1)[-1]
        headers: Dict[str, List[str]] = {}
        for o in observations:
            if (o.target, o.property) in (("sheet", "columns"), ("table", "columns")):
                headers.setdefault(o.format, []).extend(I.norm_text(v) for v in (o.value or []))
        for fmt, names in headers.items():
            ok = any(name == h or name in h.split() or h.startswith(name) for h in names)
            res.by_format[fmt] = "pass" if ok else "fail"
            if not ok:
                res.evidence.append(f"{fmt}: columns {names[:8]}")
    res.result = _combine(res.by_format)
    return res


def _doc_text(observations: Sequence[I.Observation]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for o in observations:
        if o.target == "document" and o.property == "text" and o.format in ("docx", "pdf", "pptx"):
            out[o.format] = out.get(o.format, "") + "\n" + str(o.value or "")
    return out


def _coverage_norm(text: str) -> str:
    s = I.norm_text(text)
    s = re.sub(r"(?<=\d),(?=\d)", "", s)
    s = s.replace("|", " ")
    return " ".join(s.split())


def _eval_faithfulness(item: RQ.ChecklistItem, observations: Sequence[I.Observation], ectx: EvalContext) -> ItemResult:
    key = "headings" if item.property == "headings_covered" else "cells"
    wanted = [w for w in (ectx.source_structure.get(key) or []) if _coverage_norm(w)]
    res = ItemResult(item, "unverifiable")
    if not ectx.source_structure:
        res.evidence.append("no source structure")
        return res
    cell_lists: Dict[str, List[str]] = {}
    for o in observations:
        if o.target == "document" and o.property == "table_cells" and o.format in ("docx", "pptx"):
            cell_lists.setdefault(o.format, []).extend(str(v) for v in (o.value or []))
    for fmt, text in _doc_text(observations).items():
        if key == "cells" and fmt in cell_lists:
            # Tables survive as tables: compare as a MULTISET, so a value
            # that repeats ("Open" in three rows) cannot hide a lost row.
            missing = _missing_multiset(wanted, cell_lists[fmt])
        else:
            hay = _coverage_norm(text)
            # PDF text objects break lines inside a cell or heading: compare
            # with whitespace removed as the second chance.
            hay_tight = hay.replace(" ", "")
            missing = [w for w in wanted if _coverage_norm(w) not in hay and _coverage_norm(w).replace(" ", "") not in hay_tight]
        share = 1.0 if not wanted else (len(wanted) - len(missing)) / len(wanted)
        need = 0.99 if key == "headings" else 1.0
        res.by_format[fmt] = "pass" if share >= need else "fail"
        if missing:
            res.evidence.append(f"{fmt}: {len(missing)} of {len(wanted)} source {key} missing, e.g. {missing[0][:60]!r}")
    res.result = _combine(res.by_format)
    return res


def _missing_multiset(wanted: Sequence[str], have: Sequence[str]) -> List[str]:
    from collections import Counter

    pool = Counter(_coverage_norm(h) for h in have)
    numbers: Dict[float, int] = {}
    for h, n in list(pool.items()):
        value = _num(h.replace(" ", ""))
        if value is not None:
            numbers[value] = numbers.get(value, 0) + n
    missing: List[str] = []
    for w in wanted:
        k = _coverage_norm(w)
        if pool.get(k, 0) > 0:
            pool[k] -= 1
            value = _num(k.replace(" ", ""))
            if value is not None and numbers.get(value, 0) > 0:
                numbers[value] -= 1
            continue
        value = _num(k.replace(" ", ""))
        if value is not None and numbers.get(value, 0) > 0:
            # "1200" in the answer, "1,200.00" in a numeric column.
            numbers[value] -= 1
            continue
        missing.append(w)
    return missing


def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)


def _section_blocks(spec: Any) -> Dict[str, str]:
    """H1 section name -> canonical JSON of its blocks (documents); sheet or
    slide title -> canonical JSON (workbooks, presentations)."""
    out: Dict[str, str] = {}
    try:
        body = spec.body
    except Exception:  # noqa: BLE001
        return out
    blocks = list(getattr(body, "blocks", None) or [])
    if blocks:
        current = "(before the first heading)"
        acc: Dict[str, List[Any]] = {}
        for b in blocks:
            if getattr(b, "type", "") == "heading" and int(getattr(b, "level", 9)) == 1:
                current = b.text
            acc.setdefault(current, []).append(b.model_dump(mode="json"))
        return {k: _canonical(v) for k, v in acc.items()}
    for sheet in list(getattr(body, "sheets", None) or []):
        out[sheet.name] = _canonical(sheet.model_dump(mode="json"))
    for i, slide in enumerate(list(getattr(body, "slides", None) or [])):
        out[f"{i}:{slide.title}"] = _canonical(slide.model_dump(mode="json"))
    return out


def _file_sections(observations: Sequence[I.Observation], fmt: str) -> Dict[str, str]:
    for o in observations:
        if o.format == fmt and o.target == "document" and o.property == "sections":
            return {k: _coverage_norm(v) for k, v in (o.value or {}).items()}
    return {}


def _eval_preservation(item: RQ.ChecklistItem, observations: Sequence[I.Observation], ectx: EvalContext) -> ItemResult:
    res = ItemResult(item, "unverifiable")
    if ectx.parent_spec is None or ectx.spec is None:
        res.evidence.append("no parent version to compare")
        return res
    before, after = _section_blocks(ectx.parent_spec), _section_blocks(ectx.spec)
    untouched = [name for name, blob in before.items() if after.get(name) == blob]
    changed = [name for name in before if name in after and after[name] != before[name]]
    if ectx.touched is not None:
        touched_norm = {I.norm_text(t) for t in ectx.touched}
        outside = [n for n in changed if I.norm_text(n) not in touched_norm]
        if outside:
            res.result = "fail"
            res.evidence.append(f"spec: sections outside the edit changed: {outside[:3]}")
            res.by_format["spec"] = "fail"
            return res
    if ectx.touched is None:
        verdict = _unexplained_changes(before, after, ectx.instruction)
        if verdict is None:
            res.evidence.append("the edit applies to the whole file; nothing is held unchanged")
            return res
        if verdict:
            res.result = "fail"
            res.by_format["spec"] = "fail"
            res.evidence.append(f"sections the request did not name changed or disappeared: {verdict[:3]}")
            return res
    if ectx.parent_observations is None:
        res.evidence.append("the parent version's files could not be read")
        return res
    # File level: the untouched sections' TEXT in the new files equals the
    # parent's files (docx sections; xlsx/csv cells via table_cells).
    for fmt in ("docx",):
        now, then = _file_sections(observations, fmt), _file_sections(ectx.parent_observations, fmt)
        if not now or not then:
            continue
        diffs = [name for name in untouched if name in then and now.get(name) != then[name]]
        res.by_format[fmt] = "fail" if diffs else "pass"
        if diffs:
            res.evidence.append(f"{fmt}: untouched sections differ from the previous version: {diffs[:3]}")
    if ectx.spec is not None and getattr(ectx.spec, "kind", "") == "workbook":
        untouched_sheets = set(untouched)
        now_cells = _sheet_cells(observations)
        then_cells = _sheet_cells(ectx.parent_observations)
        diffs = [s for s in untouched_sheets if s in then_cells and now_cells.get(s) != then_cells[s]]
        if then_cells:
            res.by_format["xlsx"] = "fail" if diffs else "pass"
            if diffs:
                res.evidence.append(f"xlsx: untouched sheets differ from the previous version: {diffs[:3]}")
    res.result = _combine(res.by_format)
    return res


_WHOLE_EDIT_RE = re.compile(
    r"\b(whole|entire|everything|every|all|throughout|overall|rewrite|re-write|redo|regenerate|shorten|shorter|longer|lengthen|expand"
    r"|summar(?:y|i[sz]e)|condense|translate|proofread|typos?|spelling|grammar|tone|simplif(?:y|ied)|restructure|reorder|reorgani[sz]e"
    r"|undo|revert|go\s+back|previous\s+version|poora|pura|pure|puri|sab|saare|sabhi|har)\b"
    r"|पूरा|पूरी|सभी|सब|हर|આખું|આખો|બધા|બધું",
    re.I,
)
_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1}


def _named_units(names: Sequence[str], instruction: str) -> Set[str]:
    """The sections/sheets/slides of the PARENT the request names: by their
    words ("update the Actions section", "rename Findings") or by position
    ("section 3", "the third slide", "last sheet")."""
    words = _stems(instruction)
    low = (instruction or "").lower()
    named: Set[str] = set()
    ordered = [n for n in names if n != "(before the first heading)"]
    for name in ordered:
        title = name.split(":", 1)[1] if re.match(r"^\d+:", name) else name
        want = _stems(re.sub(r"^\s*\d+[.)]?\s*", "", title))
        if want and want <= words:
            named.add(name)
    positions: List[int] = [int(m.group(1)) for m in re.finditer(r"\b(?:section|slide|sheet|part|chapter|tab|page)\s*(?:no\.?\s*|number\s*|#\s*)?(\d{1,3})\b", low)]
    positions += [int(m.group(1)) for m in re.finditer(r"\b(\d{1,3})(?:st|nd|rd|th)\s+(?:section|slide|sheet|part|chapter|tab)\b", low)]
    positions += [_ORDINAL_WORDS[m.group(1)] for m in re.finditer(r"\b(first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last)\s+(?:section|slide|sheet|part|chapter|tab)\b", low)]
    for pos in positions:
        if pos == -1 and ordered:
            named.add(ordered[-1])
        elif 1 <= pos <= len(ordered):
            named.add(ordered[pos - 1])
    return named


def _unexplained_changes(before: Dict[str, str], after: Dict[str, str], instruction: str) -> Optional[List[str]]:
    """Units of the parent whose content is not found unchanged in the new
    version and that the request did not name. None when the request
    changes the whole file (nothing is held unchanged). Without a named
    unit the edit's location is unknown, so ONE changed unit is allowed.

    Before this, "untouched" was every section whose spec had not changed —
    a model that rewrote every section passed preservation by construction
    (verifier 2026-09-15)."""
    if _WHOLE_EDIT_RE.search(instruction or ""):
        return None
    remaining = list(after.values())
    lost: List[str] = []
    for name, blob in before.items():
        if blob in remaining:
            remaining.remove(blob)
        else:
            lost.append(name)
    named = _named_units(list(before), instruction)
    unexplained = [n for n in lost if n not in named]
    if named:
        return unexplained
    return unexplained if len(unexplained) > 1 else []


def _sheet_cells(observations: Sequence[I.Observation]) -> Dict[str, List[Any]]:
    out: Dict[str, List[Any]] = {}
    for o in observations:
        if o.format == "xlsx" and o.target == "sheet" and o.property == "cell_values":
            out[str(o.locator.get("sheet"))] = list(o.value or [])
    return out


def _eval_language(item: RQ.ChecklistItem, observations: Sequence[I.Observation]) -> ItemResult:
    res = ItemResult(item, "unverifiable")
    for fmt, text in _doc_text(observations).items():
        counts = I.script_of(text)
        total = sum(counts.values()) or 1
        ok = counts.get(str(item.expected), 0) / total >= 0.3
        res.by_format[fmt] = "pass" if ok else "fail"
        if not ok:
            res.evidence.append(f"{fmt}: script mix {counts}")
    res.result = _combine(res.by_format)
    return res


def _eval_house(item: RQ.ChecklistItem, observations: Sequence[I.Observation], ectx: EvalContext) -> ItemResult:
    res = ItemResult(item, "unverifiable")
    prop = item.property
    if prop == "page_numbers":
        lr = _eval_layout(RQ.ChecklistItem(item.id, "layout", "page", "page_numbers", True), observations)
        return ItemResult(item, lr.result, lr.evidence, lr.by_format)
    if prop == "title_block":
        for o in observations:
            if o.target in ("title", "slide_title") and o.property == "size_pt" and o.format in ("docx", "pdf", "pptx"):
                ok = float(o.value or 0) >= 18
                res.by_format[o.format] = "pass" if ok or res.by_format.get(o.format) == "pass" else "fail"
        for fmt in {o.format for o in observations if o.format in ("docx", "pdf")}:
            res.by_format.setdefault(fmt, "fail")
            if res.by_format[fmt] == "fail":
                res.evidence.append(f"{fmt}: no title set in a title size")
    elif prop == "fill_present":
        fills = [o for o in observations if o.target == "table_header" and o.property == "background" and o.verifiable]
        for o in fills:
            ok = bool(o.value) and not _hex_close(o.value, "#FFFFFF", tol=8)
            prev = res.by_format.get(o.format)
            res.by_format[o.format] = "fail" if prev == "fail" or not ok else "pass"
        if not fills:
            has_tables = _spec_has_tables(ectx.spec)
            if not has_tables:
                res.evidence.append("no tables")
                res.result = "pass"
                return res
    elif prop == "no_letter_spacing":
        for o in _obs(observations, "title", "letter_spacing"):
            ok = abs(float(o.value or 0)) < 0.05
            res.by_format[o.format] = "pass" if ok else "fail"
            if not ok:
                res.evidence.append(f"{o.format}: title letter spacing {o.value}pt")
    elif prop == "fonts_cover_script":
        for o in _obs(observations, "document", "fonts_cover_script"):
            if o.verifiable and o.value is not None:
                res.by_format[o.format] = "pass" if o.value else "fail"
                if not o.value:
                    res.evidence.append(f"{o.format}: text set in fonts without the script: {o.locator.get('uncovered')}")
    elif prop == "contrast":
        pairs: Dict[Tuple[str, str], Dict[str, Any]] = {}
        for o in observations:
            if o.target == "table_header" and o.property in ("color", "background") and o.verifiable and o.value:
                key = (o.format, _canonical({k: v for k, v in o.locator.items() if k != "text"}) + str(o.locator.get("text", "")))
                pairs.setdefault(key, {})[o.property] = o.value
        for (fmt, _), pair in pairs.items():
            if "color" in pair and "background" in pair:
                ratio = I.contrast_ratio(I.norm_hex(pair["color"]) or "#000000", I.norm_hex(pair["background"]) or "#FFFFFF")
                ok = ratio >= 4.5
                res.by_format[fmt] = "fail" if res.by_format.get(fmt) == "fail" or not ok else "pass"
                if not ok:
                    res.evidence.append(f"{fmt}: header text {pair['color']} on {pair['background']} is {ratio:.1f}:1")
    res.result = _combine(res.by_format)
    return res


def _spec_has_tables(spec: Any) -> bool:
    try:
        body = spec.body
    except Exception:  # noqa: BLE001
        return False
    return (any(getattr(b, "type", "") == "table" for b in list(getattr(body, "blocks", None) or []))
            or any(getattr(s, "table", None) is not None for s in list(getattr(body, "slides", None) or []))
            or bool(list(getattr(body, "sheets", None) or [])))


def _eval_security(item: RQ.ChecklistItem, observations: Sequence[I.Observation]) -> ItemResult:
    res = ItemResult(item, "pass")
    names = ("unsafe_external_targets", "svg_problems") if item.property == "no_unsafe_links" else ("unprefixed_formula_text", "unexpected_formulas")
    for o in observations:
        if o.target == "security" and o.property in names:
            bad = list(o.value or [])
            res.by_format[o.format] = "fail" if bad or res.by_format.get(o.format) == "fail" else "pass"
            if bad:
                res.evidence.append(f"{o.format}: {o.property} {bad[:3]}")
    res.result = "fail" if "fail" in res.by_format.values() else "pass"
    return res


def evaluate(checklist: RQ.Checklist, observations: Sequence[I.Observation], ectx: EvalContext) -> List[ItemResult]:
    results: List[ItemResult] = []
    for item in checklist.items:
        try:
            if item.category == "style":
                r = _eval_style(item, observations)
            elif item.category == "layout":
                r = _eval_layout(item, observations)
            elif item.category == "format":
                r = _eval_format(item, observations)
            elif item.category == "chart":
                r = _eval_chart(item, observations, ectx)
            elif item.category == "content":
                r = _eval_content(item, observations)
            elif item.category == "data":
                r = _eval_data(item, observations)
            elif item.category == "faithfulness":
                r = _eval_faithfulness(item, observations, ectx)
            elif item.category == "preservation":
                r = _eval_preservation(item, observations, ectx)
            elif item.category == "language":
                r = _eval_language(item, observations)
            elif item.category == "house_style":
                r = _eval_house(item, observations, ectx)
            elif item.category == "security":
                r = _eval_security(item, observations)
            else:
                r = ItemResult(item, "unverifiable")
        except Exception as exc:  # noqa: BLE001 — one item's evaluator must not sink the rest
            log.warning("selfcheck: evaluating %s/%s failed: %s", item.category, item.property, type(exc).__name__)
            r = ItemResult(item, "unverifiable", [f"evaluator error {type(exc).__name__}"])
        if item.contested:
            r.evidence.insert(0, f"file shows: {r.result}")
            r.result = "contested"
        results.append(r)
    return results


def failing_musts(results: Sequence[ItemResult]) -> Set[str]:
    return {r.item.id for r in results if r.item.must and not r.item.contested and r.result == "fail"}


def passing(results: Sequence[ItemResult]) -> Set[str]:
    return {r.item.id for r in results if r.result == "pass"}


def accept_repair(before: Sequence[ItemResult], after: Sequence[ItemResult], *, operation: str) -> Tuple[bool, str]:
    """THE STRICT RULE: failing musts strictly shrink, nothing that passed
    fails now, and an edit still preserves. Returns (accepted, reason)."""
    fb, fa = failing_musts(before), failing_musts(after)
    after_by_id = {r.item.id: r for r in after}
    regressed = [i for i in passing(before) if after_by_id.get(i) is not None and after_by_id[i].result == "fail"]
    if regressed:
        return False, "regressed"
    if operation == "edit" and any(r.item.category == "preservation" and r.result == "fail" for r in after):
        return False, "preservation"
    if not (fa < fb):
        return False, "not_improved"
    return True, ""


# -------------------------------------------------------------- chart values --


def chart_values_check(spec: Any, observations: Sequence[I.Observation], tables: Sequence[Any] = ()) -> Optional[Tuple[bool, List[str]]]:
    """Numbers in the native charts == the numbers code computed.

    When the charts track is merged, chart_data.recompute_matches recomputes
    each bound chart from its tables; the file comparison below then holds
    the FILE to those recomputed series. Without it, the spec's series are
    the expected values (a renderer that drops or reorders a point fails)."""
    native = [o for o in observations if o.target == "chart" and o.property == "values" and o.format in ("xlsx", "pptx")]
    charts = list(_spec_charts(spec))
    if not charts:
        return None
    diffs: List[str] = []
    try:
        from . import chart_data  # type: ignore

        # AS3 integration: a LITERAL chart (an old spec.json, or a restore)
        # has nothing to recompute — the file is held to its spec below; a
        # sheet chart is recomputed over its own rows; a chart whose table is
        # not in this job's material (an edit turn that attached nothing) is
        # not verifiable here rather than a failure.
        table_ids = {str(t.get("id") if isinstance(t, dict) else getattr(t, "id", "")) for t in tables}
        for sheet_dict, chart in _charts_with_sheets(spec):
            data = getattr(chart, "data", None)
            if data is None:
                continue
            own = chart_data._sheet_table(sheet_dict) if sheet_dict is not None else None
            tid = str(getattr(data, "table_id", "") or "")
            if own is None and tid not in table_ids:
                continue
            if own is not None and tid and tid not in table_ids and tid != str(own.get("id") if isinstance(own, dict) else getattr(own, "id", "")):
                continue
            ok, why = chart_data.recompute_matches(chart, list(tables), default_table=own)
            if not ok:
                diffs.extend(str(w) for w in why[:2])
    except ImportError:
        pass
    if not native:
        return (not diffs, diffs) if diffs else None
    expected = [[[float(v) for v in s.values] for s in chart.series] for chart in charts]
    for o in native:
        got = [[_num(v) for v in (series or [])] for series in (o.value or {}).get("series", [])]
        match = any(_series_equal(got, exp) for exp in expected)
        if not match:
            diffs.append(f"{o.format}: chart values {got[:2]} match no chart of the spec")
    return (not diffs, diffs)


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _series_equal(got: List[List[Optional[float]]], exp: List[List[float]]) -> bool:
    if len(got) != len(exp):
        return False
    for g, e in zip(got, exp):
        if len(g) != len(e):
            return False
        for a, b in zip(g, e):
            if a is None:
                # A chart bound to a blank cell draws a gap; the spec's
                # series says 0 for the same point (verifier 2026-09-15).
                if b != 0:
                    return False
                continue
            if abs(a - b) > 1e-9 * max(1.0, abs(b)):
                return False
    return True


def _charts_with_sheets(spec: Any):
    """(sheet as a dict or None, chart) for every chart of the spec."""
    try:
        body = spec.body
    except Exception:  # noqa: BLE001
        return
    for b in list(getattr(body, "blocks", None) or []):
        if getattr(b, "type", "") == "chart":
            yield None, b.chart
    for sl in list(getattr(body, "slides", None) or []):
        if getattr(sl, "chart", None) is not None:
            yield None, sl.chart
    for sh in list(getattr(body, "sheets", None) or []):
        sheet_dict = None
        for c in list(getattr(sh, "charts", None) or []):
            if sheet_dict is None:
                sheet_dict = sh.model_dump(mode="python") if hasattr(sh, "model_dump") else dict(sh)
            yield sheet_dict, c


def _spec_charts(spec: Any):
    try:
        body = spec.body
    except Exception:  # noqa: BLE001
        return
    for b in list(getattr(body, "blocks", None) or []):
        if getattr(b, "type", "") == "chart":
            yield b.chart
    for s in list(getattr(body, "slides", None) or []):
        if getattr(s, "chart", None) is not None:
            yield s.chart
    for sh in list(getattr(body, "sheets", None) or []):
        for c in list(getattr(sh, "charts", None) or []):
            yield c


# ----------------------------------------------------------------- repair --


@dataclass
class RepairPlan:
    spec: Any = None
    formats_added: List[str] = field(default_factory=list)
    item_ids: List[str] = field(default_factory=list)
    kinds: List[str] = field(default_factory=list)  # code | model
    model_calls: int = 0
    notes: List[str] = field(default_factory=list)


def _set_body(spec: Any, **updates: Any) -> Any:
    body = spec.body.model_copy(update=updates)
    return spec.model_copy(update={spec.kind: body})


def _style_patch_for(items: Sequence[RQ.ChecklistItem]):
    """A style.StylePatch carrying the failed style items (styling track)."""
    from . import style as _style  # type: ignore

    rules = []
    for it in items:
        target_kind = "heading" if it.target.startswith("heading") else it.target.split(":", 1)[0]
        target_kw: Dict[str, Any] = {"kind": target_kind}
        if it.target in ("heading1", "heading2", "heading3"):
            target_kw["level"] = int(it.target[-1])
        if it.target.startswith(("column:", "row:", "cell_range:")):
            rest = it.target.split(":", 1)[1]
            target_kw["name" if target_kind == "column" else ("index" if target_kind == "row" else "a1")] = int(rest) if target_kind == "row" else rest
        rules.append(_style.StyleRule(target=_style.StyleTarget(**target_kw), style=_style.TextStyle(**{it.property: it.expected})))
    return _style.StylePatch(rules=rules)


async def plan_repair(results: Sequence[ItemResult], spec: Any, *, kind: str, operation: str, effort: str, formats: Sequence[str],
                      tables: Sequence[Any] = (), ccx: Any = None, allow_model: bool = True) -> Optional[RepairPlan]:
    """One revised spec (or an added format) that addresses the failed
    must-items by code first. None when nothing failed is repairable."""
    failed = [r for r in results if r.item.must and not r.item.contested and r.result == "fail"]
    if not failed:
        return None
    plan = RepairPlan(spec=spec)
    changed = False
    style_items = [r.item for r in failed if r.item.category == "style"]
    layout_items = [r.item for r in failed if r.item.category == "layout" and r.item.property in ("orientation", "page_size", "margins")]
    if style_items:
        try:
            from . import edits as _edits  # type: ignore

            patch = _style_patch_for(style_items)
            outcome = _edits.apply(plan.spec, _edits.ops_for_style(patch))
            if getattr(outcome, "spec", None) is not None:
                plan.spec = outcome.spec
                plan.item_ids.extend(i.id for i in style_items)
                plan.kinds.append("code")
                changed = True
        except Exception as exc:  # noqa: BLE001 — the edits/style tracks are absent or refused the patch
            plan.notes.append(f"style repair unavailable ({type(exc).__name__})")
    for it in layout_items:
        done = False
        try:
            from . import edits as _edits  # type: ignore

            op = {"orientation": it.expected} if it.property == "orientation" else {"page": {it.property: it.expected}}
            outcome = _edits.apply(plan.spec, _edits.ops_for_layout(op.get("orientation") or op))
            if getattr(outcome, "spec", None) is not None:
                plan.spec = outcome.spec
                done = True
        except Exception:  # noqa: BLE001
            done = False
        if not done and it.property == "orientation" and kind == "document" and "orientation" in type(plan.spec.body).model_fields:
            # Built-in fallback: DocumentSpec has carried orientation since v1.
            plan.spec = _set_body(plan.spec, orientation=str(it.expected))
            done = True
        if done:
            plan.item_ids.append(it.id)
            plan.kinds.append("code")
            changed = True
    for r in failed:
        if r.item.category == "format":
            fmt = str(r.item.expected)
            if fmt in T.FORMATS_FOR_KIND.get(kind, ()) and fmt not in formats:
                plan.formats_added.append(fmt)
                plan.item_ids.append(r.item.id)
                plan.kinds.append("code")
                changed = True
    if any(r.item.property == "values_match" for r in failed):
        try:
            from . import chart_data  # type: ignore

            fixed, _notes = await asyncio.to_thread(chart_data.resolve_spec, plan.spec, list(tables))
            plan.spec = fixed
            plan.item_ids.extend(r.item.id for r in failed if r.item.property == "values_match")
            plan.kinds.append("code")
            changed = True
        except ImportError:
            plan.notes.append("chart values cannot be recomputed without the charts track")
    content = [r for r in failed if r.item.category == "content"]
    if content and allow_model and effort in ("think", "max") and ccx is not None:
        issues = [{"where": r.item.target.split(":", 1)[-1], "problem": "the section the person asked for is missing",
                   "fix": f"add a section titled {r.item.target.split(':', 1)[-1]!r} with real content"} for r in content]
        try:
            if operation == "edit":
                from . import compose as _compose

                reviser = getattr(_compose, "revise_section", None)
                if reviser is None:
                    raise ImportError("revise_section")
                revised = await reviser(None, plan.spec, None, [i["problem"] for i in issues])
            else:
                repairer = _content_repairer or _default_content_repairer
                revised = await repairer(ccx, plan.spec, issues)
            plan.model_calls += 1
            if revised is not None:
                plan.spec = _carry_style(plan.spec, revised)
                plan.item_ids.extend(r.item.id for r in content)
                plan.kinds.append("model")
                changed = True
        except ImportError:
            plan.notes.append("section repair on an edit needs compose.revise_section")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            plan.model_calls += 1
            plan.notes.append(f"content repair failed ({type(exc).__name__})")
    return plan if changed else None


async def _default_content_repairer(ccx: Any, spec: Any, issues: List[dict]) -> Any:
    from . import compose as C

    m = dict(ccx.material or {})
    # The WHOLE material: a revision without the job's tables would have the
    # model retype (or invent) rows the composer copied by code
    # (verifier 2026-09-15). Same reading as engines.artifact._material_from_dict.
    row_count = m.get("row_count")
    transform = m.get("transform") if isinstance(m.get("transform"), dict) else {}
    material = C.Material(
        instruction=ccx.instruction, history_text=str(m.get("history_text") or ""), previous_answer=str(m.get("previous_answer") or ""),
        uploads_text=str(m.get("uploads_text") or ""), notes=[str(n) for n in m.get("notes") or []],
        sources=[C.Source(**{k: v for k, v in s.items() if k in C.Source.__dataclass_fields__}) for s in m.get("sources") or [] if isinstance(s, dict)],
        tables=[C.DataTable(**{k: v for k, v in t.items() if k in C.DataTable.__dataclass_fields__}) for t in m.get("tables") or [] if isinstance(t, dict)],
        transform=dict(transform or {}),
        row_count=row_count if isinstance(row_count, int) and not isinstance(row_count, bool) and row_count > 0 else None,
    )
    req = C.ComposeRequest(kind=ccx.kind, formats=ccx.formats, template_id=ccx.template_id, effort=ccx.effort, operation="edit",
                           material=material, parent_spec=spec, instruction=ccx.instruction)
    return await C.revise(req, spec, issues)


def _carry_style(original: Any, revised: Any) -> Any:
    """Code-owned fields a model revision must not drop: spec.style (the
    styling track) and each chart's data binding (the charts track)."""
    try:
        from .pipeline import carry_code_owned

        return carry_code_owned(original, revised)
    except Exception:  # noqa: BLE001
        return revised


# ------------------------------------------------------------------ hook --


def budget_for(effort: str) -> float:
    return float({"fast": settings.artifact_selfcheck_budget_fast_s, "think": settings.artifact_selfcheck_budget_think_s,
                  "max": settings.artifact_selfcheck_budget_max_s}.get(effort, settings.artifact_selfcheck_budget_fast_s))


def _version_paths(directory: str, validation: Optional[dict]) -> Dict[str, Path]:
    out: Dict[str, Path] = {}
    for f in list((validation or {}).get("files") or []):
        name = os.path.basename(str((f or {}).get("filename") or ""))
        path = os.path.join(directory, name)
        if name and os.path.isfile(path) and not os.path.islink(path):
            out[name] = Path(path)
    return out


def _parent_version_number(job: dict) -> Optional[int]:
    try:
        from . import db as adb

        row = adb.get_version(str(job["artifact_id"]), int(job["version"]), int(job["user_id"])) or {}
        parent = row.get("parent_version")
        if parent:
            return int(parent)
        art = adb.get_artifact(str(job["artifact_id"]), int(job["user_id"])) or {}
        current = int(art.get("current_version") or 0)
        return current if current and current != int(job["version"]) else None
    except Exception:  # noqa: BLE001
        return None


def _touched(job: dict) -> Optional[Set[str]]:
    progress = job.get("progress") if isinstance(job.get("progress"), dict) else {}
    edit = progress.get("edit") if isinstance(progress.get("edit"), dict) else {}
    touched = edit.get("touched")
    if isinstance(touched, (list, tuple, set)):
        return {str(t) for t in touched}
    return None


def _source_summary(material: dict) -> str:
    for key in ("previous_answer", "uploads_text", "history_text"):
        text = str(material.get(key) or "").strip()
        if text:
            return text[:1500] if key != "history_text" else text[-1500:]
    return ""


def _unmet_line(r: ItemResult) -> str:
    what = RQ.describe(r.item)
    if r.result == "contested":
        phrase = (r.item.phrase or "").strip()
        alt = r.item.note.replace("the request can also be read as ", "") if r.item.note else ""
        return f"I read '{phrase[:60]}' as {what}" + (f" (it could also mean {alt})" if alt else "")
    ev = f" — {r.evidence[0]}" if r.evidence else ""
    return f"not met: {what}{ev}"[:240]


def _claim_line(r: ItemResult) -> str:
    """What may be claimed for a met item. A font met by a mapped
    equivalent says which font the file uses, so the answer never claims
    'body in Georgia' for a PDF drawn in Caladea."""
    what = RQ.describe(r.item)
    if r.item.property == "font_family":
        note = next((e for e in r.evidence if " was set in " in e or " was drawn with " in e), "")
        if note:
            return f"{what} ({note})"[:240]
    return what


def summarize(results: Sequence[ItemResult], *, repaired_ids: Sequence[str] = ()) -> SelfcheckReport:
    report = SelfcheckReport(items=[r.to_dict() for r in results])
    report.passed = sum(r.result == "pass" for r in results)
    report.failed = sum(r.result == "fail" for r in results)
    report.unverifiable = sum(r.result == "unverifiable" for r in results)
    report.contested = sum(r.result == "contested" for r in results)
    report.repaired = sum(1 for r in results if r.item.id in set(repaired_ids) and r.result == "pass")
    unmet = [r for r in results if r.result == "fail" and r.item.must] + [r for r in results if r.result == "contested"]
    report.unmet = [_unmet_line(r) for r in unmet][:3]
    report.false_claim_guard = {
        "claimable": [_claim_line(r) for r in results if r.result == "pass" and r.item.category not in ("security", "house_style")],
        "not_claimable": [RQ.describe(r.item) for r in results if r.result in ("fail", "contested", "unverifiable") and r.item.category not in ("security",)],
    }
    return report


async def run_hook(runner: Any, ctx: Any, stages: Dict[str, dict], progress: Dict[str, Any]) -> Optional[str]:
    """The pipeline hook. Returns a failure string only when restoring the
    pre-repair files itself failed (the job must not publish a mix)."""
    from . import pipeline as P

    started = time.perf_counter()
    job = ctx.job
    effort = str(job.get("effort") or "fast")
    budget = budget_for(effort)
    report = SelfcheckReport()
    P._publish(runner.job_id, {"stage": CHECK_STAGE, "status": "running", "percent": None, "detail": "checking the files against the request", "elapsed_s": 0})
    restore_failure: Optional[str] = None
    try:
        report, restore_failure = await _run(runner, ctx, stages, progress, started=started, budget=budget, effort=effort)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the self-check never fails a job
        log.exception("artifact job %s: self-check failed", str(runner.job_id)[:8])
        report.outcome = "error"
        report.notes.append(type(exc).__name__)
    report.seconds = time.perf_counter() - started
    _record(runner, ctx, progress, report)
    try:
        await asyncio.to_thread(store.write_json, os.path.join(ctx.work_dir, SELFCHECK_NAME), report.to_dict())
        from .. import db as core_db
        from . import db as adb

        progress["selfcheck"] = {k: v for k, v in report.to_dict().items() if k != "items"}
        await core_db.run_in_thread(adb.set_job_progress, runner.job_id, {**ctx.job.get("progress", {}), **{"selfcheck": progress["selfcheck"]}, "warnings": ctx.warnings})
        ctx.job["progress"] = {**ctx.job.get("progress", {}), "selfcheck": progress["selfcheck"]}
    except Exception:  # noqa: BLE001
        log.warning("artifact job %s: could not persist the self-check report", str(runner.job_id)[:8], exc_info=True)
    detail = f"{report.passed} met · {report.failed} not met · {report.unverifiable} not checkable" + (f" · {report.repaired} fixed" if report.repaired else "")
    P._publish(runner.job_id, {"stage": CHECK_STAGE, "status": "done", "percent": 100, "detail": detail, "elapsed_s": round(report.seconds, 1)})
    return restore_failure


async def _run(runner: Any, ctx: Any, stages: Dict[str, dict], progress: Dict[str, Any], *, started: float, budget: float,
               effort: str) -> Tuple[SelfcheckReport, Optional[str]]:
    from . import pipeline as P

    job = ctx.job
    operation = str(job.get("operation") or "create")
    spec = ctx.load_spec()
    material = ctx.load_material()
    validation = store.read_json(os.path.join(ctx.work_dir, T.VALIDATION_NAME)) or {}
    paths = _version_paths(ctx.work_dir, validation)
    formats = list(job.get("selected_formats") or [])
    busy = P._busy_probe

    def remaining() -> float:
        return budget - (time.perf_counter() - started)

    checklist = await asyncio.wait_for(
        RQ.build(job, instruction=str(job.get("instruction") or ""), kind=spec.kind, formats=formats, operation=operation,
                 parent_spec=ctx.parent_spec, source_summary=_source_summary(material), effort=effort,
                 source_markdown=str(material.get("previous_answer") or ""), busy=busy,
                 model_enabled=effort in ("think", "max"), timeout_s=min(8.0, max(1.0, remaining() / 3))),
        timeout=max(1.0, remaining()),
    )
    # The format decision is formats.py's contract with the engine: a format
    # word the checklist read but the engine did not select ("convert this
    # pdf to word") is recorded, never a must and never added by a repair.
    for item in checklist.items:
        if item.category == "format" and str(item.expected) not in formats:
            item.must = False
            item.note = "not among the formats this job was asked to make"
    # Charts in the spec: the numbers in the file are a must, whatever was asked.
    if any(True for _ in _spec_charts(spec)) and len(checklist.items) < RQ.MAX_ITEMS:
        checklist.items.append(RQ.ChecklistItem(f"c{len(checklist.items) + 1:02d}", "chart", "chart", "values_match", True, must=True, phrase="chart values from the data"))
    model_calls = checklist.model_calls

    tables = list(material.get("tables") or [])
    ectx = EvalContext(spec=spec, source_structure=checklist.source_structure, parent_spec=ctx.parent_spec, touched=_touched(job),
                       instruction=str(job.get("instruction") or ""))
    if operation == "edit" and ctx.parent_spec is not None:
        parent_n = await asyncio.to_thread(_parent_version_number, job)
        if parent_n:
            pdir = store.version_dir(int(job["user_id"]), str(job["artifact_id"]), parent_n)
            ppaths = _version_paths(pdir, store.read_json(os.path.join(pdir, T.VALIDATION_NAME)) or store.read_json(os.path.join(pdir, T.MANIFEST_NAME)))
            if ppaths:
                ectx.parent_observations = await asyncio.to_thread(_inspect_for_preservation, ppaths, ctx.parent_spec)

    async def observe_and_evaluate(the_spec: Any, the_paths: Dict[str, Path]) -> List[ItemResult]:
        observations = await asyncio.wait_for(asyncio.to_thread(_inspect_for_preservation, the_paths, the_spec), timeout=max(1.0, remaining()))
        ectx.spec = the_spec
        ectx.chart_values_ok = await asyncio.to_thread(chart_values_check, the_spec, observations, tables)
        # Items x observations (a 60-page PDF reads thousands), and a font
        # item may ask fc-match once per family: off the event loop.
        return await asyncio.to_thread(evaluate, checklist, observations, ectx)

    results = await observe_and_evaluate(spec, paths)
    report = summarize(results)
    report.checklist = checklist.to_dict()
    report.model_calls = model_calls
    if checklist.model_skipped == "busy":
        report.notes.append("the checklist proposer was skipped because chat was busy")

    restore_failure: Optional[str] = None
    if failing_musts(results) and settings.artifact_selfcheck_repair:
        allow_model = effort in ("think", "max") and not (busy is not None and _busy(busy))
        estimate = _rerender_estimate(stages)
        if remaining() < estimate:
            report.repair = {"attempted": False, "reason": "budget", "estimate_s": round(estimate, 1)}
            report.outcome = "skipped_budget"
        else:
            ccx = P.ComposeContext(ctx, progress=lambda pct, detail: asyncio.sleep(0), progress_stage=lambda stage, status, detail="": asyncio.sleep(0))
            plan = await plan_repair(results, spec, kind=spec.kind, operation=operation, effort=effort, formats=formats,
                                     tables=tables, ccx=ccx, allow_model=allow_model and model_calls < 2)
            if plan is None:
                report.repair = {"attempted": False, "reason": "nothing repairable by code at this effort"}
            else:
                model_calls += plan.model_calls
                decision: Dict[str, Any] = {}
                original_formats = list(job.get("selected_formats") or [])

                async def judge() -> bool:
                    new_validation = store.read_json(os.path.join(ctx.work_dir, T.VALIDATION_NAME)) or {}
                    new_paths = _version_paths(ctx.work_dir, new_validation)
                    after = await observe_and_evaluate(ctx.load_spec(), new_paths)
                    ok, reason = accept_repair(results, after, operation=operation)
                    decision.update({"after": after, "reason": reason})
                    return ok

                if plan.formats_added:
                    job["selected_formats"] = original_formats + [f for f in plan.formats_added if f not in original_formats]
                P._publish(runner.job_id, {"stage": CHECK_STAGE, "status": "running", "percent": None, "detail": "fixing what the check found", "elapsed_s": 0})
                try:
                    accepted = await P._try_revision(runner, ctx, stages, plan.spec, "self-check repair", accept=judge)
                except P.RevisionRestoreFailed as exc:
                    accepted = False
                    restore_failure = str(exc)
                if not accepted:
                    job["selected_formats"] = original_formats
                    reason = decision.get("reason") or "render_failed"
                    metrics.inc("artifact_selfcheck_repair_rejected_total", "self-check repairs rejected", reason=reason)
                    report.repair = {"attempted": True, "accepted": False, "reason": reason, "kinds": plan.kinds, "notes": plan.notes}
                    ectx.spec = spec
                else:
                    after = decision["after"]
                    report_after = summarize(after, repaired_ids=plan.item_ids)
                    report_after.checklist, report_after.notes = report.checklist, report.notes
                    report = report_after
                    results = after
                    report.repair = {"attempted": True, "accepted": True, "kinds": plan.kinds, "formats_added": plan.formats_added, "notes": plan.notes}
        report.model_calls = model_calls
    if report.outcome not in ("skipped_budget",):
        if report.repair.get("accepted") and not failing_musts(results) and not report.contested:
            report.outcome = "repaired"
        elif failing_musts(results):
            report.outcome = "unmet"
        elif report.contested:
            report.outcome = "contested"
        elif checklist.model_skipped == "busy":
            report.outcome = "skipped_busy"
        else:
            report.outcome = "clean"
    if report.unmet:
        log.warning("artifact job %s: self-check %s: %s", str(runner.job_id)[:8], report.outcome, " | ".join(report.unmet))
    # Honesty: every unmet must and every contested reading is a warning,
    # so the version is completed_with_warnings and the card names it.
    for line in report.unmet:
        text = line[:300]
        if text not in ctx.warnings:
            ctx.warnings.append(text)
    return report, restore_failure


def _inspect_for_preservation(paths: Dict[str, Path], spec: Any) -> List[I.Observation]:
    observations = I.inspect(paths, spec)
    # Sheet cell values for workbook preservation, read once here.
    for name, p in paths.items():
        if p.suffix.lower() != ".xlsx":
            continue
        try:
            import openpyxl

            wb = openpyxl.load_workbook(str(p), read_only=True, data_only=False)
            try:
                for ws in wb.worksheets:
                    values = [list(row) for row in ws.iter_rows(max_row=5000, values_only=True)]
                    observations.append(I.Observation("sheet", "cell_values", values, "xlsx", {"sheet": ws.title}))
            finally:
                wb.close()
        except Exception:  # noqa: BLE001
            continue
    return observations


def _busy(fn: Callable[[], bool]) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def _rerender_estimate(stages: Dict[str, dict]) -> float:
    """Seconds a re-render is expected to take: this job's own render,
    validate and preview times with a 1.5x margin (the p95 of one sample is
    itself), plus the re-inspection."""
    ms = sum(int((stages.get(s) or {}).get("ms") or 0) for s in ("render", "validate", "preview"))
    return ms / 1000.0 * 1.5 + 2.0


def _record(runner: Any, ctx: Any, progress: Dict[str, Any], report: SelfcheckReport) -> None:
    metrics.inc("artifact_selfcheck_jobs_total", "artifact self-check runs by outcome", outcome=report.outcome)
    metrics.observe("artifact_selfcheck_seconds", report.seconds, "seconds the artifact self-check took", phase="total")
    if report.model_calls:
        for _ in range(int(report.model_calls)):
            metrics.inc("artifact_selfcheck_model_calls_total", "model calls the artifact self-check made", phase="checklist_or_repair")
    for item in report.items:
        metrics.inc("artifact_selfcheck_items_total", "artifact self-check items by category and result",
                    category=str(item.get("category")), result=str(item.get("result")))
    try:
        from ..core import tracing

        if tracing.current() is not None:
            loop = asyncio.get_running_loop()
            loop.create_task(tracing.event("artifact_selfcheck", component="artifacts.selfcheck",
                                           details={"job_id": str(runner.job_id), "outcome": report.outcome, "passed": report.passed,
                                                    "failed": report.failed, "unverifiable": report.unverifiable, "contested": report.contested,
                                                    "repaired": report.repaired, "model_calls": report.model_calls},
                                           duration_ms=int(report.seconds * 1000)))
    except Exception:  # noqa: BLE001 — tracing is diagnostic
        pass


__all__ = [
    "SELFCHECK_NAME", "CHECK_STAGE", "ItemResult", "SelfcheckReport", "EvalContext", "RepairPlan", "evaluate", "accept_repair",
    "failing_musts", "plan_repair", "run_hook", "summarize", "font_matches", "chart_values_check", "set_content_repairer", "budget_for",
]
