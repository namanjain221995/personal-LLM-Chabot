"""How to show an answer, split from what to fetch (prerequisite P3 of 2026-11-25): one home for the display words.

Before this, a question's display words reached every search: "artifact" was searched as a Salesforce field, "plot a
graph" went to the route call and the knowledge search, and "can you do visualizations ?" was rewritten into a
question nobody asked. :func:`split` now takes them out of the question BEFORE the knowledge and record search and
gives them back as flags (:class:`Display`): the search text feeds the spelling help, the knowledge question, the route
and names calls, the people and value spots, the record hints, ``begin_question`` and the record agent's draft checks;
the flags feed the chart note, the chart overrule, the record agent's display note (:data:`RECORDS_DISPLAY_NOTE`,
:func:`note`) and the file line. The record agent's user message, the merge, the contact rule, ``charts.make`` and the
JSONL keep the typed text (the record agent's message since the pre-release check of 2026-11-25, with ``SFA_CHARTS``
on: read as "show X of last 6 months", "plot X of last 6 months" got a record list instead of a monthly trend with a
chart; off, it reads the search text as before).

The words (the only list; ``agent/charts.py`` and ``followup._CHART_WORDS`` read theirs from here):

- **free words** are taken out wherever they stand, unless a guard keeps them: chart, graph, plot, diagram, histogram,
  visualize (and its forms and typos), visualization, "visually", "trend line", a chart type inside a chart phrase
  ("bar chart", "as a pie", "switch to line"), artifact (and typos), tabular / tabulate, xlsx / xls. None of them is a
  field word of the knowledge files;
- **gated words** are taken out only in a display shape, because each is also a field, flow, feature or skill word of
  this org: table ("as a table", "make a table of", "table me dikhao"), dashboard (the same shapes; never "the
  dashboard", an org feature), detail ("in detail", "with full details", "detailed report", "detail me batao"; never
  "details of" or "<noun> details"), summary, overview, report, brief ("summarize", "a summary of", "as a report",
  "in short"; never "reports to", "the report", "<noun> summary", Hinglish "<noun> summary batao" or "X ka summary
  batao", which name a field), breakdown (only with no grouping beside it: never "breakdown by", "monthwise breakdown",
  "breakdown of X by Y", "niche breakdown of X") and excel / csv / spreadsheet ("in excel", "export X to csv", "excel
  me bhejo", "make an excel of X"; never after know, use, learn or a skill word: "candidates who are good in excel").
  A table is also read at the head of a clause ("table of interviews by month") and at the end of a grouped or dated
  ask ("interviews by month table"), and after a removed display phrase ("... in a pie chart and a table").

Guards (a word is kept, no flag is set from it, and :attr:`Display.kept` says why): ``quoted`` (inside quotes),
``api`` (part of a token with ``_``), ``about`` (a question about the display thing itself: "what is a dashboard",
"how do I add a chart to a report in salesforce"; its third reading holds within one clause that asks for no data),
``not_a_chart`` (flow chart, org chart, plot number, GraphQL ...), ``grouping`` (breakdown by / per / for each / -wise,
or a field word right before it: "a status breakdown of X"), ``gate`` (a gated word outside its shapes), ``skill``
(visualization, visually outside the chart grammar's own shapes: "visually impaired", "visualization engineer"),
``refused`` (a refusal the chart grammar reads: "hide the pie chart", "stop showing charts", "graph hata do": the
chart flag is refused and the chart words stay), and with a record session
(:class:`SessionGuards`): ``value`` (inside a stored record value the question names; a value that is only the file
word of a file shape, a company named "Excel" in "send X to excel", keeps the word and still sets the file flag),
``name`` (inside a stored person's name) and ``invariant`` (the search text would change what the contact rule or the
business-term contracts read, or lose a number or a quoted span: the split is dropped whole, the flags stay).

What is left must read as a sentence: the article and connector of a removed phrase go with it, a removed display verb
at the head of a clause becomes "show" (Hinglish: "dikhao"), a clause left with filler only goes whole ("..., monthwise.
and also plot a graph for it" -> "..., monthwise."), and the user's words are never reordered.

The chart ask grammar of ``agent/charts.py`` (:func:`fold`, :func:`fires`, ``_ASK``, ``_STOP``, ``_ABOUT``,
``_NOT_A_CHART``, ``_FILLER`` ...) lives here too, unchanged, and charts imports it back under the same names: with
``SFA_CHARTS=off`` the charts module is never imported, while the display split runs on every answer.

Standard library, :mod:`sf_assistant.prompts` and :func:`sf_assistant.knowledge.resolver.osa_distance` only: a
records-off run imports this module and loads no engine module (:class:`SessionGuards` imports the contact rule and
the term contracts inside its methods, which run only with a record session).
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import Any

from sf_assistant import prompts as _prompt_files
from sf_assistant.knowledge.resolver import osa_distance

__all__ = ["DISPLAY_WORDS_TEXT", "RECORDS_DISPLAY_NOTE", "LINES", "NOTE_LINES", "CHART_VERBS", "Display", "NO_DISPLAY",
           "SessionGuards", "split", "note", "line", "bare_key", "capability_key", "generic_ask", "fold", "fires"]

DISPLAY_WORDS_TEXT = _prompt_files.text("display_words")
#: The record agent's note on how to lay out its answer (one lead line and one line per flag; prompts/records_display_note.md).
RECORDS_DISPLAY_NOTE = _prompt_files.text("records_display_note")
LINES: dict[str, str] = dict(ln.split(": ", 1) for ln in DISPLAY_WORDS_TEXT.split("\n") if ": " in ln)
NOTE_LINES: dict[str, str] = dict(ln.split(": ", 1) for ln in RECORDS_DISPLAY_NOTE.split("\n") if ": " in ln)
#: The verbs of a chart ask the follow-up rewrite reads (``followup._CHART_WORDS``).
CHART_VERBS = frozenset("plot plots chart charts graph graphs visualise visualize draw".split())


# ------------------------------------------------------------------------------------------------ the chart ask grammar
# Moved verbatim from agent/charts.py (prerequisite P3, 2026-11-25); charts imports every name back.
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
_FILLER = frozenset("""a an the this that it them these those now then please pls plz me my mujhe ek isko iska iski uska uski
    usko is us ye yeh wo woh ka ki ke ko mein se aur bhi and also or as in on of for with to from show give make
    draw create get need want can could you i we do display see view plot plots chart charts graph graphs visualize
    visualise visually graphically graphical bar bars pie line lines donut doughnut column columns horizontal vertical
    stacked dikhao dikha dikhana dikhado dikhaye banao bana bnao banado de karo kar kardo chahiye dena dedo
    using into same above previous last earlier again one data records numbers table list dono both
    just only sirf plus hai kya
    thing things answer answers result results mai main wale wala upar pichle pichla able are have tum aap sakte sakta
    sakti ho was not na no showing visible empty wrong visualization visualisation visual plotting plotted""".split())


def fold(q):
    t = (q or "").lower().replace("’", "'")
    for p, r in _FOLD:
        t = p.sub(r, t)
    return re.sub(r"\s+", " ", t).strip()


def fires(t):
    if _STOP.search(t) or _ABOUT.search(t):
        return False
    return bool(_ASK.search(_NOT_A_CHART.sub(" ", t)))


# ------------------------------------------------------------------------------------------------ the display words
#: The longest question read (the page's cap); a longer one is searched as typed.
_MAX_TEXT = 1000
_ART = r"(?:a|an|the|some|one|ek)"
_ADJ = (r"(?:full|short|brief|quick|small|simple|complete|detailed|visual|graphical|pictorial|proper|nice|clear|good|"
        r"neat|big|single|separate|new|interactive|beautiful|colou?rful)")
#: Time adjectives and "<word>wise" group the data: they stay in the search text when the noun they stand before goes
#: ("a monthly report of X" -> "monthly X", "a monthwise graph for X" -> "monthwise X").
_TIME_ADJ = r"(?:monthly|weekly|daily|quarterly|yearly|annual|[a-z]+-?wise)"
_TIME_ADJ_RE = re.compile(rf"\b{_TIME_ADJ}\b")
_TYPE = r"(?:bar|bars|pie|line|lines|column|columns|donut|doughnut|area|stacked|horizontal|vertical|scatter|funnel)"
_MOD = rf"(?:(?:{_ADJ}|{_TIME_ADJ}|{_TYPE})\s+){{0,3}}"
_N_CHART = (r"(?:charts?|chrats?|chartt|chrt|chatr|charrt|cahrt|chaart|graphs?|grafh|garaf|garph|grpah|grph|grahp|"
            r"grapgh|graphh|graps?|plots?|plto|diagrams?|histograms?|histogarm|histgram|infographics?|"
            r"vi[sz]u?a?li[sz]ations?|visualzations?|visuals|trend\s*lines?|heat\s*maps?|"
            # "bar chat", "pie chat": a chart typed as a chat right after a type word (agent/charts.py folds the same)
            r"(?:(?<=bar )|(?<=pie )|(?<=line )|(?<=area )|(?<=column )|(?<=donut )|(?<=scatter ))chat(?=\s*(?:$|[,.;:!?]|of\b|for\b|by\b|me\b|mein\b|ka\b|ki\b|ke\b|banao\b|bana\b|dikhao\b|please\b)))")
#: "viz" ("create a viz of X"): only after a make or show verb or a preposition, never alone ("viz." is "namely")
_N_VIZ = r"(?:viz|vizs)"
_N_ART = r"(?:artifacts?|artefacts?|artifcats?|artfacts?|artifects?|artefects?|artificats?)"
_N_TABLE = r"(?:tables?|tabels?|tabbles?|tbles?)"
_N_DASH = r"(?:dashboards?|dashbords?|dashbaords?|dashbaord|dash\s+boards?)"
_N_SUM = r"(?:summary|summaries|sumary|summery|overview|reports?|reprot|brief)"
_N_FILE = r"(?:excel|exel|excell|csv|spreadsheets?|google\s+sheets?|xlsx|xls)"
_N_BREAK = r"(?:breakdown|break-down)"
_N_DETAIL = r"(?:details?|detials?)"
#: Every display noun (the refusals read them all)
_N_ANY = rf"(?:{_N_CHART}|{_N_ART}|{_N_TABLE}|{_N_DASH})"
_V_MAKE = r"(?:make|create|build|prepare|generate|draw|plot|do|produce|render|design|bana|banao)"
_V_GIVE = r"(?:show|give|send|share|get|display|provide|present)"
_HINDI_VERB = (r"(?:dikhao|dikha\s+do|dikhado|dikha\s+dijiye|dikhaiye|dikhana|batao|bata\s+do|bataiye|samjhao|do|dedo|"
               r"de\s+do|dijiye|bhejo|bhej\s+do|bhejiye|banao|bana\s+do|bana\s+ke\s+do|bnao|banado|banaiye|chahiye|"
               r"chaiye|draw\s+karo|karo|kar\s+do)")
#: The end of a clause or a word that shows a type word is the picture ("as a pie", "switch to line")
_TYPE_END = r"(?=\s*(?:$|[,.;:!?]|please\b|pls\b|plz\b|me\b|mein\b|by\b|per\b|for\s+(?:the|last|this|past|each|every|all)\b))"
#: The end of a refusal: "no chart, just the count", "chart nahi chahiye", "without a table please"
_REFUSAL_END = (r"(?=\s*(?:$|[,.;:!?]|please\b|pls\b|needed\b|required\b|chahiye\b|just\b|only\b|sirf\b|and\b|but\b|"
                r"ke\b|batao\b|dikhao\b|bhejo\b|in\s+the\s+answer\b))")

_REFUSE = re.compile(
    r"(?:\bno\s+need\s+(?:for|of)\s+|\bno\s+need\s+to\s+(?:show|make|draw|give|send)\s+|\bno\s+|\bwithout\s+|\bbina\s+"
    r"|\b(?:don'?t|dont|do\s+not)\s+(?:need|want|show|make|give|draw|use|send|add)\s+(?:me\s+)?|\bskip\s+|\bnot\s+(?:as|in)\s+"
    r"|\binstead\s+of\s+|\brather\s+than\s+)(?:an?\s+|the\s+|any\s+)?" + _MOD + r"(?P<n>" + _N_ANY + r")" + _REFUSAL_END
    + r"|\b(?P<n2>" + _N_ANY + r")\s+(?:ki\s+(?:zaroorat|zarurat|jarurat)\s+)?(?:nahi|nahin|nhi|mat)\b"
    r"(?:\s+(?:chahiye|chaiye|banao|dikhao|do|bhejo))?"
    r"|\b(?P<n3>" + _N_ANY + r")\s+(?:ke\s+)?(?:bina|bagair)\b")
#: "what is a dashboard", "which flow updates the trainer dashboard", "how do I add a chart to a report in salesforce"
_ABOUT_WHAT = re.compile(r"^\W*(?:what(?:'s|\s+is|\s+are|\s+does|\s+do)\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,2}?"
                         r"(?:" + _N_ANY + r"|" + _N_SUM + r"|" + _N_BREAK + r")\W*$)")
_ABOUT_ASK = re.compile(r"\b(?:what|which|where|how|kaun|kya|kaise)\b")
_ABOUT_META = re.compile(r"\b(?:salesforce|fields?|objects?|flows?|components?|pages?|layouts?|lightning|setup|apex|"
                         r"metadata|configure|configured|configuration)\b")
_ABOUT_NOUN = re.compile(r"\b(?:" + _N_ANY + r"|" + _N_SUM + r"|" + _N_FILE + r")\b")
_FILE_SKILL = re.compile(r"\b(?:know|knows|knowing|knowledge|use|uses|using|used|learn|learns|learning|learnt|skilled|"
                         r"skill|skills|expert|expertise|experience|experienced|proficient|proficiency|trained|training|"
                         r"certified|certification|course|courses|good|strong|fluent|comfortable|advanced|basic|"
                         r"working|hands-on|hands\s+on|excellent|great|familiar|knowledgeable|beginner|intermediate)\b")
_QUOTED = re.compile(r"\"[^\"\n]{1,300}\"|“[^”\n]{1,300}”|‘[^’\n]{1,300}’|(?<![\w'])'[^'\n]{1,300}'(?![\w'])")
_API = re.compile(r"[^\W_]*_[\w]*")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_GROUPING_AFTER = re.compile(r"^[^.!?;]*?\b(?:by|per|for\s+each|for\s+every|each|har)\b|^[^.!?;]*?\w[\s-]?wise\b")
#: Words that may stand right before "breakdown" without naming what to split by ("a full breakdown", "give me
#: breakdown"); any other word there is the grouping ("a status breakdown of X", "niche breakdown": review of 2026-11-25)
_NOT_GROUPING = frozenset("""a an the me us give show get send share provide display full detailed quick complete
    short simple proper nice clear whole entire brief overall total small big good my our your this that of for and also
    plus with in as ek its their please pls plz any some new visual graphical""".split())
_GROUPING_BEFORE = re.compile(r"(?:\w-?wise|\bwise)\s*$")

#: (rule name, regex, flag source group). Applied in this order; a match that touches a span an earlier one took is
#: skipped. Each match is read by :func:`_op`.
_RULES: list[tuple[str, re.Pattern]] = [
    ("refuse", _REFUSE),
    # "export the interviews to excel": the verb reads as "show", the file words go
    ("export", re.compile(r"\b(?P<v>export|download|extract)\b(?P<mid>(?:\s+(?!(?:to|as|in|into)\b)[^\s,.;:!?]+){0,8}?)"
                          r"\s+(?:to|as|in|into)\s+(?:an?\s+|the\s+)?(?P<n>" + _N_FILE + r")"
                          r"(?:\s+(?:file|sheet|sheets|format|doc|document|workbook))?\b")),
    # Hinglish: "<obj> ka graph dikhao", "pie chart banao", "table me dikhao", "detail me batao", "excel me bhejo"
    ("hinglish", re.compile(r"(?:\b(?P<gen>ka|ki|ke)\s+)?(?:ek\s+)?" + _MOD + r"\b(?P<n>" + _N_CHART + r"|polt|plott|graf|graaf|"
                            + _N_ART + r"|" + _N_TABLE + r"|" + _N_DASH + r"|" + _N_FILE + r"|" + _N_DETAIL + r"|"
                            + _N_SUM + r"|short|" + _N_BREAK + r")(?P<post>\s+(?:file|sheet|format|form|style)?\s*(?:me|mein|"
                            r"mai|main|se|ke\s+roop\s+(?:me|mein)|form\s+(?:me|mein))|\s+(?:me|mein|mai|main|se)|"
                            r"\s+(?:file|sheet))?\s+(?P<hv>" + _HINDI_VERB + r")\b")),
    # "make a table of", "plot a bar graph to show", "give me a summary of", "show me a chart of"
    ("verb", re.compile(r"\b(?P<v>" + _V_MAKE + r"|" + _V_GIVE + r")(?:\s+(?:me|us|mujhe))?\s+(?P<art>" + _ART
                        + r"\s+)?(?P<mod>" + _MOD + r")(?P<n>" + _N_CHART + r"|" + _N_ART + r"|" + _N_TABLE + r"|" + _N_DASH
                        + r"|" + _N_SUM + r"|" + _N_BREAK + r"|" + _N_FILE + r"|" + _N_VIZ + r")\b"
                        r"(?P<suf>\s+(?:format|form|view|style|chart|graph|sheet|file))?"
                        r"(?P<conn>\s+(?:of|for|on|about|showing|with|displaying|to\s+show|that\s+shows?|which\s+shows?))?\b")),
    # "table of interviews by month", "give me table of offers": a table at the head of a clause (E-04)
    ("lead_table", re.compile(r"(?:^|(?<=[.!?;:,]))\s*(?:(?P<v>give|show|send|share|get|provide|make|create|prepare)"
                              r"\s+(?:me\s+|us\s+)?)?(?:an?\s+)?(?P<n>" + _N_TABLE + r")\s+(?:of|for|with|showing)\b")),
    # "as a pie", "in bar form", "switch to line", "make it a bar", "pie of"
    ("type", re.compile(r"\bas\s+(?:an?\s+)?(?:pie|donut|doughnut|bars?|scatter|histogram|funnel|lines?|area|columns?)\b"
                        + _TYPE_END + r"|\bin\s+(?:an?\s+)?(?:bar|pie|line|column)\s+(?:form|format|style)\b"
                        r"|\bswitch\s+(?:it\s+)?to\s+(?:an?\s+)?(?:bars?|lines?|pie|donut|area|scatter)\b" + _TYPE_END
                        + r"|\bmake\s+(?:it|them|that)\s+(?:an?\s+)?(?:horizontal|vertical|stacked|bars?|lines?|pies?|donut|"
                        r"doughnut|area)\b|(?:\b(?:a|an|the)\s+)?\b(?:pie|donut|doughnut)\s+(?:of|for)\b"
                        r"|(?:\b(?:a|an|the)\s+)?\b(?:stacked|horizontal|vertical)\s+(?:bars?|columns?)"
                        r"(?:\s+(?:charts?|graphs?))?(?:\s+(?:of|for)\b)?"
                        r"|\bin\s+(?:an?\s+)?(?:pie|donut|doughnut)\b" + _TYPE_END)),
    # "as a table", "in table format", "in the form of a chart", "into an excel sheet", "in graphical form"
    ("prep", re.compile(r"(?P<pre>,\s*)?\b(?P<p>as|in|into|using|via|with|through|in\s+the\s+form\s+of|in\s+form\s+of|to)\s+"
                        r"(?P<art>(?:a|an|the|some)\s+)?(?P<mod>" + _MOD + r")(?P<n>" + _N_CHART + r"|" + _N_ART + r"|"
                        + _N_TABLE + r"|" + _N_DASH + r"|" + _N_SUM + r"|" + _N_FILE + r"|" + _N_BREAK + r"|" + _N_VIZ
                        + r"|graphical|"
                        r"pictorial|visual|tabular)\b(?P<suf>\s+(?:format|form|view|style|layout|mode|file|sheet|sheets|"
                        r"doc|document|workbook|way|manner|representation))?\b")),
    ("detail", re.compile(r"\bin\s+(?:more\s+|full\s+|great\s+|complete\s+|much\s+)?(?P<n>details?|detials?)\b"
                          r"(?!\s+(?:of|about|for|on)\b)"
                          r"|\bwith\s+(?:full\s+|all\s+(?:the\s+)?|more\s+|complete\s+)?(?P<n2>details|detials)\b"
                          r"(?!\s+(?:of|about|for|on|pending|missing|required|submitted|verified|incomplete|complete|"
                          r"received|awaited|filled|updated|uploaded|shared|provided)\b)"
                          r"|\b(?P<n3>detailed|detaild|detailled)\b(?=\s+(?:report|breakdown|list|view|analysis|summary|"
                          r"table|answer|data|information|info|overview|reply|response|output|explanation|chart|graph|"
                          r"picture|look)\b)|\b(?P<n4>vi?staa?r)\s+se\b"
                          r"|(?:^|(?<=[.!?;:,]))\s*(?P<n5>detailed|detaild|detailled)\b(?=\s+[a-z])")),
    ("summary", re.compile(r"\b(?P<v>summari[sz]e[sd]?|summari[sz]ing|sumari[sz]e)\b"
                           r"|\b(?:a|an)\s+(?P<mod>(?:" + _ADJ + r"\s+|" + _TIME_ADJ + r"\s+){0,2})(?P<n>summary|sumary|summery|"
                           r"overview|report|reprot)\s+(?:of|on|for|about)\b"
                           r"|\b(?:in\s+)?(?P<n2>report)\s+format\b|\bin\s+(?P<n3>short|brief)\b"
                           r"(?!\s+(?:notice|time|term|span|supply|run|while|order|duration|staffed|listed|period)\b)"
                           r"|(?:^|(?<=[.!?;:,]))\s*(?:a\s+|give\s+(?:me\s+)?(?:a\s+)?)?quick\s+(?P<n4>summary)\b"
                           r"(?!\s+(?:field|fields|of\s+[a-z]+-\d))(?:\s+(?:of|on|for|about)\b)?"
                           r"|\b(?P<n5>brief)\s+(?:me|us)\s+(?:on|about)\b")),
    ("breakdown", re.compile(r"(?:\b(?:a|an|the)\s+)?(?:(?:detailed|full|quick|complete|short)\s+)?\b(?P<n>" + _N_BREAK
                             + r")\b(?P<conn>\s+(?:of|for|on))?")),
    ("tabular", re.compile(r"(?:\b(?:in|as)\s+(?:an?\s+)?|\b(?:ka|ki|ke)\s+)?\b(?P<n>tabular)\b"
                           r"(?:\s+(?:form|format|view|way|manner))?(?:\s+(?:me|mein)(?=\s+" + _HINDI_VERB + r"\b))?"
                           r"|\b(?P<n2>table)\s+(?:format|form|view)\b|\b(?P<v>tabulate[sd]?)\b")),
    # "can you plot X", "please visualize X", "to graph X": the verb reads as "show"
    ("head_verb", re.compile(r"(?P<head>^|[.!?;:,]|\b(?:and|also|then|aur|plus|but|so|now|please|pls|kindly|to|you|u|"
                             r"lets|let's))\s*\b(?P<v>plot|plotting|plto|graph|graphing|chart|charting|vi[sz]u?a?li[sz]e|"
                             r"visulize|vizualize|visualze)\b(?!\s*(?:$|[.!?,;:]|of\b|for\b|by\b|per\b|me\b|mein\b))")),
    ("visualize", re.compile(r"\b(?P<v>vi[sz]u?a?li[sz]e[sd]?|vi[sz]u?a?li[sz]ing|visulize|vizualize|visualze)\b")),
    # the free words anywhere else: "bar chart", "the monthwise plot", "performance artifact", "visually"
    ("free", re.compile(r"(?:\b(?P<gen>ka|ki|ke)\s+)?\b(?P<mod>" + _MOD + r")(?P<n>" + _N_CHART + r"|" + _N_ART
                        + r"|xlsx|xls|charted|graphed|plotted)\b(?P<suf>\s+(?:format|form|view|representation))?"
                        r"|\b(?P<n2>visually|graphically|pictorially)\b"
                        r"|\b(?P<n3>visual)\s+(?:view|representation|form)\b(?:\s+of\b)?")),
]
#: words that make no content of their own: a clause or a question of only these (after the display words are out)
#: names no data
_EMPTY_WORDS = _FILLER | frozenset("""show me all can could would will you u please kindly how what which also too here
    there now so then well form format way view mode style type kind version mujhe hume humein karke dena dijiye bhejo
    batao bataiye dikhaiye give provide send share export download get need want like should possible is it does did
    make create build generate prepare nice good quick simple full short detailed proper clear let lets us am be able
    sure ok okay any some about its my our your very more just do something possible something""".split())
#: an ask whose first words ask whether something can be done: a capability ask when nothing else is named
_MODAL = re.compile(r"^\W*(?:can|could|do|does|will|would|are|is\s+it\s+possible|are\s+you\s+able)\b"
                    r"|\bkya\b.*\bsak(?:te|ta|ti)\b", re.IGNORECASE)
_POINTER = re.compile(r"\b(?:it|this|that|these|those|them|same|above|previous|earlier|isko|iska|iski|ise|ye|yeh|wo|woh|"
                      r"usko|uska|uski)\b", re.IGNORECASE)
_SENTINEL = "\x00"
_CLAUSE_SPLIT = re.compile(r"(?<=[.!?;,])|(?=\b(?:and|aur|also|then|plus)\b)")
_WORDS = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class Display:
    """What :func:`split` read from a question: the search text and how to show the answer."""

    search: str                                  # the question without display words: what to fetch
    chart: bool = False                          # a chart or visual word was removed (and not refused)
    visual: bool = False                         # artifact / dashboard
    table: bool = False
    detail: str | None = None                    # "full" | "summary" | None
    breakdown: bool = False
    file: str | None = None                      # "excel" | "csv" | None
    refused: frozenset = frozenset()             # flags the question turns down: "no chart", "table nahi", "bina graph"
    empty: bool = False                          # no content word left: a display-only ask
    capability: bool = False                     # empty, a modal ask and no pointer: "can you do visualizations ?"
    words: tuple = ()                            # removed display words, lower case (log only: fixed vocabulary)
    kept: tuple = ()                             # (word, guard) kept by a guard (log only)

    def flags(self) -> dict:
        """The flags that are set, as JSON-safe values."""
        out: dict[str, Any] = {k: True for k in ("chart", "visual", "table", "breakdown") if getattr(self, k)}
        if self.detail:
            out["detail"] = self.detail
        if self.file:
            out["file"] = self.file
        if self.refused:
            out["refused"] = sorted(self.refused)
        if self.empty:
            out["empty"] = True
        if self.capability:
            out["capability"] = True
        return out

    def found(self) -> bool:
        """Any display word was read (removed, refused or kept by a guard)."""
        return bool(self.words or self.refused or self.kept)

    def as_log(self) -> dict:
        """The JSONL ``display`` entry: the set flags, the removed words and the kept (word, guard) pairs."""
        return {"flags": self.flags(), "words": list(self.words), "kept": [list(k) for k in self.kept]}


NO_DISPLAY = Display(search="")


def _flag_of(word: str) -> tuple[str, Any]:
    """The flag a display word sets."""
    w = " ".join(word.split())
    if re.fullmatch(_N_ART, w) or re.fullmatch(_N_DASH, w):
        return "visual", True
    if re.fullmatch(_N_TABLE, w) or w in ("tabular", "tabulate", "tabulated", "tabulates"):
        return "table", True
    if re.fullmatch(_N_FILE, w):
        return "file", "csv" if w == "csv" else "excel"
    if re.fullmatch(_N_BREAK, w):
        return "breakdown", True
    if re.fullmatch(_N_DETAIL, w) or w in ("detailed", "detaild", "detailled", "vistaar", "vistar", "vstaar"):
        return "detail", "full"
    if re.fullmatch(_N_SUM, w) or w in ("short",) or w.startswith(("summari", "sumari")):
        return "detail", "summary"
    return "chart", True


def _fuzzy_free(low: str, taken: list[bool]) -> list[tuple[int, int, str]]:
    """Words of 7 or more letters one edit from "artifact" or "artefact" that no rule took (typos outside the list)."""
    out = []
    for m in re.finditer(r"[a-z]{7,10}", low):
        if any(taken[m.start():m.end()]):
            continue
        w = m.group(0)
        if not re.fullmatch(_N_ART, w) and min(osa_distance(w, t, 1) for t in ("artifact", "artefact", "artifacts")) <= 1:
            out.append((m.start(), m.end(), w))
    return out


class SessionGuards:
    """Guards 7-9 of :func:`split` over an open record session (records on only): stored record values and stored
    people the typed question names (``tools.spot_values``, ``tools.spot_people``), and the invariant check (the
    contact rule and the business-term contracts read the search text as they read the typed one). The engine modules
    are imported inside :meth:`same`, so building this imports nothing. Never raises."""

    def __init__(self, session: Any) -> None:
        self.tools = getattr(session, "tools", None)

    def spans(self, typed: str) -> list[tuple[int, int, str]]:
        """``(start, end, guard)`` of the listed value spots (``value``) and of the strong people spots (``name``)."""
        out: list[tuple[int, int, str]] = []
        fn = getattr(self.tools, "spot_values", None)
        try:
            for s in (fn(typed, people=[]) if callable(fn) else []) or []:
                if isinstance(s, dict) and s.get("listed") and isinstance(s.get("start"), int) \
                        and isinstance(s.get("end"), int):
                    out.append((s["start"], s["end"], "value"))
        except Exception:  # noqa: BLE001 - optional
            pass
        fn = getattr(self.tools, "spot_people", None)
        try:
            for d in (fn(typed) if callable(fn) else []) or []:
                words = str(d.get("words") or "").split() if isinstance(d, dict) else []
                if not words or d.get("strength") != "strong":
                    continue
                rx = r"(?<![^\W_])" + r"[\W_]+".join(re.escape(w) for w in words) + r"(?![^\W_])"
                out += [(m.start(), m.end(), "name") for m in re.finditer(rx, typed, re.IGNORECASE)]
        except Exception:  # noqa: BLE001 - optional
            pass
        return out

    def same(self, typed: str, search: str) -> bool:
        """The contact rule and the business-term contracts read ``search`` as they read ``typed``."""
        try:
            from sf_assistant.engine.contact import asks_for_contact
            from sf_assistant.engine.term_contracts import detect

            if asks_for_contact(typed) != asks_for_contact(search):
                return False
            # the tools' own reading, with the Account record type names (review nit of 2026-11-25), when they have it
            terms = getattr(self.tools, "_terms_for", None)
            read = terms if callable(terms) else detect
            return dataclasses.replace(read(typed), question="") == dataclasses.replace(read(search), question="")
        except Exception:  # noqa: BLE001 - when in doubt, the typed text
            return False


def _lowered(text: str) -> str:
    """``text`` in lower case with ``’`` as ``'``, one character for each character (the offsets stay)."""
    return "".join(("'" if c == "’" else c.lower() if len(c.lower()) == 1 else c) for c in text)


