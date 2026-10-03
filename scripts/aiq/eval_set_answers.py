"""Hand-written answers for the evaluation set: the control for its checks.

A check that rejects a correct answer would score every model unfairly, and a
check that accepts a wrong one measures nothing. So every case in eval_set.py
has ONE good answer that must pass all of its checks, and one or more bad
answers, each listing exactly the checks it must fail (`fails`). Between them
the bad answers fail every check type answer_checks.py defines at least once.
`sandbox_fails` lists the code_sandbox checks a bad answer must fail when its
code is really run (tests/test_eval_set.py runs those only where the sandbox
can isolate model-written code).

The answers are written the way an assistant would answer, not the way the
checks are written. Every URL is under example.com / example.org /
example.net (RFC 2606), every name is invented, and the "facts" in the web
answers are placeholders: these records test the checks, not the world.
They are never shown to a model.
"""
from __future__ import annotations

from typing import Dict, List


def src(n: int, url: str, read: bool = True, title: str = "") -> dict:
    """One meta.sources row, in the shape the orchestrator emits."""
    return {"n": n, "url": url, "title": title or url, "read": read, "cited": True}


# ------------------------------------------------------------------- EV04 --

PY_SOURCES = [src(1, "https://example.org/downloads/release-3-14-0", title="Release notes"),
              src(2, "https://example.com/news/python-3-14")]

# ------------------------------------------------------------------- EV06 --

INVENTORY_PY = '''"""A small in-memory inventory."""


class Inventory:
    """Stock levels and unit prices, keyed by SKU."""

    def __init__(self):
        self._stock = {}

    def add(self, sku, qty, unit_price):
        if qty <= 0:
            raise ValueError("quantity must be positive")
        count, _ = self._stock.get(sku, (0, unit_price))
        self._stock[sku] = (count + qty, unit_price)

    def remove(self, sku, qty):
        if sku not in self._stock:
            raise ValueError(f"unknown SKU {sku!r}")
        count, price = self._stock[sku]
        if qty > count:
            raise ValueError(f"cannot remove {qty} of {sku!r}: only {count} in stock")
        self._stock[sku] = (count - qty, price)

    def quantity(self, sku):
        return self._stock.get(sku, (0, 0.0))[0]

    def total_value(self):
        return sum(count * price for count, price in self._stock.values())
'''

TEST_INVENTORY_PY = '''import unittest

from inventory import Inventory


class InventoryTest(unittest.TestCase):
    def setUp(self):
        self.inv = Inventory()
        self.inv.add("bolt", 10, 0.25)

    def test_add_accumulates_and_reprices(self):
        self.inv.add("bolt", 5, 0.30)
        self.assertEqual(self.inv.quantity("bolt"), 15)
        self.assertAlmostEqual(self.inv.total_value(), 4.5)

    def test_remove_too_many_raises_and_keeps_stock(self):
        with self.assertRaises(ValueError):
            self.inv.remove("bolt", 11)
        self.assertEqual(self.inv.quantity("bolt"), 10)

    def test_unknown_sku(self):
        self.assertEqual(self.inv.quantity("nut"), 0)
        with self.assertRaises(ValueError):
            self.inv.remove("nut", 1)

    def test_non_positive_quantity_raises(self):
        with self.assertRaises(ValueError):
            self.inv.add("nut", 0, 0.10)


if __name__ == "__main__":
    unittest.main()
'''

EV06_GOOD = ("Here are both files.\n\n**inventory.py**\n\n```python\n" + INVENTORY_PY + "```\n\n"
             "**test_inventory.py**\n\n```python\n" + TEST_INVENTORY_PY + "```\n\n"
             "Run the tests with `python -m unittest test_inventory`.")

# remove() never checks the stock: it compiles, its own (too weak) tests pass,
# and the harness checker catches it.
INVENTORY_NO_STOCK_CHECK = INVENTORY_PY.replace(
    '''        if sku not in self._stock:
            raise ValueError(f"unknown SKU {sku!r}")
        count, price = self._stock[sku]
        if qty > count:
            raise ValueError(f"cannot remove {qty} of {sku!r}: only {count} in stock")
        self._stock[sku] = (count - qty, price)''',
    '''        count, price = self._stock.get(sku, (0, 0.0))
        self._stock[sku] = (count - qty, price)''')
