"""The upgrade programme's evaluation set (task B-02): 16 synthetic cases.

MASTER_PROMPT §11 names nine situations every release must handle and §13
seven request-understanding traps. Each one is a case here, with synthetic
inputs only (fixtures/evalset/, the texts below) and DETERMINISTIC checks:
harness.check_turn's keys plus answer_checks.py's. Nothing here calls a model
or a service; running the set against a stack is B-04's job.

These cases are deliberately NOT in cases.CASES: that list is the frozen,
gated QA suite (its baseline re-scores in CI and gate.py derives a row for
every category in it), and this set uses Think and Max, which that suite
asserts it never does.

A case is the aiq case shape (cases.case) plus:

  effort        the mode: "fast", "think" or "max" (the /chat `effort` field)
  history       synthetic earlier turns, sent as `messages` before the first
                turn and never generated: [{"role", "content"}, ...]
  attachments   synthetic files to upload first: [{"fixture", "purpose"}];
                `fixture` is relative to fixtures/, purpose "document" goes to
                /uploads and the turn carries it in `pdf_uploads`
  web_search    "off" | "auto" (the browser's default) | "on"
  deep_research True only for the Max research case (the explicit toggle)
  section       "11" or "13", the MASTER_PROMPT list the case answers
  kind          what the case stands for in that list

Scoring a finished turn: harness.check_turn(turn["expect"], res, effort=...,
fail_text=FAIL_TEXT, prev_file=None, upload_path=None). A `code` expect needs
res["code_result"] from code_sandbox.run_code first, exactly as run.py does.
eval_set_answers.py holds a hand-written good answer per case and bad answers
that each check must reject; tests/test_eval_set.py proves both.
"""
from __future__ import annotations

import os
import random
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
EVALSET = os.path.join(FIXTURES, "evalset")

#: cases.FAIL_TEXT without three phrases a CORRECT answer here may use: a
#: document-only answer says a figure "is not available" in the document, and
#: an explain-only answer says "I did not change" the code.
FAIL_TEXT = ["couldn't finish", "could not finish", "could not be made", "no charts to draw"]

EFFORTS = ("fast", "think", "max")


def fixture_text(name: str) -> str:
    with open(os.path.join(EVALSET, name), encoding="utf-8") as fh:
        return fh.read()


def eval_case(cid: str, category: str, section: str, kind: str, turns: List[dict], *, effort: str = "fast",
              history: Optional[List[dict]] = None, attachments: Optional[List[dict]] = None,
              web_search: str = "off", deep_research: bool = False, note: str = "") -> dict:
    return {"id": cid, "category": category, "section": section, "kind": kind, "effort": effort,
            "upload": None, "history": list(history or []), "attachments": list(attachments or []),
            "web_search": web_search, "deep_research": deep_research, "turns": turns, "note": note}


def turn(message: str, **expect) -> dict:
    return {"message": message, "expect": expect}


def _pairs(*exchanges: str) -> List[dict]:
    """user, assistant, user, assistant ... -> message dicts."""
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": text} for i, text in enumerate(exchanges)]


# Patterns shared by several cases.
NO_CUTOFF_TALK = [r"knowledge cut-?off", r"as of my (?:last|latest) (?:update|training)",
                  r"i (?:can(?:no|')t|do not|don't) (?:browse|access the internet|search the web)"]
DATE_PATTERNS = [
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b",
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\b",
    r"\b\d{4}-\d{2}-\d{2}\b", r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
]

# ================================================================== §11 ==

REWRITE_SOURCE = (
    "So basically the team had a meeting about the roadmap and after a lot of back and forth discussion we kind of "
    "decided that the mobile app launch, which was originally supposed to happen in Q2, is going to move to Q3, "
    "because the payments integration is not ready yet and also two of the four engineers on the project are going "
    "to be on leave in May, so realistically speaking Q3 is the earliest we can do it, and the budget stays the same "
    "at 120,000 dollars.")

