"""Asking before answering.

The engine's failure mode was never refusal — it was picking one reading of an
ambiguous question and reporting the number with full confidence. "How many
candidates completed the training from slot 128 and how many failed the mock"
scoped the mock three different ways on three runs and returned 7, 20 and 0.
All three are defensible readings; only one was meant.
"""
from app.core import clarify


SLOT_Q = ("how may candite compelted the traingi from slot 128 "
          "and how many failed the mock")


def test_a_mock_scoped_to_a_slot_is_asked_about():
    c = clarify.needs_clarification(SLOT_Q)
    assert c is not None
    labels = [o.label for o in c.options]
    assert any("this slot's training" in l for l in labels)
    assert any("ever took" in l for l in labels)


def test_the_options_carry_how_to_resolve_them():
    """A label the follow-up turn cannot act on is decoration."""
    c = clarify.needs_clarification(SLOT_Q)
    assert all(o.resolves_to for o in c.options)
    assert "Candidate_Training__c" in c.options[0].resolves_to


def test_there_is_always_a_free_text_escape():
    c = clarify.needs_clarification(SLOT_Q)
    assert c.wire()["options"][-1]["label"] == clarify.OTHER


def test_interview_is_asked_about_because_it_names_two_objects():
    c = clarify.needs_clarification("how many interviews last month")
    assert c is not None
    assert "Internal_Interview__c" in " ".join(o.resolves_to for o in c.options)


def test_an_already_disambiguated_question_is_not_second_guessed():
    """Naming the object settles WHICH object. It may still be asked about a
    period — that is a different, still-open ambiguity."""
    c = clarify.needs_clarification("how many internal interviews")
    assert c is None or "Which interviews" not in c.question
    # "today" settles the period and "oot mocks" settles the object: nothing left.
    assert clarify.needs_clarification("how many oot mocks today") is None


def test_a_question_with_a_period_is_not_asked_for_one():
    assert clarify.needs_clarification("invoiced amount last month") is None
    assert clarify.needs_clarification("invoiced amount by month") is None


def test_a_countless_question_is_left_alone():
    assert clarify.needs_clarification("list the active candidates") is None
    assert clarify.needs_clarification("hello") is None


def test_the_text_form_works_without_any_ui():
    text = clarify.needs_clarification(SLOT_Q).as_text()
    assert "**1.**" in text and "**2.**" in text
    assert clarify.OTHER in text


def test_a_clarified_question_is_not_asked_again():
    """The follow-up turn must reach the engine, not loop."""
    resolved = clarify.resolve(SLOT_Q, "Only mocks from this slot's training")
    assert "(Clarified:" in resolved


def test_the_engine_skips_clarification_once_answered():
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert '"(Clarified:" not in text' in source
    assert "clarify_before_answering" in source


def test_a_word_containing_count_is_not_a_counting_question():
    """`\\bhow many|count|...\\b` alternates across the whole pattern, so the
    bare `count` branch matched the middle of "ac(count)s" and asked "over
    what period?" about "top accounts"."""
    assert clarify.needs_clarification("top accounts") is None
    assert clarify.needs_clarification("show me discounts") is None
    assert clarify.needs_clarification("how many accounts") is not None


# ── The loop ─────────────────────────────────────────────────────────────────
# Picking "Only mocks from this slot's training" sent that sentence back as the
# next message. It contains "mock" and "slot", so the detector fired again and
# asked the identical question — three times in a row, forever.

def _asked_history(question=SLOT_Q):
    return [
        {"role": "user", "content": question},
        {"role": "assistant", "content": clarify.needs_clarification(question).as_text()},
    ]


def test_choosing_an_option_by_label_does_not_ask_again():
    label = "Only mocks from this slot's training"
    resolved = clarify.answered(_asked_history(), label)
    assert resolved is not None
    assert "(Clarified:" in resolved
    assert "Candidate_Training__c" in resolved
    # And the resolved text is exempt from being asked about again.
    assert "(Clarified:" in resolved


def test_choosing_an_option_by_number_works():
    resolved = clarify.answered(_asked_history(), "2")
    assert "across all their trainings" in resolved


def test_the_label_with_its_description_appended_still_matches():
    """The UI sends "label — description" as one string."""
    reply = ("Only mocks from this slot's training — The mock attached to the "
             "training they took in this slot.")
    resolved = clarify.answered(_asked_history(), reply)
    assert "Candidate_Training__c" in resolved


def test_free_text_is_carried_through_verbatim():
    resolved = clarify.answered(_asked_history(), "only the final week mocks")
    assert "only the final week mocks" in resolved


def test_the_original_question_survives_into_the_resolution():
    resolved = clarify.answered(_asked_history(), "1")
    assert "slot 128" in resolved


