"""The conversation's picture survives a restart (V41, 2026-09-21).

THE DEFECT (completeness critic R6). `engines/image_memory.py` kept the image
turn's bytes in the orchestrator PROCESS with a TTL, and said so itself: "a
restart loses it and the next turn behaves exactly as it did before this
module existed". Auto-deploy fires on every push to main, so every day, several
times a day, a conversation in flight quietly went back to the behaviour the
2026-09-17 audit reported — "I don't see the note you're referring to in our
chat. Could you please paste it or upload it here?" about a photo sent one turn
earlier.

HOW A RESTART IS SIMULATED HERE. By emptying the module's two in-process dicts
and nothing else: that is precisely, and only, what a new process starts with.
Deliberately NOT `image_memory.clear()`, which means "this conversation never
had a picture" and now empties both halves — the two are different events and
the tests below say which one they mean.

Every test in this file fails on the parent commit (91ac019).
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app import db
from app.engines import image_memory
from app.engines import router as router_engine
from app.engines import vision
from app.main import app

IMG = "aGVsbG8="
OTHER_IMG = "d29ybGQ="

TURN1 = "From this note: what number do I call, and how much is still owed after the deposit?"
ANSWER1 = (
    "**Number to call:** +31 6 24 88 17 05.\n\n"
    "**Still owed:** 4,820 EUR invoiced minus the 1,250 EUR deposit already paid "
    "= **3,570 EUR**."
)
#: The live check's follow-up, and the audit's shape: it names the picture, so
#: it fires on evidence (1) with no help from the image turn's wording.
FOLLOW = "What else is written in the photo?"


@pytest.fixture(autouse=True)
def _clean():
    image_memory.clear()
    yield
    image_memory.clear()


@pytest.fixture()
def engines(monkeypatch):
    """Every engine a text follow-up can reach, faked to say who it is.

    The same harness as tests/test_vision_adversarial.py: what is under test
    is which engine the turn reaches and what it is handed, never the model.
    """
    seen: dict = {"vision": [], "routes": []}

    async def fake_vision(message, images, history, emit, *, effort="think", max_tokens=None, conversation_id=None):
        seen["vision"].append({"message": message, "images": list(images)})
        text = ANSWER1 if message == TURN1 else "vision answer"
        await emit("token", {"text": text})
        await emit("meta", {"route": "vision"})
        return text

    async def fake_chat(text, history, emit, **kw):
        await emit("token", {"text": "chat answer"})
        await emit("meta", {"route": "chat"})
        return "chat answer"

    async def route_chat(message, has_image=False, history=()):
        return "vision" if has_image else "chat"

    from app.engines import chat as chat_engine

    monkeypatch.setattr(vision, "run_vision_engine", fake_vision)
    monkeypatch.setattr(chat_engine, "run_chat_engine", fake_chat)
    monkeypatch.setattr(router_engine, "route_request", route_chat)
    return seen


def _restart() -> None:
    """Everything a new orchestrator process has: nothing in memory.

    Only the two module dicts, by name, so this test reads the same on the
    commit before the fix as on the commit after it.
    """
    image_memory._remembered_images.clear()
    image_memory._latest.clear()


def _image_turn(client, conv, question=TURN1, image=IMG, **extra):
    body = {"message": question, "image": image, "effort": "think", **extra}
    if conv:
        body["conversation_id"] = conv
    return client.post("/chat", json=body)


def _text_turn(client, conv, message, history=(), **extra):
    body = {"message": message, "effort": "think", "history": list(history), **extra}
    if conv:
        body["conversation_id"] = conv
    return client.post("/chat", json=body)


def _events(resp):
    out = []
    for block in resp.text.split("\n\n"):
        if not block.strip():
            continue
        lines = block.split("\n")
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def _route(resp) -> str:
    metas = [d for e, d in _events(resp) if e == "meta"]
    return (metas[-1] if metas else {}).get("route", "")


def _answer(resp) -> str:
    return "".join(d.get("text", "") for e, d in _events(resp) if e == "token")


# ---------------------------------------------------------------------------
# 1. The defect itself
# ---------------------------------------------------------------------------


def test_the_follow_up_still_reaches_the_picture_after_a_restart(engines, as_user):
    """The whole point: a deploy in the middle of a conversation used to undo
    the fix, and the person saw the original reported bug again."""
    as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "restart-1").status_code == 200
        engines["vision"].clear()

        _restart()

        resp = _text_turn(
            client,
            "restart-1",
            FOLLOW,
            history=[
                {"role": "user", "content": TURN1},
                {"role": "assistant", "content": ANSWER1},
            ],
        )
        assert resp.status_code == 200, resp.text
        assert _route(resp) == "vision", "the restart sent the photo question to a text engine"
        assert engines["vision"][-1]["images"] == [IMG]


def test_the_picture_is_in_the_database_after_the_image_turn(engines, as_user):
    """The row is the mechanism, so it is asserted directly: one row, for
    this viewer and this conversation, holding the bytes and the turn's
    words."""
    alice = as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "restart-row").status_code == 200
    row = db.get_conversation_image(int(alice["id"]), "restart-row")
    assert row is not None
    assert row["images"] == [IMG]
    assert TURN1.lower() in row["context"]
    assert row["age_s"] < 60


# ---------------------------------------------------------------------------
# 2. Per viewer and per conversation, across the restart too
# ---------------------------------------------------------------------------


def test_another_account_never_reaches_the_picture_across_a_restart(engines, as_user):
    """The durable half is keyed (user_id, conversation_id), so a second
    account sending the SAME conversation id gets its own row or none — never
    the first account's picture. The in-process half already proved this; a
    row that was keyed on the conversation alone would have undone it."""
    alice = as_user("alice")
    bob_conv = "shared-id"
    with TestClient(app) as client:
        assert _image_turn(client, bob_conv, image=IMG).status_code == 200
        bob = as_user("bob")
        # Bob owns no conversation with that id, so /chat refuses it outright.
        assert _text_turn(client, bob_conv, FOLLOW).status_code == 404
        # Bob's own conversation, same id shape, his own picture.
        assert _image_turn(client, "bob-own", image=OTHER_IMG).status_code == 200
        engines["vision"].clear()

        _restart()

        assert _text_turn(client, bob_conv, FOLLOW).status_code == 404
        resp = _text_turn(client, "bob-own", FOLLOW)
        assert resp.status_code == 200
        assert engines["vision"][-1]["images"] == [OTHER_IMG]

    assert db.get_conversation_image(int(alice["id"]), bob_conv)["images"] == [IMG]
    assert db.get_conversation_image(int(bob["id"]), bob_conv) is None
    assert db.get_conversation_image(int(bob["id"]), "bob-own")["images"] == [OTHER_IMG]


def test_a_second_conversation_of_the_same_account_gets_nothing(engines, as_user):
    as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "conv-with").status_code == 200
        engines["vision"].clear()
        _restart()
        resp = _text_turn(client, "conv-without", FOLLOW)
        assert resp.status_code == 200
        assert _route(resp) == "chat"
        assert engines["vision"] == []


# ---------------------------------------------------------------------------
# 3. The TTL bounds the row, not only the dict
# ---------------------------------------------------------------------------


def test_a_row_past_the_ttl_is_neither_served_nor_kept(engines, as_user, monkeypatch):
    alice = as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "ttl-conv").status_code == 200
        engines["vision"].clear()
        _restart()
        # The TTL is measured from `created_at` by the database, so shrinking
        # it below the row's age is the same event as time passing.
        monkeypatch.setenv("IMAGE_MEMORY_TTL_S", "0.001")
        resp = _text_turn(client, "ttl-conv", FOLLOW)
        assert resp.status_code == 200
        assert _route(resp) == "chat"
        assert engines["vision"] == []
    assert db.get_conversation_image(int(alice["id"]), "ttl-conv") is None, (
        "an expired picture was left in the database: the TTL has to release "
        "storage, not only recall"
    )


def test_the_sweep_deletes_expired_rows(as_user, monkeypatch):
    """`prune_conversation_images` is what makes the TTL a storage bound; the
    hydrate/write paths call it on a cadence."""
    alice = as_user("alice")
    db.save_conversation_image(int(alice["id"]), "old", [IMG], "ctx")
    db.save_conversation_image(int(alice["id"]), "new", [IMG], "ctx")
    assert db.prune_conversation_images(3600.0) == 0
    assert db.prune_conversation_images(0.0) == 2
    assert db.get_conversation_image(int(alice["id"]), "old") is None


# ---------------------------------------------------------------------------
# 4. A deleted conversation forgets, durably
# ---------------------------------------------------------------------------


def test_deleting_the_conversation_forgets_the_picture_across_a_restart(engines, as_user):
    """`history.delete_conversation` already called `image_memory.forget`. If
    forget had stayed process-only, a restart would have hydrated the deleted
    conversation's photo straight back."""
    alice = as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "del-conv").status_code == 200
        assert client.delete("/history/conversations/del-conv").status_code == 200
        assert db.get_conversation_image(int(alice["id"]), "del-conv") is None

        engines["vision"].clear()
        _restart()
        # /chat recreates a conversation for an id it does not know, so the
        # turn succeeds — as a NEW conversation, with no picture in it.
        resp = _text_turn(client, "del-conv", FOLLOW)
        assert resp.status_code == 200
        assert _route(resp) == "chat"
    assert engines["vision"] == []