HALYARD = "evalset/halyard-h2-spec.md"
TRAVEL = "evalset/travel-policy.md"

#: EV07's early decisions. The supplier log pushes them far back: about
#: 210,000 characters (roughly 52,000 tokens at four characters a token),
#: past the 40,000-token compaction threshold (DISCOVERY.md §5, 3.4), so the
#: budget change and the catering rules sit outside any short recent window.
SUPPLIER_LOG_LINES = 2000


def supplier_log(lines: int = SUPPLIER_LOG_LINES, seed: int = 20261003) -> str:
    """A long, dull, deterministic paste that carries none of the facts asked for."""
    rng = random.Random(seed)
    items = ["folding chairs", "projector rental", "name badges", "shuttle minibus", "coffee service",
             "stage lighting", "whiteboards", "extension cables", "photo backdrop", "printed agendas",
             "lanyards", "water dispensers", "flip charts", "speaker hire", "tablecloths"]
    out = []
    for i in range(1, lines + 1):
        out.append(f"Quote {i:04d}: {rng.choice(items)}, {rng.randint(2, 400)} units, "
                   f"{rng.randint(80, 9000)} euro in total, valid for {rng.choice([14, 30, 45, 60])} days, "
                   f"supplier reference S{rng.randint(100, 999)}-{rng.randint(10, 99)}.")
    return "\n".join(out)


_EV07_DECISIONS = [
    "we're planning the Bluefin team offsite. 35 people, budget EUR 40,000, in Porto, second week of May. "
    "can you help me keep track of decisions as we go?",
    "Happy to. Recorded so far: Bluefin offsite, 35 people, budget EUR 40,000, Porto, second week of May.",
    "venue options we got: Casa Ribeira (holds 40, EUR 9,500 for the two days), Armazem 12 (holds 60, EUR 14,000), "
    "Quinta do Vale (holds 30, EUR 7,000).",
    "Noted the three venues. Quinta do Vale holds 30, which is fewer than your 35 people.",
    "right, drop Quinta do Vale. also finance approved more money: the budget is now EUR 48,000.",
    "Updated: the budget is now EUR 48,000 (it was EUR 40,000). Quinta do Vale is out; Casa Ribeira and "
    "Armazem 12 remain.",
    "catering rules: two vegetarian menus and one vegan menu, and no alcohol before 6 pm.",
    "Recorded the catering rules: two vegetarian menus, one vegan menu, and no alcohol before 6 pm.",
]
_EV07_LATER = [
    "Saved the supplier quote log for reference. Nothing in it changes the venue, budget or catering decisions.",
    "travel: most people fly in from Berlin, 6 from Madrid. flights should come to about EUR 9,800 in total.",
    "Noted: flights about EUR 9,800 in total.",
    "activities: a half-day boat trip on the Douro on day two.",
    "Added the half-day Douro boat trip on day two.",
    "agenda wise, day one starts at 9:30 with a strategy session",
    "Day one starts at 9:30 with the strategy session.",
    "let's go with Casa Ribeira.",
    "Confirmed: Casa Ribeira, which holds 40 and costs EUR 9,500. After the venue and the flights, EUR 28,700 of the "
    "EUR 48,000 budget remains.",
]
EV07_PASTE_INTRO = "here's the supplier quote log from procurement, just keep it for reference:\n\n"
EV07_HISTORY = _pairs(*_EV07_DECISIONS, EV07_PASTE_INTRO + supplier_log(), *_EV07_LATER)
EV07_QUESTION = ("ok remind me: whats the final budget, which venue did we pick and how many ppl does it hold, "
                 "and what were the catering rules again?")
#: The planning turns without the paste: every number the answer gives must come from them.
EV07_FACTS = "\n".join(_EV07_DECISIONS + [EV07_PASTE_INTRO] + _EV07_LATER + [EV07_QUESTION])