WEAK_TESTS_PY = '''import unittest

from inventory import Inventory


class InventoryTest(unittest.TestCase):
    def test_add(self):
        inv = Inventory()
        inv.add("bolt", 10, 0.25)
        self.assertEqual(inv.quantity("bolt"), 10)

    def test_total_value(self):
        inv = Inventory()
        inv.add("bolt", 4, 0.50)
        self.assertAlmostEqual(inv.total_value(), 2.0)

    def test_remove(self):
        inv = Inventory()
        inv.add("bolt", 4, 0.50)
        inv.remove("bolt", 1)
        self.assertEqual(inv.quantity("bolt"), 3)
'''

# ------------------------------------------------------------------- EV09 --

EV09_SOURCES = [
    src(1, "https://example.org/cells/energy-density-survey", title="Cell energy density survey"),
    src(2, "https://example.com/reports/cycle-life", title="Stationary cycle life test report"),
    src(3, "https://example.net/surveys/pack-prices", title="Battery pack price survey"),
    src(4, "https://example.org/safety/thermal-runaway", title="Thermal runaway comparison"),
    src(5, "https://example.com/grid/project-economics", title="Grid storage project economics"),
]
EV09_PASSAGES = {
    1: "Cell-level energy density in this survey: NMC cells about 250 Wh/kg; LFP cells about 160 Wh/kg. "
       "Pack-level figures run 20 to 30 percent lower.",
    2: "Rated cycle life to 80% of initial capacity under stationary duty: LFP 6000 cycles or more; "
       "NMC about 2000 cycles.",
    3: "Average pack prices over the last three years: LFP fell from 150 to 95 USD/kWh; NMC fell from 170 to "
       "130 USD/kWh.",
    4: "Thermal runaway onset: LFP about 270 C, NMC about 210 C. Phosphate cathodes release far less oxygen. "
       "Cell-to-pack designs raise LFP pack density.",
    5: "A 15-year project cycling once a day needs about 5500 full cycles. Lithium carbonate prices fell sharply "
       "and drove most of the pack price decline.",
}
EV09_GOOD = """## Summary

LFP (lithium iron phosphate) and NMC (nickel manganese cobalt) cells both serve grid storage, but they trade energy density against cycle life and cost. NMC cells reach about 250 Wh/kg, while LFP cells sit nearer 160 Wh/kg [1]. LFP cells, in exchange, last for 6000 cycles or more in stationary duty, against roughly 2000 cycles for NMC [2]. Pack prices for LFP fell to about 95 USD per kWh in the latest survey [3]. For most stationary projects, where weight matters far less than lifetime cost per delivered kilowatt-hour, LFP is now the default choice, and NMC keeps an edge only where floor space is tight.

## Energy density

NMC cells reach about 250 Wh/kg at cell level [1]. LFP cells reach about 160 Wh/kg [1]. At pack level the gap narrows, because LFP packs can use cell-to-pack designs that leave out the module housings [4]. In a container-sized installation the difference shows up mainly as floor space rather than as a hard engineering limit, which is why the density gap matters less for grid storage than it does for vehicles.

## Cycle life

LFP cells in stationary duty are rated for 6000 cycles or more to 80% of their original capacity [2]. NMC cells are rated for about 2000 cycles under the same test conditions [2]. A project that cycles once a day over a 15-year life needs about 5500 cycles, which an LFP system can meet without a mid-life cell replacement [5]. An NMC system on the same duty would normally plan for at least one replacement, and that cost belongs in any comparison of the two chemistries.

## Safety

LFP has a higher thermal runaway onset, around 270 C, compared with about 210 C for NMC [4]. Its phosphate cathode releases far less oxygen when a cell fails, so a single cell fault is less likely to spread to its neighbours [4]. Both chemistries still need cell-level monitoring, ventilation and fire suppression in a grid installation, and the site design matters as much as the chemistry.

## Cost trend

Average LFP pack prices fell from about 150 USD per kWh to about 95 USD per kWh over the last three years [3]. NMC packs fell less, from about 170 USD per kWh to about 130 USD per kWh, over the same period [3]. Lower lithium carbonate prices drove most of the decline for both chemistries [5]. Because the cheaper chemistry is also the longer-lived one for stationary duty, the lifetime cost gap is wider than the pack price gap alone suggests.

## Sources

1. [Cell energy density survey](https://example.org/cells/energy-density-survey)
2. [Stationary cycle life test report](https://example.com/reports/cycle-life)
3. [Battery pack price survey](https://example.net/surveys/pack-prices)
4. [Thermal runaway comparison](https://example.org/safety/thermal-runaway)
5. [Grid storage project economics](https://example.com/grid/project-economics)
"""

