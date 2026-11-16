"""The charts code draws from record results (the charts of 2026-11-03; new code, no notebook function behind it). Used
only with ``SFA_CHARTS=on``, the default (:func:`sf_assistant.config.charts_settings`); with ``off`` nothing here runs
and every answer, model request and listing is as before. The page imports this module at start-up even with the switch
off (``web/pipeline.py`` ``preload_package``), so an import holds constants only and reads only its two prompt files.
The record modules are imported inside the functions that read them: a records-off run with the switch on imports this
module on every answer, and it still loads no engine module and no duckdb.

Code decides everything but the data: whether a question asks for a chart (:func:`chart_ask`), which result is drawn,
the chart type and the title. The model fetches the data as it does today, and code reads those results after it
finishes, so no model text can reach a chart and this code adds no model step of its own; a NO_RECORDS route it
overrules runs the record agent as well (docs/runbook.md, Limits). The one thing the model gets is one note on a
chart question (:data:`RECORDS_CHART_NOTE`, :func:`note_for`); ``RecordSession.ask`` appends it to the record agent's
system prompt, never inside ``render_records_system``, which the MCP template answer_from_records also serves. A
question that names nothing to chart ("now plot that") gets one line and no model call (:func:`bare_line`).

Which result (:func:`_pick`): the last chartable result of the answer. A chartable result is a successful data call of
group_records, trend_records, compare_records or count_records with a breakdown that gives a chart (:func:`_build`); a
breakdown gives one item per field with 2 or more values. More than one chartable result: the chart says so
(``several``). No chartable result: no chart, and one plain line says why, the reason of the last data result.
get_field_values and get_field_history are never drawn, because they ignore filters and periods. about_people and
find_references are never drawn and give no rows.

Which type (:func:`_choose_type`): the question's named type when the shape can carry it, else the type of the shape
and a note. A pie or donut only when the rows are parts of one whole (:func:`_pie_fits`). Numbers come only from the
result's own rows and keys (``QueryEvidence.model_result``, the dict the model received): code makes no total, no
percent and no "Other". Null is never drawn as 0, with one exception: on a line per series under count, a series a
bucket lacks is 0, because absent there means no records and the add-up test proves its split statement was not cut.

Words on a chart (``prompts/records_chart_words.md``, :data:`_WORDS`): catalog labels, dates and the result's numbers
in the title, subtitle and notes, and a compare's partial note also names its cleaned labels; a filter is named by its
field's label, never by its value or raw text, and a text that does not start with a known field reads ``another field``
(:func:`_subtitle`). Every row label, series name and compare label goes through :class:`_Cleaner` (what XML forbids,
then secrets, the contact rule and a 200-character cap), and a compare label holding a digit run neither the question
nor its segment holds is replaced (:func:`_compare_label`).

The renderer (:func:`_draw`): pure Python, one SVG drawn once, 640 px wide, with its title, subtitle and notes as text
nodes. Every mark carries its light colour as a plain attribute, and the inner style only adds dark mode, so a browser
that blocks the style still draws a readable chart. :func:`_check_svg` parses every SVG and checks its tags and
attributes against fixed lists; a failure gives no picture, the rows and the ``fault`` reason. Nothing is written to a
file.

:func:`make` (the answer path) and :func:`for_tool` (MCP ``chart: true``) share the pure core :func:`_build`, which
takes ``tool, result, arguments, object, clean, labels, asked type, today``. Neither of them raises: a fault gives the
reason line and leaves the answer whole.

:func:`drop_talk` removes what the model says about a chart from a record answer: only code talks about whether a chart
exists. It runs on chart questions only, and never on what code wrote (:func:`_code_wrote`).
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import math
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from xml.sax.saxutils import escape

from sf_assistant import prompts as _prompt_files   # the note and the words of a chart (prompts/records_chart_*.md)
from sf_assistant.config import charts_settings

__all__ = ["ChartAsk", "chart_ask", "drawable_ask", "note_for", "bare_line", "drop_talk", "make", "for_tool",
           "for_jsonl", "text_lines", "with_chart_arg", "CHART_ARG_TOOLS", "OVERRULE_REASON", "RECORDS_CHART_NOTE",
           "RECORDS_CHART_WORDS"]

#: The record agent's note on a chart question, appended to its system prompt (never inside ``render_records_system``,
#: which the MCP template answer_from_records also serves).
RECORDS_CHART_NOTE = _prompt_files.text("records_chart_note")
RECORDS_CHART_WORDS = _prompt_files.text("records_chart_words")   # raw text; _WORDS is its dict
_WORDS = dict(ln.split(": ", 1) for ln in RECORDS_CHART_WORDS.split("\n") if ": " in ln)
#: Why ``RecordSession.triage`` overruled a NO_RECORDS route: a chart question is a data question.
OVERRULE_REASON = _WORDS["overrule"]
#: The tools an MCP client can ask a chart from, with ``chart: true``.
CHART_ARG_TOOLS = ("group_records", "trend_records", "compare_records")
_CHART_ARG = {"type": "boolean", "description": "true: also return a chart of this result (spec, rows, notes, svg)."}
_CHART_TOOLS = (*CHART_ARG_TOOLS, "count_records")
#: Every other data tool's result gives no chart, and this word says why.
_OTHER_WORDS = {"about_people": "people", "find_references": "people", "aggregate_records": "one_number",
                "percentage_of_records": "one_share", "list_records": "list", "search_records": "list",
                "get_field_values": "field_values", "get_field_history": "field_values"}
_MEASURES = ("count", "count_distinct", "sum", "avg", "min", "max", "percent")
#: A question that asks for a rate, share or percentage (placement fixes, 2026-11-16): counts over time are never
#: drawn in place of a rate per period (:func:`_rate_results`). A "%" right after a digit ("100% done") and a money
#: rate ("bill rate", "bill-rate") ask for no rate.
_RATE_ASK = re.compile(r"\b(?:percent(?:age)?s?|share|ratio)\b|(?<![\d.])%"
                       r"|(?<!\bbill[ -])(?<!\bbilling[ -])(?<!\bpay[ -])(?<!\bhourly[ -])(?<!\bdaily[ -])"
                       r"(?<!\binterest[ -])(?<!\bexchange[ -])(?<!\btax[ -])(?<!\bsalary[ -])(?<!\bclient[ -])"
                       r"(?<!\bvendor[ -])\brates?\b", re.I)
#: The results a rate question never draws: counts over time (review of 2026-11-16: a share by group is drawn as a
#: pie, and an average, sum, min or max of a field, a rate field among them, is drawn as asked).
_RATE_REFUSED = (("trend_records", "compare_records"), ("count", "count_distinct"))
_GRAINS = ("day", "week", "month", "quarter", "year")
#: The caps of a chart: bars, line points, slices and series.
MAX_BARS, MAX_POINTS, MAX_SLICES, MAX_SERIES = 30, 120, 8, 8
_KEEP_LABEL = 200   # as _cell in tools/record_base.py; the display cut comes after every cleaner
_CUT_ROW, _CUT_LEGEND, _CUT_TITLE = 28, 22, 70
#: The labels the tools write for rows that are not a value of the field.
_SPECIAL_LABELS = ("(blank)", "(unresolved id)")
_RESERVED_KEYS = frozenset({"__proto__"})   # never a key of a row: a page's script may copy the row


# ------------------------------------------------------------------------------------------------ the ask
_TYPES = r"bar|bars|pie|line|lines|column|donut|doughnut|area|scatter|stacked|funnel|histogram|trend|distribution"
#: After a type word typed with "chat" or "char" (a chat, and "char" is four in Hinglish): what makes it a chart.
_AFTER_TYPO = r"(?=\s*(?:$|[,.;:!?]|of\b|for\b|by\b|me\b|mein\b|ka\b|ki\b|ke\b|banao\b|bana\b|dikhao\b|dikha\b|please\b))"
_FOLD = [(re.compile(p), r) for p, r in [
    # "ek bar", "is bar", "har bar" are Hinglish for once, this time, every time ("is bar chart" stays a chart)
    (r"\b(ek|is|us|har|pichli|pichhli|agli|kitni|kitne|kai|teen|dusri|doosri|pehli|pahli|aakhri|akhri|aur)\s+bar\b"
     r"(?!\s+(?:charts?|graphs?|plots?)\b)", r"\1 baar"),
    (r"\b(?:chrat|chartt|chrt|chatr|charrt|cahrt|chaart|chrats)\b", "chart"),
    (r"\b(?:grafh|garaf|garph|grpah|grph|grahp|grapgh|graphh)\b", "graph"),
    (r"\bplto\b", "plot"),
    # also surnames ("did Polt attend"): a typo only right before a word that draws
    (r"\b(?:graf|graaf)(?=\s+(?:dikhao|dikha|banao|bana|bnao|draw)\b)", "graph"),
    (r"\b(?:polt|plott)(?=\s+(?:dikhao|dikha|banao|bana|bnao|karo|kar)\b)", "plot"),
    (r"\b(histogarm|histgram|histogrm|histagram|histrogram|hisogram)\b", "histogram"),
    (r"\b(scater|scattter|sactter)\b", "scatter"),
    (rf"\b({_TYPES})\s+chat\b{_AFTER_TYPO}", r"\1 chart"),
    (rf"\b(bar|pie|line)\s+char\b{_AFTER_TYPO}", r"\1 chart"),
    (r"\b(bar|pie|line)\s+diagram\b", r"\1 chart"),
    (r"\b(bar|pie|line|area|column|donut|doughnut|flow|org)(chart|graph|plot)(s?)\b", r"\1 \2\3"),
]]
_NOT_A_CHART = re.compile(
    r"(?<!cash )(?<!fund )(?<!funds )(?<!money )(?<!capital )(?<!net )(?<!free )"
    r"\b(?:flow|work\s*flow|data\s*flow|process(?:\s+flow)?|org|organi[sz]ation(?:al)?|sequence|swim\s*lane|hierarchy|architecture)[\s-]*(?:charts?|graphs?)\b"
    r"|\b(?:flowcharts?|orgcharts?)\b"
    r"|(?<!bar )(?<!pie )(?<!line )(?<!column )(?<!donut )(?<!doughnut )(?<!area )(?<!stacked )"
    r"\bcharts?\s+of\s+accounts?\b(?!\s+(?:by|per|with|ka|ke|ki|\w+\s*wise)\b)"
    r"|\b(?:report|dashboard|list\s*view)[\s-]+charts?\b(?!\s+of\b)"
    r"|\bcharts?\s+(?:views?|themes?)\b(?!\s+of\b)"
    r"|\bgraph\s*(?:ql|api|db|database|theory|engine|explorer)\b"
    r"|\b(?:knowledge|data|dependency|call|microsoft|ms|facebook|open|composite)\s+graphs?\b"
    r"|\bplots?\s*(?:no|num|number)\b\.?(?!\s*of\b)|\bplots?[\s-]*#?[\s-]*(?:[a-z]-)?\d+[a-z]?\b(?!\s+(?:months?|weeks?|years?|days?))"
    r"|\bplot\s+(?:twist|line)\b|\bplot\s+of\s+(?:the\s+|this\s+)?(?:book|film|movie|story|novel|show|land)\b|\b(?:sub|story)plots?\b"
    r"|\bgraphical\s+designers?\b|\bjava\s+tree\s*maps?\b"
)
_CHART_WORD = r"(?:charts?|graphs?|plots?)"
_ANY_CHART = r"(?:charts?|graphs?|plots?|histograms?|heat\s*maps?|tree\s*maps?|sunbursts?|gantt)"
_ABOUT = re.compile(
    r"\bwhat(?:'s|\s+is|\s+are|\s+does|\s+do)\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,2}?" + _ANY_CHART + r"(?:\s+(?:mean|means|used\s+for))?\W*$"
    r"|\b" + _ANY_CHART + r"\s+(?:kya|kab|ka\s+matlab|ka\s+mtlb)\b"
    r"|\b(?:how\s+(?:do|can|to|should)\s+(?:i\s+|we\s+|you\s+)?|can\s+(?:i|we)\s+)(?:add|create|make|build|configure|embed|set\s*up)\b.*\b(?:charts?|graphs?)\b.*\b(?:in|to|on|into)\s+(?:salesforce|lightning|(?:an?\s+|the\s+|my\s+|our\s+)?(?:\w+\s+){0,2}?(?:report|dashboard|list\s*view|page|layout))"
    r"|\bwhen\s+(?:(?:should|do|can|would)\s+(?:i|we|you|one)|to)\s+use\b.*\b" + _CHART_WORD + r"\b"
    r"|\bdifference\s+between\s+(?:an?\s+|the\s+)?(?:\w+\s+)?" + _CHART_WORD + r"\b"
    r"|\bwhich\s+" + _CHART_WORD + r"\s+(?:is|are|should|would|works?|fits?|suits?)\b"
    r"|\b(?:how\s+(?:do|can|to|should)\s+(?:i\s+|we\s+|you\s+)?read|define)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?" + _CHART_WORD + r"\b"
    r"|\bwhat(?:'s|\s+is|\s+are)\s+(?:an?\s+|the\s+)?funnels?\s+(?:of|by)\b"
)
#: The words that can stand between a refusal and its chart word: any other word is about something else ("no table just
#: chart", "not submitted candidates graph", "without visa chart"). A type word belongs ("hide the pie chart").
_REFUSAL_GAP = (r"(?:(?:a|an|the|any|this|that|need|want|show|use|make|me|it|columns|horizontal|vertical|" + _TYPES
                + r")\s+){0,2}?")
_STOP = re.compile(
    r"(?:^|[,.;:!?]\s*|\b(?:and|but|please|with|so)\s+)no\s+(?!of\b)" + _REFUSAL_GAP + _CHART_WORD + r"\b"
    r"|\b(?:without|skip|hide|remove|drop|(?<!why\s)not|don'?t|dont|do\s+not)\s+" + _REFUSAL_GAP + _CHART_WORD + r"\b"
    r"|\bbina\s+" + _CHART_WORD + r"\b"
    r"|\b" + _CHART_WORD + r"\s+(?:\w+\s+)?(?:nahi|nahin|nhi|mat)\b"
    r"|\b" + _CHART_WORD + r"\s+na\s+\w+"                 # a trailing "na" is a soft please ("chart dikhao na")
    r"|\b" + _CHART_WORD + r"\s+(?:ke\s+)?(?:bina|bagair)\b"
    r"|\b" + _CHART_WORD + r"\s+ki\s+(?:zaroorat|zarurat|jarurat)\s+(?:nahi|nahin|nhi)\b"
    r"|\b" + _CHART_WORD + r"\s+(?:hata|hatao|band)\b"
    r"|\bno\s+need\s+(?:for|of|to)\s+(?:\w+\s+){0,2}?" + _CHART_WORD + r"\b"
    r"|\bstop\s+(?:showing|drawing|making)\s+(?:\w+\s+){0,2}?" + _CHART_WORD + r"\b"
    r"|\b(?:did\s+not|didn'?t)\s+ask\s+for\s+(?:an?\s+|the\s+)?" + _CHART_WORD + r"\b"
    r"|\b(?:instead\s+of|rather\s+than)\s+(?:an?\s+|the\s+)?" + _CHART_WORD + r"\b"
)
#: After "as a line" or "switch to area": the type word ends the clause, or what follows says it is the picture
#: ("working as line supervisor", "as an area for improvement" and "switch to line manager" are not).
_ENDS_TYPE = (r"(?=\s*(?:$|[,.;:!?]|please\b|pls\b|plz\b|me\b|mein\b|by\b|per\b|for\s+(?:the|last|this|past|each|every|all)\b"
              r"|(?:charts?|graphs?|plots?|diagrams?)\b))")
_ASK = re.compile(
    r"\b(?:charts?|graphs?|plots?|plotting|plotted|histograms?|graphical(?:ly)?|pictorial(?:ly)?|visuals?\s+(?:view|representation|of)|trend\s*lines?|heat\s*maps?|tree\s*maps?|sunbursts?|gantt)\b"
    r"|\bvi[sz]u?a?l+i[sz]e[sd]?\b|\bvi[sz]u?a?l+i[sz]ations?\s+of\b"
    r"|\b(?:show|display|draw|see|dikhao)\b(?:\s+\w+){0,4}\s+visually\b"
    r"|\b(?:horizontal|vertical|stacked)\s+(?:bars?|columns?)\b"
    r"|\b(?:pie|donut|doughnut|scatter)\s+(?:of|by|for|me|mein|banao|bana|dikhao)\b"
    r"|\b(?:isko|ise|iska)\s+(?:bar|line)\s+(?:me|mein)\b"
    r"|\bfunnel\s+(?:of|by)\b"
    r"|\b(?:bars?|pie|donut|funnel)\s+please\W*$"
    r"|\bas\s+(?:an?\s+)?(?:pie|donut|doughnut|bars?|scatter|histogram|funnel|line|area|columns)\b" + _ENDS_TYPE +
    r"|\bswitch\s+to\s+(?:an?\s+)?(?:bars?|lines?|pie|donut|area|scatter)\b" + _ENDS_TYPE +
    r"|\bmake\s+(?:it|them|that)\s+(?:an?\s+)?(?:horizontal|vertical|stacked|bars?|lines?|pies?|donut|doughnut|area)\b"
)
_TYPE_WORDS = [(n, re.compile(p)) for n, p in (
    ("horizontal_bar", r"\bhorizontal(?:\s+(?:bars?|columns?|charts?|graphs?))?\b"), ("donut", r"\b(?:donut|doughnut)\b"), ("pie", r"\bpie\b"),
    ("line", r"\blines?\b"), ("bar", r"\b(?:bars?|columns?|stacked)\b"),
    ("unsupported", r"\b(?:histogram|scatter|funnel|heat\s*map|tree\s*map|sunburst|gantt|area)\b"))]
_NOUN = re.compile(r"\s+(?:charts?|graphs?|plots?)\b")
_NOT_TYPE = re.compile(r"\s+(?:managers?|of\s+business|items?|stages?)\b")
_REFUSED_TYPE = re.compile(r"\b(?:instead\s+of|rather\s+than|not|no)\s+(?:an?\s+|the\s+)?$")
#: "in west area": a word before them that the question reads as a field, not as a type ("by" and "per": named_type).
_FIELD_BEFORE = re.compile(r"\b(?:in|of)\s+(?!an?\s|the\s)\w+\s+$")
#: The page's cap on a question (web/server.py); a longer one (the command line has no cap) reads as no ask.
_MAX_ASK = 1000
_FILLER = frozenset("""a an the this that it them these those now then please pls plz me my mujhe ek isko iska iski uska uski
    usko is us ye yeh wo woh ka ki ke ko mein se aur bhi and also or as in on of for with to from show give make
    draw create get need want can could you i we do display see view plot plots chart charts graph graphs visualize
    visualise visually graphically graphical bar bars pie line lines donut doughnut column columns horizontal vertical
    stacked dikhao dikha dikhana dikhado dikhaye banao bana bnao banado de karo kar kardo chahiye dena dedo
    using into same above previous last earlier again one data records numbers table list dono both
    just only sirf plus hai kya
    thing things answer answers result results mai main wale wala upar pichle pichla able are have tum aap sakte sakta
    sakti ho was not na no showing visible empty wrong visualization visualisation visual plotting plotted""".split())
_WORD_RE = re.compile(r"[^\W\d_]+|\d+")
_TIME_WORDS = frozenset("day days week weeks month months quarter quarters year years".split())
_PERIOD_WORDS = frozenset("this last next past previous pichle is".split())
_BACK_WORDS = frozenset("""that this it those these them same above previous earlier isko iska iski uska uski usko
    ye yeh wo woh upar pichle pichla""".split())
_SPLIT_NOUNS = frozenset("source status type name stage level date owner round outcome".split())


def fold(q):
    t = (q or "").lower().replace("’", "'")
    for p, r in _FOLD:
        t = p.sub(r, t)
    return re.sub(r"\s+", " ", t).strip()


def fires(t):
    if _STOP.search(t) or _ABOUT.search(t):
        return False
    return bool(_ASK.search(_NOT_A_CHART.sub(" ", t)))


def named_type(t):
    # the first the question names, and never one it turns down ("a bar chart instead of a pie chart")
    named = [(m.start(), name) for name, rx in _TYPE_WORDS for m in rx.finditer(t)
             if _NOUN.match(t, m.end()) and not _REFUSED_TYPE.search(t[:m.start()])]
    if named:
        return min(named)[1]
    for name, rx in _TYPE_WORDS:   # a word after by or per, or before manager, item and so on, names a field
        for m in rx.finditer(t):
            before = t[:m.start()]
            if re.search(r"\b(?:by|per)\s+(?:\w+\s+)?$", before) or _NOT_TYPE.match(t, m.end()) \
                    or _REFUSED_TYPE.search(before):
                continue
            if m.group(0) in ("line", "lines", "area") and _FIELD_BEFORE.search(before):
                continue
            return name
    return None


def _without_split(words: list[str]) -> list[str]:
    """The words left once a split ("by month", "per lead source", "month wise") and a period ("last year") are out."""
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w in ("by", "per") and i + 1 < len(words) and words[i + 1] not in _FILLER:
            i += 3 if i + 2 < len(words) and words[i + 2] in _SPLIT_NOUNS else 2
            continue
        if i + 1 < len(words) and words[i + 1] == "wise":
            i += 2
            continue
        elif w in _TIME_WORDS and out and (out[-1] in _PERIOD_WORDS or out[-1].isdigit()):
            while out and (out[-1] in _PERIOD_WORDS or out[-1].isdigit()):
                out.pop()
            i += 1
            continue
        out.append(w)
        i += 1
    return out


def bare(t):
    # no earlier result is kept, so a word that points back with only a split or a period beside it names no data: "now
    # plot that by month" would make the record agent guess what to count (model test 2026-11-04: it drew an object nobody
    # asked for). Without such a word a split can be the data ("candidate wise chart"), and a false bare loses the answer.
    words = _WORD_RE.findall(t)
    if not [w for w in words if w not in _FILLER]:
        return True
    left = _without_split(words)
    return bool(_BACK_WORDS.intersection(left)) and not [w for w in left if w not in _FILLER]


@dataclass(frozen=True)
class ChartAsk:
    type: str | None     # "bar", "horizontal_bar", "line", "pie", "donut", "unsupported" or None
    bare: bool           # names nothing to chart ("now plot that", "graph dikhao")


def chart_ask(question) -> ChartAsk | None:
    try:
        t = fold(question)
        if len(t) > _MAX_ASK or not fires(t):
            return None
        return ChartAsk(named_type(t), bare(t))
    except Exception:  # noqa: BLE001 - a question never fails on this
        return None


def drawable_ask(question: Any) -> bool:
    """The switch is on (read now), the question asks for a chart and it names something to chart."""
    if not charts_settings().on:
        return False
    ask = chart_ask(question)
    return ask is not None and not ask.bare


def note_for(question: Any) -> str:
    """The record agent's note for this question: :data:`RECORDS_CHART_NOTE`, "" unless :func:`drawable_ask`."""
    return RECORDS_CHART_NOTE if drawable_ask(question) else ""