EV06_CHECK = '''\
import re
import subprocess
import sys

from inventory import Inventory

inv = Inventory()
inv.add("bolt", 10, 0.25)
inv.add("nut", 4, 0.10)
assert inv.quantity("bolt") == 10, inv.quantity("bolt")
assert inv.quantity("washer") == 0, "an unknown SKU has quantity 0"
assert abs(inv.total_value() - 2.9) < 1e-9, inv.total_value()

inv.add("bolt", 5, 0.30)
assert inv.quantity("bolt") == 15, "adding an existing SKU adds to its quantity"
assert abs(inv.total_value() - (15 * 0.30 + 4 * 0.10)) < 1e-9, "the latest unit price replaces the old one"

inv.remove("bolt", 3)
assert inv.quantity("bolt") == 12

for sku, qty in (("nut", 5), ("washer", 1)):
    try:
        inv.remove(sku, qty)
    except ValueError:
        pass
    else:
        raise AssertionError(f"remove({sku!r}, {qty}) must raise ValueError")
assert inv.quantity("nut") == 4, "a refused removal leaves the stock unchanged"

for bad in (0, -2):
    try:
        inv.add("bolt", bad, 1.0)
    except ValueError:
        continue
    raise AssertionError(f"add with quantity {bad} must raise ValueError")

# the answer's own tests must run and pass
r = subprocess.run([sys.executable, "-m", "unittest", "-v", "test_inventory"], capture_output=True, text=True,
                   timeout=60)
assert r.returncode == 0, "test_inventory.py failed: " + r.stderr[-600:]
ran = re.search(r"Ran (\\d+) tests?", r.stderr)
assert ran and int(ran.group(1)) >= 3, "test_inventory.py must hold at least three tests: " + r.stderr[-300:]
print("ok")
'''

RQ05_CHECK = '''\
import re
import subprocess
import sys

with open("sample.txt", "w", encoding="utf-8") as fh:
    fh.write("Lantern river lantern. River, stone; LANTERN! a river? An owl.\\n")


def run(*args):
    return subprocess.run([sys.executable, "wordfreq.py", *args], capture_output=True, text=True, timeout=30)


def pairs(out):
    rows = []
    for line in out.strip().splitlines():
        m = re.match(r"^\\s*([a-z]+)\\W+(\\d+)\\s*$", line.strip().lower())
        assert m, f"not a 'word count' line: {line!r}"
        rows.append((m.group(1), int(m.group(2))))
    return rows


r = run("sample.txt", "--top", "2")
assert r.returncode == 0, r.stderr[-400:]
assert pairs(r.stdout) == [("lantern", 3), ("river", 3)], r.stdout

r = run("sample.txt")
assert r.returncode == 0, r.stderr[-400:]
assert pairs(r.stdout) == [("lantern", 3), ("river", 3), ("a", 1), ("an", 1), ("owl", 1), ("stone", 1)], \\
    "default --top 10, ties A to Z: " + r.stdout

r = run("sample.txt", "--top", "3", "--min-length", "4")
assert r.returncode == 0, r.stderr[-400:]
assert pairs(r.stdout) == [("lantern", 3), ("river", 3), ("stone", 1)], r.stdout

r = run("no-such-file.txt")
assert r.returncode != 0, "a missing file must exit non-zero"
assert "Traceback" not in r.stderr, "a missing file must give a one-line error, not a traceback"
print("ok")
'''

