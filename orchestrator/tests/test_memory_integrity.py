"""Memory integrity (2026-09-18): what may become a durable fact, and what
may not.

The platform sweep found four ways the fact store took in things the person
never said about themselves: a pasted CV overwrote the account's own name and
email, "forget that" stored a negated copy instead of deleting, the extractor
carried a number from one employer to another, and a third of the store was
one-off task requests replayed as a to-do list. Each test below is one of
those reproductions; the extractor's model call is injected, so no LLM runs.
"""
import asyncio

import pytest

from app import db
from app.facts import (
    is_durable,
    parse_extraction,
    remember_from_message,
    strip_ungrounded_suffixes,
    ungrounded_in,
)


@pytest.fixture()
def owner():
    uid = db.create_user("alice", "hash")
    db.create_conversation(uid, "c1", "Company talk")
    return uid


def _fake_complete(reply, calls=None):
    async def complete(messages, **kwargs):
        if calls is not None:
            calls.append(messages)
        return reply

    return complete


def _texts(uid):
    return [f["fact"] for f in db.list_user_facts(uid)]


# --- 1. Third-party content is never a fact ABOUT the person ---------------

_CV = (
    "NAVEEN R. VELUMALA\n"
    "AI/ML Engineer | San Francisco, CA | naveen.v@example.invalid\n"
    "Senior Machine Learning Engineer at Cognitiv, 2021-present.\n"
    + "Built and shipped retrieval systems, ranking models and evaluation "
    "harnesses for a production recommendation stack. " * 20
)


def test_a_pasted_document_never_becomes_a_fact_about_the_person(owner):
    calls = []
    stored = asyncio.run(
        remember_from_message(
            owner,
            "Can you review this profile?\n\n" + _CV,
            "c1",
            complete=_fake_complete(
                '{"add": ["The user\'s name is Naveen R. Velumala",'
                ' "The user\'s email is naveen.v@example.invalid",'
                ' "The user is a Senior Machine Learning Engineer at Cognitiv"],'
                ' "replace": []}',
                calls,
            ),
        )
    )
    assert stored == []
    assert _texts(owner) == []
    # and the extractor was never even asked about someone else's document
    assert calls == []


def test_an_uploaded_document_turn_stores_nothing(owner):
    calls = []
    stored = asyncio.run(
        remember_from_message(
            owner,
            "Summarise the attached contract for me.",
            "c1",
            attachments=True,
            complete=_fake_complete(
                '{"add": ["The user is the tenant of 14 Bridge Street"],'
                ' "replace": []}',
                calls,
            ),
        )
    )
    assert stored == []
    assert _texts(owner) == []
    assert calls == []


def test_a_short_self_disclosure_is_still_remembered(owner):
    stored = asyncio.run(
        remember_from_message(
            owner,
            "By the way, my name is Naman and I prefer answers in Hindi.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user\'s name is Naman",'
                ' "The user prefers answers in Hindi"], "replace": []}'
            ),
        )
    )
    assert [f["fact"] for f in stored] == [
        "The user's name is Naman",
        "The user prefers answers in Hindi",
    ]


# --- 2. "Forget that" deletes ---------------------------------------------


def test_forget_deletes_the_fact_from_the_store(owner):
    saved = db.add_user_fact(owner, "The user is vegetarian", "c1")
    stored = asyncio.run(
        remember_from_message(
            owner,
            "Please forget that I'm vegetarian, that's out of date.",
            "c1",
            complete=_fake_complete(
                '{"add": [], "replace": [], "remove": [%d]}' % saved["id"]
            ),
        )
    )
    assert _texts(owner) == []
    assert stored and stored[0]["deleted"] is True
    assert db.delete_user_fact(owner, saved["id"]) is False  # really gone


def test_forget_never_leaves_a_negated_fact_behind(owner):
    saved = db.add_user_fact(owner, "The user is vegetarian", "c1")
    asyncio.run(
        remember_from_message(
            owner,
            "Please forget that I'm vegetarian, that's out of date.",
            "c1",
            complete=_fake_complete(
                '{"add": [], "replace": [{"id": %d,'
                ' "fact": "The user is not vegetarian"}]}' % saved["id"]
            ),
        )
    )
    facts = _texts(owner)
    assert "The user is not vegetarian" not in facts
    assert facts == []  # the one fact the request names is deleted, not rewritten


