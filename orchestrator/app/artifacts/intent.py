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
    "What does this sheet contain?"               none     (a question about THIS file:
                                                            answer_about_artifact, read back)

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

THE FOLLOW-UP ROUND (2026-09-16). Measured over 188 turns of 62 real
conversations, 47 were wrong, and the shape of the error was always the
same: the turn pointed at something and nothing here resolved it.

  * "the same" had no rule at all. "Do the same for headcount", "same but
    shorter", "do it again", "and the same again but as a line" and "now do
    the same on a map" were answered as chat (`_SAME_AGAIN_RE`), while "one
    more like that but for onboarding" asks for an ADDITIONAL file and keeps
    its own rule (`_ANOTHER_LIKE_RE`).
  * `make` was an edit verb only in front of a closed adjective list, so
    "make it two pages", "make it a pie" and "make it five slides" were
    chat; a comparative with no verb ("and the report longer", "a bit more
    spacing") had no rule either.
  * The hand-over of the previous ANSWER needed a pronoun AND a verb:
    "now as a docx", "all of that as a briefing doc", "both of those as one
    pdf", "download this table" and "pdf of the second summary" made a new
    file out of the words, or nothing.
  * One content word broke a reference: "put the revenue table in excel"
    invented a workbook rather than exporting the table the answer had.
  * A REMARK was an instruction: "I opened the docx on my phone and the
    table is cut off" silently re-rendered the file.

Each rule below carries the case it was written against.

A QUESTION ABOUT THE FILE IS A QUESTION (2026-09-27). "Ok What This sheet
have ??", asked right after a workbook was made, came back as "Converted …
to Excel" — and again, after the person wrote "please tell me Only Not
create". Over a 119-turn corpus, 0 of the 49 questions about an artifact
were answered: fifteen made a FILE (thirteen through `convert-artifact-turn`,
which read the word "sheet" INSIDE the question as a conversion target, and
two through `ui-edit`) and thirty-four made no file and no answer either.
Step 1a now decides them: `action` stays "none" — no caller has to learn a
new action to stop making a file — and `answer_about_artifact` is True, with
`reference` naming the artifact the answer is read back from. The ask wins
when both are said, in two clauses ("tell me what the sheet has and then
convert it to pdf") or in one ("show me the totals as a pie chart", "tell me
the totals and put them in the sheet"): `_names_a_deliverable` reads the
question's OWN clause for the thing to be produced. The gate reads the
person's OWN prose, so an instruction pasted under the question cannot order
a file (see `_own_prose`).

Those 119 turns were authored the same day as the gate, so they are
in-sample. On a 180-turn corpus with 61 HELD-OUT neighbours added (measured
2026-09-27, each labelled with the class 1f80aa3 satisfies): 1f80aa3 scores
115/180 with 16 files nobody asked for and 4 asked-for files missing; the
first version of this gate scored 125/180 but took 43 further requested files
down with it — every "show me … as a pie chart" (the class PR #77 shipped)
and every polite instruction typed into the UI edit box; this version scores
173/180 with 0 files nobody asked for and the same 4 missing as 1f80aa3.

"IN THE REPORT" IS A PLACE. "The numbers in the report are wrong" was a
create (the destination rule read "in the report" as a deliverable). A
definite article makes it a location; "in a report", "as a PDF" and "in
Excel" still name one, and a destination with no request verb, question
mark or "please" ("I have this in Excel") is a statement.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, List, Optional, Sequence, Tuple

from . import formats as F
from . import lexicon as LX
from . import types as T
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
#: `word` the FORMAT, never the unit of a count. "Write a 5,000-word article",
#: "a 5000 word article" and "a 1,500 word blog post" were files (measured
#: 2026-09-18): the bare noun matched the unit, so an article asked for by
#: its length opened a Word job. A digit right before it — alone, or with the
#: hyphen, comma or space of "5,000-word" / "5000 word" — makes it the unit.
#: "make me a word document" and "a word version" still name the format.
#: `word` before a PLURAL documents/files/docs is always the format: "Create
#: 3 word documents" and "Make 5 word docs" are Word files (QA 2026-09-18:
#: the digit lookbehind turned both into no-request). The singular keeps the
#: lookbehind: "a 10,000-word document" is one document of that length. The
#: lookbehind also knows "10k-word", "5,000–word" and "5,000 - word" (QA
#: 2026-09-18: all three still opened a Word job).
_WORD_FORMAT = (
    r"(?:word(?=\s+(?:documents|files|docs)\b)|"
    r"(?<![0-9])(?<![0-9][-–—,\s])(?<![0-9][-–—,]\s)(?<![0-9]\s[-–—])(?<![0-9]\s[-–—]\s)"
    r"(?<![0-9][kK])(?<![0-9][kK][-–—\s])word)"
)
_ARTIFACT_NOUNS = (
    rf"(?:pdf|docx|{_WORD_FORMAT}(?:\s+(?:document|file|doc))?|powerpoint|power ?point|powerpint|pptx?|presentation|slides?|"
    r"slide ?deck|deck|pitch ?deck|excel|exel|excell|xlsx|xlxs|xls|spread ?sheet|work ?book|tracker|document|doc|"
    r"report|sop|standard operating procedure|memo|brief|one[- ]pagers?|one[- ]page|proposal|"
    r"(?<!cheat )(?<!fact )(?<!balance )(?<!time )(?<!score )(?<!answer )sheets?(?!\s*\d)|"
    r"policy|letter|handout|write[- ]?up|whitepaper|white paper|summary document|"
    r"deliverables?|files?|dashboard|"
    r"csv|cvs|comma[- ]separated(?: values?)?(?: file)?|data ?set|data file|table file|sample data|"
    # The spreadsheet nouns formats._KIND_RULES has classified all along
    # (formats._KIND_RULES, the "spreadsheet words" rule). The two vocabularies had drifted:
    # "create a budget calculator" was refused here as no-request while
    # formats.kind_for read it as ("workbook", "spreadsheet words") — the gate
    # turned down a request the format policy already knew how to fulfil
    # (MEASURE 1 B33, 2026-09-16). A BARE `budget` stays out: "give me the
    # budget for q3" is a question for the dataset engine, so only the
    # compound names a file.
    r"calculator|financial model|budget\s+(?:calculator|tracker|planner|sheet|spread ?sheet|template|model|workbook)|"
    rf"{_DATA_NOUNS})"
)
_FORMAT_WORD = (
    rf"(?:pdf|docx|{_WORD_FORMAT}|powerpoint|power ?point|powerpint|pptx?|excel|exel|excell|xlsx|xlxs|xls|spread ?sheet|"
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
    rf"(?:pdf|docx|{_WORD_FORMAT}(?:\s+(?:document|file|doc))?|powerpoint|power ?point|pptx?|presentation|slide ?deck|deck|pitch ?deck|"
    r"excel|xlsx|spread ?sheet|work ?book|tracker|calculator|financial model|document|doc|report|sop|memo|brief|"
    r"one[- ]pagers?|proposal|policy|letter|handout|write[- ]?up|whitepaper|csv|data ?set|data file|file|dashboard)"
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
#: ONE word between the article and the format word that the closed `_ADJ`
#: list does not know: "as a briefing doc", "as one pdf", "in a combined
#: excel". A DETERMINER is excluded, because "in the report" is a place and
#: not a form (the rule at the top of this file) and a bare word gap would
#: have taken it. Measured 2026-09-16 (follow-up corpus): "all of that as a
#: briefing doc" and "both of those as one pdf" named no destination at all,
#: so both were answered in chat.
_GAP_WORD = r"(?:(?!the\s|this\s|that\s|these\s|those\s|my\s|our\s|your\s|his\s|her\s|its\s|their\s|a\s|an\s)\w+\s+)"
#: "as a PDF", "in Word", "into an Excel", "to csv", "in a classy pdf" — the
#: deliverable named as a form; and the postposition forms ("pdf _in_",
#: "docx file _in_") the normaliser writes for "pdf me", "पीडीएफ में",
#: "પીડીએફમાં" (AS3 (d)). "in the report" is a place, not a form.
_AS_FORMAT_RE = re.compile(
    rf"\bas\s+(?:an?\s+|the\s+|one\s+|two\s+|three\s+|\d+\s+)?(?:{_ADJ}\s+){{0,2}}{_GAP_WORD}{{0,1}}(?:{_FORMAT_WORD})\b"
    rf"|\b(?:in|into|to)\s+(?:an?\s+|one\s+)?(?:{_ADJ}\s+){{0,2}}{_GAP_WORD}{{0,1}}(?:{_FORMAT_WORD})\b"
    rf"|\b(?:{_FORMAT_WORD}|sheet)(?:\s+(?:file|format|version|copy|doc))?\s+_in_\b{LX.DEST_AFTER}"
    # "put it in a 2000 word doc" (normalised to "docx"): the count's `word`
    # stopped being the format (2026-09-18), so the doc it sizes is named here.
    r"|\b(?:as|in|into)\s+(?:an?\s+)?[0-9][0-9,]*[kK]?\s*[-–—]?\s*word\s+(?:docx|docs?|documents?|files?)\b",
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
    # `original|first|initial` (2026-09-16): after "summarise the incident" →
    # "make that a pdf" (card) → "also give me the ORIGINAL answer as a word
    # file", the turn converted the PDF instead of exporting the answer,
    # because only previous|last|above|earlier|prior named an answer here.
    r"\b(?:(?:the|your|that) (?:previous|last|above|earlier|prior|original|first|initial) (?:answer|reply|response|message|summary|explanation)|"
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
    #: The turn is a QUESTION about an artifact the conversation already
    #: holds — "what does this sheet contain?", "what columns does it have?",
    #: "tell me what you put in there" — so the answer is READ BACK from that
    #: artifact and NO file is made (2026-09-27). The action stays "none",
    #: because that is the outcome every caller already understands as "no
    #: file" (`wants_file` below, and main.py / fast_lane.py / material_in.py
    #: branch on it); `reference` and `reference_hint` say WHICH artifact,
    #: and `instruction` keeps the question the answer is written from.
    answer_about_artifact: bool = False
    #: The question POINTS AT the artifact — a pronoun it owns ("what is in
    #: it"), a determiner and a file word ("this sheet", "the workbook", "the
    #: tracker"), a numbered part ("slide 3"), a question about what YOU did
    #: ("which sheets did you create?"), the SOV order the Indian languages
    #: use, or a refusal of a new file while asking to be told. Set only on an
    #: artifact-question verdict, and False on every other intent.
    #:
    #: It exists because `answer_about_artifact` alone cannot say WHICH file
    #: the question is about, and the route needs that when the conversation
    #: also holds an uploaded dataset: "what is the total spend?" is an
    #: artifact question by shape and a DATASET question by subject. Computed
    #: here, where the normalised text and the question's own shape are
    #: already in hand, so the route does not normalise a second time
    #: (`decide` runs on the event loop). Read by
    #: artifacts/describe.answers_from_spec.
    names_our_file: bool = False

    @property
    def wants_file(self) -> bool:
        """Does this turn end in a FILE? An artifact QUESTION does not: it
        decides action "none" on purpose, so no caller has to learn a new
        action to stop making one."""
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


def _rule_view(text: str) -> str:
    """The text the rules read: the lexicon's normalisation (typos, Hindi,
    Gujarati, Hinglish and Gujlish mapped to the rule vocabulary) with every
    negated creation clause blanked (#9). `decide` reads it for the whole
    message; the question gate (step 1a) reads it for the person's own prose
    alone."""
    low = LX.normalize(_without_negated_clauses((text or "").lower()))
    return _without_negated_clauses(_blank_neg_token_clauses(low))


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
    # `both` only where it stands alone ("pdf of both please", "both of
    # them") — "a pdf of both the sales and marketing plans" names two new
    # topics and is a create.
    r"\b(?:it|this|that|these|those|_this_|everything|all\s+of\s+(?:it|this|that)|all\s+that|"
    r"both(?=\s*(?:please|too|$|[.,!?])|\s+of\s+(?:them|those|these|it))|"
    r"the\s+above(?:\s+\w+)?|above\s+(?:answer|report|audit|content|text|one|response|reply|table|list)|"
    r"(?:the|your|that|this|my)\s+(?:last\s+|previous\s+|above\s+|earlier\s+|whole\s+|full\s+)?"
    # ONE describing word of the person's own ("the REVENUE table", "the
    # SECOND summary"). Measured 2026-09-16: "put the revenue table in excel"
    # made a brand-new workbook out of the words, with no link to the table
    # the answer two turns back had printed, because one content word between
    # the determiner and the noun broke the reference. Prepositions and
    # determiners are excluded so "the numbers IN THE report" does not become
    # a reference to a report.
    r"(?:(?!the\s|a\s|an\s|my\s|our\s|your\s|in\s|on\s|of\s|for\s|with\s|and\s|or\s|is\s|are\s|was\s|were\s)\w+\s+){0,1}"
    r"(?:answer|reply|response|report|audit|summary|analysis|content|text|findings|explanation|plan|write[- ]?up|table|list|output)"
    r"(?:\s+above)?)\b",
    re.I,
)
#: A bare ANAPHOR: the turn points at what was just made without naming it.
#: "do the same for headcount", "same but shorter", "do it again", "and the
#: same again but as a line". Measured 2026-09-16 (follow-up corpus): 9 of
#: 47 wrong turns were one of these, and "the same" resolved to nothing
#: anywhere in the layer — every one was answered as chat.
_SAME_AGAIN_RE = re.compile(
    r"\b(?:the\s+same(?:\s+(?:thing|one|again|chart|file))?|do\s+the\s+same|same\s+(?:but|except|only|again|chart|thing)|"
    r"(?:do|try|run|build|make|generate)\s+(?:it|this|that|_this_)\s+again|try\s+again|once\s+more|"
    # "graph this as bars INSTEAD": a replacement of what was just made.
    r"instead)\b"
    # "and for last year too": the same file, a different subject, said with
    # the verb elided entirely (S56 of the corpus).
    r"|^\W*(?:and|also|plus)\s+(?:for|with|on)\s+(?:\w+\s+){0,3}?(?:too|as\s+well)\b",
    re.I,
)
#: ...but "one more like that" asks for an ADDITIONAL file modelled on the
#: last one, which is a create, not an edit of it (S27 of the corpus).
_ANOTHER_LIKE_RE = re.compile(
    r"\b(?:one|another|a\s+second|a\s+similar|a\s+new)\s+(?:more\s+)?(?:like|similar\s+to|modell?ed\s+on|based\s+on|in\s+the\s+style\s+of)\s+"
    r"(?:that|this|it|the\s+\w+)\b",
    re.I,
)
#: A turn that only REPEATS what was said, never a change to the file:
#: "explain that again", "say it again" (the anaphor rule's veto).
_SAY_AGAIN_RE = re.compile(r"\b(?:explain|describe|tell|say|repeat|read|show\s+me)\b", re.I)
#: A finite copula: what separates an instruction from a description of
#: something that already exists ("this data is in excel").
_COPULA_RE = re.compile(r"\b(?:is|are|was|were|has|have|had|comes|came|looks|seems|stays|remains)\b", re.I)
#: "make it five slides", "keep it to two pages": a SIZE, not a conversion
#: target — `slides`, `pages` and `sheets` are format and kind words too, so
#: the conversion rules read them as the deliverable and re-rendered the
#: file instead of shortening it (measured 2026-09-16, S10 of the corpus).
_COUNT_PART_RE = re.compile(
    r"\b(?:\d{1,3}|one|two|three|four|five|six|seven|eight|nine|ten)[- ](?:pages?|slides?|sheets?|columns?|rows?|paragraphs?|sections?|bullets?|lines?|words?)\b",
    re.I,
)
#: "make it two pages", "make it a pie", "turn it into bullets": a verb with
#: a PRONOUN object and any complement changes the file that was just made.
#: "make it a docx" is a CONVERSION and is decided before this.
_MAKE_IT_RE = re.compile(
    r"^\W*(?:(?:please|now|ok|okay|and|also|then|but)\s+)*(?:make|turn|set|keep|leave|render)\s+(?:it|this|that|them|_this_)\s+\S",
    re.I,
)
#: An edit said with no verb at all, as a comparative: "and the report
#: longer", "a bit more spacing", "the title bigger". The comparative is a
#: closed list, because a generic `\w+er` reads "the customer list" and "the
#: weather is colder" as edits.
_COMPARATIVE_EDIT_RE = re.compile(
    r"^\W*(?:(?:and|but|also|plus|now|please|ok|okay)\s+)*(?:(?:the|its|it)\s+)?(?:\w+\s+){0,2}?"
    r"(?:a\s+bit\s+|a\s+little\s+|slightly\s+|much\s+)?"
    r"(?:more|less|bigger|smaller|shorter|longer|wider|narrower|tighter|cleaner|simpler|darker|lighter|bolder|neater|fewer)\b",
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
#: A named format whose object is the reference: "pdf of the second
#: summary", "a word file of the above audit". The reference itself is
#: matched by `_BARE_REF_RE` in `_export_shape`; this only fixes the shape.
_FORMAT_OF_REF_RE = re.compile(
    rf"^\W*(?:(?:a|an|the|just|only|also|and|please|now|then)\s+)*(?:{_ADJ}\s+){{0,2}}(?:{_FORMAT_WORD})"
    rf"(?:\s+(?:file|version|copy|format|doc))?\s+(?:of|for)\s+",
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
#: the report is delayed", "write a short paragraph about our policy". A
#: word count may size it ("write a 1,200-word essay on ..."): without the
#: count's slot the rule missed, and a topic that named reports or documents
#: sent the essay to the classifier (B6, live 2026-09-19: "Write a 1,200-word
#: essay ..." came back as a PDF and a DOCX after 72-336 s, nothing streamed).
#: `article` is prose too; a file named anywhere still wins (`explicit`,
#: `_AS_FORMAT_RE`, `_FILE_CUE_RE` at the call site).
_WORD_COUNT_MOD = r"(?:[0-9][0-9,]*[kK]?\s*[-–—]?\s*words?|[0-9][0-9,]*[kK]?)\s+"
_TEXT_OBJECT_RE = re.compile(
    r"^\W*(?:(?:please|just|pls|kindly|ok|okay|now)\s+)*(?:can\s+you\s+|could\s+you\s+)?(?:write|draft|compose|give\s+me|make|create|prepare|(?:i\s+|we\s+)?(?:need|want))\s+(?:me\s+)?(?:an?\s+|the\s+|some\s+)?"
    rf"(?:{_WORD_COUNT_MOD}|\w+\s+){{0,2}}?"
    r"(?:email|e-mail|mail|message|reply|response|paragraph|poem|story|tweet|post|caption|essay|article|cover\s+letter|note|list|table|summary|outline|bio|headline|slogan|"
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
#: A remark that ENDS in a request is an instruction: "i opened the docx on
#: my phone and the table is cut off, can you fix it?" is not small talk. The
#: two 2026-09-16 rounds disagreed here — one read the opening as a remark,
#: the other read the closing as an edit — and the closing is the ask.
_ASK_CLAUSE_RE = re.compile(
    r"\b(?:can|could|would|will)\s+you\b|\bplease\b|\bkindly\b|\bpls\b"
    r"|\b(?:fix|correct|redo|resend|update|change|adjust)\s+(?:it|this|that|the)\b",
    re.I,
)


def _is_remark(low: str) -> bool:
    """A statement ABOUT the file with no request in it."""
    return bool(_STATEMENT_RE.match(low)) and not _ASK_CLAUSE_RE.search(low)


def _negates_every_file(low: str) -> bool:
    """`low` (lower-cased, NOT yet clause-blanked) rules out any file and asks
    for none after saying so. See `_NEGATED_FILE_RE`. A negation after a
    colon that follows a request is PASTED material ("Turn this policy into a
    report: Staff don't create files on the shared drive"), not the person."""
    for m in _NEGATED_FILE_RE.finditer(low):
        head = low[max(0, m.start() - 12):m.start()]
        if _FIRST_PERSON_NEGATION_RE.search(head + m.group(0)[:24]):
            continue
        colon = low.rfind(":", 0, m.start())
        if colon >= 0 and (_CREATE_RE.search(low[:colon]) or _AS_FORMAT_RE.search(low[:colon])):
            continue
        # The first real negation decides: any later one lies inside `after`
        # and is blanked there, so one scan of the rest is enough.
        after = _without_negated_clauses(low[m.end():])
        return not (_CREATE_RE.search(after) or _AS_FORMAT_RE.search(after) or _NEW_FILE_RE.search(after))
    return False

#: Chart phrasing that asks for one: a verb, "chart of|with|for", a chart
#: phrase leading the message, or a data colon.
_CHART_ASK_RE = re.compile(
    r"\b(?:make|create|generate|build|draw|plot|render|give|show(?:\s+me)?|prepare|produce|visuali[sz]e|_give_|need|want)\b"
    # A chart word used AS THE VERB on a bare reference: "chart it", "graph
    # this", "plot that table". Measured 2026-09-16: "chart it" after a
    # table answer produced no file at all — the chart word switched the
    # export path off (step 3b) and then failed this gate, so the request
    # fell through to chat. "plot that" already passed here, so this closes
    # an inconsistency rather than opening a class. The story-plot and
    # maths-graph vetoes still run in front of it (`chart_ask` below).
    r"|^\W*(?:(?:now|please|ok|okay|and|also)\s+)*(?:chart|graph|plot|visuali[sz]e|map)\s+"
    r"(?:it|this|that|these|those|_this_|the\s+(?:table|data|numbers|results|rows|above|figures))\b"
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
#: `mention|call out|spell out|point out|redesign|rebrand` were added on
#: 2026-09-16: "mention the SLA in the intro" was dropped as chat because
#: no list here held the verb.
_MORE_EDIT_VERBS_RE = re.compile(
    r"\b(?:complete|finish|fill\s+in|apply|set|highlight|sort|restyle|reformat|redesign|rebrand|merge|split|translate|"
    r"mention|call\s+out|spell\s+out|point\s+out|"
    # Changes to an existing file that this platform cannot make. They
    # belong to the EDIT path so the answer is "not applied: signing a PDF
    # is not supported" on the file the person pointed at — measured
    # 2026-09-16 (H4/H5): "Sign it digitally and password-protect the PDF"
    # and "Embed a live Salesforce dashboard in it" were read as CREATES and
    # minted a second artifact under the same title.
    r"sign|password[- ]protect|encrypt|watermark|embed)\b",
    re.I,
)
#: "a bar chart", "line graph", "pie plot" — the type vocabulary lives in
#: lexicon.CHART_TYPE_WORDS, which edits.py reads too.
_CHART_TYPE_RE = re.compile(rf"\b(?:{LX.CHART_TYPE_WORDS})\s+(?:chart|graph|plot)\b", re.I)
#: The words that point at the chart THAT WAS JUST MADE rather than asking
#: for a new one: "instead", "the same", "make it a …", "the chart".
_SAME_CHART_RE = re.compile(
    r"\b(?:instead|rather|same|again)\b"
    r"|\b(?:make|change|turn|redo|render|draw|show|convert|give|do|_give_)\s+(?:me\s+)?(?:it|this|that|_this_)\b"
    r"|\bthe\s+(?:chart|graph|plot)\b",
    re.I,
)
#: A NEW subject: the words that point at DATA instead of at the file, so
#: "visualise this table on pie chart" stays a create even when a chart is
#: already in the conversation.
_NEW_CHART_SUBJECT_RE = re.compile(
    r"\b(?:this|these|that|those|the|_this_)\s+(?:\w+\s+){0,2}?"
    r"(?:table|tables|data|dataset|numbers|figures|rows|records|list|sheet|results|breakdown|split)\b",
    re.I,
)
#: The whole message is the chart type: "as a bar chart", "line graph please".
_ONLY_CHART_TYPE_RE = re.compile(
    rf"^\W*(?:(?:ok|okay|now|and|also|plus|please)\s+)*(?:as|in|to|into)?\s*(?:an?\s+|the\s+)?"
    rf"(?:{LX.CHART_TYPE_WORDS})\s+(?:chart|graph|plot)\s*(?:please|instead|now|again)?\s*[.!?]?\s*$",
    re.I,
)
#: An instruction said as a NEED rather than as a verb: "the deck needs our
#: logo on every slide", "the report is missing the summary section", "the
#: document should have a table of contents". Measured as no-request — the
#: instruction was dropped — because no verb list held these words
#: (MEASURE 1 E20, 2026-09-16). Third person only, and a NOUN complement
#: only: "i need this as a pdf" is a hand-over of the answer (`needs` never
#: matches `i need`), "the report needs TO go out by friday" and "the deck
#: should have BEEN sent" are remarks about the world, not about the file.
_NEEDS_EDIT_RE = re.compile(
    r"\b(?:needs(?!\s+to\b)|is\s+missing|are\s+missing|should\s+(?:have(?!\s+been\b)|include|show|say|list))\b",
    re.I,
)
#: "Write the whole thing out here in this chat", "just answer inline"
#: (2026-09-18: both were files). Each names the place of the ANSWER, so a
#: verb of answering or of putting the TEXT down must lead it: "make a PDF
#: of everything in this chat", "the answer in this chat into a docx" and "a
#: PDF report with inline citations" still ask for a file. send/post/give/
#: paste/show are not text verbs: "Create a docx and post it in this chat"
#: delivers the FILE here (QA 2026-09-18: six such files went to chat).
_ANSWER_VERB = (
    r"(?<!the\s)(?<!your\s)(?<!an\s)(?<!my\s)(?<!this\s)(?<!that\s)(?:answer|reply|respond)(?:\s+(?:it|this|that|me))?"
)
_PUT_TEXT = (
    r"(?:write|put|keep|type)\s+"
    r"(?:it|this|that|everything|them|the\s+(?:whole\s+thing|answer|article|essay|text|content))(?:\s+out)?"
)
_ANSWER_PLACE_RE = re.compile(
    rf"\b(?:{_ANSWER_VERB}|{_PUT_TEXT})(?:\s+(?:right\s+)?here)?(?:"
    r"\s+in\s+this\s+chat\b(?!\s+(?:into|as|to)\s+(?:an?\s+|the\s+)?(?:\w+\s+)?"
    r"(?:pdf|docx?|word|excel|xlsx|csv|sheet|spreadsheet|deck|pptx|presentation|file|document|report)\b)"
    r"|\s+inline\b(?!\s+(?:citations?|images?|charts?|comments?|code|links?|styles?|footnotes?|references?|tables?|formulas?|equations?))"
    r"(?!\s+(?:in|into|within|on)\s+(?:the|a|an|this|that|your|my)\s+(?!chat\b|conversation\b|reply\b|answer\b|message\b)))",
    re.I,
)
#: The person asked for the answer HERE: "in the chat", "no download",
#: "just tell me", "in 3 lines", "yahin chat me".
_CHAT_ONLY_RE = re.compile(
    r"\b(?:(?:here\s+)?in\s+(?:the\s+)?chat|no\s+(?:download|file|files|attachment|pdf|doc)s?|without\s+(?:a\s+)?(?:file|download)|"
    r"(?:don'?t|do\s+not)\s+(?:need|want)\s+(?:a\s+|any\s+)?(?:file|download|document)|just\s+tell\s+me|answer\s+here|"
    r"in\s+\d+\s+(?:lines?|points?|bullets?|sentences?|words?)|yahi[n]?\s+chat|chat\s+(?:_in_|me|mein|ma)\b|here\s+only)\b",
    re.I,
)
#: A negated creation of ANY file: "do not create a file", "don't generate a
#: document", "without making an attachment". `_without_negated_clauses`
#: blanks such a clause so the rest decides — which, after "Write a 10,000-
#: word document on X. Do not create a file.", is a document request again.
#: The person ruled out every file, so the turn is chat unless a request
#: FOLLOWS the negation ("don't create a document, make a deck") or a
#: first-person negation describes the person ("I can't make a file myself").
#: IMPERATIVE negations only, and never "a new|separate|extra file": "don't
#: make a new file, update the deck" is an edit and "Create a PDF. Don't
#: create a separate file for the appendix." a create — both rule out
#: ANOTHER file, not every file (QA 2026-09-18: 22 such turns lost their
#: file); and "my laptop can't create files" describes the world.
_NEGATED_FILE_RE = re.compile(
    rf"\b(?:don['’]?t|dont|do\s+not|never|no\s+need\s+to|there'?s\s+no\s+need\s+to|without)\s+(?:{_NEGATION_ADVERBS}\s+)*"
    r"(?:creat\w*|mak(?:e|es|ing)|generat\w*|produc\w*|build\w*|prepar\w*|export\w*|"
    r"send\w*|attach\w*|sav(?:e|es|ing)|giv(?:e|es|ing)\s+me)\s+(?:me\s+)?(?:an?\s+|any\s+|the\s+)?"
    r"(?:(?:downloadable|actual)\s+)?(?:files?|documents?|attachments?|downloads?)\b",
    re.I,
)
#: A FILE named outright — a format, or a deck/sheet-shaped deliverable. The
#: two forms above never overrule it: "Make a PDF of this and send it here
#: in this chat" and "Build me a pitch deck. Don't generate a document." ask
#: for that file. `report` and `document` are not here: they can be text.
_NAMED_FILE_RE = re.compile(
    rf"\b(?:pdf|docx|{_WORD_FORMAT}\s+(?:document|file|doc)s?|powerpoint|power ?point|pptx?|excel|xlsx|xls|csv|"
    r"spread ?sheets?|work ?books?|slides?|slide ?decks?|decks?|presentations?|trackers?)\b",
    re.I,
)


#: A creation verb, then a word count within three words: "write a 2,000-
#: word article", "give me a 1500 word essay".
_COUNTED_PIECE_RE = re.compile(rf"\b{_CREATE_VERBS}\b(?:\W+\w+){{0,3}}?\W+[0-9][0-9,]*[kK]?\s*[-–—]?\s*words?\b", re.I)
#: ...and a file asked for without a format: "downloadable", "I can print",
#: or put on a slide ("Create a 12-word slogan and put it on a slide" was a
#: create at 4810da0 only through the count's `word`; QA r1). Each cue is
#: about the PIECE: a bare "download" or "printable" is as often the topic
#: ("a 2,000-word essay on why people download music", "a blog post about
#: printable planners" were files through it, 2026-09-19).
_FILE_CUE_RE = re.compile(
    r"\b(?:(?:i|we|you)\s+can\s+(?:download|print|save)|(?:ready|available)\s+(?:to|for)\s+(?:download|print(?:ing)?)|"
    r"(?:make|have|want|need|get)\s+(?:it|this|them|that)\s+(?:as\s+)?(?:an?\s+)?(?:downloadable|printable)|"
    r"(?:downloadable|printable|print[- ]?out)\s+(?:version|copy|file|format|link|document|doc)s?|"
    r"(?:as|in|into)\s+an?\s+(?:downloadable|printable|print[- ]?out)|an?\s+(?:downloadable|printable)\s+[0-9]|"
    r"(?:put|place|add)\s+(?:it|this|them|that)\s+(?:on|onto)\s+(?:an?|one)\s+(?:\w+\s+)?slides?)\b"
    r"|[,;:(–—-]\s*(?:downloadable|printable)\b", re.I)


def _answer_placed_here(low: str) -> bool:
    """The answer is placed here ("answer inline", "write it out in this
    chat") and no file is asked for AFTER that: "Write it out in this chat,
    then make a PDF of it" still makes the PDF. One scan of the rest, after
    the first placement only, so a 4,000-character message costs two passes."""
    m = _ANSWER_PLACE_RE.search(low)
    if m is None:
        return False
    rest = low[m.end():]
    return not (_CREATE_RE.search(rest) or _AS_FORMAT_RE.search(rest))
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
        # An ELLIPTICAL hand-over: a reference, a destination and no finite
        # verb at all — "all of that as a briefing doc", "both of those as
        # one pdf". Measured 2026-09-16: both were answered in chat, the
        # destination never seen. A sentence with a copula ("this data is in
        # excel", "I have this in Excel") is a statement and stays chat,
        # which is why the verb/question test above was the only way in.
        if len(low.split()) <= 8 and not _COPULA_RE.search(low) and not _STATEMENT_RE.match(low) and not LX.reads_source(low):
            return "export-elliptical"
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
    # "now as a docx", "then as a pdf": a destination, a named format and
    # nothing else — the most common export follow-up in the product, and
    # until 2026-09-16 the one shape with no rule: it has no pronoun (so
    # `ref` is false), no verb, and `_FORMAT_ONLY_RE` has no room for a
    # leading "now as a". `_FUNCTION_WORDS_RE` already counts `now`, `as`,
    # `a` and the format names, so this fires only on a content-free
    # destination.
    if dest and explicit and not _content_words(low):
        return "export-destination-only"
    # "download this table", "save that": a KEEPING verb on a bare
    # reference, with the format left to the policy (a table becomes a
    # workbook). `reads_source` keeps "download the pdf I attached" a read.
    if ref and not explicit and _KEEP_VERB_RE.search(low) and not LX.reads_source(low) and not _UPLOAD_SOURCE_RE.search(low):
        return "export-keep-verb"
    # "pdf of the second summary", "a word file of the above audit": a named
    # format whose OBJECT is the reference. `_FORMAT_ONLY_RE` reads only the
    # pronoun objects ("pdf of this"), so a named answer was made from the
    # words instead of exported.
    if explicit and ref and _FORMAT_OF_REF_RE.match(low):
        return "export-format-of-reference"
    # Without a reference, only a CONTENT-FREE postposition hands the answer
    # over ("pdf bana do"); "sales ki report banao" names a new topic and is a
    # create (verifier 2026-09-15: it exported the previous answer).
    if _SOV_RE.search(low) and (ref or (len(low.split()) <= 6 and not _content_words(low))):
        return "export-postposition"
    return None


def _shape_of(value: Any) -> Optional[Any]:
    """`last_deliverable` as a deliverable.Deliverable, whatever the caller
    handed over (the dataclass, the version row's jsonb, or None). Never
    raises: a shape the gate cannot read is no shape, and the rules that
    do not need one are unaffected."""
    if value is None:
        return None
    if hasattr(value, "has_chart"):
        return value
    try:
        from . import deliverable as _D

        return _D.from_json(value)
    except Exception:  # noqa: BLE001 — the shape is an optimisation, never a gate
        return None


def _chart_type_change(low: str) -> bool:
    """The words change the TYPE of the chart that is already there, and
    say nothing about new data."""
    if _ONLY_CHART_TYPE_RE.match(low):
        return True
    return bool(_CHART_TYPE_RE.search(low)) and bool(_SAME_CHART_RE.search(low)) and not _NEW_CHART_SUBJECT_RE.search(low)


#: "report" the VERB after a verb that takes an infinitive: "I want to
#: report a bug", "we need to report it to the team". `i want` plus the noun
#: `report` within six words made the first a create on main (4e7cf8e). The
#: noun keeps its article ("I want a report"), and "convert this to report"
#: has no infinitive lead, so both are untouched.
_REPORT_VERB_RE = re.compile(
    r"\b(?P<lead>(?:want|wants|wanted|wanna|need|needs|needed|like|love|have|has|had|got|going|trying|try|tried|how|able|"
    r"forgot|forget|remember|wish|plan|planning|hope|hoping|decided|asked|ask|supposed|meant|used)\s+to)\s+report\b",
    re.I,
)
#: THE CONVERSATION HOLDS A DATASET (hotfix 1.1, 2026-09-19). The owner's
#: turns after uploading customers-100.csv were "I want plot ??" and "give
#: Big report". The rules read the first as `ambiguous` (the question marks)
#: and the second as no-request, so both hung on the classifier, which said
#: none for "give Big report" about 5 times in 6 and flipped between png and
#: none for the plot — and whose 2.5 s Fast budget ran out under load, which
#: falls back to these rules in silence. With a dataset in the room these
#: are requests for a file made FROM it, and the rules say so themselves.
_DATASET_LEAD = r"^\W*(?:(?:ok|okay|so|and|also|now|then|please|just|hey|hi|sir|bro)\W+)*"
#: The whole message is a report ask: "give Big report", "full report",
#: "detailed report please", "I want a big report on this data". Anchored,
#: so "report a bug" and "as I reported earlier" are never this; and only
#: an indefinite article, because "give the report" may mean the one made.
_DATASET_REPORT_RE = re.compile(
    _DATASET_LEAD
    + r"(?:(?:give|send|share|provide|make|create|generate|prepare|write|build|get|_give_)\s+(?:me\s+|us\s+)?"
    r"|(?:i|we)\s+(?:(?:really|just|also|still)\s+)?(?:want|need|would\s+like|'d\s+like|wanna)\s+)?"
    r"(?:an?\s+|one\s+)?"
    r"(?:(?:big|bigger|full|detailed|complete|comprehensive|proper|long|longer|large|in[- ]depth|thorough|deep|final|"
    r"whole|entire|overall|short|quick|brief|summary|data|dataset|analysis|analytical|nice|good|professional|clear)\s+){0,3}"
    r"report(?:\s+(?:on|of|for|about|from)\s+(?:it|this|that|_this_|the|my|our)(?:\s+(?:data|dataset|file|csv|sheet|table|upload))?)?"
    r"(?:\s+(?:please|now|too|also|asap))*\W*$",
    re.I,
)
#: A chart ASKED FOR, said with question marks: "I want plot ??", "plot ??",
#: "show me a graph?". Three openers only — a first-person want/need, a
#: request verb, or the chart word itself as the verb — and never a copula
#: after it, so "should I plot this?", "the plot is wrong?" and "I want to
#: know if the plot is right?" stay with the ambiguous band.
_DATASET_CHART_ASK_RE = re.compile(
    _DATASET_LEAD
    + r"(?:(?:(?:i|we)\s+(?:(?:really|just|also|still|only)\s+)?(?:want|need|would\s+like|'d\s+like|wanna|require)\s+"
    r"|(?:give|show|make|create|generate|build|prepare|send|draw|get|_give_)\s+(?:me\s+|us\s+)?)"
    r"(?:(?!(?:know|understand|ask|check|confirm|verify|whether|if|why|how|what|when|where|which)\b)\w+\s+){0,3}?"
    r"(?:plot|chart|graph|visual|visuali[sz]ation|diagram|histogram)s?\b"
    r"|(?:plot|chart|graph|visuali[sz]e|draw)s?\b(?!\s+(?:is|are|was|were|looks?|seems?|has|have|does|did)\b))",
    re.I,
)


def _dataset_ask(low: str, raw: str, chart: bool) -> str:
    """The rule name when these words ask for a file made from the
    conversation's dataset, else "". Called only when one exists."""
    if _DATASET_REPORT_RE.match(low):
        return "dataset-report"
    # Without a question mark the create rules below already take a chart
    # ask; this closes only the band they call ambiguous.
    if chart and "?" in raw and _DATASET_CHART_ASK_RE.match(low) and not _STORY_PLOT_RE.search(low):
        return "dataset-chart"
    return ""


# ------------------------------------- A QUESTION ABOUT THIS FILE (2026-09-27) --
#
# THE ANCHOR CASE, in the owner's words: "The user wants to understand the
# sheet -- what it has -- then it does not give an answer, it creates that
# sheet again." The production transcript, after a workbook was made:
#
#   "Ok What This sheet have ??"                 -> "Converted … to Excel" v2
#   "I said ??? what you create inside the sheet ??? i want to Know ??
#    please tell me Only Not create d??"         -> "Converted … to Excel" v3
#
# Measured on 1f80aa3: thirteen of the fifteen files nobody asked for came
# from `convert-artifact-turn` — the word "sheet" INSIDE the question was read
# as a conversion target, which is why the card said "Converted" and not
# "Created" — and two from `ui-edit`, which the UI's edit box binds every turn
# to. The other thirty-four questions produced no file and no answer either:
# nothing in the layer said "this turn is a question ABOUT the file", so
# nothing downstream could read it back.
#
# THE DECISION, not the answer. These rules say only that the turn is a
# question about the artifact: `action` stays "none" (no file — the outcome
# every caller already understands) and `answer_about_artifact` is True.
# Reading the workbook back — "2 sheets, 5 rows, 6 columns" — needs the
# stored spec (`store.read_spec`), which is not reachable from what the gate
# is handed (`deliverable.Deliverable` carries kind/formats/charts only): that
# is the engine's half of the fix and is deliberately not attempted here.
#
# WHY THE RULES AND NOT THE CLASSIFIER. On Fast the classifier has a ~2.5 s
# budget and, when it runs out, the system falls back to these rules IN
# SILENCE (the hotfix-1.1 history). A question that is only understood when a
# model answers in time is not understood. `_should_consult` therefore refuses
# to offer an artifact question to the model at all.

#: The words that name what is INSIDE a file: a question about one of these
#: is answered by reading the file back. Format names are deliberately
#: absent — "what is a PDF?" asks about the format as a concept
#: (`_ABOUT_FORMAT_RE`) — and so are the file nouns themselves, so "what do
#: you think of the tracker?" cannot reach these rules through its noun.
#:
#: The nouns are a LIST, not a pattern, because two other patterns below are
#: this list with something taken out of it, and a copy of the list cannot be
#: trusted to stay a copy (2026-09-28: the first attempt at
#: `_Q_CONTENT_ONLY_NOUN` restated all 37 of them by hand).
_Q_CONTENT_NOUN_WORDS = (
    "columns?", "colums?", "rows?", "sheets?", "worksheets?", "tabs?", "sections?", "headings?",
    "headers?", "titles?", "subtitles?", "pages?", "slides?", "cells?", "fields?", "formulas?",
    "formulae", "totals?", "subtotals?", "charts?", "graphs?", "tables?", "data", "datasets?",
    "contents?", "records?", "entries", "values", "figures", "numbers", "names", "labels?",
    "paragraphs?", "bullets?", "dates?", "structure", "format",
)
#: The file itself, as a noun a determiner can point at.
_Q_FILE_WORD_WORDS = (
    "files?", "documents?", "docs?", "pdfs?", "docx", "xlsx", "xls", "excel", "csv", "pptx",
    "powerpoint", "ppt", "work ?books?", "spread ?sheets?", "sheets?", "trackers?", "reports?",
    "decks?", "presentations?", "attachments?", "outputs?", "deliverables?", "versions?",
)


def _alt(words) -> str:
    """The words as one non-capturing alternation, in the order given."""
    return "(?:" + "|".join(words) + ")"


_Q_CONTENT_NOUN = _alt(_Q_CONTENT_NOUN_WORDS)
_Q_FILE_WORD = _alt(_Q_FILE_WORD_WORDS)
#: The file words that CANNOT name our own file with no determiner in front of
#: them, COMPUTED as the difference below (2026-09-28, round 4). Two kinds:
#:
#:   * a bare FORMAT name — `csv`, `excel`, `pdf`, `xlsx`, `ppt` — is what the
#:     person's OWN upload is called, and this platform publishes those formats
#:     too (artifacts/formats.py), so the word names either file;
#:   * a bare GENERIC CONTAINER — `file`, `document`, `doc`, `attachment` — names
#:     any file at all, and `attachments?` is the vocabulary
#:     `_Q_THEIR_UPLOAD_RE` uses to say a file is THEIRS.
#:
#: `_Q_OUR_FILE`'s determiner arm already requires the|this|that|my|our|your in
#: front of a file word for exactly this reason. The SOV pointer below has NO
#: determiner in its bare arm, so it needs the narrower set: measured through
#: POST /chat with customers.csv and a workbook in one conversation, the bare
#: `_Q_FILE_WORD` there answered "csv me total spend kitna hai ??", "excel me
#: kitne rows hai ??", "csv में कितने rows हैं ?", "csv ma ketla rows che ??",
#: "pdf me kitne pages hai ??", "file me kitne rows hai ??" and "doc me kitne
#: pages hai ??" with "**Workflow Tracker** (v1) is a workbook with 1 sheet:
#: `Tasks`" — a description of a workbook nobody asked about.
#:
#: What is LEFT is the vocabulary of a thing this platform MADE: `work ?books?`,
#: `spread ?sheets?`, `sheets?`, `trackers?`, `reports?`, `decks?`,
#: `presentations?`, `outputs?`, `deliverables?`, `versions?`. Nobody calls the
#: CSV they just uploaded a tracker or a deliverable.
_Q_NOT_OURS_ALONE_WORDS = (
    "files?", "documents?", "docs?", "pdfs?", "docx", "xlsx", "xls", "excel", "csv",
    "pptx", "powerpoint", "ppt", "attachments?",
)
_Q_OUR_FILE_NOUN = _alt(w for w in _Q_FILE_WORD_WORDS if w not in _Q_NOT_OURS_ALONE_WORDS)
#: The content nouns a DATASET owns just as much as a file this platform made —
#: the content nouns MINUS the file words, COMPUTED (2026-09-28). A noun that is
#: also a file word (`sheets?` is the only one today) still points at our file,
#: and every other one of them names something a CSV has too, so it points at
#: nothing on its own. The difference is taken here rather than written out
#: because a hand-copied list is a claim about two other lists that nothing
#: checks; this one cannot be wrong, and
#: tests/test_artifact_answer_read_source.py pins what the difference currently
#: is so that adding an overlapping word to either list is a decision someone
#: has to make on purpose.
_Q_CONTENT_ONLY_NOUN = _alt(w for w in _Q_CONTENT_NOUN_WORDS if w not in _Q_FILE_WORD_WORDS)
#: THIS file: a bare pronoun (with an artifact in the room, "it" is the
#: artifact), a determiner and a file or content word, or a numbered part.
#: An INDEFINITE article is not here: "a pdf" is the format as a concept.
_Q_THIS_FILE = (
    rf"(?:(?:it|this|that|these|those|them|_this_)\b"
    rf"|(?:the|this|that|these|those|my|our|your)\s+(?:\w+\s+){{0,2}}?(?:{_Q_FILE_WORD}|{_Q_CONTENT_NOUN})\b"
    rf"|(?:slide|page|sheet|tab|section|column|row)\s+\d+\b)"
)
#: A containment word: what a file DOES with its contents. `of` is not here
#: — "what do you think OF the tracker?" is an opinion, not the contents.
_Q_INSIDE = (
    r"(?:in|inside|within|contain\w*|includ\w*|have|has|had|got|hold\w*|consist\w*|compris\w*|says?|shows?|lists?)"
)
#: The question words, English and the two Indian languages the normaliser
#: leaves as written (`क्या`/`શું`, and the romanised kya/su/shu the
#: Hinglish and Gujlish turns use). `who` is absent: "who else can see this
#: file?" asks a fact the file does not hold.
#: `\b` is useless at the end of `क्या` / `શું`: both end in a combining
#: vowel mark, which Python's `\w` does not match, so there is no word
#: boundary there at all and every Devanagari and Gujarati question word
#: silently failed to match (measured on the corpus: q34/q35 still converted
#: the workbook). The guards are lookarounds instead, and the shapes below
#: must NOT wrap `_Q_WH` in `\b`.
_Q_WH_ANY = (
    # `kitni` is the feminine of `kitna`, and Gujarati's `ketli` was here
    # without it: "workbook me KITNI sheets hai ??" was not a question at all
    # while "workbook me KITNE sheets hai ??" was (measured 2026-09-28).
    r"(?<!\w)(?:what'?s?|how\s+many|how\s+much|kya|kaya|kitne|kitna|kitni|ketla|ketli|su|shu"
    r"|क्या|कितन\w*|શું|કેટલ\w*)(?!\w)"
)
#: `where`, `which` and `why` open a question at the START of a clause and
#: SUBORDINATE one inside it: "make the Status column red WHERE Open, in the
#: sheet" is an instruction, and reading `where` as a question word wherever
#: it stood turned that verifier case into one
#: (tests/test_artifact_intent.py, the style-as-a-place cases of 2026-09-15).
#: `^` is the clause's start: the gate reads one clause at a time and the
#: text has no newlines left in it.
_Q_WH_LEAD = (
    r"^\W*(?:(?:ok|okay|so|and|also|but|now|then|please|pls|hey|hi|sir|bro|just|i\s+said)\W+)*"
    r"(?:where|which|why)(?!\w)"
)
_Q_WH = rf"(?:{_Q_WH_ANY}|{_Q_WH_LEAD})"
_Q_GAP = r"(?:\w+\W+){0,4}?"
#: The three atoms above, as whole words, for the tests that need one of them
#: present anywhere in a clause.
_Q_CONTENT_NOUN_RE = re.compile(rf"\b{_Q_CONTENT_NOUN}\b", re.I)
_Q_CONTENT_ONLY_NOUN_RE = re.compile(rf"\b{_Q_CONTENT_ONLY_NOUN}\b", re.I)
_Q_INSIDE_RE = re.compile(rf"\b{_Q_INSIDE}\b", re.I)
_Q_THIS_FILE_RE = re.compile(rf"\b{_Q_THIS_FILE}", re.I)
#: THIS file, with the CONTENT nouns removed: a pronoun the artifact owns
#: ("what is in it"), a determiner and a word for a FILE ("this sheet", "the
#: workbook", "the tracker"), or a numbered part ("slide 3"). It is
#: `_Q_THIS_FILE` minus `_Q_CONTENT_NOUN`, and the subtraction is the point.
#:
#: WHY IT EXISTS (verifier, 2026-09-27). `_Q_THIS_FILE` admits `the` + a
#: content noun, and `totals?`, `dates?`, `data`, `values`, `numbers` and
#: `figures` are content nouns — so "what is the TOTAL spend?", "what is the
#: DATE range?" and "which countries are in the DATA?" all point at "this
#: file" as far as that pattern is concerned. They are questions about the
#: person's own uploaded DATASET, whose answer is a real number the dataset
#: engine computes; the read-back holds the artifact's structure and never a
#: cell value, so answering them from it replaces the number with a
#: description of the wrong file. This pattern is what `names_our_file`
#: records, and it is read ONLY when the conversation also holds a dataset
#: (artifacts/describe.answers_from_spec) — with no dataset in the room there
#: is no other file for the question to be about, and the broad reading is
#: right.
_Q_OUR_FILE = (
    rf"(?:(?:it|this|that|these|those|them|_this_)\b(?!\s+{_Q_CONTENT_ONLY_NOUN}\b)"
    rf"|(?:the|this|that|these|those|my|our|your|_this_)\s+(?:\w+\s+){{0,2}}?{_Q_FILE_WORD}\b"
    rf"|(?:slide|page|sheet|tab|section|column|row)\s+\d+\b)"
)
_Q_OUR_FILE_RE = re.compile(rf"\b{_Q_OUR_FILE}", re.I)
#: THE SOV POINTER — a file word, then the postposition the normaliser writes
#: (2026-09-28). `_Q_SOV_RE` cannot serve as one, and reading it as one is what
#: sent every Indian-language value question to the workbook: its first arm is
#: <wh> … `_in_` with NO file reference in it at all, and its second ORs
#: `_Q_CONTENT_NOUN` straight back in — the exact set `_Q_OUR_FILE` subtracts.
#: Measured through POST /chat with customers.csv and a workbook in one
#: conversation: "डेटा में कुल spend कितना है ?", "data में कुल spend कितना है ?"
#: and "rows में कितने countries हैं ?" were each answered "**Workflow Tracker**
#: (v1) is a workbook with 1 sheet: `Tasks`", while the English equivalent
#: ("what is the total spend in the data ?") reached the dataset engine — the
#: same question, answered correctly in one language and not the other.
#:
#: `\b` IS LOAD-BEARING at the front: `it` closes ordinary words, so without it
#: "credit note में क्या है ?" — normalised "credit note _in_ kya hai" — matched
#: through the `it` inside "credit" and pointed a question about the person's
#: own credit note at our workbook.
#:
#: THE BARE ARM CARRIES NO DETERMINER, so it reads `_Q_OUR_FILE_NOUN` and not
#: `_Q_FILE_WORD` (2026-09-28, round 4): a bare format name or a bare generic
#: container is what the person's own upload is called, and this arm was
#: answering ten measured questions about an uploaded CSV with a description of
#: a workbook. The determiner arm inside `_Q_OUR_FILE` still admits the whole
#: file-word list, which is why "what is in the csv?" stays the stated residual
#: that tests/test_artifact_answer_read_source.py pins.
_Q_SOV_OUR_FILE_RE = re.compile(
    rf"\b(?:{_Q_OUR_FILE}|{_Q_OUR_FILE_NOUN})(?:\W+\w+){{0,4}}?\W+_in_\b",
    re.I,
)
#: …and THEIR file, named as their own: "the csv i uploaded", "the attachment",
#: "the sheet i sent". It vetoes the pointer above, because our own artifacts
#: are xlsx/pdf/docx/pptx AND csv (formats.py's `data` template makes a CSV), so
#: `csv`, `excel` and `spread sheet` are in `_Q_FILE_WORD` and cannot say whose
#: file is meant on their own: measured 2026-09-27, "what is in the csv i
#: uploaded?" pointed at OUR workbook. Naming the artifact by its TITLE still
#: wins — that is the most specific pointer there is.
#:
#: NARROW, and it is the same vocabulary app.main._NAMES_AN_UPLOAD_RE uses one
#: layer up, minus the `page \d+` / timestamp arms: a PDF this platform made
#: HAS pages. Measured over the programme's 119-turn corpus: 0 rows match, so
#: it cannot cost the read-back class.
_Q_THEIR_UPLOAD_RE = re.compile(
    r"\b(?:i|we)\s+(?:just\s+)?(?:sent|uploaded|shared|attached)\b"
    r"|\b(?:the|that|this|my)\s+(?:attach(?:ment|ed)|upload(?:ed)?)\b"
    r"|\b(?:attach(?:ed|ment)|upload(?:ed)?)\s+"
    r"(?:file|csv|xlsx?|excel|sheet|workbook|spread ?sheet|data ?set|data|pdf|doc|document)\b",
    re.I,
)

#: "what is in it", "what's inside the workbook", "what does this sheet
#: contain", "what the tracker has", "what is on slide 3".
_Q_FILE_CONTENTS_RE = re.compile(
    rf"{_Q_WH}\W+{_Q_GAP}(?:{_Q_INSIDE}|on)\b\W+{_Q_GAP}{_Q_THIS_FILE}"
    rf"|{_Q_WH}\W+{_Q_GAP}{_Q_THIS_FILE}\W+{_Q_GAP}{_Q_INSIDE}\b",
    re.I,
)
#: A question word and one of the file's own content words, in either order:
#: "what columns does it have?", "how many rows are in it?", and the SOV
#: order the Indian languages use ("isme kya kya COLUMNS hai" — the question
#: word comes first, the noun last, and "is sheet me kya kya hai" reverses
#: them).
_Q_WH_CONTENT_RE = re.compile(
    rf"{_Q_WH}(?:\W+\w+){{0,5}}?\W+{_Q_CONTENT_NOUN}\b",
    re.I,
)
#: The REVERSED order — the content word first, the question word after it —
#: read only when a question mark closed the clause. Unmarked it claimed a
#: noun-first chart REQUEST: `charts?` is a content noun and "how many"
#: follows within five words, so the repository's own authored chart ask
#: (tests/fixtures/chart_requests.py t02, "Bar chart of how many tickets each
#: priority has.") was answered instead of drawn — measured 2026-09-27
#: against 1f80aa3, which draws it (create/create-chart).
_Q_CONTENT_WH_RE = re.compile(
    rf"\b{_Q_CONTENT_NOUN}\b(?:\W+\w+){{0,5}}?\W+{_Q_WH}",
    re.I,
)


def _q_wh_content(clause: str, marked: bool) -> bool:
    """A question word and a content word in the same clause. The reversed
    order needs the question mark; see `_Q_CONTENT_WH_RE`."""
    if _Q_WH_CONTENT_RE.search(clause):
        return True
    return (marked or "?" in clause) and bool(_Q_CONTENT_WH_RE.search(clause))


#: The postposition forms the normaliser writes: "is sheet me kya hai"
#: becomes "_this_ sheet _in_ kya hai", where the question word can be
#: anywhere. `_in_` is written only for me/mein/में/માં, so this shape
#: cannot fire on an English turn.
#:
#: A BARE FILE WORD counts (2026-09-28, round 4). The second arm read
#: `_Q_THIS_FILE` — which needs a determiner — or a content noun, so "tracker me
#: kya hai ??", "deliverable me kya hai ??" and "document me kya hai ??" were
#: not questions about a file AT ALL: they carry no determiner and no content
#: noun, and the whole turn reached the dataset engine although the person had
#: just been shown the file card. The English "what is in the tracker?" is
#: `_Q_FILE_CONTENTS_RE`'s, because `in` is a containment word there; `_in_` is
#: not in `_Q_INSIDE` and must not be (every `pdf me convert karo` writes one),
#: so the SOV shape names the case itself. WHOSE file it is stays
#: `_Q_SOV_OUR_FILE_RE`'s question, and a bare format word there is still the
#: person's own upload.
_Q_SOV_RE = re.compile(
    rf"{_Q_WH}(?:\W+\w+){{0,6}}?\W+_in_\b"
    rf"|(?:{_Q_THIS_FILE}|{_Q_CONTENT_NOUN}|{_Q_FILE_WORD})(?:\W+\w+){{0,4}}?\W+_in_\b(?:\W+\w+){{0,4}}?\W+{_Q_WH}",
    re.I,
)
#: A question about WHAT WAS DONE: "what did you put in the second sheet",
#: "which sheets did you create?", "did you include the due dates?", "why
#: did you add a priority column?", "explain what you created", and the
#: owner's own "what you create inside the sheet" — which has no `did` at
#: all. `can you …` is deliberately absent: that is a request to do
#: something, not a question about what is already there.
_Q_DID_YOU_RE = re.compile(
    r"\b(?:what'?s?|which|why|where|when|how|did|do|does|have|has|are|were|was)\s+(?:\w+\s+){0,3}?"
    r"(?:did\s+|do\s+|have\s+)?you\s+(?:\w+\s+){0,2}?"
    r"(?:do|does|did|done|mak\w*|made|creat\w*|add\w*|put|includ\w*|writ\w*|wrote|generat\w*|build|built|"
    r"sav\w*|us\w*|used|choos\w*|chose|pick\w*|nam\w*|call\w*|set|insert\w*|fill\w*|leave|left)\b",
    re.I,
)
#: The same shape with NO NOUN PHRASE OF ITS OWN between the question word
#: and `you`: the question word governs `you` directly ("what you created",
#: "what did you put …"), or the clause OPENS with the auxiliary ("did you
#: include the due dates?").
#:
#: The four-word gap in `_Q_DID_YOU_RE` exists for "which SHEETS did you
#: create?" and "what FORMAT did you save it in ??", and it also let a
#: question about something else entirely wear this hat: "what model are you
#: using?" matched <wh> model are you us… and was answered from the workbook.
#: Measured 2026-09-27: 1f80aa3 decides that none/no-request — ordinary chat —
#: and answering it from the spec would describe the workbook to someone who
#: asked which model is running. A `what-you-did` clause must therefore either
#: name the file or something in it, or have this tighter shape.
_Q_DID_YOU_VERB = (
    r"(?:do|does|did|done|mak\w*|made|creat\w*|add\w*|put|includ\w*|writ\w*|wrote|generat\w*|build|built|"
    r"sav\w*|us\w*|used|choos\w*|chose|pick\w*|nam\w*|call\w*|set|insert\w*|fill\w*|leave|left)"
)
_Q_DID_YOU_DIRECT_RE = re.compile(
    rf"\b(?:what'?s?|which|why|where|when|how)\s+(?:did\s+|do\s+|does\s+|have\s+|has\s+)?you\s+"
    rf"(?:\w+\s+){{0,2}}?{_Q_DID_YOU_VERB}\b"
    rf"|^\W*(?:(?:ok|okay|so|and|also|but|now|then|please|pls|hey|hi|sir|bro|just|i\s+said)\W+)*"
    rf"(?:did|do|does|have|has|are|were|was)\s+you\s+(?:\w+\s+){{0,2}}?{_Q_DID_YOU_VERB}\b",
    re.I,
)
#: A yes/no question about the contents: "does it have a status column?",
#: "is there a column for owner?", "is the tracker two sheets or one ??".
#: The question MARK is required here and nowhere else: without it the same
#: words are an imperative ("do the same for headcount", "delete the last
#: column"), and a wh-question needs no mark to be a question ("what is in
#: this sheet").
_Q_YES_NO_RE = re.compile(
    rf"^\W*(?:(?:ok|okay|so|and|also|but|now|then|please|pls|hey|hi|sir|just)\W+)*"
    rf"(?:does|do|did|is|are|was|were|has|have)\s+(?:there\b|you\b|{_Q_THIS_FILE})",
    re.I,
)
#: An instruction to SPEAK, not to build: "tell me what is inside the
#: workbook", "list the headings in the document", "summarise the tracker
#: you made", "read back what the sheet has", and the `_read_` the
#: normaliser writes for "bata do" / "kaho" / "दिखाओ". `give me …` is NOT
#: here: a hand-over verb asks for the thing itself, not for a description
#: of it, and `explain how to …` is a how-to (`LX.negative_shape`).
_Q_TELL_RE = re.compile(
    r"\b(?:tell|show)\s+(?:me|us)\b|\blet\s+(?:me|us)\s+know\b|\b_read_\b"
    r"|\b(?:list|explain|describe|summari[sz]e|recap|read\s+back|read\s+out|walk\s+(?:me|us)\s+through)\b",
    re.I,
)
#: ADVICE, OPINION AND ACCESS ARE NOT THE CONTENTS. "what should I put in
#: it?" asks what to write, "what do you think of the tracker?" asks for an
#: opinion, and "who else can see this file?" asks a fact the file does not
#: hold: reading the spec back would be the wrong answer to all three.
_Q_NOT_CONTENTS_RE = re.compile(
    # "what should the sheet contain?" and "how many pages should it be?" ask
    # what to PUT there, not what is there: any subject, not just the first
    # person, because a read-back is the wrong answer to all of them.
    r"\b(?:should|shall|ought\s+to)\s+(?:i|we|it|this|that|the|there|they|you|he|she)\b"
    r"|\bwhat\s+(?:should|would|could|can)\s+(?:i|we|you)\b"
    r"|\bdo\s+you\s+(?:think|reckon|feel|suggest|recommend|advise|prefer)\b"
    r"|\bwhat\s+(?:do|did)\s+you\s+think\b|\byour\s+(?:opinion|advice|thoughts)\b"
    r"|\bany\s+(?:ideas|suggestions|advice|thoughts)\b"
    r"|\bwho\s+(?:else\s+)?(?:can|could|may|has|have|is|are)\b"
    # A POLITE INSTRUCTION wearing a question word. "what if you made it a
    # pdf as well", "how about you make it two pages", "why don't you add a
    # totals row", "do you mind making it landscape", "is it possible to add
    # a status column?", "which columns do you want removed?" and "do you
    # have the bandwidth to also make a deck?" all ask for WORK. They open
    # with the question word, so `_Q_IMPERATIVE_LEAD_RE` cannot see the build
    # verb; measured 2026-09-27 against 1f80aa3, which gives them
    # convert/['pdf'], edit and create/create-first-clause respectively, and
    # edit/ui-edit for every one of them typed into the UI's edit box.
    r"|\b(?:what|how)\s+if\s+(?:you|we)\b|\b(?:what|how)\s+about\b"
    r"|\bwhy\s+(?:not|do\s?n['’]?t|dont|do\s+not)\b"
    r"|\b(?:is|are|was|were|would|will)\s+(?:it|this|that)\s+(?:be\s+)?possible\b"
    r"|\bdo\s+you\s+mind\b|\bdo\s+you\s+have\s+(?:the\s+)?(?:time|bandwidth|capacity|availability)\b"
    r"|\bdo\s+you\s+(?:want|wanna|need|propose|plan)\b"
    r"|\b(?:how\s+many|how\s+much)(?:\W+\w+){0,4}?\W+can\s+you\b"
    # "what I want is a totals row in the sheet", "what I need is the sheet
    # in pdf": a STATEMENT of the deliverable, not a question about the
    # contents — and "what I want to know is …" is untouched, because `is`
    # has to follow the verb directly.
    r"|\bwhat\s+(?:i|we)\s+(?:really\s+|just\s+)?(?:want|wanted|need|needed|would\s+like)\s+is\b",
    re.I,
)
#: "just tell me", "tell me only", "i want to know", "sirf bata do": the
#: person asks to be TOLD, and for nothing else.
#:
#: `told_gap` NAMES the words between the intensifier and the speech verb,
#: because `_names_nothing_but_the_ask` has to put them back (2026-09-28, round
#: 4). In English the verb comes before its object, so "just tell me the total
#: spend" leaves the subject standing after the phrase; in VERB-FINAL Hinglish
#: and Gujlish the object sits INSIDE the gap — "fakt kul spend kaho", "bas
#: countries bata do" — and subtracting the phrase whole subtracted the subject
#: with it, which is why six measured refusals that plainly name their own
#: subject went on pointing at our workbook.
_Q_TELL_ONLY_RE = re.compile(
    r"\b(?:just|only|simply|sirf|faqt|fakt|khali|bas)\s+(?P<told_gap>(?:\w+\s+){0,2}?)(?:tell|say|show|explain|answer|_read_)\b"
    r"|\b(?:tell|explain|show)\s+(?:me|us)\s+(?:only|just)\b"
    r"|\b(?:i|we)\s+(?:just\s+|only\s+|really\s+)?(?:want|need|would\s+like|wanna)\s+to\s+know\b"
    r"|\b(?:i|we)\s+(?:only|just)\s+(?:want|need)\b"
    r"|\blet\s+(?:me|us)\s+know\b|\bplease\s+tell\s+(?:me|us)\b|\btell\s+(?:me|us)\s+only\b|\b_read_\b",
    re.I,
)
#: The WHOLE message is the ask to be told, and nothing else: "i only want
#: to know", "just tell me", "only tell me please". Anchored at both ends, so
#: "just tell me the deadline" — which names its own subject — is not this.
#: Read only when the last assistant turn was the file card: then there is
#: nothing else the person can mean, and the transcript's third turn is this
#: shape ("i want to Know ?? please tell me Only").
_Q_TELL_ONLY_BARE_RE = re.compile(
    r"^\W*(?:(?:ok|okay|no|nope|and|but|so|now|then|please|pls|kindly|hey|sir|bro|yaar|bhai|i\s+said)\W+)*"
    r"(?:(?:i|we)\s+(?:just\s+|only\s+|really\s+)?(?:want|need|would\s+like|wanna)\s+to\s+know"
    r"|(?:just|only|simply|sirf|fakt|faqt|khali|bas)\s+(?:tell|let|say)\s+(?:me|us)(?:\s+know)?"
    r"|(?:tell|let)\s+(?:me|us)(?:\s+know)?\s+(?:only|just)"
    r"|(?:i|we)\s+(?:only|just)\s+(?:want|need)(?:\s+to\s+know)?"
    r"|_read_)"
    r"(?:\s+(?:please|pls|now|first|only|just|ok|okay|na))*\W*$",
    re.I,
)
#: A refusal to make anything: "don't create anything", "Not create",
#: "i dont want a new one", and the `_neg_` the normaliser writes for
#: "nayi file mat banao" / "navi file na banavo". Read on the text BEFORE
#: `_without_negated_clauses` blanks it — that pass exists so the rest of
#: the message decides, and here the negation IS the decision.
_Q_REFUSE_CREATE_RE = re.compile(
    r"\b(?:do\s*n['’]?t|dont|do\s+not|never|no\s+need\s+to|not|mat|na|nahi|nathi)\s+"
    r"(?:(?:just|simply|actually|really|please|bother\s+to|go\s+ahead\s+and|try\s+to|need\s+to|"
    r"want\s+to|have\s+to|you\s+to|to)\s+)*"
    r"(?:creat\w*|re-?creat\w*|mak\w*|remak\w*|generat\w*|produc\w*|build\w*|rebuild|banao|banavo|banavvu|banana)\b"
    r"|_neg_"
    r"|\b(?:i|we)\s+(?:do\s*n['’]?t|dont|do\s+not)\s+(?:want|need)\s+"
    r"(?:a\s+|an\s+|any\s+|another\s+|the\s+)?(?:new|other|second|extra|more|another)\b",
    re.I,
)
#: WORDS THAT NAME NOTHING: politeness, discourse filler, the pronouns and
#: auxiliaries the ask to be told is built from, and the bare objects a refusal
#: takes ("a new one", "anything", "a file"). Whatever survives this AND the
#: three phrase patterns above is a subject the person named for themselves.
#: `d` is in it because the transcript's own turn ends "Not create d??".
#: The MANNER ADVERBS are here because `_Q_TELL_ONLY_RE`'s gap is kept rather
#: than subtracted (2026-09-28, round 4): in English the two words that may
#: stand between "just" and the speech verb are an adverb — "just quickly tell
#: me" — and an adverb names no subject, so keeping the gap must not turn one
#: into a subject and cost the read-back the turn had before.
_Q_NO_SUBJECT_FILLER_RE = re.compile(
    r"\b(?:i|we|me|us|my|our|you|your|it|its|this|that|just|only|simply|sirf|fakt|faqt|khali|bas|"
    r"please|pls|kindly|ok|okay|no|nope|yes|ya|and|but|so|now|then|also|first|na|hey|hi|sir|bro|yaar|"
    r"bhai|said|want|wanted|need|needed|wanna|would|like|to|know|d|"
    r"quick|quickly|brief|briefly|short|shortly|fast|straight|plainly|honestly|directly|"
    r"a|an|the|any|another|new|other|second|extra|more|one|ones|anything|something|thing|files?)\b"
    r"|\W+",
    re.I,
)
#: THE LONGEST MESSAGE that can be nothing but the ask to be told. The ask and
#: the refusal are short phrases — the longest real one measured is 84
#: characters ("please tell me only, do not make a new file - what is the
#: average spend per country?", which names a subject and is not this) — and a
#: message longer than this names plenty besides them, so it is not a pointer
#: whatever the subtraction would say. It is a BOUND, not a heuristic: a pasted
#: refusal is a realistic shape and `_names_nothing_but_the_ask` runs three
#: regex substitutions over the whole text, which cost 24.8 ms on a 108,000-
#: character turn against 330-380 us for a short one (measured 2026-09-28).
_Q_NOTHING_BUT_THE_ASK_MAX_CHARS = 400


def _names_nothing_but_the_ask(text: str) -> bool:
    """Is the whole message the ask to be TOLD and/or the refusal of a new file,
    and nothing else?

    WHY THIS IS THE TEST (2026-09-28). A refusal says which OUTPUT the person
    wants — a sentence, not a file — and never which FILE the question is about.
    Reading it as a pointer at the artifact is what answered three questions
    about an uploaded dataset with a description of a workbook; measured through
    POST /chat with customers.csv and a workbook in one conversation, "dont
    create a file, just tell me the total spend", "please tell me only, do not
    make a new file - what is the average spend per country?" and "sirf bata do
    nayi file mat banao - total spend kitna hai" each came back
    `meta.route` "artifact" and "**Workflow Tracker** (v1) is a workbook with 1
    sheet: `Tasks`".

    What the refusal CAN say is that the message names no subject at all — and
    then the file card the last assistant turn showed is the only thing left for
    it to be about. That is the transcript's own third turn, "i want to Know ??
    please tell me Only Not create d??", which names nothing: it keeps its
    read-back with a dataset in the room, and the three above do not.

    A SUBTRACTION, not a list of shapes. The three phrase patterns are the ones
    the gate already uses to recognise the ask and the refusal, so this cannot
    drift away from them; the residual is measured, not enumerated. It must be
    read on the UNBLANKED text (`decide`'s `own_unblanked`): `_rule_view` blanks
    negated clauses, and on these turns that blanks the question itself — the
    whole of "sirf bata do nayi file mat banao - total spend kitna hai" reduces
    to a single space there, which is how a first attempt at this rule passed a
    turn that names a total straight through.

    WORD ORDER (2026-09-28, round 4). The subtraction has to KEEP the words
    `_Q_TELL_ONLY_RE` allows between the intensifier and the speech verb. In
    English the verb precedes its object, so "just tell me the total spend"
    leaves "total spend" standing; in the verb-final order Gujarati and Hindi
    speakers type, those words ARE the object — "fakt kul spend _read_", "bas
    countries _read_" — and subtracting the phrase whole left nothing, so six
    measured refusals that name their own subject were read as pointers and
    answered "**Workflow Tracker** (v1) is a workbook with 1 sheet: `Tasks`".
    `_Q_NO_SUBJECT_FILLER_RE` still decides whether what is put back names
    anything, which is what keeps "just quickly tell me" a pointer.
    """
    if len(text) > _Q_NOTHING_BUT_THE_ASK_MAX_CHARS:
        return False
    rest = _Q_TELL_ONLY_RE.sub(lambda m: " %s " % (m.group("told_gap") or ""), text)
    rest = _Q_REFUSE_CREATE_RE.sub(" ", rest)
    rest = _Q_TELL_RE.sub(" ", rest)
    return not _Q_NO_SUBJECT_FILLER_RE.sub(" ", rest).strip()


#: A clause that OPENS WITH A BUILD VERB is an instruction, whatever
#: question word stands later in it: "make it show what the totals are"
#: changes the file. The speech verbs (tell, list, explain, summarise, read
#: back) are deliberately absent — they are how the owner asked to be told.
_Q_IMPERATIVE_LEAD_RE = re.compile(
    r"^\W*(?:(?:please|pls|kindly|now|ok|okay|and|also|but|then|plus|just|can|could|would|will|you)\W+)*"
    r"(?:make|creat|add|remov|delet|drop|chang|updat|convert|export|sav|download|renam|retitl|sort|"
    r"highlight|bold|italici|underlin|set|put|insert|fill|turn|render|redo|recreat|rebuild|build|generat|"
    r"writ|draft|prepar|design|develop|mov|merg|split|translat|apply|format|restyl|reformat|redesign|"
    r"shorten|lengthen|expand|trim|cut|fix|tweak|adjust|replac|swap|reorder|restructur|colou?r|styl)\w*\b",
    re.I,
)
#: WRITE THIS INTO the file: "put them in the sheet", "copy that into the
#: tracker", "stick it on slide 3". `_EDIT_VERBS_RE` carries no `put`, and
#: widening that list would change the edit path everywhere, so the shape is
#: named here and read only by the question gate. Measured 2026-09-27: "tell
#: me the totals and put them in the sheet" is ONE clause, and with no shape
#: for it the gate kept the whole turn as a question (1f80aa3:
#: convert/['xlsx']). A question ABOUT what was written is exempt from the
#: veto, so "what did you put in the second sheet ??" is untouched.
_Q_WRITE_INTO_RE = re.compile(
    rf"\b(?:put|place|stick|paste|enter|log|append)\s+"
    rf"(?:(?:it|them|that|this|those|these|_this_|the\s+\w+|a\s+\w+|an\s+\w+)\s+)?"
    rf"(?:in|into|inside|on|onto|to)\s+{_Q_THIS_FILE}",
    re.I,
)
#: A line that opens PASTED material: a table row, a quote marker, a rule, a
#: fence, or a mail/chat header. What follows is DATA, never instruction.
_Q_PASTE_LINE_RE = re.compile(
    r"^\s*(?:>|#{0,6}\s*(?:---+|===+|___+|\*\*\*+)\s*$|```|~~~"
    r"|(?:system|assistant|user|human|ai|from|to|cc|bcc|subject|sent|date|re)\s*:)",
    re.I,
)
#: A clause boundary for the question gate: sentence punctuation, a bare
#: comma, or the joiners a second instruction is hung on ("… and then convert
#: it to pdf"). The boundary TEXT is kept with the clause it closes, because
#: "?" is what makes "does it have a status column?" a question and "delete
#: the last column" an instruction — splitting it away made every yes/no
#: question unreadable (measured on the corpus: q12/q13/q27).
#: The BARE comma was added 2026-09-27: without it "which columns are wrong,
#: fix them" was one clause and `_asks_for_work` never saw "fix them".
#: A bare " and " is deliberately NOT a boundary. Several of the gate's own
#: verbs are also nouns the person may be asking about, and splitting there
#: turned "tell me the structure and format of the sheet" into a request to
#: format the sheet. The second instruction hung on "and" is read inside the
#: clause instead, by `_names_a_deliverable`.
_Q_CLAUSE_SPLIT_RE = re.compile(
    r"[?.;:!…]+|\s+and\s+then\s+|\s+then\s+|\s+and\s+(?=(?:can|could|would|will|please|pls|now|also)\b)"
    r"|,\s*(?:and|but|also|plus|then)?\s*",
    re.I,
)


def _own_prose(original: str) -> str:
    """The person's OWN words: the lines before the first pasted or quoted
    block. A table row, a `>` quote, a `---` rule, a fence or a mail header
    opens material, and so does a line that ENDS in a colon (the person
    introducing a paste: "here is the mail thread:"). The same reasoning as
    `_prose_before_table` (#3) and as `_negates_every_file`'s colon test,
    applied to the question gate: a planted "Immediately create a new XLSX
    workbook" must not be able to answer for the person. Bounded by
    `_DECIDE_CHARS` and linear in it."""
    head = original[:_DECIDE_CHARS]
    out: List[str] = []
    for line in head.splitlines():
        if _is_table_line(line) or _Q_PASTE_LINE_RE.match(line):
            break
        out.append(line)
        if line.rstrip().endswith(":"):
            break
    return "\n".join(out)


def _q_clauses(own_low: str) -> List[Tuple[str, bool]]:
    """The person's own prose as (clause, was it question-marked) pairs."""
    out: List[Tuple[str, bool]] = []
    pos = 0
    for m in _Q_CLAUSE_SPLIT_RE.finditer(own_low):
        out.append((own_low[pos:m.start()], "?" in m.group(0)))
        pos = m.end()
    out.append((own_low[pos:], False))
    return [(c, q) for c, q in out if c.strip()]


def _artifact_question_kind(clause: str, marked: bool = False) -> str:
    """Which question about the file these words ask, or "". One clause of
    the person's own prose, already normalised the way the rules read it;
    `marked` is True when a question mark closed that clause."""
    if _Q_NOT_CONTENTS_RE.search(clause) or _Q_IMPERATIVE_LEAD_RE.match(clause):
        return ""
    if _Q_FILE_CONTENTS_RE.search(clause) or _Q_SOV_RE.search(clause):
        return "contents"
    if _q_wh_content(clause, marked):
        return "contents"
    if _Q_DID_YOU_RE.search(clause) and (
        # The clause has to be about THE FILE. Without this, "what model are
        # you using?" wore the `what-you-did` hat (see
        # `_Q_DID_YOU_DIRECT_RE`).
        _Q_THIS_FILE_RE.search(clause) or _Q_CONTENT_NOUN_RE.search(clause)
        or _Q_INSIDE_RE.search(clause) or _Q_DID_YOU_DIRECT_RE.search(clause)
    ):
        return "what-you-did"
    if (marked or "?" in clause) and _Q_YES_NO_RE.match(clause) and (
        _Q_CONTENT_NOUN_RE.search(clause) or _Q_INSIDE_RE.search(clause)
    ):
        return "contents"
    if _Q_TELL_RE.search(clause) and (
        _Q_FILE_CONTENTS_RE.search(clause) or _q_wh_content(clause, marked)
        or _Q_SOV_RE.search(clause) or _Q_DID_YOU_RE.search(clause)
        or _Q_THIS_FILE_RE.search(clause)
    ):
        return "tell-me"
    return ""


def _chart_ask(clause: str) -> bool:
    """These words ask for a CHART to be drawn — the same test the create
    path makes (`chart_ask`, step 4), so the question gate vetoes itself
    exactly where a chart would otherwise have been produced. A chart merely
    MENTIONED is not this: "what is in the chart?" names no ask verb, which
    is why `LX.chart_signal` alone cannot stand here."""
    return (LX.chart_signal(clause) and bool(_CHART_ASK_RE.search(clause))
            and not _QUESTION_ABOUT_RE.match(clause.strip())
            and not _STORY_PLOT_RE.search(clause))


def _names_a_deliverable(clause: str) -> bool:
    """Does THIS clause — the one that also reads as a question — name the
    thing to be PRODUCED? A format to put the content in, a new file, a chart
    to draw, an edit to make, or a second instruction hung on "and".

    `_CREATE_RE` and `F.explicit_formats` are deliberately absent. Both match
    inside the owner's own question — "what you create inside the sheet" has
    the verb `create` and the format word `sheet` — and reading either as a
    deliverable is the defect this gate exists to fix.

    `_AS_FORMAT_RE` is not read on an SOV clause. Its postposition arm exists
    for "pdf me de do" (give it IN pdf), but `_in_` is also the locative of
    the question itself: corpus q31 "kya hai is sheet me ??" normalises to
    "kya hai _this_ sheet _in_", where that arm matched "sheet _in_" and the
    Hinglish question went back to converting the workbook (measured
    2026-09-27). `_Q_SOV_RE` is exactly that shape, and it needs a question
    word, so "is sheet ko pdf me de do" is untouched."""
    return bool(
        _CONVERT_RE.search(clause) or _MAKE_IT_FORMAT_RE.search(clause)
        or (_AS_FORMAT_RE.search(clause) and not _Q_SOV_RE.search(clause))
        or _NEW_FILE_RE.search(clause) or _POSITIONAL_CREATE_RE.search(clause)
        or _EDIT_VERBS_RE.search(clause) or _MORE_EDIT_VERBS_RE.search(clause)
        or _Q_WRITE_INTO_RE.search(clause) or _MAKE_IT_RE.match(clause.strip())
        or _chart_ask(clause)
    )


def _asks_for_work(clause: str) -> bool:
    """Does this clause ask for a file to be made or changed? Read on the
    clauses that are NOT the question, where the broad creation shapes are
    safe: "what is in it? and can you add a total row?" keeps its edit."""
    return bool(
        _CREATE_RE.search(clause) or _Q_IMPERATIVE_LEAD_RE.match(clause)
        or _names_a_deliverable(clause)
    )


def _artifact_question(own_low: str) -> str:
    """The question these words ask about the file, or "": a question in one
    clause, and no clause — the question's own included — asking for work.
    The ask wins when both are said, whether they are said in two clauses
    ("tell me what the sheet has and then convert it to pdf") or in one
    ("tell me the totals and put them in the sheet", "show me the totals as a
    pie chart").

    A clause that is a question ABOUT WHAT WAS DONE (`_Q_DID_YOU_RE`) is the
    one exception: the verb there belongs to the question, so "what you create
    inside the sheet", "did you include the due dates?", "why did you add a
    priority column?" (corpus q26) and "what did you put in the second sheet
    ??" (q11) keep their answer. The test is that shape and NOT the kind
    label: q11 and q26 are labelled `contents`, because a past-tense question
    about the contents is still a question about the contents. A polite
    instruction that wears a question word ("why don't you add a totals row")
    never reaches this point — `_Q_NOT_CONTENTS_RE` drops it first.

    Until 2026-09-27 the veto ran only on the clauses that produced NO kind,
    so one clause that both asked to be told and asked for a file was decided
    as a question. Measured against 1f80aa3: that cost 43 of the 61 held-out
    corpus neighbours their file, and 50 of a 118-case probe wave over the two
    verifiers' phrasings. Among them every "show me … as a pie chart"
    (create/create-chart) and every "… and add them to the sheet"
    (edit/edit-element)."""
    clauses = _q_clauses(own_low)
    kinds = [_artifact_question_kind(c, marked) for c, marked in clauses]
    if not any(kinds):
        return ""
    for (clause, _marked), kind in zip(clauses, kinds):
        if not kind:
            if _asks_for_work(clause):
                return ""
        elif not _Q_DID_YOU_RE.search(clause) and _names_a_deliverable(clause):
            return ""
    return next(k for k in kinds if k)


def decide(
    text: str,
    *,
    has_artifacts: bool = False,
    artifact_hints: Sequence[str] = (),
    has_assistant_answer: bool = False,
    upload_formats: Sequence[str] = (),
    last_turn_is_artifact: bool = False,
    artifact_id: Optional[str] = None,
    last_deliverable: Optional[Any] = None,
    has_dataset: bool = False,
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
    `last_deliverable`: the SHAPE of the most recent published version
    (artifacts/deliverable.Deliverable, or the jsonb the version row
    carries), so "make it a bar chart instead" can be read as a change to
    the chart that was just made instead of a second one.
    `has_dataset`: the conversation holds an uploaded dataset (the chat
    route's `dataset_ready`), so a bare "give Big report" or "I want plot ??"
    is a file made from it (`_dataset_ask`).
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
    low = _rule_view(raw)
    low = _REPORT_VERB_RE.sub(r"\g<lead> _report_", low)
    uploads = [str(f).lower().lstrip(".") for f in (upload_formats or ()) if f]
    explicit = F.explicit_formats(low)
    rows = _row_count(_without_negated_clauses(_prose_before_table(original).lower()))
    language = LX.language_of(raw)
    style = bool(LX.style_phrases(low))
    chart = LX.chart_signal(low)
    prev_shape = _shape_of(last_deliverable)

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
            if refs or (uploads and _UPLOAD_SOURCE_RE.search(low)):
                target = "upload"
            elif has_assistant_answer and _BARE_REF_RE.search(low) and not _NEW_TOPIC_RE.search(low):
                # "visualise this table on pie chart", "pie chart of that",
                # "chart it": the file is made FROM the answer being pointed
                # at, not from the conversation at large. Measured
                # 2026-09-16: the owner's own trigger case reported its
                # source as `conversation`, so nothing downstream could tell
                # that the table in the previous answer was the material.
                target = "previous_answer"
            else:
                target = "conversation"
            kw.setdefault("target", target)
            kw.setdefault("upload_refs", refs)
        kw.setdefault("style_request", style and action != "none")
        kw.setdefault("chart_request", chart and action != "none")
        return ArtifactIntent(action, raw_text=original, row_count=rows, new_artifact=(action == "create"),
                              language=language, **kw)

    #: The person's own prose — the message without the material pasted
    #: under it — as the rules read it, and as the NORMALISER wrote it before
    #: the negated clauses were blanked (the refusal needs to see them). Both
    #: are computed once, only for a conversation that holds an artifact, and
    #: the common case (nothing pasted) reuses `low` instead of normalising
    #: again: `decide` runs on the event loop.
    own: dict = {}

    def own_text() -> str:
        if "low" not in own:
            head = _clean(_own_prose(original))[:_DECIDE_CHARS]
            own["raw"] = head
            own["low"] = low if head == raw else _rule_view(head)
        return own["low"]

    def own_unblanked() -> str:
        if "norm" not in own:
            own_text()
            own["norm"] = LX.normalize(own["raw"].lower())
        return own["norm"]

    def artifact_question_answer(**kw) -> Optional[ArtifactIntent]:
        """The turn is a QUESTION about the artifact this conversation holds,
        so it is ANSWERED from that artifact and no file is made. Two
        signals, either of which is enough:

          * an interrogative shape about THIS file with no other clause
            asking for work ("what does this sheet contain?", "what columns
            does it have?", "tell me what you put in there");
          * the person REFUSING a new file while asking to be told ("please
            tell me Only Not create", "sirf bata do nayi file mat banao",
            "i dont want a new one") — the strongest signal in the
            transcript, and the one that must hold whatever else the
            sentence contains.

        A file ATTACHED to this turn is not this: a read verb on an upload is
        the hard-negative pass's ("summarize this pdf"), and answering it from
        the artifact instead would read back the wrong document.

        The shapes are tested first and the vetoes second: a turn that asks no
        question about the file leaves this function after one pass over the
        person's own prose, which is what every edit, conversion and create
        turn does."""
        kind = _artifact_question(own_text())
        if kind:
            if _Q_REFUSE_CREATE_RE.search(own_unblanked()):
                kind = "told-not-to-create"
        elif (_Q_REFUSE_CREATE_RE.search(own_unblanked())
                and (_Q_TELL_ONLY_RE.search(own_text()) or _Q_TELL_ONLY_RE.search(own_unblanked()))):
            kind = "told-not-to-create"
        elif last_turn_is_artifact and _Q_TELL_ONLY_BARE_RE.match(own_text()):
            kind = "tell-me-only"
        else:
            return None
        if uploads and (LX.reads_source(own_text()) or _UPLOAD_SOURCE_RE.search(own_text())):
            return None
        shape = LX.negative_shape(own_text(), uploads)
        if shape is not None and shape != "trivia":
            # A how-to, a request for code, praise: the hard negatives own
            # them. `trivia` is excepted because it is what "what is in this
            # sheet" and "what is on slide 3?" were classified as — a
            # question about the FORMAT is `_ABOUT_FORMAT_RE`'s, below.
            return None
        if _ABOUT_FORMAT_RE.search(own_text()) and not _Q_THIS_FILE_RE.search(own_text()):
            # "what is a PDF?" is the format as a concept. "what is on slide
            # 3?" names a part of THIS file and is not: `slides?` is a format
            # word, so the concept rule matched it and the answer was lost.
            return None
        kw.setdefault("reference", _which(low, artifact_hints))
        kw.setdefault("reference_hint", _hint(low, artifact_hints))
        # WHICH file the question is about, recorded for the route (see
        # `ArtifactIntent.names_our_file`). The shape signals are as good as
        # the pointer here: a question about what YOU did is about the file
        # you made, the SOV order carries the file word as its object
        # ("sheet _in_ su su che" — corpus q36), and a person refusing a new
        # file while asking to be told is talking about the one that exists.
        names_ours = bool(
            # The artifact by TITLE outranks everything: nothing is more
            # specific than the file's own name.
            _mentions_hint(own_text(), artifact_hints)
            or (
                (_Q_OUR_FILE_RE.search(own_text())
                 or _Q_DID_YOU_RE.search(own_text())
                 or _Q_SOV_OUR_FILE_RE.search(own_text())
                 # A REFUSAL IS NOT A POINTER — unless the message names
                 # nothing else at all (2026-09-28; see
                 # `_names_nothing_but_the_ask`, which measures that). "Don't
                 # make a file, just tell me X" says which OUTPUT the person
                 # wants and leaves X to say what the question is about; "just
                 # tell me" after the file card names nothing, so the card is
                 # the only thing left.
                 or (kind in ("told-not-to-create", "tell-me-only")
                     and _names_nothing_but_the_ask(own_unblanked())))
                # …unless the person named the file as THEIRS.
                and not _Q_THEIR_UPLOAD_RE.search(own_text())
            )
        )
        return made("none", rule=f"answer-artifact:{kind}", answer_about_artifact=True,
                    names_our_file=names_ours, target="artifact", formats=[], **kw)

    # 0. The UI's "Edit with a prompt" names the artifact (AS3 (i)); the
    #    caller checked that the person owns it.
    if artifact_id:
        if _CONVERT_RE.search(low) and explicit:
            return made("convert", reference="named", reference_hint="", rule="ui-convert", artifact_id_hint=str(artifact_id))
        if LX.undo_signal(low) and not _VERSION_RE.search(low):
            return made("edit", reference="latest", rule="ui-undo", artifact_id_hint=str(artifact_id))
        # A QUESTION typed into the edit box is still a question. Until
        # 2026-09-27 this branch bound EVERY turn to an edit, so "what
        # columns does it have?" re-rendered the file (rule=`ui-edit`).
        answer = artifact_question_answer(artifact_id_hint=str(artifact_id))
        if answer is not None:
            return answer
        version = _VERSION_RE.search(low)
        return made("edit", reference="named", reference_hint=f"version {version.group(1)}" if version else "",
                    version=int(version.group(1)) if version and re.search(r"\b(?:go back|revert|restore|use|return|switch|undo)\b", low) else None,
                    rule="ui-edit", artifact_id_hint=str(artifact_id))

    # 1a. A QUESTION ABOUT THE ARTIFACT (2026-09-27). Before every other
    #     rule that can read a file word as a deliverable: "Ok What This
    #     sheet have ??" was a CONVERSION to xlsx because `sheet` inside the
    #     question named a format, and "what is in this sheet" was format
    #     trivia. Nothing here makes or changes a file, so no rule below is
    #     weakened; the turn is answered from the artifact instead.
    if has_artifacts:
        answer = artifact_question_answer()
        if answer is not None:
            return answer

    # 1. The HARD-NEGATIVE pass (AS3 (a)) and the old question/code checks:
    #    a format named as the source, a how-to, trivia, praise or a request
    #    for code is not a file. First, because "explain how to create a PDF
    #    in Python" has a creation verb.
    # The two forms added 2026-09-18 — the answer placed here, every file
    # ruled out — yield to a file named outright and to a chart (a chart is
    # SHOWN in the chat: "plot this and show it inline"). With a file in the
    # conversation they are read after the edit rules (step 2), because
    # "Update the report, don't create a file" edits that report.
    no_file = (bool(_answer_placed_here(low) or _negates_every_file(raw.lower()))
               and not (chart or _NAMED_FILE_RE.search(low)) and not _FILE_DESPITE_RE.search(low))
    if (_CHAT_ONLY_RE.search(low) and not _FILE_DESPITE_RE.search(low)) or (no_file and not has_artifacts):
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
    #     report, whose chart is refused where charts are refused, and
    #     "if a map is not possible, draw a pie chart of the states" draws
    #     the pie chart. That second guard is `names_a_drawable_type`: until
    #     2026-09-16 the check read FILE formats only (F.kind_for), so a
    #     message naming a map AND a chart this platform CAN draw was
    #     refused outright and nothing was drawn (verifier).
    _visual = VIS.asked_for(low)
    if _visual is None and VIS.named_unsupported(low) is not None:
        # THE ASK VERB IS NORMALISED AWAY (verifier gap, 2026-09-16).
        # `low` is the lexicon's normalisation, where "dikhao"/"दिखाओ"
        # becomes the token `_read_`: "isko map pe dikhao" and
        # "इसे नक्शे पर दिखाओ" kept the map but lost the verb, so both fell
        # through to no-request — no file, and no honest sentence either.
        # The person's own words still carry the verb, and this only runs
        # when the NORMALISED text already names the visual, so a clause the
        # normalisation blanked ("I don't want a map") cannot come back.
        _visual = VIS.asked_for(raw.lower())
    if (_visual is not None and not explicit and F.kind_for(low, [])[1] == "default"
            and not VIS.names_a_drawable_type(low)):
        return made("none", rule=f"unsupported-visual:{_visual.token}", instruction="",
                    unsupported_visual=_visual.token)

    # 2. Follow-ups on an existing artifact.
    if has_artifacts:
        # A REMARK about the file is not an instruction. "i opened the docx
        # on my phone and the table is cut off" was read as rule="edit" and
        # the platform silently re-rendered the artifact (MEASURE 1 A26,
        # 2026-09-16): `cut` is an edit verb (_EDIT_VERBS_RE) and `the docx` a
        # reference, and `table` is an element, so three branches below fired
        # on a sentence that asked for nothing. The convert branch already
        # refused this same sentence; the edit branches now read the same
        # shape. A request marker anywhere in the sentence takes it back —
        # "i opened the docx and the table is cut off, can you fix it?" is an
        # edit — so only a report with nothing asked in it is dropped.
        remark = bool(_STATEMENT_RE.match(low)) and not _ASKING_RE.search(low)
        version = _VERSION_RE.search(low)
        if version and re.search(r"\b(?:go back|revert|restore|use|return|switch|undo)\b", low):
            return made("edit", reference="named", reference_hint=f"version {version.group(1)}",
                        version=int(version.group(1)), rule="restore-version")
        if LX.undo_signal(low) and len(low.split()) <= 12 and not (explicit and _AS_FORMAT_RE.search(low)):
            # "undo that", "revert", "पहले जैसा कर दो" (AS3 (g)).
            return made("edit", reference="latest", rule="restore-version")
        # 2a. The last deliverable HELD A CHART and these words change only
        #     its type: "make it a bar chart instead", "now do the same as a
        #     line chart". An EDIT, so the binding that version already
        #     carries is re-rendered (edits.preplan → set_chart → 0 model
        #     calls) instead of a second artifact whose numbers the model is
        #     asked for again — production 2026-09-16, where "visualise this
        #     table on pie chart" was followed by a new file every time.
        #     A message that names its own DATA ("visualise this table …") is
        #     a new chart, and a document format named outright is a convert.
        if prev_shape is not None and prev_shape.has_chart and not explicit and _chart_type_change(low):
            return made("edit", reference="latest", reference_hint=_hint(low, artifact_hints),
                        rule="edit-chart-type", chart_request=True)
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
        # An EDIT that NAMES the file it changes is not a conversion of the
        # newest one: "not that one, change the leave policy pdf" re-rendered
        # the hiring-plan deck (measured 2026-09-16, S57) because this branch
        # hard-codes reference="latest", and "make the deck shorter" must not
        # become a re-render just because `deck` also names the pptx format.
        # A real conversion still says so ("as a pdf", "convert", "make it a
        # docx") and is not caught here.
        _edit_named = bool((_EDIT_VERBS_RE.search(low) or _MORE_EDIT_VERBS_RE.search(low))
                           and (_mentions_hint(low, artifact_hints) or _REFERENCE_FILE_RE.search(low))
                           and not (_CONVERT_RE.search(low) or _AS_FORMAT_RE.search(low) or _MAKE_IT_FORMAT_RE.search(low)))
        if last_turn_is_artifact and explicit and not _positional_create(low) and not _NEW_TOPIC_RE.search(low) and not _styled_place \
                and not _STATEMENT_RE.match(low) and not _edit_named and not _COUNT_PART_RE.search(low) and (
            _BARE_REF_RE.search(low) or _FOLLOWUP_VERB_RE.search(low) or _AS_FORMAT_RE.search(low) or _FORMAT_ONLY_RE.match(low)
        ):
            return made("convert", reference="latest", reference_hint=_hint(low, artifact_hints, exclude=_target_words(explicit)),
                        rule="convert-artifact-turn")
        if _CONVERT_RE.search(low) and explicit:
            return made("convert", reference=_which(low, artifact_hints),
                        reference_hint=_hint(low, artifact_hints, exclude=_target_words(explicit)), rule="convert")
        # 2a-bis. A bare ANAPHOR points at the file that was just made: "do
        #     the same for headcount", "same but shorter", "do it again",
        #     "and the same again but as a line". Measured 2026-09-16: nine
        #     of the forty-seven wrong turns in the follow-up corpus were one
        #     of these, and every one was answered as chat — "the same"
        #     resolved to nothing anywhere in the layer. "One more like that"
        #     is the other shape: an ADDITIONAL file modelled on it, which
        #     stays a create (S27). Both need the words to be anchored to a
        #     file — the last turn was its card, or the title is named —
        #     so "do the same" in a fresh conversation is still chat.
        _anchored_anaphor = last_turn_is_artifact or _mentions_hint(low, artifact_hints)
        if _anchored_anaphor and _ANOTHER_LIKE_RE.search(low):
            return made("create", rule="another-like")
        # A QUESTION about what was done ("did you do the same for q2?") is
        # not an instruction; a bare "do it again" is, and `do` leads both —
        # so the question mark, not the opening word, decides here.
        if _anchored_anaphor and _SAME_AGAIN_RE.search(low) and not _SAY_AGAIN_RE.search(low) \
                and not ("?" in raw and _QUESTION_ABOUT_RE.match(low)) and not LX.reads_source(low) \
                and not _is_remark(low) and not _chart_type_change(low):
            if explicit and (_CONVERT_RE.search(low) or _AS_FORMAT_RE.search(low) or _positional_create(low)):
                return made("convert", reference=_which(low, artifact_hints),
                            reference_hint=_hint(low, artifact_hints, exclude=_target_words(explicit)), rule="anaphor-convert")
            if not _positional_create(low):
                # "also make a deck of the same" names a NEW file with the
                # old content: the create rules below own it.
                return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="anaphor-edit")
        # 2b. A new file, said first: "Create a professional PDF report on
        #     X. Make it visually professional." is a create, not an edit
        #     of the last artifact (CONTRACT-2 §5; discovery C2).
        if _positional_create(low) and not no_file:
            return made("create", rule="create-first-clause")
        # 2c. A pronoun follow-up after an ANSWER (not a file card) exports
        #     that answer even when files exist in the conversation.
        if has_assistant_answer and not last_turn_is_artifact and not chart and not style and not (
            _EDIT_VERBS_RE.search(low) or _MORE_EDIT_VERBS_RE.search(low)
        ):
            rule = _export_shape(low, explicit)
            if rule and not re.search(r"\b(?:also|too|as well|same|version|copy)\b", low):
                return made("export", reference="previous_answer", rule=rule)
        # A REMARK about the file is not an instruction: "I opened the docx
        # on my phone and the table is cut off" silently re-rendered the
        # artifact, because `cut` is an edit verb and `the docx` a reference
        # (measured 2026-09-16). `_STATEMENT_RE` already knew this shape and
        # was read only by the create rules. `_MORE_EDIT_VERBS_RE` joins the
        # edit verbs here so "sort it by value" — a verb this list holds and
        # a pronoun the reference rule resolves — stops being chat; the
        # reference condition on the same line keeps it off an unanchored
        # turn.
        # The WIDER verb list needs the words to point at the FILE, not at any
        # noun in the room: "highlight the key risks in this contract" is a
        # question about a contract in a conversation that happens to hold a
        # file, and `highlight` + `this contract` made it an edit of that file
        # (AS3 verifier case, re-measured 2026-09-16 on the merged tree).
        _wider_edit = _MORE_EDIT_VERBS_RE.search(low) and (
            last_turn_is_artifact or _mentions_hint(low, artifact_hints)
            or _REFERENCE_FILE_RE.search(low) or _PRONOUN_OBJECT_RE.search(low)
        )
        if not _is_remark(low) and (_EDIT_VERBS_RE.search(low) or _wider_edit) \
                and (_REFERENCE_RE.search(low) or _mentions_hint(low, artifact_hints)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit")
        if not _is_remark(low) and _IMPERATIVE_EDIT_RE.match(low) and len(low.split()) <= 12:
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit-imperative")
        # 2d. A style clause, or an edit verb on an element (AS3 (f)): "make
        #     the headings dark blue", "make the document landscape", "font
        #     Arial kar do". A question ABOUT the look is not an edit.
        asks = (not _QUESTION_ABOUT_RE.match(low) or re.match(r"^\W*(?:can|could|would|will)\s+(?:you|the|it|we)\b", low)) and not remark
        # Verifier 2026-09-15: in a conversation that holds any file, "give
        # me bullet points on climate change" (change), "highlight the main
        # takeaways from our discussion" and "bold claim: …, discuss" were
        # edits of that file. A style or element edit needs the words to
        # point at the file — a part only a file has, the file itself, a
        # pronoun that is the object ("make it bold") — or the last turn to
        # be the file card.
        anchored = last_turn_is_artifact or bool(_FILE_PART_RE.search(low) or _PRONOUN_OBJECT_RE.search(low)
                                                  or _REFERENCE_FILE_RE.search(low) or _mentions_hint(low, artifact_hints))
        # A REMARK is not an instruction, whatever its words point at: "I
        # opened the docx on my phone and the table is cut off" reached the
        # element rule with `table` and `cut` and re-rendered the file.
        asks = asks and anchored and not _is_remark(low)
        if style and asks and not (explicit and _AS_FORMAT_RE.search(low) and _CREATE_RE.search(low)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints),
                        rule="edit-style", style_request=True)
        if asks and _ELEMENT_RE.search(low) and (_EDIT_VERBS_RE.search(low) or _PUT_IN_ARTIFACT_RE.search(low)
                                                 or _MORE_EDIT_VERBS_RE.search(low) or _NEEDS_EDIT_RE.search(low)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints), rule="edit-element")
        # "make it two pages", "make it a pie", "keep it to one slide": a
        # verb with a PRONOUN object and any complement changes the file.
        # A FORMAT complement ("make it a docx") is a conversion and was
        # decided above. Measured 2026-09-16: five consecutive follow-ups on
        # one report ("make it two pages", "make it a pie", …) were all
        # answered as chat, because `make` is an edit verb only in front of
        # a closed adjective list.
        # …but "make it a bar chart instead" when NOTHING chart-shaped was
        # made changes nothing: there is no chart to retype, so it is a new
        # chart and the create rules below own it (the shape-aware branch
        # above already took the case where a chart IS there).
        if asks and _MAKE_IT_RE.match(low) and not _MAKE_IT_FORMAT_RE.search(low) \
                and not (_chart_type_change(low) and (prev_shape is None or not prev_shape.has_chart)):
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints),
                        rule="edit-pronoun-object")
        # A short verbless comparative on the file or one of its parts:
        # "and the report longer", "a bit more spacing".
        if asks and _COMPARATIVE_EDIT_RE.match(low) and len(low.split()) <= 8:
            return made("edit", reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints),
                        rule="edit-comparative")
        # "Also as PDF" with nothing else said.
        if explicit and re.match(r"^\s*(?:also|and|plus|too)?\s*(?:as|in)\s+(?:an?\s+)?\w+(?:\s+\w+)?\s*(?:too|as well|please)?\s*[.!]?\s*$", low):
            return made("convert", reference="latest", rule="convert-short")
        if explicit and re.search(r"\balso\b", low) and (_SOV_RE.search(low) or _FORMAT_ONLY_RE.match(low)):
            # "pdf version bhi chahiye" → "pdf version also _give_".
            return made("convert", reference="latest", rule="convert-short")
        # No edit rule took it, and the person ruled out a file or placed the
        # answer here: no rule below may make one. The classifier may still
        # read it (not "chat-only"): with a file in the room, "In the audit
        # report, answer inline each reviewer question" can be an edit, and
        # before 2026-09-18 it reached the classifier as no-request.
        if no_file:
            return made("none", rule="no-file-asked", instruction="")

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

    # 3c. A report or a chart asked of the conversation's DATASET (hotfix
    #     1.1): decided here so it never depends on the classifier. The
    #     report is made from the conversation, not from the answer before.
    if has_dataset:
        dataset_rule = _dataset_ask(low, raw, chart)
        if dataset_rule == "dataset-report":
            return made("create", rule=dataset_rule, target="conversation")
        if dataset_rule:
            return made("create", rule=dataset_rule)

    # 4. Creation.
    if _TEXT_OBJECT_RE.search(low) and not explicit and not _AS_FORMAT_RE.search(low) and not _FILE_CUE_RE.search(low):
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
    # A piece asked for by its length, with a format named anywhere: "Write
    # a 2,000-word article on leave. PDF please." Until 2026-09-18 the count's
    # `word` made every such ask a file; now the named format has to.
    counted = (bool(explicit or _FILE_CUE_RE.search(low)) and bool(_COUNTED_PIECE_RE.search(low))
               and not _QUESTION_ABOUT_RE.match(low))
    if _CREATE_RE.search(low) or as_format or _BEST_OR_ALL_RE.search(low) or sov or chart_ask or noun_first or counted:
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
#: The formats a verdict may name. png and svg are on it since 2026-09-16:
#: intent_llm.FORMATS has offered them since the AS3 integration and the
#: prompt names "PNG/SVG charts", but this filter listed only the five
#: document formats, so the classifier could never produce a chart — the one
#: escape hatch for a chart ask the rules cannot read was closed at the exit.
_VERDICT_FORMATS = ("pdf", "docx", "pptx", "xlsx", "csv", "png", "svg")


def _should_consult(intent: ArtifactIntent, text: str) -> bool:
    """The band the model may decide (AS3 (3)): the rules found no request,
    no negative shape (or code/about-format veto) fired, and the message
    names a file, a format or a chart. The old ambiguous band is in it."""
    if intent.action != "none":
        return False
    if intent.answer_about_artifact:
        # A question about the artifact is DECIDED. On Fast the classifier has
        # a ~2.5 s budget and falls back to these rules in silence when it
        # runs out, so a decision that can be flipped into a file by a model
        # is a decision that fails under load — which is how the transcript's
        # "please tell me Only Not create" came back as a third workbook.
        #
        # This line removes the recovery path for a MISREAD, so it is only
        # honest while the rules do not misread. Re-measured 2026-09-27 on the
        # 180-turn corpus after the clause-level veto landed: of the 61
        # held-out neighbours, 0 file requests are claimed as questions
        # (no_file 4, the same 4 that 1f80aa3 misses) and 0 turns that are not
        # about the artifact are claimed either (over_grounded 0). Before the
        # veto those two counts were 43 and 5.
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
    formats = [f for f in (getattr(verdict, "formats", None) or []) if f in _VERDICT_FORMATS]
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
        # png and svg exist in this product only as chart images
        # (types.CHART_IMAGE_FORMATS_FOR_KIND), so a verdict that names one
        # IS a chart request whatever the model put in the boolean.
        chart_request=bool(getattr(verdict, "chart_request", False)) or any(f in T.IMAGE_FORMATS for f in formats),
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
    "last_deliverable", "has_dataset",
})
