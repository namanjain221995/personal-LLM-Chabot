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

WHAT IS NOT MADE IS SAID (2026-09-16). The drop list could only ever see
formats the alias table already recognised, so "an xlsx, a csv, a Word file
and a LaTeX file" made three files and never mentioned LaTeX, and "as JSON"
and "as an .epub" came back as a Word file and a PDF with nothing said.
`unmakeable_formats` reads the format-shaped words this platform does NOT
make and `decide_base` warns on them — and a KIND the person named that a
format word overruled ("make a presentation as an xlsx" → a spreadsheet)
is warned about too: substituting the kind is a bigger change than dropping
a format, and it was the one made in silence.

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

#: Data asked for by the shape or by the count: "sample records", "250
#: realistic sample rows of support tickets". `_DATASET` below is compiled
#: from this one string, so the KIND and the csv default are decided by the
#: same words — until 2026-09-16 only the default read it, and by then the
#: kind had defaulted to document (MEASURE 1 B29: 250 rows of data were
#: delivered as a Word file and a PDF).
_DATASET_WORDS = (
    r"\b(data ?sets?|data files?|sample data|sample records|synthetic data|dummy data|test data|mock data|"
    r"(?:\d[\d,]*\s+(?:\w+\s+){0,2}?(?:records|rows|entries)))\b"
)

# Order matters: the first rule whose words match decides the kind.
_KIND_RULES: Tuple[Tuple[str, str, str], ...] = (
    # (kind, rule name, pattern)
    ("presentation", "deck words", r"\b(presentation|slides?|slide ?deck|deck|pitch ?deck|powerpoint|power ?point|pptx?|keynote)\b"),
    ("workbook", "spreadsheet words", r"\b(spread ?sheet|work ?book|excel|xlsx?|tracker|budget|calculator|financial model|pivot|dashboard sheet|data extract|csv|data ?set|data file|sample data|sample records|table file|synthetic data|dummy data|test data|mock data)\b"),
    ("document", "document words", r"\b(document|report|sop|standard operating procedure|memo|brief|one[- ]pager|one[- ]page|proposal|policy|letter|handout|summary|write[- ]?up|whitepaper|white paper|guide|manual|plan|pdf|docx|word)\b"),
    # Counted rows, LAST: after the document rule, so "write a report on the
    # 250 rows we logged" is still a report, and "generate 250 realistic
    # sample rows of support tickets" — which names no other kind — is the
    # data file the person asked for.
    ("workbook", "dataset words", _DATASET_WORDS),
)