def test_deleting_the_account_takes_its_pictures(as_user):
    """The row hangs off users(id) ON DELETE CASCADE, which is what stops a
    departed colleague's photo outliving their account."""
    alice = as_user("alice")
    db.save_conversation_image(int(alice["id"]), "acct-conv", [IMG], "ctx")
    with db.connection() as con:
        con.execute("DELETE FROM users WHERE id = %s", (int(alice["id"]),))
    assert db.get_conversation_image(int(alice["id"]), "acct-conv") is None


# ---------------------------------------------------------------------------
# 5. When the bytes are gone, say so (the critic's option (b), as residual)
# ---------------------------------------------------------------------------


def test_the_durable_copy_respects_the_durable_byte_budget(as_user, monkeypatch):
    """A picture above `IMAGE_MEMORY_DB_CHARS` is stored as the 1600 px copy
    the module already makes for the in-process budget, not dropped and not
    written whole. The process keeps the original; the row is the smaller
    one, which is what a follow-up after a restart reads."""
    import base64
    import io

    import numpy as np
    from PIL import Image

    rng = np.random.default_rng(1)
    buf = io.BytesIO()
    Image.fromarray(rng.integers(0, 255, (1400, 1400, 3), dtype=np.uint8)).save(buf, format="PNG")
    big = base64.b64encode(buf.getvalue()).decode()

    alice = as_user("alice")
    budget = len(big) // 3
    monkeypatch.setenv("IMAGE_MEMORY_DB_CHARS", str(budget))
    image_memory.remember("budget-conv", [big], question=TURN1, answer=ANSWER1, user_id=int(alice["id"]))

    assert image_memory.recall("budget-conv", int(alice["id"])) == [big]
    stored = db.get_conversation_image(int(alice["id"]), "budget-conv")["images"]
    assert len(stored) == 1 and stored[0] != big
    assert len(stored[0]) <= budget
    raw = base64.b64decode(stored[0].split(",", 1)[-1])
    with Image.open(io.BytesIO(raw)) as im:
        assert max(im.size) <= 1600