def _clause_rest(low: str, at: int) -> str:
    end = re.search(r"[.!?;]", low[at:])
    return low[at:at + end.start()] if end else low[at:]


def _op(rule: str, m: re.Match, low: str) -> dict | None:
    """What one match of :data:`_RULES` does: ``{"start", "end", "insert", "flags", "words", "refused", "kept"}``,
    or a kept word (``{"kept": [(word, guard)]}``) when a gate says the shape is no display shape. None: nothing."""
    g = m.groupdict()
    noun = " ".join((g.get("n") or g.get("n2") or g.get("n3") or g.get("n4") or g.get("n5") or g.get("v") or "")
                    .split())
    start, end = m.start(), m.end()
    if rule == "refuse":
        flag = _flag_of(noun)[0]
        return {"start": start, "end": end, "insert": "", "flags": [], "words": [noun], "refused": {flag}}
    kept = {"kept": [(noun, "gate")]}
    flag, value = _flag_of(noun) if noun else ("chart", True)
    insert = ""
    if rule == "export":
        if _FILE_SKILL.search(low[max(0, start - 40):start]):
            return {"kept": [(noun, "gate")]}
        mid = g.get("mid") or ""
        return {"start": start, "end": end, "insert": "show" + mid, "flags": [("file", value)], "words": [noun]}
    if rule == "hinglish":
        post, hv = (g.get("post") or "").strip(), " ".join((g.get("hv") or "").split())
        core = post.split()[-1] if post else ""   # "format me", "sheet me" -> "me"
        make = hv in ("banao", "bana do", "bana ke do", "bnao", "banado", "banaiye")
        if flag in ("detail",) and noun.startswith(("detail", "detial")) and not post:
            return kept
        if flag in ("table", "visual") and not re.fullmatch(_N_ART, noun) and not (
                (core in ("me", "mein") and hv in ("dikhao", "do", "bhejo", "batao")) or make):
            return kept
        if flag == "file" and not (core in ("me", "mein", "mai", "main") or make):
            return kept   # "excel me bhejo", "interviews excel bana do" (E-08); never "excel se" or "excel batao"
        if flag == "detail" and value == "summary":
            if noun == "short" and post not in ("me", "mein"):
                return None
            # review fix E-10: "comment summary batao", "X ka summary batao" name a field (Comment Summary, Summary__c):
            # only "summary me batao" or a summary at the head of a clause ("summary do is mahine ke ...") is the format
            head = re.search(r"(?:^|[.!?;:,])\s*$", low[:start]) is not None and not g.get("gen")
            if noun not in ("short",) and not (core in ("me", "mein") or (head and not post and hv in (
                    "do", "batao", "dikhao", "bhejo", "chahiye"))):
                return kept
            if noun in ("report", "reports", "overview", "brief"):
                return kept
        if flag == "breakdown" and (_GROUPING_BEFORE.search(low[:start]) or re.search(r"wise\b", low[start:m.start("n")])):
            return {"kept": [(noun, "grouping")]}
        insert = "batao" if hv.startswith(("batao", "bata ", "bataiye", "samjhao")) else "dikhao"
        detailed = [("detail", "full")] if re.search(r"\bdetail", low[start:m.start("n")]) else []
        return {"start": start, "end": end, "insert": insert, "flags": detailed + [(flag, value)], "words": [noun],
                "keep_time": True}
    if rule == "verb":
        v, art, conn = g.get("v") or "", (g.get("art") or "").strip(), (g.get("conn") or "").strip()
        if re.fullmatch(_N_VIZ, noun):
            flag, value = "chart", True
        if v in ("do", "get") and flag not in ("chart", "visual"):
            return None
        if flag == "file":   # "make an excel of interviews", "give me an excel of X" (E-08)
            if art not in ("a", "an") or conn not in ("of", "for", "with", "on") \
                    or _FILE_SKILL.search(low[max(0, start - 40):start]):
                return kept
        if flag in ("table", "visual") and not re.fullmatch(_N_ART, noun):
            if art == "the" or (v in ("show", "give", "send", "share", "get", "display", "provide", "present")
                                and art not in ("a", "an")):
                return kept
        if flag == "detail" and value == "summary":
            if art not in ("a", "an") or conn not in ("of", "on", "for", "about"):
                return kept
        if flag == "breakdown":
            if _GROUPING_AFTER.search(low[end:]) or _GROUPING_BEFORE.search(low[:start]) \
                    or re.search(r"wise\b", g.get("mod") or "") or _grouping_word_before(low, m.start("n")):
                return {"kept": [(noun, "grouping")]}
        detailed = [("detail", "full")] if re.search(r"\bdetail", g.get("mod") or "") else []
        return {"start": start, "end": end, "insert": "show", "flags": detailed + [(flag, value)], "words": [noun],
                "keep_time": True}
    if rule == "lead_table":   # "table of interviews by month" (E-04): the verb reads as "show"
        return {"start": m.start("v") if g.get("v") else m.start("n"), "end": end,
                "insert": "show" if g.get("v") else "", "flags": [("table", True)], "words": [noun]}
    if rule == "type":
        return {"start": start, "end": end, "insert": "", "flags": [("chart", True)],
                "words": [re.findall(r"[a-z]+", m.group(0))[-1 if not m.group(0).rstrip().endswith(("of", "for")) else -2]]}
    if rule == "prep":
        p, art = " ".join((g.get("p") or "").split()), (g.get("art") or "").strip()
        if noun in ("graphical", "pictorial", "visual"):
            if not (g.get("suf") or "").strip():
                return None
            flag, value = "chart", True
        elif noun == "tabular":
            flag, value = "table", True
        elif re.fullmatch(_N_VIZ, noun):
            flag, value = "chart", True
        if flag == "file":
            if p not in ("in", "to", "as", "into") or _FILE_SKILL.search(low[max(0, start - 40):start]):
                return kept
        elif flag in ("table", "visual") and not re.fullmatch(_N_ART, noun):
            # review fix E-11: "logged into the dashboard" is a place (the candidate portal), "as the table" a format
            if p in ("using", "via", "through", "to") or (art == "the" and p != "as"):
                return kept
        elif flag == "detail":
            if p not in ("as", "in") or noun.endswith("s") or noun == "overview" or art == "the":
                return kept
            # review fix R7: "placed in report period" is a field phrase; "in report form" or a clause end is the format
            if noun in ("report", "reprot") and not (g.get("suf") or "").strip() \
                    and not re.match(r"\s*(?:$|[.!?;:,])", low[end:]):
                return kept
        elif flag == "breakdown":
            if p not in ("as", "in", "with") or _GROUPING_AFTER.search(low[end:]):
                return {"kept": [(noun, "grouping")]}
        elif p == "to":
            return None
        start = m.start("p") if g.get("pre") and re.match(r"\s*(?:$|[.!?;])", low[end:]) is None else start
        return {"start": start, "end": end, "insert": "", "flags": [(flag, value)], "words": [noun], "keep_time": True}
    if rule == "detail":
        if g.get("n5"):   # "detailed interviews last month": "detailed" heads the clause (E-09)
            return {"start": m.start("n5"), "end": end, "insert": "", "flags": [("detail", "full")], "words": [noun]}
        if g.get("n4"):
            return {"start": start, "end": end, "insert": "", "flags": [("detail", "full")], "words": ["vistaar se"]}
        return {"start": start, "end": end, "insert": "", "flags": [("detail", "full")], "words": [noun]}
    if rule == "summary":
        if g.get("n5"):   # "brief me on interviews": the verb reads as "show" (E-09)
            return {"start": start, "end": end, "insert": "show", "flags": [("detail", "summary")], "words": ["brief"]}
        if g.get("n4"):   # "quick summary interviews last month", "give me a quick summary of X" (E-09)
            return {"start": start, "end": end, "insert": "show" if re.search(r"\bgive\b", m.group(0)) else "",
                    "flags": [("detail", "summary")], "words": ["summary"]}
        if g.get("v"):
            more = re.match(r"\s*[^\W_]", low[end:]) is not None and not re.match(r"\s*[.!?;]", low[end:])
            return {"start": start, "end": end, "insert": "show" if more else "", "flags": [("detail", "summary")],
                    "words": [noun]}
        return {"start": start, "end": end, "insert": "", "flags": [("detail", "summary")], "words": [noun],
                "keep_time": True}
    if rule == "breakdown":
        if _GROUPING_AFTER.search(low[end:]) or _GROUPING_BEFORE.search(low[:start]) \
                or _grouping_word_before(low, m.start("n")):
            return {"kept": [(noun, "grouping")]}
        return {"start": start, "end": end, "insert": "", "flags": [("breakdown", True)], "words": [noun]}
    if rule == "tabular":
        if g.get("v"):
            more = re.match(r"\s*[^\W_]", low[end:]) is not None and not re.match(r"\s*[.!?;]", low[end:])
            return {"start": start, "end": end, "insert": "show" if more else "", "flags": [("table", True)],
                    "words": [noun]}
        return {"start": start, "end": end, "insert": "", "flags": [("table", True)], "words": [noun]}
    if rule in ("head_verb", "visualize"):
        more = re.match(r"\s*[^\W_]", low[end:]) is not None and not re.match(r"\s*[.!?;]", low[end:])
        return {"start": m.start("v"), "end": end, "insert": "show" if more else "", "flags": [("chart", True)],
                "words": [noun]}
    if rule == "free":
        at = m.start("n2") if g.get("n2") else m.start("n") if g.get("n") else m.start("n3")
        if re.fullmatch(r"vi[sz]u?a?li[sz]ations?|visualzations?|visuals|visually", noun) \
                and not any(a.start() <= at < a.end() for a in _ASK.finditer(low)):
            # review fix R1: "visually impaired", "data visualization", "visualization engineer" are no chart ask;
            # the chart grammar reads these words only in its own shapes ("visualization of", "show ... visually")
            return {"kept": [(noun, "skill")]}
        if g.get("n2") or g.get("n3"):
            return {"start": start, "end": end, "insert": "", "flags": [("chart", True)], "words": [noun]}
        return {"start": start, "end": end, "insert": "", "flags": [(flag, value)], "words": [noun], "keep_time": True}
    return None


