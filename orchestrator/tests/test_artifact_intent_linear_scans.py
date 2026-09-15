"""The two intent scans that replaced quadratic regexes accept exactly what the
regexes accepted, and stay linear on one long line (CI, 2026-09-15: decide()
took 0.27 s on a 4,000-character request)."""
import random
import re
import time

from app.artifacts import intent as I

_OLD_TABLE_LINE_RE = re.compile(r"\t|\||(?:[^,;\n]*[,;](?![ \t])){3}")
_OLD_NEG_TOKEN_CLAUSE_RE = re.compile(r"[^.;,!?\n]*_neg_[^.;,!?\n]*")


def _strings(alphabet, n=4000, seed=15):
    rng = random.Random(seed)
    for _ in range(n):
        yield "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))


def test_table_line_scan_matches_the_regex_it_replaced():
    for s in _strings(["a", " ", "\t", ",", ";", "|", "\n", "x,", ", ", ";;", "1,2", "\r"]):
        assert I._is_table_line(s) == bool(_OLD_TABLE_LINE_RE.search(s)), repr(s)


def test_neg_token_clause_blanking_matches_the_regex_it_replaced():
    for s in _strings(["a", " ", "_neg_", ".", ";", ",", "!", "?", "\n", "make", "_neg"]):
        assert I._blank_neg_token_clauses(s) == _OLD_NEG_TOKEN_CLAUSE_RE.sub(" ", s), repr(s)


def test_the_scans_stay_linear_on_one_long_line():
    line = "a classy pdf report of the audit with tables and bold headings " * 700
    t0 = time.perf_counter()
    for _ in range(5):
        I._is_table_line(line)
        I._blank_neg_token_clauses(line + " _neg_ tail")
    assert (time.perf_counter() - t0) / 5 < 0.05
