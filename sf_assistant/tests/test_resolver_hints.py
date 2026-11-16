"""The term resolver's wiring, steps (1), (2) and (6) (docs/resolver_integration.md): the question's spelling hints
for both agents, the knowledge BM25 query expansion and the answer's "Read as" line.

With the switch off (``SFA_RESOLVER=off``) no resolver exists: ``KnowledgeIndex.expand`` is None, the search tools
report no ``read_as``, ``RecordTools.object_hints`` is the mixin's hints, ``question_spelling`` is None, and the merge's
name note is as before (the first tests here; the other test files and the parity suite cover the rest). With it on (the
"flag-on twins"): misspelt query words are searched as they read, the hints list the misspelt names first (metadata
only; a person is left to the people lines; the knowledge side never sees a stored value), and the final answer states
each correction it rests on in one code line from ``prompts/read_as_line.md``. Every test sets the switch itself. Fake
data only: tests/warehouse_fixture.py, tests/fixtures/Internal_Interview__c.json and made-up word lists.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant import config as C  # noqa: E402
from sf_assistant import pipeline  # noqa: E402
from sf_assistant import prompts as P  # noqa: E402
from sf_assistant.agent import knowledge_agent as KA  # noqa: E402
from sf_assistant.agent.question import Question  # noqa: E402
from sf_assistant.config import ResolverConfig, load_records_config  # noqa: E402
from sf_assistant.knowledge import search as S  # noqa: E402
from sf_assistant.knowledge.resolver import Correction, Resolution  # noqa: E402
from sf_assistant.knowledge.store import ObjectKnowledge  # noqa: E402
from sf_assistant.tools import knowledge_tools as KT  # noqa: E402

FIXTURE_JSON = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H") + \
    tuple(C.RESOLVER_ENV_VARS.values())
#: A made-up stand-in for the English word list: generic words of the test questions only.
WORDS = ("a about all and are by count did does for has have how is many me of records show take taken tell the to "
         "was were what when where which who why with work mean means used list").split()
#: Correctly spelt questions of the fixture: no spelling section, no block, no line (the controls).
CONTROLS = ["how many interviews are completed", "how many internal interviews were scheduled",
            "what does the interview outcome field mean", "show the candidate training records",
            "how many interviews did delta person take", "what is a deliverable", "who is echo one"]


@pytest.fixture(autouse=True)
def _switch_unset(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def _words(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    p = base / "words.txt"
    p.write_text("\n".join(WORDS) + "\n", encoding="utf-8")
    return p


def _open(base: Path, on: bool):
    pytest.importorskip("duckdb")
    import warehouse_fixture as F
    from sf_assistant.tools.open_records import open_records

    t = open_records(load_records_config(warehouse=str(F.make_warehouse(base)), today=dt.date(2026, 10, 13)),
                     str(F.make_knowledge(base)), ResolverConfig(enabled=on, lexicon=(str(_words(base)),)))
    assert (t.catalog.resolver is not None) is on, t.resolver_lines
    return t


@pytest.fixture(scope="module")
def tools_on(tmp_path_factory):
    t = _open(tmp_path_factory.mktemp("hints_on"), on=True)
    yield t
    t.close()


@pytest.fixture(scope="module")
def tools_off(tmp_path_factory):
    t = _open(tmp_path_factory.mktemp("hints_off"), on=False)
    yield t
    t.close()


def _knowledge_dir(base: Path) -> Path:
    pytest.importorskip("duckdb")
    import warehouse_fixture as F

    return F.make_knowledge(base)


def _registry(base: Path, on: bool, single: bool = False):
    """``pipeline.build`` over the fixture's knowledge files (or the one fixture JSON) with the switch on or off."""
    from sf_assistant import pipeline

    paths = [str(FIXTURE_JSON)] if single else sorted(str(p) for p in _knowledge_dir(base).glob("*.json"))
    settings = dataclasses.replace(C.load_settings(records=False),
                                   resolver=ResolverConfig(enabled=on, lexicon=(str(_words(base)),)))
    reg = pipeline.build(paths, settings)[2]
    assert (reg.resolver is not None) is on, reg.resolver_lines
    return reg


