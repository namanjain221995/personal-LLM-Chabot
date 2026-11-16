"""The evaluation runner (sf_assistant/evaluation: sets, expected_data, autograde, people_grader, runner, __main__).

Offline: no model call. Every LLM is :class:`sf_assistant.agent.mock_llm.MockClient`; the question sets are synthetic
(fake names, P-codes) in a temporary private folder, so the tests run on a clean checkout without ``.private``.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from sf_assistant.evaluation import autograde, expected_data, people_grader
from sf_assistant.evaluation import __main__ as eval_main
from sf_assistant.evaluation.autograde import grade
from sf_assistant.evaluation.sets import (EvalItem, SETS_FILE, load_items, load_sets, parse_yaml, plan,
                                          read_question_file, sample_indices)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
SOURCE = "\n\nSource: preprod sandbox copy, synced 2026-10-13 14:56 UTC."


# ------------------------------------------------------------------------------------------------ sets.yaml
def test_parse_yaml_subset():
    text = textwrap.dedent("""\
        # comment
        version: 1
        name: "a # not a comment"
        other: 'x'   # trailing comment
        flags: [merged, records]
        flow: {a: 1, b: true}
        nested:
          one:
            n: 10
            f: 0.5
            empty:
          items:
            - a
            - 2
        """)
    data = parse_yaml(text)
    assert data == {"version": 1, "name": "a # not a comment", "other": "x", "flags": ["merged", "records"],
                    "flow": {"a": 1, "b": True}, "nested": {"one": {"n": 10, "f": 0.5, "empty": None},
                                                            "items": ["a", 2]}}
    with pytest.raises(ValueError):
        parse_yaml("a: 1\n\tb: 2\n")
    with pytest.raises(ValueError):
        parse_yaml("a: 1\na: 2\n")
    with pytest.raises(ValueError):
        parse_yaml("just text\n")


def test_package_sets_yaml_holds_paths_only(tmp_path):
    cfg = load_sets(private_dir=tmp_path, home=tmp_path)
    assert list(cfg.order) == ["lookup", "golden", "easy", "people_dev", "people_holdout", "controls"]
    assert {s.grader for s in cfg.sets.values()} == {"lookup", "golden", "route", "people", "control"}
    assert sum(s.quick for s in cfg.sets.values()) == 35
    assert cfg.today == "2026-10-13" and cfg.results_dir.startswith(".private/runs/")
    for spec in cfg.sets.values():   # every file in the private folder, given relative to it
        for rel in (spec.questions, spec.expected):
            assert rel is None or (not rel.startswith("/") and ".." not in rel)
    values = [v for line in SETS_FILE.read_text(encoding="utf-8").splitlines()
              if ":" in line and not line.lstrip().startswith("#") for v in [line.split(":", 1)[1].split("#")[0]]]
    assert all("@" not in v for v in values)


# ------------------------------------------------------------------------------------------------ a fake private set
def _fake_private(tmp: Path, sizes: dict[str, int] | None = None) -> Path:
    """Question / expected files shaped like the real ones (fake names only)."""
    sizes = sizes or {"lookup": 40, "golden": 24, "easy": 14, "dev": 44, "holdout": 19, "controls": 16}
    priv = tmp / ".private"
    (priv / "okeval_run/lookup_eval/people").mkdir(parents=True)
    (priv / "questions").mkdir(parents=True)
    lk = [{"n": i, "q": f"How many mocks did Person{i} take?", "field": "Interviewer__c", "score": [i],
           "accept": "correct: the count.", "expected": {"main_count": i}, "sql": {}} for i in range(1, sizes["lookup"] + 1)]
    (priv / "okeval_run/lookup_eval/expected.json").write_text(json.dumps(lk), encoding="utf-8")
    (priv / "okeval_run/lookup_eval/questions.txt").write_text(
        "".join(f"# [{e['n']:02d} A tag]\n{e['q']}\n" for e in lk), encoding="utf-8")
    gold = [{"n": i, "question": f"How many records of kind {i}?", "tool": "count_records", "args": {}, "expect": [i],
             "tol": 0} for i in range(1, sizes["golden"] + 1)]
    (priv / "questions/records_golden.json").write_text(json.dumps(gold), encoding="utf-8")
    (priv / "questions/records.txt").write_text("".join(f"# [EASY] expect: {g['n']}\n{g['question']}\n" for g in gold),
                                                encoding="utf-8")
    (priv / "questions/easy.txt").write_text("".join(f"# expect: x\nWhat does field {i} mean?\n"
                                                     for i in range(1, sizes["easy"] + 1)), encoding="utf-8")
    people, k = [], 0
    for name, n in (("dev", sizes["dev"]), ("holdout", sizes["holdout"])):
        lines = []
        for _ in range(n):
            k += 1
            people.append({"id": f"P{k:02d}", "set": name, "category": "identity", "q": f"who is person {k}",
                           "must": [f"Person {k} is stored"], "partial": [], "wrong": [], "expected": {}, "sql": {}})
            lines.append(f"# [P{k:02d} identity] fake\nwho is person {k}\n")
        (priv / f"okeval_run/lookup_eval/people/{name}.txt").write_text("".join(lines), encoding="utf-8")
    controls = [{"id": f"C{i:02d}", "category": "control", "q": f"what does lookup {i} mean",
                 "must": [f"Field_{i}__c: the meaning"], "wrong": [], "expected": {"route": "NO_RECORDS"}}
                for i in range(1, sizes["controls"] + 1)]
    (priv / "okeval_run/lookup_eval/people/controls.txt").write_text(
        "".join(f"# [{c['id']} control] -\n{c['q']}\n" for c in controls), encoding="utf-8")
    (priv / "okeval_run/lookup_eval/people/expected.json").write_text(
        json.dumps({"today": "2026-10-13", "questions": people, "controls": controls}), encoding="utf-8")
    return priv


def test_levels_and_modes_plan_the_round_sizes(tmp_path):
    cfg = load_sets(private_dir=_fake_private(tmp_path), home=tmp_path)
    assert len(plan(cfg, "all", None, "quick")) == 35
    assert len(plan(cfg, "all", None, "standard")) == 157
    assert len(plan(cfg, "all", None, "full")) == 284
    assert {m for _, m in plan(cfg, "all", None, "standard")} == {"merged"}
    assert len(plan(cfg, "people_dev", "both", "standard")) == 88
    assert plan(cfg, "easy", "records", "standard") == []          # easy has no records mode
    quick = [it.id for it, _ in plan(cfg, "lookup", None, "quick")]
    assert quick == [f"Q{i:02d}" for i in range(1, 41, 4)] == [it.id for it, _ in plan(cfg, "lookup", None, "quick")]
    with pytest.raises(ValueError):
        plan(cfg, "nope", None, "quick")
    with pytest.raises(ValueError):
        plan(cfg, "all", "sideways", "quick")


def test_items_get_ids_and_their_expected_entries(tmp_path):
    cfg = load_sets(private_dir=_fake_private(tmp_path, {"lookup": 3, "golden": 2, "easy": 2, "dev": 2, "holdout": 1,
                                                       "controls": 1}), home=tmp_path)
    lookup = load_items(cfg.sets["lookup"], cfg)
    assert [i.id for i in lookup] == ["Q01", "Q02", "Q03"] and lookup[2].expected["n"] == 3
    gold = load_items(cfg.sets["golden"], cfg)
    assert [i.id for i in gold] == ["G01", "G02"] and gold[1].expected["expect"] == [2]
    easy = load_items(cfg.sets["easy"], cfg)
    assert [i.id for i in easy] == ["E01", "E02"] and easy[0].expected is None
    hold = load_items(cfg.sets["people_holdout"], cfg)
    assert [i.id for i in hold] == ["P03"] and hold[0].expected["q"] == "who is person 3"
    assert load_items(cfg.sets["controls"], cfg)[0].expected["id"] == "C01"


def test_read_question_file_and_fixed_sample(tmp_path):
    f = tmp_path / "q.txt"
    f.write_text("# head\n\n# [P07 x] tag\nfirst?\nsecond?\n", encoding="utf-8")
    assert read_question_file(f) == [("first?", "# [P07 x] tag"), ("second?", "")]
    assert sample_indices(40, 10) == [0, 4, 8, 12, 16, 20, 24, 28, 32, 36]
    assert sample_indices(19, 5) == [0, 3, 7, 11, 15] and sample_indices(3, 5) == [0, 1, 2] and sample_indices(9, 0) == []


# ------------------------------------------------------------------------------------------------ expected SQL re-run
def _db(path: Path, rows: list[tuple]) -> Path:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(path))
    con.execute('CREATE TABLE "Internal_Interview__c" ("Id" VARCHAR, "Status__c" VARCHAR, "Interviewer__c" VARCHAR, '
                '"Trainers" VARCHAR)')
    con.executemany('INSERT INTO "Internal_Interview__c" VALUES (?, ?, ?, ?)', rows)
    con.close()
    return path


ROWS = [("i1", "Completed", "e1", "Bravo, Alpha"), ("i2", "Completed", "e1", None), ("i3", "Cancelled", "e1", None),
        ("i4", None, "e2", None)]
BY_STATUS = ('SELECT coalesce("Status__c", \'(blank)\') AS k, COUNT(*) FROM main."Internal_Interview__c" '
             'WHERE "Interviewer__c" = \'e1\' GROUP BY 1 ORDER BY 2 DESC, 1')
LOOKUP_ENTRY = {"n": 1, "q": "How many mocks did Delta Person take, by status?", "field": "Interviewer__c",
                "accept": "correct: 3 split by Status__c on Interviewer__c (field named). wrong: 0.",
                "score": [3, 2, 1], "expected": {"main_by_status": {"Completed": 2, "Cancelled": 1},
                                                 "main_count": 3, "wrong_lookup_recruiter": 5},
                "sql": {"main_by_status": BY_STATUS,
                        "main_count": 'SELECT COUNT(*) FROM main."Internal_Interview__c" WHERE "Interviewer__c" = \'e1\''}}


def test_expected_sql_is_rerun_read_only_and_maps_changed_numbers(tmp_path):
    con = expected_data.open_readonly(str(_db(tmp_path / "w.duckdb", ROWS)))
    chk = expected_data.check_entry(con, LOOKUP_ENTRY, "lookup")
    assert chk.status == "same" and chk.keys == 2 and chk.unverified == ["wrong_lookup_recruiter"]
    assert expected_data.refresh_numbers(["3", "2", "1"], chk) == (["3", "2", "1"], [], [])
    with pytest.raises(Exception):   # read-only
        con.execute('DELETE FROM main."Internal_Interview__c"')
    con.close()
    # a refresh added a completed mock: 2 -> 3 completed, 3 -> 4 in total (the sum and the count follow)
    con = expected_data.open_readonly(str(_db(tmp_path / "w2.duckdb", ROWS + [("i5", "Completed", "e1", None)])))
    chk = expected_data.check_entry(con, LOOKUP_ENTRY, "lookup")
    assert chk.status == "changed" and set(chk.changed) == {"main_by_status", "main_count"}
    keep, stale, notes = expected_data.refresh_numbers(["3", "2", "1"], chk)
    assert (keep, stale) == (["4", "3", "1"], [])          # total 3 -> 4, completed 2 -> 3, cancelled 1 stays
    assert notes == ["data changed: 3 -> 4", "data changed: 2 -> 3"]
    # a {label: count} fact is aligned by label: a new label (e2's blank status) leaves the old counts as they are
    entry = dict(LOOKUP_ENTRY, expected={"main_by_status": {"Completed": 2, "Cancelled": 1}}, sql={
        "main_by_status": BY_STATUS.replace("\'e1\'", "\'e1\' OR \"Interviewer__c\" = \'e2\'")})
    chk = expected_data.check_entry(con, entry, "lookup")
    assert chk.status == "changed"
    assert expected_data.refresh_numbers(["2", "1"], chk) == (["3", "1"], [], ["data changed: 2 -> 3"])
    # a number that both stayed and changed (two statuses with 1, one of them now 3) cannot be re-derived
    entry = dict(LOOKUP_ENTRY, expected={"main_by_status": {"Completed": 1, "Cancelled": 1}},
                 sql={"main_by_status": BY_STATUS})
    chk = expected_data.check_entry(con, entry, "lookup")
    assert chk.status == "changed" and expected_data.refresh_numbers(["1"], chk)[1] == ["1"]
    # a label that is gone: its count is suspect and the label is gone text
    entry = dict(LOOKUP_ENTRY, expected={"main_by_status": {"Completed": 4, "Rescheduled": 7}},
                 sql={"main_by_status": BY_STATUS})
    chk = expected_data.check_entry(con, entry, "lookup")
    assert expected_data.refresh_numbers(["7"], chk)[1] == ["7"] and "Rescheduled" in chk.gone_text
    assert expected_data.stale_texts(["Rescheduled", "Completed"], chk) == ["Rescheduled"]
    con.close()


def test_people_sql_shapes_are_rechecked(tmp_path):
    con = expected_data.open_readonly(str(_db(tmp_path / "w.duckdb", ROWS)))
    entry = {"expected": {"by_status": {"Completed": 2, "Cancelled": 1},
                          "people": {"Internal_Interview__c": [["i1", "Completed"], ["i2", "Completed"]]},
                          "refs": {"Recruiter__c:e1": {"business": [["Internal_Interview__c", "Interviewer__c", 3]],
                                                       "system_top": []}},
                          "links": [["same record", "a link", 3]],
                          "trainers": [["Alpha, Bravo"]]},
             "sql": {"by_status": BY_STATUS,
                     "people": ['SELECT "Id", "Status__c" FROM main."Internal_Interview__c" WHERE "Status__c" = '
                                '\'Completed\' ORDER BY 1'],
                     "refs": {"Recruiter__c:e1": {"business": 'SELECT \'Internal_Interview__c\', \'Interviewer__c\', '
                                                              'COUNT(*) FROM main."Internal_Interview__c" WHERE '
                                                              '"Interviewer__c" = \'e1\'', "system": None}},
                     "links": {"positive_statements": ['SELECT COUNT(*) FROM main."Internal_Interview__c" WHERE '
                                                       '"Interviewer__c" = \'e1\''], "method": "pairs"},
                     "trainers": 'SELECT "Trainers" FROM main."Internal_Interview__c" WHERE "Trainers" IS NOT NULL'}}
    chk = expected_data.check_entry(con, entry, "people")
    assert chk.status == "same", chk.summary()   # string_agg lists compare as sets ("Bravo, Alpha" = "Alpha, Bravo")
    entry["expected"]["by_status"]["Completed"] = 9
    assert expected_data.check_entry(con, entry, "people").status == "changed"
    with pytest.raises(ValueError):
        expected_data._run(con, 'SELECT 1; DROP TABLE x')
    con.close()


def test_the_live_warehouse_is_refused():
    from sf_assistant.engine.types import RecordsUnavailable

    with pytest.raises(RecordsUnavailable):
        expected_data.open_readonly("/data/warehouse.duckdb")


# ------------------------------------------------------------------------------------------------ graders
def _item(grader: str, expected: dict | None, qid: str = "X01", question: str = "q?") -> EvalItem:
    return EvalItem(set="t", id=qid, n=1, question=question, grader=grader, expected=expected)


def _row(answer: str, kind: str = "records", **kw) -> dict:
    return {"question": "q?", "answer": answer, "answer_body": answer, "answer_kind": kind, "unsupported_numbers": [],
            "queries": [], "triage": {}, **kw}


def test_golden_grader():
    it = _item("golden", {"n": 1, "question": "q?", "expect": [37, 6.8], "tol": 0.05})
    assert grade(it, _row("37 interviews ended with an offer, 6.8 on average." + SOURCE), "records").verdict == "correct"
    g = grade(it, _row("37 interviews." + SOURCE), "records")
    assert g.verdict == "partial" and "6.8" in g.reasons[0]
    assert grade(it, _row("Twelve." + SOURCE), "records").verdict == "wrong"
    assert grade(it, _row(""), "records").verdict == "wrong"
    g = grade(it, _row("37 and 6.8, and 99." + SOURCE, unsupported_numbers=["99"]), "records")
    assert g.verdict == "needs_review" and g.checks["unsupported"] == ["99"]
    assert grade(it, None, "records").verdict == "not_run"           # its group failed: in no score


def test_lookup_grader():
    it = _item("lookup", LOOKUP_ENTRY)
    ok = "Delta Person took 3 mocks as interviewer (`Interviewer__c`): Completed 2, Cancelled 1." + SOURCE
    assert grade(it, _row(ok), "records").verdict == "correct"
    g = grade(it, _row("Delta Person took three mocks: two Completed, one Cancelled." + SOURCE), "records")
    assert g.verdict == "partial" and g.checks["field_ok"] is False      # number words count; the field is missing
    g = grade(it, _row("Delta Person is the recruiter on 5 mocks." + SOURCE), "records")
    assert g.verdict == "wrong" and "wrong lookup" in " ".join(g.reasons)
    assert grade(it, _row("Delta Person has none." + SOURCE), "records").verdict == "wrong"
    g = grade(it, _row("Delta Person took 3 mocks on `Interviewer__c`: 2 Completed." + SOURCE), "records")
    assert g.verdict == "partial" and g.checks["missing"] == ["1"]
    nf = {"n": 9, "q": "q?", "field": "Interviewer__c", "expect_kind": "not_found", "expected": {},
          "accept": "correct: not found + suggests the stored name. partial: 'no such name' with no suggestion. "
                    "wrong: '0 interviews'."}
    it = _item("lookup", nf)
    assert grade(it, _row("No employee is named 'Delta Persn'; the closest stored name is Delta Person (3 mocks)."
                          + SOURCE), "records").verdict == "correct"
    assert grade(it, _row("No employee is named 'Delta Persn'." + SOURCE), "records").verdict == "partial"
    assert grade(it, _row("Delta Persn took 0 interviews." + SOURCE), "records").verdict == "wrong"


def test_lookup_field_rule_follows_accept():
    entry = dict(LOOKUP_ENTRY, accept="correct: 3 split by Status__c.")
    g = grade(_item("lookup", entry), _row("3 mocks: 2 Completed, 1 Cancelled." + SOURCE), "records")
    assert g.verdict == "correct" and g.checks["fields"] is None
    entry = dict(LOOKUP_ENTRY, accept="correct: attendee rows (or hosts with Session_Role__c, field named).")
    assert autograde._accept_fields(entry) is None
    entry = dict(LOOKUP_ENTRY, accept="correct: 3 by Round_Info__c or Round__c (field named).")
    assert autograde._accept_fields(entry) == ["Round_Info__c", "Round__c"]


PERSON = {"id": "P01", "category": "identity", "q": "who is delta",
          "must": ["Delta Person is the only stored person with the first name Delta (Employee, User)",
                   "Employee: Active Employee, Sales department, joined 2026-02-16, Fresher level, reports to Echo Marsh",
                   "his employee work: hosts 15 sessions (Session__c.Host_User__c), interviewer on 7 mocks "
                   "(Internal_Interview__c.Interviewer__c)",
                   "the other trainers may be named"],
          "partial": ["right person, counts missing"], "wrong": ["a NO_RECORDS answer"],
          "expected": {"people": {"Recruiter__c": [["a06FAKE0000000001A", "Delta Person", "Active Employee", "Sales"]]},
                       "employee_profile": [{"id": "a06FAKE0000000001A", "name": "Delta Person",
                                             "status": "Active Employee", "department": "Sales",
                                             "reporting_to": "Echo Marsh", "date_of_joining": "2026-02-16",
                                             "experience_level": "Fresher level"}]}}
FULL = ("**Delta Person** is an Active Employee in the **Sales** department at Fresher level, joined February 16, 2026 "
        "and reporting to Echo Marsh.\n\n- Host of 15 sessions (`Session__c`)\n- Interviewer on 7 internal mock "
        "interviews" + SOURCE)


def test_people_atoms_from_must_items():
    atoms, optional, unchecked = people_grader.must_atoms(PERSON)
    assert optional == [3] and unchecked == [3]
    values = {a.value for a in atoms[1] if a.kind == "value"}
    assert values == {"Active Employee", "Sales", "Echo Marsh", "Fresher level"}
    assert [a.value for a in atoms[1] if a.kind == "date"] == ["2026-02-16"]
    nums = {a.value: a.context for a in atoms[2] if a.kind == "number"}
    assert nums == {"15": {"session"}, "7": {"mock"}}           # what each number counts
    assert not any(a.kind == "api" for item in atoms for a in item)  # API names explain, they are not required here


def test_people_grader_verdicts():
    it = _item("people", PERSON, "P01", "who is delta")
    assert grade(it, _row(FULL), "records").verdict == "correct"     # the date in words counts
    g = grade(it, _row(FULL.replace("on 7 internal", "on several internal")), "records")
    assert g.verdict == "partial" and g.checks["missing"] == ["must 3: 7 (mock)"]
    g = grade(it, _row("The knowledge file does not contain Delta Person.", kind="knowledge"), "merged")
    assert g.verdict == "wrong"
    g = grade(it, _row(FULL.replace("and reporting", "(RECORD FACTS show P1) and reporting")), "merged")
    assert g.verdict == "needs_review" and "P1" in g.checks["codes"]
    g = grade(it, _row("Delta Person exists." + SOURCE), "records")
    assert g.verdict == "partial"


def test_people_grader_measures_the_status_lines_apart():
    """Group B item 11: the code's status lines are never graded (answer_body); the grade lists what they would add."""
    it = _item("people", PERSON, "P01", "who is delta")
    body = FULL.replace("on 7 internal", "on several internal")
    g = grade(it, _row(body, status_lines=["Status of these Internal Interview records (`Status__c`, 7): Completed 7; "
                                           "none is Cancelled."]), "records")
    assert g.verdict == "partial" and g.checks["status_lines_add"] == ["must 3: 7 (mock)"]
    g = grade(it, _row(body, status_lines=["Status of these Session records (`Status__c`, 2): Completed 2."]), "records")
    assert g.verdict == "partial" and g.checks["status_lines_add"] == []
    assert "status_lines_add" not in grade(it, _row(FULL, status_lines=["x"]), "records").checks