def test_forget_with_nothing_matching_stores_nothing(owner):
    db.add_user_fact(owner, "The user's name is Naman", "c1")
    db.add_user_fact(owner, "The user lives in Ahmedabad", "c1")
    asyncio.run(
        remember_from_message(
            owner,
            "Forget the thing about the quarterly budget, please.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user does not want to discuss the budget"],'
                ' "replace": []}'
            ),
        )
    )
    assert sorted(_texts(owner)) == [
        "The user lives in Ahmedabad",
        "The user's name is Naman",
    ]


def test_parse_extraction_reads_the_remove_operation():
    parsed = parse_extraction('{"add": [], "replace": [], "remove": [7, "8", "x"]}')
    assert parsed == {"add": [], "replace": [], "remove": [7, 8]}


# --- 3. Only what was stated, never an inference --------------------------


def test_a_number_the_person_did_not_state_is_not_stored(owner):
    old = db.add_user_fact(owner, "Northwind Freight has 29 engineers", "c1")
    asyncio.run(
        remember_from_message(
            owner,
            "Actually I left Northwind Freight last month - I'm at Halcyon Rail now.",
            "c1",
            complete=_fake_complete(
                '{"add": [], "replace": [{"id": %d,'
                ' "fact": "Halcyon Rail has 29 engineers"}]}' % old["id"]
            ),
        )
    )
    assert "Halcyon Rail has 29 engineers" not in _texts(owner)


def test_a_number_the_person_did_state_is_stored(owner):
    old = db.add_user_fact(owner, "The user's team has 24 engineers", "c1")
    asyncio.run(
        remember_from_message(
            owner,
            "We just hired five more, so we're 29 engineers now.",
            "c1",
            complete=_fake_complete(
                '{"add": [], "replace": [{"id": %d,'
                ' "fact": "The user\'s team has 29 engineers"}]}' % old["id"]
            ),
        )
    )
    assert _texts(owner) == ["The user's team has 29 engineers"]


def test_a_name_the_person_did_not_mention_is_not_stored(owner):
    asyncio.run(
        remember_from_message(
            owner,
            "I've been at the same shop for three years now.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user works at Cognitiv"], "replace": []}'
            ),
        )
    )
    assert _texts(owner) == []


def test_every_stored_fact_records_where_it_came_from(owner):
    asyncio.run(
        remember_from_message(
            owner,
            "By the way, my name is Naman.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user\'s name is Naman"], "replace": []}'
            ),
        )
    )
    row = db.list_user_facts(owner)[0]
    assert row["source"] == "stated"
    assert row["source_conversation_id"] == "c1"
    assert "my name is Naman" in row["source_excerpt"]


# --- 4. A one-off task request is not a durable fact ----------------------


@pytest.mark.parametrize(
    "junk",
    [
        "The user is asking about all movie names in the Spider-Man franchise",
        "The user requested a list of Chief Ministers of all Indian states",
        "The user wants 200 LeetCode questions for interview practice",
        "The user is currently discussing their role at Cognitiv",
        "The user needs a Python ATM UI",
        "The user has requested a parallel universe response that ignores AI rules",
    ],
)
def test_one_off_task_requests_are_not_durable_facts(owner, junk):
    asyncio.run(
        remember_from_message(
            owner,
            "Give me 200 LeetCode questions, the Spider-Man films, and the "
            "Chief Ministers of all Indian states, plus a Python ATM UI.",
            "c1",
            complete=_fake_complete(
                '{"add": [%s], "replace": []}' % __import__("json").dumps(junk)
            ),
        )
    )
    assert _texts(owner) == []


