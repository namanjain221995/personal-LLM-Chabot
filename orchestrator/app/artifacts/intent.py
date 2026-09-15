"""Is this turn asking for a FILE? — the artifact-intent gate.

    "Create a professional PDF about this."       create   (explicit pdf)
    "Give this to me as a document."              create   (kind: document)
    "Share XLSX, Word, PDF and CSV of this audit." create   (four formats, in that order)
    "Create a CSV of 500 sample customers."       create   (csv; row_count 500)
    "Make slide 4 shorter."                       edit     (the deck in this conversation)
    "Convert the previous document to PDF."       convert  (explicit pdf)
    "Export the previous answer as PDF."          export   (the last assistant turn → a file)
    "What is a PDF?" / "Can a PDF contain video?" none     (a question ABOUT the format)
    "Show me Python code that reads a DOCX."      none     (code, not a file)

DETERMINISTIC FIRST. Every example in the product brief is decided by the
rules here, and the rules are tested one by one — a request that plainly
asks for a file must never depend on a model's mood. The rules are
conservative in the other direction too: a format word in ordinary
conversation ("should I use Excel or a database?") is not a request.

A CLASSIFIER ONLY FOR THE GENUINELY AMBIGUOUS. `decide()` returns
`ambiguous=True` for the narrow band where a creation verb and a document
noun are both present but so is a question shape it cannot read — the chat
engine may then ask a small strict-JSON classifier (`classify_hook`) and,
absent one, treats the turn as NOT an artifact request: a text answer to a
request for a file is a smaller failure than a file nobody asked for.

Explicit intent always wins: a named format is used, a named target
("version 1", "the deck") is used, and "it" resolves to the most recent
artifact in the conversation unless the words name another.

A NEW FILE IS SAID FIRST. "Create a professional PDF report on X. Make it
visually professional." was an EDIT of whatever came before (discovery of
2026-09-12, C2): the edit rules ran first and "make it … professional"
plus a bare "it" won. The creation verb in the FIRST clause is the
request; what follows is how to do it. So the positional create rule runs
before the edit block (CONTRACT-2 §5), and only the conversion shapes
("Create a PDF version too.") are looked at before it.

A NEGATED VERB IS NOT A REQUEST. "Don't create a PDF, just answer here"
was a create (security review of 2026-09-12, #9): the rules saw the verb
and the noun and never the "don't". A negation right before a creation
verb — don't, never, no need to, must not, without, rather than —
takes that clause out of every rule's view (`_without_negated_clauses`),
so the rest of the message decides: "Don't create a Word doc, create a
PDF instead" is still a PDF, and "don't forget to create a PDF" is not a
negation of the verb.

THE PASTE IS NOT THE PROSE. The row count ("500 rows") is read from the
text BEFORE the first table line (a tab, a pipe, a comma-separated
record) — never from a pasted cell, which would otherwise steer the
generator to the number a comment happened to contain (#3).

AS3 (2026-09-15): THE WORDS PEOPLE ACTUALLY TYPE. After a long audit answer,
"just give it in docs, in a classy format, provide a dox file" was answered
in chat with "as an AI I cannot create a .docx": the rules knew neither
"docs"/"dox", nor a hand-over verb with a bare "it", nor Hindi, Gujarati,
Hinglish or Gujlish. Four changes, in this order inside `decide()`:

  1. The rules read `lexicon.normalize(text)`: typos and script variants are
     mapped to the English words and the few SOV tokens the rules know.
  2. A HARD-NEGATIVE pass runs before any create rule: a read verb on an
     attached source ("summarize this pdf"), a how-to, format trivia,
     feedback ("the pdf looks good") and a request FOR code are not files.
  3. A pronoun or postposition follow-up that hands a format over ("give it
     in docs", "isko pdf me de do") EXPORTS the most recent SUBSTANTIAL
     answer — or, when the last assistant turn was itself a file card,
     CONVERTS that artifact (its content is the one-line card sentence).
  4. With an artifact present, a style clause ("make the headings dark
     blue", "landscape") or an edit verb on an element is an edit, and
     "undo"/"revert" restores.

`decide()` stays rules-only and synchronous: fast_lane.py calls it on the
event loop. Only `decide_with_hook` may consult the model, and only when
the rules found no request, a file word is present and no negative shape
fired.

"IN THE REPORT" IS A PLACE. "The numbers in the report are wrong" was a
create (the destination rule read "in the report" as a deliverable). A
definite article makes it a location; "in a report", "as a PDF" and "in
Excel" still name one, and a destination with no request verb, question
mark or "please" ("I have this in Excel") is a statement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Sequence

from . import formats as F
from . import lexicon as LX
from . import visuals as VIS

Action = str  # "create" | "edit" | "convert" | "export" | "none"

#: How much of a message the rules read.
_DECIDE_CHARS = 4000
#: A conversation artifact's title is matched as a whole phrase, and only
#: when it is distinctive enough to mean something: "The" or "Plan" would
#: turn every later message with an edit verb into an edit.
_MIN_HINT_CHARS = 6

# ------------------------------------------------------------ vocabulary --

_CREATE_VERBS = (
    r"(?:make|create|generate|build|write|draft|prepare|produce|compile|assemble|"
    r"put together|design|develop|export|save|download|print|render|give me|send me|"
    r"i need|we need|i want|we want|i'd like|we'd like|i would like|we would like|looking for|"
    r"can i get|could i get|can i have|could i have|may i have|get me|"
    r"turn(?:\s+\w+){0,4}?\s+into|convert(?:\s+\w+){0,4}?\s+(?:to|into))"
)
#: Hand-over verbs (CONTRACT-2 §5). They create only when the noun is
#: CLOSE — "share XLSX, Word, PDF and CSV" — because "share your thoughts
#: on the report" is conversation, and a six-word gap would take it.
_HANDOVER_VERBS = r"(?:share|provide|deliver|hand over|hand me|supply)"
#: Rows and records are artifact nouns only when they are counted or
#: qualified as data: "500 records", "sample rows", "rows of data" — never
#: "the records from last week", which is a Salesforce question.
_DATA_NOUNS = (
    r"(?:\d[\d,]*\s+(?:\w+\s+){0,2}?(?:records|rows|entries|data points|samples)|"
    r"(?:sample|dummy|synthetic|test|mock|realistic|fake|random)\s+(?:\w+\s+)?(?:records|rows|data|entries)|"
    r"(?:records|rows)\s+of\s+(?:\w+\s+)?data)"
)
_ARTIFACT_NOUNS = (
    r"(?:pdf|docx|word(?:\s+(?:document|file|doc))?|powerpoint|power ?point|powerpint|pptx?|presentation|slides?|"
    r"slide ?deck|deck|pitch ?deck|excel|exel|excell|xlsx|xlxs|xls|spread ?sheet|work ?book|tracker|document|doc|"
    r"report|sop|standard operating procedure|memo|brief|one[- ]pagers?|one[- ]page|proposal|"
    r"(?<!cheat )(?<!fact )(?<!balance )(?<!time )(?<!score )(?<!answer )sheets?(?!\s*\d)|"
    r"policy|letter|handout|write[- ]?up|whitepaper|white paper|summary document|"
    r"deliverables?|files?|dashboard|"
    r"csv|cvs|comma[- ]separated(?: values?)?(?: file)?|data ?set|data file|table file|sample data|"
    rf"{_DATA_NOUNS})"
)
_FORMAT_WORD = (
    r"(?:pdf|docx|word|powerpoint|power ?point|powerpint|pptx?|excel|exel|excell|xlsx|xlxs|xls|spread ?sheet|"
    r"work ?book|slides?|deck|presentation|document|report|csv|cvs|comma[- ]separated(?: values?)?|data ?set|data file)"
)

#: A creation verb, then an artifact noun within six words. The noun alone
#: is never enough: "the report from finance said…" is conversation.
_CREATE_RE = re.compile(
    rf"\b{_CREATE_VERBS}\b(?:\W+\w+){{0,6}}?\W+{_ARTIFACT_NOUNS}\b"
    rf"|\b{_HANDOVER_VERBS}\b(?:\W+\w+){{0,3}}?\W+{_ARTIFACT_NOUNS}\b",
    re.I,
)
#: The POSITIONAL create: a creation verb that can only mean a new thing —
#: `create`, `generate`, … with an article or a count, or any creation
#: verb with `new|another|separate|fresh|second` — in the first clause.
#: `make` alone is not here: "make slide 4 shorter" and "make the deck
#: shorter" are edits, and the edit block still owns them.
_STRICT_CREATE_VERBS = (
    r"(?:create|generate|build|write|draft|prepare|produce|compile|assemble|put together|design|develop|"
    r"share|provide|deliver|give me|send me|i need|we need|i want|we want|i'd like|we'd like|i would like|we would like|make)"
)
_ARTICLE = r"(?:me\s+)?(?:(?:a|an|another|new|a new|an? (?:new|separate|fresh|second|different)|separate|fresh|second|the best|some|two|three|\d+)\s+)"
_POSITIONAL_CREATE_RE = re.compile(
    rf"\b{_STRICT_CREATE_VERBS}\s+{_ARTICLE}(?:\w+\W+){{0,4}}?{_ARTIFACT_NOUNS}\b",
    re.I,
)
#: "another report", "a new deck", "a separate PDF": a new file whatever
#: the verb — but not "another slide" or "a new section", which are parts.
_FILE_NOUNS = (
    r"(?:pdf|docx|word(?:\s+(?:document|file|doc))?|powerpoint|power ?point|pptx?|presentation|slide ?deck|deck|pitch ?deck|"
    r"excel|xlsx|spread ?sheet|work ?book|tracker|document|doc|report|sop|memo|brief|one[- ]pagers?|proposal|policy|letter|"
    r"handout|write[- ]?up|whitepaper|csv|data ?set|data file|file|dashboard)"
)
_NEW_FILE_RE = re.compile(rf"\b(?:new|another|separate|fresh|second|different)\s+(?:\w+\s+){{0,2}}?{_FILE_NOUNS}\b", re.I)
#: "as a PDF" / "in Word" / "to Excel" — the deliverable named as a form.
#: Up to two adjectives between the preposition and the format (AS3 (c)):
#: "in a standard and classy format" is not this, "in a classy pdf" is.
_ADJ = (
    r"(?:nice|classy|standard|proper|professional|clean|formal|neat|good|simple|editable|downloadable|printable|single|"
    r"separate|new|nice[- ]looking|good[- ]looking|looking|well[- ]formatted|formatted|beautiful|polished|elegant|modern|"
    r"styled|colou?red|colou?rful|landscape|portrait|one[- ]page|short|detailed|official|presentable|shareable|pretty|"
    r"decent|executive|corporate|branded|full|complete|final|ms|microsoft|proper|properly|nicely)"
)
#: "as a PDF", "in Word", "into an Excel", "to csv", "in a classy pdf" — the
#: deliverable named as a form; and the postposition forms ("pdf _in_",
#: "docx file _in_") the normaliser writes for "pdf me", "पीडीएफ में",
#: "પીડીએફમાં" (AS3 (d)). "in the report" is a place, not a form.
_AS_FORMAT_RE = re.compile(
    rf"\bas\s+(?:an?\s+|the\s+)?(?:{_ADJ}\s+){{0,2}}(?:{_FORMAT_WORD})\b"
    rf"|\b(?:in|into|to)\s+(?:an?\s+)?(?:{_ADJ}\s+){{0,2}}(?:{_FORMAT_WORD})\b"
    rf"|\b(?:{_FORMAT_WORD}|sheet)(?:\s+(?:file|format|version|copy|doc))?\s+_in_\b{LX.DEST_AFTER}",
    re.I,
)
#: "the best format" / "best deliverable" / "all (the) (required|final) files|deliverables"
_BEST_OR_ALL_RE = re.compile(
    r"\b(?:best\s+(?:format|deliverable|output|file)|all\s+(?:the\s+)?(?:required|final|necessary)?\s*(?:files|deliverables|documents|outputs))\b",
    re.I,
)
#: Explicit formats and nothing else — "XLSX, Word, PDF and CSV of this
#: audit please" (CONTRACT-2 §5): a list of format names at the start of
#: the message, then an object ("of this", "for the table above"). No verb
#: is needed; the shape is unmistakable. Anchored, so "summarise the
#: slides above" is not taken.
_FORMAT_LIST_OBJECT_RE = re.compile(
    rf"^\W*(?:(?:please|also|and|plus|just|only|now)\s+)?(?:(?:an?|the)\s+)?{_FORMAT_WORD}(?:\s*(?:,|and|or|&|/|\+)\s*(?:an?\s+|the\s+)?{_FORMAT_WORD})*"
    rf"\s+(?:versions?\s+|files?\s+|copies\s+)?(?:of|for|from)\s+(?:this|the|these|those|that|my|our|it|everything)\b",
    re.I,
)

#: A question ABOUT a format, not a request for one.
_ABOUT_FORMAT_RE = re.compile(
    rf"^\s*(?:what(?:'s| is| are| does)|why|how (?:do|does|did|is|are|can|would)|explain|describe|"
    rf"tell me (?:about|how)|define|is (?:a|an|the)|are|can (?:a|an|the)|could (?:a|an)|does (?:a|an|the)|"
    rf"should (?:i|we)|which (?:is|one)|difference between|compare)\b.*?\b{_FORMAT_WORD}s?\b",
    re.I,
)
#: "show me code that…", "write a script that reads…" — the artifact is code.
#: Narrowed in AS3 (e) to requests FOR code (lexicon's code shapes): "code
#: of conduct", "the audit of the code" and "SQL audit findings" named no
#: code request and still vetoed the file.
_CODE_RE = re.compile(r"\b(?:regex|regular expression)\b", re.I)
#: An imperative edit at the start of a short message — "Add our logo.",
#: "Use a more formal tone." — is about the latest artifact when there is one.
_IMPERATIVE_EDIT_RE = re.compile(
    r"^\s*(?:please\s+)?(?:add|insert|include|remove|delete|drop|change|update|rename|retitle|shorten|expand|"
    r"rewrite|reword|revise|tighten|trim|fix|tweak|adjust|use (?:a )?(?:more|less)|make\b.{1,40}?\b(?:shorter|longer|simpler|clearer|concise|formal|professional))\b",
    re.I,
)
#: Polite imperatives are requests: "can you make…", "could you create…".
_POLITE_RE = re.compile(r"^\s*(?:can|could|would|will|please|pls|kindly)\b\s*(?:you|u)?\s*(?:please\s+)?", re.I)

#: A negation, then at most a few adverbs, then a creation verb (any form:
#: "making", "created") and the rest of that clause up to a comma, an
#: "and" or a clause end — the words the rules must not read. "Don't
#: forget to" and "don't hesitate to" are not here on purpose: they ask
#: for the file; nor is "stop" ("stop making excuses and create a PDF").
_NEGATION = (
    r"(?:don['’]?t|dont|do not|never|no need to|there'?s no need to|(?:must|should|shall|will|would|can|could)\s+not|"
    r"mustn['’]?t|shouldn['’]?t|won['’]?t|wouldn['’]?t|can['’]?t|cannot|rather than|instead of|without|not(?:\s+to)?)"
)
_NEGATION_ADVERBS = r"(?:just|simply|actually|really|even|ever|also|then|please|bother(?:\s+to)?|go\s+and|go\s+ahead\s+and|try\s+to|need\s+to|have\s+to|want\s+to|you\s+to)"
_NEGATED_VERBS = (
    r"(?:mak(?:e|es|ing)|creat(?:e|es|ing|ed)|generat(?:e|es|ing)|build(?:s|ing)?|writ(?:e|es|ing)|draft(?:s|ing)?|prepar(?:e|es|ing)|"
    r"produc(?:e|es|ing)|compil(?:e|es|ing)|assembl(?:e|es|ing)|put(?:ting)?\s+together|design(?:s|ing)?|develop(?:s|ing)?|export(?:s|ing)?|"
    r"sav(?:e|es|ing)|download(?:s|ing)?|print(?:s|ing)?|render(?:s|ing)?|giv(?:e|es|ing)\s+me|send(?:s|ing)?\s+me|turn(?:s|ing)?|"
    r"convert(?:s|ing)?|shar(?:e|es|ing)|provid(?:e|es|ing)|deliver(?:s|ing)?|hand(?:s|ing)?\s+(?:over|me)|supply(?:ing)?)"
)
_NEGATED_CLAUSE_RE = re.compile(rf"\b{_NEGATION}\s+(?:{_NEGATION_ADVERBS}\s+)*{_NEGATED_VERBS}\b(?:(?!\band\b)[^.;:!?,\n])*", re.I)
#: "I can't make a spreadsheet myself, can you build one?" — a first-person
#: negation describes the person, not the instruction; it is left in place
#: so the request after it still reads as a request (the security-fix
#: review of 2026-09-12 found the clause rule swallowing it).
_FIRST_PERSON_NEGATION_RE = re.compile(rf"\b(?:i|we)\s+(?:{_NEGATION_ADVERBS}\s+)*(?:{_NEGATION})\b", re.I)
#: "not a Word document", "rather than a deck", "instead of Excel": a
#: format the person ruled out, which must not become one of the files.
_NEGATED_FORMAT_RE = re.compile(rf"\b(?:not|never|no|rather than|instead of|don['’]?t\s+want|do\s+not\s+want|no\s+need\s+for)\s+(?:(?:as|in|into|to)\s+)?(?:an?\s+|the\s+)?{_FORMAT_WORD}\b", re.I)
#: A line of a pasted table: a tab, a pipe, or a comma/semicolon record
#: of four or more cells with no space after the separators (a CSV export;
#: a sentence puts a space after its commas). The prose the row count is
#: read from ends at the first such line (#3).
def _is_table_line(line: str) -> bool:
    """A tab, a pipe, or three consecutive `,`/`;` separators none of which is
    followed by a space or tab. The same language as the old
    r"\\t|\\||(?:[^,;\\n]*[,;](?![ \\t])){3}", read in one pass: that regex
    rescanned the rest of the line from every position, so one 4,000-character
    line cost tens of milliseconds on the event loop (CI, 2026-09-15)."""
    if "\t" in line or "|" in line:
        return True
    run = 0
    last = len(line) - 1
    for i, ch in enumerate(line):
        if ch == "\n":
            run = 0
        elif ch == "," or ch == ";":
            if i < last and line[i + 1] in " \t":
                run = 0
            else:
                run += 1
                if run >= 3:
                    return True
    return False

# --- follow-ups --------------------------------------------------------------

_REFERENCE_RE = re.compile(
    r"\b(?:it|this|that|the (?:previous|last|same|current|existing|earlier) (?:one|file|document|doc|deck|presentation|report|spreadsheet|workbook|brief|proposal|version|dataset|csv)|"
    r"the (?:file|document|doc|deck|presentation|report|spreadsheet|workbook|brief|proposal|sop|memo|pdf|docx|pptx|xlsx|csv|dataset)|"
    r"(?:slide|page|sheet|section|chapter|tab)\s+\d+|the (?:title|intro|introduction|conclusion|summary|chart|table|cover|tone|font|logo))\b",
    re.I,
)
_EDIT_VERBS_RE = re.compile(
    r"\b(?:make\b.{1,40}?\b(?:shorter|longer|briefer|simpler|clearer|more \w+|less \w+|formal|professional|concise|punchier)|"
    r"shorten|lengthen|expand|trim|cut|tighten|rewrite|reword|rephrase|revise|edit|update|change|modify|tweak|adjust|fix|"
    r"rename|retitle|add|insert|include|remove|delete|drop|replace|swap|move|reorder|restructure|"
    r"use (?:a )?(?:more|less)|go back to|revert to|restore|redo)\b",
    re.I,
)
#: A conversion names the SAME content in another form: "convert it to",
#: "export as", "also as", "a Word version". "Turn this into an Excel
#: tracker" and "make it a deck" are creation verbs and stay with create —
#: the content is the conversation's, and the engine makes a new artifact
#: of the requested kind rather than refusing to convert the latest one.
_CONVERT_RE = re.compile(
    rf"\b(?:convert|export|save|also|too|as well|another|a copy|same)\b(?:\W+\w+){{0,8}}?\W+(?:as|to|into|in)\s+(?:an?\s+|the\s+)?{_FORMAT_WORD}\b"
    rf"|\b(?:also|too)\s+(?:as|in)\s+(?:an?\s+)?{_FORMAT_WORD}\b"
    rf"|\b(?:{_FORMAT_WORD})\s+(?:version|copy|too|as well)\b",
    re.I,
)
_PREVIOUS_ANSWER_RE = re.compile(
    r"\b(?:(?:the|your|that) (?:previous|last|above|earlier|prior) (?:answer|reply|response|message|summary|explanation)|"
    r"(?:what|everything) you (?:just )?(?:said|wrote|explained)|your answer|that answer|this answer|the answer above|the above)\b",
    re.I,
)
_VERSION_RE = re.compile(r"\b(?:version|v)\s*(\d{1,3})\b", re.I)
#: "500 rows", "1,000 records", "250 sample entries", "500 rows of data".
_ROW_COUNT_RE = re.compile(
    r"\b(\d{1,3}(?:,\d{3})+|\d{1,6})\s+(?:(?:realistic|sample|random|synthetic|dummy|test|mock|fake|data|unique|distinct|new)\s+){0,3}"
    r"(?:rows|records|entries|lines|samples|data points|customers|candidates|employees|leads|accounts|contacts|transactions|orders|items|people|users)\b",
    re.I,
)
#: The first clause: what comes before the first `.`, `;`, `:` or " and then ".
_CLAUSE_END_RE = re.compile(r"[.;:]|\s+and then\s+", re.I)


@dataclass
class ArtifactIntent:
    action: Action
    #: Formats the person named, in order. Empty means "policy decides".
    formats: List[str] = field(default_factory=list)
    #: For edit/convert: what the person pointed at.
    reference: str = "none"          # none | latest | previous_answer | named
    reference_hint: str = ""         # "deck", "slide 4", "version 1", a title fragment
    version: Optional[int] = None    # "go back to version 1"
    rule: str = "none"
    ambiguous: bool = False
    #: The instruction with the reference words left in — the composer
    #: needs "make slide 4 shorter" verbatim. Whitespace-collapsed and cut
    #: at `_DECIDE_CHARS`: it is the DECISION's view of the text.
    instruction: str = ""
    #: A create that is a NEW artifact even though the conversation holds
    #: one and later sentences say "make it …" (CONTRACT-2 §5).
    new_artifact: bool = False
    #: "500 rows|records|entries" — how many data rows were asked for, for
    #: the generator; None when the text names no count.
    row_count: Optional[int] = None
    #: The ORIGINAL text, untruncated, tabs and newlines intact — the
    #: engine's view. A 30-row pasted table lives here; `instruction`
    #: flattens it (discovery of 2026-09-12, C5).
    raw_text: str = ""
    # --- AS3 (2026-09-15) ---------------------------------------------------
    #: What the file is made FROM: the most recent substantial answer, an
    #: upload, an existing artifact, the conversation, or nothing decided.
    target: str = "none"             # previous_answer | upload | artifact | conversation | none
    #: The words ask how something LOOKS (colours, fonts, orientation…).
    style_request: bool = False
    #: The words ask for a chart or plot.
    chart_request: bool = False
    #: en | hinglish | hi | gu | gujlish — the request's language form.
    language: str = "en"
    #: The upload formats (or names) the words point at as the source.
    upload_refs: List[str] = field(default_factory=list)
    #: The classifier decided this intent (not the rules).
    llm_used: bool = False
    #: The artifact the UI's "Edit with a prompt" named (ownership is checked
    #: by the caller before it gets here).
    artifact_id_hint: Optional[str] = None
    #: The `visuals.Visual.token` of a visual the person asked for that this
    #: platform has no chart type for ("map", "sankey", …). Set only with
    #: action "none": the turn is answered in chat, by `visuals.refusal_for`,
    #: and opens no job (2026-09-16 — a map request became a Word file).
    unsupported_visual: str = ""

    @property
    def wants_file(self) -> bool:
        return self.action != "none"


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def _first_clause(low: str) -> str:
    m = _CLAUSE_END_RE.search(low)
    return low[: m.start()] if m else low


def _without_negated_clauses(low: str) -> str:
    """`low` with every negated creation clause blanked (#9): the rules
    then read "don't create a pdf, just answer here" as ", just answer
    here", and "create a pdf, not a word document" as "create a pdf,
    document". A FIRST-PERSON negation ("I can't make a spreadsheet
    myself, can you build one?") describes the person, not the
    instruction, and is left alone. Bounded input (the caller cut it at
    _DECIDE_CHARS); the patterns have no nested quantifier over a word gap."""
    def clause(m: "re.Match[str]") -> str:
        head = low[max(0, m.start() - 12):m.start()]
        return m.group(0) if _FIRST_PERSON_NEGATION_RE.search(head + m.group(0)[:24]) else " "

    return _NEGATED_FORMAT_RE.sub(" ", _NEGATED_CLAUSE_RE.sub(clause, low))


def _prose_before_table(original: str) -> str:
    """The lines before the first table line (#3): what the row count is
    read from. A message with no table line is all prose. Linear: one
    regex per line, stopping at the first hit, over the decision's prefix
    of the text."""
    head = original[:_DECIDE_CHARS]
    table = None
    if "\n" in head.strip():  # a table is never one line; a one-line turn is never parsed
        try:
            from .tables import parse_table

            table = parse_table(head)
        except Exception:  # noqa: BLE001 — the parser is a courtesy here; the line scan below is the rule
            table = None
    if table is not None and getattr(table, "prose_before", None) is not None:
        # The parser knows every delimiter it accepts (a 3-column comma
        # paste, a space-aligned table), so its prose_before is the honest
        # cut (security-fix review 2026-09-12, #3 residual).
        return " ".join(str(table.prose_before).split())
    out: List[str] = []
    for line in head.splitlines():
        if _is_table_line(line):
            break
        out.append(line)
    return " ".join(" ".join(out).split())


def _row_count(low: str) -> Optional[int]:
    m = _ROW_COUNT_RE.search(low)
    if not m:
        return None
    try:
        n = int(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return n if n > 0 else None


def _positional_create(low: str) -> bool:
    """Does the FIRST clause ask for a new file? A strict creation verb
    with an article and an artifact noun, or "new|another|separate|fresh|
    second <file noun>". A clause that points at a PART ("slide 4") is an
    edit whatever its verb."""
    clause = _first_clause(low)
    if re.search(r"\b(?:slide|page|sheet|section|chapter|tab)\s+\d+\b", clause):
        return False
    return bool(_POSITIONAL_CREATE_RE.search(clause) or _NEW_FILE_RE.search(clause))


# --------------------------------------------------------- AS3 vocabulary --

#: A bare reference to what came before: "it", "this", "the above audit",
#: "your last response", and the normaliser's `_this_` (isko, इसे, આને).
_BARE_REF_RE = re.compile(
    r"\b(?:it|this|that|these|those|_this_|everything|all\s+of\s+(?:it|this|that)|all\s+that|"
    r"the\s+above(?:\s+\w+)?|above\s+(?:answer|report|audit|content|text|one|response|reply|table|list)|"
    r"(?:the|your|that|this|my)\s+(?:last\s+|previous\s+|above\s+|earlier\s+|whole\s+|full\s+)?"
    r"(?:answer|reply|response|report|audit|summary|analysis|content|text|findings|explanation|plan|write[- ]?up|table|list|output)"
    r"(?:\s+above)?)\b",
    re.I,
)
#: Hand-over verbs of a follow-up: the words that pass something over.
_FOLLOWUP_VERB_RE = re.compile(
    r"\b(?:give|provide|send|share|make|put|turn|convert|export|save|download|format|wrap|need|want|get|deliver|create|"
    r"generate|prepare|compile|render|print|_give_|_convert_|(?:can|could|may)\s+i\s+have|let\s+me\s+have)\b",
    re.I,
)
#: Verbs that keep what exists: the answer is the content.
_KEEP_VERB_RE = re.compile(r"\b(?:save|download|export|convert|_convert_)\b", re.I)
#: "as a file", "into a nice looking document", "in a doc".
_AS_FILE_RE = re.compile(rf"\b(?:as|in|into|to)\s+(?:an?\s+)?(?:{_ADJ}\s+){{0,2}}(?:file|document|doc|downloadable)\b", re.I)
#: A format with nothing but "please"/"version"/"of this": "docx version
#: please", "pdf of this pls", "word file too".
_FORMAT_ONLY_RE = re.compile(
    rf"^\W*(?:(?:a|an|the|just|only|also|and)\s+)?(?:{_ADJ}\s+)?(?:{_FORMAT_WORD})(?:\s+(?:file|version|copy|format|doc))?"
    rf"(?:\s+(?:of|for)\s+(?:it|this|that|_this_|the\s+above|everything|all\s+of\s+(?:it|this|that)))?"
    rf"(?:\s+(?:please|too|as\s+well|also))*\W*$",
    re.I,
)
#: The normaliser's verb-final forms: "docx file _in_ _give_", "pdf _give_".
_SOV_RE = re.compile(
    rf"\b(?:{_FORMAT_WORD}|{_ARTIFACT_NOUNS}|file|sheet|charts?)\b(?:\s+\S+){{0,6}}?\s+(?:_give_|_convert_)",
    re.I,
)
#: A new deliverable with its own topic: "a Word document summarizing the
#: vendors", "a pdf on two-factor authentication" — a create, not an export
#: of the answer, unless the topic IS the reference ("a word version of the
#: audit").
_NEW_TOPIC_RE = re.compile(
    rf"\b(?:an?|new|another|some)\s+(?:{_ADJ}\s+){{0,2}}(?:{_FORMAT_WORD}|{_FILE_NOUNS}|charts?)\b(?:\s+(?:file|version|copy|report|document|deck|sheet))?"
    rf"\s+(?:about|on|for|summari[sz]ing|covering|regarding|listing|of|showing|explaining|describing|comparing)\s+"
    rf"(?!(?:it|this|that|these|those|_this_|everything|the\s+above|all\s+of|the\s+(?:answer|audit|report|findings|summary|table|content|above)|your|above)\b)",
    re.I,
)
#: A text deliverable that is NOT a file: "draft an email telling the team
#: the report is delayed", "write a short paragraph about our policy".
_TEXT_OBJECT_RE = re.compile(
    r"^\W*(?:(?:please|just|pls|kindly|ok|okay|now)\s+)*(?:can\s+you\s+|could\s+you\s+)?(?:write|draft|compose|give\s+me|make|create|prepare|(?:i\s+|we\s+)?(?:need|want))\s+(?:me\s+)?(?:an?\s+|the\s+|some\s+)?(?:\w+\s+){0,2}?"
    r"(?:email|e-mail|mail|message|reply|response|paragraph|poem|story|tweet|post|caption|essay|cover\s+letter|note|list|table|summary|outline|bio|headline|slogan|"
    r"tips|ideas|steps|examples|suggestions|points|reasons|questions|advice|pointers|options)s?\b"
    r"(?!\s+(?:document|doc|docx|file|report|pdf|sheet|spreadsheet|deck|presentation|slides|workbook))",
    re.I,
)
#: A request marker for a destination-only create ("in excel?", "as pdf
#: please"): without one, "I have this in Excel" is a statement.
_ASKING_RE = re.compile(
    r"\?|\b(?:please|kindly|can\s+you|could\s+you|would\s+you|can\s+i|could\s+i|i\s+(?:need|want|would\s+like)|let\s+me\s+have|"
    r"give|send|share|provide|make|create|generate|build|prepare|produce|export|convert|turn|put|save|download|format|wrap|get|"
    r"deliver|compile|draft|write|design|render|print|restyle|reformat|redesign|redo|recreate|polish|extract|transcribe|copy|"
    r"_give_|_convert_)\b",
    re.I,
)
_WH_QUESTION_RE = re.compile(r"\b(?:what|which|why|who|where|when)\b", re.I)
_KYA_WHAT_RE = re.compile(r"\b(?:kya|shu|su)\s+(?:likh|daal|dal|rakh|hona|hovu|lakh)\w*", re.I)
#: "make it a docx", "turn this into an excel": the format is the TARGET (verifier 2026-09-15).
_MAKE_IT_FORMAT_RE = re.compile(rf"\b(?:make|turn|change|switch)\s+(?:it|this|that|_this_)\s+(?:in)?to\s+(?:an?\s+)?{_FORMAT_WORD}\b|\b(?:make|turn)\s+(?:it|this|that|_this_)\s+(?:an?\s+)?{_FORMAT_WORD}\b", re.I)
_STATEMENT_RE = re.compile(r"^\W*(?:i|we|they|he|she)\s+(?:have|had|keep|kept|store|use|used|got|received|opened|saw|read)\b", re.I)
#: Chart phrasing that asks for one: a verb, "chart of|with|for", a chart
#: phrase leading the message, or a data colon.
_CHART_ASK_RE = re.compile(
    r"\b(?:make|create|generate|build|draw|plot|render|give|show\s+me|prepare|produce|visuali[sz]e|_give_|need|want)\b"
    # Noun-first ("Histogram of hours", "box plot of salary by department")
    # only at the start, and never a story's plot or a maths graph to explain
    # (verifier 2026-09-15: "plot of the movie Inception" and "explain the
    # graph of y=x^2" made chart files).
    r"|^\W*(?:an?\s+|the\s+)?(?:[\w-]+\s+)?(?:chart|graph|plot|histogram|heat\s*map|timeline)s?\s+(?:of|with|for|showing|comparing|by)\b"
    r"(?!\s+(?:the\s+|this\s+|that\s+|a\s+)?(?:movie|film|book|novel|story|show|series|play|episode|game|anime|manga|song|opera|poem)s?\b)"
    # AS3 integration (live 2026-09-15): "scatter of Salary vs Experience
    # with a trend line in a pdf" read as no request (lexicon._CHART_RE reads
    # the chart; this reads the noun-first ask).
    r"|^\W*(?:an?\s+|the\s+)?(?:scatter|bubble|waterfall|funnel|radar|pie|donut|doughnut|gantt)\s+(?:of|showing|comparing)\s+\S+(?:\s+\S+){0,6}?\s+(?:vs\.?|versus|by|per|against|over)\b"
    r"|^\W*(?:an?\s+)?(?:(?:bar|line|pie|donut|doughnut|area|scatter|stacked|column|bubble|radar|funnel|waterfall|gantt|box|combo|"
    r"horizontal|vertical|simple|colou?red|blue|red|green|monthly|weekly)[- ]?){1,2}(?:chart|graph|plot)s?\b"
    r"|^\W*(?:an?\s+)?(?:histogram|heat\s*map|scatter\s*plot|box\s*plot)\b|(?:chart|graph|plot)[^:]{0,60}:\s*\S",
    re.I,
)
#: An element of an existing file an edit points at.
_ELEMENT_RE = re.compile(
    r"\b(?:headings?|titles?|subtitle|header(?:\s+row)?|footer|body(?:\s+text)?|paragraphs?|columns?|rows?|cells?|sections?|slides?|"
    r"sheets?|tabs?|tables?|charts?|bars|legend|labels?|appendix|summary|conclusion|introduction|intro|cover(?:\s+page)?|pages?|"
    r"margins?|fonts?|bullets?|totals?|notes?|points?|numbers|figures|timeline|risk\s+matrix|toc|logo|colou?rs?|layout|"
    r"orientation|workbook|document|deck|tracker|report|spreadsheet|file|data|excel|csv|pdf|docx|doc|word\s+file|ppt|pptx|presentation)\b",
    re.I,
)
#: "put a chart of the scores in the workbook": `put` edits only with a
#: named existing file as the destination.
_PUT_IN_ARTIFACT_RE = re.compile(
    r"\b(?:put|place|drop|stick)\b.{1,80}?\b(?:in|into|on|to)\s+(?:the|this|that)\s+(?:workbook|document|deck|report|sheet|tracker|spreadsheet|presentation|pdf|docx|doc|file|slides?)\b",
    re.I,
)
#: The parts only a file has (a style or element edit names one of them);
#: _ELEMENT_RE less the words a chat answer has too (summary, points,
#: bullets, numbers, notes, data, colours, labels, timeline, report).
_FILE_PART_RE = re.compile(
    r"\b(?:headings?|titles?|subtitle|header(?:\s+row)?|footer|body(?:\s+text)?|paragraphs?|columns?|rows?|cells?|sections?|slides?|"
    r"sheets?|tabs?|tables?|charts?|bars|legend|appendix|cover(?:\s+page)?|pages?|margins?|fonts?|totals?|toc|logo|risk\s+matrix|"
    r"layout|orientation|landscape|portrait|workbook|document|deck|tracker|spreadsheet|file|excel|csv|pdf|docx|doc|word\s+file|ppt|pptx|presentation)\b"
    # The same parts in Hindi and Gujarati the normaliser leaves as written
    # (plurals such as शीर्षकों).
    r"|शीर्षक|हेडिंग|कॉलम|कालम|पंक्ति|पेज|पृष्ठ|फ़ॉन्ट|फॉन्ट|तालिका|टेबल|चार्ट|स्लाइड|शीट|फ़ाइल|फाइल|रिपोर्ट|दस्तावेज़|दस्तावेज"
    r"|શીર્ષક|હેડિંગ|કૉલમ|કોલમ|પંક્તિ|પેજ|પાન|ફોન્ટ|કોષ્ટક|ટેબલ|ચાર્ટ|સ્લાઇડ|શીટ|ફાઇલ|રિપોર્ટ|દસ્તાવેજ",
    re.I,
)
#: A pronoun that is the object of the change: "make it bold", "colour
#: this", "isme …" (normalised to _this_), "… in it."
_PRONOUN_OBJECT_RE = re.compile(
    r"\b(?:make|turn|set|keep|give|format|style|restyle|colou?r|change|bold|italici[sz]e|underline|highlight)\s+(?:the\s+whole\s+)?(?:it|this|that|_this_)\b"
    r"|(?:^|\s)_this_\b|\b(?:it|this|that)\s*(?:$|[.!?,;])|\b(?:in|on|of|to|for)\s+(?:it|this|that)\s*(?:$|[.!?,;])",
    re.I,
)
#: The file named with a determiner ("the report", "this deck").
_REFERENCE_FILE_RE = re.compile(
    r"\b(?:the|this|that|my|our)\s+(?:(?:previous|last|same|current|existing|earlier)\s+)?(?:one|file|document|doc|deck|presentation|report|"
    r"spreadsheet|workbook|brief|proposal|sop|memo|pdf|docx|pptx|xlsx|csv|dataset|version)\b",
    re.I,
)
#: A request said as a noun phrase: "sheet with colors for the budget",
#: "PDF in landscape with Arial about the travel policy", "need a
#: presentaion on cyber security".
_NOUN_PHRASE_REQUEST_RE = re.compile(
    rf"^\W*(?:(?:need|want|require|would\s+like|just)\s+)?(?:an?\s+|the\s+|some\s+)?(?:{_ADJ}\s+){{0,2}}(?:{_FORMAT_WORD}|sheet|{_FILE_NOUNS})"
    rf"(?:\s+(?:file|document|report|sheet|deck))?\s+(?:with|for|of|about|on|listing|showing|covering|in\s+(?:landscape|portrait|a4|colou?rs?))\b",
    re.I,
)
#: Edit verbs the pre-AS3 list did not have ("complete the document",
#: "apply a formula", "fill in the owner column").
_MORE_EDIT_VERBS_RE = re.compile(r"\b(?:complete|finish|fill\s+in|apply|set|highlight|sort|restyle|reformat|merge|split|translate)\b", re.I)
#: The person asked for the answer HERE: "in the chat", "no download",
#: "just tell me", "in 3 lines", "yahin chat me".
_CHAT_ONLY_RE = re.compile(
    r"\b(?:(?:here\s+)?in\s+(?:the\s+)?chat|no\s+(?:download|file|files|attachment|pdf|doc)s?|without\s+(?:a\s+)?(?:file|download)|"
    r"(?:don'?t|do\s+not)\s+(?:need|want)\s+(?:a\s+|any\s+)?(?:file|download|document)|just\s+tell\s+me|answer\s+here|"
    r"in\s+\d+\s+(?:lines?|points?|bullets?|sentences?|words?)|yahi[n]?\s+chat|chat\s+(?:_in_|me|mein|ma)\b|here\s+only)\b",
    re.I,
)
#: ...unless a file is still named as the deliverable ("no pdf, give me a docx").
_FILE_DESPITE_RE = re.compile(r"\b(?:instead|rather)\b", re.I)
#: A story's plot, not a chart: "plot of the movie Inception".
_STORY_PLOT_RE = re.compile(
    r"\bplots?\s+(?:of|in|for|from)\s+(?:the\s+|this\s+|that\s+|a\s+|an\s+)?(?:movie|film|book|novel|story|show|series|play|episode|game|anime|manga|song|opera|poem)s?\b",
    re.I,
)
_QUESTION_ABOUT_RE = re.compile(r"^\W*(?:what|which|why|how|who|where|when|is|are|does|do|did|was|were)\b", re.I)
#: The upload named as the source: "the pdf I uploaded", "from the attached
#: sheet", "this file" (AS3 (h)).
_UPLOAD_SOURCE_RE = re.compile(
    r"\b(?:uploaded|attached|i\s+(?:just\s+)?(?:uploaded|attached|shared|sent)|this\s+(?:file|document|pdf|csv|sheet|spreadsheet|excel|docx|workbook|data)|"
    r"_this_\s+(?:file|document|pdf|csv|sheet|excel|docx|data)|the\s+file|from\s+the\s+(?:file|pdf|csv|sheet|excel|docx|document|spreadsheet|workbook|data\s+file))\b",
    re.I,
)
_FORMAT_NAMES_FOR_UPLOAD = {"pdf": "pdf", "docx": "docx", "doc": "docx", "word": "docx", "xlsx": "xlsx", "excel": "xlsx", "sheet": "xlsx",
                            "spreadsheet": "xlsx", "workbook": "xlsx", "csv": "csv", "pptx": "pptx", "txt": "txt", "md": "md"}

#: A turn is SUBSTANTIAL — worth exporting — at this size, or with structure.
SUBSTANTIAL_ANSWER_CHARS = 400
_STRUCTURE_RE = re.compile(r"(?m)^\s{0,3}(?:#{1,6}\s+\S|[-*+]\s+\S|\d{1,3}[.)]\s+\S|\|.*\|\s*$)")
#: The one-line sentence an artifact turn leaves in history (engines/
#: artifact._sentence): exporting it would make a file of that sentence.
_ARTIFACT_SENTENCE_RE = re.compile(
    r"^\s*(?:Created\s+\*\*.+?\*\*\s+(?:as|in)\s|Updated\s+\*\*.+?\*\*\s+as\s|Converted\s+\*\*.+?\*\*\s+to\s|"
    r"Created the (?:CSV )?dataset with|Done — I preserved)",
)


#: A clause the normaliser marked as a negated hand-over ("pdf mat banao").
_CLAUSE_RE = re.compile(r"[^.;,!?\n]+")


def _blank_neg_token_clauses(text: str) -> str:
    """Blank every clause that holds a `_neg_` token. Same result as
    re.sub(r"[^.;,!?\\n]*_neg_[^.;,!?\\n]*", " ", text), which retried the
    whole clause from every start position — quadratic on one long clause."""
    if "_neg_" not in text:
        return text
    return _CLAUSE_RE.sub(lambda m: " " if "_neg_" in m.group(0) else m.group(0), text)


def turn_text(turn: Any) -> str:
    content = (turn or {}).get("content") if isinstance(turn, dict) else None
    if isinstance(content, list):
        return "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def is_artifact_turn(turn: Any) -> bool:
    """An assistant turn that IS a file card: its meta carries artifacts, or
    its whole content is the engine's one-line sentence."""
    if not isinstance(turn, dict) or str(turn.get("role")) != "assistant":
        return False
    meta = turn.get("meta")
    if isinstance(meta, dict) and meta.get("artifacts"):
        return True
    text = turn_text(turn)
    return len(text) <= 1200 and bool(_ARTIFACT_SENTENCE_RE.match(text))


def _is_substantial(text: str) -> bool:
    t = (text or "").strip()
    return len(t) >= SUBSTANTIAL_ANSWER_CHARS or bool(_STRUCTURE_RE.search(t[:20000]))


def substantial_answer_index(history: Sequence[dict], *, window: int = 3) -> Optional[int]:
    """The index in `history` of the most recent SUBSTANTIAL assistant turn
    among the last `window` assistant turns (AS3 (b)): "thanks" → "you're
    welcome" → "give it in docs" still finds the report. Artifact turns are
    never substantial."""
    seen = 0
    for i in range(len(history) - 1, -1, -1):
        turn = history[i]
        if not isinstance(turn, dict) or str(turn.get("role")) != "assistant":
            continue
        seen += 1
        if seen > window:
            break
        if is_artifact_turn(turn):
            continue
        if _is_substantial(turn_text(turn)):
            return i
    return None


def last_turn_is_artifact(history: Sequence[dict]) -> bool:
    """Is the most recent ASSISTANT turn a file card?"""
    for turn in reversed(list(history or ())):
        if isinstance(turn, dict) and str(turn.get("role")) == "assistant":
            return is_artifact_turn(turn)
    return False


def _upload_formats_named(low: str, upload_formats: Sequence[str]) -> List[str]:
    """The upload formats the words point at as the source."""
    named: List[str] = []
    for m in re.finditer(r"\b(pdf|docx|doc|word|xlsx|excel|sheet|spreadsheet|workbook|csv|pptx|txt|md)\b", low):
        fmt = _FORMAT_NAMES_FOR_UPLOAD.get(m.group(1))
        if fmt and fmt in upload_formats and fmt not in named:
            named.append(fmt)
    if not named and upload_formats and _UPLOAD_SOURCE_RE.search(low):
        named = [str(f) for f in upload_formats][:5]
    return named


#: The words a content-free hand-over is made of; anything else is a topic.
_FUNCTION_WORDS_RE = re.compile(
    rf"\b(?:{_FORMAT_WORD}|{_ADJ}|a|an|the|i|we|me|my|us|you|your|it|this|that|can|could|would|will|please|pls|just|only|also|too|now|"
    r"want|need|like|would|get|give|provide|send|share|make|put|turn|convert|export|save|download|format|wrap|deliver|create|generate|"
    r"prepare|compile|render|print|version|copy|file|files|doc|docs|document|downloadable|as|in|into|to|of|for|and|or|kindly|one|"
    r"_give_|_convert_|_in_|_this_|ok|okay|hey|hi|thanks|sir|bro|yaar|bhai|"
    # How it should LOOK is not a topic: "provide the dox file, standard format, classy look".
    r"look|looking|style|styled|design|layout|format|formatting|font|fonts|colou?rs?|theme|with|proper|properly)\b|[^\w\s]",
    re.I,
)


def _content_words(low: str) -> bool:
    return bool(_FUNCTION_WORDS_RE.sub(" ", low).split())


def _export_shape(low: str, explicit: Sequence[str]) -> Optional[str]:
    """The follow-up that hands the previous answer over in a format (AS3
    (b)). Returns the rule name, or None."""
    if _NEW_TOPIC_RE.search(low):
        return None
    if _TEXT_OBJECT_RE.search(low) and not explicit:
        return None
    # Verifier 2026-09-15: a WH-question about the file's content is not a
    # hand-over ("my boss said pdf bana do, so what should go in it?", "docs me
    # kya likhna chahiye").
    if ("?" in low and _WH_QUESTION_RE.search(low)) or _KYA_WHAT_RE.search(low):
        return None
    ref = bool(_BARE_REF_RE.search(low))
    # "I need to know the page count of this PDF": a question, whatever the verb.
    verb = bool(_FOLLOWUP_VERB_RE.search(low)) and not (LX.reads_source(low) and not _AS_FORMAT_RE.search(low))
    dest = bool(_AS_FORMAT_RE.search(low) or (_AS_FILE_RE.search(low) and verb))
    if ref and dest:
        asked = "?" in low or re.search(r"\bplease\b", low)
        if verb or (asked and not LX.reads_source(low)):
            return "export-followup"
        return None
    if ref and verb and explicit:
        return "export-followup-handover"
    # "इसे फाइल में सेव कर दो", "યહ ફાઇલ ડાઉનલોડ કરો", "save this as a document":
    # a reference, a file noun and a keeping verb.
    if ref and _KEEP_VERB_RE.search(low) and re.search(r"\b(?:file|document|doc|downloadable)\b(?:\s+_in_)?\s+(?:\S+\s+)?(?:save|download|export|_convert_|as)\b|\b(?:as|in|into|to)\s+(?:an?\s+)?(?:\S+\s+)?(?:file|document|doc)\b", low):
        return "export-followup-file"
    if _FORMAT_ONLY_RE.match(low) and explicit:
        return "export-format-only"
    if explicit and verb and not _content_words(low):
        return "export-content-free"
    # Without a reference, only a CONTENT-FREE postposition hands the answer
    # over ("pdf bana do"); "sales ki report banao" names a new topic and is a
    # create (verifier 2026-09-15: it exported the previous answer).
    if _SOV_RE.search(low) and (ref or (len(low.split()) <= 6 and not _content_words(low))):
        return "export-postposition"
    return None


def decide(
    text: str,
    *,
    has_artifacts: bool = False,
    artifact_hints: Sequence[str] = (),
    has_assistant_answer: bool = False,
    upload_formats: Sequence[str] = (),
    last_turn_is_artifact: bool = False,
    artifact_id: Optional[str] = None,
) -> ArtifactIntent:
    """Decide from the words alone; nothing here calls a model.

    `has_artifacts`: the conversation already holds at least one artifact
    (so "make it shorter" can mean the file). `artifact_hints`: short labels
    of those artifacts (kind words / titles) so "the deck" can be matched to
    one. `has_assistant_answer`: there is a previous assistant turn to
    export — callers pass whether a SUBSTANTIAL one exists
    (`substantial_answer_index`). `upload_formats`: formats of the files
    attached to this turn (a read verb on one of them is a question).
    `last_turn_is_artifact`: the most recent assistant turn is a file card.
    `artifact_id`: the artifact the UI's "Edit with a prompt" names.
    """
    # A request for a file is stated in the first sentences; what follows is
    # material. The rules run on a bounded prefix, because a regex with a
    # word gap is quadratic in what it scans and a 250 KB paste held the
    # event loop for minutes (review, 2026-09-11). The engine gets the
    # whole text in `raw_text`.
    original = text or ""
    raw = _clean(original)[:_DECIDE_CHARS]
    if not raw:
        return ArtifactIntent("none", rule="empty")
    # The rules read the NORMALISED text (AS3: typos, Hindi/Gujarati/
    # Hinglish/Gujlish mapped to the rule vocabulary) with its negated
    # creation clauses blanked (#9); `raw` — the instruction the composer
    # gets — keeps the person's words.
    low = LX.normalize(_without_negated_clauses(raw.lower()))
    low = _without_negated_clauses(_blank_neg_token_clauses(low))
    uploads = [str(f).lower().lstrip(".") for f in (upload_formats or ()) if f]
    explicit = F.explicit_formats(low)
    rows = _row_count(_without_negated_clauses(_prose_before_table(original).lower()))
    language = LX.language_of(raw)
    style = bool(LX.style_phrases(low))
    chart = LX.chart_signal(low)

    def made(action: Action, **kw) -> ArtifactIntent:
        kw.setdefault("formats", explicit)
        kw.setdefault("instruction", raw)
        if action == "none":
            kw.setdefault("target", "none")
        elif action == "export":
            kw.setdefault("target", "previous_answer")
        elif action in ("edit", "convert"):
            kw.setdefault("target", "artifact")
        else:
            refs = _upload_formats_named(low, uploads)
            kw.setdefault("target", "upload" if refs or (uploads and _UPLOAD_SOURCE_RE.search(low)) else "conversation")
            kw.setdefault("upload_refs", refs)
        kw.setdefault("style_request", style and action != "none")
        kw.setdefault("chart_request", chart and action != "none")
        return ArtifactIntent(action, raw_text=original, row_count=rows, new_artifact=(action == "create"),
                              language=language, **kw)

    # 0. The UI's "Edit with a prompt" names the artifact (AS3 (i)); the
    #    caller checked that the person owns it.
    if artifact_id:
        if _CONVERT_RE.search(low) and explicit:
            return made("convert", reference="named", reference_hint="", rule="ui-convert", artifact_id_hint=str(artifact_id))
        if LX.undo_signal(low) and not _VERSION_RE.search(low):
            return made("edit", reference="latest", rule="ui-undo", artifact_id_hint=str(artifact_id))
        version = _VERSION_RE.search(low)
        return made("edit", reference="named", reference_hint=f"version {version.group(1)}" if version else "",
                    version=int(version.group(1)) if version and re.search(r"\b(?:go back|revert|restore|use|return|switch|undo)\b", low) else None,
                    rule="ui-edit", artifact_id_hint=str(artifact_id))

    # 1. The HARD-NEGATIVE pass (AS3 (a)) and the old question/code checks:
    #    a format named as the source, a how-to, trivia, praise or a request
    #    for code is not a file. First, because "explain how to create a PDF
    #    in Python" has a creation verb.
    if _CHAT_ONLY_RE.search(low) and not _FILE_DESPITE_RE.search(low):
        return made("none", rule="chat-only", instruction="")
    shape = LX.negative_shape(low, uploads)
    if shape is not None:
        return made("none", rule=f"negative:{shape}", instruction="")
    if _CODE_RE.search(low) and not _AS_FORMAT_RE.search(low):
        return made("none", rule="code", instruction="")
    if _ABOUT_FORMAT_RE.search(low) and not _POLITE_RE.match(low):
        return made("none", rule="about-format", instruction="")

    # 1b. A VISUAL THIS PLATFORM CANNOT DRAW (2026-09-16). "plot this on a
    #     map", asked twice, opened a document job and came back as a Word
    #     file and a PDF with prose in them. There is no geographic chart
    #     type in chart_spec.CHART_TYPES, so no job can end in the picture
    #     that was asked for: the turn is answered in chat with a sentence
    #     `visuals.refusal_for` writes. It runs before the follow-up and
    #     creation rules — the conversation usually already holds the file
    #     the earlier chart request made — and only when the words name no
    #     OTHER deliverable: "put the map in a PDF report" still makes the
    #     report, whose chart is refused where charts are refused.
    _visual = VIS.asked_for(low)
    if _visual is not None and not explicit and F.kind_for(low, [])[1] == "default":
        return made("none", rule=f"unsupported-visual:{_visual.token}", instruction="",
                    unsupported_visual=_visual.token)

    # 2. Follow-ups on an existing artifact.
    if has_artifacts:
        version = _VERSION_RE.search(low)
        if version and re.search(r"\b(?:go back|revert|restore|use|return|switch|undo)\b", low):
            return made("edit", reference="named", reference_hint=f"version {version.group(1)}",
                        version=int(version.group(1)), rule="restore-version")
        if LX.undo_signal(low) and len(low.split()) <= 12 and not (explicit and _AS_FORMAT_RE.search(low)):
            # "undo that", "revert", "पहले जैसा कर दो" (AS3 (g)).
            return made("edit", reference="latest", rule="restore-version")
        # The ANSWER named as the source beats a conversion of the file:
        # "save the answer above as an excel file".
        if has_assistant_answer and _PREVIOUS_ANSWER_RE.search(low) and (explicit or _AS_FORMAT_RE.search(low) or _CREATE_RE.search(low)):
            return made("export", reference="previous_answer", rule="export-answer")
        # The last assistant turn IS a file card: "make it a docx" converts
        # that artifact — never an export of its one-line sentence.
        # Verifier 2026-09-15: a STYLE clause that names the file as a place
        # ("bold the first row ... in the pdf", "make the Status column red, in
        # the sheet") is an edit of that file, not a re-render in the same or
        # another format; and a remark ("I opened the docx on my phone and the
        # table is cut off") is not a request. A real target ("as a docx",
        # "make it a docx", "convert") still converts.
        _styled_place = style and not (_CONVERT_RE.search(low) or _AS_FORMAT_RE.search(low) or _MAKE_IT_FORMAT_RE.search(low))
        if last_turn_is_artifact and explicit and not _positional_create(low) and not _NEW_TOPIC_RE.search(low) and not _styled_place \
                and not _STATEMENT_RE.match(low) and (
            _BARE_REF_RE.search(low) or _FOLLOWUP_VERB_RE.search(low) or _AS_FORMAT_RE.search(low) or _FORMAT_ONLY_RE.match(low)
        ):
            return made("convert", reference="latest", reference_hint=_hint(low, artifact_hints, exclude=_target_words(explicit)),
                        rule="convert-artifact-turn")
        if _CONVERT_RE.search(low) and explicit:
            return made("convert", reference=_which(low, artifact_hints),
                        reference_hint=_hint(low, artifact_hints, exclude=_target_words(explicit)), rule="convert")
        # 2b. A new file, said first: "Create a professional PDF report on
        #     X. Make it visually professional." is a create, not an edit
        #     of the last artifact (CONTRACT-2 §5; discovery C2).
        if _positional_create(low):
            return made("create", rule="create-first-clause")
        # 2c. A pronoun follow-up after an ANSWER (not a file card) exports
        #     that answer even when files exist in the conversation.
        if has_assistant_answer and not last_turn_is_artifact and not chart and not style and not (
            _EDIT_VERBS_RE.search(low) or _MORE_EDIT_VERBS_RE.search(low)
        ):
            rule = _export_shape(low, explicit)
            if rule and not re.search(r"\b(?:also|too|as well|same|version|copy)\b", low):
                return made("export", reference="previous_answer", rule=rule)
        if _EDIT_VERBS_RE.search(low) and (_REFERENCE_RE.search(low) or _mentions_hint(low, artifact_hints)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit")
        if _IMPERATIVE_EDIT_RE.match(low) and len(low.split()) <= 12:
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit-imperative")
        # 2d. A style clause, or an edit verb on an element (AS3 (f)): "make
        #     the headings dark blue", "make the document landscape", "font
        #     Arial kar do". A question ABOUT the look is not an edit.
        asks = not _QUESTION_ABOUT_RE.match(low) or re.match(r"^\W*(?:can|could|would|will)\s+(?:you|the|it|we)\b", low)
        # Verifier 2026-09-15: in a conversation that holds any file, "give
        # me bullet points on climate change" (change), "highlight the main
        # takeaways from our discussion" and "bold claim: …, discuss" were
        # edits of that file. A style or element edit needs the words to
        # point at the file — a part only a file has, the file itself, a
        # pronoun that is the object ("make it bold") — or the last turn to
        # be the file card.
        anchored = last_turn_is_artifact or bool(_FILE_PART_RE.search(low) or _PRONOUN_OBJECT_RE.search(low)
                                                  or _REFERENCE_FILE_RE.search(low) or _mentions_hint(low, artifact_hints))
        asks = asks and anchored
        if style and asks and not (explicit and _AS_FORMAT_RE.search(low) and _CREATE_RE.search(low)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints),
                        rule="edit-style", style_request=True)
        if asks and _ELEMENT_RE.search(low) and (_EDIT_VERBS_RE.search(low) or _PUT_IN_ARTIFACT_RE.search(low) or _MORE_EDIT_VERBS_RE.search(low)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit-element")
        # "Also as PDF" with nothing else said.
        if explicit and re.match(r"^\s*(?:also|and|plus|too)?\s*(?:as|in)\s+(?:an?\s+)?\w+(?:\s+\w+)?\s*(?:too|as well|please)?\s*[.!]?\s*$", low):
            return made("convert", reference="latest", rule="convert-short")
        if explicit and re.search(r"\balso\b", low) and (_SOV_RE.search(low) or _FORMAT_ONLY_RE.match(low)):
            # "pdf version bhi chahiye" → "pdf version also _give_".
            return made("convert", reference="latest", rule="convert-short")

    # 3. Exporting the previous answer as a file.
    if has_assistant_answer and _PREVIOUS_ANSWER_RE.search(low) and (explicit or _AS_FORMAT_RE.search(low) or _CREATE_RE.search(low)):
        return made("export", reference="previous_answer", rule="export-answer")
    # 3b. A pronoun/postposition follow-up (AS3 (b)): "give it in docs",
    #     "isko docx me dedo", "इसे पीडीएफ में बदल दो". A chart is made, not
    #     exported. After a FILE CARD the same words convert that artifact.
    if not chart:
        rule = _export_shape(low, explicit)
        if rule and last_turn_is_artifact and explicit:
            return made("convert", reference="latest", rule="convert-artifact-turn")
        if rule and has_assistant_answer:
            return made("export", reference="previous_answer", rule=rule)

    # 4. Creation.
    if _TEXT_OBJECT_RE.search(low) and not explicit and not _AS_FORMAT_RE.search(low):
        # "draft an email telling the team the report is delayed".
        return made("none", rule="text-object", instruction="")
    as_format = bool(_AS_FORMAT_RE.search(low)) and bool(_ASKING_RE.search(low)) and not _STATEMENT_RE.match(low)
    sov = bool(_SOV_RE.search(low)) and not (("?" in low and _WH_QUESTION_RE.search(low)) or _KYA_WHAT_RE.search(low))
    chart_ask = chart and bool(_CHART_ASK_RE.search(low)) and not _QUESTION_ABOUT_RE.match(low) and not _STORY_PLOT_RE.search(low)
    # Explicit formats with an object and no verb: "XLSX, Word, PDF and
    # CSV of this audit please". A question is not this shape.
    if explicit and not _CREATE_RE.search(low) and _FORMAT_LIST_OBJECT_RE.match(low) and ("?" not in raw or _POLITE_RE.match(low)):
        return made("create", rule="create-formats-object")
    noun_first = bool(_NOUN_PHRASE_REQUEST_RE.match(low)) and not _QUESTION_ABOUT_RE.match(low) and "?" not in raw
    if _CREATE_RE.search(low) or as_format or _BEST_OR_ALL_RE.search(low) or sov or chart_ask or noun_first:
        if "?" in raw and not _POLITE_RE.match(low) and not explicit and not sov:
            # "Would a report help here?" — a creation verb, a document noun,
            # a question, no format: the one shape the rules cannot read.
            return made("none", rule="ambiguous", ambiguous=True)
        return made("create", rule="create-chart" if chart_ask and not (_CREATE_RE.search(low) or as_format or sov) else
                    ("create-postposition" if sov and not _CREATE_RE.search(low) else "create"))
    # Explicit formats with an object and no verb: "XLSX, Word, PDF and
    # CSV of this audit please". A question is not this shape.
    if explicit and _FORMAT_LIST_OBJECT_RE.match(low) and ("?" not in raw or _POLITE_RE.match(low)):
        return made("create", rule="create-formats-object")

    return made("none", rule="no-request", instruction="")


def _usable_hints(hints: Sequence[str]) -> List[str]:
    return [h for h in hints if h and len(h.strip()) >= _MIN_HINT_CHARS]


def _mentions_hint(low: str, hints: Sequence[str]) -> bool:
    return any(re.search(rf"\b{re.escape(h.lower().strip())}\b", low) for h in _usable_hints(hints))


def _which(low: str, hints: Sequence[str]) -> str:
    return "named" if _mentions_hint(low, hints) or re.search(r"\b(?:the (?:deck|presentation|document|report|spreadsheet|workbook|brief|proposal|sop|memo|dataset))\b", low) else "latest"


def _hint(low: str, hints: Sequence[str], *, exclude: Sequence[str] = ()) -> str:
    """What the person pointed at: a known artifact label, else the artifact
    noun they used, else the part ("slide 4", "the title"). `exclude` holds
    the words that name the TARGET of a conversion, which are not a hint."""
    for h in _usable_hints(hints):
        if re.search(rf"\b{re.escape(h.lower().strip())}\b", low):
            return h
    skip = {w.lower() for w in exclude}
    for m in re.finditer(r"\b(?:the )?(deck|presentation|document|report|spreadsheet|workbook|brief|proposal|sop|memo|dataset|pdf|docx|pptx|xlsx|csv)\b", low):
        if m.group(1) not in skip:
            return m.group(1)
    m = re.search(r"\b((?:slide|page|sheet|section)\s+\d+)\b", low)
    if m:
        return m.group(1)
    m = re.search(r"\bthe (title|intro|introduction|conclusion|summary|chart|table|cover|tone|font|logo)\b", low)
    return m.group(1) if m else ""


_FORMAT_WORDS_FOR = {
    "pdf": ("pdf",),
    "docx": ("docx", "word"),
    "pptx": ("pptx", "powerpoint", "ppt"),
    "xlsx": ("xlsx", "excel", "spreadsheet", "workbook"),
    "csv": ("csv", "cvs"),
}


def _target_words(explicit: Sequence[str]) -> List[str]:
    out: List[str] = []
    for f in explicit:
        out.extend(_FORMAT_WORDS_FOR.get(f, ()))
    return out


# ------------------------------------------------------------- classifier --

ClassifyHook = Callable[..., Awaitable[Any]]

#: The verdict fields a hook may return instead of an ArtifactIntent
#: (intent_llm.IntentVerdict): converted here, so the decision of what a
#: verdict MEANS stays with the rules' vocabulary.
_HOOK_ACTIONS = ("create", "export", "convert", "edit", "none")


def _should_consult(intent: ArtifactIntent, text: str) -> bool:
    """The band the model may decide (AS3 (3)): the rules found no request,
    no negative shape (or code/about-format veto) fired, and the message
    names a file, a format or a chart. The old ambiguous band is in it."""
    if intent.action != "none":
        return False
    if intent.ambiguous:
        return True
    if intent.rule.startswith(("negative:", "unsupported-visual:")) or intent.rule in ("code", "about-format", "empty", "text-object", "chat-only"):
        # A visual with no chart type cannot become a file whatever the
        # classifier believes; asking it would only buy back the document
        # the 2026-09-16 incident produced.
        return False
    return LX.file_signal(text[:_DECIDE_CHARS])


def verdict_to_intent(verdict: Any, rules: ArtifactIntent, *, has_artifacts: bool, has_assistant_answer: bool,
                      upload_formats: Sequence[str] = (), last_turn_is_artifact: bool = False) -> Optional[ArtifactIntent]:
    """A classifier verdict → an intent the engine can act on, or None. The
    rules' view of the text (instruction, raw_text, row_count, language)
    is kept; an action the context cannot carry is mapped to one it can:
    an edit or conversion with no artifact is a create, an export with no
    answer is a create, and a conversion right after an ANSWER exports it."""
    action = str(getattr(verdict, "action", "") or "none")
    if action not in _HOOK_ACTIONS or action == "none":
        return None
    formats = [f for f in (getattr(verdict, "formats", None) or []) if f in ("pdf", "docx", "pptx", "xlsx", "csv")]
    target = str(getattr(verdict, "target", "") or "none")
    if action == "edit" and not has_artifacts:
        # An edit of a file that does not exist: the model misread a remark
        # about some file as a request. Only an ATTACHED file can be restyled
        # into a new one.
        if not upload_formats:
            return None
        action = "create"
    if action == "convert" and not has_artifacts:
        action = "export" if (has_assistant_answer and not last_turn_is_artifact) else "create"
    if action == "export" and not has_assistant_answer:
        action = "create"
    if action == "convert" and has_assistant_answer and not last_turn_is_artifact and target == "previous_answer":
        action = "export"
    out = ArtifactIntent(
        action,
        formats=formats or list(rules.formats),
        reference={"export": "previous_answer", "edit": "latest", "convert": "latest"}.get(action, "none"),
        rule="model",
        instruction=rules.instruction,
        new_artifact=action == "create",
        row_count=rules.row_count,
        raw_text=rules.raw_text,
        language=rules.language,
        style_request=bool(getattr(verdict, "style_request", False)),
        chart_request=bool(getattr(verdict, "chart_request", False)),
        llm_used=True,
    )
    if action == "export":
        out.target = "previous_answer"
    elif action in ("edit", "convert"):
        out.target = "artifact"
    elif upload_formats and target == "upload":
        out.target = "upload"
        out.upload_refs = [str(f) for f in upload_formats][:5]
    else:
        out.target = "conversation"
    return out


async def decide_with_hook(
    text: str,
    hook: Optional[ClassifyHook],
    **kw,
) -> ArtifactIntent:
    """The rules, then — only for the band the rules cannot read — the hook
    (a strict-JSON classifier the engine provides). No hook, or a hook that
    fails, means the rules' answer: the text answer is the safe default.

    Keyword arguments `decide()` does not take (upload_names, artifact_titles,
    last_answer_head) are for the hook's context; a hook that accepts
    keyword arguments receives them all. A hook may return an ArtifactIntent
    (the pre-AS3 shape) or a verdict with `action`/`confidence` fields
    (intent_llm.IntentVerdict)."""
    decide_kw = {k: v for k, v in kw.items() if k in _DECIDE_KWARGS}
    intent = decide(text, **decide_kw)
    if hook is None or not _should_consult(intent, text or ""):
        return intent
    try:
        try:
            verdict = await hook(text, **kw)
        except TypeError as exc:
            if "unexpected keyword" not in str(exc) and "positional argument" not in str(exc):
                raise
            verdict = await hook(text)
    except Exception:  # noqa: BLE001 — a classifier outage is the rules' answer
        return intent
    if verdict is None:
        return intent
    if not isinstance(verdict, ArtifactIntent):
        if str(getattr(verdict, "action", "")) in ("create", "export") and not LX.request_marker((text or "")[:_DECIDE_CHARS]):
            # A statement that mentions a format is not a request for a new
            # file, whatever the classifier's confidence (verifier 2026-09-15).
            return intent
        converted = verdict_to_intent(
            verdict, intent,
            has_artifacts=bool(kw.get("has_artifacts")), has_assistant_answer=bool(kw.get("has_assistant_answer")),
            upload_formats=kw.get("upload_formats") or (), last_turn_is_artifact=bool(kw.get("last_turn_is_artifact")),
        )
        return converted if converted is not None else intent
    verdict.rule = f"classifier:{verdict.rule or 'model'}"
    verdict.llm_used = True
    if not verdict.raw_text:
        verdict.raw_text = text or ""
    # The verdict's instruction is the DECISION's view like the rules'
    # (whitespace-collapsed, cut at _DECIDE_CHARS), whatever the hook put
    # there: a hook that echoed the whole message handed a 60 KB paste to
    # the engine's format regexes on the event loop (security review of
    # 2026-09-12, #8). The rules' own `intent.instruction` is that view.
    verdict.instruction = _clean(verdict.instruction)[:_DECIDE_CHARS] or intent.instruction or _clean(text)[:_DECIDE_CHARS]
    return verdict


_DECIDE_KWARGS = frozenset({
    "has_artifacts", "artifact_hints", "has_assistant_answer", "upload_formats", "last_turn_is_artifact", "artifact_id",
})
