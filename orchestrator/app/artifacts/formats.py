"""Which formats a request gets, and what kind of artifact it is, when the
person did not say — the format-selection policy, as a table with reasons.

    Request                                        Kind          Formats
    professional document / report / SOP / memo    document      docx + pdf
    final printable / shareable document           document      pdf
    presentation / deck / pitch / CEO presentation presentation  pptx (+ pdf preview)
    spreadsheet / tracker / model / calculator     workbook      xlsx
    dataset / data file / sample data / records    workbook      csv   (template "data")
    a table transform with styling words           workbook      xlsx  (+ csv only when named)
    one-page executive brief                       document      pdf + docx
    proposal / policy / letter                     document      docx + pdf
    visual handout                                 document      pdf
    "best format"                                  decided from the task words
    "all deliverables"                             only the formats that are useful

An EXPLICIT format always wins over the table, and a LIST of formats is
honoured in the order it was said: "XLSX, Word, PDF and CSV of this audit"
is four files from one workbook spec (CONTRACT-2 §1, §5). Nothing here is
a guess in natural language: `decide()` returns a `FormatDecision` with
the rule that fired, which is stored in the job's metadata so a person can
see why a deck came back as a .pptx.

WHAT COUNTS AS NAMING A FORMAT. The alias table below reads the words
people actually type — `spread sheet`, `xlxs`, `exel`, `powerpint`, `ppt`,
a bare `word` inside a list of formats, `comma-separated` — because the
discovery of 2026-09-12 found "Give me the audit as a cvs file" arriving
as a document and "Export this as xlxs" as no request at all. A format
named as the SOURCE of a conversion ("turn this CSV into an Excel
dashboard") is not a target and is left out.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import types as T

# Order matters: the first rule whose words match decides the kind.
_KIND_RULES: Tuple[Tuple[str, str, str], ...] = (
    # (kind, rule name, pattern)
    ("presentation", "deck words", r"\b(presentation|slides?|slide ?deck|deck|pitch ?deck|powerpoint|power ?point|pptx?|keynote)\b"),
    ("workbook", "spreadsheet words", r"\b(spread ?sheet|work ?book|excel|xlsx?|tracker|budget|calculator|financial model|pivot|dashboard sheet|data extract|csv|data ?set|data file|sample data|sample records|table file|synthetic data|dummy data|test data|mock data)\b"),
    ("document", "document words", r"\b(document|report|sop|standard operating procedure|memo|brief|one[- ]pager|one[- ]page|proposal|policy|letter|handout|summary|write[- ]?up|whitepaper|white paper|guide|manual|plan|pdf|docx|word)\b"),
)

#: The alias table: what a person types → the format id. Every alternative
#: is a NAME of the format (or a typo of one); "deck", "presentation" and
#: "tracker" are kind words and stay in `_KIND_RULES`. `spread sheet`,
#: `workbook` and `sheet` DO name the xlsx format here (CONTRACT-2 §5) —
#: a spreadsheet is an .xlsx — but "cheat sheet", "fact sheet", "balance
#: sheet" and "sheet 2" are not.
_ALIAS: Dict[str, str] = {
    "pdf": r"(?:pdfs?|\.pdf)",
    "docx": r"(?:docx|\.docx|word (?:document|file|doc|docs|version|copy|format)|ms[- ]word|microsoft word|in word|as word|to word|word docs?)",
    "pptx": r"(?:pptx|\.pptx|powerpoint|power ?point|powerpint|ppts?|slide ?decks?|slides)",
    "xlsx": r"(?:xlsx|\.xlsx|xlxs|xls|exel|excell?|spread ?sheets?|work ?books?|worksheets?|(?<!cheat )(?<!fact )(?<!term )(?<!style )(?<!rate )(?<!balance )(?<!time )sheets?(?!\s*\d))",
    "csv": r"(?:csvs?|\.csv|comma[- ]separated(?: values?)?|data ?sets?|data files?)",
}
#: `cvs` is a typo of csv only when the sentence is asking for a file — as
#: three letters it is also a pharmacy and a version-control system.
_CVS = r"\bcvs\b"
_CREATE_CONTEXT = re.compile(
    r"\b(?:make|create|generate|build|write|draft|prepare|produce|compile|export|save|download|"
    r"give me|send me|share|provide|deliver|i need|we need|i want|we want|turn|convert|as an?|in an?|to an?)\b"
    r"|\bcvs\s+(?:file|format|version|copy|export|of)\b",
    re.I,
)
#: A bare "word" names Word only inside a list of formats — "xlsx, word,
#: pdf and csv" — where it cannot mean anything else. Elsewhere it is a
#: word. The neighbour on either side must itself be a format name.
_OTHER_FORMAT = r"(?:pdfs?|csvs?|xlsx|xls|xlxs|excel|exel|pptx?|powerpoint|power ?point|docx)"
_LIST_SEP = r"\s*(?:,|and|or|&|/|\+|,\s*and|,\s*or)\s*(?:an?\s+|the\s+)?"
_WORD_IN_LIST = re.compile(
    rf"\b{_OTHER_FORMAT}{_LIST_SEP}(word)\b|\b(word){_LIST_SEP}{_OTHER_FORMAT}\b",
    re.I,
)

_ANY_ALIAS = "|".join(_ALIAS.values()) + "|cvs"
#: A format named as the SOURCE of a transformation is not a deliverable:
#: "turn this CSV into an Excel dashboard" is one xlsx; "from the attached
#: xlsx" names what to read, not what to make.
_SOURCE_RE = re.compile(
    rf"\b(?:turn|convert|transform|change|export|import|load|read|parse|take|use|clean(?:\s+up)?|process|reformat|summari[sz]e|analy[sz]e)\s+"
    rf"(?:(?:this|the|that|my|our|these|those|a|an)\s+)?(?:(?:attached|uploaded|pasted|existing|raw|following|above)\s+)?"
    rf"(?:{_ANY_ALIAS})\s*(?:files?|data|export|dump|table)?\s+(?:into|to|as|in)\b"
    rf"|\bfrom\s+(?:(?:a|an|the|this|that|my|our|these|those)\s+)?(?:(?:attached|uploaded|pasted|existing|raw)\s+)?(?:{_ANY_ALIAS})\b"
    rf"|\b(?:attached|uploaded|pasted)\s+(?:{_ANY_ALIAS})\b",
    re.I,
)

#: "X or Y": the person deferred the choice between two named formats.
_OR_RE = re.compile(rf"\b(?P<a>{_ANY_ALIAS})\s+or\s+(?:an?\s+|the\s+)?(?P<b>{_ANY_ALIAS})\b", re.I)

_PRINTABLE = re.compile(r"\b(printable|print|final|shareable|share with|send to|handout|read[- ]only)\b", re.I)
_EDITABLE = re.compile(r"\b(editable|edit later|so (?:i|we) can edit|track changes|word)\b", re.I)
_BRIEF = re.compile(r"\b(one[- ]pager|one[- ]page|executive brief|exec brief|brief)\b", re.I)
#: Words that ask for how the cells LOOK. A CSV cannot carry them
#: (CONTRACT-2 §1: CSV = data only), so they steer a table to xlsx and, when
#: csv was named as well, put the note on the decision.
#: `fill` is a styling word ("fill colour") unless it is the forward-fill of
#: a blank cell ("fill blank hosts from the row above"), which is data.
_STYLING = re.compile(
    r"\b(borders?|bold|highlight(?:ed|ing|s)?|colou?r(?:ed|s)?|red|amber|green|fill(?:ed)?(?!\s+(?:in|from|down|up|blanks?|the|missing|empty|any))|"
    r"fonts?|formatted|formatting|styled?|styling|conditional formatting|shad(?:ed|ing)|frozen header|freeze)\b",
    re.I,
)
_RAW = re.compile(r"\b(raw|portable|plain data|plain text|machine[- ]readable|import(?:able)? into|for import|data only)\b", re.I)
#: The request is about rows and columns: a table, a dataset, records —
#: WITHOUT the format names, so a named format never counts as evidence
#: that the material is tabular.
_TABULAR = re.compile(r"\b(tables?|tabular|rows?|columns?|records?|data|data ?sets?|data files?|sample data|entries|audit(?:s| log| rows| table| results)?|logs?|list of|listing)\b", re.I)
_PROSE = re.compile(r"\b(report|memo|brief|proposal|sop|policy|letter|summary|write[- ]?up|document|guide|manual|plan|handout|whitepaper|white paper|essay|article)\b", re.I)
_DECK = re.compile(r"\b(deck|slides?|presentation|pitch|powerpoint|power ?point|pptx?)\b", re.I)
#: A table someone hands over to be transformed: "this audit table",
#: "the rows above", "the pasted data".
_TABLE_TRANSFORM = re.compile(r"\b(this|the|these|those|attached|pasted|above|following|my|our)\s+(?:\w+\s+){0,2}?(?:tables?|rows|data|records|audit|log|list|results|entries)\b", re.I)
_DATASET = re.compile(r"\b(data ?sets?|data files?|sample data|sample records|synthetic data|dummy data|test data|mock data|(?:\d[\d,]*\s+(?:\w+\s+){0,2}?(?:records|rows|entries)))\b", re.I)
_SPREADSHEET = re.compile(r"\b(spread ?sheet|tracker|dashboard|work ?book|excel|xlsx|calculator|financial model|budget)\b", re.I)

_TEMPLATE_HINTS: Tuple[Tuple[str, str], ...] = (
    ("sop", r"\b(sop|standard operating procedure|procedure|runbook|checklist)\b"),
    ("brief", r"\b(one[- ]pager|one[- ]page|executive brief|exec brief|brief)\b"),
    ("technical_report", r"\b(technical|architecture|design doc|engineering|specification|spec\b)"),
    ("research_report", r"\b(research|literature|market study|landscape|survey of)\b"),
    ("proposal", r"\b(proposal|pitch|quote|quotation|statement of work|sow|offer)\b"),
    ("meeting_summary", r"\b(meeting|minutes|notes|standup|stand-up|call summary|transcript)\b"),
    ("executive_report", r"\b(executive|ceo|board|leadership|quarterly|annual|status report|business review)\b"),
)
_PRESENTATION_HINTS: Tuple[Tuple[str, str], ...] = (
    ("ceo", r"\b(ceo|board|executive|leadership|investor|pitch)\b"),
    ("quarterly_review", r"\b(quarterly|qbr|q[1-4]\b|annual review|business review|results)\b"),
    ("training", r"\b(training|onboarding|workshop|tutorial|lesson|course|how[- ]to)\b"),
)
_WORKBOOK_HINTS: Tuple[Tuple[str, str], ...] = (
    # A dataset is data before it is anything else: "a CSV dataset for an
    # evaluation dashboard" is the `data` template, not `dashboard`.
    ("data", r"\b(data ?sets?|data files?|sample data|sample records|synthetic data|dummy data|test data|mock data|csv|cvs|comma[- ]separated)\b"),
    ("dashboard", r"\b(dashboard|kpi|metrics|summary sheet|overview)\b"),
    ("tracker", r"\b(tracker|track|log|register|checklist|budget|plan)\b"),
    ("data", r"\b(data|extract|export|records|rows|table|list)\b"),
)

#: The formats a CSV cannot carry styling INTO, in the order the note names
#: them. `docx`/`pdf` of a workbook are the tabular document (CONTRACT-2 §1).
_STYLED_FORMATS = ("xlsx", "docx", "pdf")
_FORMAT_NAMES = {"pdf": "PDF", "docx": "Word", "pptx": "PowerPoint", "xlsx": "Excel", "csv": "CSV"}


@dataclass
class FormatDecision:
    kind: str
    formats: List[str]
    template_id: str
    #: Which rule fired, for the job metadata: "explicit: pdf" / "deck words".
    reason: str
    explicit: bool = False
    warnings: List[str] = field(default_factory=list)
    #: Set when csv was asked for together with styling words: the CSV is
    #: data only and the styling lives in the other files. The engine puts
    #: it in the completion sentence, in one clause (CONTRACT-2 §1).
    data_only_note: str = ""


def _source_spans(text: str) -> List[Tuple[int, int]]:
    return [(m.start(), m.end()) for m in _SOURCE_RE.finditer(text or "")]


def _in_spans(pos: int, spans: Sequence[Tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


def _mentions(text: str) -> List[Tuple[int, str]]:
    """(position, format) for every mention that names a deliverable — the
    alias table, `cvs` in a creation context, `word` inside a format list —
    with source mentions left out. Positions are what `explicit_formats`
    orders by."""
    text = text or ""
    spans = _source_spans(text)
    found: List[Tuple[int, str]] = []
    for fmt, pattern in _ALIAS.items():
        for m in re.finditer(rf"\b{pattern}\b", text, re.I):
            if not _in_spans(m.start(), spans):
                found.append((m.start(), fmt))
    if _CREATE_CONTEXT.search(text):
        for m in re.finditer(_CVS, text, re.I):
            if not _in_spans(m.start(), spans):
                found.append((m.start(), "csv"))
    for m in _WORD_IN_LIST.finditer(text):
        pos = m.start(1) if m.group(1) else m.start(2)
        if not _in_spans(pos, spans):
            found.append((pos, "docx"))
    found.sort()
    return found


def explicit_formats(text: str) -> List[str]:
    """Formats the person NAMED as deliverables. Order follows first
    mention; a format said twice is one entry."""
    out: List[str] = []
    for _, fmt in _mentions(text):
        if fmt not in out:
            out.append(fmt)
    return out


def kind_for(text: str, explicit: Sequence[str]) -> Tuple[str, str]:
    """(kind, rule). An explicit format implies its kind — the FIRST named
    one decides, and a PDF alone is a document; otherwise the first
    kind-rule that matches; otherwise a document.

    One exception, from CONTRACT-2 §5: a list that names a grid format
    (xlsx/csv) next to Word/PDF and is about a TABLE ("PDF and Excel of
    this audit") is a workbook — the only kind that carries all of them —
    rather than a document that drops the spreadsheet. Without table
    words, or with a prose noun ("a PDF report on AI, plus an Excel with
    the data"), the first-named format still decides and the rest is
    dropped with the warning, as before: a report is a report first."""
    if explicit:
        first = explicit[0]
        grid = [f for f in explicit if f in T.GRID_FORMATS]
        if grid and "pptx" not in explicit and first not in T.GRID_FORMATS and _TABULAR.search(text or "") and not _PROSE.search(text or ""):
            return "workbook", f"explicit: {grid[0]} (a table, {first} carried)"
        for kind, fmts in T.FORMATS_FOR_KIND.items():
            if first in fmts and (first not in ("pdf", "docx") or kind == "document"):
                return kind, f"explicit: {first}"
    if _TABLE_TRANSFORM.search(text or "") and _STYLING.search(text or "") and not _PROSE.search(text or "") and not _DECK.search(text or ""):
        # A handed-over table to be made to LOOK a certain way, with no
        # format and no document word: a spreadsheet, the only file that
        # carries the styling and stays a table.
        return "workbook", "table transform"
    for kind, rule, pattern in _KIND_RULES:
        if re.search(pattern, text or "", re.I):
            return kind, rule
    return "document", "default"


def template_for(kind: str, text: str) -> str:
    hints = {"document": _TEMPLATE_HINTS, "presentation": _PRESENTATION_HINTS, "workbook": _WORKBOOK_HINTS}[kind]
    for template_id, pattern in hints:
        if re.search(pattern, text or "", re.I):
            return template_id
    return "generic"


def _or_pair(text: str) -> Optional[Tuple[str, str]]:
    """The two formats of an "X or Y", as ids, when both are format names."""
    m = _OR_RE.search(text or "")
    if not m:
        return None
    a = explicit_formats(m.group("a"))
    b = explicit_formats(m.group("b"))
    if not a and re.fullmatch(_CVS, m.group("a"), re.I):
        a = ["csv"]
    if not b and re.fullmatch(_CVS, m.group("b"), re.I):
        b = ["csv"]
    if a and b and a[0] != b[0]:
        return a[0], b[0]
    return None


def _more_useful(a: str, b: str, text: str) -> str:
    """One of two formats the kind cannot both carry, from the task words:
    tabular material wants the workbook one (csv when the words ask for
    raw data, xlsx when they ask for styling, xlsx otherwise); deck words
    the pptx; otherwise the first named."""
    pair = (a, b)
    if _TABULAR.search(text) and not _PROSE.search(text):
        if "csv" in pair and _RAW.search(text) and not _STYLING.search(text):
            return "csv"
        for f in ("xlsx", "csv"):
            if f in pair:
                return f
    if _DECK.search(text) and "pptx" in pair:
        return "pptx"
    return a


def _apply_or(explicit: List[str], text: str) -> Tuple[List[str], str]:
    """"X or Y" (CONTRACT-2 §5): both when the kind the list resolves to
    carries both — two cheap files from the same spec — else the more
    useful one, WITHOUT the "cannot be produced" warning (the person said
    "or"; one of them is what was asked). Returns the formats and a note
    for the reason ("" when the rule did not fire)."""
    pair = _or_pair(text)
    if pair is None or not (pair[0] in explicit and pair[1] in explicit):
        return explicit, ""
    a, b = pair
    kind, _ = kind_for(text, explicit)
    if a in T.FORMATS_FOR_KIND[kind] and b in T.FORMATS_FOR_KIND[kind]:
        return explicit, f"or: both {a} and {b}"
    keep = _more_useful(a, b, text)
    drop = b if keep == a else a
    return [f for f in explicit if f != drop], f"or: {keep} over {drop}"


def _best_formats(text: str) -> Tuple[str, List[str], str]:
    """"The best format" (CONTRACT-2 §5): tabular material → csv (raw) or
    xlsx (styled), both when the words say neither; a deck → pptx + pdf;
    prose → docx + pdf."""
    if _DECK.search(text):
        return "presentation", ["pptx", "pdf"], "best format: deck"
    if _PROSE.search(text):
        return "document", ["docx", "pdf"], "best format: prose"
    if _TABULAR.search(text) or _DATASET.search(text):
        if _RAW.search(text) and not _STYLING.search(text):
            return "workbook", ["csv"], "best format: raw data"
        if _STYLING.search(text) or _SPREADSHEET.search(text):
            return "workbook", ["xlsx"], "best format: styled table"
        return "workbook", ["xlsx", "csv"], "best format: table, editable and portable"
    return "document", ["docx", "pdf"], "best format: prose"


_BEST_RE = re.compile(r"\bbest\s+(?:format|deliverable|output|file)\b", re.I)


def decide(text: str, *, explicit_only: Optional[Sequence[str]] = None) -> FormatDecision:
    """The policy, applied to one request.

    `explicit_only` lets a caller that already knows the formats (an edit
    of an existing artifact keeps its formats; a conversion names one) skip
    the text rules for the format while still classifying the template.
    The "X or Y" rule still reads the text, because the list it was handed
    is the one the intent gate found in that same text.
    """
    text = text or ""
    explicit = list(dict.fromkeys(explicit_only)) if explicit_only is not None else explicit_formats(text)
    explicit, or_note = _apply_or(explicit, text)
    kind, rule = kind_for(text, explicit)
    allowed = T.FORMATS_FOR_KIND[kind]
    warnings: List[str] = []
    note = ""

    if explicit:
        formats = [f for f in explicit if f in allowed]
        dropped = [f for f in explicit if f not in allowed]
        if dropped:
            warnings.append(f"{', '.join(dropped)} cannot be produced for a {kind}; it was not made")
        if not formats:
            formats = list(_default_formats(kind, text))
            reason = f"explicit formats unsupported for {kind}; defaulted"
        else:
            # A presentation named as pptx still gets its PDF preview file —
            # the viewer needs it and it is what "share the deck" wants.
            if kind == "presentation" and "pdf" not in formats:
                formats.append("pdf")
            reason = f"explicit: {', '.join(explicit)}"
            if "csv" in formats and _STYLING.search(text):
                # CSV = data only. The styling asked for goes into the
                # styled files; when none was named, the Excel file is
                # added so it has somewhere to go.
                styled = [f for f in formats if f in _STYLED_FORMATS]
                if not styled:
                    formats.append("xlsx")
                    styled = ["xlsx"]
                    reason += "; +xlsx for the styling"
                names = " and ".join(_FORMAT_NAMES[f] for f in styled)
                note = f"the CSV carries the data only; the formatting is in the {names} file{'s' if len(styled) > 1 else ''}"
        if or_note:
            reason += f" ({or_note})"
        return FormatDecision(kind, formats, template_for(kind, text), reason, explicit=True, warnings=warnings, data_only_note=note)

    if _BEST_RE.search(text):
        kind, formats, reason = _best_formats(text)
        return FormatDecision(kind, formats, template_for(kind, text), reason, explicit=False, warnings=warnings)

    formats = list(_default_formats(kind, text))
    if kind == "workbook" and formats == ["csv"]:
        rule = "dataset words"
    return FormatDecision(kind, formats, template_for(kind, text), rule, explicit=False, warnings=warnings)


def _default_formats(kind: str, text: str) -> Sequence[str]:
    if kind == "presentation":
        return ("pptx", "pdf")
    if kind == "workbook":
        # A dataset is a portable data file (CONTRACT-2 §1); a spreadsheet,
        # tracker or dashboard is the editable one; a table someone wants
        # styled is the editable one too. A dataset asked for WITH styling
        # words is a spreadsheet: the styling has to live somewhere.
        if _DATASET.search(text) and not _SPREADSHEET.search(text) and not _STYLING.search(text):
            return ("csv",)
        return ("xlsx",)
    # document
    if _BRIEF.search(text):
        return ("pdf", "docx")
    if _PRINTABLE.search(text) and not _EDITABLE.search(text):
        return ("pdf",)
    if _EDITABLE.search(text) and not _PRINTABLE.search(text):
        return ("docx", "pdf")
    return ("docx", "pdf")


def formats_for_conversion(kind: str, requested: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(formats we will make, formats we refuse) for "convert it to X"."""
    allowed = T.FORMATS_FOR_KIND[kind]
    ok = [f for f in requested if f in allowed]
    bad = [f for f in requested if f not in allowed]
    return ok, bad