def test_a_picture_over_the_durable_budget_still_writes_its_row(as_user, monkeypatch):
    """The row is the record that there WAS a picture. Without it a fresh
    process cannot tell "no picture was ever sent" from "the picture is
    gone", and the honest answer below would be unimplementable."""
    alice = as_user("alice")
    monkeypatch.setenv("IMAGE_MEMORY_DB_CHARS", "8")
    # Not a picture PIL can open, so no smaller copy can be made either.
    image_memory.remember("big-conv", ["x" * 5000], question=TURN1, answer=ANSWER1, user_id=int(alice["id"]))
    row = db.get_conversation_image(int(alice["id"]), "big-conv")
    assert row is not None and row["images"] == []
    assert TURN1.lower() in row["context"]


def test_the_follow_up_says_the_picture_is_gone_instead_of_answering_without_it(
    engines, as_user, monkeypatch
):
    alice = as_user("alice")
    monkeypatch.setenv("IMAGE_MEMORY_DB_CHARS", "8")
    with TestClient(app) as client:
        assert _image_turn(client, "gone-conv", image="x" * 5000).status_code == 200
        engines["vision"].clear()
        _restart()
        resp = _text_turn(client, "gone-conv", FOLLOW)
        assert resp.status_code == 200, resp.text
        assert _answer(resp) == image_memory.UNAVAILABLE_NOTICE
        assert engines["vision"] == [], "a question about a photo reached the model without it"
    assert "no longer attached" in image_memory.UNAVAILABLE_NOTICE