def _q(text: str) -> Question:
    return Question(qid="ask-001", category="cross_section", question=text, expected_facts=[],
                    expected_tools=["search_knowledge"], answerable=True, source="ask", gold_chunk_ids=[])


# =============================================================================================== switch off
def test_switch_off_index_has_no_expander_and_searches_as_before(tmp_path):
    reg = _registry(tmp_path, on=False)
    assert reg.index.expand is None and all(r.index.expand is None for r in reg.registries.values())
    for query in ("backgrond chech", "Interviw Outcom", "interview outcome", "delivrable"):
        assert reg.index.search_read_as(query, k=5) == (reg.index.search(query, k=5), {})
        out = reg.call("search_all_knowledge", {"query": query})
        assert "read_as" not in out["result"]
        sub = next(iter(reg.registries.values()))
        for tool in ("search_knowledge", "search_fields"):
            res = sub.call(tool, {"query": query})["result"]
            assert "read_as" not in res


def test_switch_off_hints_are_the_mixin_hints(tools_off):
    from sf_assistant.tools.names import NamesMixin

    for q in CONTROLS + ["how many internl intervews are Sheduled", "what is a delivrable"]:
        tools_off.begin_question(q)
        h = tools_off.object_hints(q)
        assert h == NamesMixin.object_hints(tools_off, q) and "Spelling (read by code" not in h
        assert tools_off._question_spots(q) == NamesMixin._question_spots(tools_off, q)
    assert tools_off._spelling_spans("what is a delivrable") == []


def test_switch_off_question_spelling_is_none(tmp_path, tools_off):
    reg = _registry(tmp_path, on=False)
    assert pipeline.question_spelling("what is a delivrable", reg) is None
    assert pipeline.question_spelling("what is a delivrable", reg, SimpleNamespace(tools=tools_off)) is None


def test_switch_off_trace_and_name_note_are_unchanged():
    from sf_assistant.agent.merge import name_notes, with_name_note

    class Reg:
        tools = {"t": None}

        @staticmethod
        def call(name, arguments):
            return {"ok": True, "result": {"x": 1}, "error": None, "chunk_ids": []}

    _payload, record = KA._execute_tool("t", {}, Reg())
    assert set(record) == {"name", "arguments", "ok", "chunk_ids", "latency_s"}
    rec_answer = "No record is named 'Kiloo'; the closest stored name is 'Kilo'. Kilo took 3 mocks."
    q = "how many mocks did kiloo take"
    merged = "Kilo took 3 mock interviews.\n\nNo record is named 'Kiloo'; the closest stored name is 'Kilo'."
    assert name_notes(rec_answer, q) == [] and with_name_note(merged, rec_answer, q) == merged   # one word: none


# ============================================================================================= (2) BM25
class _Spy:
    """An expander that records its calls and returns fixed readings."""

    def __init__(self, pairs=()):
        self.calls: list[tuple[str, list]] = []
        self.pairs = list(pairs)

    def __call__(self, query, unknown):
        self.calls.append((query, list(unknown)))
        return self.pairs


def _index() -> S.KnowledgeIndex:
    kn = ObjectKnowledge.load(str(FIXTURE_JSON))
    return S.KnowledgeIndex(kn.chunks())


def test_a_correct_query_never_calls_the_expander_and_ranks_as_before():
    plain, spied = _index(), _index()
    spy = _Spy([("x", ["status"])])
    spied.expand = spy
    for query in ("interview status", "Status__c", "mock interview score", "candidate"):
        assert all(t in spied.words for t in S.tokenize(query))
        assert spied.search_read_as(query, k=8) == (plain.search(query, k=8), {})
    assert spy.calls == []


def test_only_index_words_are_added_once_and_reported():
    idx = _index()
    idx.expand = _Spy([("stauts", ["Status", "status"]), ("zzq", ["qqqx wwwy"])])
    assert "qqqx" not in idx.words and "status" in idx.words
    hits, read_as = idx.search_read_as("stauts zzq", k=5)
    assert read_as == {"stauts": "status"}
    assert [h["id"] for h in hits] == [h["id"] for h in _index().search("stauts zzq status", k=5)]
    idx.expand = _Spy([("zzq", ["qqqx"])])          # nothing an index has: the query stays as typed
    assert idx.search_read_as("zzq", k=5) == ([], {})