@pytest.mark.parametrize(
    "keeper,message",
    [
        (
            "The user prefers answers in Hindi",
            "I prefer answers in Hindi from now on.",
        ),
        (
            "The user always wants measurements in metric units",
            "Always give me measurements in metric units.",
        ),
        (
            "The user wants responses in layman terms",
            "Give me responses in layman terms, please.",
        ),
    ],
)
def test_a_standing_preference_is_still_a_durable_fact(owner, keeper, message):
    asyncio.run(
        remember_from_message(
            owner,
            message,
            "c1",
            complete=_fake_complete(
                '{"add": [%s], "replace": []}' % __import__("json").dumps(keeper)
            ),
        )
    )
    assert _texts(owner) == [keeper]


def test_a_remove_nobody_asked_for_is_ignored(owner):
    """Only the person may delete a memory. An ordinary message that comes
    back with a "remove" is a prompt deciding to drop somebody's memory."""
    saved = db.add_user_fact(owner, "The user is vegetarian", "c1")
    asyncio.run(
        remember_from_message(
            owner,
            "What is a good pasta recipe for tonight?",
            "c1",
            complete=_fake_complete(
                '{"add": [], "replace": [], "remove": [%d]}' % saved["id"]
            ),
        )
    )
    assert _texts(owner) == ["The user is vegetarian"]


# --- 5. QA round, 2026-09-18 ------------------------------------------------
# Two silent, irreversible deletions and one identity overwrite reproduced by
# QA on the integrated tree. Each test below failed before the fix.


@pytest.mark.parametrize(
    "fact,message",
    [
        (
            "The user always wants unit tests with code",
            "Don't forget to add the unit tests when you write that module.",
        ),
        (
            "The user's password manager is Bitwarden",
            "I always forget my password, any tips for remembering it?",
        ),
    ],
)
def test_ordinary_english_forget_never_deletes_a_memory(owner, fact, message):
    """A reminder ("forget to …") and a confession ("I always forget …") are
    not erasure requests. The id-less fallback deletes the one fact whose
    content words a forget request matches, so a false positive here is a
    memory lost with no undo."""
    db.add_user_fact(owner, fact, "c1")
    asyncio.run(
        remember_from_message(
            owner,
            message,
            "c1",
            complete=_fake_complete('{"add": [], "replace": [], "remove": []}'),
        )
    )
    assert _texts(owner) == [fact]


def test_a_real_forget_request_still_deletes(owner):
    saved = db.add_user_fact(owner, "The user lives at 14 Bridge Street", "c1")
    asyncio.run(
        remember_from_message(
            owner,
            "I want you to forget my address.",
            "c1",
            complete=_fake_complete(
                '{"add": [], "replace": [], "remove": [%d]}' % saved["id"]
            ),
        )
    )
    assert _texts(owner) == []


_SHORT_CV = (
    "Can you review this profile?\n\n"
    "NAVEEN R. VELUMALA\n"
    "AI/ML Engineer | San Francisco, CA | naveen.v@example.invalid\n"
    "Senior Machine Learning Engineer at Cognitiv, 2021-present.\n"
    "Built retrieval systems and ranking models."
)


def test_a_short_pasted_cv_is_still_a_document(owner):
    """Under the length ceiling, so only the layout can say it is a paste:
    an ALL-CAPS name banner and a line carrying an email address."""
    assert len(_SHORT_CV) < 300
    calls = []
    stored = asyncio.run(
        remember_from_message(
            owner,
            _SHORT_CV,
            "c1",
            complete=_fake_complete(
                '{"add": ["The user\'s name is Naveen R. Velumala",'
                ' "The user\'s email is naveen.v@example.invalid"],'
                ' "replace": []}',
                calls,
            ),
        )
    )
    assert stored == []
    assert _texts(owner) == []
    assert calls == []


def test_a_typed_multi_line_self_disclosure_is_still_remembered(owner):
    stored = asyncio.run(
        remember_from_message(
            owner,
            "Two things about me:\nI am vegetarian.\nI live in Ahmedabad.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user is vegetarian",'
                ' "The user lives in Ahmedabad"], "replace": []}'
            ),
        )
    )
    assert [f["fact"] for f in stored] == [
        "The user is vegetarian",
        "The user lives in Ahmedabad",
    ]


def test_a_preference_stated_with_without_is_durable(owner):
    asyncio.run(
        remember_from_message(
            owner,
            "Please always give me answers without emoji.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user needs answers without emoji"], "replace": []}'
            ),
        )
    )
    assert _texts(owner) == ["The user needs answers without emoji"]


