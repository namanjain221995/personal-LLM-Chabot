"""The term resolver's wiring, step (5) person names (docs/resolver_integration.md): the name index and the people
tools read misspelt names through ``catalog.resolver`` instead of the close-surname matcher and the difflib
staff-name check.

With the switch off (``SFA_RESOLVER=off``) ``catalog.resolver`` is None and every result is the one without the resolver
(the other test files and the parity suite cover that; the first test here checks that no spelling-help code runs). With
it on: a name is used only when the resolver decides ``corrected`` (a single first or last name only for doubled letters
or an identical sound), the correction is stated first, an ``ambiguous`` name is never used (a did-you-mean, a missing
name, a not-found list that starts with the resolver's names), and a filter never binds a misspelt person. Every test
sets the switch itself. Fake data only: tests/warehouse_fixture.py and a made-up word list.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from sf_assistant import config as C  # noqa: E402
from sf_assistant.config import ResolverConfig, load_records_config  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H") + \
    tuple(C.RESOLVER_ENV_VARS.values())

#: A made-up stand-in for the English word list: generic words of the test questions only.
WORDS = ("a about all and appear are by count did does for has have host how interview interviews is linked list many "
         "me mock mocks of really records sessions show take taken tell the to was were what where who").split()


def _open(base: Path, people: bool, on: bool):
    pytest.importorskip("duckdb")
    import warehouse_fixture as F
    from sf_assistant.tools.open_records import open_records

    words = base / "words.txt"
    words.write_text("\n".join(WORDS) + "\n", encoding="utf-8")
    tables, knowledge = (F.PEOPLE_TABLES, F.PEOPLE_KNOWLEDGE) if people else (F.TABLES, F.KNOWLEDGE)
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_records(load_records_config(warehouse=str(F.make_warehouse(base, tables=tables)),
                                             today=dt.date(2026, 10, 13)),
                         str(F.make_knowledge(base, knowledge=knowledge)),
                         ResolverConfig(enabled=on, lexicon=(str(words),)))
    assert (t.catalog.resolver is not None) is on, t.resolver_lines
    return t


@pytest.fixture(scope="module")
def ptools_on(tmp_path_factory):
    """The people fixture (Kilo Zephyr, Kilo Quill, Kilo Vortex, Sierra Tango ...) with the spelling help on."""
    t = _open(tmp_path_factory.mktemp("people_on"), people=True, on=True)
    yield t
    t.close()


@pytest.fixture(scope="module")
def ptools_off(tmp_path_factory):
    """The people fixture with the spelling help off (the switch-off twins of the step (5) leftovers)."""
    t = _open(tmp_path_factory.mktemp("people_off"), people=True, on=False)
    yield t
    t.close()


@pytest.fixture(scope="module")
def tools_on(tmp_path_factory):
    """The base fixture (Delta Person, Echo One, Echo Two ...) with the spelling help on."""
    t = _open(tmp_path_factory.mktemp("base_on"), people=False, on=True)
    yield t
    t.close()


def ok(tools, name, args):
    out = tools.call(name, args)
    assert out["ok"], out["error"]
    return out["result"]


def err(tools, name, args) -> str:
    out = tools.call(name, args)
    assert out["ok"] is False and out["result"] is None, out
    return out["error"]


def _people(r):
    return [(p["label"], p["kind"]) for p in r["people"]]


def _spy(monkeypatch, module, names):
    calls: list[str] = []
    for n in names:
        real = getattr(module, n)
        monkeypatch.setattr(module, n, lambda *a, _n=n, _real=real, **k: (calls.append(_n), _real(*a, **k))[1])
    return calls


# ----------------------------------------------------------------------------------------------- switch off
def test_switch_off_runs_no_spelling_help_code(tmp_path, monkeypatch):
    """Off: no helper of the spelling help runs, no result carries ``read_as``, the close-surname step still spots
    'kilo zephir', and a doubled-letter typo is no spot (as before)."""
    import sf_assistant.tools.lookups as L
    import sf_assistant.tools.names as N
    import sf_assistant.tools.people as P
    from sf_assistant.agent.triage import Triage

    t = _open(tmp_path, people=True, on=False)
    try:
        calls = _spy(monkeypatch, N, ["_spelling_spots", "_person_reading", "_staff_spelling", "_did_you_mean_clause",
                                      "_suggested_names", "_full_name_suggestions"])
        calls += _spy(monkeypatch, P, ["_person_reading", "_suggested_names"])
        calls += _spy(monkeypatch, L, ["_suggested_names"])
        e = t.call("count_records", {"object": "Internal_Interview__c",
                                     "filters": [{"field": "Interviewer__c", "op": "eq", "value": "Kilo Zephir"}]})
        assert not e["ok"] and "Close spellings: Kilo Zephyr." in e["error"] and t.evidence[-1].error_code == "E4h"
        spot = t.spot_people("who is kilo zephir")
        assert [(d["words"], d["asked"], d["close"]) for d in spot] == [("kilo zephir", "kilo zephir", ["Kilo Zephyr"])]
        assert t.spot_people("who is kiloo") == [] and t.resolve_person_name("Kiloo") is None
        assert t.resolve_person_name("Kilo Zephir")["match"] == "closest stored name"
        assert t.staff_close_name("Sierre") and not t.staff_close_name("Juliett")
        t.begin_question("who is kilow")
        h = t.object_hints("who is kilow", missing=["kilow"])
        assert ("- 'kilow': no stored employee, user, candidate or lead has this name (about_people gives the closest "
                "stored names)\n") in h + "\n" and "did you mean" not in h
        assert not t.call("about_people", {"names": ["Kiloo"]})["ok"]
        assert ok(t, "about_people", {"names": ["Kilo Zephir"]})["people"][0]["match"] == "closest stored name"
        assert calls == []
        assert not any("read_as" in d for d in t.spot_people("who is kilo zephir"))
        assert "spelling" not in Triage(routed=True, route=True, people=spot).as_dict()
    finally:
        t.close()


# ----------------------------------------------------------------------------------------------- spots
def test_a_doubled_letter_full_name_is_spotted_with_asked_and_close(tools_on):
    q = "how many mock interviews has dellta person taken"
    [d] = tools_on.spot_people(q)
    assert (d["words"], d["asked"], d["close"], d["read_as"], d["first_only"], d["strength"]) == \
        ("dellta person", "dellta person", ["Delta Person"], "Delta Person", False, "strong")
    assert {" ".join(r[2].split()) for r in d["recs"]} == {"Delta Person"}
    assert tools_on.names_person(q)
    tools_on.begin_question(q)
    h = tools_on.object_hints(q)
    people = h.split("People named in the question (code matched these words to stored names; filter with the full "
                     "name):\n", 1)[1].splitlines()
    assert people[0] == ("- 'dellta person' is not a stored name; the closest stored name is 'Delta Person'. If you "
                         "answer for it, say that no record is named 'dellta person' and which stored name you used.")
    assert h.count("is not a stored name") == 1
    assert tools_on.question_people() == [{"words": "dellta person", "names": ["Delta Person"], "first_only": False,
                                           "asked": "dellta person", "read_as": "Delta Person"}]


def test_the_close_surname_step_is_replaced(tools_on, monkeypatch):
    """Flag-on twin of test_tools_records.py::test_shared_first_names_and_misspelled_names_in_the_hints: 'delta persn'
    is still found, through the resolver (the close-surname matcher is never called), with the same hint line."""
    import sf_assistant.tools.names as N
    import sf_assistant.tools.people as P

    def never(*_a, **_k):
        raise AssertionError("_close_surname called with the spelling help on")

    monkeypatch.setattr(N, "_close_surname", never)
    monkeypatch.setattr(P, "_close_surname", never)
    q = "how many mock interviews did delta persn take"
    tools_on.begin_question(q)
    h = tools_on.object_hints(q)
    assert ("- 'delta persn' is not a stored name; the closest stored name is 'Delta Person'. If you answer for it, say "
            "that no record is named 'delta persn' and which stored name you used.") in h
    assert tools_on.question_people() == [{"words": "delta persn", "names": ["Delta Person"], "first_only": False,
                                           "asked": "delta persn", "read_as": "Delta Person"}]
    q = "how many mock interviews did echo really take"     # no close spelling of a surname: not read as one
    tools_on.begin_question(q)
    assert tools_on.question_people()[0]["asked"] is None
    tools_on.begin_question("how many mock interviews has delta person taken")
    assert tools_on.question_people() == [{"words": "delta person", "names": ["Delta Person"], "first_only": False,
                                           "asked": None}]
    r = ok(tools_on, "about_people", {"names": ["Delta Persn"]})
    assert r["people"][0]["match"] == "closest stored name"
    assert "no record is named 'Delta Persn'; the closest stored name is 'Delta Person'" in r["notes"]


def test_a_one_word_doubled_letter_is_a_first_name_spot_stated_first(ptools_on):
    q = "who is kiloo"
    [d] = ptools_on.spot_people(q)
    assert (d["words"], d["asked"], d["close"], d["read_as"], d["first_only"], d["strength"]) == \
        ("kiloo", "kiloo", ["Kilo"], "Kilo", True, "weak")            # a single name needs the names call
    assert [r[2] for r in d["recs"]] == ["Kilo Zephyr", "Kilo Quill", "Kilo Vortex"]
    ptools_on.begin_question(q)
    h = ptools_on.object_hints(q)
    people = h.split("filter with the full name):\n", 1)[1].splitlines()
    assert people[0] == ("- 'kiloo' is not a stored name; the closest stored name is 'Kilo'. If you answer for it, say "
                         "that no record is named 'kiloo' and which stored name you used.")
    assert people[1].startswith("- 'kilo' is only a first name, and several people have it: 'Kilo Zephyr', 'Kilo "
                                "Quill', 'Kilo Vortex'.")
    assert ptools_on.question_people() == [{"words": "kiloo", "names": ["Kilo Zephyr", "Kilo Quill", "Kilo Vortex"],
                                            "first_only": True, "asked": "kiloo", "read_as": "Kilo"}]
    from sf_assistant.agent.nudges import people_nudges

    ppl = ptools_on.question_people()   # a draft that only writes the stored first name is nudged too (decision B)
    assert people_nudges("Kilo took 3 mock interviews.", ppl) and people_nudges("Kilo Zephyr took 3.", ppl)
    assert people_nudges("No record is named 'kiloo'; the closest stored name is 'Kilo'. Kilo took 3.", ppl) == []


def test_an_unstored_full_name_is_no_misspelt_first_name(ptools_on):
    """'killo brown': nobody stored, and 'brown' (no known word here, no catalog word) reads as a surname that no
    stored Kilo has, so 'killo' is not read as the stored first name of another person; the names call can list the
    full name as missing. A known word after it ('took' is none here; 'take' is) or a close surname keeps the spot."""
    assert ptools_on.spot_people("how many mocks did killo brown take") == []
    [d] = ptools_on.spot_people("how many mocks did killo take")
    assert (d["words"], d["read_as"], d["first_only"]) == ("killo", "Kilo", True)
    spots = ptools_on.spot_people("how many mocks did killo zephir take")
    assert [(x["words"], x["read_as"]) for x in spots] == [("killo zephir", "Kilo Zephyr")]
    # a known word after the name is a surname only when the question writes it with a capital letter
    assert [d["words"] for d in ptools_on.spot_people("how many mocks did killo really take")] == ["killo"]
    assert ptools_on.spot_people("how many mocks did Killo Really take") == []


def test_a_one_word_typo_of_another_kind_is_no_spot_but_a_did_you_mean(ptools_on):
    """'kilow' and 'sierre' are close to a stored first name but neither a doubled letter nor the same sound: no spot,
    not resolved; as a missing name the hint line offers the stored names and never uses them."""
    for q, name in (("who is kilow", "Kilow"), ("how many sessions did sierre host", "Sierre")):
        assert ptools_on.spot_people(q) == [], q
        assert ptools_on.resolve_person_name(name) is None, name
    ptools_on.begin_question("who is kilow")
    h = ptools_on.object_hints("who is kilow", missing=["kilow"])
    assert ("- 'kilow': no stored employee, user, candidate or lead has this name (about_people gives the closest "
            "stored names); did you mean 'Kilo' (first name of 3 stored people)? Never answer for a did-you-mean name "
            "as if it were 'kilow': ask which one is meant, or say first that no record is named 'kilow' and which "
            "stored name you used") in h
    ptools_on.begin_question("how many sessions did sierre host")
    h = ptools_on.object_hints("how many sessions did sierre host", missing=["sierre"])
    assert "has this name (about_people gives the closest stored names); did you mean 'Sierra Tango'? Never" in h
    ptools_on.begin_question("is kilo zephyr linked to yankee doodle")       # nothing close: the line as before
    h = ptools_on.object_hints("is kilo zephyr linked to yankee doodle", missing=["Yankee Doodle"])
    assert ("- 'Yankee Doodle': no stored employee, user, candidate or lead has this name (about_people gives the "
            "closest stored names)\n") in h + "\n"


def test_more_misspelt_full_names_in_questions(ptools_on):
    cases = {"who is kilo zephir": ("kilo zephir", ["Kilo Zephyr"], "Kilo Zephyr"),
             "how many sessions did sierre tango host": ("sierre tango", ["Sierra Tango"], "Sierra Tango"),
             # the first and last word of a longer stored name: the stored person is the longer name
             "who is xavier zorne": ("xavier zorne", ["Xavier Yale Zorn"], "Xavier Zorn")}
    for q, (words, close, read_as) in cases.items():
        [d] = ptools_on.spot_people(q)
        assert (d["words"], d["asked"], d["close"], d["read_as"], d["strength"]) == \
            (words, words, close, read_as, "strong"), q
    for q in ("who is kilo", "who is kilo zephyr", "what does session purpose mean for kilo zephyr"):
        assert not any(d.get("read_as") or d.get("asked") for d in ptools_on.spot_people(q)), q
    d = {x["words"]: x["strength"] for x in ptools_on.spot_people("what does session purpose mean for kilo zephyr")}
    assert d == {"session purpose": "weak", "kilo zephyr": "strong"}


def test_one_name_slip_rule():
    from sf_assistant.knowledge.resolver import Vocabulary, build
    from sf_assistant.tools.names import _one_name_slip

    r = build(Vocabulary())
    assert _one_name_slip(r, "kiloo", "Kilo") and _one_name_slip(r, "zorrvan", "Zorvan")          # doubled letters
    assert _one_name_slip(r, "kweena", "Kwina") and _one_name_slip(r, "zaxen", "Zacksen")         # the same sound
    assert not _one_name_slip(r, "talvon", "Talvin") and not _one_name_slip(r, "kilow", "Kilo")
    assert not _one_name_slip(r, "kilo", "Kilo") and not _one_name_slip(r, "kilo zephir", "Kilo Zephyr")


# ------------------------------------------------------------------------------------- the names call's names
def test_resolve_person_name_twins(ptools_on):
    """Flag-on twins of test_tools_records.py::test_resolve_person_name_bypasses_vocab_filter and
    ::test_resolve_person_name_general_rules: the same answers, plus the corrected single names."""
    t = ptools_on
    assert t.spot_people("who is session") == []
    r = t.resolve_person_name("Session")
    assert r is not None and r["first_only"] and [x[2] for x in r["recs"]] == ["Session Purpose"]
    assert t.resolve_person_name("Kilo’s")["words"] == "kilo"
    close = t.resolve_person_name("Kilo Zephir")
    assert close["asked"] == "kilo zephir" and close["close"] == ["Kilo Zephyr"]
    assert close["match"] == "closest stored name"
    assert t.resolve_person_name("Yankee Nobody") is None and t.resolve_person_name("the") is None
    for asked in ("Zephyr Kilo", "Kilo Z.", "K. Zephyr", "Kilo Q Zephyr", "Kilo Zephry"):
        r = t.resolve_person_name(asked)
        assert r is not None and r["close"] == ["Kilo Zephyr"] and r["match"] == "closest stored name", asked
    r = t.resolve_person_name("Mr. Kilo")               # a title and a first name: partial, never a correction
    assert r["partial"] == "kilo" and r["match"] == "partial first name" and "read_as" not in r
    r = t.resolve_person_name("Kilo Smith")             # an unknown surname: partial as before
    assert r["partial"] == "kilo" and "read_as" not in r
    r = t.resolve_person_name("Zephyr")
    assert r["match"] == "last name" and [x[2] for x in r["recs"]] == ["Kilo Zephyr"]
    assert t.resolve_person_name("Client Co") is None and t.company_name("Client Co")
    r = t.resolve_person_name("Kiloo")                  # a doubled letter: the stored first name
    assert (r["match"], r["asked"], r["close"], r["read_as"], r["first_only"]) == \
        ("closest stored name", "kiloo", ["Kilo"], "Kilo", True)
    assert [x[2] for x in r["recs"]] == ["Kilo Zephyr", "Kilo Quill", "Kilo Vortex"]
    r = t.resolve_person_name("Zephhyr")                # a last name alone stays a last-name match (no overrule)
    assert (r["match"], r["close"], r["read_as"]) == ("closest stored last name", ["Zephyr"], "Zephyr")
    r = t.resolve_person_name("Uniform Vael")           # the same name written another way: _close_people, as before
    assert (r["match"], r["close"]) == ("closest stored name", ["Uniform Vale"]) and "read_as" not in r
    for asked, stored in (("Uniforrm Vael", "Uniform Vale"), ("Sierre Tangoo", "Sierra Tango")):   # no exact word
        r = t.resolve_person_name(asked)
        assert (r["match"], r["close"], r["read_as"], r["first_only"]) == \
            ("closest stored name", [stored], stored, False), asked


def test_staff_close_name_twin(ptools_on):
    t = ptools_on
    assert t.staff_close_name("Sierre") and t.staff_close_name("Siera")      # Sierra Tango is an employee
    assert not t.staff_close_name("Juliett")                                # a candidate's and a lead's name
    assert not t.staff_close_name("Einstein") and not t.staff_close_name("Sierra Tango")
    assert not t.staff_close_name("Sierra") and not t.staff_close_name("Kilo")   # stored names are not close ones


def test_a_full_name_did_you_mean_is_never_used(ptools_on, monkeypatch):
    """'Kelta Person' is near 'Delta Person' only beyond a correction: not resolved (the triage lists it as missing),
    the hint offers it, about_people does not find it and its close list starts with the resolver's name."""
    t = ptools_on
    assert t.resolve_person_name("Kelta Person") is None
    q = "who is kelta person"
    assert t.spot_people(q) == []
    t.begin_question(q)
    assert "did you mean 'Delta Person'? Never answer" in t.object_hints(q, missing=["Kelta Person"])
    t.begin_question(q)
    monkeypatch.setattr(t, "_g2", lambda call, o, v: [("Foxtrot Person", 3)] if o == "Recruiter__c" else [])
    out = t.call("about_people", {"names": ["Kelta Person"]})
    assert not out["ok"] and "Close spellings: Delta Person, Foxtrot Person." in out["error"]
    nf = t.evidence[-1].not_found[0]
    assert t.evidence[-1].error_code == "E15a" and nf["close"][:2] == ["Delta Person", "Foxtrot Person"]