def test_people_grader_rules_for_yes_no_lists_and_unknown_names():
    inv = {"id": "P02", "category": "involvement", "q": "did delta train foxtrot?",
           "must": ["Yes: Delta Person is the trainer of Foxtrot Gale's 3 trainings"], "wrong": ["'no'"],
           "expected": {"t": [["Delta Person"], ["Foxtrot Gale"]]}}
    it = _item("people", inv, "P02")
    assert grade(it, _row("No, Delta Person is not his trainer." + SOURCE), "records").verdict == "wrong"
    listed = ("Yes. Delta Person trains Foxtrot Gale:\n- Retraining one (Completed)\n- Retraining two (Active)\n"
              "- Sample Readiness Training (Active)" + SOURCE)
    assert grade(it, _row(listed), "records").verdict == "correct"     # 3 list items state the 3 trainings
    nf = {"id": "P03", "category": "not_stored", "q": "who is zulu nobody",
          "must": ["says Zulu Nobody is not stored as an employee, user, candidate or lead (0 records)"], "wrong": [],
          "expected": {}}
    it = _item("people", nf, "P03")
    assert grade(it, _row("No Employee, User, Account or Lead record is named 'Zulu Nobody'." + SOURCE),
                 "records").verdict == "correct"
    assert grade(it, _row("Zulu Nobody hosted 4 sessions." + SOURCE), "records").verdict == "partial"


