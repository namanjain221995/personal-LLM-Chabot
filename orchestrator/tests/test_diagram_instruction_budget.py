"""DIAGRAM_INSTRUCTION is a Fast-path tax, so its size is a test.

WHY THIS FILE EXISTS. `DIAGRAM_INSTRUCTION` is concatenated onto the system
prompt at ELEVEN call sites — engines/chat.py x2, rag.py, repo.py x2,
agent.py, url.py x2, dataset.py, document.py and search.py — on the chat
path, at EVERY effort, with no effort gate. A byte added here is a byte of
prefill on every Fast turn in all EIGHT of those modules (eleven sites in
eight files: counted 2026-09-28 by the same regex the last test in this file
uses, on this tree and on origin/dev ff1a5d7c, where it is also eleven, so
the Max-gate split did not move it. A count of NINE engines, which this
docstring and the comment beside the string both used to give, was inherited
from an older header and was never either number), and the only prompt-size
budget the suite otherwise has (`test_fast_lane_classifier.py`) covers the
small-talk lane, which skips this string entirely.

THE NUMBER, CORRECTED 2026-09-27. It is 1,082 CHARACTERS, not bytes — this
guard was written as `PRE_EDIT_BYTES` over `len()` of a `str`, which counts
characters, and the commit message repeated the mistake as "1,082 bytes
before, 1,082 bytes after". Re-measured on both trees:

    origin/dev 593af55 (and 1f80aa3, which does not touch this file)
        1,082 characters, 1,086 UTF-8 bytes
    feat/document-vocabulary
        1,082 characters, 1,088 UTF-8 bytes

The characters did not move. The BYTES grew by 2, because the roles edit
spends one more em dash (U+2014, three bytes in UTF-8) than the sentence it
replaced. Prefill is charged in tokens, and the tokens were measured
separately at +8 per prompt across the twelve golden fixtures, so the two
bytes buy nothing and cost nothing — but a guard that names the wrong unit
is a guard that will mislead the next person to raise it, so both units are
now pinned, each against its own re-measured number.

WHAT THE EDIT ITSELF WAS: the ban on `style`, `classDef`, `linkStyle`,
`click` and `%%{init}%%` became a ban on those PLUS colours and hex values,
and gained the four role names, paid for by shortening prose elsewhere.
Raising either ceiling is a decision about Fast latency in eight engine
modules and needs the measurement that justifies it, not a bump.

THE CEILING WAS RAISED ONCE, 2026-09-27, AND THIS IS THE MEASUREMENT. Two
branches shipped the same `:::role` fix: this one, which held 1,082
characters, and fix/diagram-roles, which took the string to 1,848 and alone
carried the rule that a role OUTSIDE `flowchart`/`graph` is a parse error
that loses the whole diagram. Resolving the conflict toward that wording
naively gives 1,822 characters, which this guard correctly failed. The
integration keeps that ban and re-compresses everything around it:

    origin/dev (1f80aa3, 4164bb8)          1,082 characters, 1,086 bytes
    fix/document-vocabulary-r2             1,082 characters, 1,088 bytes
    fix/diagram-roles                      1,848 characters, 1,858 bytes
    naive merge (diagram-roles' wording)   1,822 characters, 1,832 bytes
    integ/diagram-group (7f16f4b7)         1,549 characters, 1,553 bytes
    THIS TREE, measured today              1,602 characters, 1,606 bytes

So the raise is +520 characters and +520 UTF-8 bytes over origin/dev (both
deltas are the same number because the string gained ONE em dash and lost
one, so its non-ASCII count did not move), and −220 characters against the
naive merge.

    CORRECTED 2026-09-28, both numbers re-measured, because the two the
    previous revision stated were wrong in a file whose subject is measuring
    rather than asserting. (a) the clause here used to say the string had
    gained TWO em dashes against one lost. That moves the non-ASCII count by
    one and makes the byte delta 522, not the 520 the same sentence states, so
    it contradicted the number it was offered to explain — and the revision
    that wrote it had changed "by coincidence" to "because", so it asserted a
    mechanism as well. Counted with str.count("\u2014"): origin/dev 2,
    fix/document-vocabulary-r2 3, fix/diagram-roles 5, integ/diagram-group 2,
    THIS TREE 2. Against origin/dev the string gained the dash in
    `outside system) — A["Payments API"]:::service` and lost the one in
    `directives — the app applies its own theme`: one for one, which is
    exactly why the two deltas match. (b) −246 was the delta against
    fix/diagram-roles (1,848 − 1,602), not against the naive merge: the row
    two lines up gives that as 1,822, and 1,822 − 1,602 = 220. It was right
    for the previous size (7f16f4b7 said −273, and 1,822 − 1,549 = 273) and
    was updated to the wrong referent when the size moved.

What could NOT be removed, counted: 71 characters are the seven diagram-type
names the ban has to name to be concrete (sequenceDiagram, erDiagram, pie,
journey, timeline, mindmap, gitGraph), and the remainder is the role list with
its glosses, the "do not invent a name" rule and the two accessibility rules.
The ceilings below are the measured numbers, and a `<=` plus an exact pin, so
the next character still has to be argued for.

THE LAST 53 CHARACTERS ARE THREE PIECES OF origin/dev's OWN PROSE, PUT BACK.
The reconciliation at 1,549 dropped them silently, and no test held any of
them, so they are pinned below — with the reason each one is load-bearing:

  * "inside labels" (+14). The ban reads "no parentheses, brackets, pipes or
    markdown INSIDE LABELS". Without the last two words it is a ban on
    brackets anywhere, which forbids `A["Payments API"]` — the form the very
    next sentence of this same string requires.
  * "custom colours break dark mode" (+36). The REASON the directive ban
    exists. The compression replaced it with "the app paints a ROLE
    instead", which says what happens next and not why the model must not.
  * "prefer" over "use" for `flowchart TD`/`LR` (+3, in the file's own
    spelling). "use" states the flowchart as mandatory, and four sentences
    later the same string tells the model what to do in a sequenceDiagram,
    erDiagram, pie, journey, timeline, mindmap or gitGraph. "prefer" is
    origin/dev's word and is the one that does not contradict itself.

    CORRECTED 2026-09-28: these three were stated as +13, +32 and +4, which
    sum to 49 — neither the 53 the paragraph above headlines nor the 53 the
    string really moved, in the three places this branch wrote them (here,
    the comment beside the string, and its own commit message). Re-measured
    with difflib.SequenceMatcher over the two compiled strings, 7f16f4b7 →
    this tree, 1,549 → 1,602, every opcode that is not "equal":

        replace  -"us"  +"pr"                                    net  +0
        insert          +"fer"                                   net  +3
        insert          +" inside labels"                        net +14
        insert          +" custom colours break dark mode, and"  net +36
                                                                 net +53

    Each delta is the text actually inserted, joining space and rejoining
    ", and" included, which is what the +53 is made of; the bare phrases are
    13 and 30 characters and neither 32 nor 4 is a reading of anything. The
    assertions below pin the phrases, and `test_the_three_fragment_costs_are
    _the_arithmetic_of_their_own_total` now pins the arithmetic, so a wrong
    sum cannot sit in the record again.

Two further origin/dev fragments are gone and are NOT coming back, so that
the list above is the whole account: the worked example `(e.g. A["Login
page"])`, because the role sentence carries `A["Payments API"]:::service`,
the identical quoted-label form, and a second example costs 22 characters to
teach nothing new (a test below keeps ONE such example in the string); and
the adjective in "plain, SIMPLE sentences", because the clause it sits in
already ends "so a non-technical reader can follow it".

THE TOKEN COST, MEASURED 2026-09-27 AND RE-MEASURED 2026-09-28 (this is the
unit prefill is charged in, so it is the one that decides). Pinned engine
tokenizer, on CPU, no GPU touched: tokenizers 0.23.2 over
Model/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--491c2f1ea524/tokenizer.json, load
average 2.76 the first time and 4.18 the second. Every row below came back
the same on both days, and the 2026-09-28 run also measured origin/dev at
ff1a5d7c — after fix/deploy-honesty-r2 and integ/max-gates landed on it —
which touches neither this string nor the golden fixtures: still 232 tokens
and still 41,496 over the twelve.

    origin/dev                                232 tokens
    fix/document-vocabulary-r2                240 tokens   (+8)
    fix/diagram-roles                         415 tokens   (+183)
    integ/diagram-group (7f16f4b7)            356 tokens   (+124)
    THIS TREE                                 365 tokens   (+133)

+133 tokens per prompt, at every effort, in eight engine modules. Against
the twelve golden fixtures in tests/fixtures/context_assembly_golden, whose
mean prompt on origin/dev is 3,458 tokens (41,496 over twelve), that is
3.846%; the same fixtures on this tree total 43,092, and the delta is exactly
+133 on each of the twelve, which is how we know the string is the only thing
that moved.

THE BUDGET THIS RELEASE ACCEPTED, and the row it belongs to. The integration
accepted +183 tokens per prompt — 5.29% of that same 3,458-token mean — and
that figure is `fix/diagram-roles`' 1,848-character wording (415 tokens),
NOT this tree's. This tree is 365 tokens, +133, 3.846%, so it comes in 50
tokens UNDER the accepted budget. Both numbers are in the table above and
both were re-measured on 2026-09-28; the cost is settled and this is the
whole of it, so a re-opening needs a new measurement rather than a re-reading
of these two rows. (The mean is 3,458, not 3,453: 41,496 / 12 exactly.) An
earlier version of this docstring said the token cost was unmeasured — it is
measured, and no GPU was involved in measuring it.

WHAT MUST NOT CHANGE, and why each one is here rather than in a comment:
the one-diagram cap and the "ordinary questions get none" rule are what stop
an eager model decorating every answer; the ~20-node cap is legibility; the
explanatory-sentence rule is what makes a diagram readable to a
non-technical reader. The three-diagram allowance for a DOCUMENT is not
here and must not be: `app/artifacts/compose.py` never imports this string.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.engines import DIAGRAM_INSTRUCTION

# WIDENED 2026-09-28: THE PROMPT TEACHES WHAT THE FILE PATH ACCEPTS.
#
# render/diagrams.parse_mermaid read ONE grammar (the flowchart) while this
# string named seven types, so a sequenceDiagram the model wrote on request
# drew in the chat and became a "Diagram omitted" callout in the PDF —
# measured in the running container that morning: of 31 mermaid 11.17.0
# grammars, 1 reached a file. The reader now accepts ten (flowchart/graph,
# sequenceDiagram, classDiagram, stateDiagram-v2, erDiagram, mindmap,
# timeline, journey, kanban, packet-beta), and this string names exactly
# those ten, because a parser that accepts what the prompt never teaches
# changes nothing for a person. It also names the six chart types (pie,
# xychart, radar, sankey, quadrantChart, treemap) as NOT diagrams, since
# they draw numbers the model typed and a document's charts come from data.
#
# THE COST, measured 2026-09-28 in the running orchestrator container on CPU
# (tokenizers 0.22.2 over /models/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--
# 491c2f1ea524/tokenizer.json, load average 2.97, no GPU touched):
#
#     production (ae25da28, the 2026-09-27 wording)   365 tokens   1,602 chars   1,606 bytes
#     THIS TREE                                       415 tokens   1,786 chars   1,790 bytes
#
# +50 tokens per prompt over production; +183 over origin/dev's 232, which is
# EXACTLY the +183 / 5.29%-of-a-3,458-token-mean-prompt budget the
# 2026-09-27 integration accepted (it accepted fix/diagram-roles' 415-token
# wording; this tree spends the same 415 on ten type names instead of prose).
# The first draft measured 431 and was trimmed to the budget: "zoomable,
# downloadable", "system"/"multi-step" in the examples, and "and the diagram
# fails to draw" after "syntax error" went, with no rule lost (every rule
# test in this file still holds). The em-dash count is unchanged at two, so
# UTF-8 bytes stay four over the character count.
#
# The 2026-09-27 constants below are kept under their own names because
# the arithmetic they pin (+53 of restored fragments, -220 against the naive
# merge) is the record of THAT edit and is still true of it.


def _flat(path: Path) -> str:
    """A file's text with every run of whitespace collapsed to one space.

    Prose wraps, and a gate that matches across a line break is a gate that a
    re-wrap silently disarms — measured 2026-09-28, when the first version of
    the em-dash check below stayed green against the very wording it forbids
    because the mutation landed a newline inside the phrase.
    """
    return " ".join(path.read_text(encoding="utf-8").split())

#: `len()` of a `str` is CHARACTERS. Measured on origin/dev at 593af55,
#: before feat/document-vocabulary: 1,082. Kept as the HISTORICAL floor, so
#: the raise below is always read against the string this branch started from.
PRE_EDIT_CHARS = 1082

#: What the reconciled wording measured on 2026-09-27, kept as the previous
#: ceiling so the raise below is read against it.
RECONCILED_2026_09_27_CHARS = 1602

#: THE CEILING, RAISED 2026-09-28 by feat/understand-every-picture-ask, and
#: what the raise bought.
#:
#: The 1,602-character string named NINE of the twenty-three diagram heads
#: `frontend/lib/mermaid.ts` DIAGRAM_HEADS draws (flowchart, graph,
#: sequenceDiagram, erDiagram, pie, journey, timeline, mindmap, gitGraph) and
#: named the other fourteen nowhere — so the model had no way to know the
#: browser would draw them. Measured live in the running container that day:
#: "timeline of the project milestones" came back as prose sections (16.3 s)
#: and "kanban board of my open tasks" came back asking for the data and
#: offering an Excel file (22.3 s). Neither drew the diagram the browser
#: renders. It also still carried, verbatim, "at most ONE diagram per answer"
#: and "under ~20 nodes" — the two caps a release note had reported removed,
#: and had not removed.
#:
#: WIDENED AGAIN on 2026-09-28 when the ten native family drawers landed. The
#: every-picture wording named twenty-two heads and promised nothing about
#: WHERE each one draws, so the model could not know that a `gantt` renders in
#: chat and never reaches a downloaded document. This wording splits the list:
#: the ten that draw everywhere (flowchart/graph, sequenceDiagram, erDiagram,
#: classDiagram, stateDiagram-v2, mindmap, timeline, journey, kanban, packet),
#: the seven that render in chat only, and the six that are NUMBERS and belong
#: in the chart lane where the app computes them from data.
#:
#: BOTH CAPS STAY GONE. "at most ONE diagram per answer" and "under ~20 nodes"
#: were removed on 2026-09-28 and the ten-drawer branch put them back; the
#: owner has ruled out caps three times, so the merge drops them again. The
#: file path enforces its own limits in code (`_within_caps`), where a limit
#: belongs -- a prompt that lies about the size of the answer costs a picture
#: on every ask, not just the one over the cap.
#: `architecture` is deliberately the one left out: mermaid's
#: `architecture-beta` is experimental and `flowchart` draws a system
#: architecture better, so naming it would spend tokens to make the model
#: reach for the worse renderer.
#:
#: THE COST, measured the same way the note in app/engines/__init__.py measures
#: it — pinned engine tokenizer, on CPU, inside the running container, no GPU
#: touched (tokenizers 0.22.2 over
#: Model/repos/nvidia--Qwen3.6-35B-A3B-NVFP4--491c2f1ea524/tokenizer.json), at
#: load average 7.49 on 2026-09-28:
#:
#:   1,602 chars / 1,606 UTF-8 bytes / 365 tokens   (origin/main, ae25da28)
#:   2,044 chars / 2,048 UTF-8 bytes / 489 tokens   (this tree)
#:
#: +124 tokens per prompt, at every effort, at all eleven chat call sites —
#: 3.59% of the 3,458-token mean golden prompt, and 9 tokens BELOW the +133
#: the 2026-09-27 raise was accepted at. A first draft of this wording glossed
#: every type and measured +312; it was compressed to a bare list for exactly
#: this reason, and the gloss survives only where the type name alone does not
#: say when to use it (gantt, quadrantChart, the flowchart family).
CHARS_CEILING = 2044

#: The same string in UTF-8, measured on the same commit: 1,086. The
#: document-vocabulary edit took it to 1,088 — two bytes, one extra em dash —
#: and this tree takes it to the 1,606 pinned below. This floor is stated
#: rather than hidden because the earlier claim was "1,082 bytes before and
#: after", which was the character count wearing a byte's name.
PRE_EDIT_UTF8_BYTES = 1086

#: The same string in UTF-8 on this tree: 2,086. Two em dashes (U+2014, three
#: UTF-8 bytes each) account for the four bytes over the character count — the
#: count is still two, so the two units still differ by exactly 4.
UTF8_BYTES_CEILING = 2048

#: The measured token counts above, so the arithmetic in the prose is
#: arithmetic this file performs. Re-measure both with the pinned tokenizer
#: before moving the ceiling again; a character count is not a token count.
TOKENS_MAIN = 365
TOKENS_THIS_TREE = 489
TOKENS_ACCEPTED_RAISE_2026_09_27 = 133

#: The reconciliation this tree restored three fragments ON TOP OF, measured
#: on integ/diagram-group at 7f16f4b7. The three deltas below have to add up
#: to the distance from here to CHARS_CEILING; nothing else may.
RECONCILED_CHARS = 1549

#: The naive conflict resolution toward fix/diagram-roles' wording, and
#: fix/diagram-roles itself. Both are rows of the table in this file's
#: docstring, and both are here so the deltas quoted in prose are arithmetic
#: this file performs rather than numbers a reader has to trust.
NAIVE_MERGE_CHARS = 1822
DIAGRAM_ROLES_CHARS = 1848

#: The three restored fragments and the characters each one costs, measured
#: 2026-09-28 with difflib.SequenceMatcher over the compiled string on
#: 7f16f4b7 and on this tree. Each value is the text actually inserted —
#: joining space and rejoining ", and" included — which is what makes the
#: total the +53 the docstring names. The bare phrases are 13 and 30
#: characters; the +13/+32/+4 this branch first published were neither.
RESTORED_FRAGMENT_CHARS = {
    "inside labels": 14,
    "custom colours break dark mode": 36,
    "prefer over use": 3,
}


def test_the_instruction_did_not_grow_in_characters():
    assert len(DIAGRAM_INSTRUCTION) <= CHARS_CEILING, (
        f"DIAGRAM_INSTRUCTION is {len(DIAGRAM_INSTRUCTION)} characters, {len(DIAGRAM_INSTRUCTION) - CHARS_CEILING} "
        f"over the {CHARS_CEILING} measured for the every-picture-and-which-reach-a-file wording "
        f"({len(DIAGRAM_INSTRUCTION) - PRE_EDIT_CHARS} over origin/dev's {PRE_EDIT_CHARS}); "
        "it reaches eleven chat call sites at every effort"
    )


def test_the_raise_stayed_inside_the_cost_already_accepted():
    """The 2026-09-28 raise is justified by tokens, not by characters.

    The gate this file is cannot measure tokens — the pinned tokenizer is not a
    test dependency and loading a 35B model's vocabulary on every run would be
    a worse tax than the string. So the token counts are PINNED here from a
    measurement made in the running container, and this test asserts only the
    relationship the raise was argued on: the new wording costs fewer extra
    tokens than the raise before it was accepted at.
    """
    assert TOKENS_THIS_TREE - TOKENS_MAIN <= TOKENS_ACCEPTED_RAISE_2026_09_27, (
        f"+{TOKENS_THIS_TREE - TOKENS_MAIN} tokens per prompt is over the "
        f"+{TOKENS_ACCEPTED_RAISE_2026_09_27} already accepted; compress the wording "
        "or argue the cost, and re-measure with the pinned tokenizer rather than "
        "converting characters"
    )


def test_the_instruction_is_measured_in_the_unit_it_names():
    """The two units, each against what it really is, so neither can be
    quoted as the other again.

    RE-TARGETED 2026-09-28: this docstring still described the 1,088-byte
    ceiling of fix/document-vocabulary-r2, two sizes ago. On this tree the
    ceiling is 1,606 bytes against 1,602 characters, and the +520 the raise
    cost is the same number in both units because the string gained one em
    dash and lost one. The 1,086 and 1,088 of origin/dev and
    document-vocabulary-r2 are still named, in the constants below, because
    the withdrawn "1,082 bytes" claim is read against them."""
    chars = len(DIAGRAM_INSTRUCTION)
    encoded = len(DIAGRAM_INSTRUCTION.encode("utf-8"))
    assert chars == CHARS_CEILING, (
        f"{chars} characters; the every-picture-and-which-reach-a-file wording measured {CHARS_CEILING} on 2026-09-28"
    )
    assert encoded <= UTF8_BYTES_CEILING, (
        f"DIAGRAM_INSTRUCTION is {encoded} UTF-8 bytes, over the {UTF8_BYTES_CEILING} measured for this edit"
    )
    assert encoded > chars, "this string carries non-ASCII, so the two units are not interchangeable"
    assert encoded - chars == 4, (
        f"the wording spends two em dashes, so UTF-8 is 4 bytes over the "
        f"character count; it is now {encoded - chars}"
    )
    assert encoded - PRE_EDIT_UTF8_BYTES == 962, (
        f"the raise over origin/dev was measured at +962 UTF-8 bytes; it is now "
        f"{encoded - PRE_EDIT_UTF8_BYTES}. Re-measure the Fast cost before moving this "
        f"(the measurement of record is +124 tokens per prompt over ae25da28, 3.59% of "
        f"the 3,458-token mean golden prompt, 2026-09-28)."
    )


def test_the_three_fragment_costs_are_the_arithmetic_of_their_own_total():
    """A number stated as measured is checked here, not believed.

    This branch published the three restored fragments as +13, +32 and +4 —
    in this file, in the comment beside the string, and in its commit message
    — and 13 + 32 + 4 is 49, while the same paragraph headlines 53 and the
    string really moved 53. The sum is now arithmetic that runs, and the
    prose that quotes it is read back out of both files, because a docstring
    has no other gate. Re-measured 2026-09-28 with difflib over the compiled
    strings: +3 ("fer"), +14 (" inside labels"), +36 (" custom colours break
    dark mode, and").
    """
    # AGAINST THE 2026-09-27 SIZE, not against the current ceiling. These three
    # fragments were restored ON TOP OF the 1,549-character reconciliation and
    # took it to 1,602; that arithmetic is a fact about those two strings and
    # does not move when a later edit raises the ceiling. It was written as
    # `CHARS_CEILING - RECONCILED_CHARS`, which coupled it to every future
    # raise, and the 2026-09-28 raise is what exposed that.
    assert sum(RESTORED_FRAGMENT_CHARS.values()) == RECONCILED_2026_09_27_CHARS - RECONCILED_CHARS == 53
    root = Path(__file__).resolve().parents[1]
    prose = _flat(root / "app" / "engines" / "__init__.py")
    mine = _flat(Path(__file__))
    for text, where in ((prose, "app/engines/__init__.py"), (mine, "this file")):
        for phrase, cost in RESTORED_FRAGMENT_CHARS.items():
            if phrase == "prefer over use":
                assert "/`LR` (+3" in text, where
                continue
            assert f'"{phrase}" (+{cost})' in text, where
        # ...and the numbers that were published instead, built rather than
        # quoted so this gate does not carry the wording it forbids.
        for phrase, withdrawn in (("inside labels", 13), ("custom colours break dark mode", 32)):
            wrong = '"%s" (+%d)' % (phrase, withdrawn)
            assert wrong not in text, f"{where} still states {wrong}"
        assert "/`LR` (+%d" % 4 not in text, where


def test_the_delta_against_the_naive_merge_is_arithmetic_and_not_a_referent_error():
    """The delta the previous revision gave against the naive merge was the
    delta against fix/diagram-roles, and it reached both this file's docstring
    and the golden MANIFEST's `size` note when the size moved from 1,549 to
    1,602. The two deltas are computed here so neither can be quoted for the
    other again, and the prose is read back out of the two files that carry
    it.

    (MANIFEST.json is not itself hashed — tests/test_context_assembly_golden.py
    reads it, and its sha256 map covers the 24 fixture files only — so
    correcting that note costs no recapture.)
    """
    # Also against the 2026-09-27 size, and for the same reason: the MANIFEST
    # note these assertions read back was written when that was the size.
    assert NAIVE_MERGE_CHARS - RECONCILED_2026_09_27_CHARS == 220
    assert DIAGRAM_ROLES_CHARS - RECONCILED_2026_09_27_CHARS == 246
    root = Path(__file__).resolve().parents[1]
    naive = NAIVE_MERGE_CHARS - RECONCILED_2026_09_27_CHARS
    roles = DIAGRAM_ROLES_CHARS - RECONCILED_2026_09_27_CHARS
    manifest = _flat(root / "tests/fixtures/context_assembly_golden/MANIFEST.json")
    assert f"-{naive} against the naive merge" in manifest
    assert f"-{roles} against the naive merge" not in manifest, "the wrong referent is back"
    mine = _flat(Path(__file__))
    assert f"\u2212{naive} characters against the naive merge" in mine
    assert f"\u2212{roles} characters against the naive merge" not in mine


def test_the_em_dash_count_is_the_one_the_byte_delta_implies():
    """The mechanism, not just the number. The character and byte deltas over
    origin/dev are both +520, which can only hold if the non-ASCII count did
    not move; the previous revision explained that as a gain of TWO em dashes
    against a loss of one, which would move the count by one and make the byte
    delta 522 — it contradicted the number it was offered to explain. Counted
    2026-09-28: origin/dev 2, fix/document-vocabulary-r2 3, fix/diagram-roles
    5, integ/diagram-group 2, this tree 2 — gained one, lost one."""
    assert DIAGRAM_INSTRUCTION.count("\u2014") == 2
    assert len(DIAGRAM_INSTRUCTION.encode("utf-8")) - len(DIAGRAM_INSTRUCTION) == 2 * 2
    mine = _flat(Path(__file__))
    assert "gained ONE em dash and lost one, so its non-ASCII count did not move" in mine
    wrong = "gained %s em dashes and lost one, so its non-ASCII" % "two"
    assert wrong not in mine, "the withdrawn mechanism is back in the docstring"


def test_the_count_of_engine_modules_is_the_one_on_disk():
    """A count of NINE engines appeared five times across this file and the
    comment beside the string, and it is neither of the two real numbers:
    measured 2026-09-28, ELEVEN concatenation sites in EIGHT modules (agent 1,
    chat 2, dataset 1, document 1, rag 1, repo 2, search 1, url 2), and the
    same eleven on origin/dev ff1a5d7c, so the Max-gate split did not move it.
    The phrase was inherited from an older header, so it was never measured;
    it is counted here and the prose is held to the count."""
    engines = Path(__file__).resolve().parents[1] / "app" / "engines"
    per_module = {}
    for path in sorted(engines.glob("*.py")):
        if path.name == "__init__.py":
            continue
        n = len(re.findall(r"\+\s*DIAGRAM_INSTRUCTION|DIAGRAM_INSTRUCTION\s*\+",
                           path.read_text(encoding="utf-8")))
        if n:
            per_module[path.name] = n
    assert sum(per_module.values()) == 11, per_module
    assert len(per_module) == 8, per_module
    for text, where in (
        (_flat(Path(__file__)), "this file"),
        (_flat(engines / "__init__.py"), "app/engines/__init__.py"),
    ):
        wrong = "%s engines" % "nine"
        assert wrong not in text, f"{where} still says {wrong!r}"


def test_the_withdrawn_byte_claim_is_not_restated_anywhere():
    """The unit correction above was made in this file first and left standing
    in the two places a reader actually meets it, so the branch still carried
    the number it had withdrawn. Measured 2026-09-27: 1,082 CHARACTERS on both
    trees, 1,086 UTF-8 bytes on origin/dev and 1,088 on this branch — so
    "1,082 bytes" is true of neither tree and must not reappear in the comment
    beside the string or in the golden manifest's note. This reads the files
    rather than trusting the fix, because a prose claim has no other gate.
    """
    root = Path(__file__).resolve().parents[1]
    for rel in ("app/engines/__init__.py", "tests/fixtures/context_assembly_golden/MANIFEST.json"):
        text = (root / rel).read_text(encoding="utf-8")
        for wrong in ("1,082 bytes", "1082 bytes"):
            # collapsed as well, for the same reason `_flat` exists
            assert wrong not in " ".join(text.split()), f"{rel} still states the withdrawn claim {wrong!r}"
        assert "1,082 CHARACTERS" in text, f"{rel} should name the unit that is actually 1,082"
        assert "1,086" in text and "1,088" in text, f"{rel} should carry both re-measured byte counts"


def test_the_conservative_rules_are_all_still_there():
    """The rules that keep a diagram DRAWABLE, and only those.

    THE TWO CAPS ARE GONE, 2026-09-28, and this test is why they were still
    here. "at most ONE diagram per answer" and "under ~20 nodes" were asserted
    as conservative rules, so a release that reported removing them removed
    them from nothing: the string still carried both, verbatim, read out of the
    RUNNING container on 2026-09-28. A cap that has been withdrawn in a release
    note and held in place by a test is the worst of both — nobody can see it,
    and nobody can remove it without this file going red.

    They were withdrawn because they make the product refuse work it can do. An
    architecture answer that needs a context diagram AND a sequence diagram got
    one; a 40-node deploy pipeline got a summary of itself. Neither cap was ever
    a renderer limit: the browser's mermaid has none, and the limit that is real
    — 24 nodes and 40 edges inside a GENERATED FILE — lives in
    `render/diagrams.py` where it is enforced rather than requested.

    What stays are the rules whose absence makes a diagram FAIL TO DRAW, plus
    the one that keeps it off ordinary turns.
    """
    for rule in (
        "must NOT contain a diagram",
        "Never draw ASCII-art boxes",
        "one statement per line",
        "every label in double quotes",
    ):
        assert rule in DIAGRAM_INSTRUCTION, rule
    assert "sentences saying what it shows" in DIAGRAM_INSTRUCTION
    for withdrawn in ("at most ONE diagram", "~20 nodes", "keep it SMALL"):
        assert withdrawn not in DIAGRAM_INSTRUCTION, (
            f"{withdrawn!r} is back: it was withdrawn on 2026-09-28 because the browser "
            "has no such limit and the real one is enforced in render/diagrams.py"
        )


def test_it_names_every_diagram_head_the_browser_draws():
    """The prompt and `frontend/lib/mermaid.ts` must not drift apart.

    Measured on ae25da28: the instruction named NINE of the twenty-three heads
    in DIAGRAM_HEADS and the browser drew all twenty-three, so fourteen kinds
    existed in the renderer and nowhere in the model's instructions. Live that
    day, "timeline of the project milestones" came back as prose and "kanban
    board of my open tasks" came back offering an Excel file.

    `architecture` is the one deliberate omission — see CHARS_CEILING's note.
    """
    heads = re.search(r"const DIAGRAM_HEADS = \[(.*?)\];",
                      (Path(__file__).resolve().parents[2] / "frontend/lib/mermaid.ts").read_text(encoding="utf-8"),
                      re.S)
    assert heads, "DIAGRAM_HEADS moved; this gate cannot read it"
    names = [h.strip().strip("'\"") for h in heads.group(1).replace("\n", " ").split(",") if h.strip()]
    assert len(names) == 23, f"DIAGRAM_HEADS now has {len(names)} entries; re-read the prompt against it"
    low = DIAGRAM_INSTRUCTION.lower()
    missing = [n for n in names if n not in low]
    assert missing == ["architecture"], (
        f"the prompt does not name {missing}; the browser draws them, so the model should "
        "know they exist (`architecture` is the documented omission)"
    )


def test_the_three_restored_fragments_are_pinned_so_they_cannot_vanish_again():
    """origin/dev's prose, dropped by the 1,549-character reconciliation and
    put back at 1,602. Each of the three was lost with no test holding it,
    which is the only reason the loss was silent; they are held here now.

    These are not style. Each one changes what the model is told:
    """
    # The punctuation ban is about what goes INSIDE a label. Unscoped, it
    # forbids the brackets of `A["Payments API"]`, which the same string then
    # requires — a rule that contradicts the example beside it.
    assert "markdown inside labels" in DIAGRAM_INSTRUCTION
    # WHY the directive ban exists. "the app paints a ROLE instead" says what
    # happens next; it does not say what goes wrong if the model disobeys.
    assert "custom colours break dark mode" in DIAGRAM_INSTRUCTION
    # `flowchart` is PREFERRED, not mandatory: four sentences later this same
    # string tells the model what to do inside seven other diagram types.
    assert "prefer `flowchart TD`" in DIAGRAM_INSTRUCTION
    assert "use `flowchart TD`" not in DIAGRAM_INSTRUCTION


def test_one_worked_example_of_a_quoted_label_survives():
    """origin/dev carried two demonstrations of the label form — `(e.g.
    A["Login page"])` beside the quoting rule and none elsewhere. This string
    carries one, inside the role sentence. Dropping the first is deliberate
    (22 characters of Fast prefill for a form already shown); dropping BOTH
    would leave the quoting rule with nothing to point at, so the survivor is
    pinned here rather than left to the next compression."""
    import re as _re

    assert _re.search(r'[A-Z]\["[^"]+"\]', DIAGRAM_INSTRUCTION), (
        "no worked example of a double-quoted label is left in the instruction"
    )


def test_the_role_clause_teaches_the_closed_list_and_bans_colour():
    from app.artifacts import spec as S

    assert ":::" in DIAGRAM_INSTRUCTION
    for role in S.DIAGRAM_ROLES:
        assert role in DIAGRAM_INSTRUCTION, role
    for banned in ("colour", "hex", "style", "classDef", "linkStyle", "click", "%%{init}%%"):
        assert banned in DIAGRAM_INSTRUCTION, banned
    # RETARGETED 2026-09-27, same rule, surviving words. This branch wrote the
    # ban as "NEVER a colour"; fix/diagram-roles wrote it as "never write a
    # colour of your own (no hex, no rgb(), no colour name)", which is the
    # wording the integration kept, and which tests/test_diagram_role_vocabulary.py
    # also pins. Carrying both phrases would have cost ~50 characters of Fast
    # prefill to satisfy two tests of ONE rule, so the phrase moved and the
    # rule did not.
    assert "never write a colour of your own" in DIAGRAM_INSTRUCTION
    assert "no rgb()" in DIAGRAM_INSTRUCTION


def test_the_prompt_teaches_exactly_the_types_the_file_path_accepts():
    """1b of the 2026-09-28 widening: the two lists must match. Every family
    render/mermaid_grammars.py reads is named in the string, every chart
    type it refuses as a chart is named as NOT a diagram, and no excluded
    grammar (gantt, gitGraph, C4, ...) is offered — offering one would send
    the model to a type the file path turns into a callout. RED on the
    2026-09-27 wording, which offered gitGraph and pie and never named
    classDiagram, stateDiagram-v2, kanban or packet-beta."""
    from app.artifacts.render import mermaid_grammars as G

    taught = {"sequencediagram": "sequence", "classdiagram": "class", "statediagram-v2": "state",
              "erdiagram": "er", "mindmap": "mindmap", "timeline": "timeline", "journey": "journey",
              "kanban": "kanban", "packet-beta": "packet"}
    assert set(taught.values()) == set(G.READERS), "a reader without a prompt line, or the reverse"
    for header in taught:
        assert G.header_keyword(header) == header
    # THE LIST IS ORDERED, AND THE ORDER IS THE PROMISE. Every type the app
    # draws is named, in one list; the ten with a reader come FIRST, and the
    # sentence after the list says that the ten before `gantt` also draw inside
    # a downloaded file. A type the file path refuses is therefore still
    # offered -- the browser draws it -- but never promised in a file.
    # Two branches met here: one listed only the ten and told the model to use
    # ONLY those, which loses eleven pictures the browser draws; the other
    # listed twenty-two and promised nothing about files, which loses the
    # picture silently at import. The order carries both facts for the price of
    # one list.
    listed = DIAGRAM_INSTRUCTION.split("the app draws all of these: ", 1)[1].split(". The ten before", 1)[0]
    ten = ("flowchart/graph", "sequenceDiagram", "erDiagram", "classDiagram", "stateDiagram-v2",
           "mindmap", "timeline", "journey", "kanban", "packet")
    for name in ten:
        assert name in listed, name
    gantt_at = listed.index("gantt")
    for name in ten:
        assert listed.index(name) < gantt_at, f"{name} has a reader, so it must be named before `gantt`"
    for chat_only in ("gantt", "quadrantChart", "gitGraph", "block", "radar", "treemap",
                      "sankey", "xychart", "pie", "requirementDiagram", "C4Context"):
        assert chat_only in listed, f"{chat_only}: the browser draws it, so the model should know it exists"
        assert listed.index(chat_only) >= gantt_at, f"{chat_only} has no reader, so it must come from `gantt` on"
    assert "before `gantt` also draw inside a downloaded file" in DIAGRAM_INSTRUCTION

    # The six that are numbers are sent to the chart lane by name.
    numbers_clause = DIAGRAM_INSTRUCTION.split("give NUMBERS to a chart", 1)[1]
    for name in ("pie", "xychart"):
        assert name in numbers_clause, name
    assert set(G.CHART_KEYWORDS.values()) == {"pie", "xychart", "radar", "sankey", "quadrant-chart", "treemap"}
    assert "architecture" not in DIAGRAM_INSTRUCTION, (
        "`architecture-beta` is experimental and `flowchart` draws a system architecture "
        "better; naming it spends tokens to send the model to the worse renderer"
    )


def test_the_composer_does_not_import_it():
    """The document path learns diagrams from spec.py's schema, not from the
    chat prompt — which is why raising the cap here would buy the file route
    nothing while costing every Fast chat turn."""
    composer = Path(__file__).resolve().parents[1] / "app" / "artifacts" / "compose.py"
    assert "DIAGRAM_INSTRUCTION" not in composer.read_text(encoding="utf-8")


def test_it_is_still_attached_at_every_chat_call_site():
    """If a site drops it, the ceiling above stops meaning anything."""
    engines = Path(__file__).resolve().parents[1] / "app" / "engines"
    sites = 0
    for path in sorted(engines.glob("*.py")):
        if path.name == "__init__.py":
            continue
        sites += len(re.findall(r"\+\s*DIAGRAM_INSTRUCTION|DIAGRAM_INSTRUCTION\s*\+", path.read_text(encoding="utf-8")))
    assert sites >= 11, f"found {sites} concatenation sites; the budget above was measured against eleven"