# ----------------------------------------------------------------------------------------------- about_people
def test_about_people_twins(ptools_on):
    """Flag-on twins of test_tools_records.py's about_people name tests, plus the corrected single names."""
    t = ptools_on
    r = ok(t, "about_people", {"names": ["Kilo Zephir"]})
    assert _people(r) == [("Kilo Zephyr", "employee")] and r["people"][0]["match"] == "closest stored name"
    assert "no record is named 'Kilo Zephir'; the closest stored name is 'Kilo Zephyr'" in r["notes"]
    r = ok(t, "about_people", {"names": ["zephyr kilo"]})
    assert r["people"][0]["match"] == "closest stored name" and r["people"][0]["label"] == "Kilo Zephyr"
    out = t.call("about_people", {"names": ["mr kilo"]})
    assert not out["ok"] and "Stored people with the first name 'kilo': Kilo Zephyr" in out["error"]
    assert "Kilo Zephyr" in t.evidence[-1].not_found[0]["close"]
    r = ok(t, "about_people", {"names": ["zephyr"]})
    assert r["people"][0]["match"] == "last name" and r["people"][0]["label"] == "Kilo Zephyr"
    r = ok(t, "about_people", {"names": ["Kiloo"]})
    assert _people(r) == [("Kilo Zephyr", "employee"), ("Kilo Quill", "candidate"), ("Kilo Vortex", "lead")]
    assert {p["match"] for p in r["people"]} == {"closest stored name"}
    assert ("no record is named 'Kiloo'; the closest stored names are 'Kilo Zephyr', 'Kilo Quill', 'Kilo Vortex'"
            in r["notes"])
    r = ok(t, "about_people", {"names": ["Lima Nobell"]})
    assert _people(r) == [("Lima Nobel", "employee")]
    assert "no record is named 'Lima Nobell'; the closest stored name is 'Lima Nobel'" in r["notes"]
    out = t.call("about_people", {"names": ["Kilow"]})                    # a did-you-mean: not found
    assert not out["ok"] and t.evidence[-1].error_code == "E15a"
    assert t.evidence[-1].not_found[0]["close"][:3] == ["Kilo Zephyr", "Kilo Quill", "Kilo Vortex"]
    out = t.call("about_people", {"names": ["Kiloo"], "object": "Lead"})   # limited to the asked object
    assert out["ok"] and _people(out["result"]) == [("Kilo Vortex", "lead")]