def bare_line() -> str:
    """The one line a question that names nothing to chart gets, with no model call."""
    return _WORDS["bare"]


# ------------------------------------------------------------------------------------------------ chart talk
_MARK = re.compile(r"[*_`]{0,3}\{\{\s*(?:charts?|graphs?|plots?)\b[^{}\n]{0,80}\}\}[*_`]{0,3}", re.I)
_IMAGE = re.compile(r"!\[[^\]\n]{0,200}\]\([^)\n]{0,500}\)")
_BLOCKS = re.compile(r"[▀-▟■▪]{2,}")
_PLOT_CODE = re.compile(r"matplotlib|plt\.|seaborn|plotly|mermaid|\bpie\s+title\b|\bxychart\b|[▀-▟]", re.I)
_TALK = re.compile(r"\b(?:charts?|graphs?|plots?|plotting|plotted|visuali[sz]\w*|diagrams?)\b"
                   r"|\b\w*(?:plot|chart|graph)_\w+\b|\b\w+_(?:plot|chart|graph)\b", re.I)
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_SOURCE_RE = re.compile(r"[*_`>\-\s]*source\s*:", re.I)
_LIST_MARK_RE = re.compile(r"\s*(?:[-*+]\s+|\d{1,3}[.)]\s+)?")
_EMPTY_ITEM_RE = re.compile(r"(?:[-*+]|\d{1,3}[.)])?")   # all that is left of a list item once its span is gone


