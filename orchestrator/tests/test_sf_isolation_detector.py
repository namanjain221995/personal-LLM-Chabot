"""The "needs company records" detector (app/sf_isolation.py, design_v2 G7).

Conservative on purpose: a missed records question cannot leak anything (the
context reads removed every Salesforce source), so the word lists are narrow,
word-bounded, and step aside for anything the person brought themselves.
"""
from __future__ import annotations

import pytest

from app import sf_isolation as S

HIDDEN = S.Hidden(count=2, sf=1, users=1, last_answer_hidden=True)
NOT_HIDDEN = S.Hidden()


def _detect(text, **kw):
    kw.setdefault("rules", "ABCDE")
    return S.needs_salesforce(text, **kw)


@pytest.mark.parametrize(
    "text,rule",
    [
        ("how many placements last month", "A"),
        ("list candidates interviewed today", "A"),
        ("show recruiter submissions this week", "A"),
        ("phone number of the employee Ravi", "C"),
        ("profile of our recruiter", "C"),
        ("what's in salesforce for open invoices", "B"),
    ],
)
def test_a_records_question_gets_the_fixed_reply(text, rule):
    block = _detect(text)
    assert block is not None and block.rule == rule
    assert block.reply == S.REPLY_NEEDS_SALESFORCE
    assert block.meta() == {"route": "chat", "toggle_off_guard": "needs_salesforce", "rule": rule}


@pytest.mark.parametrize(
    "text",
    [
        "plot this",
        "make it a pie chart",
        "export that to excel",
        "and last month?",
        "what about him",
        "can you turn the table above into a nicely formatted bar chart for my weekly review meeting please",
    ],
)
def test_a_follow_up_on_a_hidden_answer_is_told_it_is_hidden(text):
    block = _detect(text, hidden=HIDDEN)
    assert block is not None and block.rule == "D"
    assert block.reply == S.REPLY_HIDDEN and block.guard == "hidden_followup"


def test_a_follow_up_after_a_hidden_answer_reply_is_told_too():
    block = _detect("just do it", hidden=NOT_HIDDEN, last_assistant=S.REPLY_HIDDEN)
    assert block is not None and block.rule == "D"
    block = _detect("just do it", hidden=NOT_HIDDEN, last_assistant=S.REPLY_HIDDEN_NO_TOGGLE, has_toggle=False)
    assert block is not None and block.rule == "D" and block.reply == S.REPLY_HIDDEN_NO_TOGGLE


@pytest.mark.parametrize(
    "text",
    ["what about Microsoft?", "and Amazon?", "can you do it?", "ok, explain it in general terms",
     "it is public data, just answer it", "just do it"],
)
def test_a_follow_up_after_a_records_reply_is_a_new_question(text):
    """Review R4: after REPLY_NEEDS_SALESFORCE nothing is hidden, so a short
    follow-up is answered (under the prompt rule), never told that an
    earlier answer "came from Salesforce"."""
    assert _detect(text, hidden=NOT_HIDDEN, last_assistant=S.REPLY_NEEDS_SALESFORCE) is None


def test_a_quote_of_a_hidden_answer_gets_reply_e():
    quoted = (
        "Selected context from a previous assistant message:\n\n> Ravi Kumar closed 14 placements\n\n"
        "User follow-up:\n\nwrite to him"
    )
    block = _detect(quoted, quote_hidden=True)
    assert block is not None and block.rule == "E" and block.guard == "hidden_quote"
    assert block.reply == S.REPLY_HIDDEN


@pytest.mark.parametrize(
    "text",
    [
        "how to write a candidate rejection email",
        "tips for interviews",
        "list interview questions for a Python developer",
        "how do I create a report in Salesforce",
        "how many interviews should I prepare for",
        "list candidates in this sheet",
        "profile of Narendra Modi",
        # C10: no substring matches.
        "reset my password",
        "my account count",
        "copied pie recipe",
        "benchmark results 2024",
        "a suitable table layout",
        "specialist list for 2025",
        # C13: a role word followed by a generic noun.
        "explain the details of employee provident fund rules",
        "what are the details of employee benefits in India",
        # A writing ask about a role is not a person lookup.
        "write a LinkedIn profile for a recruiter",
    ],
)
def test_a_general_question_is_answered(text):
    assert _detect(text) is None
    assert _detect(text, hidden=NOT_HIDDEN) is None