SECTION_11 = [
    eval_case("EV01", "greeting", "11", "a greeting", [turn(
        "hey there!",
        artifact=False, max_chars=400, max_headings=0, max_code_blocks=0, max_tables=0, no_citations=True,
        sources_used={"max": 0}, regex_all=[r"\b(?:hi|hello|hey|good (?:morning|afternoon|evening))\b"])],
        note="the Fast small-talk lane: short, no search, no structure"),

    eval_case("EV02", "rewrite", "11", "rewriting supplied text", [turn(
        "Rewrite this paragraph so it is clear and concise. Keep every fact.\n\n" + REWRITE_SOURCE,
        artifact=False, max_chars=len(REWRITE_SOURCE), max_headings=0, max_tables=0,
        must_contain=[["q2"], ["q3"], ["payment"], ["120,000", "120000", "120k", "120 000"]],
        regex_all=[r"\b(?:two|2)\b", r"\b(?:four|4)\b", r"\b(?:in|during|through|over) may\b"],
        numbers_grounded={"source": REWRITE_SOURCE})],
        note="keeps every fact, adds none, and comes back shorter than it went in"),

    eval_case("EV03", "multi_part", "11", "an informal multi-part request", [turn(
        "yo quick qs!! 1) whats 15% of 240 2) convert 72F to celsius (1 decimal pls) 3) gimme 3 synonyms for "
        "'happy' thx",
        artifact=False, max_chars=900, regex_all=[r"\b36(?:\.0+)?(?!\.?\d)", r"\b22\.2(?!\d)"],
        min_matches={"options": ["joyful", "cheerful", "content", "contented", "glad", "delighted", "pleased",
                                 "elated", "jubilant", "merry", "ecstatic", "upbeat", "jolly", "thrilled",
                                 "overjoyed", "chipper", "blissful", "sunny", "chirpy", "buoyant", "gleeful"],
                     "min": 3},
        order=[["36"], ["22.2"]])],
        note="three asks in one informal message; each answered, in order"),

    eval_case("EV04", "fresh_fact", "11", "a factual question that needs fresh evidence", [turn(
        "What's the latest stable version of Python right now, and when was it released?",
        artifact=False, max_chars=1500, sources_used={"min": 1},
        citations={"min_distinct": 1, "support": [[{"regex": r"\b3\.\d{1,2}(?:\.\d{1,2})?\b"}]]},
        regex_all=[r"\b3\.\d{1,2}(?:\.\d{1,2})?\b"], regex_none=NO_CUTOFF_TALK)],
        web_search="auto",
        note="the browser default (web_search auto): the answer must rest on a page read in this run"),

    eval_case("EV05", "document_qa", "11", "a question about an uploaded document", [turn(
        "Using the attached spec sheet: how long does the battery last at the 5-minute reporting interval, what's "
        "the operating temperature range, and which radio does it use?",
        artifact=False, max_chars=1200,
        must_contain=[["14 months"], ["-20", "\u221220", "minus 20"], ["60"], ["lorawan", "lora"]],
        numbers_grounded={"source": fixture_text("halyard-h2-spec.md")})],
        attachments=[{"fixture": HALYARD, "purpose": "document"}],
        note="the operating range (-20 to 60) is not the measuring range (-10 to 50); every number from the sheet"),

    eval_case("EV06", "complete_files", "11", "a coding task that needs complete files", [turn(
        "Write a small Python inventory module as two complete files.\n\n"
        "`inventory.py`: a class `Inventory` with `add(sku, qty, unit_price)`, `remove(sku, qty)`, "
        "`quantity(sku)` and `total_value()`. Adding an existing SKU adds to its quantity and replaces its unit "
        "price with the new one. `quantity` of an unknown SKU is 0. `add` with a quantity of 0 or less raises "
        "ValueError. Removing an unknown SKU, or more than is in stock, raises ValueError and leaves the stock "
        "unchanged.\n\n"
        "`test_inventory.py`: unittest tests for that behaviour, at least three test methods.\n\n"
        "Standard library only. Put each file in its own fenced code block with the file name on the line above it.",
        artifact=False, min_code_blocks=2,
        complete_code={"lang": "python", "files": ["inventory.py", "test_inventory.py"],
                       "symbols": {"inventory.py": ["Inventory", "add", "remove", "quantity", "total_value"]}},
        code={"lang": "python", "layout": "files", "answer_files": ["inventory.py", "test_inventory.py"],
              "files": {"check.py": EV06_CHECK}})],
        effort="think",
        note="DISCOVERY 5.1: Think/Max build requests go to the agent route, whose synthesis is one call"),

    eval_case("EV07", "long_followup", "11", "a long-conversation follow-up", [turn(
        EV07_QUESTION,
        artifact=False, max_chars=1500,
        must_contain=[["48,000", "48000", "48 000", "48k"], ["casa ribeira"], ["40"], ["vegan"], ["vegetarian"],
                      ["6 pm", "6pm", "6 p.m.", "18:00"]],
        regex_none=[r"final budget\W+(?:is\W+)?(?:eur\s*|\u20ac\s*)?40[,. ]?000"],
        numbers_grounded={"source": EV07_FACTS, "allow": ["18", "0"]})],
        history=EV07_HISTORY,
        note="the decisions sit before a ~52K-token paste; the revised budget (48,000) must win over the first one"),

    eval_case("EV08", "think_task", "11", "a moderately complex Think task", [turn(
        "Capacity question. At peak we get two request types: 900 chat requests per minute that each hold a GPU "
        "slot for 0.4 s, and 300 summarisation requests per minute that each hold a slot for 2.0 s. One GPU serves "
        "8 requests at once, and we never want a GPU more than 70% busy. How many GPUs do we need at peak, and how "
        "many if we also keep one spare for failover? Use the headings Working, Answer and Risks, and keep Risks to "
        "two bullet points.",
        artifact=False, required_sections=[["working"], ["answer"], ["risks"]], section_bullets={"risks": [2, 2]},
        regex_all=[r"\b16(?!\.?\d)", r"\b5\.6(?!\d)", r"(?:\b3\b|\bthree\b)\W{0,4}gpus?\b", r"(?:\b4\b|\bfour\b)\W{0,4}gpus?\b"])],
        effort="think",
        note="15/s x 0.4 s + 5/s x 2.0 s = 16 busy slots; 8 x 0.7 = 5.6 per GPU; ceil(2.86) = 3, plus a spare = 4"),

    eval_case("EV09", "max_research", "11", "a deep Max research task", [turn(
        "Write a research report comparing LFP and NMC lithium-ion batteries for stationary grid storage: energy "
        "density, cycle life, safety, and how pack prices have moved over the last three years. Use current "
        "sources and cite every figure. Use the headings Summary, Energy density, Cycle life, Safety, Cost trend "
        "and Sources.",
        artifact=False, min_chars=2500, no_runon=True,
        required_sections=[["summary"], ["energy density"], ["cycle life"], ["safety"], ["cost"],
                           ["sources", "references"]],
        sources_used={"min": 5},
        citations={"min_distinct": 5, "passage_support": True,
                   "support": [[{"regex": r"wh/kg"}], [{"regex": r"\bcycles\b"}], [{"regex": r"/\s?kwh|per kwh"}]]},
        regex_all=[r"wh/kg", r"\bcycles?\b", r"/\s?kwh|per kwh"], regex_none=NO_CUTOFF_TALK)],
        effort="max", web_search="on", deep_research=True,
        note="every figure must sit in a sentence citing a READ source whose captured passage holds that figure"),
]