def _without(line: str, *spans: re.Pattern) -> str:
    """``line`` without these spans. With one gone, no double space is left and no space at either end."""
    s = line
    for rx in spans:
        s = rx.sub("", s)
    if s != line:
        s = line[:len(line) - len(line.lstrip())] + re.sub(r" {2,}", " ", s).strip()
    return s


def _kept_line(line: str) -> str | None:
    """``line`` without chart marks, images, block bars and the sentences that talk about a chart and hold no digit;
    None when nothing of it is left. The same text when nothing goes. The end of a list item with no closing stop reads
    as a row ("- Chart Analyst (Open)"), not as a sentence, and rows are never removed (decision 14)."""
    s = _without(line, _MARK, _IMAGE, _BLOCKS)
    if _EMPTY_ITEM_RE.fullmatch(s.strip()):
        return None
    lead = _LIST_MARK_RE.match(s).group(0)
    parts = [p for p in _SENTENCE_RE.split(s[len(lead):]) if p.strip()]
    row = len(parts) - 1 if lead.strip() and not parts[-1].rstrip().endswith((".", "!", "?", ":")) else -1
    keep = [p for i, p in enumerate(parts) if re.search(r"\d", p) or not _TALK.search(p) or i == row]
    if not keep:
        return None
    return lead + " ".join(keep) if keep != parts or s != line else line


def _marks_gone_line(line: str) -> str | None:
    """``line`` without chart marks, images and block bars, every sentence kept; None when nothing of it is left."""
    s = _without(line, _MARK, _IMAGE, _BLOCKS)
    return None if _EMPTY_ITEM_RE.fullmatch(s.strip()) else s


def _clean_lines(text: str, rows: set[str], one_line: Any) -> str:
    """``text`` with ``one_line`` run on each line that may change: a fenced block of plotting code goes, any other
    fence stays, and a table row, a Source line and a line of ``rows`` stay untouched. Per line, by a loop and never a
    whole-text regex."""
    out: list[str] = []
    fence: list[str] | None = None
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            if fence is None:
                fence = [line]
            else:
                fence.append(line)
                if not _PLOT_CODE.search("\n".join(fence)):
                    out.extend(fence)
                fence = None
        elif fence is not None:
            fence.append(line)
        elif not line.strip() or line.lstrip().startswith("|") or _SOURCE_RE.match(line) or line in rows:
            out.append(line)
        else:
            kept = one_line(line)
            if kept is not None:
                out.append(kept)
    if fence is not None and not _PLOT_CODE.search("\n".join(fence)):   # a reply cut off inside its fence
        out.extend(fence)
    return "\n".join(out)


def drop_talk(text: Any, blocks: Any = None) -> Any:
    """``text`` without what the model says about a chart: a fenced block of plotting code, chart marks, images, bars
    of block characters and the sentences that name a chart and hold no digit. A table row and a Source line stay
    untouched, and so does any other fence and a line of the rows code wrote (``blocks``: ``rec.table_blocks``). When
    that leaves no answer (``agent.nudges.answer_text``, the test the rest of the answer path uses), the talk sentences
    come back, because a Source line or a label alone would read as no answer; the marks, images, bars and plotting code
    never do, so a reply of nothing else leaves no answer and gets the usual reason line. Never raises."""
    try:
        from sf_assistant.agent.nudges import answer_text
        rows = {ln for block in (blocks or {}).values() for ln in str(block).split("\n")}
        kept = _clean_lines(text, rows, _kept_line)
        return kept if answer_text(kept) else _clean_lines(text, rows, _marks_gone_line)
    except Exception:  # noqa: BLE001 - the text as it is
        return text


def _marks_out(text: Any) -> Any:
    """``text`` without chart marks, every sentence and fence kept: a mark is never an answer's content, whatever its
    kind, unlike a sentence about a chart."""
    if not isinstance(text, str) or "{{" not in text:
        return text
    lines = [(ln, _without(ln, _MARK)) for ln in text.split("\n")]
    return "\n".join(s for ln, s in lines if s == ln or s.strip())


# ------------------------------------------------------------------------------------------------ numbers and words
def _say(key: str, **values: Any) -> str:
    return _prompt_files.fill(_WORDS[key], **values)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _enum(v: Any) -> str:
    """An order or measure as the tool reads it (tools/record_tools.py ``_ENUM_SYNONYMS``): ``evidence.arguments`` keeps
    what the model typed ("ASC", "average")."""
    from sf_assistant.tools.record_tools import _ENUM_SYNONYMS
    s = str(v).strip().lower()
    return _ENUM_SYNONYMS.get(s, s)


def _fmt(v: int | float | None) -> str:
    """A value on a chart, exact: thousands marks, a whole float as a whole number, no currency symbol; null as the
    words ``no value``."""
    if v is None:
        return _WORDS["no_value"]
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return f"{v:,}"


def _number(v: Any) -> int | float | None:
    """A number the model typed as an argument ("5" or 5), None when it is none. Its text never reaches a chart."""
    if _is_num(v):
        return v
    try:
        n = float(str(v).strip())
    except ValueError:
        return None
    return (int(n) if n.is_integer() else n) if math.isfinite(n) else None


def _day(v: Any) -> dt.date | None:
    """``v`` (a date or its ISO text) as a date; None when it is neither."""
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def _date_text(v: Any) -> str:
    """A result's date as it stands, "" for anything else: only a date reaches a chart from a period."""
    return v if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else ""


def _past_today(period: Any, today: dt.date | None) -> bool:
    """The test of ``RecordTools._future_note``: the period's end is later than today + 1 day. Never without a today."""
    if today is None or not isinstance(period, dict):
        return False
    end = _day(period.get("to_exclusive"))
    return end is not None and end > today + dt.timedelta(days=1)


class _Cleaner:
    """Every row label, series name and compare label, in this order: what XML forbids (:data:`_BAD_XML`) goes,
    ``tables._label``, ``hide_embedded_secrets``, ``drop_contact_details`` unless the question asks for contact details,
    then a 200-character cap with no space left at its end, because a caller that strips a series name would no longer
    find it among the columns. The display cut comes last, so an e-mail at character 190 is still caught. ``keep``: the
    numbers the results hold as a whole value, as ``RecordSession.ask`` reads them."""

    def __init__(self, question: Any, results: Any):
        from sf_assistant.engine.contact import asks_for_contact, result_numbers
        self.question = str(question or "")
        self.asked = asks_for_contact(self.question)
        self.keep = result_numbers(results)

    def __call__(self, v: Any) -> str:
        from sf_assistant.agent.tables import _label
        from sf_assistant.engine.catalog import hide_embedded_secrets
        from sf_assistant.engine.contact import drop_contact_details
        # what XML forbids goes first, so rows, notes and alt match the SVG, and a lone surrogate never costs the answer
        s = hide_embedded_secrets(_label(_BAD_XML.sub("", v) if isinstance(v, str) else v))
        if not self.asked:
            s = drop_contact_details(s, self.keep)[0]
        return s[:_KEEP_LABEL].rstrip()


class _Labels:
    """``labels(object, column)`` as :func:`~sf_assistant.agent.tables.column_labels`, and the object words of a title
    and a subtitle from the catalog. Without a catalog (stub tools, records off on MCP) every word is humanized and no
    filter text names a known field."""

    def __init__(self, catalog: Any):
        from sf_assistant.agent.tables import column_labels
        self._column = column_labels(catalog)
        objects = getattr(catalog, "objects", None)
        self._objects = objects if isinstance(objects, dict) else {}

    def __call__(self, obj: Any, column: Any) -> str:
        return self._column(obj, column)

    def _word(self, obj: Any, *names: str) -> str:
        spec = self._objects.get(obj)
        for name in names:
            word = " ".join(str(getattr(spec, name, "") or "").split())
            if word and "__" not in word:
                return word
        return self._column(None, obj) if obj else ""

    def plural(self, obj: Any) -> str:
        """The object's word in a title: its plural label, else its label with a plural ending (a third of the objects,
        ``Lead`` and ``Case`` among them, have no plural label), else its API name in words."""
        word = self._word(obj, "plural_label", "label")
        spec = self._objects.get(obj)
        if str(getattr(spec, "plural_label", "") or "").strip() or not str(getattr(spec, "label", "") or "").strip() \
                or word.endswith("s"):
            return word
        if re.search(r"[^aeiou]y$", word):
            return word[:-1] + "ies"
        return word + ("es" if re.search(r"(?:ch|sh|x|z)$", word) else "s")

    def singular(self, obj: Any) -> str:
        return self._word(obj, "label")

    def knows(self, obj: Any) -> bool:
        return obj in self._objects

    def has_field(self, obj: Any, path: str) -> bool:
        """The first hop of a field path is a field of the object."""
        fields = getattr(self._objects.get(obj), "fields", None)
        return isinstance(fields, dict) and path.split(".")[0] in fields


# ------------------------------------------------------------------------------------------------ one result
class _NoChart(Exception):
    """No chart from these rows: its one argument is the reason's key."""


class _SvgRefused(Exception):
    """An SVG did not pass :func:`_check_svg`."""


@dataclass(frozen=True)
class _Row:
    label: str    # cleaned, whole
    source: Any   # the label as the result gives it: the partial notes name it so
    value: Any
    by: dict      # the split as the result gives it (raw names); {} without one
    counts: dict = None   # a rate per period: its "part" and "base" as the result gives them; None otherwise


#: A partial period, from the note words of ``RecordTools._trend_records``, which cover all three cases.
_PARTIAL_RE = re.compile(r"^the (?:first|last) bucket \((.+?)\) (?:covers only|is still running)")
#: The cut note of ``RecordTools._fit`` or of the 50-group cap.
_SHOWING_RE = re.compile(r"\bshowing (\d+) of (\d+)(?: (?:groups|segments))?$", re.I)
#: The tool's note when a ``top`` ranking leaves out the blank group.
_BLANK_LEFT_OUT = "the (blank) group is left out of the ranking"
_TOKEN_RE = re.compile(r"[A-Za-z_][\w.]*")
_RELATED_RE = re.compile(r"has (?:a|no|at least \d+) (\w+)")
_ROW_KEYS = {"group_records": ("groups", "group", "groups"), "trend_records": ("buckets", "bucket", "periods"),
             "compare_records": ("segments", "label", "segments")}
_PLAIN_TYPE = {"horizontal_bar": "horizontal bar"}   # <<TYPE>> and <<ASKED>>: bar, line, pie and donut read as they are


def _query_id(result: Any) -> str | None:
    qid = result.get("query_id") if isinstance(result, dict) else None
    return qid if isinstance(qid, str) else None


def _no_chart(tool: Any, result: Any, word: str) -> dict:
    """An item with no chart, no rows and only its reason (:func:`_item` makes the ones that show their rows)."""
    return {"query_id": _query_id(result), "tool": tool if isinstance(tool, str) else None, "chart": None, "data": [],
            "truncated": False, "subtitle": [], "notes": [], "partial": [], "reason": _WORDS[word],
            "rows_in_answer": False, "alt": ""}


def _compare_label(segment: dict, n: int, clean: _Cleaner) -> str:
    """A compare label is model text: cleaned, and a digit run in neither the question nor this segment's ``applied``
    replaces it with the segment's own dates, or ``Segment N`` when it has no period."""
    label = clean(segment.get("label"))
    applied = _dict_of(segment.get("applied"))
    held = set(re.findall(r"\d+", f"{clean.question} {json.dumps(applied, ensure_ascii=False, default=str)}"))
    if set(re.findall(r"\d+", label)) <= held:
        return label
    period = _dict_of(applied.get("period"))
    start, end = _date_text(period.get("from")), _date_text(period.get("to"))
    return f"{start} to {end}" if start and end else start or end or _say("segment", COUNT=str(n))


def _result_notes(res: dict) -> list[str]:
    held = res.get("notes")
    return [n for n in held if isinstance(n, str)] if isinstance(held, list) else []


