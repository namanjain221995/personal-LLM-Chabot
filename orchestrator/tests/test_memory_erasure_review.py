"""Reviewer round 3 (memory-recall-and-facts): set-asides that must delete nothing.

Written after reading the fc8e41e gate. Each message puts a profile item aside
for a task, a document or a moment; none asks for memory to be deleted."""
from __future__ import annotations

import asyncio
import json

import pytest

from app import db
from app.facts import remember_from_message

FACTS = (
    "The user works for Halden Analytics",
    "The user lives in Ahmedabad",
    "The user's name is Arjun",
    "The user is vegetarian",
    "The user's dog is named Rex",
)

#: A second sentence opening "I moved/left/quit…" that is about a task, or a
#: reminder that sets the item aside. At fc8e41e the fallback deletes the
#: profile fact for every one of these with an EMPTY extractor reply.
FALLBACK_SET_ASIDES = (
    "Forget my employer. I left that field blank on purpose.",
    "Forget my name. I left it off the slides.",
    "Forget where I live. I moved the party to the office.",
    "Forget my address. I left it in the form already.",
    "Forget my home address. I just moved the meeting online.",
    "Forget my employer. I moved that paragraph to the end.",
    "Forget my address. I moved on to the next section.",
    "Forget my city. I moved the trip to Goa instead.",
    "Forget my name. I left the signature empty.",
    "Forget my company. I switched jobs in the story to make it fiction.",
    "Forget my address. I relocated the event to the community hall.",
    "Forget where I work. I left out the company on purpose in this bio.",
    "Forget my address. Because I moved the delivery to my office.",
    "Forget my address. Remember I told you I'd send it later?",
    "Forget my name. Remember I use a pen name for this blog.",
    "Forget my address. Don't forget I'm using the office address for this parcel.",
)

#: A bare "forget my <thing>" that is a moment or a chat message, not a memory.
#: At fc8e41e these are requests, so a model that proposes removing every row
#: deletes every row.
EXTRACTOR_SET_ASIDES = (
    "Forget my job tonight.",
    "Forget my job today.",
    "Forget my diet today.",
    "Forget my address tomorrow.",
    "Forget my diet tonight.",
    "Forget my job right now.",
    "Forget my previous question.",
    "Forget my last message.",
    "Forget my typo.",
    "Forget my mistake.",
    "Forget my work problems.",
    "Forget my earlier request.",
    "Forget my job interview nerves.",
    "Forget my last prompt.",
)

#: …and real requests the fix must keep honouring.
STILL_ERASES = (
    ("Forget my address. I moved to Mumbai.", "The user lives in Ahmedabad"),
    ("Forget my employer. I've left Halden.", "The user works for Halden Analytics"),
    ("Forget my employer. I left the company.", "The user works for Halden Analytics"),
    ("Forget my address. I moved out.", "The user lives in Ahmedabad"),
    ("Forget where I live, I moved.", "The user lives in Ahmedabad"),
)


def _complete(reply):
    async def complete(messages, **kwargs):
        return reply
    return complete


@pytest.fixture()
def owner():
    uid = db.create_user("rv3-review", "hash")
    db.create_conversation(uid, "c1", "Chat")
    return uid


def _deleted(uid, message, remove_all):
    for f in db.list_user_facts(uid):
        db.delete_user_fact(uid, f["id"])
    for fact in FACTS:
        db.add_user_fact(uid, fact, "c1")
    ids = {f["fact"]: f["id"] for f in db.list_user_facts(uid)}
    reply = json.dumps({"add": [], "replace": [], "remove": sorted(ids.values()) if remove_all else []})
    asyncio.run(remember_from_message(uid, message, "c1", complete=_complete(reply)))
    return sorted(set(ids) - {f["fact"] for f in db.list_user_facts(uid)})


@pytest.mark.parametrize("message", FALLBACK_SET_ASIDES)
def test_a_set_aside_with_a_task_sentence_deletes_nothing(owner, message):
    assert _deleted(owner, message, False) == []
    assert _deleted(owner, message, True) == []


@pytest.mark.parametrize("message", EXTRACTOR_SET_ASIDES)
def test_a_bare_set_aside_gives_the_extractor_nothing(owner, message):
    assert _deleted(owner, message, False) == []
    assert _deleted(owner, message, True) == []


@pytest.mark.parametrize("message,fact", STILL_ERASES)
def test_a_request_with_a_reason_still_erases(owner, message, fact):
    assert _deleted(owner, message, False) == [fact]