def test_an_ordinary_follow_up_is_not_treated_as_an_answer():
    history = [
        {"role": "user", "content": "how many invoices"},
        {"role": "assistant", "content": "There are 116 invoices."},
    ]
    assert clarify.answered(history, "and how many are unpaid") is None


def test_no_history_means_nothing_to_resolve():
    assert clarify.answered([], "1") is None


def test_the_dispatch_checks_for_an_answer_before_asking_again():
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert "clarify_mod.answered(full_history, text)" in source


# ── Always-ask mode ──────────────────────────────────────────────────────────

def test_always_mode_confirms_even_an_unambiguous_question():
    c = clarify.needs_clarification("how many invoices last month", always=True)
    assert c is not None
    assert "read it right" in c.question
    assert c.options[0].label == "Yes, run it"


def test_ambiguous_mode_stays_quiet_on_a_clear_question():
    assert clarify.needs_clarification("how many invoices last month") is None


def test_a_real_ambiguity_still_wins_over_the_generic_confirmation():
    c = clarify.needs_clarification(SLOT_Q, always=True)
    assert "Which mocks" in c.question


def test_the_confirmation_states_the_actual_reading():
    """It describes what will run, from the same matchers the query uses —
    not a second, prettier guess at it."""
    c = clarify.confirmation("how many training sessions last month")
    assert "period: last month" in c.reason
    assert "from:" in c.reason


def test_a_question_with_no_period_says_all_time():
    assert "period: all time" in clarify.confirmation("how many invoices").reason


def test_the_mode_defaults_to_asking_only_when_it_matters(monkeypatch):
    """Owner feedback: "it should ask only relevant questions, not the same
    one again and again". `always` remains available for a deployment that
    wants a checkpoint on every question."""
    from app.config import Settings

    monkeypatch.delenv("CLARIFY_MODE", raising=False)
    assert Settings().clarify_mode == "ambiguous"
    monkeypatch.setenv("CLARIFY_MODE", "always")
    assert Settings().clarify_mode == "always"
    monkeypatch.setenv("CLARIFY_MODE", "off")
    assert Settings().clarify_before_answering is False


# ── The second loop: "Yes, run it" was clarified again, and again ────────────
# The options sent back their LABEL. With the assistant turn not always present
# in the history the client posts, the server saw a bare new question.

def test_each_option_sends_the_whole_resolved_question():
    wire = clarify.needs_clarification(SLOT_Q).wire(SLOT_Q)
    assert wire["original"] == SLOT_Q
    for option in wire["options"]:
        if option.get("free_text"):
            continue
        assert option["send"].startswith(SLOT_Q)
        assert "(Clarified:" in option["send"]


def test_what_the_options_send_is_never_clarified_again():
    """The server skips any message already carrying a resolution."""
    for option in clarify.needs_clarification(SLOT_Q).wire(SLOT_Q)["options"]:
        if option.get("free_text"):
            continue
        assert "(Clarified:" in option["send"]


def test_something_else_asks_for_text_instead_of_sending_its_own_label():
    other = clarify.confirmation("how many invoices").wire("how many invoices")["options"][-1]
    assert other["label"] == clarify.OTHER
    assert other["free_text"] is True
    assert other["send"] == ""


def test_the_confirmation_carries_the_original_question_for_free_text():
    wire = clarify.confirmation("how many invoices").wire("how many invoices")
    assert wire["original"] == "how many invoices"


# ── Irrelevant questions ─────────────────────────────────────────────────────
# "Interview Readiness Training" is a PROGRAMME. Asked for its sessions, the
# interview-object question fired on the bare word "interview" and interrupted
# a question that was never about interviews.

READINESS_Q = ("now i want to know the session that are taken for the interview "
               "readiness training for yesterday so give all sessions details")


def test_a_programme_name_is_not_an_interview_question():
    assert clarify.needs_clarification(READINESS_Q) is None
    assert clarify.needs_clarification("interview skills training sessions") is None


def test_asking_about_sessions_is_not_asking_about_interviews():
    assert clarify.needs_clarification("show me the sessions for yesterday") is None


def test_a_genuine_interview_question_is_still_asked_about():
    c = clarify.needs_clarification("how many interviews were ghosted")
    assert c is not None and "Which interviews" in c.question


def test_every_question_offers_a_way_straight_past_it():
    """Owner request: a one-click "just answer it" on every question."""
    wire = clarify.needs_clarification(SLOT_Q).wire(SLOT_Q)
    labels = [o["label"] for o in wire["options"]]
    assert clarify.PROCEED in labels
    assert clarify.OTHER in labels
    proceed = next(o for o in wire["options"] if o["label"] == clarify.PROCEED)
    assert proceed["send"].startswith(SLOT_Q)
    assert "do not narrow" in proceed["send"]


def test_the_text_form_lists_both_escapes():
    text = clarify.needs_clarification(SLOT_Q).as_text()
    assert clarify.PROCEED in text and clarify.OTHER in text