def test_people_answer_states_the_correction_first(ptools_on):
    from sf_assistant.agent.people_answer import people_answer

    r = ok(ptools_on, "about_people", {"names": ["Uniform Vael"]})
    assert people_answer(r).startswith("No record is named 'Uniform Vael'; the closest stored name is 'Uniform Vale'.")


def test_a_failing_resolver_never_loses_a_stored_name(ptools_on, monkeypatch):
    """The spelling help is optional: a resolver that raises leaves every exact match, today's close-name rules and
    about_people working."""
    class Broken:
        def resolve_term(self, *_a, **_k):
            raise RuntimeError("broken")

        spell_correct = resolve_term

    t = ptools_on
    monkeypatch.setattr(t.catalog, "resolver", Broken())
    assert [d["words"] for d in t.spot_people("who is kilo zephyr and sierra tango")] == ["kilo zephyr", "sierra tango"]
    assert t.resolve_person_name("Kilo Zephyr")["match"] == "full name"
    assert t.resolve_person_name("Kilo Zephir")["match"] == "closest stored name"      # _close_people
    assert t.resolve_person_name("Kiloo") is None and t.resolve_person_name("Mr. Kilo")["partial"] == "kilo"
    assert t.staff_close_name("Sierre") is False
    r = ok(t, "about_people", {"names": ["Kilo Zephir"]})
    assert r["people"][0]["match"] == "closest stored name"
    assert not t.call("about_people", {"names": ["Kelta Person"]})["ok"] and t.evidence[-1].error_code == "E15a"
    t.begin_question("who is kilow")
    assert "did you mean" not in t.object_hints("who is kilow", missing=["kilow"])