def _grouping_word_before(low: str, at: int) -> bool:
    """The word right before ``breakdown`` (at ``at``) names what to split by: "a status breakdown of X", "niche
    breakdown", "per niche breakdown" (review fix R4 of 2026-11-25). Never an article, a size or a time adjective (a
    monthly breakdown loses the word and keeps "monthly"), a verb or a filler word."""
    before = re.search(r"([a-z]+)[\s-]*$", low[:at])
    if before is None:
        return False
    w = before.group(1)
    return w not in _NOT_GROUPING and not _TIME_ADJ_RE.fullmatch(w) and not re.fullmatch(_V_MAKE + "|" + _V_GIVE, w)


#: An ask of data or of a display in a clause: such a clause is never a question about the display thing itself
_ABOUT_NOT = re.compile(r"\b(?:how\s+many|how\s+much|count|kitn[aie]|list)\b|\b(?:bana|banao|bnao|banado|dikhao|dikha)\b"
                        r"|^\W*(?:please\s+)?(?:show|give|send|make|create|draw|plot|display|prepare|generate)\b"
                        r"|^\W*(?:please\s+)?(?:can|could|will|would)\s+(?:you|u)\b")


def _about(low: str) -> bool:
    """A question about the display thing itself (guard ``about``). The third reading (an ask word, a word of the org's
    setup and a display noun) holds in one clause that asks for no data and no display (review fix E-05: "how many
    interviews per status? show it as a pie chart using the status field" is a chart ask)."""
    if _ABOUT.search(fold(low)) or _ABOUT_WHAT.search(low):
        return True
    for clause in re.split(r"[.!?;]+|\b(?:and|but|also|then|aur)\b", low):
        if _ABOUT_ASK.search(clause) and _ABOUT_META.search(clause) and _ABOUT_NOUN.search(clause) \
                and not _ABOUT_NOT.search(clause.strip()):
            return True
    return False