def test_a_failing_expander_leaves_the_query_as_typed():
    idx = _index()

    def broken(query, unknown):
        raise RuntimeError("boom")

    idx.expand = broken
    assert idx.search_read_as("interview stauts", k=5) == (_index().search("interview stauts", k=5), {})


def _corr(text, decision, suggestion, suggestions, kinds=("field",), cands=()):
    return Correction(0, len(text), text, decision, suggestion, tuple(suggestions), "edit", 0.9, tuple(kinds),
                      tuple(cands))


class _FakeResolver:
    """``spell_correct`` / ``resolve_term`` with fixed answers."""

    def __init__(self, spans=(), terms=None):
        self.spans = list(spans)
        self.terms = dict(terms or {})
        self.asked: list[tuple] = []

    def spell_correct(self, question):
        return list(self.spans)

    def resolve_term(self, text, kinds=None, wide=True, **_kw):
        self.asked.append((text, tuple(kinds) if kinds else None, wide))
        return self.terms.get(text) or Resolution(text, "none", None, (), ())


def test_the_expander_reads_spans_then_single_tokens_strictly():
    from sf_assistant.knowledge.resolver import Candidate

    person = Candidate("person", "person:Recruiter__c/1", "Persona", "persn", "edit", 0.9)
    field = Candidate("field", "A.B__c", "Interview Support Person", "persn", "edit", 0.9)
    r = _FakeResolver(
        spans=[_corr("backgrond chech", "corrected", "Background Check", ["Background Check"]),
               _corr("persn", "ambiguous", None, ["CS Person", "Finance Person", "PersonAccount"], cands=(field,)),
               _corr("already", "corrected", "Ready", ["Ready"])],          # covers no unknown token: ignored
        terms={"stauts": Resolution("stauts", "corrected", "Status", ("Status",), ()),
               "kiloo": Resolution("kiloo", "corrected", "Kilo", ("Kilo",), (person,))})
    ex = S.resolver_expander(r)
    out = ex("how does backgrond chech persn stauts", ["backgrond", "chech", "persn", "stauts"])
    assert out == [("backgrond chech", ["background check"]), ("persn", ["person"]), ("stauts", ["status"])]
    assert r.asked == [("stauts", None, False)]                 # strict scoring, only the uncovered token
    meta = S.resolver_expander(r, kinds=S.METADATA_KINDS)
    assert meta("kiloo", ["kiloo"]) == []                       # a person reading is no catalog search word
    assert S.resolver_expander(r)("kiloo", ["kiloo"]) == [("kiloo", ["kilo"])]


def test_an_expander_reads_each_query_once_and_a_new_one_starts_fresh():
    class Counting(_FakeResolver):
        n = 0

        def spell_correct(self, question):
            Counting.n += 1
            return super().spell_correct(question)

    r = Counting(spans=[_corr("stauts", "corrected", "Status", ["Status"])])
    idx = _index()
    idx.expand = S.resolver_expander(r)
    for section in ("field", "picklist", None):        # search_fields asks once per section
        assert idx.search_read_as("stauts", k=3, section=section)[1] == {"stauts": "status"}
    assert Counting.n == 1
    r.spans = [_corr("stauts", "corrected", "Stage", ["Stage"])]
    idx.expand = S.resolver_expander(r)                 # attach_resolver makes a new expander: no old reading
    assert idx.search_read_as("stauts", k=3)[1] == {} and Counting.n == 2   # 'stage' is no index word here


def test_a_one_token_reading_adds_only_the_words_the_token_stands_for():
    r = _FakeResolver(terms={"ereference": Resolution("ereference", "corrected", "Candidate Preference",
                                                      ("Candidate Preference",), (), missing_words=("candidate",))})
    assert S.resolver_expander(r)("ereference check", ["ereference"]) == [("ereference", ["preference"])]
    r.terms["ereference"] = Resolution("x", "corrected", "Candidate", ("Candidate",), (), missing_words=("candidate",))
    assert S.resolver_expander(r)("ereference", ["ereference"]) == []
    one = _FakeResolver(spans=[_corr("ereference", "ambiguous", None, ["Candidate Preference"])])   # a containing term
    assert S.resolver_expander(one)("ereference", ["ereference"]) == [("ereference", ["preference"])]
    two = _FakeResolver(spans=[_corr("internl intrview logs", "ambiguous", None,
                                     ["Internal Interview Question Log", "Internal Interview Section Log"])])
    assert S.resolver_expander(two)("internl intrview logs", ["internl", "intrview"]) == \
        [("internl intrview logs", ["internal interview log"])]


