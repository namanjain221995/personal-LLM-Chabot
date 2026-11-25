"""No invented personal details: the code mask and the saved-facts rule
(app/core/personal_details.py, design_v2 G8 and owner decision 2026-11-25)."""
from __future__ import annotations

import asyncio
import random

import pytest

from app.core import personal_details as P

INVENTED = "98450 12345"


def test_an_invented_mobile_and_email_are_masked():
    g = P.Guard("system prompt\nuser: give me Ravi's profile")
    out = g.mask(f"Ravi Kumar. Phone: {INVENTED}. Email: ravi.kumar@acmecorp.in.")
    assert INVENTED not in out and "ravi.kumar@acmecorp.in" not in out
    assert out == f"Ravi Kumar. Phone: {P.PHONE_MASK}. Email: {P.EMAIL_MASK}."
    assert g.count == 2


@pytest.mark.parametrize(
    "allowed",
    [
        f"user: my number is {INVENTED}",  # the question
        "Saved memory:\n- The user's phone number is +91-98450-12345",  # a fact, other spacing
        "Context from the user's earlier conversations: call 9845012345",  # recall
        "Current date: today\n[1] Contact page: +91 98450 12345",  # grounding
    ],
)
def test_a_mobile_the_prompt_held_is_kept_whatever_its_spacing(allowed):
    g = P.Guard(allowed)
    for shown in (INVENTED, "+91 98450 12345", "9845012345", "+91-9845012345"):
        assert g.mask(f"Your number is {shown}.") == f"Your number is {shown}."
    assert g.count == 0


def test_an_email_the_prompt_held_is_kept_in_any_case():
    g = P.Guard("user: write to Asha.Rao@Globex.com please")
    assert g.mask("Sent to asha.rao@globex.com") == "Sent to asha.rao@globex.com"


@pytest.mark.parametrize(
    "text",
    [
        "Write to name@example.com or ops@example.org",  # example domains
        "Reach us at someone@yourcompany.com or x@domain.com or me@test.test",  # placeholders (C15)
        "Apply at careers@acmecorp.in, hr@acmecorp.in or no-reply@acmecorp.in",  # role mailboxes (C15)
        "Call 9999999999, 1234567890 or 9876543210",  # dummy numbers
    ],
)
def test_example_placeholder_role_and_dummy_values_are_kept(text):
    g = P.Guard("")
    assert g.mask(text) == text and g.count == 0


@pytest.mark.parametrize(
    "text",
    [
        "Released on 2024-01-15, supported 2019-2024.",
        "The budget is Rs 1,50,000 or $25,000.00, 1500000 in all.",
        "Bengaluru 560001 and Pune 411001.",
        "Version 10.12.3 and port 8080.",
    ],
)
def test_dates_ranges_money_and_pins_are_untouched(text):
    g = P.Guard("")
    assert g.mask(text) == text and g.count == 0


def test_international_numbers_and_landlines_are_candidates():
    g = P.Guard("")
    assert g.mask("Call +1 415 555 0132 or 080-23456789.") == f"Call {P.PHONE_MASK} or {P.PHONE_MASK}."


PIECES = [
    "call ", INVENTED, "+91 98450 12345", "9845012345", "a@b.co", "ravi.k@acme.in", " ", "\n", ".", "-",
    "(", ")", "+", "x", "abc", "2024-01-15", "2019-2024", "560001", "+1 415 555 0132", "080-23456789",
    "hr@acme.com", "name@example.com", "@", "7", "0", "98450", " 12345", "a1@", "(98450)", "@acme", ".in",
]


def test_streaming_equals_masking_the_whole_text_for_any_split():
    """The property the stream rests on: whatever the split points, the joined
    `feed` output plus `finish()` is `mask(full)`."""
    for trial in range(1500):
        rnd = random.Random(trial)
        text = "".join(rnd.choice(PIECES) for _ in range(rnd.randint(1, 30)))
        allowed = "user said +91 98450 12345 and ravi.k@acme.in" if trial % 3 == 0 else ""
        whole = P.Guard(allowed)
        expected = whole.mask(text)
        streamed = P.Guard(allowed)
        out = []
        i = 0
        while i < len(text):
            n = rnd.randint(1, 7)
            out.append(streamed.feed(text[i : i + n]))
            i += n
        out.append(streamed.finish())
        assert "".join(out) == expected, (trial, text)
        assert streamed.count == whole.count