def test_a_deleted_fact_is_not_announced_as_memory_updated():
    """The chat's memory chip lists what was SAVED. A deleted row comes back
    from remember_from_message flagged, and must not ride out as an update
    carrying the very sentence the person asked to erase."""
    import inspect

    from app import main

    src = inspect.getsource(main)
    assert 'f["fact"] for f in saved if not f.get("deleted")' in src


# --- 6. Release-2 backlog, 2026-09-18 ---------------------------------------
# Three facts the person did state were refused by the guards above, and an
# erasure the person did ask for was a silent no-op. Each test failed at
# 4810da0.


@pytest.mark.parametrize(
    "fact,message",
    [
        ("The user wants to be called Sam", "Please call me Sam from now on."),
        ("The user wants to be addressed as Dr. Rao", "I'd like you to address me as Dr. Rao."),
        ("The user is looking after two children", "I'm looking after two children at home."),
    ],
)
def test_a_name_preference_and_a_life_situation_are_durable(owner, fact, message):
    """QA-mem-called-sam: 'wants' and 'looking' made both read as task
    requests, so how the person wants to be addressed was never saved."""
    asyncio.run(
        remember_from_message(
            owner,
            message,
            "c1",
            complete=_fake_complete(
                '{"add": [%s], "replace": []}' % __import__("json").dumps(fact)
            ),
        )
    )
    assert _texts(owner) == [fact]


def test_a_call_back_request_is_still_a_task(owner):
    asyncio.run(
        remember_from_message(
            owner,
            "Can someone call me back tomorrow about the invoice?",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user wants to be called back tomorrow"], "replace": []}'
            ),
        )
    )
    assert _texts(owner) == []


def test_a_corporate_suffix_the_person_did_not_say_is_stripped_not_fatal(owner):
    """QA-mem-techsara-solutions: the extractor completed "TechSara" to its
    registered name, "Solutions" was not in the message, and the whole fact
    was dropped."""
    asyncio.run(
        remember_from_message(
            owner,
            "I work at TechSara as a backend engineer.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user works at TechSara Solutions"], "replace": []}'
            ),
        )
    )
    assert _texts(owner) == ["The user works at TechSara"]


def test_a_suffix_chain_after_an_ungrounded_name_still_fails_closed(owner):
    asyncio.run(
        remember_from_message(
            owner,
            "I work at a logistics company as a backend engineer.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user works at Northwind Freight Pvt Ltd"], "replace": []}'
            ),
        )
    )
    assert _texts(owner) == []


def test_a_suffix_the_person_did_say_is_kept(owner):
    asyncio.run(
        remember_from_message(
            owner,
            "I work at TechSara Solutions as a backend engineer.",
            "c1",
            complete=_fake_complete(
                '{"add": ["The user works at TechSara Solutions"], "replace": []}'
            ),
        )
    )
    assert _texts(owner) == ["The user works at TechSara Solutions"]


def test_a_rewrite_with_an_ungrounded_suffix_keeps_the_grounded_name(owner):
    old = db.add_user_fact(owner, "The user works at Cognitiv", "c1")
    asyncio.run(
        remember_from_message(
            owner,
            "I moved jobs - I'm at TechSara now.",
            "c1",
            complete=_fake_complete(
                '{"add": [], "replace": [{"id": %d,'
                ' "fact": "The user works at TechSara Labs"}]}' % old["id"]
            ),
        )
    )
    assert _texts(owner) == ["The user works at TechSara"]


def _profile(uid):
    db.add_user_fact(uid, "The user's name is Naman", "c1")
    db.add_user_fact(uid, "The user lives in Ahmedabad", "c1")
    employer = db.add_user_fact(uid, "The user works at Cognitiv", "c1")
    db.add_user_fact(uid, "The user prefers answers in Hindi", "c1")
    return employer


_NO_OPS = '{"add": [], "replace": [], "remove": []}'