def test_a_turn_that_is_not_about_the_picture_is_unaffected_by_a_missing_one(
    engines, as_user, monkeypatch
):
    """The honest notice replaces an ANSWER, never a route: a turn the word
    test does not fire on still goes where it always went."""
    as_user("alice")
    monkeypatch.setenv("IMAGE_MEMORY_DB_CHARS", "8")
    with TestClient(app) as client:
        assert _image_turn(client, "gone-conv-2", image="x" * 5000).status_code == 200
        _restart()
        resp = _text_turn(client, "gone-conv-2", "What's the capital of Australia?")
        assert resp.status_code == 200
        assert _route(resp) == "chat"
        assert image_memory.UNAVAILABLE_NOTICE not in _answer(resp)


# ---------------------------------------------------------------------------
# 6. What the restart must NOT bring back
# ---------------------------------------------------------------------------


def test_the_turns_since_counter_survives_the_restart(engines, as_user):
    """"That chart" means the picture only while the picture is the last
    thing the chat was shown. If the counter reset to zero on hydration, a
    restart would re-arm a demonstrative ten turns after the photo — the
    wrong fire the 2026-09-18 adversarial round was spent on."""
    alice = as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "count-conv").status_code == 200
        # One ordinary turn passes: the picture is no longer what was last shown.
        assert _text_turn(client, "count-conv", "What's the capital of Australia?").status_code == 200
        assert db.get_conversation_image(int(alice["id"]), "count-conv")["turns_after"] >= 1

        engines["vision"].clear()
        _restart()

        resp = _text_turn(client, "count-conv", "Which region is smallest in that chart?")
        assert resp.status_code == 200
        assert engines["vision"] == [], "a distal demonstrative re-fired after the restart"
        # ...and naming the picture still works, restart or no restart.
        assert _text_turn(client, "count-conv", FOLLOW).status_code == 200
        assert engines["vision"][-1]["images"] == [IMG]


def test_an_image_turn_replaces_the_row_it_does_not_add_to_it(as_user):
    """One conversation, one picture — the budget of this feature is the
    budget of one turn, durably as well as in memory."""
    alice = as_user("alice")
    uid = int(alice["id"])
    image_memory.remember("replace", [IMG], question="q1", answer="a1", user_id=uid)
    image_memory.remember("replace", [OTHER_IMG], question="q2", answer="a2", user_id=uid)
    row = db.get_conversation_image(uid, "replace")
    assert row["images"] == [OTHER_IMG]
    assert "q1" not in row["context"]


def _drain_durable_writes() -> None:
    """Wait for every queued durable write. Correct only because the module
    writes on ONE thread: a no-op submitted behind them cannot run until they
    have."""
    image_memory._writer_pool().submit(lambda: None).result(timeout=30)


def test_durable_writes_land_in_the_order_they_were_made(as_user):
    """Two image turns in the same conversation, from the event loop where
    the writes do not block the turn: the row must end up holding the SECOND
    picture. On a shared thread pool the order is whatever the scheduler
    chose, and the conversation could be left showing the older photo."""
    alice = as_user("alice")
    uid = int(alice["id"])

    async def two_turns() -> None:
        image_memory.remember("order-conv", [IMG], question="q1", answer="a1", user_id=uid)
        image_memory.remember("order-conv", [OTHER_IMG], question="q2", answer="a2", user_id=uid)

    asyncio.run(two_turns())
    _drain_durable_writes()
    assert db.get_conversation_image(uid, "order-conv")["images"] == [OTHER_IMG]