def test_people_grader_main_answer_mostly_missing_is_wrong():
    entry = {"id": "P04", "category": "people_list", "q": "who did delta interview",
             "must": ["Delta Person interviewed Alpha Kay, Bravo Lee, Charlie May and Dora Nye in 9 mocks"],
             "wrong": [], "expected": {"names": [["Delta Person"], ["Alpha Kay"], ["Bravo Lee"], ["Charlie May"],
                                                 ["Dora Nye"]]}}
    it = _item("people", entry, "P04")
    g = grade(it, _row("Delta Person reports to Echo Marsh." + SOURCE), "records")
    assert g.verdict == "wrong" and "more than half" in g.reasons[0]


def test_control_and_route_graders():
    ctl = {"id": "C01", "must": ["Session__c.Host_User__c: employee hosting the meeting; mocks use Other_Field__c",
                                 "no record counts"], "wrong": [], "expected": {"route": "NO_RECORDS"}}
    it = _item("control", ctl, "C01")
    good = "`Host_User__c` on `Session__c` is the employee hosting the session."
    assert grade(it, _row(good, kind="knowledge"), "merged").verdict == "correct"   # the side note is not required
    assert grade(it, _row("`Host_User__c` holds the host.", kind="knowledge"), "merged").verdict == "partial"
    assert grade(it, _row("Delta Person hosted 15 sessions." + SOURCE, kind="merged"), "merged").verdict == "wrong"
    it = _item("route", None, "E01")
    assert grade(it, _row("Values: A, B.", kind="knowledge"), "merged").verdict == "correct"
    assert grade(it, _row("2 records." + SOURCE, kind="merged"), "merged").verdict == "wrong"
    assert grade(it, _row("", kind="none"), "merged").verdict == "wrong"