def test_forget_my_employer_deletes_exactly_the_employer_fact(owner):
    """QA-mem-forget-employer: the request says "employer", the fact says
    "works at", no content word is shared, and the id-less fallback found
    nothing — the person was told nothing and the fact stayed."""
    employer = _profile(owner)
    stored = asyncio.run(
        remember_from_message(
            owner, "Please forget my employer.", "c1", complete=_fake_complete(_NO_OPS)
        )
    )
    assert [f["id"] for f in stored if f.get("deleted")] == [employer["id"]]
    assert sorted(_texts(owner)) == [
        "The user lives in Ahmedabad",
        "The user prefers answers in Hindi",
        "The user's name is Naman",
    ]


def test_forget_where_i_live_deletes_the_home_fact(owner):
    _profile(owner)
    asyncio.run(
        remember_from_message(
            owner, "Forget where I live, please.", "c1", complete=_fake_complete(_NO_OPS)
        )
    )
    assert "The user lives in Ahmedabad" not in _texts(owner)
    assert len(_texts(owner)) == 3


def test_a_synonym_that_names_two_facts_deletes_neither(owner):
    _profile(owner)
    db.add_user_fact(owner, "The user worked at Northwind Freight before", "c1")
    asyncio.run(
        remember_from_message(
            owner, "Please forget my employer.", "c1", complete=_fake_complete(_NO_OPS)
        )
    )
    assert len(_texts(owner)) == 5


@pytest.mark.parametrize(
    "message",
    [
        "I always forget my password, any tips?",
        "Don't forget to add the tests.",
        "Don't forget my name is Naman when you sign the letter.",
        "Forget about work for a second, recommend a film.",
    ],
)
def test_ordinary_forget_sentences_delete_no_profile_fact(owner, message):
    _profile(owner)
    asyncio.run(
        remember_from_message(owner, message, "c1", complete=_fake_complete(_NO_OPS))
    )
    assert len(_texts(owner)) == 4


# --- 7. QA round 1 on the release-2 changes, 2026-09-18 ----------------------
# The synonym fallback above matched a BAG of words ("named", "works",
# "based", "working"), and exactly-one-match does not help when the single
# match is the wrong fact: every case in the next two tests hard-deleted an
# unrelated fact at f449785 and deleted nothing at 4810da0.


def _forget(uid, message, reply=_NO_OPS):
    return asyncio.run(
        remember_from_message(uid, message, "c1", complete=_fake_complete(reply))
    )


def _deleted(stored):
    return [f["fact"] for f in stored if f.get("deleted")]


@pytest.mark.parametrize(
    "message,facts",
    [
        ("Please forget my name.", ["The user's dog is named Rex", "The user lives in Pune"]),
        ("Please forget my name.", ["The user's manager is called Priya", "The user lives in Pune"]),
        ("Please forget my name.", ["The user's dog is named Bruno"]),
        # the content word "name" deleted this one already at 4810da0
        ("Please forget my name.", ["The user's dog's name is Rex", "The user lives in Pune"]),
        (
            "Forget where I live, please.",
            ["The user prefers answers based on primary sources", "The user works at Cognitiv"],
        ),
        ("Please forget where I live.", ["The user's startup is based in Berlin"]),
        ("Please forget my job.", ["The user is working on a novel about sailors", "The user lives in Pune"]),
        ("Please forget my employer.", ["The user's wife works at Google", "The user lives in Pune"]),
        ("Please forget my employer.", ["The user prefers to work late at night"]),
        ("Please forget my company.", ["The user uses Linux for work"]),
    ],
)
def test_the_synonym_table_never_deletes_a_fact_about_something_else(owner, message, facts):
    for fact in facts:
        db.add_user_fact(owner, fact, "c1")
    stored = _forget(owner, message)
    assert _deleted(stored) == [], f"{message!r} deleted {_deleted(stored)}"
    assert sorted(_texts(owner)) == sorted(facts)


@pytest.mark.parametrize(
    "message",
    [
        "Forget my work problems, tell me a joke.",
        "Forget my job interview nerves, let's focus on the essay.",
        "forget my job title for now, just write the cover letter",
        "Forget my work, tell me a joke.",
    ],
)
def test_setting_a_topic_aside_is_not_an_erasure_of_the_profile(owner, message):
    """"Forget my work problems" puts a topic aside; only a request that ENDS
    at the profile noun ("forget my employer.") names the saved fact."""
    db.add_user_fact(owner, "The user works at Cognitiv", "c1")
    db.add_user_fact(owner, "The user is vegetarian", "c1")
    stored = _forget(owner, message)
    assert _deleted(stored) == []
    assert len(_texts(owner)) == 2