def test_a_delete_is_never_overtaken_by_a_write_that_was_already_queued(as_user):
    """The one that matters for privacy: a picture remembered microseconds
    before the conversation was deleted must not be written back AFTER the
    delete, or the next process hydrates a deleted conversation's photo."""
    alice = as_user("alice")
    uid = int(alice["id"])

    async def remember_then_delete() -> None:
        image_memory.remember("race-conv", [IMG], question=TURN1, answer=ANSWER1, user_id=uid)
        image_memory.forget("race-conv", uid)

    asyncio.run(remember_then_delete())
    _drain_durable_writes()
    assert db.get_conversation_image(uid, "race-conv") is None
    assert image_memory.recall("race-conv", uid) == []


def test_replacing_a_picture_drops_the_old_row_at_once(as_user, monkeypatch):
    """An image over the in-process budget is downscaled in a worker thread
    (143-238 ms) and the previous picture is dropped immediately. The ROW has
    to go at the same moment: otherwise, for the length of that downscale,
    this process holds no picture and the database holds the OLD one, and a
    restart inside the window hydrates the photo the person just replaced."""
    alice = as_user("alice")
    uid = int(alice["id"])
    image_memory.remember("swap-conv", [IMG], question="q1", answer="a1", user_id=uid)
    assert db.get_conversation_image(uid, "swap-conv")["images"] == [IMG]

    monkeypatch.setenv("IMAGE_MEMORY_MAX_CHARS", "4")
    seen: dict = {}

    async def replace() -> None:
        image_memory.remember("swap-conv", ["x" * 5000], question="q2", answer="a2", user_id=uid)
        seen["in_process"] = image_memory.recall("swap-conv", uid)
        _drain_durable_writes()
        seen["row"] = db.get_conversation_image(uid, "swap-conv")

    asyncio.run(replace())
    assert seen["in_process"] == []
    assert seen["row"] is None


def test_a_write_that_forget_cancelled_writes_nothing_even_if_it_runs(as_user):
    """The cancellation itself, without the queue: `forget` drops the token,
    so a persist that had already been handed to the writer is a no-op."""
    alice = as_user("alice")
    uid = int(alice["id"])
    key = image_memory.scope("cancel-conv", uid)
    token = object()
    image_memory._durable_latest[key] = token
    image_memory.forget("cancel-conv", uid)
    image_memory._persist_now((uid, "cancel-conv"), [IMG], "ctx", key, token)
    assert db.get_conversation_image(uid, "cancel-conv") is None


def test_a_call_with_no_viewer_writes_no_row():
    """`scope` already refused to store without a viewer; the row must refuse
    for the same reason, or the durable half would reintroduce the shared
    entry that repair round 2 removed."""
    image_memory.remember("no-viewer", [IMG], question="q", answer="a", user_id=None)
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM conversation_images").fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# 7. Nothing above the answer chain may claim the turn first
# ---------------------------------------------------------------------------


@pytest.fixture()
def salesforce_planner(monkeypatch):
    """Intelligence Mode on, with a planner that claims everything it is
    asked. Measured live at Fast on 2026-09-21: the real planner claimed the
    photo follow-up about one turn in five and answered "I can't answer that
    from Salesforce. The user wants to know what text is written in a photo",
    so the test makes that certain instead of occasional."""
    from app.config import settings
    from app.engines import sf_intel

    calls: list = []

    async def claiming_run(text, history, emit, **kw):
        calls.append(text)
        await emit("token", {"text": "I can't answer that from Salesforce."})
        await emit("meta", {"route": "chat", "salesforce_mode": "intelligence"})
        return sf_intel.Outcome(handled=True, answer="I can't answer that from Salesforce.")

    monkeypatch.setattr(settings, "salesforce_intelligence_enabled", True)
    monkeypatch.setattr(settings, "sf_live_enabled", False)
    monkeypatch.setattr(sf_intel, "run", claiming_run)
    return calls


def test_the_salesforce_planner_does_not_claim_a_question_about_the_picture(
    engines, as_user, salesforce_planner
):
    """`sf_outcome.handled` returns the answer before the image branch is
    ever reached, and the gate only knew about an ATTACHED image. A follow-up
    about a picture already in the conversation is a vision turn with no
    bytes on the wire — which the gate's own comment says does not belong to
    it."""
    as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "sf-conv").status_code == 200
        engines["vision"].clear()
        _restart()

        resp = _text_turn(client, "sf-conv", FOLLOW)
        assert resp.status_code == 200, resp.text
        assert salesforce_planner == [], "the planner claimed a question about a photo"
        assert _route(resp) == "vision"
        assert engines["vision"][-1]["images"] == [IMG]