# ----------------------------------------------------------------------------------------------- filters
def test_a_filter_never_binds_a_misspelt_person(tools_on):
    """A person name inside a filter is never bound, whatever the resolver decides: E4h, and the count never runs."""
    for value in ("Dellta Person", "Delta Persn"):
        for field in ("Interviewer__c", "Interviewer__c.Name"):
            tools_on.begin_question()
            e = err(tools_on, "count_records", {"object": "Internal_Interview__c",
                                                "filters": [{"field": field, "op": "eq", "value": value}]})
            assert e.startswith(f"no Employee (Recruiter__c) record is named '{value}'."), e
            ev = tools_on.evidence[-1]
            assert ev.error_code == "E4h"
            assert not any("Internal_Interview__c" in s.sql for s in ev.statements)


def _e4h(t, field: str, value: str, op: str = "eq") -> tuple[str, dict]:
    """count_records on Internal_Interview__c filtered on ``field`` = ``value``: (the E4h text, its not_found entry);
    the main statement never ran."""
    t.begin_question()
    e = err(t, "count_records", {"object": "Internal_Interview__c",
                                 "filters": [{"field": field, "op": op, "value": value}]})
    ev = t.evidence[-1]
    assert ev.error_code == "E4h", e
    assert not any("Internal_Interview__c" in s.sql for s in ev.statements)
    return e, ev.not_found[0]