def test_ordinary_prose_streams_with_at_most_its_last_word_held():
    g = P.Guard("")
    assert g.feed("Hi ") == "Hi "
    assert g.feed("there") == ""
    assert g.feed("!") == "there!"
    assert g.finish() == ""


def test_the_count_is_correct():
    g = P.Guard("")
    for piece in ("a 98450 1", "2345, b 98450 54321 and x@acmecorp.in"):
        g.feed(piece)
    g.finish()
    assert g.count == 3


def test_a_reasoning_delta_with_an_invented_mobile_is_masked(monkeypatch):
    """C9: the reasoning stream has its own guard in the chat engine."""
    from app import llm
    from app.engines import chat

    async def fake_stream(messages, **_kwargs):
        yield "reasoning", f"I recall his number is {INVENTED[:5]}"
        yield "reasoning", f"{INVENTED[5:]} maybe."
        yield "token", f"His number is {INVENTED}."

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    events = []

    async def emit(event, data):
        events.append((event, data))

    answer = asyncio.run(chat.run_chat_engine("what is Ravi's number?", [], emit, mode="assistant", effort="fast"))
    reasoning = "".join(d["text"] for e, d in events if e == "reasoning")
    tokens = "".join(d["text"] for e, d in events if e == "token")
    assert INVENTED not in reasoning and P.PHONE_MASK in reasoning
    assert INVENTED not in tokens and answer == tokens
    meta = events[-1][1]
    assert events[-1][0] == "meta" and meta["personal_details_masked"] == 2


def test_salesforce_mode_and_the_flag_off_are_not_masked(monkeypatch):
    from app import llm
    from app.config import settings
    from app.engines import chat

    async def fake_stream(messages, **_kwargs):
        yield "token", f"Number: {INVENTED}"

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    async def emit(event, data):
        pass

    assert asyncio.run(chat.run_chat_engine("q", [], emit, mode="salesforce", effort="fast")) == f"Number: {INVENTED}"
    monkeypatch.setattr(settings, "toggle_off_mask_personal_details", False)
    assert asyncio.run(chat.run_chat_engine("q", [], emit, mode="assistant", effort="fast")) == f"Number: {INVENTED}"


def test_a_number_in_the_question_survives_the_engine(monkeypatch):
    from app import llm
    from app.engines import chat

    async def fake_stream(messages, **_kwargs):
        yield "token", f"Saved {INVENTED} for you."

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)

    async def emit(event, data):
        pass

    answer = asyncio.run(chat.run_chat_engine(f"remember {INVENTED}", [], emit, mode="assistant", effort="fast"))
    assert answer == f"Saved {INVENTED} for you."


def test_the_rule_is_in_every_assistant_mode_answer_prompt():
    from app.engines import agent, chat

    assert P.PERSONAL_DETAILS_RULE in chat.ASSISTANT_SYSTEM
    assert P.PERSONAL_DETAILS_RULE not in chat.SALESFORCE_ASSISTANT_SYSTEM
    assert P.PERSONAL_DETAILS_RULE in agent._synthesis_messages("q", [], salesforce=False)[0]["content"]
    assert P.PERSONAL_DETAILS_RULE not in agent._synthesis_messages("q", [], salesforce=True)[0]["content"]
    assert agent._personal_details_rule(False).strip() == P.PERSONAL_DETAILS_RULE
    assert agent._personal_details_rule(True) == ""