def _no_rows_word(res: dict, args: dict) -> str:
    """Why a result holds no rows: a minimum no named group reaches (the blank and unresolved groups can pass it, and a
    ``top`` ranking leaves them out), a cut that left none (``_fit``: "showing 0 of 5"), records that all fall in those
    left-out groups, or no record matches. Only a count is sure to hold those groups' numbers. Under another measure
    the note of ``RecordTools._group_records`` tells the blank group, and nothing tells the unresolved group from no
    match, so a ranking says only that it has no named group."""
    if _number(args.get("min_value")) is not None:
        return "no_rows_min"
    notes = [n.strip() for n in _result_notes(res)]
    if "0" in [k for n in notes for k, _n in _SHOWING_RE.findall(n)]:
        return "too_large"
    special = [res.get("blank_group_value"), res.get("unresolved_ids")]
    if _BLANK_LEFT_OUT in notes or any(_is_num(v) and v > 0 for v in special):
        return "only_blank"
    return "no_rows_top" if "top" in args and res.get("measure", "count") != "count" else "no_rows"


def _rows_of(tool: str, res: dict, args: dict, field: Any, clean: _Cleaner) -> list[_Row]:
    """The rows of a chartable result, in its order. Raises :class:`_NoChart` when it holds none."""
    if tool == "count_records":
        return [_Row(clean(k), k, v, {}) for k, v in res["breakdown"][field].items()]
    key, label_key, _unit_word = _ROW_KEYS[tool]
    items = res.get(key)
    if not isinstance(items, list):
        raise _NoChart("fault")
    if not items:
        raise _NoChart(_no_rows_word(res, args))
    rows = []
    for i, it in enumerate(items):
        label = _compare_label(it, i + 1, clean) if tool == "compare_records" else clean(it.get(label_key))
        by = it.get("by")
        rows.append(_Row(label, it.get(label_key), it.get("value"), by if isinstance(by, dict) else {},
                         {k: it.get(k) for k in ("part", "base") if k in it}))
    return rows


def _value(v: Any, clean: _Cleaner) -> Any:
    """A value of a row of ``data``: a number or null as it stands, anything else as cleaned text (it gives a reason,
    and such rows are never drawn)."""
    return v if v is None or _is_num(v) else clean(v)


def _refused(tool: str, res: dict, rows: list[_Row]) -> str:
    """Why these rows give no chart, or "". Rows that do not fit a type are :func:`_choose_type`'s."""
    if len(rows) == 1:
        return _WORDS["one_row"]
    all_values = [r.value for r in rows] + [v for r in rows for v in r.by.values()]
    if any(isinstance(v, str) for v in all_values):
        return _WORDS["dates"]
    if any(v is not None and not _is_num(v) for v in all_values):
        return _WORDS["fault"]
    values = [r.value for r in rows]
    numbers = [v for v in values if v is not None]
    if not numbers:
        return _WORDS["no_values"]
    if any(v < 0 for v in all_values if v is not None):
        return _WORDS["negative"]
    if not any(numbers):   # nothing to draw: all zeros, or zeros beside nulls
        return _WORDS["all_zero" if len(numbers) == len(values) else "no_positive"]
    if all("[hidden]" in r.label for r in rows):
        return _WORDS["hidden"]
    if len(rows) > (MAX_POINTS if tool == "trend_records" else MAX_BARS):
        total = res.get("group_count")
        return _say("too_many", COUNT=_fmt(max(len(rows), total if _is_num(total) else 0)))
    if len(set(_shown_all([r.label for r in rows]))) < len(rows):
        return _WORDS["same_labels"]
    return ""


def _unit(measure: str, res: dict, args: dict, obj: Any, labels: _Labels) -> str:
    """The word of a row's value: ``Records`` only for count, ``Percent`` for a rate per period."""
    if measure == "count":
        return _WORDS["records"]
    if measure == "percent":
        return _WORDS["percent"]
    field = res.get("measure_field")
    named = isinstance(field, str) and field.strip()
    word = labels(obj, field) if named else _field_word(obj, args.get("measure_field"), labels)
    if not word:
        raise _NoChart("fault")
    return _say(measure, FIELD=word)


def _field_word(obj: Any, typed: Any, labels: _Labels) -> str:
    """The label of a field the result does not name, only the model's arguments do; "" when there is none. Typed text
    reaches a chart only as a field the catalog knows (with no catalog: in words)."""
    m = _TOKEN_RE.fullmatch(typed.strip()) if isinstance(typed, str) else None
    if m is None:
        return ""
    known = labels.has_field(obj, m.group(0)) or not labels.knows(obj)
    return labels(obj, m.group(0)) if known else _WORDS["other_field"]


def _title(tool: str, res: dict, obj: Any, field_word: str, measure: str, unit: str, labels: _Labels) -> str:
    name = labels.plural(obj) or _WORDS["records"]
    if measure != "count":
        name = _say("title_measure", OBJECT=name, UNIT=unit)
    if tool == "compare_records":
        return _say("title_compared", OBJECT=name)
    if tool == "trend_records":
        return _say("title_per", OBJECT=name, FIELD=res["grain"])
    return _say("title_by", OBJECT=name, FIELD=field_word)


def _period_line(period: dict, obj: Any, labels: _Labels) -> str:
    """Dates and the date field's label from the result's own keys; never ``phrase``, which can be model text."""
    date_field = period.get("date_field")
    word = labels(obj, date_field) if isinstance(date_field, str) else _WORDS["other_field"]
    start, end = _date_text(period.get("from")), _date_text(period.get("to"))
    if start and end:
        return _say("period", FROM=start, TO=end, FIELD=word)
    if start:
        return _say("period_from", FROM=start, FIELD=word)
    return _say("period_to", TO=end, FIELD=word) if end else ""


def _filter_words(applied: list[dict], obj: Any, labels: _Labels) -> list[str]:
    """Each filter, any_of and related condition of these ``applied`` blocks by its label, once per field: two fields
    never share a word. A filter text holds values and model-typed words, so only a first token that is a known field
    reaches a chart. A person's filter reads as its lookup (``Interviewer__c.Name``: Interviewer, not Name)."""
    out: dict[str, str] = {}
    for block in applied:
        for key in ("filters", "any_of"):
            for text in block.get(key) or []:
                m = _TOKEN_RE.match(str(text))
                if m is not None and labels.has_field(obj, m.group(0)):
                    token = m.group(0).removesuffix(".Name")
                    out[token] = labels(obj, token)
                else:
                    out[_WORDS["other_field"]] = _WORDS["other_field"]
        for text in block.get("related") or []:
            m = _RELATED_RE.match(str(text))
            known = m is not None and labels.knows(m.group(1))
            word = _say("related", OBJECT=labels.singular(m.group(1))) if known else _WORDS["other_field"]
            out[word] = word
    return list(out.values())