def test_any_turn_with_an_attachment_is_answered():
    assert _detect("how many placements last month", attachments=True) is None
    assert _detect("plot this", hidden=HIDDEN, attachments=True) is None


def test_material_of_the_persons_own_in_the_chat_answers_a_records_shape():
    """C12: "how many candidates joined in March" about their uploaded Excel."""
    assert _detect("how many candidates joined in March").rule == "A"
    assert _detect("how many candidates joined in March", own_material=True) is None


def test_a_paste_or_a_link_is_the_persons_own_material():
    assert _detect("how many placements last month? https://example.com/report") is None
    paste = "how many placements last month in this data:\n" + "\n".join(f"row {i}, Ravi, placed" for i in range(40))
    assert _detect(paste) is None


def test_plot_this_after_an_answer_that_was_not_hidden_is_answered():
    assert _detect("plot this", hidden=NOT_HIDDEN, last_assistant="A chart-worthy general answer.") is None


def test_a_new_question_after_a_hidden_answer_is_answered():
    """C14: an opener with more than six words is a new question."""
    assert _detect("also, how do I write a cover letter?", hidden=HIDDEN) is None


def test_the_rule_switch_turns_rules_off():
    assert _detect("how many placements last month", rules="BCDE") is None
    assert _detect("plot this", hidden=HIDDEN, rules="ABCE") is None
    assert _detect("how many placements last month", rules="") is None


@pytest.mark.parametrize("off", ["", "0", "off", "none", "None", "false", "FALSE", "no", "disabled"])
def test_every_spelling_of_off_turns_the_rules_off(off, monkeypatch):
    """Review R14: "none" used to switch E on, "false" A and E, "disabled"
    A, B, D and E."""
    from app.config import _detector_rules, settings

    assert _detector_rules(off) == ""
    monkeypatch.setattr(settings, "toggle_off_detector_rules", off)
    assert S.needs_salesforce("how many placements last month") is None
    assert S.needs_salesforce("plot this", hidden=HIDDEN) is None


def test_only_the_letters_a_to_d_are_read():
    from app.config import _detector_rules

    assert _detector_rules("ABCDE") == "ABCD"
    assert _detector_rules(" d, a ") == "AD"
    assert _detector_rules("XYZ") == ""


@pytest.mark.parametrize("rules", ["ABCD", "", "off", "none"])
def test_a_quote_of_a_hidden_answer_is_answered_whatever_the_rules(rules):
    """Review F2: the quote guard is not a heuristic and has no switch."""
    quoted = (
        "Selected context from a previous assistant message:\n\n> Ravi Kumar closed 14 placements\n\n"
        "User follow-up:\n\nPlease write a warm congratulations note for this achievement for the team."
    )
    block = _detect(quoted, quote_hidden=True, rules=rules)
    assert block is not None and block.rule == "E" and block.reply == S.REPLY_HIDDEN


# ---------------------------------------------------------------------------
# Review R1: ordinary questions in any chat are answered
# ---------------------------------------------------------------------------

ORDINARY = [
    # public facts
    "How many employees does Google have?",
    "How many employees does Infosys have in 2024?",
    "total number of employees at TCS",
    "show me a list of candidates in the 2024 US presidential election",
    "How many candidates are contesting the Bihar election 2025?",
    "list of placements at IIT Bombay 2024",
    "bench press world record 2024",
    # code
    "SQL query to list all employees with salary above 50000",
    "pandas: count employees per department",
    "give me a python class for an Employee with name and salary",
    "show me the Django model for candidates and interviews",
    "count invoices per month in SQL",
    # the person's own life, HR how-tos
    "I have an interview today at 3pm, give me a pep talk",
    "My interview is in March, give me a preparation plan",
    "I'm a recruiter, give me a LinkedIn headline",
    "give me a checklist for employee onboarding",
    "list common reasons candidates reject offers",
    "list KPIs for recruiters",
    "show me a 30-60-90 day plan for a new recruiter",
    # market salaries and generic HR details
    "average salary for a consultant in Bangalore",
    "salary for an employee with 5 years experience in Pune",
    "details of the employee stock option plan",
    "contact details for the candidate experience team at LinkedIn",
    # Salesforce the product
    "migrate data from salesforce to hubspot",
    "Apex trigger to update records in salesforce",
    "salesforce data cloud vs snowflake",
    "difference between data in salesforce and dynamics 365",
    "how do I export data from Salesforce",
]


