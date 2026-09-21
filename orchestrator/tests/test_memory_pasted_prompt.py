"""A pasted PROMPT is not the person speaking (2026-09-21).

The owner's production account (13 rows, all written 2026-09-16) holds a
stranger's name, the role that stranger was interviewing for, and the
formatting rules of the script they were interviewing under — one paste of an
"Interview Simulation" prompt, read back on every later turn under "treat as
true for this user". "hello ai ?? What is My name ??" was answered with the
candidate's name, and "hi ??" with the script's 150-180 word bolded
self-introduction. 16 of the 132 rows across the whole store carry that shape.

Release 1's write rules did not stop it: an instruction script has no ALL-CAPS
name banner and no e-mail line, so the document rule never fired, and a
two-paragraph one sits well under the 1,200-character self-disclosure ceiling.
Each test below is one half of the repair — the message rule that refuses the
whole paste (`pasted_instructions` / `own_words`) and the per-fact rules that
catch the same shape arriving any other way (`is_durable`) — and the ordinary
messages that must keep working either way.
"""
import asyncio

import pytest

from app import db
from app.facts import is_durable, own_words, pasted_instructions, remember_from_message


@pytest.fixture()
def owner():
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "c1", "Interview practice")
    return uid


def _fake_complete(reply, calls=None):
    async def complete(messages, **kwargs):
        if calls is not None:
            calls.append(messages)
        return reply

    return complete


def _texts(uid):
    return [f["fact"] for f in db.list_user_facts(uid)]


#: The shape of the paste the owner's 13 rows came from, rebuilt from those
#: rows (the account's real message is not copied here). 438 characters — a
#: third of the ceiling that was supposed to catch a paste.
_INTERVIEW_PROMPT = """Interview Simulation

Act as an interviewer for a designation_of_candidate role at Company_name.
My name is Aa m a n i Nand e n d l a.

Rules:
1. Bold all the keywords in your responses.
2. Answers must be paragraphs, no meta-text.
3. The first answer should be a professional self-introduction of 150-180 words.
4. Answer length should be dynamic based on the question.
5. Handle sensitive questions about visa and salary professionally."""

#: The same script with its template slots filled in, so nothing but the role
#: and the rule list is left to recognise it by.
_INTERVIEW_PROMPT_FILLED = _INTERVIEW_PROMPT.replace(
    "designation_of_candidate", "Senior Data Engineer"
).replace("Company_name", "Acme Rail")


# --- 1. The message rule: a pasted script writes nothing -------------------


def test_the_interview_prompt_is_under_the_paste_ceiling():
    """The measurement that explains why release 1 missed it: length and the
    document banner had nothing to fire on."""
    assert len(_INTERVIEW_PROMPT) == 438
    assert "@" not in _INTERVIEW_PROMPT


def test_a_pasted_interview_prompt_is_not_the_person_speaking():
    assert pasted_instructions(_INTERVIEW_PROMPT) is True
    assert own_words(_INTERVIEW_PROMPT) is None


def test_a_pasted_script_is_recognised_without_its_template_slots():
    """A role for the assistant plus a list of rules for playing it is the
    shape; the unfilled slots only make it obvious."""
    assert pasted_instructions(_INTERVIEW_PROMPT_FILLED) is True
    assert own_words(_INTERVIEW_PROMPT_FILLED) is None


def test_the_interview_paste_stores_nothing_and_is_never_extracted(owner):
    calls = []
    stored = asyncio.run(
        remember_from_message(
            owner,
            _INTERVIEW_PROMPT,
            "c1",
            complete=_fake_complete(
                '{"add": ["The user\'s name is Aa m a n i Nand e n d l a",'
                ' "The user is a professional interviewing for a'
                ' designation_of_candidate role at Company_name",'
                ' "The user requires answers to be formatted as paragraphs'
                ' without meta-text",'
                ' "The user wants the first answer to be a professional'
                ' self-introduction (~150-180 words)"],'
                ' "replace": [], "remove": []}',
                calls,
            ),
        )
    )
    assert stored == []
    assert _texts(owner) == []
    # the extractor is never even asked about somebody else's script
    assert calls == []


@pytest.mark.parametrize(
    "message",
    [
        # one signal only: a role, with no rules for it
        "You are my assistant. My name is Naman.",
        "Act as a translator and translate this: hola",
        # one signal only: a list, with no role
        "My name is Naman. Please:\n1. answer in Hindi\n2. use metric units",
        # neither
        "By the way, my name is Naman and I prefer answers in Hindi.",
        "I'm vegetarian and I live in Surat.",
        # a Markdown link is not a template slot
        "My favourite site is [Google](https://google.com) and my name is Naman.",
    ],
)
def test_ordinary_messages_are_still_the_person_speaking(message):
    assert pasted_instructions(message) is False
    assert own_words(message) == message


def test_a_preference_typed_beside_a_numbered_list_is_still_saved(owner):
    stored = asyncio.run(
        remember_from_message(
            owner,
            "My name is Naman. Please:\n1. answer in Hindi\n2. use metric units",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user\'s name is Naman",'
                ' "The user prefers answers in Hindi"],'
                ' "replace": [], "remove": []}'
            ),
        )
    )
    assert [f["fact"] for f in stored] == [
        "The user's name is Naman",
        "The user prefers answers in Hindi",
    ]


# --- 2. The per-fact rules: the same shape arriving any other way ----------


@pytest.mark.parametrize(
    "fact",
    [
        # a template slot the script never filled in
        "The user is a professional interviewing for a designation_of_candidate"
        " role at Company_name",
        "The user works at {{company}}",
        "The user's title is <job_title>",
        "The user lives in [CITY NAME]",
        # a wish about ONE answer, not about every answer
        "The user wants the first answer to be a professional self-introduction"
        " (~150-180 words)",
        "The user wants the next response to be a summary",
        # already refused at release 1, asserted here so it stays refused
        "The user wants all keywords to be bolded in responses",
    ],
)
def test_a_script_instruction_is_not_a_durable_fact(fact):
    assert is_durable(fact) is False


@pytest.mark.parametrize(
    "fact",
    [
        "The user prefers answers in Hindi",
        "The user wants explanations in layman terms",
        "The user always wants metric units",
        "The user's name is Naman",
        # scoped to one answer, but stated as a STANDING preference
        "The user prefers the first line of an answer to be a summary",
        # one underscore is a handle, not a template slot
        "The user's GitHub handle is naman_jain",
        "The user works at TechSara Solutions",
    ],
)
def test_a_genuine_preference_is_still_durable(fact):
    assert is_durable(fact) is True


def test_a_placeholder_fact_is_dropped_even_from_an_ordinary_message(owner):
    """The message rule takes the paste; this takes the row, wherever the
    extractor found it."""
    stored = asyncio.run(
        remember_from_message(
            owner,
            "I am interviewing for a designation_of_candidate role soon.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user is interviewing for a'
                ' designation_of_candidate role"],'
                ' "replace": [], "remove": []}'
            ),
        )
    )
    assert stored == []
    assert _texts(owner) == []