# ---------------------------------------------------------------------------
# Saved facts: other people's contact details (owner decision 2026-11-25)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fact,third_party",
    [
        (f"The user's phone number is {INVENTED}", False),
        ("The user's email is dev.user@initech.in", False),
        ("The user lives at 12 MG Road, Bengaluru", False),
        (f"The user is a recruiter; the user's phone is {INVENTED}", False),
        (f"The user's name is Priya; her phone is {INVENTED}", False),
        (f"The user's manager Ravi's phone is {INVENTED}", True),
        ("Ravi Kumar's email is ravi@acmecorp.in", True),
        ("The user's wife lives at 12 MG Road", True),
        ("Contact for Globex: 9845012345", True),
        ("Dana Moss is the CEO of Initech", False),  # no contact detail at all
        ("Ravi's careers mailbox is careers@acmecorp.in", False),  # a role mailbox is no person's
    ],
)
def test_third_party_contact_details_are_told_from_the_users_own(fact, third_party):
    assert P.third_party_contact(fact) is third_party
    masked = P.mask_third_party_contacts(fact)
    assert (masked != fact) is third_party
    if third_party:
        assert P.CONTACT_MASK in masked


def test_a_fact_with_both_keeps_the_users_detail_and_masks_the_other():
    fact = "The user's email is p@acmecorp.in and the manager's email is m@acmecorp.in"
    assert P.mask_third_party_contacts(fact) == f"The user's email is p@acmecorp.in and the manager's email is {P.CONTACT_MASK}"


def _complete(ops):
    async def complete(messages, **_kwargs):
        complete.prompt = messages[1]["content"]
        import json

        return json.dumps(ops)

    return complete


def test_the_extractor_never_saves_a_third_party_contact_detail():
    from app import db, facts

    uid = db.create_user("facts-owner", "hash")
    message = f"My manager Ravi's phone is {INVENTED} and my own number is 98450 99999."
    saved = asyncio.run(
        facts.remember_from_message(
            uid,
            message,
            None,
            complete=_complete({"add": [f"The user's manager Ravi's phone is {INVENTED}",
                                        "The user's phone number is 98450 99999"], "replace": [], "remove": []}),
        )
    )
    stored = [f["fact"] for f in db.list_user_facts(uid)]
    assert stored == ["The user's phone number is 98450 99999"]
    assert [f["fact"] for f in saved] == stored


def test_a_rewrite_that_adds_a_third_party_contact_is_dropped():
    from app import db, facts

    uid = db.create_user("facts-rewrite", "hash")
    row = db.add_user_fact(uid, "Ravi is the user's manager", source="stated", source_excerpt="Ravi is my manager")
    message = f"Ravi is my manager and his number is {INVENTED}"
    asyncio.run(
        facts.remember_from_message(
            uid,
            message,
            None,
            complete=_complete({"add": [], "replace": [{"id": row["id"], "fact": f"Ravi, the user's manager, has phone {INVENTED}"}], "remove": []}),
        )
    )
    assert [f["fact"] for f in db.list_user_facts(uid)] == ["Ravi is the user's manager"]


def test_a_saved_third_party_contact_is_masked_before_any_prompt():
    from app import db, facts

    uid = db.create_user("facts-reader", "hash")
    db.add_user_fact(uid, f"Ravi Kumar's phone is {INVENTED}", source="stated", source_excerpt="x")
    db.add_user_fact(uid, "The user's phone number is 98450 99999", source="stated", source_excerpt="y")
    rows = db.list_user_facts(uid)
    block = facts.facts_block(rows)
    assert INVENTED not in block and P.CONTACT_MASK in block
    assert "98450 99999" in block
    assert any(INVENTED in f["fact"] for f in db.list_user_facts(uid)), "the stored row is unchanged"
    extractor = _complete({"add": [], "replace": [], "remove": []})
    asyncio.run(facts.remember_from_message(uid, "I prefer short answers in English", None, complete=extractor))
    assert INVENTED not in extractor.prompt and P.CONTACT_MASK in extractor.prompt