def test_switch_on_typo_queries_find_the_clean_querys_chunk(tmp_path):
    reg = _registry(tmp_path, on=True)
    assert reg.index.expand is not None and all(r.index.expand is not None for r in reg.registries.values())
    for typo, clean in (("Interviw Outcom", "Interview Outcome"), ("delivrable", "deliverable"),
                        ("Ofer Recieved", "Offer Received"), ("candidat trainng", "candidate training")):
        got = reg.call("search_all_knowledge", {"query": typo, "k": 3})["result"]
        want = reg.call("search_all_knowledge", {"query": clean, "k": 3})["result"]
        assert got["results"][0]["id"] == want["results"][0]["id"], (typo, got.get("read_as"))
        assert got["read_as"] and "read_as" not in want
    single = _registry(tmp_path / "one", on=True, single=True)
    res = single.call("search_knowledge", {"query": "Humann Totall Scor"})["result"]
    assert res["read_as"] and res["results"][0]["id"].endswith("Human_Total_Score__c"), res.get("read_as")
    fields = single.call("search_fields", {"query": "Humann Totall Scor"})["result"]
    assert fields["fields"][0]["api_name"] == "Human_Total_Score__c" and fields["read_as"]


def test_switch_on_correct_queries_rank_as_with_the_switch_off(tmp_path):
    on, off = _registry(tmp_path / "on", on=True), _registry(tmp_path / "off", on=False)
    for q in CONTROLS:
        assert on.call("search_all_knowledge", {"query": q}) == off.call("search_all_knowledge", {"query": q}), q


def test_attach_resolver_none_removes_the_expander(tmp_path):
    reg = _registry(tmp_path, on=True)
    reg.attach_resolver(None)
    assert reg.index.expand is None and all(r.index.expand is None for r in reg.registries.values())


# ============================================================================================ (1) hints
def test_switch_on_hints_list_the_misspelt_names_first(tools_on):
    q = "how many internl intervews are Sheduled"
    tools_on.begin_question(q)
    h = tools_on.object_hints(q)
    lines = h.split("\n")
    assert lines[0] == "[Hints from code, not from the user]"
    assert lines[1].startswith("Spelling (read by code, not from the user): ")
    assert "- 'internl intervews' -> Internal Interview (object Internal_Interview__c)" in lines
    assert any(ln.startswith("- 'Sheduled' -> Scheduled (value of ") for ln in lines)
    # the objects and the stored values were searched with the corrected words
    objects = h.split("Objects whose knowledge matches the question words:\n", 1)[1]
    assert objects.startswith("- Internal_Interview__c (Internal Interview)")
    assert "- 'Scheduled' in Internal_Interview__c.Status__c" in h
    assert h.index("Spelling (read by code") < h.index("Objects whose knowledge")


def test_switch_on_hints_leave_people_to_the_people_lines(tools_on):
    q = "how many interviews did Dellta Person take"
    tools_on.begin_question(q)
    h = tools_on.object_hints(q)
    assert "Spelling (read by code" not in h and "dellta" not in h.split("People named")[0].lower()
    assert "the closest stored name is 'Delta Person'" in h


def test_switch_on_hints_keep_the_sessions_people_for_a_corrected_question(tools_on):
    from sf_assistant.tools.names import NamesMixin

    q = "how many internl intervews did Dellta Person take"
    fixed = KT.corrected_text(q, tools_on._spelling_spans(q))
    assert fixed != q and "Dellta Person" in fixed
    tools_on.begin_question(q, people=[])          # the session confirmed nobody
    assert tools_on._question_spots(fixed) == [] and "People named" not in tools_on.object_hints(q)
    tools_on.begin_question(q)                     # no people given: spotted from the question as typed
    assert tools_on._question_spots(fixed) == NamesMixin._question_spots(tools_on, q)
    assert "People named" in tools_on.object_hints(q)