def test_forget_it_before_an_unrelated_question_deletes_nothing(owner):
    """Older than the synonym table (present at 4810da0): the content word
    "Pune" of the QUESTION deleted the home fact. Only the clause that holds
    the forget verb names what is to be forgotten."""
    db.add_user_fact(owner, "The user lives in Pune", "c1")
    db.add_user_fact(owner, "The user works at Cognitiv", "c1")
    stored = _forget(owner, "Forget it, what's the weather in Pune?")
    assert _deleted(stored) == []
    assert len(_texts(owner)) == 2


def test_a_profile_noun_in_a_reminder_clause_is_not_what_is_erased(owner):
    """Only a profile noun inside an ERASURE clause names a fact: here the
    erasure is "Forget that", and "don't forget my name" asks to keep it."""
    db.add_user_fact(owner, "The user's name is Naman", "c1")
    db.add_user_fact(owner, "The user works at Cognitiv", "c1")
    stored = _forget(owner, "Forget that. But don't forget my name.")
    assert _deleted(stored) == []
    assert len(_texts(owner)) == 2


@pytest.mark.parametrize(
    "message,facts,expected",
    [
        ("Please forget my name.", ["The user's name is Naman", "The user's dog is named Rex"], "The user's name is Naman"),
        ("Please forget my name.", ["The user wants to be called Sam", "The user lives in Pune"], "The user wants to be called Sam"),
        ("Forget where I live.", ["The user is based in Berlin", "The user's startup is based in Berlin"], "The user is based in Berlin"),
        ("Please forget my employer now.", ["The user's employer is Cognitiv", "The user's wife works at Google"], "The user's employer is Cognitiv"),
        ("Please forget my job.", ["The user works for Northwind", "The user is working on a novel about sailors"], "The user works for Northwind"),
    ],
)
def test_the_synonym_table_deletes_the_fact_that_states_the_attribute(owner, message, facts, expected):
    for fact in facts:
        db.add_user_fact(owner, fact, "c1")
    stored = _forget(owner, message)
    assert _deleted(stored) == [expected]
    assert sorted(_texts(owner)) == sorted(f for f in facts if f != expected)


def test_dont_forget_elsewhere_does_not_cancel_a_real_erasure(owner):
    """The "don't forget" exception was applied to the WHOLE message, so a
    reminder in a second sentence masked the erasure in the first: the
    extractor's remove id was discarded, and its negated rewrite — the
    steakhouse bug of section 2 — was written instead."""
    veg = db.add_user_fact(owner, "The user is vegetarian", "c1")
    db.add_user_fact(owner, "The user works at Cognitiv", "c1")
    message = "Please forget that I'm vegetarian. Don't forget I like spicy food though."
    stored = _forget(
        owner,
        message,
        '{"add": [], "replace": [{"id": %d, "fact": "The user is not vegetarian"}],'
        ' "remove": [%d]}' % (veg["id"], veg["id"]),
    )
    assert _deleted(stored) == ["The user is vegetarian"]
    assert _texts(owner) == ["The user works at Cognitiv"]


def test_a_negated_rewrite_is_never_written_when_the_reminder_follows(owner):
    veg = db.add_user_fact(owner, "The user is vegetarian", "c1")
    db.add_user_fact(owner, "The user works at Cognitiv", "c1")
    _forget(
        owner,
        "Please forget that I'm vegetarian. Don't forget I like spicy food though.",
        '{"add": [], "replace": [{"id": %d, "fact": "The user is not vegetarian"}],'
        ' "remove": []}' % veg["id"],
    )
    assert "The user is not vegetarian" not in _texts(owner)


