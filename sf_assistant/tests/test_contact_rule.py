"""The phase-1 contact rule (owner, 2026-10-26; docs/data_and_privacy.md): admins get contact details when the
question asks for them, and nobody gets them without asking.

* ``engine.contact.asks_for_contact``: the question cue (English, misspellings one edit away, possessives, romanized
  Hindi and Gujarati, script words, an e-mail address in the question), and the questions that do not ask (counts,
  objects, the presence of a contact field, e-mail messages, phone calls, the location of a session; review F5);
* every record tool, asked and not asked: list_records (the column left out with a note that tells the model to name
  people, never one that says the question asked; nothing else left: the default columns; a contact path through a
  lookup: the lookup's name instead, review F9 / I-2), get_field_values, group_records and its split, a filter (its
  value hidden in the applied text), get_field_history (old and new values, count_by new_value with and without a
  field, the statement rows themselves, the changed_to error; review F1, I-3) and the about_people profile;
* ``engine.contact.drop_contact_details``: e-mail addresses and phone numbers replaced, dates, ranges, counts, amounts,
  record numbers, Ids and the numbers the tool results hold as whole values kept (review F6); ``RecordSession.ask``
  and ``pipeline.answer`` run it on an answer of any kind whose question did not ask (records on), and the knowledge
  files a records-off run answers from hold no contact details (review F7);
* links are no contact details: a url field is readable, and the passcodes and tokens inside a stored link, and the
  labelled passcodes, passwords and identity numbers of free text, stay hidden (``engine.catalog.hide_embedded_secrets``,
  very high; review F3), also in the RECORDS block (review F4).

Synthetic warehouse and knowledge only (the e-mail addresses end in example.invalid).
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

import warehouse_fixture as F  # noqa: E402
from sf_assistant.engine.catalog import SECRET_PARAM_RE, contact_text, hide_embedded_secrets  # noqa: E402
from sf_assistant.engine.contact import (EMAIL_SHOWN, PHONE_SHOWN, asks_for_contact,  # noqa: E402
                                         drop_contact_details, result_numbers)

ENV = ("OKEVAL_WAREHOUSE", "OKEVAL_WAREHOUSE_CONTAINER", "OKEVAL_RECORDS_TZ", "OKEVAL_RECORDS_TODAY",
       "OKEVAL_RECORDS_TIMEOUT_S", "OKEVAL_RECORDS_MAX_STEPS", "OKEVAL_RECORDS_MAX_AGE_H")
IV = [F.sid("a0A", i) for i in (41, 42)]
MAILS = ("iv1@example.invalid", "iv2@example.invalid", "iv0@example.invalid")
JOIN = "https://zoom.example.invalid/j/8123?pwd=ZQPASS1&from=addon"
NOT_ASKED = "Who were the interviewers of the client interviews of Candidate 1?"
ASKED = "What are the e-mail addresses of the interviewers of Candidate 1's client interviews?"

ASKS = [
    "What is the email of Recruiter A?", "give me E-Mail ID of the interviewer", "Mail id bhejo", "emial of X",
    "send me the email address of recruiter a",
    "uska number batao", "mobile number do", "Phone no of candidate X", "whatsapp number of the trainer",
    "her number", "number to call the trainer", "X ka contact number", "phn of the candidate",
    "contact details of the recruiter", "how can I reach him", "contact kaise kare X se", "sampark",
    "personal details of Kilo", "get in touch with the support person",
    "where does Kilo live", "candidate ka address kya hai", "adress of X", "zip code of the candidate", "pincode",
    "send me her linkedin", "linkdin profile of X",
    "which IP addresses logged in", "what browser did she use", "which devices did the users log in from",
    "ईमेल बताओ", "ફોન નંબર આપો",
    "interviewers with their emails",
    # review F5: possessives, plain forms, misspellings one edit away, an e-mail address in the question
    "Kilo ka number kya hai", "Kilo ka contact batao", "contact of the trainer", "what is her contact?", "ph no of Kilo",
    "mail of the recruiter", "candidate ka mail", "Kilo kidhar rehta hai", "Kilo kaha rehti hai", "Kilo ka pata",
    "Kilo's phon number", "contect number of Kilo", "contact detials of Kilo", "Kilo's e-mial", "emai of Kilo",
    "wa number of Kilo", "Kilo nu number aapo", "which city does Kilo live in", "the candidate's location",
    "current location of the trainer", "details to contact Kilo", "can I call him? number please", "cell of Kilo",
    "trainer ki emial id do", "who has kilo@example.invalid",
]
DOES_NOT_ASK = [
    "", "how many interviews ended with an offer?", "number of interviews this month", "how many contacts are there",
    "who is the point of contact for X", "their number of sessions", "list the 3 newest client interviews",
    "kitne candidates ke interviews scheduled hai", "tell me about delta person", "what did kilo do",
    "how was the issue addressed", "his no show count", "Contact object fields", "interview status by recruiter",
    "which flows send emails?", "how many emails were sent this week", "how many phone calls did recruiter a make",
    "what is the email template of the onboarding flow", "who are the interviewers of Candidate 1",
    # review F5: a count or the presence of a contact field, the field itself, a session's location, a mobile app,
    # phone interviews, contracts, the contact object
    "How many leads have an email?", "How many candidates have no phone number?", "count of accounts without email",
    "which candidates are missing linkedin", "the address field is filled for how many accounts", "device count",
    "what is the location of the session", "how many mobile app users", "Kilo's number of submissions",
    "how many phone interviews this week", "contract roles by client", "who is the primary contact for the client",
    "list contacts of the account", "kitne leads ke paas email hai", "Kilo ka number of interviews",
]


def test_asks_for_contact():
    assert [q for q in ASKS if not asks_for_contact(q)] == []
    assert [q for q in DOES_NOT_ASK if asks_for_contact(q)] == []
    assert asks_for_contact(None) is False


def test_drop_contact_details_keeps_dates_counts_amounts_and_record_numbers():
    text, n = drop_contact_details("Ask iv1@example.invalid or +91 98765 43210, (555) 123-4567, 9876543210, "
                                   "+1-555-123-4567 or 555.123.4567.")
    assert n == 6 and "@" not in text and text.count(PHONE_SHOWN) == 5 and EMAIL_SHOWN in text
    for keep in ("Synced 2026-10-13 14:12 UTC.", "37 interviews, 2,562 records, 6.80% and 1,234,567,890.",
                 "Counts 12 15 18 20 25 and 1000 - 2000 - 3000.", "REC-00024, INT-0000012345, I-0001 and 12345678.90.",
                 "Record a0A000000000041AAA.", "Between 2026-10-01 and 2026-10-31.", "Meeting ID 812 3456 7890.",
                 "Zoom_Meeting_Id__c: 81234567890", "webinar 81234567890"):
        assert drop_contact_details(keep) == (keep, 0), keep
    # a date in front of a phone number does not hide it
    assert drop_contact_details("Called 2026-10-13 9876543210.") == (f"Called 2026-10-13 {PHONE_SHOWN}.", 1)


def test_numbers_the_results_hold_are_kept():
    """Review F6: a meeting id written as the answer's plain number stays when the tool results hold it as a whole
    value; a number only inside a longer text of the results does not count."""
    keep = result_numbers([{"rows": [{"Name": "INT-12345", "Zoom_Meeting_Id__c": "81234567890"}],
                            "groups": [{"group": "812 3456 7899", "value": 2}],
                            "notes": ["call 9876543210 to reschedule"]}, None])
    assert keep == {"81234567890", "81234567899"}
    for text in ("The Zoom meeting of INT-12345 is 81234567890.", "| INT-12345 | 81234567890 |",
                 "- INT-1: 812 3456 7899"):
        assert drop_contact_details(text, keep) == (text, 0), text
        assert drop_contact_details(text)[1] == 1, text
    assert drop_contact_details("Call 9876543210.", keep) == (f"Call {PHONE_SHOWN}.", 1)


def test_labelled_secrets_in_free_text_stay_hidden():
    """Review F3: a passcode, password, OTP or key after its label, and a government identity or bank number after its
    label, become [hidden]; the label and the rest stay."""
    for raw, want in (("Meeting ID: 812 3456 7890\nPasscode: ab12cd\nJoin", "Meeting ID: 812 3456 7890\nPasscode: [hidden]\nJoin"),
                      ('{"zoom_passcode": "ab12", "x": 1}', '{"zoom_passcode": "[hidden]", "x": 1}'),
                      ("Meeting Password | Password: 123456", "Meeting Password | Password: [hidden]"),
                      ("OTP = 4321 sent", "OTP = [hidden] sent"), ("SSN no.: 123-45-6789", "SSN no.: [hidden]"),
                      ("Passport No X1234567 valid", "Passport No [hidden] valid"),
                      ("Bank account no: 1234", "Bank account no: [hidden]"),
                      ("Passcode: \n\nMeeting: 3pm", "Passcode: \n\nMeeting: 3pm"),
                      ("Password reset: done", "Password reset: done"), ("passport number is pending", "passport number is pending"),
                      ("carbon footprint: 5", "carbon footprint: 5"), ("Japan no 1", "Japan no 1")):
        assert hide_embedded_secrets(raw) == want, raw
    assert hide_embedded_secrets("?pwd=[hidden]&x=1") == "?pwd=[hidden]&x=1"


def test_secrets_inside_links_stay_hidden():
    assert hide_embedded_secrets(JOIN) == "https://zoom.example.invalid/j/8123?pwd=[hidden]&from=addon"
    for raw, want in (("https://z.example.invalid/s/1?zak=eyJ0&x=1", "https://z.example.invalid/s/1?zak=[hidden]&x=1"),
                      ("https://f.example.invalid/s/form?flowtoken=abc%3D", "https://f.example.invalid/s/form?flowtoken=[hidden]"),
                      ('{"start_url": "https://z.example.invalid/s/1?zak=tok"}',
                       '{"start_url": "https://z.example.invalid/s/1?zak=[hidden]"}'),
                      ("https://d.example.invalid/x?usp=sharing&utm_source=a", "https://d.example.invalid/x?usp=sharing&utm_source=a"),
                      ("a=b and no link", "a=b and no link")):
        assert hide_embedded_secrets(raw) == want, raw
    assert SECRET_PARAM_RE.search("?access_token=1") and SECRET_PARAM_RE.search("&X-Amz-Credential=1")


def test_contact_text_never_says_the_question_asked():
    t = contact_text("Interview__c", "Interviewer_s_Email__c")
    assert "The question does not ask for contact details" in t and "identify people by name" in t
    assert "asked for" not in t and "because the question" not in t


@pytest.fixture(scope="module")
def tools(tmp_path_factory):
    from sf_assistant.config import load_records_config
    from sf_assistant.tools.open_records import open_records

    tables = copy.deepcopy(F.TABLES)
    day = "2026-10-01T09:00:00.000Z"
    F._with(tables, "Interview__c", {"Interviewer_s_Email__c": None, "Zoom_Join_URL__c": None, "Resume_Link__c": None}, [
        {"Id": IV[0], "Name": "I-0041", "IsDeleted": "false", "OwnerId": F.USER_1, "CreatedDate": day,
         "Interview_Status__c": "Completed", "Recruiter__c": F.REC_A, "Candidate__c": F.CAND_1,
         "Interviewer_s_Email__c": MAILS[0], "Zoom_Join_URL__c": JOIN, "Resume_Link__c": "https://r.example.invalid/1"},
        {"Id": IV[1], "Name": "I-0042", "IsDeleted": "false", "OwnerId": F.USER_1, "CreatedDate": day,
         "Interview_Status__c": "Completed", "Recruiter__c": F.REC_A, "Candidate__c": F.CAND_1,
         "Interviewer_s_Email__c": MAILS[1]}])
    F._with(tables, "Interview__History", {}, [
        {"Id": F.sid("017", 41), "IsDeleted": "false", "ParentId": IV[0], "CreatedById": F.USER_1,
         "CreatedDate": "2026-10-02T10:00:00.000Z", "Field": "Interviewer_s_Email__c", "DataType": "Text",
         "OldValue": MAILS[2], "NewValue": MAILS[0]},
        {"Id": F.sid("017", 42), "IsDeleted": "false", "ParentId": IV[0], "CreatedById": F.USER_1,
         "CreatedDate": "2026-10-03T10:00:00.000Z", "Field": "Zoom_Join_URL__c", "DataType": "Text",
         "OldValue": None, "NewValue": JOIN}])
    knowledge = copy.deepcopy(F.KNOWLEDGE)
    by = {x["object"]["api_name"]: x for x in knowledge}
    by["Interview__c"]["fields"] += [
        F._field("Interviewer_s_Email__c", "Interviewer's Email", "email", "E-mail address of the client interviewer."),
        F._field("Zoom_Join_URL__c", "Zoom Join URL", "url", "Join link of the interview's Zoom meeting."),
        F._field("Resume_Link__c", "Resume Link", "url", "Link to the candidate's resume.")]
    root = tmp_path_factory.mktemp("contact_rule")
    with pytest.MonkeyPatch.context() as mp:
        for name in ENV:
            mp.delenv(name, raising=False)
        t = open_records(load_records_config(warehouse=str(F.make_warehouse(root, tables=tables)),
                                             today=dt.date(2026, 10, 13)),
                         str(F.make_knowledge(root, knowledge=knowledge)))
    yield t
    t.close()


def _call(t, name, args, question) -> tuple[dict, str | None, str]:
    t.begin_question(question)
    out = t.call(name, args)
    return out, (t.evidence[-1].error_code if t.evidence else None), json.dumps(out)


def _mails(text: str) -> list[str]:
    return [m for m in MAILS + ("a@example.invalid", "c1@example.invalid") if m in text]


def test_contact_fields_and_links_in_the_catalog(tools):
    f = tools.catalog.objects["Interview__c"].fields
    assert f["Interviewer_s_Email__c"].sensitivity == "contact"
    assert f["Zoom_Join_URL__c"].sensitivity is None and f["Resume_Link__c"].sensitivity is None   # a link: no contact
    assert tools._contact_ok is False
    tools.begin_question(ASKED)
    assert tools._contact_ok is True and tools.build_ctx().contact_ok is True
    tools.begin_question("")
    assert tools._contact_ok is False and tools.build_ctx().contact_ok is False


def test_list_records_leaves_a_contact_column_out_unless_asked(tools):
    args = {"object": "Interview__c", "fields": ["Name", "Recruiter__c", "Interviewer_s_Email__c"],
            "filters": [{"field": "Candidate__c.Name", "op": "eq", "value": "Candidate 1"}], "limit": 20}
    out, code, text = _call(tools, "list_records", args, NOT_ASKED)
    assert out["ok"], out["error"]
    r = out["result"]
    assert "Interviewer_s_Email__c" not in r["columns"] and not _mails(text)
    assert "left out Interviewer_s_Email__c: contact details are shown only when the user's question asks for them, " \
           "and this question does not; identify people by name" in r["notes"]
    assert not [n for n in r["notes"] if "question asks for contact" in n or "question asked" in n]
    assert all('"Interviewer_s_Email__c"' not in st.sql for ev in tools.evidence for st in ev.statements)
    out, code, text = _call(tools, "list_records", args, ASKED)
    assert out["ok"], out["error"]
    r = out["result"]
    assert "Interviewer_s_Email__c" in r["columns"] and MAILS[0] in text and MAILS[1] in text
    assert "contact details are shown: the question asks for contact details" in r["notes"]
    # only contact columns asked for, not asked: Name and the default columns (review I-2)
    spec = tools.catalog.objects["Interview__c"]
    want = ["Name"] + [f"{f.api} (name)" if f.kind == "reference" else f.api for f in tools._default_list_fields(spec)]
    out, code, text = _call(tools, "list_records", {"object": "Interview__c", "fields": ["Interviewer_s_Email__c"]},
                            NOT_ASKED)
    assert out["ok"] and out["result"]["columns"] == want and len(want) > 2 and not _mails(text)
    # a contact path through a lookup, not asked: the lookup's name instead (review F9)
    out, code, text = _call(tools, "list_records", {"object": "Interview__c", "fields": ["Recruiter__c.Email__c"]},
                            NOT_ASKED)
    assert out["ok"] and out["result"]["columns"] == ["Name", "Recruiter__c (name)"] and not _mails(text)
    assert any(n.startswith("left out Recruiter__c.Email__c: contact details are shown only") and
               n.endswith("(listed instead: Recruiter__c (name))") for n in out["result"]["notes"])
    out, code, text = _call(tools, "list_records", {"object": "Interview__c", "fields": ["Recruiter__c.Email__c"]},
                            ASKED)
    assert out["ok"] and out["result"]["columns"] == ["Name", "Recruiter__c.Email__c"] and _mails(text)


@pytest.mark.parametrize("name,args", [
    ("get_field_values", {"object": "Interview__c", "field": "Interviewer_s_Email__c"}),
    ("group_records", {"object": "Interview__c", "group_by": "Interviewer_s_Email__c"}),
    ("group_records", {"object": "Interview__c", "group_by": "Interview_Status__c", "split_by": "Interviewer_s_Email__c"}),
    ("get_field_values", {"object": "Recruiter__c", "field": "Email__c"}),
])
def test_values_and_groups_of_a_contact_field_only_when_asked(tools, name, args):
    out, code, text = _call(tools, name, args, NOT_ASKED)
    assert out["ok"] is False and code == "E2d" and not _mails(text), (name, out)
    assert out["error"] == contact_text(*(("Interview__c", "Interviewer_s_Email__c") if args["object"] == "Interview__c"
                                          else ("Recruiter__c", "Email__c")))
    out, code, text = _call(tools, name, args, ASKED)
    assert out["ok"], (name, code, out["error"])
    assert _mails(text), name


def test_a_filter_on_a_contact_field_hides_its_value_unless_asked(tools):
    args = {"object": "Interview__c", "filters": [{"field": "Interviewer_s_Email__c", "op": "eq", "value": MAILS[0]}]}
    out, code, text = _call(tools, "count_records", args, NOT_ASKED)
    assert out["ok"] and out["result"]["count"] == 1 and not _mails(text)
    assert "Interviewer_s_Email__c = <value hidden>" in out["result"]["applied"]["filters"]
    out, code, text = _call(tools, "count_records", args, ASKED)
    assert out["ok"] and out["result"]["count"] == 1
    assert f"Interviewer_s_Email__c = '{MAILS[0]}'" in out["result"]["applied"]["filters"]


def _statement_text(t) -> str:
    return json.dumps([list(r) for ev in t.evidence for st in ev.statements for r in st.rows], default=str)


def test_history_of_a_contact_field_only_when_asked(tools):
    """Old and new values with and without a field, count_by new_value with and without a field (review F1): the SQL
    selects '(hidden)', so no statement row (the RECORDS block, review F4) holds a contact value either."""
    calls = [{"object": "Interview__c", "field": "Interviewer_s_Email__c"},
             {"object": "Interview__c", "field": "Interviewer_s_Email__c", "count_by": "new_value"},
             {"object": "Interview__c"}, {"object": "Interview__c", "count_by": "new_value"}]
    for args in calls:
        out, code, text = _call(tools, "get_field_history", args, NOT_ASKED)
        assert out["ok"] and not _mails(text) and "(hidden)" in text, (args, out["error"])
        assert not _mails(_statement_text(tools)), args
        out, code, text = _call(tools, "get_field_history", args, ASKED)
        assert out["ok"] and MAILS[0] in text and "(hidden)" not in text, (args, out["error"])
    # the Zoom link's history row is no contact detail: shown, without its passcode
    out, code, text = _call(tools, "get_field_history", {"object": "Interview__c", "field": "Zoom_Join_URL__c"}, NOT_ASKED)
    assert out["ok"] and "pwd=[hidden]" in text and "ZQPASS1" not in text
    # changed_to of a contact field, not asked: refused, nothing stored is read (E2d)
    bad = {"object": "Interview__c", "field": "Interviewer_s_Email__c", "changed_to": "nobody@example.invalid"}
    out, code, text = _call(tools, "get_field_history", bad, NOT_ASKED)
    assert code == "E2d" and out["error"] == contact_text("Interview__c", "Interviewer_s_Email__c")
    assert not [m for m in MAILS if m in text] and not _mails(_statement_text(tools))
    out, code, text = _call(tools, "get_field_history", bad, ASKED)
    assert code == "E4" and MAILS[0] in out["error"]
    # the E4 list of stored new values hides a link's passcode (review I-3)
    out, code, text = _call(tools, "get_field_history", {"object": "Interview__c", "field": "Zoom_Join_URL__c",
                                                         "changed_to": "nothing"}, NOT_ASKED)
    assert code == "E4" and "pwd=[hidden]" in out["error"] and "ZQPASS1" not in out["error"]


def test_the_profile_gets_contact_details_only_when_asked(tools):
    args = {"names": ["Recruiter A"]}
    out, code, text = _call(tools, "about_people", args, "tell me about Recruiter A")
    assert out["ok"], out["error"]
    assert "a@example.invalid" not in text and "Email__c" not in text
    out, code, text = _call(tools, "about_people", args, "what is the email id of Recruiter A")
    assert out["ok"], out["error"]
    res = out["result"]
    profiles = [m.get("profile") or {} for m in list(res.get("people") or []) + list(res.get("matched_records") or [])]
    assert any(v == "a@example.invalid" for pr in profiles for k, v in pr.items() if k.startswith("Email__c")), profiles


def test_links_are_readable_without_their_passcodes(tools):
    for question in (NOT_ASKED, ASKED):
        out, code, text = _call(tools, "list_records", {"object": "Interview__c", "fields": ["Zoom_Join_URL__c",
                                                                                              "Resume_Link__c"],
                                                        "filters": [{"field": "Name", "op": "eq", "value": "I-0041"}]},
                                question)
        assert out["ok"], out["error"]
        assert "pwd=[hidden]" in text and "ZQPASS1" not in text and "https://r.example.invalid/1" in text
    out, code, text = _call(tools, "get_field_values", {"object": "Interview__c", "field": "Zoom_Join_URL__c"}, NOT_ASKED)
    assert out["ok"] and "ZQPASS1" not in text and "pwd=[hidden]" in text


def test_the_records_block_hides_passcodes_and_unasked_contact_values(tools, monkeypatch):
    """Review F4: the RECORDS block (CLI, evaluation transcripts, the page's admin trace) prints the statement rows; a
    link's passcode is [hidden] there too, and a history list of a question that does not ask holds no e-mail. The
    cells are printed whole here (CELL_MAX raised), so a cut cannot hide a passcode."""
    from sf_assistant.agent import render
    from sf_assistant.agent.render import format_records_block
    from sf_assistant.agent.results import RecordAnswer

    monkeypatch.setattr(render, "CELL_MAX", 400)
    monkeypatch.setattr(render, "TABLE_MAX", 2000)

    def block(name, args, question):
        _call(tools, name, args, question)
        rec = RecordAnswer(question=question, answer="x", no_records=False, used_data=True, evidence=list(tools.evidence),
                           agent=None, error=None, unsupported_numbers=[], seconds=0.0)
        return format_records_block(rec, tools.status(), 20, verbose=True)

    text = block("list_records", {"object": "Interview__c", "fields": ["Zoom_Join_URL__c"],
                                  "filters": [{"field": "Name", "op": "eq", "value": "I-0041"}]}, NOT_ASKED)
    assert "ZQPASS1" not in text and "pwd=[hidden]&from=addon" in text
    text = block("get_field_history", {"object": "Interview__c"}, NOT_ASKED)
    assert "(hidden)" in text and not _mails(text) and "ZQPASS1" not in text
    assert MAILS[0] in block("get_field_history", {"object": "Interview__c"}, ASKED)


# ------------------------------------------------------------------------------- the answers (record session, pipeline)

def test_the_record_session_drops_contact_details_the_question_did_not_ask_for():
    from test_agent_record_session import COUNT_ARGS, ScriptedLLM, StubTools, _cfg

    from sf_assistant.agent.record_session import RecordSession

    reply = "37 client interviews ended with an offer; the interviewer was iv1@example.invalid."
    script = [{"name": "count_records", "args": COUNT_ARGS}, reply]
    rec = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(script)).ask("How many interviews ended with an offer?")
    assert not rec.no_records and "@" not in rec.answer and EMAIL_SHOWN in rec.answer
    rec = RecordSession(StubTools(), _cfg(), llm=ScriptedLLM(script)).ask(
        "How many interviews ended with an offer, and what is the interviewer's e-mail?")
    assert "iv1@example.invalid" in rec.answer


def test_the_pipeline_drops_contact_details_from_the_final_answer():
    from test_agent_eval_fixes import _cfg, _KnowledgeLLM, _registry, _StubSession

    from sf_assistant import pipeline
    from sf_assistant.agent.record_session import RecordAnswer

    body = ("2 client interviews: interviewer iv1@example.invalid (+1 555 123 4567).\n"
            "Source: preprod sandbox copy, synced 2026-10-13 14:12 UTC.")

    def rec(question):
        return RecordAnswer(question=question, answer=body, no_records=False, used_data=True, evidence=[], agent=None,
                            error=None, unsupported_numbers=[], seconds=1.0)

    o = pipeline.answer(NOT_ASKED, _cfg(), _registry(), None, _KnowledgeLLM(), 1, session=_StubSession(rec(NOT_ASKED)),
                        records_only=True)
    assert o.kind == "records" and "@" not in o.final and "555" not in o.final and o.final == o.body
    assert EMAIL_SHOWN in o.final and PHONE_SHOWN in o.final and "synced 2026-10-13 14:12 UTC" in o.final
    o = pipeline.answer(ASKED, _cfg(), _registry(), None, _KnowledgeLLM(), 2, session=_StubSession(rec(ASKED)),
                        records_only=True)
    assert "iv1@example.invalid" in o.final and "+1 555 123 4567" in o.final
    # records on, a knowledge answer (the record side found nothing): the same check (review F7)
    empty = RecordAnswer(question=NOT_ASKED, answer="", no_records=True, used_data=False, evidence=[], agent=None,
                         error=None, unsupported_numbers=[], seconds=1.0)
    llm = _KnowledgeLLM(answer="The support desk is iv1@example.invalid, +1 555 123 4567.")
    o = pipeline.answer(NOT_ASKED, _cfg(), _registry(), None, llm, 3, session=_StubSession(empty))
    assert o.kind == "knowledge" and "@" not in o.final and "555" not in o.final and EMAIL_SHOWN in o.final


@pytest.mark.skipif(not any((ROOT / "knowledge").rglob("*.json")),
                    reason="the git-ignored knowledge files are not there")
def test_the_knowledge_files_hold_no_contact_details():
    """A records-off run reads no record and imports no record code, so its answers (from the knowledge files) get no
    answer check (review F7): the knowledge files hold no e-mail address and no phone number (kb_build masks them)."""
    hits = [str(p.relative_to(ROOT)) for p in sorted((ROOT / "knowledge").rglob("*.json"))
            if drop_contact_details(p.read_text(encoding="utf-8"))[1]]
    assert hits == []


@pytest.mark.parametrize("name,args", [
    ("group_records", {"object": "Interview__c", "group_by": "Interview_Status__c", "split_by": "Zoom_Join_URL__c"}),
    ("trend_records", {"object": "Interview__c", "grain": "month", "date_field": "CreatedDate",
                       "period": "last 12 months", "split_by": "Zoom_Join_URL__c"}),
])
def test_split_keys_hide_a_links_passcode(tools, name, args):
    out, code, text = _call(tools, name, args, "")
    assert out["ok"], out["error"]
    assert "ZQPASS1" not in text and "pwd=[hidden]" in text and "pwd=[hidden]" in json.dumps(out["result"]["split_totals"])


def test_split_keys_that_hide_to_one_key_add_up():
    from sf_assistant.tools.records import _put_split
    for measure, a, b, want in (("count", 2, 3, 5), ("sum", 1.5, 2.0, 3.5), ("avg", 1.0, 3.0, None)):
        d: dict = {}
        _put_split(d, "Passcode: [hidden]", a, measure)
        _put_split(d, "Passcode: [hidden]", b, measure)
        assert d == {"Passcode: [hidden]": want}