def _content(text: str) -> list[str]:
    return [w for w in _WORDS.findall(text.lower()) if w not in _EMPTY_WORDS]


def _tidy(text: str) -> str:
    """Spaces and punctuation after the words went: no double space, no space before a mark, no dangling article,
    connector or comma."""
    t = re.sub(r"[ \t]+", " ", text)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,;:])(\s*[,;:.!?])+", lambda m: m.group(0)[-1] if m.group(0)[-1] in ".!?" else m.group(1), t)
    t = re.sub(r"(?i)\b(?:a|an|the)\s*(?=[,.;:!?]|$)", "", t)
    t = re.sub(r"(?i)\b(show|dikhao)(\s+\1\b)+", r"\1", t)
    t = re.sub(r"(?i)[\s,;:]*\b(?:and|also|aur|or|then|plus)\s*$", "", t)
    t = re.sub(r"(?i)[\s,;:]*\b(?:and|also|aur|or|then|plus)\s*([.!?])\s*$", r"\1", t)
    t = re.sub(r"([.!?])(?:\s*[.!?])+", r"\1", t)   # one end mark where a dropped clause brought its own
    t = re.sub(r"\(\s*\)|\[\s*\]", "", t)              # "interviews by month (chart)" (E-13)
    t = re.sub(r"\s*[-\u2013\u2014]+\s*$", "", t)        # "interviews by month - chart"
    t = re.sub(r"^[\s,.;:]+", "", t)
    t = re.sub(r"[\s,;:]+$", "", t)
    t = re.sub(r"[ \t]+", " ", t)
    return t.strip()