def test_e4h_close_spellings_start_with_the_resolver_names(ptools_on, ptools_off, monkeypatch):
    """Step (5) leftover: an unknown person name in a filter (E4h) lists the stored names the resolver offers first
    among the close spellings, in the error text and in ``not_found`` "close", as E15a does; the code stays E4h and
    the count never runs. Off: the G2 order (how, then name) as before."""
    g2 = [("Della Parson", 3), ("Delta Person", 3)]   # G2's own order for a made-up second close name
    for t, order in ((ptools_off, ["Della Parson", "Delta Person"]), (ptools_on, ["Delta Person", "Della Parson"])):
        with monkeypatch.context() as mp:
            mp.setattr(t, "_g2", lambda call, o, v: list(g2) if o == "Recruiter__c" else [])
            for field in ("Interviewer__c", "Interviewer__c.Name"):
                e, nf = _e4h(t, field, "Dellta Person")
                assert e.startswith("no Employee (Recruiter__c) record is named 'Dellta Person'. Close spellings: "
                                    + ", ".join(order) + "."), e
                assert nf["close"] == order and nf["match"] == "exact"
    # the resolver finds what G2 misses (a one-word slip of a last name), listed and never used
    e, nf = _e4h(ptools_off, "Interviewer__c", "Persn")
    assert nf["close"] == [] and "No name contains it and no name is close." in e
    e, nf = _e4h(ptools_on, "Interviewer__c", "Persn")
    assert nf["close"] == ["Delta Person", "Foxtrot Person"] and "Close spellings: Delta Person, Foxtrot Person." in e


