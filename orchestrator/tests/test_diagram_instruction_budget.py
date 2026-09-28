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

#: What the reconciled wording actually measures, imported on this tree on
#: 2026-09-27. It is the CEILING: every rule from both branches, plus the
#: three restored origin/dev fragments above, is in the string at this size,
#: so anything larger is a rule nobody has argued for.
CHARS_CEILING = 1602

#: The same string in UTF-8, measured on the same commit: 1,086. The
#: document-vocabulary edit took it to 1,088 — two bytes, one extra em dash —
#: and this tree takes it to the 1,606 pinned below. This floor is stated
#: rather than hidden because the earlier claim was "1,082 bytes before and
#: after", which was the character count wearing a byte's name.
PRE_EDIT_UTF8_BYTES = 1086

#: The same string in UTF-8 on this tree: 1,606. Two em dashes (U+2014, three
#: UTF-8 bytes each) account for the four bytes over the character count.
UTF8_BYTES_CEILING = 1606

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
        f"over the {CHARS_CEILING} measured for the reconciled wording "
        f"({len(DIAGRAM_INSTRUCTION) - PRE_EDIT_CHARS} over origin/dev's {PRE_EDIT_CHARS}); "
        "it reaches eleven chat call sites at every effort"
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
        f"{chars} characters; the reconciled wording measured {CHARS_CEILING} on 2026-09-27"
    )
    assert encoded <= UTF8_BYTES_CEILING, (
        f"DIAGRAM_INSTRUCTION is {encoded} UTF-8 bytes, over the {UTF8_BYTES_CEILING} measured for this edit"
    )
    assert encoded > chars, "this string carries non-ASCII, so the two units are not interchangeable"
    assert encoded - chars == 4, (
        f"the reconciled wording spends two em dashes, so UTF-8 is 4 bytes over the "
        f"character count; it is now {encoded - chars}"
    )
    assert encoded - PRE_EDIT_UTF8_BYTES == 520, (
        f"the raise over origin/dev was measured at +520 UTF-8 bytes; it is now "
        f"{encoded - PRE_EDIT_UTF8_BYTES}. Re-measure the Fast cost before moving this "
        f"(the measurement of record is +133 tokens per prompt, 3.85% of the 3,458-token "
        f"mean golden prompt, 2026-09-27)."
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
    assert sum(RESTORED_FRAGMENT_CHARS.values()) == CHARS_CEILING - RECONCILED_CHARS == 53
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
    assert NAIVE_MERGE_CHARS - CHARS_CEILING == 220
    assert DIAGRAM_ROLES_CHARS - CHARS_CEILING == 246
    root = Path(__file__).resolve().parents[1]
    naive, roles = NAIVE_MERGE_CHARS - CHARS_CEILING, DIAGRAM_ROLES_CHARS - CHARS_CEILING
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
    for rule in (
        "at most ONE diagram per answer",
        "must NOT contain a diagram",
        "under ~20 nodes",
        "Never draw ASCII-art boxes",
        "one statement per line",
        "every label in double quotes",
    ):
        assert rule in DIAGRAM_INSTRUCTION, rule
    assert "sentences saying what it shows" in DIAGRAM_INSTRUCTION


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