# ================================================================== §13 ==

TOKEN_BUCKET = fixture_text("token_bucket.py")

ROSTER = ["Avery Quinn", "Bo Lindqvist", "Chen Okafor", "Dara Muntz", "Eli Navarro", "Farah Idowu"]
PLATFORM = ["Avery Quinn", "Chen Okafor", "Eli Navarro"]
ROSTER_TEXT = (
    "Team roster (made-up names):\n"
    "- Avery Quinn: platform team, on-call lead\n"
    "- Bo Lindqvist: data team\n"
    "- Chen Okafor: platform team\n"
    "- Dara Muntz: design\n"
    "- Eli Navarro: platform team, joined in March\n"
    "- Farah Idowu: data team\n")

QUOTED_SIGN = "Closed today for calibration"

RQ07_HISTORY = _pairs(
    "I'm comparing options for an internal summariser. First one from the vendor sheet: Kestrel-7B, 7 billion "
    "parameters, Apache-2.0 licence, 32,768-token context window, runs on one 24 GB GPU.",
    "Noted: Kestrel-7B has 7 billion parameters, an Apache-2.0 licence and a 32,768-token context window, and it "
    "fits on a single 24 GB GPU.",
    "Also looking at the Wren-QA dataset for evaluation: 12,000 question-answer pairs, CC BY-SA 4.0 licence.",
    "Wren-QA: 12,000 question-answer pairs under CC BY-SA 4.0. Share-alike applies if you redistribute changes.",
    "And our GPU budget is two 24 GB cards.",
    "Two 24 GB cards are enough to run one Kestrel-7B instance per card.",
)