def test_with_the_flag_off_facts_are_read_as_saved(monkeypatch):
    from app import facts
    from app.config import settings

    monkeypatch.setattr(settings, "facts_block_third_party_contacts", False)
    block = facts.facts_block([{"id": 1, "fact": f"Ravi Kumar's phone is {INVENTED}"}])
    assert INVENTED in block


def test_the_answer_mask_switch_leaves_owner_decision_1_on(monkeypatch):
    """Review R18: TOGGLE_OFF_MASK_PERSONAL_DETAILS no longer turns the
    third-party facts rule off with it."""
    from app import facts
    from app.config import settings

    monkeypatch.setattr(settings, "toggle_off_mask_personal_details", False)
    block = facts.facts_block([{"id": 1, "fact": f"Ravi Kumar's phone is {INVENTED}"}])
    assert INVENTED not in block and P.CONTACT_MASK in block


# ---------------------------------------------------------------------------
# Review fixes 2026-11-25 (review_mask.md, review_regression.md R3/R9/R13,
# review_data.md F1/F4). Synthetic values only.
# ---------------------------------------------------------------------------

#: Phone shapes the answer mask must see when the prompt does not hold them
#: (F04, F16). Dotted 5.5 ("98450.12345") and paired 2-2-2-2-2 are left to
#: the facts side: in an answer they collide with decimals and number lists.
SHOULD_MASK_PHONE = [
    "+91 98450 12345", "+91-98450-12345", "+919845012345", "09845012345", "098450 12345",
    "+91 (984) 501-2345", "984-501-2345", "9845 012 345", "+91.98450.12345", "+91 98450-12345",
    "+1 (415) 555-0132", "+1-415-555-0132", "+14155550132", "+44 20 7946 0958", "+44 (0)20 7946 0958",
    "080-23456789", "tel:+919845012345", "(9845012345)",
    "(415) 555-0132", "415-555-0132", "415.555.0132", "415 555 0132", "212-555-0132",
    "+91–98450–12345", "+91 98450 12345", "98450 12345",
    "020 7946 0958", "080 2345 6789", "(080) 23456789", "022-2345-6789",
    "(+91) 98450 12345", "0091 98450 12345", "1-800-555-0199",
]


@pytest.mark.parametrize("phone", SHOULD_MASK_PHONE)
def test_review_phone_formats_are_masked_whole(phone):
    import re

    out = P.Guard("").mask(f"Ravi's number is {phone} today")
    assert P.PHONE_MASK in out and not re.search(r"\d", out), out


#: Not personal details (F05, F06, R3): numbers and e-mail-shaped code.
NOT_PERSONAL = [
    "phi = 1.6180339887", "e = 2.7182818284", "ln 2 = 0.6931471805", "sqrt(3) = 1.7320508075",
    "8 GiB is 8589934592 bytes", "6442450944 bytes", "world population 8000000000",
    "CTC range 60000-80000 per month", "expected salary 70000 95000", "port range 60000-61000",
    "Expected salary: ₹60000-90000 per month for this role.", "Rent is about 70000-85000 a month.",
    "order id 7845123690", "x = 9876543211", "+150 200 350 400", "+12 345 678 901",
    "SKU-9845012345", "a + 98450 12345", "2^33 = 8589934592 and 13! = 6227020800.",
    "npm install react@18.3.1", "npm i lodash@4.17.21", "git clone git@github.com:acme/widgets.git",
    "ssh deploy@192.168.1.10", "y = X@W.T + b", "go get golang.org/x/tools@v0.1.0",
    "<img src=\"logo@2x.png\">", "icon@3x.webp", "postgresql://admin:secret@db.internal:5432/app",
    "DATABASE_URL=postgresql://app:secret@db.acmecorp.io/app",
    "https://cdn.jsdelivr.net/npm/lodash@4.17.21/lodash.min.js",
    "timestamp 1732540800", "IP 192.168.100.200", "v10.12.3", "2026-11-25", "ISBN 978-0-306-40615-7",
    "Aadhaar format 1234 5678 9012", "PIN 560001", "Rs. 9,876,543,210", "range(60000, 80000)",
    # test-style domains and role mailboxes in templates (data F4, F12)
    "curl -u user@test.com:pass https://api.local", "write to demo@sample.com",
    "Send your CV to recruitment@acmecorp.in, talent@acmecorp.in or hr+india@acmecorp.in.",
    "careers.india@acmecorp.in and payroll@acmecorp.in",
]