def _dict_of(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _subtitle(tool: str, res: dict, args: dict, obj: Any, measure: str, rows: list[_Row], labels: _Labels) -> list[str]:
    """What was counted, metadata only: the period, every filter by label with no cut, and the result's own total when
    the rows drawn add up to it (under ``min_value`` the tool's total holds only the groups kept, so none)."""
    lines: list[str] = []
    applied = _dict_of(res.get("applied"))
    blocks = [applied]
    if tool == "compare_records":
        segments = [_dict_of(s.get("applied")) for s in res["segments"]]
        blocks += segments
        period = next((s["period"] for s in segments if isinstance(s.get("period"), dict)), None)
        if period is not None:
            date_field = period.get("date_field")
            lines.append(_say("period_by", FIELD=labels(obj, date_field) if isinstance(date_field, str)
                              else _WORDS["other_field"]))
    elif isinstance(applied.get("period"), dict):
        line = _period_line(applied["period"], obj, labels)
        if line:
            lines.append(line)
    words = _filter_words(blocks, obj, labels)
    if words:
        lines.append(_say("filtered", FIELDS=", ".join(words)))
    if measure == "percent":   # a rate per period: what the part is, by field label, and the window's own numbers
        part = _filter_words([{"filters": applied.get("part_filters") or []}], obj, labels)
        lines.append(_say("rate_of", FIELDS=", ".join(part) or _WORDS["other_field"]))
        whole = [res.get(k) for k in ("percent_overall", "part_total", "base_total")]
        if all(_is_num(v) for v in whole):
            lines.append(_say("rate_overall", COUNT=_fmt(whole[0]), SHOWN=_fmt(whole[1]), TOTAL=_fmt(whole[2])))
        return lines
    total = None
    if tool == "group_records" and measure == "count" and "records_matched" not in res \
            and _number(args.get("min_value")) is None:
        total = res.get("total_across_groups")
    elif tool == "trend_records" and measure in ("count", "sum"):
        total = res.get("total")
    elif tool == "count_records":
        total = res.get("count")
    values = [r.value for r in rows]
    # the total of exactly these rows, never a sum (the rule of tables._lines); never for compare or distinct
    if _is_num(total) and len(values) > 1 and all(_is_num(v) for v in values) \
            and round(sum(values), 2) == round(float(total), 2):
        lines.append(f"{_WORDS['total']}: {_fmt(total)}")
    return lines


def _notes(tool: str, res: dict, args: dict, measure: str, rows: list[_Row], today: dt.date | None) -> \
        tuple[list[str], bool, list[int]]:
    """``(truth notes, a row of the result is not in the rows, indexes of the partial periods)``: fixed words and the
    result's own numbers; a compare's partial note also names its cleaned labels."""
    notes: list[str] = []
    truncated = False
    unit_word = _WORDS[_ROW_KEYS[tool][2]] if tool in _ROW_KEYS else ""
    result_notes = _result_notes(res)
    labels = [r.label for r in rows]
    if tool == "group_records":
        named = sum(1 for r in rows if r.source not in _SPECIAL_LABELS)   # as the result holds it: "   " is a group
        total = res.get("group_count")
        if _is_num(total) and total > named:   # the order is only in the arguments, as the model typed it
            word = {"asc": "showing_low", "desc": "showing_top"}.get(_enum(args.get("order") or "desc"), "showing")
            notes.append(_say(word, SHOWN=_fmt(named), TOTAL=_fmt(total), UNIT=unit_word))
            truncated = True
    if unit_word and not truncated:
        pairs = [(a, b) for n in result_notes for a, b in _SHOWING_RE.findall(n.strip())]
        if pairs:
            whole = max(int(n) for _k, n in pairs)
            notes.append(_say("showing", SHOWN=_fmt(len(rows)), TOTAL=_fmt(whole), UNIT=unit_word))
            truncated = True
    if tool == "group_records":
        if _is_num(res.get("ties_at_cutoff_count")) and res["ties_at_cutoff_count"] > 0:
            notes.append(_say("ties", COUNT=_fmt(res["ties_at_cutoff_count"])))
        minimum = _number(args.get("min_value"))
        if minimum is not None:
            notes.append(_say("min_value", COUNT=_fmt(minimum)))
            truncated = True
        blank = res.get("blank_group_value")
        if _is_num(blank) and blank != 0 and "(blank)" not in labels:
            word = "blank_out" if measure == "count" else "blank_out_value"
            notes.append(_say(word, COUNT=_fmt(blank)))
            truncated = True
        unresolved = res.get("unresolved_ids")
        if _is_num(unresolved) and unresolved > 0 and "(unresolved id)" not in labels:
            notes.append(_say("unresolved_out", COUNT=_fmt(unresolved)))
            truncated = True
        if "records_matched" in res and _is_num(res["records_matched"]):
            notes.append(_say("overlap", COUNT=_fmt(res["records_matched"])))
    if tool == "trend_records" and _is_num(res.get("records_without_date")) and res["records_without_date"] > 0:
        notes.append(_say("no_date", COUNT=_fmt(res["records_without_date"])))
    if measure == "percent":
        empty = [r.label for r in rows if r.value is None]
        if empty:
            notes.append(_say("no_base", LABELS=", ".join(empty)))
    partial: list[int] = []
    if tool == "trend_records":
        named_parts = {m.group(1) for m in (_PARTIAL_RE.match(n) for n in result_notes) if m}
        partial = [i for i, r in enumerate(rows) if r.source in named_parts]
    elif tool == "compare_records":
        partial = [i for i, s in enumerate(res["segments"])
                   if _past_today(_dict_of(s.get("applied")).get("period"), today)]
    if partial:
        notes.append(_say("partial", LABELS=", ".join(rows[i].label for i in partial)))
    if tool in ("group_records", "count_records") and today is not None:
        period = _dict_of(res.get("applied")).get("period")
        no_end = isinstance(period, dict) and period.get("from") and not period.get("to_exclusive")
        if _past_today(period, today) or no_end:
            notes.append(_WORDS["running"])
    return notes, truncated, partial


@dataclass(frozen=True)
class _SplitPlan:
    names: dict         # raw series name -> row key, largest total first; {} without a split (or a dropped one)
    stack: bool         # usable as a stack
    lines: bool         # a line per series if asked (a stack, or a split under a measure that cannot be stacked)
    why: str            # the word of the note when the split stays out of the chart ("" with no note)


def _adds_up(row: _Row) -> bool:
    """The row's segments add up to its value after rounding (null segments count as nothing, never exact float
    equality). A null value with only null segments passes, and draws no bar."""
    segments = [v for v in row.by.values() if v is not None]
    if row.value is None:
        return not segments
    return _is_num(row.value) and all(_is_num(v) for v in segments) and round(sum(segments), 2) == round(row.value, 2)


def _split(rows: list[_Row], measure: str, clean: _Cleaner, taken: set[str]) -> _SplitPlan:
    """What can be done with a split. Series stay in the rows even when not drawn; two series with one cleaned name
    would overwrite each other, so that split leaves the rows too, and its note says so."""
    totals: dict[Any, float] = {}
    for r in rows:
        for raw, v in r.by.items():
            totals[raw] = totals.get(raw, 0) + (v if _is_num(v) else 0)
    if not totals:
        return _SplitPlan({}, False, False, "")
    names = {}
    for raw in sorted(totals, key=lambda k: -totals[k]):
        key = clean(raw)
        names[raw] = f"{key} (split)" if key in taken or key in _RESERVED_KEYS else key
    if len(set(names.values())) < len(names):
        return _SplitPlan({}, False, False, "split_same")
    if len(names) > MAX_SERIES:
        return _SplitPlan(names, False, False, "split_many")
    if measure not in ("count", "sum"):
        return _SplitPlan(names, False, len(names) >= 2, "split_measure")
    if not all(_adds_up(r) for r in rows):
        return _SplitPlan(names, False, False, "split_adds")
    if len(names) < 2:
        return _SplitPlan(names, False, False, "")
    return _SplitPlan(names, True, True, "")


def _pie_fits(tool: str, res: dict, measure: str, rows: list[_Row], truncated: bool) -> bool:
    """The rows are all the parts of one whole: a group under count, nothing cut, no overlap, adding up to the result's
    own total, or a breakdown field adding up to the count. Never a trend, a compare or a percentage."""
    values = [r.value for r in rows]
    if not 2 <= len(values) <= MAX_SLICES or not all(_is_num(v) for v in values):
        return False
    if tool == "group_records":
        whole = res.get("total_across_groups")
        if measure != "count" or "records_matched" in res or truncated:
            return False
    elif tool == "count_records":
        whole = res.get("count")
    else:
        return False
    return _is_num(whole) and round(sum(values), 2) == round(float(whole), 2)


#: The note of a named type a shape cannot carry.
_NOT_FIT = {"horizontal_bar": "type_no_fit", "line": "type_no_fit", "pie": "type_no_fit", "donut": "type_no_fit",
            "unsupported": "type_other"}


def _choose_type(asked: str | None, trend: bool, labels: list[str], stack: bool, lines: bool, pie: bool) -> \
        tuple[str, bool, bool, str]:
    """``(type, stacked, series drawn, note word or "")``: code decides. The question's named type wins when the shape
    can carry it. Code never picks a pie by itself, and ``stacked`` is true only on bars."""
    n = len(labels)
    if not trend:
        size = "horizontal_bar" if n > 8 or any(len(lb) > 16 for lb in labels) else "bar"
        if asked == "horizontal_bar":
            return "horizontal_bar", stack, stack, ""
        if asked in ("pie", "donut") and pie and not stack:
            return asked, False, False, ""
        return size, stack, stack, _NOT_FIT.get(asked, "")
    if asked == "line" and lines:
        return "line", False, True, ""
    if stack:
        word = _NOT_FIT.get(asked, "")
        return ("bar", True, True, word) if n <= MAX_BARS else ("line", False, True, word)
    if lines:
        return "line", False, False, _NOT_FIT.get(asked, "")
    if asked == "bar":
        return ("bar", False, False, "") if n <= MAX_BARS else ("line", False, False, "type_no_fit")
    return "line", False, False, "" if asked == "line" else _NOT_FIT.get(asked, "")


def _item(tool: str, res: dict, args: dict, obj: Any, field: Any, measure: str, clean: _Cleaner, labels: _Labels,
          asked: str | None, today: dt.date | None) -> dict:
    """The item of one set of rows of a result: its payload with a chart spec, or with no chart and the reason. The
    keys that start with "_" are for :func:`_draw` only. Numbers come from ``res`` and nothing here changes it."""
    trend = tool == "trend_records"
    try:
        rows = _rows_of(tool, res, args, field, clean)
        unit = _unit(measure, res, args, obj, labels)
        if trend:
            grain = str(res.get("grain"))
            if grain not in _GRAINS:
                raise _NoChart("fault")
            x_key = grain.title()
        elif tool == "compare_records":
            x_key = _WORDS["compared"]
        else:
            x_key = labels(obj, res.get("grouped_by") if tool == "group_records" else field)
            if not x_key:
                raise _NoChart("fault")
    except _NoChart as no:
        return _no_chart(tool, res, no.args[0])
    if x_key == unit:
        unit += " (value)"
    total_key = _WORDS["total"] if _WORDS["total"] != x_key else f"{_WORDS['total']} (split)"
    plan = _split(rows, measure, clean, {x_key, unit, total_key})
    subtitle = _subtitle(tool, res, args, obj, measure, rows, labels)
    notes, truncated, partial = _notes(tool, res, args, measure, rows, today)
    title = _title(tool, res, obj, x_key, measure, unit, labels)

    def row_of(r: _Row, stacked: bool) -> dict:
        row: dict = {x_key: r.label}
        if not stacked:
            row[unit] = _value(r.value, clean)
        for raw, key in plan.names.items():
            if raw in r.by:
                row[key] = _value(r.by[raw], clean)
        if stacked:
            row[total_key] = _value(r.value, clean)
        for k, v in (r.counts or {}).items():   # a rate's own counts, beside its percent (never drawn)
            row[_WORDS["rate_" + k]] = _value(v, clean)
        return row

    item = {"query_id": _query_id(res), "tool": tool, "chart": None, "data": [row_of(r, False) for r in rows],
            "truncated": truncated, "subtitle": subtitle, "notes": notes, "partial": partial, "reason": "",
            "rows_in_answer": False, "alt": "", "_title": title, "_measure": measure}
    refused = _refused(tool, res, rows)
    if refused:
        item["reason"] = refused
        return item
    pie = _pie_fits(tool, res, measure, rows, truncated)
    kind, stacked, series, word = _choose_type(asked, trend, [r.label for r in rows], plan.stack, plan.lines, pie)
    legend = [r.label for r in rows] if kind in ("pie", "donut") else list(plan.names.values()) if series else []
    if len(set(_fit_all(legend, _CUT_LEGEND))) < len(legend):   # a legend that cannot tell two apart
        item["reason"] = _WORDS["same_labels"]
        return item
    if plan.why and not series:
        held = res.get("split_by")   # a trend's split field is only in the arguments
        split_word = labels(obj, held) if isinstance(held, str) else _field_word(obj, args.get("split_by"), labels)
        split_word = split_word or _WORDS["other_field"]
        notes.append(_say("split_same", FIELD=split_word) if plan.why == "split_same"   # the rows hold no series then
                     else _say("split_left", FIELD=split_word, WHY=_WORDS[plan.why]))
    if word:
        notes.append(_say(word, ASKED=_PLAIN_TYPE.get(asked, asked), TYPE=_PLAIN_TYPE.get(kind, kind)))
    item["chart"] = {"type": kind, "x_key": x_key, "y_keys": list(plan.names.values()) if series else [unit],
                     "title": title, "stacked": stacked}
    item["data"] = [row_of(r, stacked) for r in rows]
    item["_total_key"] = total_key
    item["_absent_zero"] = series and kind == "line" and measure == "count"
    return item


def _build(tool: Any, result: Any, arguments: Any, obj: Any, clean: _Cleaner, labels: _Labels, asked: str | None,
           today: Any) -> list[dict]:
    """The items of one result, the pure core :func:`make` and :func:`for_tool` share: each with a chart spec, or with
    no chart and its reason. ``clean``: :class:`_Cleaner`; ``labels``: :class:`_Labels`; ``asked``: the question's named
    type; ``today``: a date or its ISO text, or None. ``result`` is never changed."""
    word = _OTHER_WORDS.get(tool)
    if word:
        return [_no_chart(tool, result, word)]
    if tool not in _CHART_TOOLS or not isinstance(result, dict):
        return [_no_chart(tool, result, "fault")]
    if result.get("trimmed"):
        return [_no_chart(tool, result, "too_large")]
    if result.get("no_data_fields"):   # the tool's own word: the data does not record this, so never a 0 on a chart
        return [_no_chart(tool, result, "no_field_data")]
    # read as RecordTools._validate reads them: a blank string, an empty list and an empty object are no argument
    args = {k: v for k, v in arguments.items() if v is not None and v != [] and v != {}
            and not (isinstance(v, str) and not v.strip())} if isinstance(arguments, dict) else {}
    measure = _enum(result.get("measure") or args.get("measure") or "count")
    if measure not in _MEASURES or (measure == "percent" and tool != "trend_records"):
        return [_no_chart(tool, result, "fault")]
    obj = result.get("object") or obj
    day = _day(today)
    if tool != "count_records":
        return [_item(tool, result, args, obj, None, measure, clean, labels, asked, day)]
    breakdown = result.get("breakdown")
    if not breakdown:
        return [_no_chart(tool, result, "one_number")]
    fields = [f for f, by in breakdown.items() if isinstance(by, dict)]
    fit = [f for f in fields if len(breakdown[f]) >= 2]
    if not fields:
        return [_no_chart(tool, result, "fault")]
    return [_item(tool, result, args, obj, f, measure, clean, labels, asked, day) for f in fit or fields[:1]]


def _safe_build(tool: Any, result: Any, arguments: Any, obj: Any, clean: _Cleaner, labels: _Labels, asked: str | None,
                today: Any) -> list[dict]:
    """:func:`_build`, with the ``fault`` reason and the fault's class name for a result it cannot read."""
    try:
        return _build(tool, result, arguments, obj, clean, labels, asked, today)
    except Exception as exc:  # noqa: BLE001 - a fault in one result never costs the answer
        return [{**_no_chart(tool, result, "fault"), "_error": type(exc).__name__}]


def _rate_results(results: list[list[dict]]) -> list[list[dict]]:
    """The results of a question that asks for a rate: a chart of counts over time (trend_records or compare_records,
    measure count or count_distinct: :data:`_RATE_REFUSED`) leaves them as its rows with the ``rate_counts`` reason,
    so code never draws counts in place of the rate per period asked. A share by group (group_records, a pie) and a
    measure of a field (avg, sum, min, max: "the average candidate rate") are drawn; a single percentage keeps its own
    reason (``one_share``)."""
    tools, measures = _RATE_REFUSED
    out = []
    for items in results:
        kept = []
        for i in items:
            if i["chart"] and i.get("tool") in tools and i.get("_measure") in measures:
                i = {**i, "chart": None, "reason": _WORDS["rate_counts"]}
            kept.append(i)
        out.append(kept)
    return out


def _pick(results: list[list[dict]]) -> list[dict]:
    """The items to show of an answer's results, in call order: those with a chart of the last chartable result, which
    say when others could be drawn (``several``); with no chartable result, the last result's own items, if any."""
    can = [items for items in results if any(i["chart"] for i in items)]
    if not can:
        return results[-1] if results else []
    picked = [i for i in can[-1] if i["chart"]]
    if len(can) > 1:
        for i in picked:
            i["notes"] = [*i["notes"], _say("several", COUNT=str(len(can)))]
    return picked


# ------------------------------------------------------------------------------------------------ the renderer
_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767")
_INK, _MUTED, _GRID, _SURFACE = "#17232e", "#586776", "#cdd6dd", "#ffffff"
#: The inner style only adds dark mode: every mark carries its light colour as a plain attribute. The page follows only
#: this media feature (web/static/app.css), and :func:`_check_svg` holds every SVG's style to this constant.
_CSS = ("@media (prefers-color-scheme:dark){.b,.h{fill:#18212a}.i{fill:#e2e8ee}.m{fill:#97a6b4}.g{stroke:#2c3844}"
        ".a{stroke:#97a6b4}.s{stroke:#18212a}"
        + "".join(f".f{i}{{fill:{c}}}.l{i}{{stroke:{c}}}" for i, c in enumerate(_DARK)) + "}")
_FONT = "system-ui,-apple-system,'Segoe UI',Roboto,'Noto Sans',Arial,sans-serif"
_W, _PAD = 640, 12
#: Character widths in em, by class, measured on DejaVu Sans; a character in none of them: 0.64, or 1 outside ASCII. A
#: bold title is about 1.15 times as wide.
_EM = {ch: em for chars, em in (("'ijlIJ,. /:;\\|f-", 0.36), ("t()[]!r", 0.41), ('"*_`sz?', 0.53),
                                ("KAVBRXZCUHNDGOQ&", 0.79), ("w#+<=>^~M%mW@", 1.0)) for ch in chars}
_TITLE_SIZE = 15 * 1.15
_PLOT_H, _BAND, _BAR, _COLUMN = 200, 26, 18, 24
_PARTIAL = ' opacity="0.45"'
#: What XML forbids, lone surrogates included: never in a text node.
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_HASH_TAIL_RE = re.compile(r" \(#\d+\)$")
_TAGS = frozenset({"svg", "title", "style", "rect", "path", "circle", "text"})
_ATTRS = frozenset({"xmlns", "width", "height", "viewBox", "role", "class", "x", "y", "rx", "d", "cx", "cy", "r",
                    "fill", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
                    "opacity", "text-anchor", "font-size", "font-weight", "font-family"})


def _x(s: Any) -> str:
    return escape(_BAD_XML.sub("", str(s)), {'"': "&quot;"})


def _width(s: Any, size: float = 13) -> float:
    """The estimated width of a text in px, by the characters it holds. Not measured in a browser: :data:`_EM` is of the
    widest face of :data:`_FONT`, so a text fits in the others too."""
    return sum(_EM.get(ch, 0.64 if ch < "\x7f" else 1.0) for ch in str(s)) * size


def _cut(s: str, n: int) -> str:
    """``s`` in at most ``n`` characters, keeping a trailing " (#k)", which tells records that share a name apart."""
    if len(s) <= n:
        return s
    m = _HASH_TAIL_RE.search(s)
    tail = m.group(0) if m else ""
    return s[:len(s) - len(tail)][:n - 1 - len(tail)] + "…" + tail


def _keep_end(s: str, n: int) -> str:
    """``s`` in at most ``n`` characters, its middle cut."""
    if len(s) <= n:
        return s
    end = n // 3
    return s[:n - 1 - end] + "…" + s[-end:]


def _fit_all(names: list[Any], n: int) -> list[str]:
    """``names`` in at most ``n`` characters each: :func:`_cut`, or the middle cut when only that tells them apart
    ("... Week 1", "... Week 2"). Names neither cut tells apart stay as :func:`_cut` gives them."""
    names = [str(s) for s in names]
    cut = [_cut(s, n) for s in names]
    if len(set(cut)) == len(cut):
        return cut
    kept = [_keep_end(s, n) for s in names]
    return kept if len(set(kept)) == len(kept) else cut


def _shown_all(labels: list[Any]) -> list[str]:
    """The rows' labels as the axis shows them: no ``week of`` (the title says per week), at most 28 characters."""
    return _fit_all([re.sub(r"^week of ", "", str(label)) for label in labels], _CUT_ROW)


def _wrap(s: Any, size: float = 12) -> list[str]:
    """``s`` in lines that fit the image's width at ``size``; a longer word is split, so no line leaves the image."""
    room = _W - 2 * _PAD
    whole = int(room / size)   # characters that fit whatever they are
    out: list[str] = []
    line = ""
    for word in str(s).split():
        while _width(word, size) > room:
            if line:
                out.append(line)
                line = ""
            out.append(word[:whole])
            word = word[whole:]
        if line and _width(f"{line} {word}", size) > room:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return out + ([line] if line else [])


def _tick(v: float) -> str:
    """An axis tick: compact from 10,000."""
    if v >= 10_000:
        for size, mark in ((10 ** 12, "T"), (10 ** 9, "B"), (10 ** 6, "M"), (1000, "K")):
            if v >= size:
                part = v / size
                return (f"{part:,.0f}" if part >= 1000 else f"{part:g}") + mark
    return f"{v:,}" if isinstance(v, int) else f"{v:g}"


def _axis(highest: float, whole: bool) -> tuple[Any, Any]:
    """``(top, step)`` of an axis that starts at 0: the step from highest / 5 over 1, 2, 2.5, 5 and 10, a whole step on
    whole numbers, and a top less than one step above the highest value."""
    if highest <= 0:
        return 1, 1
    raw = highest / 5
    k = 10.0 ** math.floor(math.log10(raw))
    step = next(s * k for s in (1, 2, 2.5, 5, 10) if s * k >= raw and (not whole or float(s * k).is_integer()))
    step = int(step) if float(step).is_integer() else step
    return math.ceil(highest / step - 1e-9) * step, step


def _ink(x: float, y: float, text: Any, cls: str = "i", anchor: str = "", size: int = 0, weight: int = 0) -> str:
    """A text node: labels only in text nodes, in ink colours, never series colours."""
    extra = (f' text-anchor="{anchor}"' if anchor else "") + (f' font-size="{size}"' if size else "") \
        + (f' font-weight="{weight}"' if weight else "")
    fill = _INK if cls == "i" else _MUTED
    return f'<text class="{cls}" x="{x:.1f}" y="{y:.1f}" fill="{fill}"{extra}>{_x(text)}</text>'


def _centre(x: float, low: float, text: str, size: int = 13) -> float:
    """Where a centred text goes: at ``x``, moved in when it would start before ``low`` or leave the image. A value's
    ``low`` is ``left - 4``, because the axis's ticks end at ``left - 6``; an axis label's is 2."""
    half = _width(text, size) / 2
    return min(max(x, low + half), _W - 2 - half)


def _rule(cls: str, x0: float, y0: float, x1: float, y1: float) -> str:
    """A grid or axis line."""
    colour = _GRID if cls == "g" else _MUTED
    d = f"M{x0:.1f} {y0:.1f}L{x1:.1f} {y1:.1f}"
    return f'<path class="{cls}" d="{d}" stroke="{colour}" stroke-width="1" fill="none"/>'


def _head(item: dict) -> tuple[list[str], float]:
    """The title and subtitle lines as text nodes, and the height they take. A long title takes a second line. A chart
    of 2 or more series has its legend under them."""
    y = 4.0
    out = []
    for part in _wrap(_cut(item["chart"]["title"], _CUT_TITLE), _TITLE_SIZE):
        y += 18
        out.append(_ink(_PAD, y, part, size=15, weight=600))
    for line in item["subtitle"]:
        for part in _wrap(line):
            y += 16
            out.append(_ink(_PAD, y, part, "m", size=12))
    y += 8
    if len(item["chart"]["y_keys"]) > 1:
        legend, y = _legend(item["chart"]["y_keys"], y)
        out += legend
    return out, y


def _foot(item: dict, y: float, body: list[str]) -> str:
    """The whole image: ``body`` and under it the notes as text nodes, from ``y`` on."""
    for note in item["notes"]:
        for part in _wrap(note):
            body.append(_ink(_PAD, y, part, "m", size=12))
            y += 16
    return _doc(y + 4, item["chart"]["title"], body)


def _swatch(i: int, x: float, y: float) -> str:
    return f'<rect class="f{i}" x="{x:.1f}" y="{y - 10:.1f}" width="11" height="11" rx="2" fill="{_LIGHT[i]}"/>'


def _legend(names: list[str], y: float) -> tuple[list[str], float]:
    """A swatch and a name per series, wrapped to the width; the height with it."""
    out, x = [], float(_PAD)
    y += 12
    for i, text in enumerate(_fit_all(names, _CUT_LEGEND)):
        wide = 15 + _width(text, 12) + 12
        if x + wide > _W - _PAD and x > _PAD:
            x, y = float(_PAD), y + 18
        out.append(_swatch(i, x, y))
        out.append(_ink(x + 15, y, text, "m", size=12))
        x += wide
    return out, y + 8


def _doc(height: float, title: str, body: list[str]) -> str:
    """The whole image: its own background first, so it reads on any surface."""
    h = int(math.ceil(height))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" role="img" width="{_W}" height="{h}" viewBox="0 0 {_W} {h}" '
            f'font-family="{_FONT}" font-size="13"><title>{_x(title)}</title><style>{_CSS}</style>'
            f'<rect class="b" x="0" y="0" width="{_W}" height="{h}" fill="{_SURFACE}"/>' + "".join(body) + "</svg>")


