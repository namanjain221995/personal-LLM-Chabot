"""Group B people fixes on the record tools (docs/people.md; items 7, 8, 9, 12 and the tools parts of 10 and 13).

Model-free, on the synthetic fixture ``groupb_fixture`` (fake names only). Every new statement runs through the guard
(a call that failed it would not be ok) and binds its Ids.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import groupb_fixture as G  # noqa: E402
import warehouse_fixture as F  # noqa: E402

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")


@pytest.fixture(scope="module")
def gtools(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = G.open_groupb_fixture(tmp_path_factory.mktemp("groupb"))
    yield t
    t.close()


def ok(tools, name, args, question=""):
    tools.begin_question(question)
    out = tools.call(name, args)
    assert out["ok"], out["error"]
    return out["result"]


def ev_last(tools):
    return tools.evidence[-1]


def F_(f, v, op="eq"):
    return {"field": f, "op": op, "value": v}


def _bound(tools, *ids):
    """No Id is written into any SQL of the last call (they are parameters)."""
    return all(i not in st.sql for st in ev_last(tools).statements for i in ids)


# ------------------------------------------------------------------------- item 12: same-name users, per record
def test_references_say_whose_record_and_users_list_their_system_work(gtools):
    r = ok(gtools, "about_people", {"names": ["hotel ivory"]})
    assert [(p["label"], p["kind"]) for p in r["people"]] == [("Hotel Ivory (#1)", "user"), ("Hotel Ivory (#2)", "user")]
    lead = [e for e in r["system_references"] if e["object"] == "Lead" and e["field"] == "OwnerId"]
    assert lead and lead[0]["who"] == "Hotel Ivory (#2) (User, standard user)" and lead[0]["person"] == "P2"
    p1, p2 = r["people"]
    assert p2["system_work"] == ["Lead.OwnerId 1"] and "system_work" not in p1
    assert p2["no_business_references"] is True                 # no business record, but the owned lead is shown
    assert r["same_name"] == {"hotel ivory": ["P1", "P2"]}        # never merged


def test_same_name_records_are_split_when_no_lookup_check_counts_them(gtools, monkeypatch):
    args = {"object": "Lead", "filters": [F_("OwnerId", "Hotel Ivory")]}
    r = ok(gtools, "count_records", args)
    assert r["count"] == 1 and {b["record"]: b["records"] for b in r["lookup_check"][0]["by_record"]} == \
        {"Hotel Ivory (#1)": 0, "Hotel Ivory (#2)": 1}
    import sf_assistant.tools.lookups as L
    monkeypatch.setattr(L, "MAX_LOOKUP_CHECKS", 0)          # as for the third name check of a call
    r = ok(gtools, "count_records", args)
    assert "lookup_check" not in r
    note = next(n for n in r["notes"] if n.startswith("'Hotel Ivory' matches 2 User or Group records with the same"))
    assert "per record: #1 0, #2 1" in note and _bound(gtools, G.USER_HI1, G.USER_HI2)


# ------------------------------------------------------------------------- item 7a: the per-person share
def test_each_record_keeps_its_top_lookup_in_the_share(gtools, monkeypatch):
    import sf_assistant.tools.people as P
    monkeypatch.setattr(P, "PERSON_REFS", 2)                 # two people: a share of 2 references each
    r = ok(gtools, "about_people", {"names": ["juliet converse", "sierra tango"]})
    jc = next(p for p in r["people"] if p["label"] == "Juliet Converse" and p["kind"] == "candidate")
    mine = [(e["object"], e["field"]) for e in r["references"] if e["person"] == jc["ref"]]
    # the account's 2-record lookups would fill the share; the converted lead's intake interview keeps its place
    assert ("Internal_Interview__c", "Lead__c") in mine and len(mine) == 2
    assert jc["references_shown"].startswith("2 of ")
    assert jc["more_references"] and all(" (R" in x for x in jc["more_references"])
    assert any(x.startswith("Interview__c.Candidate__c 2 ") for x in jc["more_references"])


def test_budget_cuts_the_smallest_reference_that_is_no_records_top(gtools):
    from sf_assistant.tools.record_base import _Call
    call = _Call(gtools, "about_people")
    call.max_chars = 660
    refs = [{"person": "P1", "record": "R1", "object": "X", "field": f"F{n}", "role": "r" * 30, "records": n}
            for n in (9, 5, 3, 2)] + [{"person": "P1", "record": "R2", "object": "Y", "field": "G", "role": "r" * 30,
                                       "records": 1}]
    out = {"people": [{"ref": "P1"}], "references": refs, "system_references": []}
    gtools._people_budget(call, out, 1, reserve=0)
    kept = [(e["record"], e["records"]) for e in out["references"]]
    assert ("R2", 1) in kept and ("R1", 9) in kept               # each record's top lookup stays
    assert ("R1", 2) not in kept                                 # the smallest that is no top goes first
    assert any("more references not shown" in n for n in call.notes)


def test_budget_cuts_the_details_of_the_fixes_before_links_and_references(gtools):
    import copy
    from sf_assistant.tools.record_base import _Call
    links = [{"between": ["P1", "P2"], "how": "parent", "path": f"X.a{i} -> X.p -> Y.b", "records": 2, "text": "t" * 60,
              "by_status": {"Status__c": {"Open": 2}}, "others": {"field": "X.a", "role": "r", "of": "P2", "records": 2,
                                                                  "names": ["Some One (1)", "Some Two (1)"]}}
             for i in range(10)]
    refs = [{"person": f"P{k}", "record": f"R{k}", "who": "Some Name (Employee, Active Employee)", "object": "X",
             "field": f"F{n}", "role": "r" * 20, "records": n, "distinct": {"Candidate__c": 1}} for k in (1, 2)
            for n in (5, 4, 3)]
    people = [{"ref": f"P{k}", "linked": ["User 'Some Name' (standard user; link)"], "system_work": ["Lead.OwnerId 1"],
               "more_references": ["X.G 1 (R1)", "X.H 1 (R1)"]} for k in (1, 2)]
    out = {"people": people, "references": refs, "system_references": [], "links": links}
    bare = copy.deepcopy(out)
    for i, x in enumerate(bare["links"]):
        x.pop("by_status")
        if i:                                    # the first link keeps the other people in its role ("who else")
            x.pop("others")
    for e in bare["references"]:
        e.pop("who"), e.pop("distinct")
    for p in bare["people"]:
        p.pop("linked"), p.pop("system_work"), p.pop("more_references")
    call = _Call(gtools, "about_people")
    call.max_chars = len(json.dumps(bare)) + 60 * 2 + 5
    gtools._people_budget(call, out, 2, reserve=0, keep={"P1", "P2"})
    assert out == bare and not call.notes        # every detail went first, without a note; links and references stay
    # a link whose other people are in the role the question asks about keeps them instead of the first one
    out = copy.deepcopy(bare)
    out["links"][0].pop("others")
    out["links"][4]["others"] = dict(bare["links"][0]["others"])
    for x in out["links"][1:]:
        x["others"] = dict(bare["links"][0]["others"])
    call = _Call(gtools, "about_people")
    call.max_chars = len(json.dumps(bare)) + 60 * 2 + 5
    gtools._people_budget(call, out, 2, reserve=0, keep={"P1", "P2"}, protect={id(out["links"][4])})
    assert [i for i, x in enumerate(out["links"]) if "others" in x] == [4]


def test_budget_without_links_shortens_then_drops_linked_lines_of_many_people(gtools):
    from sf_assistant.tools.record_base import _Call
    long_, short = "User 'Some Name' (standard user, active; portal user (User.AccountId) of R1)", \
        "User 'Some Name' (standard user, active)"
    refs = [{"person": f"P{k}", "record": f"R{k}", "object": "X", "field": f"F{n}", "role": "r" * 20, "records": n}
            for k in (1, 2, 3) for n in (5, 4)]
    sysrefs = [{"person": "P1", "record": "R1", "object": "X", "field": "CreatedById", "role": "creator", "records": 9}]

    def result():
        return {"people": [{"ref": f"P{k}", "linked": [long_, long_]} for k in (1, 2, 3)], "references": list(refs),
                "system_references": list(sysrefs)}
    call = _Call(gtools, "about_people")
    call.linked_plain[long_] = short
    base = result()
    for p in base["people"]:
        p["linked"] = [short, short]
    call.max_chars = len(json.dumps(base)) + 60 * 3 + 2
    out = result()
    gtools._people_budget(call, out, 1, reserve=0)              # one person: the short lines stay, nothing else goes
    assert [p["linked"] for p in out["people"]] == [[short, short]] * 3 and out["system_references"] == sysrefs
    bare = result()
    for p in bare["people"]:
        p.pop("linked")
    call.max_chars = len(json.dumps(bare)) + 60 * 3 + 2
    out = result()
    gtools._people_budget(call, out, 3, reserve=0)              # three people: the lines yield before old content
    assert out == bare and not call.notes
    kept = result()                                              # the asked person of a full name keeps them longest
    kept["people"][0]["linked"] = [short, short]
    kept["people"][1].pop("linked"), kept["people"][2].pop("linked")
    call.max_chars = len(json.dumps(kept)) + 60 * 3 + 2
    out = result()
    gtools._people_budget(call, out, 3, reserve=0, keep={"P1"})
    assert out == kept and not call.notes


def test_who_only_for_people_who_share_a_name(gtools):
    r = ok(gtools, "about_people", {"names": ["kilo"]})   # three people, three different names
    assert not any("who" in e for e in r["references"] + r["system_references"])


def test_one_person_still_gets_every_lookup(gtools):
    r = ok(gtools, "about_people", {"names": ["sierra tango"]})
    assert "references_shown" not in r["people"][0] and len(r["references"]) == r["lookups_with_records"]


# ------------------------------------------------------------------------- item 7b: linked records, one line each
def test_linked_records_get_one_line_each(gtools):
    r = ok(gtools, "about_people", {"names": ["echo one"]})
    p = next(x for x in r["people"] if x["kind"] == "employee")
    lines = p["linked"]
    assert len(lines) == 3
    assert any(x.startswith("Account 'Echo One' (Recruiter; Recruiter_Contact__c") for x in lines)
    assert any(x.startswith("User 'Echo One") and "portal user, active" in x for x in lines)
    assert any(x.startswith("User 'Echo One") and "standard user, active" in x for x in lines)
    assert p["profile"]                                          # the profile and references are unchanged


# ------------------------------------------------------------------------- item 7c: distinct subjects
def test_distinct_candidates_of_a_users_rows(gtools):
    r = ok(gtools, "about_people", {"names": ["india juno"]})
    e = next(e for e in r["references"] if (e["object"], e["field"]) == ("Candidate_Training__c", "Candidate_Owner__c"))
    assert e["records"] == G.GROUPB_FACTS["owner_trainings"]
    assert e["distinct"] == {"Candidate__c": G.GROUPB_FACTS["owner_training_candidates"]}
    distinct = [st for st in ev_last(gtools).statements if "COUNT(DISTINCT r." in st.sql]
    assert distinct and all(st.params for st in distinct) and _bound(gtools, G.USER_IJ)
    r = ok(gtools, "about_people", {"names": ["candidate 1"]})   # a candidate's own lookup: no distinct count
    assert not any("distinct" in e for e in r["references"])


# ------------------------------------------------------------------------- item 7d: the same role on other objects
def test_owner_count_shows_the_owner_role_on_child_rows(gtools):
    r = ok(gtools, "count_records", {"object": "Account", "filters": [F_("OwnerId", "India Juno")]})
    assert r["count"] == G.GROUPB_FACTS["owner_accounts"]
    lc = r["lookup_check"][0]
    # the owner's training row that points to no candidate is no row of an Account record (review fix)
    assert lc["related"] == [{"object": "Candidate_Training__c", "field": "Candidate_Owner__c", "via": "Candidate__c",
                              "role": "candidate owner", "fits": None,
                              "records": G.GROUPB_FACTS["owner_trainings_of_accounts"], "distinct": 2,
                              "same_role": False}]
    assert "any_path" not in lc                                  # another role: never in a union
    assert any("also holds a role sharing a word with OwnerId" in n and "3 rows of 2 different Account records" in n
               and "through Candidate_Training__c.Candidate__c" in n for n in r["notes"])


def test_another_role_gets_no_related_rows(gtools):
    r = ok(gtools, "count_records", {"object": "Internal_Interview__c", "filters": [F_("Interviewer__c", "Delta Person")]})
    assert "related" not in r["lookup_check"][0]                 # an invited employee is no interviewer


# ------------------------------------------------------------------------- item 7e: others in the same role (C4)
def test_link_lists_the_other_people_in_the_same_role(gtools):
    r = ok(gtools, "about_people", {"names": ["sierra tango", "kilo quill"]})
    refs = {p["label"]: p["ref"] for p in r["people"]}
    link = next(x for x in r["links"] if x["how"] == "same record" and "Assigned_Trainer__c" in x["path"])
    assert link["others"] == {"field": "Candidate_Training__c.Assigned_Trainer__c", "role": "trainer employee",
                              "of": refs["Kilo Quill"], "records": 3, "names": ["Golf Harbor (1)", "Mike Nectar (1)"]}
    assert "by_status" not in link                               # one record: its example shows the status


def test_link_status_split_and_the_side_with_fewer_rows(gtools):
    r = ok(gtools, "about_people", {"names": ["recruiter a", "candidate 1"]})
    refs = {p["label"]: p["ref"] for p in r["people"]}
    link = next(x for x in r["links"] if x["path"] == "Interview__c.Recruiter__c + Interview__c.Candidate__c")
    assert link["records"] == 3 and link["by_status"] == {"Interview_Status__c": {"Completed": 3}}
    oth = link["others"]
    assert oth["of"] == refs["Candidate 1"] and oth["field"] == "Interview__c.Recruiter__c" and oth["records"] == 5
    assert set(oth["names"]) == {"Recruiter B (1)", "Recruiter C (1)"}


# ------------------------------------------------------------------------- item 8: paths through parents (C5)
def test_count_through_the_parents_in_the_same_role(gtools):
    r = ok(gtools, "count_records", {"object": "Session__c", "filters": [F_("Candidate__c", "Candidate 1")]})
    assert r["count"] == G.GROUPB_FACTS["cand1_sessions_direct"]
    lc = r["lookup_check"][0]
    assert [(x["path"], x["records"]) for x in lc["paths"]] == [
        ("Session__c.Candidate_Training__c -> Candidate_Training__c.Candidate__c",
         G.GROUPB_FACTS["cand1_sessions_training"]),
        ("Session__c.Training_Step__c -> Training_Step__c.Candidate_Training__c -> Candidate_Training__c.Candidate__c",
         G.GROUPB_FACTS["cand1_sessions_step"])]
    assert [(x["object"], x["field"], x["distinct"], x["same_role"]) for x in lc["related"]] == [
        ("Session_Attendee__c", "Candidate__c", G.GROUPB_FACTS["cand1_sessions_attendee"], True)]
    assert lc["any_path"] == G.GROUPB_FACTS["cand1_sessions_any"]
    note = next(n for n in r["notes"] if "through the parents of Session__c records" in n)
    assert "never add" in note and note.endswith(f"in Candidate__c or any of these: {G.GROUPB_FACTS['cand1_sessions_any']}")
    assert _bound(gtools, F.CAND_1)


def test_a_host_count_gets_no_trainer_path(gtools):
    r = ok(gtools, "count_records", {"object": "Session__c", "filters": [F_("Host_User__c", "Sierra Tango")]})
    lc = r["lookup_check"][0]
    assert "paths" not in lc and "any_path" not in lc and "related" not in lc


def test_references_get_their_parent_paths(gtools):
    r = ok(gtools, "about_people", {"names": ["candidate 1"]})
    e = next(e for e in r["references"] if (e["object"], e["field"]) == ("Session__c", "Candidate__c"))
    assert e["via_parents"] == {"Candidate_Training__c -> Candidate__c": 3,
                                "Training_Step__c -> Candidate_Training__c -> Candidate__c": 1}
    assert e["any_path"] == G.GROUPB_FACTS["cand1_sessions_any"]
    att = next(e for e in r["references"] if (e["object"], e["field"]) == ("Session_Attendee__c", "Candidate__c"))
    assert "via_parents" not in att                              # an attendee row: its session is everyone's


def test_rows_of_one_person_in_a_role_have_no_parent_paths(gtools):
    cat = gtools.catalog
    att = cat.objects["Session_Attendee__c"]
    assert gtools._role_rows(att, att.fields["Candidate__c"])             # Role__c holds 'Candidate'
    assert gtools._parent_paths(att, att.fields["Candidate__c"]) == []
    ses = cat.objects["Session__c"]
    assert not gtools._role_rows(ses, ses.fields["Candidate__c"])          # Purpose__c names no person role


# ------------------------------------------------------------------------- item 9: a partial copy from a child object
def test_catalog_flags_a_copy_of_a_child_objects_lookup(gtools):
    f = gtools.catalog.objects["Account"].fields["Assigned_Recruiter__c"]
    assert f.child_copy == ("Marketing__c", "Candidate__c", "Recruiter__c") and not f.mirror_of
    assert f.filled == G.GROUPB_FACTS["kz_copy"] and f.copy_source_filled == 4
    same = gtools.catalog.objects["Candidate_Training__c"].fields["Program__c"]
    assert same.mirror_of == "Program_Version__c.Program__c" and not same.child_copy   # the same-record copy as before


def test_count_on_the_copy_names_the_source(gtools):
    r = ok(gtools, "count_records", {"object": "Account", "filters": [F_("Assigned_Recruiter__c", "Kilo Zephyr")]})
    assert r["count"] == G.GROUPB_FACTS["kz_copy"]
    assert r["child_copy"] == [{"field": "Assigned_Recruiter__c", "source": "Marketing__c.Recruiter__c",
                                "via": "Marketing__c.Candidate__c", "filled": 1, "source_filled": 4}]
    assert r["lookup_check"][0]["copy_source"] == {"field": "Assigned_Recruiter__c", "source": "Marketing__c.Recruiter__c",
                                                   "via": "Marketing__c.Candidate__c",
                                                   "records": G.GROUPB_FACTS["kz_source_candidates"]}
    assert any(n.startswith("Account.Assigned_Recruiter__c is a partial copy of Marketing__c.Recruiter__c") for n in r["notes"])
    assert "copied_lookups" not in r
    r = ok(gtools, "count_records", {"object": "Marketing__c", "filters": [F_("Recruiter__c", "Kilo Zephyr")],
                                     "count_distinct": "Candidate__c"})
    assert r["count"] == G.GROUPB_FACTS["kz_source_candidates"] and "child_copy" not in r


def test_profile_shows_the_copy_and_its_source(gtools):
    r = ok(gtools, "about_people", {"names": ["kilo quill", "candidate 1"]})
    prof = {p["label"]: p.get("profile") or {} for p in r["people"]}
    assert prof["Kilo Quill"]["Assigned_Recruiter__c (Assigned Recruiter; partial copy of Marketing__c.Recruiter__c)"] \
        == "Kilo Zephyr"
    assert prof["Candidate 1"]["Marketing__c.Recruiter__c (via Candidate__c)"] == "Kilo Zephyr"   # the copy is blank
    assert not any(k.startswith("Assigned_Recruiter__c") for k in prof["Candidate 1"])


def test_hints_rank_the_source_before_the_copy(gtools):
    q = "how many candidates is kilo zephyr assigned as recruiter"
    gtools.begin_question(q)
    line = next(x for x in gtools.object_hints(q).splitlines() if x.startswith("- 'Kilo Zephyr' (Employee): "))
    assert line.index("Marketing__c.Recruiter__c") < line.index("Account.Assigned_Recruiter__c")
    assert "partial copy of Marketing__c.Recruiter__c) <- fits" in line


# ------------------------------------------------------------------------- item 10 (tools part): direction wording
def test_profile_key_of_a_reporting_line_says_how_the_record_reads_it(gtools):
    f = gtools.catalog.objects["Recruiter__c"].fields["Assigned_Sr_Recruiter__c"]
    assert f.from_source == "senior recruiter this employee reports to"
    assert f.from_target == "employees reporting to this senior recruiter"
    r = ok(gtools, "about_people", {"names": ["romeo stark"]})
    prof = r["people"][0]["profile"]
    assert prof["Assigned_Sr_Recruiter__c (Reporting to; senior recruiter this employee reports to)"] == "Papa Quinn"
    assert prof["Manager__c (Manager)"] == "Oscar Prime"        # no direction text: the key as before


# ------------------------------------------------------------------------- item 7f: list_records (C8)
def test_list_records_adds_the_rows_of_linked_lookups_and_child_people(gtools):
    r = ok(gtools, "list_records", {"object": "Internal_Interview__c", "fields": ["Name", "Interviewer__c", "Status__c"],
                                    "filters": [F_("Candidate__c", "Juliet Converse")]})
    assert [x["Name"] for x in r["rows"]] == ["INT-0041"] and r["total_count"] == 1
    held = r["also_held"]
    assert [(h["field"], h["group"], h["total"]) for h in held] == [("Lead__c", "linked", 1)]
    assert held[0]["rows"] == [{"Name": "INT-0040", "Interviewer__c (name)": "Delta  Person", "Status__c": "Completed"}]
    assert r["child_people"] == [{"object": "Interview_Attendee__c", "field": "Attendee_Recruiter__c",
                                  "role_field": "Role__c", "role": "Interviewer", "column": "Interviewer__c (name)",
                                  "by_row": {"INT-0041": ["Echo One"]}}]      # the Observer row is not listed
    assert _bound(gtools, F.CAND_CONV, F.LEAD_CONV)


def test_list_records_without_a_person_filter_is_unchanged(gtools):
    r = ok(gtools, "list_records", {"object": "Internal_Interview__c", "fields": ["Name", "Status__c"], "limit": 3})
    assert "also_held" not in r and "child_people" not in r


# ------------------------------------------------------------------------- item 13 (tools part, C7): first_name_fit
def test_first_name_fit_needs_one_stored_name_and_a_fitting_lookup(gtools, monkeypatch):
    import sf_assistant.tools.names as N
    monkeypatch.setattr(N, "_COMMON_WORDS", set())              # no word list: (d) holds for every word
    q = "how many sessions did sierra host"
    spot = next(d for d in gtools.spot_people(q) if d["words"] == "sierra")
    assert gtools.first_name_fit(q, spot) == {"word": "host", "lookup": "Session__c.Host_User__c", "records": 1,
                                              "name": "Sierra Tango", "name_word": "sierra"}
    assert gtools.first_name_fit(q, "sierra")["lookup"] == "Session__c.Host_User__c"    # the word alone works too
    assert gtools.first_name_fit("is sierra around", "sierra") is None                  # no lookup fits a word
    assert gtools.first_name_fit("how many sessions did echo host", "echo") is None     # two stored names
    assert gtools.first_name_fit("how many sessions did kilo host", "kilo") is None     # three stored names
    assert gtools.first_name_fit("how many sessions did session host", "session") is None   # a design word
    assert gtools.first_name_fit(q, {"words": None}) is None and gtools.first_name_fit(q, 42) is None


def test_first_name_fit_reads_a_common_word_as_a_name_only_when_written_as_one(gtools, monkeypatch):
    import sf_assistant.tools.names as N
    monkeypatch.setattr(N, "_COMMON_WORDS", {"golf", "sierra"})
    assert gtools.first_name_fit("how many trainings does golf train", "golf") is None      # the word, lower case
    assert gtools.first_name_fit("Golf trains how many candidates", "golf") is None         # a sentence's first word
    fit = gtools.first_name_fit("how many candidates does Golf train", "golf")
    assert fit and fit["lookup"] == "Candidate_Training__c.Assigned_Trainer__c" and fit["name"] == "Golf Harbor"
    assert gtools.first_name_fit("i like sierra; how are sessions hosted", "sierra") is None
    assert N._written_as_name("so. Golf trains", "golf") is False and N._written_as_name("did Golf train", "golf")


def test_first_name_fit_is_time_bounded(gtools, monkeypatch):
    import sf_assistant.tools.record_base as B
    monkeypatch.setattr(B, "SCAN_BUDGET_S", 0.0)
    gtools.begin_question("")
    assert gtools.first_name_fit("how many sessions did sierra host", "sierra") is None


# ------------------------------------------------------------------------- sizes
def test_results_stay_within_their_limits(gtools):
    from sf_assistant.tools.record_base import MAX_RESULT_CHARS
    from sf_assistant.tools.people import PEOPLE_LINKS_EXTRA_CHARS
    for names in (["candidate 1"], ["kilo"], ["sierra tango", "kilo quill", "uniform vale"],
                  ["recruiter a", "candidate 1", "india juno", "hotel ivory"]):
        r = ok(gtools, "about_people", {"names": names})
        limit = MAX_RESULT_CHARS + (PEOPLE_LINKS_EXTRA_CHARS if len(names) >= 2 else 0)
        assert len(json.dumps(r)) <= limit, names


# ------------------------------------------------------------------------- review fixes (2026-10-21)
def test_subject_role_holds_candidates_and_leads_only(gtools):
    cat = gtools.catalog
    def sr(o, f):
        return gtools._subject_role(cat.objects[o], cat.objects[o].fields[f])
    assert sr("Session__c", "Candidate__c") and sr("Internal_Interview__c", "Lead__c") and \
        sr("Candidate_Training__c", "Candidate__c")
    assert not sr("Candidate_Training__c", "Assigned_Trainer__c") and not sr("Session__c", "Host_User__c")
    assert not sr("Candidate_Training__c", "Candidate_Owner__c") and not sr("Account", "Assigned_Recruiter__c")


def test_no_distinct_count_on_rows_that_are_candidates(gtools):
    from sf_assistant.tools.record_base import _Call
    call = _Call(gtools, "about_people")
    refs = [{"person": "P1", "record": "R1", "object": "Account", "field": "Assigned_Recruiter__c", "records": 3}]
    gtools._distinct_subjects(call, refs, {F.REC_KZ: {"ref": "R1", "id": F.REC_KZ}})
    assert "distinct" not in refs[0] and not call.statements      # the rows are the candidates: no other count
    assert all(cat_t != "Account" for cat_t in [gtools.catalog.target_of(g)
                                                for g in gtools._subject_lookups("Account", "Assigned_Recruiter__c")])


def test_list_also_held_leaves_out_the_rows_the_filter_lists(gtools):
    r = ok(gtools, "list_records", {"object": "Internal_Interview__c", "fields": ["Name", "Status__c"],
                                    "filters": [F_("Interview_Readiness_Person__c", "Delta Person")]})
    listed = {x["Name"] for x in r["rows"]}
    held = next(h for h in r["also_held"] if h["field"] == "Interviewer__c")
    names = {x["Name"] for x in held["rows"]}
    assert "INT-0001" in listed and not names & listed and held["total"] == len(names) == 3
    assert any("and this filter does not" in n for n in r["notes"])


def test_owner_rows_of_a_program_are_no_related_rows(gtools):
    cat = gtools.catalog
    owner = cat.objects["Account"].fields["OwnerId"]
    assert gtools._related_children(cat.objects["Program__c"], owner, {"User": [G.USER_IJ]}) == []
    assert gtools._related_children(cat.objects["Account"], owner, {"User": [G.USER_IJ]})   # the account: kept


def test_a_trainer_count_gets_no_parent_path_and_no_same_role_rows(gtools):
    cat = gtools.catalog
    ct = cat.objects["Candidate_Training__c"]
    assert gtools._parent_paths(ct, ct.fields["Assigned_Trainer__c"]) == []
    r = ok(gtools, "count_records", {"object": "Candidate_Training__c",
                                     "filters": [F_("Assigned_Trainer__c", "Golf Harbor")]})
    lc = r["lookup_check"][0] if r.get("lookup_check") else {}
    assert "paths" not in lc and "any_path" not in lc and not any(x["same_role"] for x in lc.get("related") or [])


def test_attendee_rows_get_no_parent_paths_for_any_lookup(gtools):
    att = gtools.catalog.objects["Session_Attendee__c"]
    assert gtools._role_rows(att) and gtools._role_rows(att, att.fields["Attendee_Recruiter__c"])
    assert gtools._parent_paths(att, att.fields["Attendee_Recruiter__c"]) == []


def test_same_name_records_get_their_own_paths(gtools):
    r = ok(gtools, "count_records", {"object": "Session__c", "filters": [F_("Candidate__c", "November Oak")]})
    lc = r["lookup_check"][0]
    assert r["count"] == 1 and lc["any_path"] == 2
    assert [(b["record"], b["records"], b["any_path"]) for b in lc["by_record"]] == [
        ("November Oak (#1)", 0, 1), ("November Oak (#2)", 1, 1)]
    note = next(n for n in r["notes"] if "through the parents of Session__c records" in n)
    assert note.endswith("(the same-name records together); per record: #1 1, #2 1")
    assert _bound(gtools, G.CAND_N1, G.CAND_N2)


def test_path_counts_run_in_their_own_statement_and_never_fail_the_check(gtools, monkeypatch):
    args = {"object": "Session__c", "filters": [F_("Candidate__c", "Candidate 1")]}
    ok(gtools, "count_records", args)
    sqls = [st.sql for st in ev_last(gtools).statements]
    check = next(x for x in sqls if '"n0"' in x)
    assert "pa1" not in check and '"ap"' not in check            # the lookup check's own statement, as before
    assert any('"ap"' in x and '"n0"' not in x for x in sqls)
    from sf_assistant.engine.query_builder import Frag
    import sf_assistant.tools.lookups as L
    monkeypatch.setattr(L.LookupsMixin, "_path_frag", staticmethod(lambda *a, **k: Frag("no_such_column = 1")))
    r = ok(gtools, "count_records", args)
    lc = r["lookup_check"][0]
    assert "paths" not in lc and "any_path" not in lc and "others" in lc
    assert any("were not counted" in n for n in r["notes"])


def test_the_copy_note_says_the_source_count_once(gtools):
    r = ok(gtools, "count_records", {"object": "Account", "filters": [F_("Assigned_Recruiter__c", "Kilo Zephyr")]})
    lc = r["lookup_check"][0]
    assert not any(x["object"] == "Marketing__c" for x in lc.get("related") or [])   # copy_source counts it
    held = [n for n in r["notes"] if "through the source" in n]
    assert len(held) == 1 and held[0].startswith("Account.Assigned_Recruiter__c is a partial copy")
    assert "3 different Account records have a Marketing__c row whose Recruiter__c is 'Kilo Zephyr'" in held[0]
    assert "the copy counted 1" in held[0]


def test_link_others_come_from_the_candidates_side(gtools):
    r = ok(gtools, "about_people", {"names": ["golf harbor", "kilo quill"]})
    refs = {p["label"]: p["ref"] for p in r["people"]}
    link = next(x for x in r["links"] if x["how"] == "same record" and "Assigned_Trainer__c" in x["path"])
    oth = link["others"]                  # the trainer has fewer rows, but the candidate's other trainers are listed
    assert oth["of"] == refs["Kilo Quill"] and oth["field"] == "Candidate_Training__c.Assigned_Trainer__c"
    assert set(oth["names"]) == {"Mike Nectar (1)", "Sierra Tango (1)"}


def test_the_keys_note_explains_the_new_keys_present(gtools):
    r = ok(gtools, "about_people", {"names": ["india juno"]})
    note = next(n for n in r["notes"] if n.startswith("people[]") or n.startswith("references[]"))
    assert "references[].distinct: how many different candidates" in note and "never add" in note
    assert "links[].others" not in note                         # no link: not explained


def test_own_record_links_name_their_holder(gtools):
    r = ok(gtools, "about_people", {"names": ["romeo stark", "papa quinn"]})
    refs = {p["label"]: p["ref"] for p in r["people"]}
    own = [x for x in r.get("links") or [] if x["how"] == "own record"]
    assert own and all(x["holder"] in x["between"] for x in own)
    assert any(x["holder"] == refs["Romeo Stark"] and "Assigned_Sr_Recruiter__c" in x["path"] for x in own)


def test_every_group_b_note_carries_a_detail_mark(gtools):
    """agent/checks.facts_json drops the Group B notes by tools.record_base.DETAIL_NOTE_MARKS: each note those fixes
    write must carry one (a rewording that drops the mark fails here)."""
    from sf_assistant.agent.checks import _strip_details
    from sf_assistant.tools.record_base import DETAIL_NOTE_MARKS
    calls = [("count_records", {"object": "Session__c", "filters": [F_("Candidate__c", "Candidate 1")]}),
             ("count_records", {"object": "Account", "filters": [F_("OwnerId", "India Juno")]}),
             ("count_records", {"object": "Account", "filters": [F_("Assigned_Recruiter__c", "Kilo Zephyr")]}),
             ("list_records", {"object": "Internal_Interview__c", "fields": ["Name", "Interviewer__c", "Status__c"],
                               "filters": [F_("Candidate__c", "Juliet Converse")]}),
             ("about_people", {"names": ["india juno"]})]
    words = ("through the parents", "sharing a word", "also_held", "child_people", "partial copy of",
             "never add these counts to each other")
    seen = 0
    for tool, args in calls:
        r = ok(gtools, tool, args)
        for n in r.get("notes") or []:
            if any(w in n for w in words):
                seen += 1
                assert any(m in n for m in DETAIL_NOTE_MARKS), n
        entry = {"result": r}
        _strip_details(entry)
        assert not any(any(w in n for w in words) for n in entry["result"].get("notes") or [])
    assert seen >= 6