SECTION_13 = [
    eval_case("RQ01", "explain_no_modify", "13", "Explain this code; do not modify it.", [turn(
        "Explain this code; do not modify it.\n\n```python\n" + TOKEN_BUCKET + "```",
        artifact=False, code_unmodified={"original": TOKEN_BUCKET},
        must_contain=[["token bucket", "tokenbucket"], ["monotonic"], ["capacity"], ["rate"]])],
        note="an explanation, quoting allowed; no edited copy, no diff, no 'fixed version'"),

    eval_case("RQ02", "document_only", "13", "Use only the uploaded document.", [turn(
        "Use only the uploaded document. What's the daily meal allowance on a domestic trip, how many days do I "
        "have to submit my claim, and what's the hotel cap for Tokyo?",
        artifact=False, max_chars=1200, must_contain=[["45"], ["21"]],
        gap_stated={"topic_any": ["tokyo"]},
        numbers_grounded={"source": fixture_text("travel-policy.md")})],
        attachments=[{"fixture": TRAVEL, "purpose": "document"}],
        note="Tokyo is not in the policy: say so instead of filling in a typical figure"),

    eval_case("RQ03", "names_only", "13", "Make the answer names only.", [turn(
        ROSTER_TEXT + "\nWho is on the platform team? Make the answer names only.",
        artifact=False, max_chars=200,
        names_only={"allowed": ROSTER, "required": PLATFORM, "min_items": 3, "max_items": 3})],
        note="three names and nothing else: no lead-in, no roles"),

    eval_case("RQ04", "research_cite", "13", "Research the current information and cite it.", [turn(
        "Research the current information and cite it. What is the latest stable release of the Rust programming "
        "language, and when was it released?",
        artifact=False, max_chars=2000, sources_used={"min": 2},
        citations={"min_distinct": 2, "support": [[{"regex": r"\b1\.\d{2,3}(?:\.\d+)?\b"}]]},
        regex_all=[r"\b1\.\d{2,3}(?:\.\d+)?\b"], regex_none=NO_CUTOFF_TALK)],
        web_search="auto",
        note="§12: an explicit web-verification request applies in Fast too"),

    eval_case("RQ05", "full_code", "13", "Make full code, not an outline.", [turn(
        "Make full code, not an outline. I need a Python command-line script `wordfreq.py` that reads a text file and "
        "prints the most frequent words, one `word count` pair per line, most frequent first, ties in A to Z order. "
        "Words are runs of letters, compared case-insensitively. Options: `--top N` (default 10) and "
        "`--min-length N` (default 1, skip shorter words). A missing file prints a one-line error and exits with a "
        "non-zero status. Put the logic in a `main()` function. Standard library only, one fenced code block.",
        artifact=False, min_code_blocks=1,
        complete_code={"lang": "python", "files": ["wordfreq.py"], "symbols": {"wordfreq.py": ["main"]}},
        code={"lang": "python", "layout": "files", "answer_files": ["wordfreq.py"], "files": {"check.py": RQ05_CHECK}})],
        note="a runnable program: no TODO, no stub, no elided section"),

    eval_case("RQ06", "quoted_today", "13", "a rewriting task with the word 'today' inside quoted text", [turn(
        "Rewrite this so it sounds polite and professional:\n\n"
        f"hey all, the sign on lab 2 says \"{QUOTED_SIGN}\" so dont go in, use lab 3 instead. back to normal tmrw",
        artifact=False, max_chars=700, max_headings=0, quoted_verbatim=[QUOTED_SIGN],
        must_contain=[["lab 3"], ["tomorrow"]], regex_none=DATE_PATTERNS)],
        note="the quoted sign is quoted: 'today' stays 'today', and no date is invented for it or for tomorrow"),

    eval_case("RQ07", "followup_reference", "13", "a follow-up that refers to 'the model discussed earlier'", [turn(
        "can we use the model discussed earlier commercially, and what's its context length?",
        artifact=False, max_chars=1200,
        must_contain=[["kestrel"], ["32,768", "32768", "32k", "32 k"], ["apache"]],
        regex_none=[r"which model", r"could you (?:please )?(?:clarify|specify)"])],
        history=RQ07_HISTORY,
        note="three turns back, one model, then a dataset and a GPU budget: the reference resolves to Kestrel-7B"),
]