#: The alias table: what a person types → the format id. Every alternative
#: is a NAME of the format (or a typo of one); "deck", "presentation" and
#: "tracker" are kind words and stay in `_KIND_RULES`. `spread sheet`,
#: `workbook` and `sheet` DO name the xlsx format here (CONTRACT-2 §5) —
#: a spreadsheet is an .xlsx — but "cheat sheet", "fact sheet", "balance
#: sheet" and "sheet 2" are not.
_ALIAS: Dict[str, str] = {
    "pdf": r"(?:pdfs?|\.pdf)",
    # The normaliser writes a destination BEFORE its postposition ("वर्ड में
    # कन्वर्ट कर दो", "aane word ma convert karo" -> "word _in_ _convert_"),
    # so no English-order alias could see it and all four "convert it to
    # Word" cases lost their format and exported the previous ANSWER instead
    # (measure2 harness, 2026-09-16; pdf/excel/ppt pass because they
    # self-name). `_convert_` and `_give_` are emitted only by
    # lexicon.normalize from Indic or romanised input, so requiring one of
    # them keeps a bare English "the word me is a pronoun" — which the
    # postposition rule also rewrites to "word _in_" — out of this.
    "docx": r"(?:docx|\.docx|word (?:document|file|doc|docs|version|copy|format)|ms[- ]word|microsoft word|in word|as word|to word|word docs?|"
            r"word _in_(?:\s+\S+){0,2}?\s+(?:_convert_|_give_)|word (?:_convert_|_give_))",
    # `deck` names the pptx only where a FORMAT belongs — "a deck version",
    # "as a deck", "in deck format". Measured 2026-09-16: "create a pdf on
    # the security review" → "and a deck version" found no explicit format,
    # so the conversion never ran, while intent.py's own `_FORMAT_WORD` and
    # `_ARTIFACT_NOUNS` both held the word. A bare `deck` stays a KIND word
    # (`_KIND_RULES`): in "give me a Word version of the deck" and "I need a
    # deck for Monday" it is the source and the thing itself, not a target.
    "pptx": r"(?:pptx|\.pptx|powerpoint|power ?point|powerpint|ppts?|slide ?decks?|slides"
            r"|(?:pitch ?)?decks?\s+(?:version|copy|format|file)|(?<=as a )deck|(?<=as an )deck|(?<=into a )deck|(?<=to a )deck|(?<=in )deck(?= format))",
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

#: FORMATS THIS PLATFORM DOES NOT MAKE, by the names people type. The table
#: exists so a request for one can be REFUSED by name. Measured 2026-09-16:
#: "as an xlsx, a csv, a Word file and a LaTeX file" made three files and
#: never mentioned LaTeX, "Export this conversation as JSON" and "the
#: proposal as an .epub" made a Word file and a PDF with no clause about the
#: format that was asked for, and "a Google Slides file" made a SECOND deck
#: because `Slides` matched the pptx alias. Two groups: a name that can only
#: be a file format is read anywhere; a name that is an ordinary word too
#: ("numbers", "pages", "text") is read only where a format belongs.
_UNMAKEABLE_ANYWHERE: Tuple[Tuple[str, str], ...] = (
    # `doc` normalises to `docx` before this runs (lexicon.normalize), so
    # "an editable Google Doc" arrives as "google docx".
    ("Google Docs", r"google\s+docs?x?(?:\s+(?:file|document))?|google\s+documents?"),
    ("Google Sheets", r"google\s+sheets?"),
    ("Google Slides", r"google\s+slides?|g[- ]?slides?"),
    ("Keynote", r"keynote"),
    ("Figma", r"figma"),
    ("LaTeX", r"latex|\.tex\b"),
    ("EPUB", r"epub|\.epub\b"),
    ("RTF", r"rtf|\.rtf\b"),
    ("ODT", r"odt|\.odt\b|open ?document text"),
    ("InDesign", r"indesign|\.indd\b"),
    ("Photoshop", r"photoshop|\.psd\b"),
    # `numbers` and `pages` are ordinary words ("as numbers", "in two
    # pages"), so only their file forms are read at all.
    ("Numbers", r"\.numbers\b|numbers\s+(?:file|document|spreadsheet)|apple\s+numbers"),
    ("Pages", r"\.pages\b|pages\s+(?:file|document)|apple\s+pages"),
)
#: The same, but only where the sentence puts a format: after "as/in/into/to"
#: (with or without an article), or in front of "file/document/version/
#: format", or written as an extension.
_UNMAKEABLE_IN_PLACE: Tuple[Tuple[str, str], ...] = (
    ("JSON", r"json"),
    ("HTML", r"html|htm"),
    ("XML", r"xml"),
    ("Markdown", r"markdown|md"),
    # `text` on its own is left out: "summarise it as text" asks for a chat
    # answer, not a file this platform refuses to make.
    ("plain text", r"txt|plain text"),
    ("YAML", r"yaml|yml"),
)
_UNMAKEABLE_PLACE = (
    r"(?:\b(?:as|in|into|to)\s+(?:an?\s+|the\s+)?(?:editable\s+|plain\s+|simple\s+|raw\s+)?(?:%(p)s)\b"
    r"|\b(?:%(p)s)\s+(?:files?|documents?|versions?|format|formats)\b"
    r"|(?<![\w.])\.(?:%(p)s)\b)"
)
_UNMAKEABLE_RES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = tuple(
    [(name, re.compile(rf"\b(?:{pat})\b", re.I)) for name, pat in _UNMAKEABLE_ANYWHERE]
    + [(name, re.compile(_UNMAKEABLE_PLACE % {"p": pat}, re.I)) for name, pat in _UNMAKEABLE_IN_PLACE]
)
#: The chart-image formats, named by a person. They are real formats (types
#: .FORMATS), but only for the kinds that carry them, so they are read here
#: for the CONVERSION refusal and nowhere else: the deliverable rules own
#: `_chart_image_formats` below.
_IMAGE_NAME_RE = re.compile(r"\b(?:pngs?|\.png|svgs?|\.svg)\b", re.I)


def unmakeable_formats(text: str) -> List[Tuple[str, int, int]]:
    """(display name, start, end) for every format-shaped word the request
    names that this platform does not make. First mention wins; the spans
    let `_mentions` drop an alias hit that lives inside one ("Slides" inside
    "Google Slides")."""
    out: List[Tuple[str, int, int]] = []
    seen = set()
    for name, rx in _UNMAKEABLE_RES:
        m = rx.search(text or "")
        if m and name not in seen:
            seen.add(name)
            out.append((name, m.start(), m.end()))
    out.sort(key=lambda t: t[1])
    return out


def unmakeable_names(text: str) -> List[str]:
    """Just the names, in the order they were said."""
    return [n for n, _s, _e in unmakeable_formats(text)]


def _without_unmakeable(text: str) -> str:
    """`text` with every unmakeable format name blanked, for the rules that
    read what KIND of artifact the words describe."""
    out = text or ""
    for _name, start, end in reversed(unmakeable_formats(out)):
        out = out[:start] + " " * (end - start) + out[end:]
    return out


def named_image_formats(text: str) -> List[str]:
    """The chart-image formats the words name ("convert it to SVG")."""
    return list(dict.fromkeys(m.group(0).lower().lstrip(".").rstrip("s") for m in _IMAGE_NAME_RE.finditer(text or "")))


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
_DATASET = re.compile(_DATASET_WORDS, re.I)
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
    # A format name INSIDE the name of a format we do not make is not a
    # deliverable: "a Google Slides file" matched the pptx alias on `Slides`
    # and produced a second deck (measured 2026-09-16, A7).
    spans = _source_spans(text) + [(s, e) for _n, s, e in unmakeable_formats(text)]
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
    mention; a format said twice is one entry.

    AS3: the text is read through `lexicon.normalize` first, so "dox",
    "in docs", "पीडीएफ", "એક્સેલમાં" and "exel" name their formats here too —
    the engine's own call on the raw instruction agrees with the gate's."""
    from . import lexicon

    text = lexicon.normalize(text or "")
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


#: The kind word for a sentence, and the words people use for each kind.
_KIND_WORDS = {"document": "document", "presentation": "presentation", "workbook": "workbook"}
_A_KIND = {"document": "a document", "presentation": "a presentation", "workbook": "an Excel workbook"}


#: The words that name a KIND of artifact and are NOT also the name of a
#: format. "A PDF report plus an Excel with the data" names a document by
#: `report` and no workbook at all — the format names have to stay out of
#: this table or every mixed list would look like a substituted kind.
_KIND_ONLY_RULES: Tuple[Tuple[str, str, str], ...] = (
    ("presentation", "deck words", r"\b(presentation|pitch ?deck|slide ?deck)\b"),
    ("workbook", "spreadsheet words", r"\b(spread ?sheet|work ?book|tracker|budget|calculator|financial model|data ?set|sample data|sample records)\b"),
    ("document", "document words", r"\b(document|report|sop|standard operating procedure|memo|brief|one[- ]pager|proposal|policy|letter|handout|whitepaper|white paper|guide|manual)\b"),
)


def _kind_from_words(text: str) -> Tuple[str, str]:
    """The kind the request's WORDS name, read without any format name —
    the reading `kind_for` skips whenever a format was said. ("", "") when
    the words name no kind of their own."""
    for kind, rule, pattern in _KIND_ONLY_RULES:
        if re.search(pattern, text or "", re.I):
            return kind, rule
    return "", ""


def _and_list(words: Sequence[str]) -> str:
    items = [str(w) for w in words if str(w).strip()]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def _a_format(fmt: str) -> str:
    name = _FORMAT_NAMES.get(fmt, fmt.upper())
    if fmt in ("docx", "xlsx", "pptx"):
        name += " file"
    return f"an {name}" if name[0] in "AEIOUX" else f"a {name}"


def _a_kind(kind: str) -> str:
    return _A_KIND.get(kind, f"a {kind}")


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


# --- AS3 integration BEGIN: standalone chart images ---
#: "a bar chart ... as a png", "pie chart svg me": a chart image named next
#: to chart words. A bare "png" ("draw a logo png") is not a chart file.
#: The tier-2 names added on 2026-09-16 reach this list too: without them
#: "visualise this table as a treemap" and "show this as a sunburst" matched
#: no chart word, fell through to the document default and were answered
#: with a Word file and a PDF — the exact answer this programme exists to
#: stop. Only the names that are unambiguous chart nouns are listed bare;
#: "pareto", "violin" and "bullet" are ordinary words ("the Pareto
#: principle", "a slide about the violin", "bullet points"), and their chart
#: forms already match through "chart", "graph" or "plot".
_CHART_WORDS_RE = re.compile(
    r"\b(?:chart|charts|graph|graphs|plot|plots|histogram|heat\s*map|pie|donut|scatter|gantt|funnel|box\s*plot"
    r"|tree\s*map|sunburst|candlestick|ohlc|pareto\s*(?:diagram|analysis))\b"
    r"|चार्ट|ग्राफ|ચાર્ટ|ગ્રાફ",
    re.I,
)
_IMAGE_FORMAT_RE = re.compile(r"\b(?:png|\.png|svg|\.svg)\b", re.I)


#: The verbs a chart request is made with. "visualise this table on pie
#: chart", "plot it", "draw a bar graph", "isko pie chart me dikhao".
_CHART_ASK_RE = re.compile(
    r"\b(?:visuali[sz]e|visuali[sz]ing|plot|plotted|graph|chart|draw|render|show|display|give|make|create|build|generate)\b"
    r"|दिखा|बना|दिखाओ|बनाओ|બતાવ|બનાવ",
    re.I,
)


def _chart_image_formats(text: str, *, chart_request: Optional[bool] = None) -> List[str]:
    """The image formats a chart request names — and PNG when it names none.

    A person who asks to "visualise this table on pie chart" has named the
    deliverable: a chart. Until 2026-09-16 this returned [] unless the text
    also said "png" or "svg", so the request fell through to the document
    default and production answered a one-line chart ask with a two-page Word
    file and a PDF. The image default applies only when nothing else in the
    text names a deliverable (kind_for stays at its "default" rule) — "a
    report with a pie chart" is still a report, and an explicit format still
    decides.

    `chart_request` is the INTENT GATE's verdict (intent.ArtifactIntent.
    chart_request), and when the caller has one it replaces the two regexes
    below. The gate reads lexicon-normalised text — typos, Hindi, Gujarati,
    Hinglish and Gujlish folded to the rule vocabulary, negated clauses
    blanked — and its chart vocabulary is the larger one: measured on this
    branch, "give me a waterfall showing revenue by quarter" and "scatter of
    Salary vs Experience" were chart_request=True at the gate and docx+pdf
    here, because `waterfall` is not in _CHART_WORDS_RE and neither phrase
    has a verb _CHART_ASK_RE knows. Everything after the verdict — a named
    image format, an explicit format, another deliverable noun — still reads
    the text, so "a report with a pie chart" stays a report.
    """
    words = bool(_CHART_WORDS_RE.search(text or ""))
    asked = words if chart_request is None else bool(chart_request)
    if not (words or asked):
        return []
    found = [m.group(0).lower().lstrip(".") for m in _IMAGE_FORMAT_RE.finditer(text or "")]
    images = list(dict.fromkeys(f for f in found if f in T.IMAGE_FORMATS))
    if images:
        return images
    if explicit_formats(text or ""):
        return []
    if kind_for(text or "", [])[1] != "default":
        return []
    if not asked or (chart_request is None and not _CHART_ASK_RE.search(text or "")):
        return []
    return ["png"]


def decide(text: str, *, explicit_only: Optional[Sequence[str]] = None,
           chart_request: Optional[bool] = None) -> FormatDecision:
    """decide_base (below), plus the chart image formats a request names:
    alone ("pie chart as png") they are the only files; next to a document
    format or a document word they are companions. A CSV asked for with
    styling the styling parser can read ("with a blue header row") also gets
    the Excel file, as the styling words below already do.

    `chart_request` is the intent gate's verdict; None (the default) leaves
    the chart decision to this module's own words."""
    return _with_styled_csv(text, _decide_images(text, explicit_only=explicit_only, chart_request=chart_request))