def split(text: str, guards: Any = None) -> Display:
    """The question ``text`` split into what to fetch and how to show it (see the module text). ``guards``: a
    :class:`SessionGuards` (records on), or None. Never raises: on any fault the question is searched as typed."""
    typed = str(text or "")
    try:
        return _split(typed, guards)
    except Exception:  # noqa: BLE001 - a question never fails on this
        return Display(search=typed)


def _split(typed: str, guards: Any) -> Display:
    if not typed.strip() or len(typed) > _MAX_TEXT:
        return Display(search=typed)
    low = _lowered(typed)
    if not re.search(r"chart|graph|grap|plot|diagram|histogram|visual|vizual|visul|\bviz|artif|artef|artfact|table|tabel|"
                     r"tabble|tble|tabul|dashb|dash b|summar|sumar|summery|overview|report|reprot|brief|short|detail|"
                     r"detial|vistaa?r|breakdown|break-down|excel|exel|csv|spreadsheet|sheet|xls|export|download|pie|"
                     r"donut|doughnut|bar|line|column|stack|horizontal|vertical|scatter|funnel|area|switch|polt|plott|"
                     r"graf|grph|grafh|garph|grpah|chrat|chrt|cahrt|chatr|plto|infographic|heat ?map", low):
        return Display(search=typed)
    taken = [False] * len(low)
    kept: list[tuple[str, str]] = []
    if _about(low):
        words = [" ".join(m.group(0).split()) for m in re.finditer(r"\b(?:" + _N_ANY + r"|" + _N_SUM + r"|" + _N_FILE
                                                                    + r"|" + _N_BREAK + r")\b", low)]
        return Display(search=typed, kept=tuple((w, "about") for w in dict.fromkeys(words)))
    for rx, guard in ((_QUOTED, "quoted"), (_API, "api"), (_NOT_A_CHART, "not_a_chart")):
        for m in rx.finditer(low):
            if m.end() > m.start() and not any(taken[m.start():m.end()]):
                for i in range(m.start(), m.end()):
                    taken[i] = True
                if re.search(r"chart|graph|plot|table|dashboard|report|summary|detail|excel|csv|artifact", m.group(0)):
                    kept.append((" ".join(m.group(0).split())[:60] if guard == "not_a_chart" else
                                 next(iter(re.findall(r"chart|graph|plot|table|dashboard|report|summary|detail|excel|"
                                                      r"csv|artifact", m.group(0)))), guard))
    ops: list[dict] = []
    for rule, rx in _RULES:
        for m in rx.finditer(low):
            if m.end() <= m.start() or any(taken[m.start():m.end()]):
                continue
            op = _op(rule, m, low)
            if op is None:
                continue
            if "start" not in op:
                kept += op["kept"]
                continue
            if any(taken[op["start"]:op["end"]]):
                continue
            for i in range(op["start"], op["end"]):
                taken[i] = True
            ops.append(op)
    for s, e, w in _fuzzy_free(low, taken):
        ops.append({"start": s, "end": e, "insert": "", "flags": [("visual", True)], "words": [w]})
    for op in _table_tails(low, taken, ops):   # "interviews by month table", "... pie chart and a table" (E-04, R10)
        for i in range(op["start"], op["end"]):
            taken[i] = True
        ops.append(op)
    refused_by_stop: set[str] = set()
    if _STOP.search(fold(low)):
        # review fix R1: a refusal the chart grammar reads ("hide the pie chart", "stop showing charts", "graph hata do")
        # turns the chart down: no chart flag, and its chart words stay in the search text as typed
        chart_only = [op for op in ops if not op.get("refused") and op["flags"]
                      and all(k == "chart" for k, _v in op["flags"])]
        kept += [(w, "refused") for op in chart_only for w in op["words"]]
        ops = [op for op in ops if op not in chart_only]
        refused_by_stop = {"chart"}
    # the gated words no shape took
    for m in re.finditer(r"\b(?:" + _N_TABLE + r"|" + _N_DASH + r"|" + _N_SUM + r"|" + _N_DETAIL + r"|" + _N_FILE + r"|"
                         + _N_BREAK + r")\b", low):
        if not any(taken[m.start():m.end()]) and not any((m.group(0), g) in kept for g in ("gate", "grouping")):
            kept.append((" ".join(m.group(0).split()), "gate"))
    if not ops:
        return Display(search=typed, refused=frozenset(refused_by_stop), kept=tuple(dict.fromkeys(kept)))
    # guards 7-8: a stored record value or a stored person's name that holds a display word keeps it
    flag_only: list[dict] = []   # kept by a value that is the file word itself: the word stays, its file flag is set
    if guards is not None:
        spans = guards.spans(typed)
        if spans:
            left = []
            for op in ops:
                hit = next(((s, e, g) for s, e, g in spans if s < op["end"] and op["start"] < e), None)
                if hit is None:
                    left.append(op)
                else:
                    kept += [(w, hit[2]) for w in op["words"]]
                    if _file_word_value(op, hit, typed):
                        flag_only.append(op)
            ops = left
            if not ops:
                file = next((v for op in flag_only for k, v in op["flags"] if k == "file"), None)
                return Display(search=typed, file=file, kept=tuple(dict.fromkeys(kept)))
    ops.sort(key=lambda o: o["start"])
    search, empty = _rebuild(typed, low, ops)
    flags: dict[str, Any] = {}
    refused: set[str] = set()
    words: list[str] = []
    refused |= refused_by_stop
    for op in ops + flag_only:   # a flag-only op removed no word: its word is in kept
        refused |= set(op.get("refused") or ())
        words += [] if any(op is f for f in flag_only) else op["words"]
        for k, v in op["flags"]:
            if k == "detail" and flags.get("detail") == "full":
                continue
            flags[k] = v
    for k in list(flags):
        if k in refused:
            del flags[k]
    chart = bool(flags.get("chart") or flags.get("visual"))
    if not empty and search != typed:
        lost = [n for n in _NUMBER.findall(typed) if n not in _NUMBER.findall(search)]
        quotes = [q.group(0) for q in _QUOTED.finditer(typed) if q.group(0) not in search]
        if lost or quotes or (guards is not None and not guards.same(typed, search)):
            search = typed
            kept.append(("*", "invariant"))
    # review fix R9: the "it" of "is it possible" points at nothing
    capability = empty and bool(_MODAL.search(typed)) and not _POINTER.search(_POSSIBLE.sub(" ", typed))
    return Display(search="" if empty else search, chart=chart, visual=bool(flags.get("visual")),
                   table=bool(flags.get("table")), detail=flags.get("detail"), breakdown=bool(flags.get("breakdown")),
                   file=flags.get("file"), refused=frozenset(refused), empty=empty, capability=capability,
                   words=tuple(dict.fromkeys(words)), kept=tuple(dict.fromkeys(kept)))


