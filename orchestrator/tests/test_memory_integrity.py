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
from app.facts import parse_extraction, remember_from_message


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
