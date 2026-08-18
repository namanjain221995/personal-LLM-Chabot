"""The deterministic floor: which ambiguities are worth a question, and which
questions must never be asked.

The engine's failure mode was never refusal — it was picking one reading of an
ambiguous question and reporting the number with full confidence. "How many
candidates completed the training from slot 128 and how many failed the mock"
scoped the mock three different ways on three runs and returned 7, 20 and 0.
All three are defensible readings; only one was meant.

This module used to own a whole parallel clarification system as well — a
markdown card, a history-sniffing answer matcher, and an always-on confirmation
prompt. That is gone: `core/sf_intel/` owns persistence, resume, loop guards and
the UI contract, and these detectors feed it (`planner.deterministic_decision`).
What is tested here is therefore the JUDGEMENT — when to ask — not a transport.
"""
from app.core import clarify
from app.core.sf_intel import planner


SLOT_Q = ("how may candite compelted the traingi from slot 128 "
          "and how many failed the mock")


# ── When there is a real ambiguity ───────────────────────────────────────────

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


def test_interview_is_asked_about_because_it_names_two_objects():
    c = clarify.needs_clarification("how many interviews last month")
    assert c is not None
    assert "Internal_Interview__c" in " ".join(o.resolves_to for o in c.options)


def test_a_genuine_interview_question_is_still_asked_about():
    c = clarify.needs_clarification("how many interviews were ghosted")
    assert c is not None and "Which interviews" in c.question


def test_every_question_carries_a_topic_header():
    """The card reads "Clarification · <header>", so a header of "Salesforce"
    tells the user nothing they did not already know."""
    for question in (SLOT_Q, "how many interviews last month", "how many accounts"):
        found = clarify.needs_clarification(question)
        assert found is not None
        assert found.header and found.header != "Salesforce"
        assert len(found.header.split()) <= 4


# ── What must never be asked ─────────────────────────────────────────────────

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


def test_a_word_containing_count_is_not_a_counting_question():
    """`\\bhow many|count|...\\b` alternates across the whole pattern, so the
    bare `count` branch matched the middle of "ac(count)s" and asked "over
    what period?" about "top accounts"."""
    assert clarify.needs_clarification("top accounts") is None
    assert clarify.needs_clarification("show me discounts") is None
    assert clarify.needs_clarification("how many accounts") is not None


def test_a_typo_in_the_period_is_not_an_absent_period():
    """The detectors run on the spelling-repaired reading, so a question that
    plainly states its period is not asked for one because it was mistyped.

    This was the most common way the feature annoyed people: "how many advance
    mock scheddule todau" says today, and the matcher's answer was "over what
    period?"."""
    assert clarify.needs_clarification("how many advance mock scheddule todau") is None
    assert clarify.needs_clarification("how many invoices todya") is None
    assert clarify.needs_clarification("how many mocks tomorow") is None


def test_a_conceptual_question_is_not_offered_a_choice_of_object():
    """"How does the interview process work" wants an explanation. A card
    offering two Salesforce objects is a non-sequitur — there are no records
    behind the question at all."""
    assert clarify.needs_clarification("how does the interview process work") is None
    assert clarify.needs_clarification("what is an interview evaluation") is None
    assert clarify.needs_clarification("explain the interview stages") is None


def test_for_the_and_for_each_do_not_read_as_a_named_person():
    """The person hint is a CAPITAL letter after "for". Case-insensitively it
    matched "for the", "for each" and "for all", so any question containing
    those words was treated as already scoped to someone."""
    assert clarify.needs_clarification("how many mocks for the team") is not None
    assert clarify.needs_clarification("how many mocks for each niche") is not None
    # A real name still settles the scope.
    assert clarify.needs_clarification("how many mocks for Divya") is None


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


# ── What the floor hands to the clarification system ─────────────────────────

def test_the_floor_produces_a_validated_decision():
    """`deterministic_decision` is the ONLY route from these detectors into the
    product. Whatever it returns is promoted to a persisted, resumable card, so
    it has to satisfy the same schema the planner's output does."""
    decision = planner.deterministic_decision(SLOT_Q)
    assert decision.action == "ASK_CLARIFICATION"
    draft = decision.clarification_draft
    assert draft is not None
    assert 2 <= len(draft.options) <= 4
    assert draft.slot in ("object", "date_range", "filter")
    # Alternative readings of ONE number: ticking two is incoherent.
    assert draft.multi_select is False


def test_no_detected_ambiguity_means_answer_not_ask_something_generic():
    """There is no worse moment to invent a question than the one where the
    component that decides what to ask has just failed."""
    decision = planner.deterministic_decision("how many oot mocks today")
    assert decision.action == "EXECUTE_SALESFORCE"
    assert decision.clarification_draft is None


def test_the_user_facing_half_of_an_option_names_no_salesforce_object():
    """`label`/`description` are read by a recruiter; `value` is read by the
    query planner. "Use Interview__c with record type 'Interview'" was shipped
    as the thing a user clicked on."""
    decision = planner.deterministic_decision("how many interviews last month")
    draft = decision.clarification_draft
    visible = " ".join(
        [draft.question, draft.header]
        + [o.label for o in draft.options]
        + [o.description for o in draft.options]
    )
    assert "__c" not in visible and "__r" not in visible
    # …and the precision survived where it is actually used.
    assert any("__c" in o.value for o in draft.options)


# ── Configuration ────────────────────────────────────────────────────────────

def test_the_mode_defaults_to_asking_only_when_it_matters(monkeypatch):
    """Owner feedback: "it should ask only relevant questions, not the same
    one again and again". `always` remains available for a deployment that
    wants a lower bar — as a BIAS on the planner, not as a card with no
    content, which is what it used to bolt onto every question."""
    from app.config import Settings

    monkeypatch.delenv("CLARIFY_MODE", raising=False)
    assert Settings().clarify_mode == "ambiguous"
    monkeypatch.setenv("CLARIFY_MODE", "always")
    assert Settings().clarify_mode == "always"
    monkeypatch.setenv("CLARIFY_MODE", "off")
    assert Settings().clarify_before_answering is False


def test_a_resolved_request_is_never_clarified_again():
    """The `"(Clarified:" ` sentinel is what stops a resumed request being read
    as a fresh, ambiguous one. It is load-bearing in the dispatch."""
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert '"(Clarified:" not in text' in source


def test_there_is_exactly_one_clarification_payload_shape():
    """The legacy `meta.clarify` card is gone. Two payloads meant two renderers,
    two loop-prevention schemes, and a kill switch that quietly downgraded the
    user to the one that could not resume."""
    import inspect

    from app import main

    source = inspect.getsource(main)
    assert '"clarify": ' not in source
    assert "clarify_mod" not in source