def _tips(item: dict) -> list[Any]:
    """Each row's value at its tip: the result's own value of the row, never a sum."""
    key = item["_total_key"] if item["chart"]["stacked"] else item["chart"]["y_keys"][0]
    return [row.get(key) for row in item["data"]]


def _is_whole(values: list[float]) -> bool:
    return all(float(v).is_integer() for v in values)


def _hbar(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """A bar from its baseline ``x``: square there, rounded at its data end."""
    r = min(r, w, h / 2)
    e = x + w
    return (f"M{x:.1f} {y:.1f}L{e - r:.1f} {y:.1f}Q{e:.1f} {y:.1f} {e:.1f} {y + r:.1f}L{e:.1f} {y + h - r:.1f}"
            f"Q{e:.1f} {y + h:.1f} {e - r:.1f} {y + h:.1f}L{x:.1f} {y + h:.1f}Z")


def _vbar(x: float, base: float, w: float, h: float, r: float = 4) -> str:
    """A column from its baseline ``base``: square there, rounded at its data end."""
    r = min(r, h, w / 2)
    t = base - h
    return (f"M{x:.1f} {base:.1f}L{x:.1f} {t + r:.1f}Q{x:.1f} {t:.1f} {x + r:.1f} {t:.1f}L{x + w - r:.1f} {t:.1f}"
            f"Q{x + w:.1f} {t:.1f} {x + w:.1f} {t + r:.1f}L{x + w:.1f} {base:.1f}Z")


def _fill(i: int, shape: str, geometry: str, partial: bool) -> str:
    """A series-coloured mark: its class for dark mode and its light colour as a plain attribute."""
    return f'<{shape} class="f{i}" {geometry} fill="{_LIGHT[i]}"{_PARTIAL if partial else ""}/>'


def _segments(row: dict, keys: list[str]) -> list[tuple[int, float]]:
    """``(series index, value)`` of what a row's stack draws, from ``data`` only."""
    return [(i, row[k]) for i, k in enumerate(keys) if _is_num(row.get(k)) and row[k] > 0]


def _label_picks(n: int, label_width: float, slot: float) -> list[int]:
    """The rows that get an axis label: evenly spaced so that none overlap, the first and the last always."""
    every = max(1, math.ceil((label_width + 6) / slot)) if slot > 0 else 1
    picks = list(range(0, n, every))
    if picks[-1] != n - 1:
        if len(picks) > 1:
            picks[-1] = n - 1
        else:
            picks.append(n - 1)
    return picks


def _svg_bars(item: dict) -> str:
    """Horizontal bars, stacked when split; each bar's base on the axis, its value at the tip."""
    chart, rows, partial = item["chart"], item["data"], set(item["partial"])
    keys = chart["y_keys"]
    body, y = _head(item)
    tips = _tips(item)
    labels = [s + ("*" if i in partial else "") for i, s in enumerate(_shown_all([row[chart["x_key"]] for row in rows]))]
    left = _PAD + max(_width(lb) for lb in labels) + 6
    right = _W - _PAD - max(_width(_fmt(v)) for v in tips) - 8
    numbers = [v for v in tips if _is_num(v)]
    top, step = _axis(max(numbers, default=0), _is_whole(numbers))
    scale = (right - left) / top
    y0 = y + 6
    y1 = y0 + _BAND * len(rows)
    for k in range(int(round(top / step)) + 1):
        gx = left + k * step * scale
        body.append(_rule("g", gx, y0 - 4, gx, y1))
        body.append(_ink(gx, y1 + 14, _tick(k * step), "m", "middle", 12))
    for i, row in enumerate(rows):
        by = y0 + i * _BAND + 4
        body.append(_ink(left - 6, by + 13, labels[i], anchor="end"))
        part, v = i in partial, tips[i]
        x = left
        drawn = _segments(row, keys)   # with no split a bar is a stack of one
        for pos, (si, sv) in enumerate(drawn):
            w = sv * scale
            if pos == len(drawn) - 1:
                body.append(_fill(si, "path", f'd="{_hbar(x, by, max(w, 1), _BAR)}"', part))
            else:
                geometry = f'x="{x:.1f}" y="{by:.1f}" width="{max(w - 2, 1):.1f}" height="{_BAR}"'
                body.append(_fill(si, "rect", geometry, part))
            x += w
        body.append(_ink(left + (v * scale if _is_num(v) else 0) + 6, by + 13, _fmt(v)))
    body.append(_rule("a", left, y0 - 4, left, y1))
    return _foot(item, y1 + 34, body)


def _label_lines(at: list[float], labels: list[str]) -> int:
    """How many lines axis labels centred at ``at`` need to take turns on, so that none touches the next on its line."""
    half = [_width(lb, 12) / 2 for lb in labels]
    n = len(labels)
    fits = (k for k in range(1, n) if all(at[i + k] - at[i] >= half[i] + half[i + k] + 4 for i in range(n - k)))
    return next(fits, n)


def _column_frame(item: dict) -> tuple[list[Any], Any, Any, list[str], float, float]:
    """``(tips, top, step, ticks, left, slot)`` of a column chart: what its picture and :func:`_valued` both read."""
    tips = _tips(item)
    numbers = [v for v in tips if _is_num(v)]
    top, step = _axis(max(numbers, default=0), _is_whole(numbers))
    ticks = [_tick(k * step) for k in range(int(round(top / step)) + 1)]
    left = _PAD + max(_width(t, 12) for t in ticks) + 6
    return tips, top, step, ticks, left, (_W - _PAD - 8 - left) / len(tips)


def _valued(item: dict) -> set[int]:
    """The columns that carry their value at the tip, the words ``no value`` of a null included. A text that fits its
    own slot goes there unless it meets a value already placed or a taller column. A wider one reaches over its
    neighbours, so only the highest, the first and the last (as on a line) try, by the same test; the rows hold the
    rest."""
    tips, top, _step, _ticks, left, slot = _column_frame(item)
    n, scale, width = len(tips), _PLOT_H / top, min(_COLUMN, slot * 0.7)
    texts = [_fmt(v) for v in tips]
    heights = [v * scale if _is_num(v) else 0 for v in tips]
    middle = [left + slot * (i + 0.5) for i in range(n)]
    at = [_centre(middle[i], left - 4, texts[i]) for i in range(n)]
    half = [_width(t) / 2 for t in texts]
    highest = max(range(n), key=lambda i: tips[i] if _is_num(tips[i]) else -1)
    placed: set[int] = set()
    for i in dict.fromkeys([highest, 0, n - 1, *range(n)]):
        if 2 * half[i] > slot - 2 and i not in (highest, 0, n - 1):
            continue
        over_value = any(abs(at[i] - at[j]) < half[i] + half[j] + 2 and abs(heights[i] - heights[j]) < 14
                         for j in placed)
        # the text's bottom is 2 px over its own column's top; a column that reaches that high runs through it
        over_column = any(abs(at[i] - middle[j]) < half[i] + width / 2 and heights[j] > heights[i] + 2
                          for j in range(n) if j != i)
        if not over_value and not over_column:
            placed.add(i)
    return placed


def _null_words_lost(item: dict) -> bool:
    """A null row of a column chart has no room for the words ``no value``: it looks like a 0 unless a note says so."""
    placed = _valued(item)
    return any(v is None and i not in placed for i, v in enumerate(_tips(item)))


def _svg_columns(item: dict) -> str:
    """Columns (periods or a few groups), stacked when split; each column's base on the axis, its value at the tip
    where :func:`_valued` finds room, written after every column is painted so that none covers a value."""
    chart, rows, partial = item["chart"], item["data"], set(item["partial"])
    keys = chart["y_keys"]
    body, y = _head(item)
    tips, top, step, ticks, left, slot = _column_frame(item)
    n = len(rows)
    right = _W - _PAD - 8
    y0 = y + 20
    y1 = y0 + _PLOT_H
    scale = _PLOT_H / top
    width = min(_COLUMN, slot * 0.7)
    for k, tick in enumerate(ticks):
        gy = y1 - k * step * scale
        body.append(_rule("g", left, gy, right, gy))
        body.append(_ink(left - 6, gy + 4, tick, "m", "end", 12))
    labels = [s + ("*" if i in partial else "") for i, s in enumerate(_shown_all([row[chart["x_key"]] for row in rows]))]
    wide = max(_width(lb, 12) for lb in labels)
    # periods can skip a label; a group or segment never does, so its labels take turns on the lines they need
    periods = item["tool"] == "trend_records"
    named = set(_label_picks(n, wide, slot)) if periods else set(range(n))
    at = [_centre(left + slot * (i + 0.5), 2, lb, 12) for i, lb in enumerate(labels)]
    lines = 1 if periods else _label_lines(at, labels)
    for i, row in enumerate(rows):
        cx = left + slot * (i + 0.5)
        part = i in partial
        base = y1
        drawn = _segments(row, keys)   # with no split a column is a stack of one
        for pos, (si, sv) in enumerate(drawn):
            h = sv * scale
            if pos == len(drawn) - 1:
                body.append(_fill(si, "path", f'd="{_vbar(cx - width / 2, base, width, max(h, 1))}"', part))
            else:   # the 2 px gap on top, and never under its own base
                tall = max(h - 2, min(h, 1))
                geometry = (f'x="{cx - width / 2:.1f}" y="{base - tall:.1f}" width="{width:.1f}" '
                            f'height="{tall:.1f}"')
                body.append(_fill(si, "rect", geometry, part))
            base -= h
        if i in named:
            body.append(_ink(at[i], y1 + 15 + 14 * (i % lines), labels[i], "m", "middle", 12))
    for i in sorted(_valued(item)):
        cx, v = left + slot * (i + 0.5), tips[i]
        text = _fmt(v)
        body.append(_ink(_centre(cx, left - 4, text), y1 - (v * scale if _is_num(v) else 0) - 5, text, anchor="middle"))
    body.append(_rule("a", left, y1, right, y1))
    return _foot(item, y1 + 36 + 14 * (lines - 1), body)


def _value_y(points: list[tuple], k: int, x: float, text: str, floor: float) -> float:
    """Where the value of ``points[k]`` centred at ``x`` goes: over its point, or under it when the line beside the
    point runs through the text and the axis leaves room. The line is walked point by point to each end of the text,
    because with many points one step is too short to see it."""
    _i, _px, py, _v = points[k]
    for way in (-1, 1):
        end = x + way * _width(text) / 2
        j = k
        while 0 <= j + way < len(points) and abs(points[j + way][0] - points[j][0]) == 1:
            ax, ay, qx, qy = points[j][1], points[j][2], points[j + way][1], points[j + way][2]
            reach = (end - ax) * way
            if reach <= 0:
                break
            if ay + (qy - ay) * min(1.0, reach / abs(qx - ax)) <= py - 6 and py + 21 <= floor:
                return py + 18
            j += way
    return py - 9


def _end_labels(ends: list[tuple], floor: float) -> list[tuple[float, float, str]]:
    """``(x, y, text)`` of each line's last value beside its point. The same value at the same point is written once.
    Labels are pushed apart only against those whose x ranges meet theirs, around their own heights and never under the
    axis (``floor``), so none reads as a tick or runs over the last axis label. A label whose line cannot sit within
    14 px of its point is left out: the rows hold it."""
    wanted = sorted({(px + 7, py + 4, _fmt(v)) for _i, px, py, v in ends})
    piles: list[list[tuple]] = []
    reach = 0.0
    for label in wanted:
        if label[0] < reach:
            piles[-1].append(label)
        else:
            piles.append([label])
        reach = max(reach, label[0] + _width(label[2]))
    out = []
    for pile in piles:
        pile.sort(key=lambda lb: lb[1])
        ys = [lb[1] for lb in pile]
        for k in range(1, len(ys)):
            ys[k] = max(ys[k], ys[k - 1] + 14)
        over = ys[-1] - pile[-1][1]
        ys = [y - over / 2 for y in ys]
        ys[-1] = min(ys[-1], floor)
        for k in range(len(ys) - 2, -1, -1):
            ys[k] = min(ys[k], ys[k + 1] - 14)
        out += [(lb[0], y, lb[2]) for lb, y in zip(pile, ys) if abs(y - (lb[1] - 4)) <= 14 + 1e-6]
    return out


def _svg_lines(item: dict) -> str:
    """Lines from 0: one per series, a gap at a null, a dashed segment and a hollow marker on a partial period, markers
    up to 40 points. One series: the first and last value and, where it fits, the highest; several: each line's last
    value, where it fits."""
    chart, rows, partial = item["chart"], item["data"], set(item["partial"])
    keys, n = chart["y_keys"], len(rows)
    absent_zero = bool(item.get("_absent_zero"))
    body, y = _head(item)
    # a key a row lacks is 0 only on a line per series under count (the one 0 code draws: the add-up test proves that
    # absent there means no records); null, and absent elsewhere, is a gap
    table = [[row.get(k, 0 if absent_zero else None) for row in rows] for k in keys]
    numbers = [v for line in table for v in line if _is_num(v)]
    top, step = _axis(max(numbers, default=0), _is_whole(numbers))
    ticks = [_tick(k * step) for k in range(int(round(top / step)) + 1)]
    labels = [s + ("*" if i in partial else "") for i, s in enumerate(_shown_all([row[chart["x_key"]] for row in rows]))]
    left = max(_PAD + max(_width(t, 12) for t in ticks) + 6, _width(labels[0], 12) / 2 + 2)
    widest = max((_width(_fmt(v)) for v in numbers), default=0)
    right = _W - max(_PAD + widest + 8 if len(keys) > 1 else _PAD + widest / 2, _width(labels[-1], 12) / 2 + 2)
    y0, dx = y + 20, (right - left) / (n - 1)
    y1 = y0 + _PLOT_H
    scale = _PLOT_H / top
    for k, tick in enumerate(ticks):
        gy = y1 - k * step * scale
        body.append(_rule("g", left, gy, right, gy))
        body.append(_ink(left - 6, gy + 4, tick, "m", "end", 12))
    ends = []
    for si, line in enumerate(table):
        points = [(i, left + i * dx, y1 - v * scale, v) for i, v in enumerate(line) if _is_num(v)]
        for (i, x0, py0, _v), (j, x1, py1, _w) in zip(points, points[1:]):
            if j == i + 1:
                dash = ' stroke-dasharray="4 4"' if i in partial or j in partial else ""
                body.append(f'<path class="l{si}" d="M{x0:.1f} {py0:.1f}L{x1:.1f} {py1:.1f}" fill="none" '
                            f'stroke="{_LIGHT[si]}" stroke-width="2" stroke-linecap="round"{dash}/>')
        lone = {p[0] for k, p in enumerate(points)
                if (k == 0 or points[k - 1][0] != p[0] - 1) and (k == len(points) - 1 or points[k + 1][0] != p[0] + 1)}
        for i, px, py, _v in points:
            if n > 40 and i not in lone and i not in partial:
                continue
            if i in partial:
                body.append(f'<circle class="h l{si}" cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{_SURFACE}" '
                            f'stroke="{_LIGHT[si]}" stroke-width="2"/>')
            else:
                body.append(f'<circle class="f{si} s" cx="{px:.1f}" cy="{py:.1f}" r="4" fill="{_LIGHT[si]}" '
                            f'stroke="{_SURFACE}" stroke-width="1"/>')
        if not points:
            continue
        if len(keys) > 1:
            ends.append(points[-1])
            continue
        highest = max(points, key=lambda p: p[3])
        shown = [points[0], points[-1]]
        if all(abs(highest[1] - p[1]) >= (_width(_fmt(highest[3])) + _width(_fmt(p[3]))) / 2 + 2
               or abs(highest[2] - p[2]) >= 14 for p in shown):
            shown.append(highest)
        for p in dict.fromkeys(shown):
            at = _centre(p[1], left - 4, _fmt(p[3]))
            body.append(_ink(at, _value_y(points, points.index(p), at, _fmt(p[3]), y1), _fmt(p[3]), anchor="middle"))
    for x, ty, text in _end_labels(ends, y1):
        body.append(_ink(x, ty, text))
    for i in _label_picks(n, max(_width(lb, 12) for lb in labels), dx):
        body.append(_ink(left + i * dx, y1 + 15, labels[i], "m", "middle", 12))
    body.append(_rule("a", left, y1, right, y1))
    return _foot(item, y1 + 36, body)


def _svg_pie(item: dict) -> str:
    """A pie or donut: a slice per row, a legend with label and value, no percent and no centre number. One slice that
    is all of it draws a circle, because an arc of 360 degrees draws nothing."""
    chart, rows = item["chart"], item["data"]
    x_key, y_key = chart["x_key"], chart["y_keys"][0]
    body, y = _head(item)
    values = [row[y_key] for row in rows]
    whole = sum(values)
    outer, inner, cx, cy = 100, 58, _PAD + 112, y + 112
    donut = chart["type"] == "donut"
    big = max(range(len(values)), key=lambda i: values[i])
    if values[big] / whole >= 0.9999:   # one slice is all of it: its arc would start and end on one point
        if donut:
            mid, band = (outer + inner) / 2, outer - inner
            body.append(f'<circle class="l{big}" cx="{cx}" cy="{cy:.1f}" r="{mid}" fill="none" stroke="{_LIGHT[big]}" '
                        f'stroke-width="{band}"/>')
        else:
            body.append(f'<circle class="f{big}" cx="{cx}" cy="{cy:.1f}" r="{outer}" fill="{_LIGHT[big]}"/>')
    else:
        a = -math.pi / 2
        for i, v in enumerate(values):
            if v <= 0:
                continue
            b = a + 2 * math.pi * v / whole
            large = 1 if v / whole > 0.5 else 0
            x0, y0 = cx + outer * math.cos(a), cy + outer * math.sin(a)
            x1, y1 = cx + outer * math.cos(b), cy + outer * math.sin(b)
            if donut:
                x2, y2 = cx + inner * math.cos(b), cy + inner * math.sin(b)
                x3, y3 = cx + inner * math.cos(a), cy + inner * math.sin(a)
                d = (f"M{x0:.2f} {y0:.2f}A{outer} {outer} 0 {large} 1 {x1:.2f} {y1:.2f}L{x2:.2f} {y2:.2f}"
                     f"A{inner} {inner} 0 {large} 0 {x3:.2f} {y3:.2f}Z")
            else:
                d = f"M{cx} {cy:.2f}L{x0:.2f} {y0:.2f}A{outer} {outer} 0 {large} 1 {x1:.2f} {y1:.2f}Z"
            body.append(f'<path class="f{i} s" d="{d}" fill="{_LIGHT[i]}" stroke="{_SURFACE}" stroke-width="2"/>')
            a = b
    ly = y + 22
    names = _fit_all([row[x_key] for row in rows], _CUT_LEGEND)
    column = min(_W - _PAD, 270 + max(_width(t) for t in names) + 16 + max(_width(_fmt(v)) for v in values))
    for i, name in enumerate(names):
        body.append(_swatch(i, 252, ly))
        body.append(_ink(270, ly, name))
        body.append(_ink(column, ly, _fmt(values[i]), anchor="end"))
        ly += 22
    return _foot(item, max(cy + outer, ly) + 26, body)


def _draw(item: dict) -> str:
    """The SVG of an item with a chart, from its ``data``, ``partial``, title, subtitle and notes only. Raises on a
    malformed item (:func:`make` and :func:`for_tool` catch it)."""
    kind = item["chart"]["type"]
    if kind == "horizontal_bar":
        return _svg_bars(item)
    if kind == "bar":
        return _svg_columns(item)
    if kind == "line":
        return _svg_lines(item)
    return _svg_pie(item)


def _check_svg(svg: Any) -> bool:
    """The check on every SVG, the real defence, because the SVG also leaves as text on MCP: it parses, its tags and
    attributes come from fixed lists, its style is the module's constant and no attribute points anywhere."""
    try:
        root = ET.fromstring(svg)
        if root.tag != "{http://www.w3.org/2000/svg}svg":
            return False
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            if tag not in _TAGS or any(a not in _ATTRS or "url(" in v.lower() for a, v in el.attrib.items()):
                return False
            if tag == "style" and el.text != _CSS:
                return False
        return True
    except Exception:  # noqa: BLE001 - not an SVG this module draws
        return False


# ------------------------------------------------------------------------------------------------ the two surfaces
def _finish(item: dict, picture: str) -> dict:
    """An item as it leaves this module: drawn once and checked (``picture``: the key it goes under), with ``alt``, and
    without the keys only the renderer reads. A fault in the picture leaves the rows, the ``fault`` reason and, under
    ``_error``, the class name."""
    out = dict(item)
    if out["chart"]:
        try:
            if out["chart"]["type"] == "bar" and _null_words_lost(out):
                out["notes"] = [*out["notes"], _WORDS["null_no_column"]]
            svg = _draw(out)
            if not _check_svg(svg):
                raise _SvgRefused
            out[picture] = svg if picture == "svg" else base64.b64encode(svg.encode("utf-8")).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - never a broken image
            out["_error"] = type(exc).__name__
            out["chart"], out["reason"] = None, _WORDS["fault"]
    title = out.get("_title")
    parts = [s if s.endswith((".", "!", "?")) else s + "."
             for s in ([title] if title else []) + out["subtitle"] + out["notes"]]
    out["alt"] = " ".join(parts) or out["reason"]
    error = out.get("_error", "")
    out = {k: v for k, v in out.items() if not k.startswith("_")}
    return {**out, "_error": error} if error else out


def _answer_word(kind: Any, rec: Any) -> str:
    """Why this answer can draw nothing whatever its results say, or "": records off, then only failed calls, then a
    record step that failed after its calls (``pipeline.records_failed``: no answer to draw beside), then a knowledge
    answer, no answer or nothing read."""
    if rec is None:
        return "records_off"
    data = [e for e in rec.evidence or [] if e.kind == "data" and e.tool != "get_sync_status"]
    done = [e for e in data if e.ok]
    if data and not done:
        return "failed"
    if kind == "none" and done and getattr(rec, "error", None) and not getattr(rec, "routed_out", False):
        return "answer_failed"
    if kind in ("knowledge", "none") or not done:
        return "no_data"
    return ""


def _code_wrote(kind: Any, rec: Any) -> bool:
    """The answer is code's own text, not the model's: the people answer (``code_answer``), or a record answer with no
    reply behind it (``no_records``: the not-found or the people answer of ``pipeline.choose_answer``). A name in it can
    hold a chart word, and only the model talks about a chart."""
    return bool(getattr(rec, "code_answer", False)) or (kind == "records" and bool(getattr(rec, "no_records", False)))


def make(ask: Any, question: Any, final: Any, kind: Any, mode: Any, rec: Any, catalog: Any, today: Any) -> \
        tuple[Any, list[dict], list[str], str]:
    """``(final, items, lines, error)`` of a chart question, just before ``pipeline.answer`` sets its body: ``final``
    without chart talk when the text is the record agent's; ``items``, the ``Outcome.charts`` entries with their picture
    (``svg_b64``); at most one reason line; the class name of the first fault it caught, also in a result it did not
    draw. Runs after the contact rule. Never raises: a fault gives the ``fault`` line and ``final`` otherwise whole."""
    try:
        if (kind == "records" or (kind == "merged" and mode == "lean")) and not _code_wrote(kind, rec):
            final = drop_talk(final, getattr(rec, "table_blocks", None))
        else:   # a design answer keeps its sentences, and code's own text its words
            final = _marks_out(final)
        word = _answer_word(kind, rec)
        if word:
            return final, [], [_WORDS[word]], ""
        from sf_assistant.agent.tables import holds
        evidence = list(rec.evidence or [])
        clean = _Cleaner(question, [e.model_result for e in evidence if e.ok])
        labels = _Labels(catalog)
        asked = getattr(ask, "type", None)
        results = [_safe_build(e.tool, e.model_result, e.arguments, e.object, clean, labels, asked, today)
                   for e in evidence if e.ok and e.kind == "data" and e.tool != "get_sync_status"]
        blocks = getattr(rec, "table_blocks", None) or {}
        items, lines = [], []
        error = next((i["_error"] for found in results for i in found if i.get("_error")), "")
        if isinstance(question, str) and _RATE_ASK.search(question):
            results = _rate_results(results)
        for item in _pick(results):
            done = _finish(item, "svg_b64")
            fault = done.pop("_error", "")   # always: no key of this module's leaves it with an item
            error = error or fault
            if not done["chart"] and not lines:
                lines = [done["reason"]]
            if done["data"]:
                block = blocks.get("{{table " + str(done["query_id"]) + "}}")
                done["rows_in_answer"] = bool(block) and holds(final, block)
                items.append(done)
        return final, items, lines, error
    except Exception as exc:  # noqa: BLE001 - a question never fails on its chart
        return final, [], [_WORDS["fault"]], type(exc).__name__


def for_tool(tool: Any, result: Any, arguments: Any, catalog: Any, today: Any) -> dict:
    """One payload for an MCP envelope's ``chart`` key, with its picture as SVG text (``svg``). The question is "", so
    contact details are never shown. Cannot draw: ``chart`` null, the ``reason`` and ``data`` if any. Never raises."""
    try:
        clean = _Cleaner("", [result])
        item = _pick([_safe_build(tool, result, arguments, None, clean, _Labels(catalog), None, today)])[0]
        done = _finish(item, "svg")
        done.pop("_error", None)
        return done
    except Exception:  # noqa: BLE001 - the usual envelope, with the reason
        return {**_no_chart(tool, result, "fault"), "alt": _WORDS["fault"]}


def for_jsonl(items: Any) -> list[dict]:
    """``items`` as a sidecar holds them: the same class of value as ``record_facts``, no picture. Never raises."""
    try:
        return [{k: v for k, v in item.items() if k not in ("svg_b64", "svg")} for item in items or []]
    except Exception:  # noqa: BLE001
        return []


def _row_lines(rows: list[dict]) -> list[str]:
    """The rows as text, one line per row under a line of their keys; a key a row lacks stays an empty cell."""
    keys = list(dict.fromkeys(k for row in rows for k in row))
    out = [" | ".join(keys)]
    for row in rows:
        cells = ["" if k not in row else f"({_WORDS['no_value']})" if row[k] is None else str(row[k]) for k in keys]
        out.append(" | ".join(cells))
    return out


def text_lines(items: Any) -> list[str]:
    """The items as the command line prints them: heading, subtitle, notes and rows. Text only: no file, because
    containers are read-only here and ``*.svg`` is not git-ignored. Never raises."""
    try:
        out: list[str] = []
        for item in items or []:
            chart = item.get("chart")
            if chart:
                out.append(_say("cli_chart", TYPE=_PLAIN_TYPE.get(chart["type"], chart["type"]), TITLE=chart["title"]))
            out += list(item.get("subtitle") or []) + list(item.get("notes") or [])
            if item.get("data") and not item.get("rows_in_answer"):
                out += [_WORDS["cli_rows"], *_row_lines(item["data"])]
        return out
    except Exception:  # noqa: BLE001
        return []


def with_chart_arg(tool: dict) -> dict:
    """A copy of ``tool`` (an MCP listing entry) with the optional ``chart`` argument; the registry's dict stays as it
    is."""
    schema = dict(tool["inputSchema"])
    schema["properties"] = {**(schema.get("properties") or {}), "chart": dict(_CHART_ARG)}
    return {**tool, "inputSchema": schema}