def test_e4h_resolver_names_are_records_of_the_filter_targets(ptools_on):
    """Only the filter's target objects are offered: 'Kiloo' on an Employee lookup lists the employee Kilo, never the
    candidate or the lead with that first name; 'Lima Nobell' on a User lookup lists the user."""
    _e, nf = _e4h(ptools_on, "Interviewer__c", "Kiloo")
    assert nf["close"] == ["Kilo Zephyr"]
    e, nf = _e4h(ptools_on, "CreatedById", "Lima Nobell")
    assert nf["close"] == ["Lima Nobel"] and e.startswith("no User record is named 'Lima Nobell'. Close spellings: "
                                                          "Lima Nobel.")
    _e, nf = _e4h(ptools_on, "Interviewer__c.Name", "Kiloo", op="contains")
    assert nf["close"] == ["Kilo Zephyr"] and nf["match"] == "contains"


def test_e4h_with_a_failing_resolver_keeps_the_g2_list(ptools_on, monkeypatch):
    class Broken:
        def resolve_term(self, *_a, **_k):
            raise RuntimeError("broken")

    monkeypatch.setattr(ptools_on.catalog, "resolver", Broken())
    e, nf = _e4h(ptools_on, "Interviewer__c", "Dellta Person")
    assert nf["close"] == ["Delta Person"] and "Close spellings: Delta Person." in e