def _file_word_value(op: dict, span: tuple[int, int, str], typed: str) -> bool:
    """A stored value spot (``span``) that is the file word of a file shape and nothing more: a company record named
    "Excel" in "send the interviews to excel" (pre-release check P-R12 of 2026-11-25). The value spot keeps the word in
    the search text, and the file flag is still set from the typed shape; a chart, table or other word inside a stored
    value sets no flag (a free word such as "chart" is no shape of its own)."""
    s, e, guard = span
    return (guard == "value" and not op.get("refused") and bool(op["flags"])
            and all(k == "file" for k, _v in op["flags"]) and len(op["words"]) == 1
            and " ".join(typed[s:e].lower().split()) == op["words"][0])


_TABLE_TAIL = re.compile(r"\b(?P<n>" + _N_TABLE + r"|pie|donut|doughnut)(?:\s+(?:please|pls|plz|chahiye|do|dikhao|bhejo))?"
                         r"\s*[.!?]*\s*$")
#: What makes a table at the end of a question a format: a grouping or a period in its clause ("interviews by month
#: table", "monthly interviews table please"); "the interview table" names the object and stays
_TABLE_CUE = re.compile(r"\b(?:by|per)\s+[a-z]+|\w-?wise\b|\b(?:monthly|weekly|daily|quarterly|yearly|annual)\b"
                        r"|\b(?:last|this|past|previous|next)\s+(?:\d+\s+)?(?:days?|weeks?|months?|quarters?|years?)\b")