def test_the_planner_does_not_claim_it_with_the_picture_still_in_memory(
    engines, as_user, salesforce_planner
):
    """The same gap with no restart involved: it is the gate that was wrong,
    not the storage, so it is asserted without V41 in the picture."""
    as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "sf-conv-live").status_code == 200
        engines["vision"].clear()
        resp = _text_turn(client, "sf-conv-live", FOLLOW)
        assert resp.status_code == 200, resp.text
        assert salesforce_planner == [], "the planner claimed a question about a photo"
        assert engines["vision"][-1]["images"] == [IMG]


def test_an_ordinary_question_still_reaches_the_salesforce_planner(
    engines, as_user, salesforce_planner
):
    """The non-vacuous half: the exclusion above is one condition, not a
    switch that turns Intelligence Mode off."""
    as_user("alice")
    with TestClient(app) as client:
        assert _image_turn(client, "sf-conv-2").status_code == 200
        _restart()
        resp = _text_turn(client, "sf-conv-2", "How many open opportunities are there this quarter?")
        assert resp.status_code == 200
        assert salesforce_planner, "Intelligence Mode stopped seeing ordinary questions"


# ---------------------------------------------------------------------------
# 8. The durable half is optional, and the schema is idempotent
# ---------------------------------------------------------------------------


def test_the_module_still_works_with_the_durable_half_switched_off(monkeypatch):
    """A deployment with no database (scripts, the bare engine harness) gets
    what this module was before V41, and nothing raises."""
    monkeypatch.setenv("IMAGE_MEMORY_DURABLE", "false")
    image_memory.remember("off-conv", [IMG], question=TURN1, answer=ANSWER1, user_id=7)
    assert image_memory.images_for_followup("off-conv", FOLLOW, 7) == [IMG]
    with db.connection() as con:
        assert con.execute("SELECT count(*) AS n FROM conversation_images").fetchone()["n"] == 0
    asyncio.run(image_memory.hydrate("off-conv", 7))


def test_a_database_failure_costs_the_memory_and_never_the_turn(engines, as_user, monkeypatch):
    """Every durable call fails soft: the improvement is lost, the turn is
    not."""
    as_user("alice")

    def boom(*_a, **_kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(db, "save_conversation_image", boom)
    monkeypatch.setattr(db, "get_conversation_image", boom)
    with TestClient(app) as client:
        assert _image_turn(client, "boom-conv").status_code == 200
        engines["vision"].clear()
        # Still in this process, so the follow-up works exactly as before V41.
        assert _text_turn(client, "boom-conv", FOLLOW).status_code == 200
        assert engines["vision"][-1]["images"] == [IMG]
        _restart()
        resp = _text_turn(client, "boom-conv", FOLLOW)
        assert resp.status_code == 200
        assert _route(resp) == "chat"


def test_the_v41_migration_is_idempotent():
    """Applying it twice must be a no-op, the way every other migration in
    the V-series is written."""
    with db.connection() as con:
        con.execute(db._MIGRATION_V41)
        con.execute(db._MIGRATION_V41)
    assert db.LATEST_SCHEMA_VERSION >= 41
    with db.read_connection() as con:
        applied = con.execute(
            "SELECT count(*) AS n FROM schema_migrations WHERE version = 41"
        ).fetchone()["n"]
    assert applied == 1


def test_the_table_is_cleared_when_a_conversation_is_deleted_by_any_path(as_user):
    """`_SIDE_TABLES` is the safety net under `image_memory.forget`."""
    alice = as_user("alice")
    assert "conversation_images" in db._SIDE_TABLES
    db.create_conversation(int(alice["id"]), "side-conv", "t")
    db.save_conversation_image(int(alice["id"]), "side-conv", [IMG], "ctx")
    assert db.delete_conversation(int(alice["id"]), "side-conv") is True
    assert db.get_conversation_image(int(alice["id"]), "side-conv") is None