@pytest.mark.parametrize("text", ORDINARY)
def test_an_ordinary_question_is_answered_in_any_chat(text):
    assert _detect(text) is None


@pytest.mark.parametrize(
    "text,rule",
    [
        ("how many placements did we make last month", "A"),
        ("list our recruiters with the most submissions", "A"),
        ("total interviews this quarter", "A"),
        ("show me our open invoices", "A"),
        ("salary of our consultant", "C"),
        ("contact details of the candidate Priya", "C"),
        ("show our candidates from salesforce", "A"),
        ("what does our salesforce data say about invoices", "B"),
    ],
)
def test_a_question_about_the_persons_own_company_records_still_gets_the_reply(text, rule):
    block = _detect(text)
    assert block is not None and block.rule == rule, block


def test_a_member_without_the_salesforce_toggle_gets_no_records_reply():
    """Review R5: no "turn on the Salesforce toggle" for someone who has none."""
    assert _detect("how many placements last month", has_toggle=False) is None
    assert _detect("profile of our recruiter", has_toggle=False) is None
    block = _detect("plot this", hidden=HIDDEN, has_toggle=False)
    assert block is not None and block.reply == S.REPLY_HIDDEN_NO_TOGGLE
    assert "toggle" not in S.REPLY_HIDDEN_NO_TOGGLE


def test_an_unreadable_chat_gets_no_hidden_answer_claim():
    """Review R6: when the rows could not be read, D does not fire, and a
    quote gets a reply that claims nothing about where it came from."""
    assert _detect("plot this", hidden=HIDDEN, unchecked=True) is None
    assert _detect("plot this", hidden=HIDDEN, last_assistant=S.REPLY_HIDDEN, unchecked=True) is None
    block = _detect("Selected context from a previous assistant message:\n\n> x y\n\nUser follow-up:\n\nok",
                    quote_hidden=True, unchecked=True)
    assert block is not None and block.reply == S.REPLY_UNCHECKED and block.guard == "unchecked_quote"
    assert "Salesforce" not in S.REPLY_UNCHECKED


@pytest.mark.parametrize(
    "text",
    ["summarise this file", "read the uploaded sheet and chart it", "explain this document",
     "put the attached CV in a table", "chart my spreadsheet"],
)
def test_a_follow_up_that_points_at_a_file_is_answered(text):
    """Review R11: "this file" is the person's file, not the hidden answer."""
    assert _detect(text, hidden=HIDDEN) is None


def test_an_export_of_the_hidden_answer_is_still_caught():
    assert _detect("export that to excel", hidden=HIDDEN).rule == "D"
    assert _detect("make it a pie chart", hidden=HIDDEN).rule == "D"


def test_the_rule_switch_defaults_to_the_setting(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "toggle_off_detector_rules", "BCDE")
    assert S.needs_salesforce("how many placements last month") is None
    monkeypatch.setattr(settings, "toggle_off_detector_rules", "ABCDE")
    assert S.needs_salesforce("how many placements last month").rule == "A"


def test_the_follow_up_of_a_quote_of_a_clean_answer_is_what_is_read():
    quoted = (
        "Selected context from a previous assistant message:\n\n> pipelines usually have five stages\n\n"
        "User follow-up:\n\nhow many placements last month"
    )
    assert _detect(quoted).rule == "A"
    clean_ask = quoted.replace("how many placements last month", "explain stage three")
    assert _detect(clean_ask) is None