def test_switch_on_ambiguous_names_give_a_may_mean_line():
    r = _FakeResolver(spans=[_corr("Statu", "ambiguous", None, ["Status", "Stage", "Kilo"])],
                      terms={"Status": Resolution("Status", "exact", "Status", ("Status",), (
                          _cand("field", "A__c.Status__c"),)),
                             "Stage": Resolution("Stage", "exact", "Stage", ("Stage",), (
                                 _cand("value", "A__c.Phase__c=Stage", source="picklist"),))})
    spans = KT.question_spans(r, "what is Statu")
    assert [s.line() for s in spans] == ["- 'Statu' may mean: Status, Stage (say which one you used, or ask)"]
    assert KT.corrected_text("what is Statu", spans) == "what is Statu"     # a did-you-mean replaces nothing


def test_a_corrected_object_line_names_the_object_not_its_lookups():
    assert KT._targets_text([("object", "Account"), ("field", "Onboarding__c.Account__c"),
                             ("value", "Invoice__c.From__c=Account")]) == "object Account"
    assert KT._targets_text([("field", "A__c.Done__c"), ("value", "B__c.Status__c=Done"), ("value", "C__c.S__c=Done"),
                             ("value", "D__c.S__c=Done"), ("record_type", "E__c.RecordType=Done")]) == \
        "field A__c.Done__c; value of B__c.Status__c, C__c.S__c, ..."
    assert KT._targets_text([("record_type", "E__c.RecordType=Done"), ("component", "flow:Done_Flow")]) == \
        "record type of E__c; flow:Done_Flow"


def test_records_off_the_knowledge_block_keeps_only_corrected_spans():
    stat = Resolution("Status", "exact", "Status", ("Status",), (_cand("field", "A__c.Status__c"),))
    acct = Resolution("Account", "exact", "Account", ("Account",), (_cand("object", "Account"),))
    spans = [_corr("Statu", "ambiguous", None, ["Status"]), _corr("accont", "corrected", "Account", ["Account"])]
    kn = SimpleNamespace(resolver=_FakeResolver(spans=spans, terms={"Status": stat, "Account": acct}))
    off = pipeline.question_spelling("Statu accont", kn)                        # records off: the knowledge resolver
    assert [(s.typed, s.decision) for s in off.knowledge] == [("accont", "corrected")] and off.records == []
    full = _FakeResolver(spans=spans, terms={"Status": stat, "Account": acct})
    session = SimpleNamespace(tools=SimpleNamespace(catalog=SimpleNamespace(resolver=full)))
    on = pipeline.question_spelling("Statu accont", kn, session)               # records on: the full resolver decides
    assert [s.decision for s in on.knowledge] == ["ambiguous", "corrected"] == [s.decision for s in on.records]


def _cand(kind, target, text="x", source="label"):
    from sf_assistant.knowledge.resolver import Candidate

    return Candidate(kind, target, text, text, "exact", 1.0, target.split(".")[0], source)


def test_switch_on_controls_give_no_spelling_section(tools_on, tmp_path):
    reg = _registry(tmp_path, on=True)
    for q in CONTROLS:
        tools_on.begin_question(q)
        assert "Spelling (read by code" not in tools_on.object_hints(q), q
        sp = pipeline.question_spelling(q, reg, SimpleNamespace(tools=tools_on))
        assert sp.knowledge == [] and sp.records == [] and sp.block == "", q
        assert pipeline.question_spelling(q, reg).block == ""