_AND_TABLE = re.compile(r"\s*\b(?:and|aur|plus|with)\s+(?:an?\s+|the\s+)?(?P<n>" + _N_TABLE + r")\b"
                        r"(?=\s*(?:$|[.!?,;]|too\b|also\b|please\b|bhi\b))")
_POSSIBLE = re.compile(r"\bis\s+it\s+possible\b", re.IGNORECASE)


def _table_tails(low: str, taken: list[bool], ops: list[dict]) -> list[dict]:
    """Table ops no rule took: a table word (or a pie or donut: "interviews by status donut") ending a question whose
    clause groups or names a period, and "and a table" right after a removed display phrase."""
    out: list[dict] = []
    m = _TABLE_TAIL.search(low)
    if m is not None and not any(taken[m.start():m.end()]):
        before = low[:m.start("n")]
        clause = re.split(r"[.!?;]", before)[-1]
        if _TABLE_CUE.search(clause) and not re.search(r"\b(?:the|this|that|its|their)\s+$", before):
            word = " ".join(m.group("n").split())
            out.append({"start": m.start("n"), "end": m.end(), "insert": "",
                        "flags": [("chart" if word in ("pie", "donut", "doughnut") else "table", True)],
                        "words": [word]})
    ends = {op["end"] for op in ops if op.get("words") and not op.get("refused")}
    for m in _AND_TABLE.finditer(low):
        if m.start() in ends and not any(taken[m.start():m.end()]) and not any(
                o["start"] < m.end() and m.start() < o["end"] for o in out):
            out.append({"start": m.start(), "end": m.end(), "insert": "", "flags": [("table", True)],
                        "words": [m.group("n")]})
    return out