@pytest.mark.parametrize("text", NOT_PERSONAL)
def test_review_numbers_and_code_are_not_masked(text):
    g = P.Guard("")
    assert g.mask(text) == text and g.count == 0


SHOULD_STILL_MASK = [
    ("user: write to xravi@acmecorp.in", "ravi@acmecorp.in"),  # tail of a longer address
    ("user: write to ravi@acmecorp.com", "ravi@acmecorp.co"),  # truncated domain
    ("user: priya.sharma@initech.com", "sharma@initech.com"),
    ("user: my number +91 98450 12345", "+1 98450 12345"),  # another country code
    ("user: numbers 98450 12345 67890", "84501 23456"),  # a window across two numbers
    ("user: id 1198450123459", "98450 12345"),  # inside a longer id
]


@pytest.mark.parametrize("allowed,candidate", SHOULD_STILL_MASK)
def test_review_a_value_not_in_the_prompt_is_not_allowed_by_a_substring(allowed, candidate):
    """F07: whole values, not substrings."""
    out = P.Guard(allowed).mask(f"Contact: {candidate} ok")
    assert candidate not in out, out


SHOULD_ALLOW = [
    ("user: my number is 98450 12345", "98450 12345"),
    ("user: my number is 98450/12345", "98450 12345"),
    ("user: call me on 6123 4567 (Singapore)", "+65 6123 4567"),
    ("user: write to Ravi.Kumar@AcmeCorp.in", "ravi.kumar@acmecorp.in"),
    ("user: +91 98450 12345", "098450 12345"),
    ("user: 98450 12345", "+91 98450 12345"),
]


@pytest.mark.parametrize("allowed,candidate", SHOULD_ALLOW)
def test_review_a_value_the_user_gave_is_kept(allowed, candidate):
    """F13."""
    out = P.Guard(allowed).mask(f"Saved: {candidate} ok")
    assert candidate in out, out


BROAD = [
    "call ", INVENTED, "+91 98450 12345", "9845012345", "+1 415 555 0132", "080-23456789", "(415) 555-0132",
    "a@b.co", "ravi.k@acme.in", "first.last+tag@sub.domain.co.in", "x_y%z@q-r.io", "@", ".", "-", "+",
    "(", ")", " ", "\n", "\t", ":", "/", "_", "%", ",", ";", "0", "7", "9", "98450", " 12345", "(98450)",
    " ", "–", "中文", "रवि", "\U0001F600", "react@18.3.1", "git@github.com:",
    "1.6180339887", "60000-80000", "x = 9876543211", "```", "word", " bytes", "₹", "order id ",
    "https://example.com/a?b=c", "hr@acme.com", "name@example.com", "v10.12.3", "2026-11-25",
    "+९१", "९८४५०", "१२३४५",
]


def test_review_streaming_equals_masking_the_whole_text_over_a_broad_alphabet():
    """The stream property with Unicode digits, separators, cues and code
    (F01), and every released prefix is a prefix of the masked whole: a Stop
    never shows part of a value that gets masked."""
    for trial in range(2500):
        rnd = random.Random(70_000 + trial)
        text = "".join(rnd.choice(BROAD) for _ in range(rnd.randint(1, 40)))
        allowed = "user said +91 98450 12345 and ravi.k@acme.in" if trial % 4 == 0 else ""
        expected = P.Guard(allowed).mask(text)
        g = P.Guard(allowed)
        out, i, size = [], 0, rnd.choice([1, 3, 9, 40])
        while i < len(text):
            n = rnd.randint(1, size)
            out.append(g.feed(text[i : i + n]))
            assert expected.startswith("".join(out)), (trial, text)
            i += n
        out.append(g.finish())
        assert "".join(out) == expected, (trial, text)