def test_switch_on_knowledge_block_never_holds_a_person_or_a_stored_value(tools_on, tmp_path):
    r = tools_on.catalog.resolver
    stored = sorted(t.text for t in r.terms if t.kind == "value" and t.source in KT.STORED_VALUE_SOURCES
                    and not KT._metadata_targets(r, t.text, stored_values=False) and len(t.text.split()[0]) >= 5)
    assert stored, "the fixture has a value that only the snapshot stores (Interviewer_s_Name__c)"
    word = stored[0]
    typo = word[:2] + word[2] + word[2:]                       # a doubled letter: corrected by the resolver
    q = f"how many interviews have {typo} as interviewer and how did Dellta Person do"
    sp = pipeline.question_spelling(q, _registry(tmp_path, on=True), SimpleNamespace(tools=tools_on))
    assert [s.used for s in sp.records] == [word]              # the record agent sees the stored value ...
    assert sp.knowledge == [] and sp.block == ""               # ... the knowledge agent neither it nor the person
    assert all(s.typed.lower() != "dellta person" for s in sp.records)


def test_switch_on_knowledge_question_carries_the_block_and_q_keeps_the_words(tmp_path):
    reg = _registry(tmp_path, on=True)
    q = _q("what is a delivrable")
    sp = pipeline.question_spelling(q.question, reg)
    kq = sp.knowledge_question(q)
    assert kq.question == "what is a delivrable\n\n" + sp.block and q.question == "what is a delivrable"
    assert sp.block == P.fill(KT.SPELLING_HINTS, LINES="- 'delivrable' -> Deliverable (object Deliverable__c)")
    plain = _q("what is a deliverable")
    assert pipeline.question_spelling(plain.question, reg).knowledge_question(plain) is plain
    assert sp.rows() == [{"typed": "delivrable", "decision": "corrected", "used": "Deliverable", "options": []}]


def test_spelling_texts_come_from_the_prompt_files():
    assert KT.SPELLING_HINTS == P.text("spelling_hints") and KT.READ_AS_LINE == P.text("read_as_line")
    assert P.load("spelling_hints").placeholders == ("<<LINES>>",)
    assert P.load("read_as_line").placeholders == ("<<PAIRS>>",)
    assert KT.SPELLING_HINTS.startswith("Spelling (read by code, not from the user): ")
    assert KT.READ_AS_LINE == "Read as: <<PAIRS>>."


# ======================================================================================= (6) answer line
SRC = "Source: preprod SANDBOX copy, synced 2026-10-13 14:00 UTC."


def test_read_as_line_goes_before_the_source_line():
    text = "Account holds the companies.\n" + SRC
    out = KT.with_read_as_line(text, [("accont", "Account"), ("backgrond chech", "Background Check")])
    assert out == ("Account holds the companies.\nRead as: 'accont' as Account; 'backgrond chech' as Background "
                   "Check.\n" + SRC)
    assert KT.with_read_as_line("Account holds the companies.", [("accont", "Account")]) == \
        "Account holds the companies.\nRead as: 'accont' as Account."
    bold = "Text.\n**Source:** x"
    assert KT.with_read_as_line(bold, [("a1bc", "Abc")]).split("\n") == ["Text.", "Read as: 'a1bc' as Abc.",
                                                                         "**Source:** x"]


def test_read_as_line_is_skipped_when_the_text_states_it_and_for_no_text():
    stated = "I read 'accont' as Account: it holds the companies.\n" + SRC
    assert KT.with_read_as_line(stated, [("accont", "Account")]) == stated
    names_only = "Account holds the companies.\n" + SRC            # names the spelling, not the reading: the line
    assert "Read as: 'accont' as Account." in KT.with_read_as_line(names_only, [("accont", "Account")])
    assert KT.with_read_as_line("", [("accont", "Account")]) == ""
    assert KT.with_read_as_line("Text.", []) == "Text."


def test_read_as_pairs_are_one_per_typed_text_and_leave_people_out():
    spans = [KT.SpellingSpan("accont", 0, 6, "corrected", "Account", (), (("object", "Account"),)),
             KT.SpellingSpan("persn", 7, 12, "ambiguous", None, ("CS Person",))]
    tool = [{"typed": "Accont", "used": "Account", "kind": "object"},          # the same typed text: once
            {"typed": "Completd", "used": "Completed", "kind": "value"},
            {"typed": "Dellta Person", "used": "Delta Person", "kind": "person"},   # a person: the lead sentence
            {"typed": "status__c", "used": "Status__c", "kind": "field"}]      # only the case: no correction
    assert KT.read_as_pairs(spans, tool) == [("accont", "Account"), ("Completd", "Completed")]