def _rebuild(typed: str, low: str, ops: list[dict]) -> tuple[str, bool]:
    """The search text after ``ops`` (sorted, not overlapping), and whether no content word is left. An op of nothing
    takes the article right before it, and at a clause's start the connector right after it; a Hinglish genitive
    ("ka graph") goes with its noun; time adjectives of a removed phrase stay."""
    parts: list[str] = []
    at = 0
    for op in ops:
        s, e, insert = op["start"], op["end"], op["insert"]
        if not insert:
            before = low[at:s]
            art = re.search(r"\b(?:a|an|the)\s+$", before)
            if art and not op.get("refused"):
                s = at + art.start()
            head = re.search(r"(?:^|[.!?;:,]|\b(?:and|also|aur|then|plus))\s*$", low[:s])
            conn = re.match(r"\s+(?:of|for|on|about|showing|with|ka|ki|ke)\b", low[e:])
            if head and conn:
                e += conn.end()
        keep = " ".join(_TIME_ADJ_RE.findall(low[s:e])) if op.get("keep_time") else ""
        piece = " ".join(x for x in (insert, keep) if x)
        parts.append(typed[at:s])
        parts.append(f" {_SENTINEL}{piece} " if piece else f" {_SENTINEL} ")
        at = e
    parts.append(typed[at:])
    marked = "".join(parts)
    kept_parts: list[str] = []
    segments = [seg for seg in _CLAUSE_SPLIT.split(marked) if seg]
    for seg in segments:
        if _SENTINEL in seg and not _content(seg.replace(_SENTINEL, " ")):
            # a clause of filler and display words only goes whole; its end mark stays when it ends the question
            tail = re.search(r"[.!?]+\s*$", seg)
            if tail and kept_parts and not re.search(r"[.!?]\s*$", kept_parts[-1]):
                kept_parts[-1] = kept_parts[-1].rstrip(" ,;:") + tail.group(0).strip()
            continue
        kept_parts.append(seg)
    out = "".join(kept_parts).replace(_SENTINEL, "")
    out = _tidy(out)
    return out, not _content(out)


def note(display: Display | None) -> str:
    """The record agent's display note for these flags (appended after the chart note): a blank line, the lead line and
    one line per set flag (table, detail, summary, breakdown, file); "" when none is set or for None."""
    if display is None or display.empty:
        return ""
    keys = [k for k, on in (("table", display.table), ("detail", display.detail == "full"),
                            ("summary", display.detail == "summary"), ("breakdown", display.breakdown),
                            ("file", bool(display.file))) if on]
    if not keys:
        return ""
    return "\n\n" + "\n".join([NOTE_LINES["lead"]] + [f"- {NOTE_LINES[k]}" for k in keys])


def line(key: str) -> str:
    """A line of ``prompts/display_words.md``: ``bare``, ``capability``, ``file`` and their forms (see below)."""
    return LINES[key]


def bare_key(charts_on: bool = True) -> str:
    """The key of the line for a question of display words only: ``bare`` (its example is a chart) or, with
    ``SFA_CHARTS`` off, ``bare_tables`` (review fix R5 of 2026-11-25)."""
    return "bare" if charts_on else "bare_tables"


def capability_key(display: Display, charts_on: bool = True) -> str:
    """The key of the line for a capability ask (review fixes E-14 and R5 of 2026-11-25): a file ask gets
    ``capability_file`` (no file is made); a dashboard or artifact ask ``capability_visual`` (no dashboards, charts and
    tables are); a chart or table ask ``capability``; with ``SFA_CHARTS`` off the ``_tables`` forms; an ask of a detail,
    summary or breakdown only gets the bare line (:func:`bare_key`)."""
    if display.file:
        return "capability_file"
    if display.visual:
        return "capability_visual" if charts_on else "capability_visual_tables"
    if display.chart or display.table:
        return "capability" if charts_on else "capability_tables"
    return bare_key(charts_on)


#: Plural or generic display nouns: "can you do visualizations ?" asks what the answers can show, even after an answer
_GENERIC = frozenset("""charts graphs plots visualizations visualisations visuals tables dashboards artifacts
    artefacts diagrams histograms infographics""".split())


def generic_ask(display: Display) -> bool:
    """A capability ask made only of plural display nouns ("can you do visualizations ?", "do you make charts"): it
    names nothing of the last answer either. "can you plot", "can you make a pie chart" after an answer ask for a chart
    of that answer, so the follow-up rewrite reads them (review fix R2 of 2026-11-25)."""
    return bool(display.capability and display.words and all(w in _GENERIC for w in display.words))