def test_review_a_phone_in_another_script_streams_masked():
    """F01."""
    text = "call +९१ ९८४५० १२३४५ now"
    g = P.Guard("")
    joined = "".join(g.feed(ch) for ch in text) + g.finish()
    assert joined == P.Guard("").mask(text) and P.PHONE_MASK in joined


@pytest.mark.parametrize("unit", ["a", "0", "-", "中", "0 ", "1."])
def test_review_a_long_unbroken_run_costs_linear_time(unit):
    """F02: 20 k characters in 4-character deltas (was 6-12 s, quadratic)."""
    import time

    text = "Answer: " + unit * (20_000 // len(unit)) + " done"
    g = P.Guard("")
    started = time.perf_counter()
    for i in range(0, len(text), 4):
        g.feed(text[i : i + 4])
    g.finish()
    assert time.perf_counter() - started < 3.0


@pytest.mark.parametrize("unit", ["0 ", "1.", "12345-", "abc."])
def test_review_the_mask_holds_back_a_bounded_tail(unit):
    """F03: a degenerate loop keeps streaming, so the loop guard behind the
    mask sees it."""
    g = P.Guard("")
    released = "".join(g.feed(unit) for _ in range(2000))
    assert len(g.held()) <= 500 and len(released) >= 2000 * len(unit) - 500


def test_review_the_loop_guard_still_stops_a_numeric_loop(monkeypatch):
    """F03, end to end."""
    from app import continuation, llm
    from app.config import settings
    from app.engines import chat

    consumed = {"n": 0}

    async def fake_stream(messages, **_kwargs):
        yield "token", "Here: "
        for _ in range(3000):
            consumed["n"] += 1
            yield "token", "0 "

    monkeypatch.setattr(llm, "stream_chat_events", fake_stream)
    monkeypatch.setattr(continuation, "budget_for", lambda effort: 8000)

    async def emit(event, data):
        pass

    monkeypatch.setattr(settings, "toggle_off_mask_personal_details", False)
    asyncio.run(chat.run_chat_engine("list the numbers", [], emit, mode="assistant", effort="fast"))
    unmasked = consumed["n"]
    consumed["n"] = 0
    monkeypatch.setattr(settings, "toggle_off_mask_personal_details", True)
    asyncio.run(chat.run_chat_engine("list the numbers", [], emit, mode="assistant", effort="fast"))
    assert unmasked < 1000 and consumed["n"] <= unmasked * 2


# -- saved facts: who HOLDS the detail (F10, F11, R9, data F1) --------------

THIRD_PARTY = [
    f"The user's emergency contact number is {INVENTED}",
    f"The user's manager is Ravi. The user can reach him at {INVENTED}.",
    f"The user's girlfriend Priya: {INVENTED}",
    f"The user's fiancée, Priya, phone {INVENTED}",
    f"The user's referee Mr. Sharma can be reached on {INVENTED}",
    "The user's mentor is Anil; his email is anil@acmecorp.in",
    "The user's CTO is reachable at cto.office@acmecorp.in",
    f"The user's roommate Karan uses {INVENTED}",
    f"The user's reportee Sneha has phone {INVENTED}",
    "The user's interviewer was Mr. Iyer (iyer@acmecorp.in)",
    "The user wants to send the offer letter to priya.s@gmail.com",
    f"The user's previous employer Globex can be reached at {INVENTED}",
    "The user's nephew stays at 4 Lake Road",
    "The user's team lead is Ravi (ravi@acmecorp.in)",
    f"The user's guardian can be contacted on {INVENTED}",
    "Ravi Kumar's number is (415) 555-0132",
    "Ravi Kumar's number is 98450 12345",
    "Ravi Kumar's number is 98450.12345",
    "Ravi Kumar's number is 98 45 01 23 45",
    f"The user's team: Ravi {INVENTED}",
    f"The user's manager Ravi's phone is {INVENTED}",
    "The user's best friend lives at 5 Park Street, Kolkata",
]


@pytest.mark.parametrize("fact", THIRD_PARTY)
def test_review_another_persons_contact_detail_is_third_party(fact):
    assert P.third_party_contact(fact) is True
    assert P.CONTACT_MASK in P.mask_third_party_contacts(fact)


USERS_OWN = [
    f"The user is an HR manager whose phone is {INVENTED}",
    f"The user is a recruiter; phone {INVENTED}",
    "The user is a recruiter and their email is me.recruit@initech.in",
    f"The user's wife is Priya and the user's phone is {INVENTED}",
    f"The user is an employee of Initech; contact {INVENTED}",
    f"The user is a recruiter at Acme and can be reached at {INVENTED}.",
    "The user is an HR executive; her email is asha@acme.com.",
    f"The user, a senior recruiter, uses the phone number {INVENTED}.",
    "The user works with clients in the US; the user's email is asha@acme.com.",
    "The user is a team member of the sales team; email asha@acme.com.",
    f"The user works as a recruiter; her phone is {INVENTED}.",
    "My office address is 12 MG Road, Bangalore.",
    f"Asha is the user's name. Her phone is {INVENTED}.",
    f"The user's phone number is {INVENTED}",
    f"The user's name is Priya. Her number is {INVENTED}.",
]


@pytest.mark.parametrize("fact", USERS_OWN)
def test_review_the_users_own_detail_stays(fact):
    assert P.third_party_contact(fact) is False
    assert P.mask_third_party_contacts(fact) == fact


def test_review_a_contact_clause_is_masked_whole_and_a_city_is_no_detail():
    """data F1 (a third-party "contact info:" clause, location included) and
    F11 (a city is no contact detail; an address ends at its sentence)."""
    masked = P.mask_third_party_contacts("Ravi Kumar contact info: ravi@acmecorp.in, (415) 555-0132, Austin, TX")
    assert masked == f"Ravi Kumar contact info: {P.CONTACT_MASK}"
    assert P.mask_third_party_contacts("The user's mother lives in Pune") == "The user's mother lives in Pune"
    masked = P.mask_third_party_contacts("Ravi's address is 12 MG Road. The user prefers Hindi answers.")
    assert masked == f"Ravi's address is {P.CONTACT_MASK}. The user prefers Hindi answers."


def test_review_a_rewrite_never_writes_the_mask_into_a_stored_fact():
    """F14 / R13 (owner decision 2: no stored data is cleaned)."""
    from app import db, facts

    uid = db.create_user("rv-rewrite", "hash")
    row = db.add_user_fact(uid, f"Ravi Kumar's phone is {INVENTED}", source="stated", source_excerpt="x")
    extractor = _complete({
        "add": [f"Ravi Kumar's number is {P.CONTACT_MASK}"],
        "replace": [{"id": row["id"], "fact": "Ravi Kumar is the user's team lead"}],
        "remove": [],
    })
    asyncio.run(facts.remember_from_message(uid, "Ravi Kumar is now my team lead", None, complete=extractor))
    assert P.CONTACT_MASK in extractor.prompt
    assert [f["fact"] for f in db.list_user_facts(uid)] == [f"Ravi Kumar's phone is {INVENTED}"]


def test_review_the_excerpt_of_a_saved_fact_holds_no_third_party_detail():
    """F17: the excerpt is part of the saved fact row."""
    from app import db, facts

    uid = db.create_user("rv-excerpt", "hash")
    message = f"My number is 98450 99999 and my manager Ravi's is {INVENTED}."
    asyncio.run(facts.remember_from_message(
        uid, message, None,
        complete=_complete({"add": ["The user's phone number is 98450 99999"], "replace": [], "remove": []}),
    ))
    rows = db.list_user_facts(uid)
    assert [f["fact"] for f in rows] == ["The user's phone number is 98450 99999"]
    excerpt = rows[0].get("source_excerpt") or ""
    assert INVENTED not in excerpt and "98450 99999" in excerpt