def test_forget_my_employer_with_a_reminder_after_it_still_erases(owner):
    """The extractor pointed at the row (it does, 3/3 live for this shape of
    request); the reminder in the next clause discarded its id."""
    employer = _profile(owner)
    stored = _forget(
        owner,
        "Please forget my employer, and don't forget to answer in English.",
        '{"add": [], "replace": [], "remove": [%d]}' % employer["id"],
    )
    assert [f["id"] for f in stored if f.get("deleted")] == [employer["id"]]


@pytest.mark.parametrize(
    "fact",
    [
        "The user wants to be called when the build finishes",
        "The user needs to be called by the recruiter this week",
        "The user is asking what the band goes by",
        "The user wants to be called back tomorrow",
        "The user wants to be called Monday morning about the contract",
        "The user needs to be called ASAP about the invoice",
        "The user wants to be called Tonight after the match",
        "The user wants the assistant to call them as soon as the report is ready",
    ],
)
def test_a_one_off_request_is_still_not_durable(fact):
    """The name-preference shapes listed what may NOT follow "to be called"
    (back, later, …) and let everything else in; they now require a name."""
    assert not is_durable(fact)


@pytest.mark.parametrize(
    "fact",
    [
        "The user wants to be called Sam",
        "The user wants to be addressed as Dr. Rao",
        "The user wants to be referred to as Ms. Iyer",
        "The user prefers to be called by their first name",
        "The user wants to be called by their nickname",
        "The user goes by Nam",
        "The user wants the assistant to address them as Captain",
        "The user is looking after two children",
    ],
)
def test_a_name_preference_is_durable(fact):
    assert is_durable(fact)


def test_a_suffix_after_an_ungrounded_name_is_left_for_the_grounding_check():
    """The suffix is cut only after a name the message contains; after an
    invented one the fact is returned whole, so `ungrounded_in` sees the
    invention with its suffix and nothing is quietly rewritten."""
    fact = "The user works at Halcyon Rail Group"
    assert strip_ungrounded_suffixes(fact, "I work at a rail company.") == fact
    assert ungrounded_in(fact, "I work at a rail company.") == "Halcyon"
    assert (
        strip_ungrounded_suffixes("The user works at TechSara Solutions", "I work at TechSara.")
        == "The user works at TechSara"
    )


# --- 8. Review round 2, 2026-09-18 -------------------------------------------
# Both reviewers hard-deleted a profile fact with messages that ask for the
# opposite, or ask nothing: the round-2 synonym table took any clause holding
# "forget" as an erasure. None of these deleted anything at 4810da0. The
# full adversarial set is tests/test_memory_erasure_requests.py; these are
# the reviewers' own reproductions, verbatim.

_ROUND2_PROFILE = (
    "The user works at Cognitiv",
    "The user lives in Pune",
    "The user's name is Naman",
    "The user is vegetarian",
)


def _round2_profile(uid):
    return {f: db.add_user_fact(uid, f, "c1")["id"] for f in _ROUND2_PROFILE}


@pytest.mark.parametrize(
    "message",
    [
        # security review F1
        "Don't ever forget where I live.",
        "Please don't ever forget my employer.",
        "Do not, ever, forget where I live.",
        "Never, ever forget my company.",
        "Don't you dare forget my job!",
        "Did you forget my employer?",
        "Did you forget where I live?",
        "Why did you forget where I work?",
        "Why do you keep forgetting? Did you forget my company?",
        # verifier, blocking failure
        "Did you forget where I work?",
        "Why would you forget my address?",
        "How do I make Chrome forget my address?",
        "How can I make LinkedIn forget my employer?",
        "How do I get Google Maps to forget my home address?",
        "Did the app forget my address?",
        "Don't you dare forget my employer!",
        # verifier note: deleted the name fact at 4810da0 AND at 2f702e1
        "Did you forget my name?",
    ],
)
def test_a_question_reminder_or_third_party_deletes_nothing(owner, message):
    _round2_profile(owner)
    assert _deleted(_forget(owner, message)) == []
    assert sorted(_texts(owner)) == sorted(_ROUND2_PROFILE)