# ------------------------------------------------------------------------------------------------ the runner
@pytest.fixture()
def mock_llm(monkeypatch):
    """Every LLM the pipeline or the record session builds talks to MockClient (no network)."""
    import sf_assistant.agent.llm as llm_mod
    import sf_assistant.pipeline as pipeline
    from sf_assistant.agent.mock_llm import MockClient

    base = llm_mod.LLM

    class MockLLM(base):
        def __init__(self, cfg, client=None):
            super().__init__(cfg, client=MockClient())

    monkeypatch.setattr(llm_mod, "LLM", MockLLM)
    monkeypatch.setattr(pipeline, "LLM", MockLLM)
    monkeypatch.setenv("OKEVAL_BASE_URL", "http://localhost:8000/v1")
    return MockLLM


def _fixture_sets(tmp_path: Path) -> tuple:
    pytest.importorskip("duckdb")
    from warehouse_fixture import make_knowledge, make_warehouse

    wh, kn = make_warehouse(tmp_path), make_knowledge(tmp_path)
    priv = tmp_path / "home" / ".private"
    (priv / "q").mkdir(parents=True)
    gold = [{"n": 1, "question": "How many interviews ended with an offer?", "tool": "count_records",
             "args": {"object": "Interview__c", "filters": [{"field": "Interview_Outcome__c", "op": "eq",
                                                             "value": "Offer Received"}]}, "expect": [2], "tol": 0}]
    (priv / "q/golden.json").write_text(json.dumps(gold), encoding="utf-8")
    (priv / "q/golden.txt").write_text("How many interviews ended with an offer?\n", encoding="utf-8")
    (priv / "q/easy.txt").write_text("What values can Status__c take?\n", encoding="utf-8")
    sets = tmp_path / "sets.yaml"
    sets.write_text(textwrap.dedent("""\
        version: 1
        results_dir: .private/runs/eval
        today: "2026-10-13"
        order: [golden, easy]
        sets:
          golden:
            questions: q/golden.txt
            expected: q/golden.json
            grader: golden
            id_prefix: G
            modes: [merged, records]
            full: [merged, records]
            quick: 1
          easy:
            questions: q/easy.txt
            grader: route
            id_prefix: E
            modes: [merged]
            full: [merged]
            quick: 1
        levels:
          quick: {sample: true, modes: [merged]}
          standard: {sample: false, modes: [merged]}
          full: {sample: false, modes: full}
        """), encoding="utf-8")
    cfg = load_sets(sets, private_dir=priv, home=tmp_path / "home")
    return cfg, wh, kn