# ------------------------------------------------------------------- RQ05 --

WORDFREQ_PY = '''import argparse
import re
import sys
from collections import Counter


def count_words(text, min_length=1):
    words = re.findall(r"[a-z]+", text.lower())
    return Counter(word for word in words if len(word) >= min_length)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print the most frequent words in a text file.")
    parser.add_argument("path")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--min-length", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        with open(args.path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print(f"wordfreq: cannot read {args.path}: {exc.strerror}", file=sys.stderr)
        return 1
    counts = count_words(text, args.min_length)
    for word, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: args.top]:
        print(f"{word} {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

WORDFREQ_OUTLINE = '''import argparse


def main():
    parser = argparse.ArgumentParser()
    # TODO: add the --top and --min-length options
    ...
    # count the words here
    # then print the results


if __name__ == "__main__":
    main()
'''

WORDFREQ_NO_MAIN = '''import argparse
import re
import sys
from collections import Counter

parser = argparse.ArgumentParser()
parser.add_argument("path")
parser.add_argument("--top", type=int, default=10)
parser.add_argument("--min-length", type=int, default=1)
args = parser.parse_args()
try:
    text = open(args.path, encoding="utf-8").read()
except OSError as exc:
    sys.exit(f"wordfreq: {exc.strerror}")
counts = Counter(w for w in re.findall(r"[a-z]+", text.lower()) if len(w) >= args.min_length)
for word, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[: args.top]:
    print(word, count)