def test_question_spelling_pairs_follow_the_answer_kind():
    rec_span = KT.SpellingSpan("Sheduled", 0, 8, "corrected", "Scheduled", (), (("value", "A.B=Scheduled"),))
    kn_span = KT.SpellingSpan("delivrable", 9, 19, "corrected", "Deliverable", (), (("object", "Deliverable__c"),))
    sp = KA.QuestionSpelling("q", knowledge=[kn_span], records=[rec_span, kn_span])
    rec = SimpleNamespace(evidence=[
        SimpleNamespace(ok=True, read_as=[{"typed": "Interviw", "used": "Interview__c", "kind": "object"}]),
        SimpleNamespace(ok=False, read_as=[{"typed": "Bad", "used": "Good", "kind": "field"}]),
        SimpleNamespace(ok=True)])                                              # evidence without the field
    r = SimpleNamespace(tool_calls=[{"ok": True, "read_as": [{"typed": "Outcom", "used": "Outcome__c",
                                                              "kind": "field"}]},
                                    {"ok": False, "read_as": [{"typed": "x1", "used": "y1"}]}])
    said = "3 Scheduled deliverables; each Deliverable has an outcome."
    assert pipeline.spelling_pairs(sp, "records", said, rec, r) == [
        ("Sheduled", "Scheduled"), ("delivrable", "Deliverable"), ("Interviw", "Interview__c")]
    assert pipeline.spelling_pairs(sp, "knowledge", said, rec, r) == [("delivrable", "Deliverable"),
                                                                     ("Outcom", "Outcome__c")]
    assert pipeline.spelling_pairs(sp, "merged", said, rec, r) == [
        ("Sheduled", "Scheduled"), ("delivrable", "Deliverable"), ("Interviw", "Interview__c"), ("Outcom", "Outcome__c")]
    assert pipeline.spelling_final(sp, "Answer.\n" + SRC, "none", rec, r) == "Answer.\n" + SRC
    out = pipeline.spelling_final(sp, "A Deliverable.\n" + SRC, "knowledge", rec, r)
    assert out == "A Deliverable.\nRead as: 'delivrable' as Deliverable; 'Outcom' as Outcome__c.\n" + SRC


def test_a_question_span_no_answer_and_no_call_used_is_never_stated():
    # a Hinglish word read as a stored value ('kaunse' -> Kansas): no call filtered by it and the answer never names
    # it, so the line would state a reading that did not happen
    span = KT.SpellingSpan("kaunse", 0, 6, "corrected", "Kansas", (), (("value", "Account.State__c=Kansas"),))
    sp = KA.QuestionSpelling("kaunse candidates ke interviews scheduled hai", knowledge=[span], records=[span])
    rec = SimpleNamespace(evidence=[SimpleNamespace(ok=True, arguments={"object": "Interview__c"}, applied={})])
    text = "12 interviews are scheduled.\n" + SRC
    assert pipeline.spelling_pairs(sp, "merged", text, rec, SimpleNamespace(tool_calls=[])) == []
    assert pipeline.spelling_final(sp, text, "merged", rec, SimpleNamespace(tool_calls=[])) == text
    used = SimpleNamespace(evidence=[SimpleNamespace(ok=True, arguments={"filters": [{"value": "Kansas"}]}, applied={})])
    assert pipeline.spelling_pairs(sp, "records", text, used) == [("kaunse", "Kansas")]   # a call used it: stated
    by_api = KT.SpellingSpan("delivrable", 0, 10, "corrected", "Deliverable", (), (("object", "Deliverable__c"),))
    knowledge_call = SimpleNamespace(tool_calls=[{"ok": True, "arguments": {"object": "Deliverable__c"}}])
    assert pipeline.spelling_pairs(KA.QuestionSpelling("q", knowledge=[by_api]), "knowledge", "It has 3 fields.",
                                   None, knowledge_call) == [("delivrable", "Deliverable")]


