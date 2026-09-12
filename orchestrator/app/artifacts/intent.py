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
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence

from . import formats as F

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
_AS_FORMAT_RE = re.compile(rf"\b(?:as|in|into|to)\s+(?:an?\s+|the\s+)?(?:{_FORMAT_WORD})\b", re.I)
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
_CODE_RE = re.compile(r"\b(?:code|script|snippet|function|program|regex|query|sql|python|javascript|typescript|bash)\b", re.I)
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
_TABLE_LINE_RE = re.compile(r"\t|\||(?:[^,;\n]*[,;](?![ \t])){3}")

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
    rf"\b(?:convert|export|save|also|too|as well|another|a copy)\b(?:\W+\w+){{0,8}}?\W+(?:as|to|into|in)\s+(?:an?\s+|the\s+)?{_FORMAT_WORD}\b"
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
        if _TABLE_LINE_RE.search(line):
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


def decide(
    text: str,
    *,
    has_artifacts: bool = False,
    artifact_hints: Sequence[str] = (),
    has_assistant_answer: bool = False,
) -> ArtifactIntent:
    """Decide from the words alone; nothing here calls a model.

    `has_artifacts`: the conversation already holds at least one artifact
    (so "make it shorter" can mean the file). `artifact_hints`: short labels
    of those artifacts (kind words / titles) so "the deck" can be matched to
    one. `has_assistant_answer`: there is a previous assistant turn to
    export.
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
    # The rules read the text with its negated creation clauses blanked
    # (#9); `raw` — the instruction the composer gets — keeps them.
    low = _without_negated_clauses(raw.lower())
    explicit = F.explicit_formats(low)
    rows = _row_count(_without_negated_clauses(_prose_before_table(original).lower()))

    def made(action: Action, **kw) -> ArtifactIntent:
        kw.setdefault("formats", explicit)
        kw.setdefault("instruction", raw)
        return ArtifactIntent(action, raw_text=original, row_count=rows, new_artifact=(action == "create"), **kw)

    # 1. Questions ABOUT a format, code requests: not a file. Checked first,
    #    because "explain how to create a PDF in Python" has a creation verb.
    if _CODE_RE.search(low) and not _AS_FORMAT_RE.search(low):
        return made("none", rule="code", instruction="")
    if _ABOUT_FORMAT_RE.search(low) and not _POLITE_RE.match(low):
        return made("none", rule="about-format", instruction="")

    # 2. Follow-ups on an existing artifact.
    if has_artifacts:
        version = _VERSION_RE.search(low)
        if version and re.search(r"\b(?:go back|revert|restore|use|return|switch)\b", low):
            return made("edit", reference="named", reference_hint=f"version {version.group(1)}",
                        version=int(version.group(1)), rule="restore-version")
        if _CONVERT_RE.search(low) and explicit:
            return made("convert", reference=_which(low, artifact_hints),
                        reference_hint=_hint(low, artifact_hints, exclude=_target_words(explicit)), rule="convert")
        # 2b. A new file, said first: "Create a professional PDF report on
        #     X. Make it visually professional." is a create, not an edit
        #     of the last artifact (CONTRACT-2 §5; discovery C2).
        if _positional_create(low):
            return made("create", rule="create-first-clause")
        if _EDIT_VERBS_RE.search(low) and (_REFERENCE_RE.search(low) or _mentions_hint(low, artifact_hints)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit")
        if _IMPERATIVE_EDIT_RE.match(low) and len(low.split()) <= 12:
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit-imperative")
        # "Also as PDF" with nothing else said.
        if explicit and re.match(r"^\s*(?:also|and|plus|too)?\s*(?:as|in)\s+(?:an?\s+)?\w+(?:\s+\w+)?\s*(?:too|as well|please)?\s*[.!]?\s*$", low):
            return made("convert", reference="latest", rule="convert-short")

    # 3. Exporting the previous answer as a file.
    if has_assistant_answer and _PREVIOUS_ANSWER_RE.search(low) and (explicit or _AS_FORMAT_RE.search(low) or _CREATE_RE.search(low)):
        return made("export", reference="previous_answer", rule="export-answer")

    # 4. Creation.
    if _CREATE_RE.search(low) or _AS_FORMAT_RE.search(low) or _BEST_OR_ALL_RE.search(low):
        if "?" in raw and not _POLITE_RE.match(low) and not explicit:
            # "Would a report help here?" — a creation verb, a document noun,
            # a question, no format: the one shape the rules cannot read.
            return made("none", rule="ambiguous", ambiguous=True)
        return made("create", rule="create")
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

ClassifyHook = Callable[[str], Awaitable[Optional[ArtifactIntent]]]


async def decide_with_hook(
    text: str,
    hook: Optional[ClassifyHook],
    **kw,
) -> ArtifactIntent:
    """The rules, then — only for the ambiguous band — the hook (a strict
    JSON classifier the engine provides). No hook, or a hook that fails,
    means no artifact: the text answer is the safe default."""
    intent = decide(text, **kw)
    if not intent.ambiguous or hook is None:
        return intent
    try:
        verdict = await hook(text)
    except Exception:  # noqa: BLE001 — a classifier outage is a text answer
        return intent
    if verdict is None:
        return intent
    verdict.rule = f"classifier:{verdict.rule or 'model'}"
    if not verdict.raw_text:
        verdict.raw_text = text or ""
    # The verdict's instruction is the DECISION's view like the rules'
    # (whitespace-collapsed, cut at _DECIDE_CHARS), whatever the hook put
    # there: a hook that echoed the whole message handed a 60 KB paste to
    # the engine's format regexes on the event loop (security review of
    # 2026-09-12, #8). The rules' own `intent.instruction` is that view.
    verdict.instruction = _clean(verdict.instruction)[:_DECIDE_CHARS] or intent.instruction or _clean(text)[:_DECIDE_CHARS]
    return verdict