def _with_styled_csv(text: str, d: FormatDecision) -> FormatDecision:
    if d.kind != "workbook" or "csv" not in d.formats or any(f in d.formats for f in _STYLED_FORMATS):
        return d
    try:
        from . import style as _style

        patch, _unparsed = _style.parse_style_request(text or "", "workbook")
        styled = not patch.is_empty()
    except Exception:  # noqa: BLE001 — the word rules stand
        styled = False
    if styled:
        d.formats = list(d.formats) + ["xlsx"]
        d.reason += "; +xlsx for the styling"
        d.data_only_note = "the CSV carries the data only; the formatting is in the Excel file"
    return d


def _decide_images(text: str, *, explicit_only: Optional[Sequence[str]] = None,
                   chart_request: Optional[bool] = None) -> FormatDecision:
    images = [f for f in _chart_image_formats(text, chart_request=chart_request) if not explicit_only or f not in explicit_only]
    if not images:
        return decide_base(text, explicit_only=explicit_only)
    rest = [f for f in (explicit_only or []) if f not in T.IMAGE_FORMATS]
    named = explicit_formats(text) if explicit_only is None else rest
    if not named and kind_for(text, [])[1] != "default":
        # "a word report with a line chart, plus the chart as png": the
        # document is asked for too, in its default formats.
        base = decide_base(text)
        base.formats = list(base.formats) + [f for f in images if f in T.FORMATS_FOR_KIND[base.kind] and f not in base.formats]
        base.reason += f"; +{', '.join(images)} (chart image)"
        return base
    if not named:
        kind, _rule = kind_for(text, [])
        allowed = [f for f in images if f in T.FORMATS_FOR_KIND[kind]]
        if not allowed:
            kind, allowed = "document", [f for f in images if f in T.FORMATS_FOR_KIND["document"]]
        return FormatDecision(kind, allowed, template_for(kind, text), f"explicit: {', '.join(allowed)} (chart image)", explicit=True)
    base = decide_base(text, explicit_only=named)
    extra = [f for f in images if f in T.FORMATS_FOR_KIND[base.kind] and f not in base.formats]
    base.formats = list(base.formats) + extra
    if extra:
        base.reason += f"; +{', '.join(extra)} (chart image)"
    return base