@pytest.mark.parametrize(
    "message",
    ["Did you forget my employer?", "Don't ever forget where I live.", "Did you forget my name?"],
)
def test_the_extractors_remove_is_ignored_when_nobody_asked(owner, message):
    """Security review, pre-existing at 4810da0: a remove id the extractor
    proposes for a QUESTION about forgetting went through. Only a request
    from the person may delete."""
    ids = _round2_profile(owner)
    reply = '{"add": [], "replace": [], "remove": %s}' % sorted(ids.values())
    assert _deleted(_forget(owner, message, reply)) == []
    assert sorted(_texts(owner)) == sorted(_ROUND2_PROFILE)


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Please forget my employer.", "The user works at Cognitiv"),
        ("Can you forget my employer?", "The user works at Cognitiv"),
        ("Could you please forget where I live?", "The user lives in Pune"),
        ("Forget where I live, please.", "The user lives in Pune"),
        ("Erase my employer.", "The user works at Cognitiv"),
        ("Stop remembering where I work.", "The user works at Cognitiv"),
        ("Ok, forget my employer.", "The user works at Cognitiv"),
        # verifier note: the round-2 clause split dropped the appositive
        ("Forget my employer, Cognitiv.", "The user works at Cognitiv"),
    ],
)
def test_a_directive_erasure_deletes_exactly_the_named_fact(owner, message, expected):
    _round2_profile(owner)
    assert _deleted(_forget(owner, message)) == [expected]
    assert sorted(_texts(owner)) == sorted(f for f in _ROUND2_PROFILE if f != expected)


def test_an_appositive_the_fact_does_not_contain_deletes_nothing(owner):
    """"Forget my address, Detective." addresses someone; the name after the
    comma must be in the fact, for the fallback and for the extractor."""
    ids = _round2_profile(owner)
    reply = '{"add": [], "replace": [], "remove": %s}' % sorted(ids.values())
    assert _deleted(_forget(owner, "Forget my address, Detective.", reply)) == []
    assert _deleted(_forget(owner, "Forget my address, Detective.")) == []


def test_the_fallback_never_reaches_another_users_fact(owner):
    bob = db.create_user("bob", "hash")
    db.create_conversation(bob, "cb", "Bob")
    db.add_user_fact(bob, "The user works at Northwind", "cb")
    carol = db.create_user("carol", "hash")
    db.create_conversation(carol, "cc", "Carol")
    asyncio.run(
        remember_from_message(
            carol, "Please forget my employer.", "cc", complete=_fake_complete(_NO_OPS)
        )
    )
    assert [f["fact"] for f in db.list_user_facts(bob)] == ["The user works at Northwind"]


def test_an_extractor_remove_for_another_users_fact_id_is_ignored(owner):
    _round2_profile(owner)
    bob = db.create_user("bob", "hash")
    db.create_conversation(bob, "cb", "Bob")
    bobs = db.add_user_fact(bob, "The user works at Northwind", "cb")
    reply = '{"add": [], "replace": [], "remove": [%d]}' % bobs["id"]
    _forget(owner, "Please forget my employer.", reply)
    assert [f["fact"] for f in db.list_user_facts(bob)] == ["The user works at Northwind"]


@pytest.mark.parametrize(
    "fact",
    [
        "The user wants the new repo to be called Atlas",
        "The user wants the report to be called Q3 Summary",
        "The user needs the function to be called ParseInvoice",
        "The user is asking for the file to be called README",
        "The user wants to know if Priya goes by Pri",
    ],
)
def test_naming_a_thing_is_not_a_name_preference(fact):
    """Security review F2: the name-preference shape did not require the
    USER to be the one named, so a task request became durable."""
    assert is_durable(fact) is False


def test_the_classifier_is_linear_on_a_long_courtesy_tail():
    """A clause read lazily up to a long run of ", please" backtracked for
    160-175 ms (measured 2026-09-19) before the tail was made possessive; a
    message this long runs on the event loop."""
    import time

    from app.facts import _erasure_requests

    worst = 0.0
    for text in (
        "Forget that I x" + ", please" * 140 + " x",
        "Delete what you know about x" + ", please" * 140 + " x",
        "Forget that I" + " ," * 560 + "x",
    ):
        started = time.perf_counter()
        _erasure_requests(text)
        worst = max(worst, time.perf_counter() - started)
    assert worst < 0.05, worst