'''


def _code(name: str, body: str, before: str = "", after: str = "") -> str:
    return f"{before}`{name}`:\n\n```python\n{body}```\n{after}"


# ================================================================ answers ==

GOOD: Dict[str, dict] = {
    "EV01": {"answer": "Hey! Good to hear from you. What can I help you with?"},
    "EV02": {"answer": "The mobile app launch is moving from Q2 to Q3. The payments integration is not ready, and two "
                       "of the four engineers on the project will be on leave in May, so Q3 is the earliest realistic "
                       "date. The budget stays at 120,000 dollars."},
    "EV03": {"answer": "1) 15% of 240 is **36**.\n2) 72F is **22.2C**.\n3) Three synonyms for 'happy': joyful, "
                       "cheerful, content."},
    "EV04": {"answer": "The latest stable version is Python 3.14.0, released on 7 October 2025 [1]. The release notes "
                       "describe 3.14.0 as the newest feature release, with bug-fix releases to follow [2].",
             "sources": PY_SOURCES},
    "EV05": {"answer": "- Battery life: 14 months at the 5-minute reporting interval.\n"
                       "- Operating temperature: -20 °C to 60 °C.\n"
                       "- Radio: LoRaWAN (EU868 and US915 bands)."},
    "EV06": {"answer": EV06_GOOD},
    "EV07": {"answer": "- Final budget: EUR 48,000 (raised from EUR 40,000).\n"
                       "- Venue: Casa Ribeira, which holds 40 people.\n"
                       "- Catering: two vegetarian menus, one vegan menu, and no alcohol before 6 pm."},
    "EV08": {"answer": "## Working\n\n"
                       "- Chat: 900 per minute is 15 per second; 15 x 0.4 s = 6 busy slots.\n"
                       "- Summaries: 300 per minute is 5 per second; 5 x 2.0 s = 10 busy slots.\n"
                       "- Total: 16 slots busy at peak.\n"
                       "- Usable slots per GPU at 70%: 8 x 0.7 = 5.6.\n"
                       "- GPUs: 16 / 5.6 = 2.86, rounded up to 3.\n\n"
                       "## Answer\n\nYou need **3 GPUs** at peak, and **4 GPUs** with one spare for failover.\n\n"
                       "## Risks\n\n"
                       "- Bursts above the per-minute peak push the busy share past 70%.\n"
                       "- Longer summaries raise the 2.0 s hold time, and the slot count with it."},
    "EV09": {"answer": EV09_GOOD, "sources": EV09_SOURCES, "passages": EV09_PASSAGES},
    "RQ01": {"answer": "This is a token bucket rate limiter.\n\n"
                       "- `__init__` stores the refill `rate` (tokens per second) and the bucket `capacity`, starts "
                       "the bucket full, and records the time with `time.monotonic()`, a clock that never goes "
                       "backwards.\n"
                       "- `allow(cost)` first refills the bucket: it adds `elapsed * rate` tokens for the time since "
                       "the last call, capped at `capacity`:\n\n"
                       "```python\nself.tokens = min(self.capacity, self.tokens + elapsed * self.rate)\n```\n\n"
                       "- If at least `cost` tokens are left, it spends them and returns True; otherwise it returns "
                       "False and spends nothing.\n\n"
                       "So bursts of up to `capacity` events pass at once, and the long-run average is held to `rate` "
                       "events per second. One thing to be aware of: there is no lock, so two threads calling "
                       "`allow` at the same moment can spend the same tokens.\n\n"
                       "Typical use:\n\n"
                       "```python\nlimiter = TokenBucket(rate=5, capacity=10)\nif limiter.allow():\n"
                       "    handle_request()\n```\n\n"
                       "I have left your code exactly as it is."},
    "RQ02": {"answer": "- Daily meal allowance on a domestic trip: EUR 45.\n"
                       "- Claim deadline: within 21 days of the last day of the trip.\n"
                       "- Tokyo: the policy does not list a hotel cap for Tokyo. It covers Berlin, Lisbon and Madrid "
                       "only, and says to ask the travel desk before booking in any other city."},
    "RQ03": {"answer": "Avery Quinn\nChen Okafor\nEli Navarro"},
    "RQ04": {"answer": "The latest stable Rust release is 1.90.0, released on 18 September 2025 [1]. The release "
                       "announcement and the version history page both list 1.90.0 as current [1][2].",
             "sources": [src(1, "https://example.org/rust/announcing-1-90-0"),
                         src(2, "https://example.com/rust/versions")]},
    "RQ05": {"answer": _code("wordfreq.py", WORDFREQ_PY, after="\nRun it as `python wordfreq.py notes.txt --top 5`.")},
    "RQ06": {"answer": "Hello everyone, the sign on Lab 2 reads \"Closed today for calibration\", so please do not "
                       "go in. Please use Lab 3 in the meantime. Lab 2 should be back to normal tomorrow. Thank you."},
    "RQ07": {"answer": "Yes. Kestrel-7B, the model from earlier, is under the Apache-2.0 licence, which allows "
                       "commercial use. Its context window is 32,768 tokens."},
}

BAD: Dict[str, List[dict]] = {
    "EV01": [
        {"label": "structured_essay", "fails": ["max_headings", "max_tables", "max_code_blocks"],
         "answer": "# Hello!\n\nHere is what I can do:\n\n| Area | Examples |\n|---|---|\n| Writing | emails, reports |\n"
                   "| Code | Python, SQL |\n\n```python\nprint('hello')\n```"},
        {"label": "searched_and_cited", "fails": ["no_citations", "sources_used"],
         "answer": "Hello! Greetings are a social ritual [1].",
         "sources": [src(1, "https://example.com/greetings")]},
    ],
    "EV02": [
        {"label": "invented_date", "fails": ["numbers_grounded"],
         "answer": "The mobile app launch is moving from Q2 to Q3, on 15 August 2025. The payments integration is not "
                   "ready, and two of the four engineers will be on leave in May. The budget stays at 120,000 dollars."},
        {"label": "headed_and_lossy", "fails": ["max_headings", "must_contain", "regex_all"],
         "answer": "## Roadmap update\n\nThe app launch moves to Q3 because payments are not ready. Budget unchanged."},
    ],
    "EV03": [
        {"label": "wrong_sum_two_synonyms", "fails": ["regex_all", "min_matches", "order"],
         "answer": "1) 15% of 240 is 34.\n2) 72F is 22.2C.\n3) joyful, glad."},
    ],
    "EV04": [
        {"label": "from_memory", "fails": ["citations", "sources_used", "regex_none"],
         "answer": "As of my knowledge cutoff, the latest stable version is Python 3.12."},
        {"label": "snippet_only", "fails": ["citations", "sources_used"],
         "answer": "The latest stable version is Python 3.14.0, released on 7 October 2025 [1].",
         "sources": [src(1, "https://example.org/downloads/release-3-14-0", read=False)]},
        {"label": "dangling_marker", "fails": ["citations"],
         "answer": "The latest stable version is Python 3.14.0, released on 7 October 2025 [3].",
         "sources": PY_SOURCES},
    ],
    "EV05": [
        {"label": "measuring_range_and_invented_life", "fails": ["must_contain", "numbers_grounded"],
         "answer": "- Battery life: about 18 months.\n- Operating temperature: -10 to 50 °C.\n- Radio: LoRaWAN."},
    ],
    "EV06": [
        {"label": "one_file_with_stub", "fails": ["min_code_blocks", "code_files_present", "code_no_placeholders"],
         "sandbox_fails": ["code_runs", "code_correct"],
         "answer": _code("inventory.py", INVENTORY_PY.replace(
             '''        if sku not in self._stock:
            raise ValueError(f"unknown SKU {sku!r}")
        count, price = self._stock[sku]
        if qty > count:
            raise ValueError(f"cannot remove {qty} of {sku!r}: only {count} in stock")
        self._stock[sku] = (count - qty, price)''', "        pass  # TODO: check the stock"),
             after="\nI'll write the tests next.")},
        {"label": "runs_but_wrong", "fails": [], "sandbox_fails": ["code_correct"],
         "answer": ("**inventory.py**\n\n```python\n" + INVENTORY_NO_STOCK_CHECK + "```\n\n"
                    "**test_inventory.py**\n\n```python\n" + WEAK_TESTS_PY + "```\n")},
    ],
    "EV07": [
        {"label": "stale_budget_wrong_venue", "fails": ["must_contain", "regex_none"],
         "answer": "The final budget is EUR 40,000 and you picked Armazem 12, which holds 60. Catering: one vegan "
                   "menu."},
        {"label": "invented_remainder", "fails": ["numbers_grounded"],
         "answer": "- Final budget: EUR 48,000.\n- Venue: Casa Ribeira, which holds 40 people.\n"
                   "- Catering: two vegetarian menus, one vegan menu, and no alcohol before 6 pm. About EUR 12,500 "
                   "of the budget is left for catering."},
    ],
    "EV08": [
        {"label": "no_headings_wrong_total", "fails": ["required_sections", "section_bullets", "regex_all"],
         "answer": "You need 2 GPUs: 16 busy slots / 8 slots per GPU = 2. Keep one spare, so 3 GPUs in total."},
        {"label": "four_risks", "fails": ["section_bullets"],
         "answer": None},  # set below: the good answer plus two more risk bullets
    ],
    "EV09": [
        {"label": "uncited_figure", "fails": ["citations"],
         "answer": EV09_GOOD.replace("NMC cells reach about 250 Wh/kg at cell level [1].",
                                     "NMC cells reach about 250 Wh/kg at cell level."),
         "sources": EV09_SOURCES, "passages": EV09_PASSAGES},
        {"label": "figure_not_in_passage", "fails": ["citation_passages"],
         "answer": EV09_GOOD.replace("NMC cells reach about 250 Wh/kg at cell level [1].",
                                     "NMC cells reach about 300 Wh/kg at cell level [1]."),
         "sources": EV09_SOURCES, "passages": EV09_PASSAGES},
        {"label": "no_passages_captured", "fails": ["citation_passages"],
         "answer": EV09_GOOD, "sources": EV09_SOURCES},
        {"label": "uninspected_url_and_no_safety_heading", "fails": ["citations", "required_sections"],
         "answer": EV09_GOOD.replace("## Safety", "## Hazards")
         + "6. [Industry blog](https://example.net/blog/batteries)\n",
         "sources": EV09_SOURCES, "passages": EV09_PASSAGES},
    ],
    "RQ01": [
        {"label": "thread_safe_copy", "fails": ["code_unmodified"],
         "answer": "It's a token bucket limiter that refills at `rate` using `time.monotonic()` up to `capacity`. "
                   "I made it thread-safe:\n\n```python\nimport threading\nimport time\n\n\nclass TokenBucket:\n"
                   "    def __init__(self, rate: float, capacity: int) -> None:\n        self.rate = rate\n"
                   "        self.capacity = capacity\n        self.tokens = float(capacity)\n"
                   "        self.updated = time.monotonic()\n        self._lock = threading.Lock()\n```"},
        {"label": "diff", "fails": ["code_unmodified"],
         "answer": "A token bucket: tokens refill at `rate` per second (measured with `time.monotonic()`) up to "
                   "`capacity`. A small fix:\n\n```diff\n-        self.tokens = min(self.capacity, self.tokens + "
                   "elapsed * self.rate)\n+        self.tokens = min(float(self.capacity), self.tokens + "
                   "elapsed * self.rate)\n```"},
        {"label": "offers_corrected_version", "fails": ["code_unmodified"],
         "answer": "This is a token bucket: it refills at `rate` using `time.monotonic()` and caps at `capacity`. "
                   "Here is the corrected version, which I would use instead."},
    ],
    "RQ02": [
        {"label": "filled_in_tokyo", "fails": ["numbers_grounded", "gap_stated"],
         "answer": "- Meals: EUR 45 per day.\n- Claims: within 21 days.\n- Tokyo: about EUR 220 per night, which is "
                   "typical for Tokyo."},
        {"label": "skipped_tokyo", "fails": ["gap_stated"],
         "answer": "- Meals: EUR 45 per day on a domestic trip.\n- Claims: within 21 days of the trip's last day."},
    ],
    "RQ03": [
        {"label": "sentence", "fails": ["names_only"],
         "answer": "The platform team is Avery Quinn, Chen Okafor and Eli Navarro."},
        {"label": "with_roles", "fails": ["names_only"],
         "answer": "- Avery Quinn (on-call lead)\n- Chen Okafor\n- Eli Navarro: joined in March"},
        {"label": "extra_person", "fails": ["names_only"],
         "answer": "Avery Quinn, Bo Lindqvist, Chen Okafor, Eli Navarro"},
        {"label": "lead_in", "fails": ["names_only"],
         "answer": "Here are the names:\n- Avery Quinn\n- Chen Okafor\n- Eli Navarro"},
    ],
    "RQ04": [
        {"label": "uncited", "fails": ["citations", "sources_used"],
         "answer": "The latest stable Rust release is 1.90.0, released in September 2025."},
        {"label": "uninspected_url", "fails": ["citations", "sources_used"],
         "answer": "The latest stable Rust release is 1.90.0 [1], according to "
                   "https://example.com/rust-blog/latest.",
         "sources": [src(1, "https://example.org/rust/announcing-1-90-0")]},
    ],
    "RQ05": [
        {"label": "outline", "fails": ["code_no_placeholders"], "sandbox_fails": ["code_correct"],
         "answer": "Here is an outline of the script; fill in the rest as needed.\n\n"
                   + _code("wordfreq.py", WORDFREQ_OUTLINE)},
        {"label": "syntax_error", "fails": ["code_compiles", "code_symbols"], "sandbox_fails": ["code_runs", "code_correct"],
         "answer": _code("wordfreq.py", WORDFREQ_PY.replace("def main(argv=None):", "def main(argv=None)"))},
        {"label": "no_main", "fails": ["code_symbols"],
         "answer": _code("wordfreq.py", WORDFREQ_NO_MAIN)},
    ],
    "RQ06": [
        {"label": "today_resolved", "fails": ["quoted_verbatim", "regex_none", "must_contain"],
         "answer": "Hello everyone, the sign on Lab 2 reads \"Closed on Saturday, 3 October 2026 for calibration\", "
                   "so please do not go in. Please use Lab 3 instead. Normal service resumes on Sunday, 4 October."},
        {"label": "quote_paraphrased", "fails": ["quoted_verbatim"],
         "answer": "Hello all, Lab 2 is closed today for calibration, so please use Lab 3. It reopens tomorrow."},
    ],
    "RQ07": [
        {"label": "asks_which", "fails": ["must_contain", "regex_none"],
         "answer": "Which model do you mean? Could you clarify?"},
        {"label": "answers_for_the_dataset", "fails": ["must_contain"],
         "answer": "Wren-QA is under CC BY-SA 4.0, so commercial use is allowed if you share alike. It has 12,000 "
                   "pairs."},
    ],
}

# EV08 "four_risks": the good answer with two more risk bullets.
BAD["EV08"][1]["answer"] = GOOD["EV08"]["answer"] + (
    "\n- A GPU failure during a burst leaves no headroom.\n- Model upgrades may change the hold times.")