def _inprocess_batch(wh: Path, kn: Path):
    """An AnswerBatch through the real pipeline in this process (the LLM is the mock)."""
    from sf_assistant import pipeline
    from sf_assistant.config import load_settings
    from sf_assistant.knowledge.store import knowledge_files

    def batch(questions, mode, out_base):
        settings = load_settings(records=True, warehouse=str(wh), today="2026-10-13", knowledge_dir=str(kn))
        cfg, _, registry, system, llm = pipeline.build(knowledge_files(str(kn)), settings)
        session, lines = pipeline.open_record_session(cfg, settings.records, str(kn))
        assert session is not None, lines
        try:
            return [{"n": n, **pipeline.run_question(q, cfg, registry, system, llm, n, session,
                                                     records_only=mode == "records")["jsonl"]}
                    for n, q in enumerate(questions, 1)]
        finally:
            session.close()
    return batch


def test_run_grades_writes_reports_and_compares_with_the_previous_run(tmp_path, mock_llm):
    cfg, wh, kn = _fixture_sets(tmp_path)
    logs: list[str] = []
    from sf_assistant.evaluation.runner import run

    first = run(cfg, "all", None, "full", warehouse=str(wh), answer_batch=_inprocess_batch(wh, kn),
                progress=logs.append, knowledge_dir=str(kn))
    assert [(r["set"], r["id"], r["mode"]) for r in first.results] == [
        ("golden", "G01", "merged"), ("golden", "G01", "records"), ("easy", "E01", "merged")]
    assert all(r["verdict"] in autograde.VERDICTS and r["reasons"] is not None for r in first.results)
    # the mock's record agent finds no data: the knowledge answer is shown, and the failed record attempt is flagged
    easy = first.results[2]
    assert easy["answer_kind"] == "knowledge" and easy["verdict"] == "needs_review"
    assert easy["reasons"][0].startswith("record error:")
    golden = first.results[0]
    assert golden["verdict"] == "wrong" and golden["reasons"] == ["expected numbers missing: 2"]
    assert golden["data"] == {"status": "same", "gone": []}     # the golden tool call replayed on the snapshot
    assert first.results[1]["verdict"] == "wrong" and first.results[1]["answer_kind"] == "none"
    files = {p.name for p in first.run_dir.iterdir()}
    assert {"results.jsonl", "summary.json", "report.md"} <= files
    summary = json.loads((first.run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["total"]["n"] == 3 and set(summary["latency"]) == {"golden/merged", "golden/records", "easy/merged"}
    assert summary["latency"]["easy/merged"]["n"] == 1 and first.comparison is None
    report = (first.run_dir / "report.md").read_text(encoding="utf-8")
    assert "## Scores" in report and "No earlier run" in report and "| E01 | easy/merged | needs_review |" in report
    second = run(cfg, "all", None, "full", warehouse=str(wh), answer_batch=_inprocess_batch(wh, kn),
                 progress=logs.append, knowledge_dir=str(kn))
    assert second.previous["run_id"] == first.summary["run_id"] and second.comparison["new"] == 0
    assert f"Previous run: {first.summary['run_id']}" in (second.run_dir / "report.md").read_text(encoding="utf-8")
    other = run(cfg, "easy", None, "quick", warehouse=str(wh), answer_batch=_inprocess_batch(wh, kn),
                progress=logs.append, grade_data=False)
    assert other.previous is None                                    # another set / level: nothing to compare


def test_cli_batch_runs_the_ask_cli_with_the_mock_llm(tmp_path, monkeypatch):
    """The production path: one ``python -m sf_assistant ask --file`` subprocess per group (here a wrapper that
    swaps in MockClient before calling the same cli)."""
    pytest.importorskip("duckdb")
    from warehouse_fixture import make_knowledge, make_warehouse

    from sf_assistant.evaluation.runner import cli_batch

    wh, kn = make_warehouse(tmp_path), make_knowledge(tmp_path)
    wrapper = tmp_path / "mock_ask.py"
    wrapper.write_text(textwrap.dedent("""\
        import sys
        import sf_assistant.agent.llm as llm_mod
        import sf_assistant.cli as cli
        import sf_assistant.pipeline as pipeline
        from sf_assistant.agent.mock_llm import MockClient

        class MockLLM(llm_mod.LLM):
            def __init__(self, cfg, client=None):
                super().__init__(cfg, client=MockClient())

        llm_mod.LLM = pipeline.LLM = MockLLM
        cli.KNOWLEDGE_DIR = sys.argv[1]
        raise SystemExit(cli.main(sys.argv[2:]))
        """), encoding="utf-8")
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith(("OKEVAL_", "SFA_"))}
    env["OKEVAL_BASE_URL"] = "http://localhost:8000/v1"
    batch = cli_batch(today="2026-10-13", warehouse=str(wh), cmd=[sys.executable, str(wrapper), str(kn)], env=env,
                      timeout_s=300)
    out = tmp_path / "out"
    out.mkdir()
    rows = batch(["How many interviews ended with an offer?", "What values can Status__c take?"], "records",
                 out / "golden_records")
    assert [r["n"] for r in rows] == [1, 2] and rows[0]["question"] == "How many interviews ended with an offer?"
    assert (out / "golden_records.txt").is_file() and (out / "golden_records.log").is_file()
    assert "--records-only" not in (out / "golden_records.questions.txt").read_text(encoding="utf-8")
    assert all(r["knowledge_answer"] is None for r in rows)          # records-only: no knowledge agent ran
    # a shell without SFA_RESOLVER: the answers get the spelling help, on by default since 2026-10-27 (as every cli
    # run); without a system word list it stays off and says so. SFA_RESOLVER=off answers without it.
    log = (out / "golden_records.log").read_text(encoding="utf-8")
    words = any(Path(p).is_file() for p in ("/usr/share/dict/american-english", "/usr/share/dict/british-english"))
    assert ("spelling help on (" in log) if words else ("spelling help off: no English word list" in log)
    off = cli_batch(today="2026-10-13", warehouse=str(wh), cmd=[sys.executable, str(wrapper), str(kn)],
                    env={**env, "SFA_RESOLVER": "off"}, timeout_s=300)
    assert [r["n"] for r in off(["What values can Status__c take?"], "records", out / "off_records")] == [1]
    assert "spelling help" not in (out / "off_records.log").read_text(encoding="utf-8")
    with pytest.raises(RuntimeError):                                 # no sidecar: the group fails loudly
        cli_batch(warehouse=str(tmp_path / "missing.duckdb"), cmd=[sys.executable, str(wrapper), str(kn)],
                  env=env, timeout_s=300)(["q?"], "records", out / "broken")


def test_grade_saved_answers_against_hand_grades(tmp_path):
    cfg = load_sets(private_dir=_fake_private(tmp_path, {"lookup": 3, "golden": 1, "easy": 1, "dev": 1, "holdout": 1,
                                                       "controls": 1}), home=tmp_path)
    sidecar = tmp_path / "side.jsonl"
    sidecar.write_text("".join(json.dumps(dict(_row(f"Person{i} took {i if i < 3 else 9} mocks." + SOURCE),
                                               n=i, question=f"How many mocks did Person{i} take?",
                                               seconds=10.0 * i)) + "\n" for i in (1, 2, 3)), encoding="utf-8")
    hand = tmp_path / "hand.tsv"
    hand.write_text("n\tgrade\tnote\n1\tcorrect\t-\n2\tpartial\t-\n3\twrong\t-\n", encoding="utf-8")
    from sf_assistant.evaluation.runner import grade_saved

    res = grade_saved(cfg, "lookup", "records", str(sidecar), hand=str(hand), progress=lambda s: None,
                      grade_data=False)
    assert [r["verdict"] for r in res.results] == ["correct", "correct", "wrong"]   # 9 instead of 3: wrong
    assert res.summary["agreement"]["lookup/records"] == {"n": 3, "agree": 2, "disagree": 1, "needs_review": 0,
                                                          "agreement": "66.7%"}
    assert res.summary["latency"]["lookup/records"]["median"] == 20.0
    assert "## Agreement with the hand grades" in (res.run_dir / "report.md").read_text(encoding="utf-8")


def test_module_cli_list_and_dry_run(tmp_path, capsys):
    _fake_private(tmp_path)
    sets = tmp_path / "sets.yaml"
    sets.write_text(SETS_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    import sf_assistant.evaluation.sets as sets_mod

    orig = sets_mod.load_sets
    try:
        sets_mod.load_sets = lambda path=None, private_dir=None, home=None: orig(path, tmp_path / ".private", tmp_path)
        eval_main.load_sets = sets_mod.load_sets
        assert eval_main.main(["--sets", str(sets), "list"]) == 0
        out = capsys.readouterr().out
        assert "level quick     35 answers" in out and "level full      284 answers" in out
        assert eval_main.main(["--sets", str(sets), "run", "--level", "quick", "--dry-run"]) == 0
        assert "35 answers (level quick, today 2026-10-13)" in capsys.readouterr().out
        assert eval_main.main(["--sets", str(sets), "run", "--set", "nope", "--dry-run"]) == 2
    finally:
        eval_main.load_sets = orig
        sets_mod.load_sets = orig
    assert not (tmp_path / ".private" / "runs").exists()             # a dry run writes nothing


def test_module_help_runs_without_a_model():
    p = subprocess.run([sys.executable, "-m", "sf_assistant.evaluation", "--help"], capture_output=True, text=True,
                       timeout=60)
    assert p.returncode == 0 and "run" in p.stdout and "grade" in p.stdout


# ------------------------------------------------------------------------------------------------ adversarial set
#: Answers the round-2 hand grades never tested (2026-10-20 review): wrong counts, counts inside dates and record
#: codes, a required 0 next to a non-zero count, a stored 'Draft' value. (answer, expected verdict) per entry.
ADVERSARIAL_LOOKUP = [
    ({"n": 5, "q": "How many mocks did Person5 take?", "score": [5], "accept": "correct: 5.", "expected": {}},
     [("There are 40 matching records (as of 2026-05-20).", "wrong"),            # 5 only inside a date
      ("Person5 took 5 mocks.", "correct"),
      ("Person5 took 22 mocks.", "wrong"),                                        # a different count
      ("The newest one is INT-0005, held 2026-10-05 14:05 UTC.", "partial")]),   # no count stated at all
    ({"n": 32, "q": "How many resumes did Person32 approve?", "score": [0], "accept": "correct: 0.", "expected": {}},
     [("There are 5 matching records. No other details are stored.", "wrong"),    # "no" is not the claim
      ("No, there are 5 matching records.", "wrong"),
      ("None: the approver field is blank on every record. 5 resumes were uploaded by Person32.", "correct"),
      ("Person32 approved 0 resumes.", "correct")]),
    ({"n": 8, "q": "How many interviews did Person8 have in August?", "score": [8], "accept": "correct: 8.",
      "expected": {}},
     [("Person8 had 3 interviews, on 2026-08-15, 2026-08-20 and 2026-08-22.", "wrong"),
      ("Person8 had 8 interviews in August (2026-08-01 to 2026-08-31).", "correct")]),
]


@pytest.mark.parametrize("entry,cases", ADVERSARIAL_LOOKUP, ids=[e["q"][:30] for e, _ in ADVERSARIAL_LOOKUP])
def test_adversarial_lookup_answers(entry, cases):
    it = _item("lookup", entry)
    got = [(answer, grade(it, _row(answer + SOURCE), "records").verdict) for answer, _ in cases]
    assert got == cases


def test_answer_numbers_leave_out_dates_times_and_codes():
    text = "40 records (2026-05-20), synced 2026-10-13 14:56 UTC; newest INT-0268 at 09:30; 2026-08: 5 more."
    assert autograde.answer_numbers(text) == ["40", "5"]
    assert autograde.answer_numbers("three of 12, and both") == ["12", "3", "2"]


def test_a_stored_draft_value_is_no_leak():
    it = _item("lookup", {"n": 1, "q": "How many resumes are in Draft?", "score": [4], "accept": "correct: 4.",
                          "expected": {}})
    assert grade(it, _row("4 resumes are in Draft status." + SOURCE), "records").verdict == "correct"
    g = grade(it, _row("4 resumes, as in the draft answer." + SOURCE), "records")
    assert g.verdict == "needs_review" and g.checks["leak"] == "in the draft"
    assert grade(it, _row("4 resumes (RECORD FACTS)." + SOURCE), "records").verdict == "needs_review"


def test_expected_sql_that_fails_gives_needs_review(tmp_path):
    con = expected_data.open_readonly(str(_db(tmp_path / "w.duckdb", ROWS)))
    try:
        entry = dict(LOOKUP_ENTRY, sql={"main_count": 'SELECT COUNT(*) FROM main."Gone__c"'}, score=[3])
        chk = expected_data.check_entry(con, entry, "lookup")
        assert chk.status == "error" and expected_data.refresh_numbers(["3"], chk) == ([], ["3"], [])
        g = grade(_item("lookup", entry), _row("Delta Person took 3 mocks on `Interviewer__c`." + SOURCE), "records",
                  con=con)
        assert g.verdict == "needs_review" and g.reasons[-1].startswith("expected SQL failed on the data:")
        person = {"category": "owner", "must": ["took 3 mocks"], "expected": {"mocks": 3},
                  "sql": {"mocks": 'SELECT COUNT(*) FROM main."Gone__c"'}}
        g = grade(_item("people", person), _row("Delta Person took 3 mocks." + SOURCE), "records", con=con)
        assert g.verdict == "needs_review" and "expected SQL failed" in g.reasons[-1]
    finally:
        con.close()


def test_a_status_split_follows_a_data_change(tmp_path):
    """The counts of a ``{label: count}`` fact are re-derived by label after a refresh (one more completed mock)."""
    con = expected_data.open_readonly(str(_db(tmp_path / "w.duckdb", ROWS + [("i5", "Completed", "e1", None)])))
    try:
        entry = {"category": "owner", "must": ["took 3 mocks; status split from by_status"],
                 "expected": {"mocks": 3, "by_status": {"Completed": 2, "Cancelled": 1}},
                 "sql": {"mocks": 'SELECT COUNT(*) FROM main."Internal_Interview__c" WHERE "Interviewer__c" = \'e1\'',
                         "by_status": BY_STATUS}}
        it = _item("people", entry)
        assert grade(it, _row("Delta Person took 4 mocks: 3 Completed and 1 Cancelled." + SOURCE), "records",
                     con=con).verdict == "correct"
        g = grade(it, _row("Delta Person took 3 mocks: 2 Completed and 1 Cancelled." + SOURCE), "records", con=con)
        assert g.verdict == "partial" and g.checks["missing"] == ["must 1: 4 (mock)"]   # the old total
    finally:
        con.close()


def test_a_failed_group_is_not_run_and_no_baseline(tmp_path):
    from sf_assistant.evaluation.runner import find_previous, run

    cfg = load_sets(private_dir=_fake_private(tmp_path, {"lookup": 2, "golden": 1, "easy": 1, "dev": 1, "holdout": 1,
                                                       "controls": 1}), home=tmp_path)
    calls = {"n": 0}

    def batch(questions, mode, out_base):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("records off")
        return [dict(_row(f"Person{k} took {k} mocks." + SOURCE), n=k, question=q, seconds=1.0)
                for k, q in enumerate(questions, 1)]

    first = run(cfg, "lookup", "records", "standard", answer_batch=batch, progress=lambda s: None, grade_data=False)
    assert [r["verdict"] for r in first.results] == ["not_run", "not_run"] and first.summary["errors"]
    assert first.summary["total"] == {"correct": 0, "partial": 0, "wrong": 0, "needs_review": 0, "not_run": 2, "n": 0}
    report = (first.run_dir / "report.md").read_text(encoding="utf-8")
    assert "**Failed groups**" in report and "| lookup/records | 0 | 0 | 0 | 0 | 0 | 2 |" in report
    second = run(cfg, "lookup", "records", "standard", answer_batch=batch, progress=lambda s: None, grade_data=False)
    assert [r["verdict"] for r in second.results] == ["correct", "correct"]
    assert second.previous is None and second.comparison is None      # the failed run is no baseline
    assert find_previous(cfg.results_path(), "lookup", "standard", "records", "~")["run_id"] == second.summary["run_id"]


def test_question_files_follow_the_questions_dir(tmp_path):
    """config.QUESTIONS_DIR / SFA_QUESTIONS_DIR (Settings.questions_dir) is where the questions/... set files are."""
    priv = _fake_private(tmp_path, {"lookup": 1, "golden": 2, "easy": 3, "dev": 1, "holdout": 1, "controls": 1})
    moved = tmp_path / "elsewhere"
    (priv / "questions").rename(moved)
    cfg = load_sets(private_dir=priv, home=tmp_path, questions_dir=moved)
    assert cfg.path("questions/easy.txt") == moved / "easy.txt"
    assert cfg.path("okeval_run/lookup_eval/expected.json") == priv / "okeval_run/lookup_eval/expected.json"
    assert len(load_items(cfg.sets["easy"], cfg)) == 3
    assert load_sets(private_dir=priv, home=tmp_path).questions_dir == priv / "questions"
    from sf_assistant import config
    assert load_sets().questions_dir == config.QUESTIONS_DIR


def test_a_close_count_is_partial_and_a_far_one_wrong():
    it = _item("lookup", {"n": 30, "q": "How many candidates does Person30 recruit?", "score": [26],
                          "accept": "correct: 26.", "expected": {}})
    g = grade(it, _row("Person30 recruits 25 candidates." + SOURCE), "records")
    assert g.verdict == "partial" and g.checks["close"] == ["25"] and "check the scope" in g.reasons[-1]
    assert grade(it, _row("Person30 recruits 22 candidates." + SOURCE), "records").verdict == "wrong"
    assert grade(it, _row("Person30 recruits 26 candidates." + SOURCE), "records").verdict == "correct"