def test_person_lead_sentence_states_a_read_name_the_answer_does_not_name():
    sp = KA.QuestionSpelling("how many interviews did kiloo take")
    tools = SimpleNamespace(question_people=lambda: [{"words": "kiloo", "asked": "kiloo", "read_as": "Kilo",
                                                      "names": ["Kilo Zephyr"], "first_only": True}])
    session = SimpleNamespace(tools=tools)
    text = "Kilo took 3 interviews.\n" + SRC
    lead = P.fill(pipeline.READ_AS_PERSON_LINE, ASKED="kiloo", STORED="Kilo")
    assert lead == "No record is named 'kiloo'; the closest stored name is 'Kilo'."
    assert pipeline.spelling_final(sp, text, "records", None, None, session) == lead + "\n\n" + text
    stated = "No record is named 'kiloo'; I used Kilo. Kilo took 3 interviews."
    assert pipeline.spelling_final(sp, stated, "merged", SimpleNamespace(evidence=[]), None, session) == stated
    assert pipeline.spelling_final(sp, text, "knowledge", None, None, session) == text      # no record side
    plain = SimpleNamespace(tools=SimpleNamespace(question_people=lambda: [{"words": "kilo", "asked": None}]))
    assert pipeline.spelling_final(sp, text, "records", None, None, plain) == text          # nothing was read
    assert P.load("read_as_person_line").placeholders == ("<<ASKED>>", "<<STORED>>")


def test_knowledge_tool_trace_keeps_the_corrected_arguments_only():
    class Reg:
        tools = {"t": None}

        def __init__(self, result):
            self.result = result

        def call(self, name, arguments):
            return {"ok": True, "result": self.result, "error": None, "chunk_ids": []}

    entry = {"arg": "field", "typed": "Outcom", "used": "Outcome__c", "kind": "field", "target": "X.Outcome__c",
             "how": "closest field name"}
    _p, record = KA._execute_tool("t", {}, Reg({"read_as": [entry]}))
    assert record["read_as"] == [entry]
    _p, record = KA._execute_tool("t", {}, Reg({"read_as": {"backgrond": "background"}}))   # a search's reading
    assert "read_as" not in record


def test_switch_on_one_word_name_note_from_the_structured_names():
    from sf_assistant.agent.merge import name_notes, with_name_note

    rec_answer = "No record is named 'Kiloo'; the closest stored name is 'Kilo'. Kilo took 3 mocks."
    q = "how many mocks did kiloo take"
    note = "No record is named 'Kiloo'; the closest stored name is 'Kilo'."
    assert name_notes(rec_answer, q, ("kiloo",)) == [("Kiloo", note)]
    merged = "Kilo took 3 mock interviews.\n\nNo record is named 'Kiloo'; the closest stored name is 'Kilo'."
    assert with_name_note(merged, rec_answer, q, ("kiloo",)) == note + "\n\nKilo took 3 mock interviews."
    assert name_notes("The value 'Support' is not stored in the warehouse.", "is support named", ("kiloo",)) == []
    assert name_notes(rec_answer, q, ("",)) == []


def test_switch_on_name_words_come_from_the_record_tools_people():
    sp = KA.QuestionSpelling("q")
    tools = SimpleNamespace(question_people=lambda: [{"words": "kiloo", "asked": "kiloo"},
                                                     {"words": "delta persn", "asked": "delta persn"},
                                                     {"words": "echo", "asked": None}])
    assert pipeline.spelling_name_words(sp, SimpleNamespace(tools=tools)) == ("kiloo",)
    assert pipeline.spelling_name_words(sp, None) == ()
    rec = SimpleNamespace(triage={"spelling": [
        {"typed": "Sierre", "decision": "corrected", "suggestion": "Sierra", "kind": "person"},
        {"typed": "Kilo Zephir", "decision": "corrected", "suggestion": "Kilo Zephyr", "kind": "person"},
        {"typed": "Statu", "decision": "corrected", "suggestion": "Status", "kind": "field"}]})
    assert pipeline.spelling_name_words(sp, SimpleNamespace(tools=tools), rec) == ("Sierre", "kiloo")
    assert pipeline.spelling_name_words(sp, None, SimpleNamespace(triage={})) == ()

    def broken():
        raise RuntimeError("x")
    assert pipeline.spelling_name_words(sp, SimpleNamespace(tools=SimpleNamespace(question_people=broken))) == ()