# ------------------------------------------------------------------------------- the several-people nudge's data
def test_a_corrected_shared_first_name_carries_the_several_people_data(ptools_on, ptools_off):
    """Step (5) leftover, the data the several-people nudge needs (``agent/nudges.people_nudges``, AGENT lane): a
    misspelt first name the resolver reads as a stored first name that several people have is a ``question_people``
    entry with ``first_only``, ``asked`` (the typed word), ``read_as`` (the stored first name) and ``names`` (the full
    names of the people with that first name, one per person), whether the hints spotted it or the names call gave it
    (``begin_question(people=...)``); a corrected first name of one person has one name. Off: the misspelt word is no
    person, and the correct spelling gives the same names without ``asked`` or ``read_as``."""
    kilos = ["Kilo Zephyr", "Kilo Quill", "Kilo Vortex"]
    q = "how many sessions did kiloo host"
    want = [{"words": "kiloo", "names": kilos, "first_only": True, "asked": "kiloo", "read_as": "Kilo"}]
    ptools_on.begin_question(q)
    assert ptools_on.question_people() == want
    given = ptools_on.resolve_person_name("Kiloo")
    assert (given["first_only"], given["asked"], given["read_as"]) == (True, "kiloo", "Kilo")
    ptools_on.begin_question(q, people=[given])                       # the names call's path (the session triage)
    assert ptools_on.question_people() == want
    ptools_on.begin_question("how many sessions did siera host")      # one person has the corrected first name
    assert ptools_on.question_people() == [{"words": "siera", "names": ["Sierra Tango"], "first_only": True,
                                            "asked": "siera", "read_as": "Sierra"}]
    ptools_off.begin_question(q)
    assert ptools_off.question_people() == []
    ptools_off.begin_question("how many sessions did kilo host")
    assert ptools_off.question_people() == [{"words": "kilo", "names": kilos, "first_only": True, "asked": None}]


# ------------------------------------------------------------------------------------------ _close_surname
def test_close_surname_runs_only_with_the_switch_off(ptools_on, ptools_off, monkeypatch):
    """``_close_surname`` (the close-surname step) stays for the switch-off path only: off, the hint spots and
    about_people use it; on, no entry point calls it (the resolver replaces it)."""
    import sf_assistant.tools.names as N
    import sf_assistant.tools.people as P

    calls = _spy(monkeypatch, N, ["_close_surname"])
    calls_p = _spy(monkeypatch, P, ["_close_surname"])
    assert [d["asked"] for d in ptools_off.spot_people("who is kilo zephir")] == ["kilo zephir"]
    assert calls and not calls_p
    assert ok(ptools_off, "about_people", {"names": ["Kilo Zephir"]})["people"][0]["match"] == "closest stored name"
    assert calls_p
    calls.clear()
    calls_p.clear()
    for q in ("who is kilo zephir", "how many mock interviews did delta persn take", "how many sessions did kiloo host"):
        ptools_on.begin_question(q)
        ptools_on.spot_people(q)
        ptools_on.object_hints(q)
        ptools_on.question_people()
    assert ptools_on.resolve_person_name("Kilo Zephir")["match"] == "closest stored name"
    r = ok(ptools_on, "about_people", {"names": ["Kilo Zephir", "Delta Persn"]})
    assert {p["match"] for p in r["people"]} == {"closest stored name"}
    assert {p["asked"] for p in r["people"]} == {"Kilo Zephir", "Delta Persn"}
    _e4h(ptools_on, "Interviewer__c", "Kilo Zephir")
    assert calls == [] and calls_p == []