# --- AS3 integration END ---


def decide_base(text: str, *, explicit_only: Optional[Sequence[str]] = None) -> FormatDecision:
    """The policy, applied to one request.

    `explicit_only` lets a caller that already knows the formats (an edit
    of an existing artifact keeps its formats; a conversion names one) skip
    the text rules for the format while still classifying the template.
    The "X or Y" rule still reads the text, because the list it was handed
    is the one the intent gate found in that same text.
    """
    from . import lexicon

    text = text or ""
    # The kind and the formats are read from the SAME words. `explicit_formats`
    # has read the text through `lexicon.normalize` since AS3, while kind_for
    # and template_for got the raw string — so the two halves of one decision
    # disagreed whenever the normaliser was what recognised the deck: "need a
    # presentaion on cyber security" and "एक प्रेजेंटेशन बनाओ" both came back as a
    # Word file and a PDF although the intent gate had read them correctly
    # (MEASURE 1 B18/B24, 2026-09-16; kind_for(normalize(text)) is
    # ("presentation", "deck words") for both). Measured cost of the extra
    # normalise: 126 us on a 98-character instruction, inside a decide() call
    # that then takes 293 us in total — once per artifact turn, not per token.
    norm = lexicon.normalize(text)
    explicit = list(dict.fromkeys(explicit_only)) if explicit_only is not None else explicit_formats(text)
    explicit, or_note = _apply_or(explicit, text)
    # The name of a format we do not make says nothing about the KIND
    # either: "convert it to a Google Slides file" was read as deck words
    # and produced a second .pptx (measured 2026-09-16, A7).
    kind, rule = kind_for(_without_unmakeable(norm), explicit)
    allowed = T.FORMATS_FOR_KIND[kind]
    warnings: List[str] = []
    note = ""
    # A format the person NAMED that this platform does not make is said
    # plainly, the way a format we DO make but cannot carry already is
    # ("xlsx cannot be produced for a presentation"). Until 2026-09-16 the
    # drop list could only see formats `explicit_formats` recognised, so
    # LaTeX, JSON and EPUB vanished from the request without a word.
    unmakeable = unmakeable_names(text)
    if unmakeable:
        warnings.append(f"I don't make {_and_list(unmakeable)} files")
    # A KIND the person named, overruled by a format word. `kind_for` lets
    # the first explicit format pick the kind, so "make a presentation as an
    # xlsx" returned a workbook with no warning at all — a bigger change
    # than any dropped format, made silently (measured 2026-09-16, G6).
    if explicit and explicit_only is None:
        said_kind, _said_rule = _kind_from_words(text)
        if said_kind and said_kind != kind:
            warnings.append(
                f"a {_KIND_WORDS.get(said_kind, said_kind)} cannot be {_a_format(explicit[0])}; "
                f"I made {_a_kind(kind)} of the same content instead"
            )

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
        return FormatDecision(kind, formats, template_for(kind, norm), reason, explicit=True, warnings=warnings, data_only_note=note)

    if _BEST_RE.search(text):
        kind, formats, reason = _best_formats(text)
        return FormatDecision(kind, formats, template_for(kind, norm), reason, explicit=False, warnings=warnings)

    formats = list(_default_formats(kind, text))
    if kind == "workbook" and formats == ["csv"]:
        rule = "dataset words"
    return FormatDecision(kind, formats, template_for(kind, norm), rule, explicit=False, warnings=warnings)


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