EVAL_SET_CASES = SECTION_11 + SECTION_13
BY_ID: Dict[str, dict] = {c["id"]: c for c in EVAL_SET_CASES}

assert len(SECTION_11) == 9 and len(SECTION_13) == 7, (len(SECTION_11), len(SECTION_13))
assert len(BY_ID) == len(EVAL_SET_CASES), "duplicate case id"


def validate(cases: List[dict] = EVAL_SET_CASES) -> List[str]:
    """Structural problems with the set, as sentences. Empty means valid."""
    import answer_checks
    import harness

    known = harness.EXPECT_KEYS | answer_checks.EXPECT_KEYS
    problems = []
    for c in cases:
        cid = c.get("id", "?")
        if c.get("effort") not in EFFORTS:
            problems.append(f"{cid}: effort {c.get('effort')!r} is not one of {EFFORTS}")
        if c.get("web_search") not in ("off", "auto", "on"):
            problems.append(f"{cid}: web_search {c.get('web_search')!r}")
        if c.get("section") not in ("11", "13"):
            problems.append(f"{cid}: section {c.get('section')!r}")
        if not c.get("turns"):
            problems.append(f"{cid}: no turns")
        for i, msg in enumerate(c.get("history") or []):
            want = "user" if i % 2 == 0 else "assistant"
            if msg.get("role") != want or not str(msg.get("content") or "").strip():
                problems.append(f"{cid}: history[{i}] must be a non-empty {want} message")
        if c.get("history") and c["history"][-1]["role"] != "assistant":
            problems.append(f"{cid}: history must end with an assistant message")
        for a in c.get("attachments") or []:
            if a.get("purpose") not in ("document", "dataset"):
                problems.append(f"{cid}: attachment purpose {a.get('purpose')!r}")
            if not os.path.isfile(os.path.join(FIXTURES, a.get("fixture", ""))):
                problems.append(f"{cid}: attachment {a.get('fixture')!r} does not exist")
        for t in c.get("turns") or []:
            if not str(t.get("message") or "").strip():
                problems.append(f"{cid}: empty message")
            unknown = set(t.get("expect") or {}) - known
            if unknown:
                problems.append(f"{cid}: unknown expect key(s) {sorted(unknown)} would be ignored")
            if not t.get("expect"):
                problems.append(f"{cid}: a turn with no checks")
    return problems


if __name__ == "__main__":
    import sys

    sys.path.insert(0, HERE)
    for c in EVAL_SET_CASES:
        keys = sorted({k for t in c["turns"] for k in t["expect"]})
        print(f"{c['id']}  §{c['section']}  {c['effort']:<5}  {c['category']:<18}  history={len(c['history']):>2}  "
              f"attach={len(c['attachments'])}  checks={','.join(keys)}")
    errs = validate()
    print("\n".join(errs) or f"{len(EVAL_SET_CASES)} cases, valid")
    sys.exit(1 if errs else 0)