# ----------------------------------------------------------------------------------------------- the triage
class _Names:
    """A scripted model for RecordSession.triage: the route call says RECORDS, the names call gives ``names``."""

    def __init__(self, names: str):
        self.names = names

    def chat(self, messages, tools=None, max_tokens=None, **_):
        from sf_assistant.agent import record_prompts as rp

        content = messages[0].get("content")
        if content == rp.NAMES_SYSTEM:
            return {"content": self.names, "tool_calls": [], "usage": {}}
        if content == rp.ROUTE_SYSTEM:
            return {"content": "RECORDS", "tool_calls": [], "usage": {}}
        raise AssertionError("only the route and names calls run in a triage")


def test_triage_with_the_spelling_help(ptools_on):
    from sf_assistant.agent.record_session import RecordSession

    cfg = C.load_config(base_url="http://localhost:8000/v1", api_key="test-key", max_tool_steps=6)

    def triage(q, names):
        return RecordSession(ptools_on, cfg, llm=_Names(names)).triage(q)

    tri = triage("how many sessions did kiloo host", "kiloo")
    assert [(p["words"], p.get("read_as")) for p in tri.people] == [("kiloo", "Kilo")] and tri.missing == []
    assert tri.people_data() and tri.routed is True
    assert tri.as_dict()["spelling"] == [{"typed": "kiloo", "decision": "corrected", "suggestion": "Kilo",
                                          "kind": "person"}]
    tri = triage("how many sessions did sierre tango host", "NONE")      # a strong spot needs no names call
    assert [(p["words"], p["read_as"]) for p in tri.people] == [("sierre tango", "Sierra Tango")]
    tri = triage("who is kelta person", "Kelta Person")                  # a did-you-mean: missing, never a person
    assert tri.people == [] and tri.missing == ["Kelta Person"] and not tri.people_data()
    assert "spelling" not in tri.as_dict()
    tri = triage("who is kilo zephyr", "Kilo Zephyr")
    assert [p["words"] for p in tri.people] == ["kilo zephyr"] and "spelling" not in tri.as_dict()
    assert set(tri.as_dict()) == {"routed", "route", "names", "people", "missing", "people_data", "seconds"}


# ------------------------------------------------------------------------------------- live (opt-in, counts only)
_LIVE = Path(os.environ.get("SFA_LIVE_SNAPSHOT") or C.DEFAULT_WAREHOUSE)


@pytest.mark.skipif(os.environ.get("OKEVAL_RECORDS_LIVE") != "1" or not _LIVE.is_file()
                    or not Path("/usr/share/dict/american-english").is_file(),
                    reason="live: set OKEVAL_RECORDS_LIVE=1 with the local snapshot and the system word lists")
def test_live_doubled_letter_surnames_are_read_as_the_stored_name():
    """On the real snapshot: a stored employee's full name with one letter of the surname doubled is read as that
    name (resolve_person_name and spot_people), a stored name stays an exact match, and no name is ever read as a
    different person. Counts only: nothing is printed."""
    import random

    from sf_assistant.tools.open_records import open_records
    from sf_assistant.tools.record_base import name_key

    t = open_records(load_records_config(warehouse=str(_LIVE), today=dt.date(2026, 10, 13), tz="America/New_York"),
                     str(C.KNOWLEDGE_DIR), ResolverConfig(enabled=True))
    try:
        assert t.catalog.resolver is not None, t.resolver_lines
        full, _fl, first = t._name_index()
        staff = sorted({name_key(r[2]) for recs in full.values() for r in recs
                        if r[0] == "Recruiter__c" and len(name_key(r[2]).split()) == 2
                        and all(w.isalpha() and len(w) >= 4 for w in name_key(r[2]).split())})
        rng = random.Random(5)
        sample = rng.sample(staff, min(60, len(staff)))
        read = wrong = exact = 0
        for key in sample:
            fn, ln = key.split()
            i = rng.randrange(1, len(ln))
            typo = f"{fn} {ln[:i]}{ln[i - 1]}{ln[i:]}" if ln[i - 1] != ln[i] else f"{fn} {ln}{ln[-1]}"
            r = t.resolve_person_name(key)
            exact += r is not None and r["match"] == "full name"
            if name_key(typo) in full:
                continue
            r = t.resolve_person_name(typo)
            if r is not None and r.get("asked"):
                if {name_key(c) for c in r["close"]} == {key} or key in {name_key(x[2]) for x in r["recs"]}:
                    read += 1
                else:
                    wrong += 1
        assert exact == len(sample)
        assert wrong == 0 and read >= 0.9 * len(sample), (read, wrong, len(sample))
    finally:
        t.close()
