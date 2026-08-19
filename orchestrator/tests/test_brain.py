"""The Salesforce brain: knowledge packs and the learn-from-chat loop.

The load-bearing claims:
- a pack dropped into the brain directory reaches every knowledge surface —
  domain rules, metrics, pinned tables, glossary, prose retrieval, and the
  field dictionary's help text — with no restart;
- a malformed pack degrades to "less knowledge", never to an exception, and
  a metric missing its canonical SQL is skipped rather than half-injected;
- field notes are ENRICH-ONLY: a note for a field the org lacks vanishes,
  because teaching the model a sandbox-only field is how silently-wrong SQL
  gets written;
- thumbs-up answers become few-shot SQL examples for similar questions, a
  thumbs-down anywhere disqualifies that SQL globally, and the whole loop
  failing (no DB, nothing rated) leaves the prompt unchanged;
- the shipped qb-invoicing pack actually parses and triggers.
"""
import json
import textwrap

import pytest

from app import db
from app.config import settings
from app.core import brain, learned_examples, org_brief, sf_dictionary


@pytest.fixture(autouse=True)
def clean_brain(tmp_path, monkeypatch):
    """Each test gets its own empty brain directory and cold caches."""
    monkeypatch.setattr(settings, "brain_dir", str(tmp_path / "packs"))
    (tmp_path / "packs").mkdir()
    monkeypatch.setattr(brain, "_cache", None)
    monkeypatch.setattr(sf_dictionary, "_cache", None)
    learned_examples.invalidate()
    yield
    monkeypatch.setattr(sf_dictionary, "_cache", None)


def write_pack(tmp_path, name="billing", **overrides) -> None:
    pack = {
        "name": name,
        "triggers": ["invoice", "emi", "lump sum"],
        "tables": ["Invoice__c", "Payment__c"],
        "rules": "Billing rules:\n- EMI cycles are Payments with a sales receipt id.",
        "metrics": [
            {
                "name": "emi cycles completed",
                "aliases": ["cycles completed", "emis paid"],
                "table": "Payment__c",
                "definition": "Recurring charges billed.",
                "sql": "SELECT count(*) FROM Payment__c WHERE QB_Sales_Receipt_ID__c IS NOT NULL",
                "caveat": "Partial payments are not cycles.",
            }
        ],
        "glossary": {"EMI": "One installment of a recurring plan."},
        "field_notes": {
            "Invoice__c": {
                "Type__c": "One-Time or Recurring; drives most logic.",
                "Ghost_Field__c": "This field does not exist in the org.",
            }
        },
        "knowledge": [
            {
                "title": "How recurring plans re-amortize",
                "keywords": ["amortization", "reschedule"],
                "text": "The plan keeps a fixed EMI; changes recompute the "
                        "number of cycles, and reschedule is the exception "
                        "that changes the EMI itself.",
            }
        ],
    }
    pack.update(overrides)
    import yaml

    (tmp_path / "packs" / f"{name}.yaml").write_text(
        yaml.safe_dump(pack, sort_keys=False), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_a_dropped_pack_is_live_without_a_restart(tmp_path):
    assert brain.load() == []
    write_pack(tmp_path)
    packs = brain.load()  # no cache reset in between — mtime scan finds it
    assert [p["name"] for p in packs] == ["billing"]


def test_a_malformed_pack_never_raises_and_never_blocks_the_rest(tmp_path):
    (tmp_path / "packs" / "broken.yaml").write_text("]{not yaml:::", encoding="utf-8")
    write_pack(tmp_path)
    assert [p["name"] for p in brain.load()] == ["billing"]


def test_a_metric_without_canonical_sql_is_skipped(tmp_path):
    write_pack(tmp_path, metrics=[{"name": "half", "table": "X", "definition": "d"}])
    assert brain.load()[0]["metrics"] == []


def test_the_master_switch_turns_everything_off(tmp_path, monkeypatch):
    write_pack(tmp_path)
    monkeypatch.setattr(settings, "brain_enabled", False)
    assert brain.load() == []
    assert brain.rules_for("invoice status") == ""


# ---------------------------------------------------------------------------
# Reaching the prompts
# ---------------------------------------------------------------------------


def test_rules_reach_grounding_when_and_only_when_triggered(tmp_path):
    write_pack(tmp_path)
    assert "sales receipt id" in org_brief.grounding_for("how many invoices are unpaid?")
    assert "sales receipt id" not in org_brief.grounding_for("sessions hosted yesterday")


def test_multiword_trigger_matches_as_a_phrase(tmp_path):
    write_pack(tmp_path)
    assert brain.rules_for("apply a lump sum to the plan") != ""
    # "lump" alone is not the phrase.
    assert brain.rules_for("a lump of data") == ""


def test_pack_metrics_join_the_semantic_layer(tmp_path):
    write_pack(tmp_path)
    picked = org_brief.match_metrics("how many emis paid this month?")
    assert any(m["name"] == "emi cycles completed" for m in picked)
    hint = org_brief.metric_hint("cycles completed so far")
    assert "QB_Sales_Receipt_ID__c IS NOT NULL" in hint
    assert "Partial payments are not cycles." in hint


def test_pack_tables_are_pinned_into_the_schema_slice(tmp_path):
    write_pack(tmp_path)
    assert "Payment__c" in org_brief.tables_for("what is our emi position?")


def test_glossary_defines_only_terms_the_question_uses(tmp_path):
    write_pack(tmp_path)
    assert "installment" in brain.glossary_for("what does EMI mean here?")
    assert brain.glossary_for("training sessions today") == ""


def test_knowledge_retrieval_prefers_title_and_keyword_hits(tmp_path):
    write_pack(tmp_path)
    block = brain.knowledge_for("how does the plan re-amortize after a reschedule?")
    assert "fixed EMI" in block
    assert brain.knowledge_for("who attended the python class?") == ""


def test_stacked_pack_rules_are_capped_strongest_match_first(tmp_path):
    """A multi-domain question must not stack every matched pack's rules.

    The budget moved 9KB -> 24KB (2026-08-18) — the question that needs the
    most knowledge was getting the least — but the ORDERING guarantee is what
    this protects: strongest trigger match first, whole blocks only."""
    filler = "x" * 4800
    write_pack(tmp_path, name="strong",
               triggers=["invoice", "emi", "payment"],
               rules="STRONG-RULES " + filler, metrics=[], field_notes={}, knowledge=[])
    write_pack(tmp_path, name="weak-a", triggers=["payment"],
               rules="WEAK-A-RULES " + filler, metrics=[], field_notes={}, knowledge=[])
    write_pack(tmp_path, name="weak-b", triggers=["payment"],
               rules="WEAK-B-RULES " + filler, metrics=[], field_notes={}, knowledge=[])
    rules = brain.rules_for("emi payment on this invoice")
    assert "STRONG-RULES" in rules              # 3 trigger hits — always first
    assert len(rules) <= brain._RULES_TOTAL_CAP
    # Whole blocks only, strongest first: with ~4.8KB each (capped per pack at
    # _RULES_CAP) and a 24KB total, all three fit and none is sliced.
    assert rules.count("-RULES") == 3
    assert rules.index("STRONG-RULES") == 0


# ---------------------------------------------------------------------------
# The field dictionary overlay
# ---------------------------------------------------------------------------


def test_field_notes_enrich_existing_fields_and_drop_ghosts(tmp_path, monkeypatch):
    write_pack(tmp_path)
    dictionary = {
        "objects": {
            "Invoice__c": {
                "api": "Invoice__c",
                "label": "Invoice",
                "fields": [{"api": "Type__c", "label": "Type", "type": "Picklist"}],
            }
        }
    }
    path = tmp_path / "sf_dictionary.json"
    path.write_text(json.dumps(dictionary), encoding="utf-8")
    loaded = sf_dictionary.load(str(path))
    fields = {f["api"]: f for f in loaded["objects"]["Invoice__c"]["fields"]}
    assert "drives most logic" in fields["Type__c"]["help"]
    # The ghost note must NOT invent a field the org lacks.
    assert "Ghost_Field__c" not in fields
    # And the help text reaches the rendered hint.
    hint = sf_dictionary.hint_for("invoice type")
    assert "drives most logic" in hint


# ---------------------------------------------------------------------------
# Learn-from-chat (unit level — the SQL few-shot block)
# ---------------------------------------------------------------------------


@pytest.fixture()
def corpus(monkeypatch):
    examples = [
        {"question": "how many invoices are unpaid?",
         "sql": "SELECT count(*) FROM Invoice__c WHERE Invoice_Status__c = 'Not Paid'"},
        {"question": "sessions hosted by each trainer",
         "sql": "SELECT r.Last_Name__c, count(*) FROM Session__c s JOIN Recruiter__c r ON s.Host_User__c = r.Id GROUP BY 1"},
    ]
    monkeypatch.setattr(db, "list_confirmed_sql_examples", lambda limit=200: examples)
    learned_examples.invalidate()
    return examples


def test_a_similar_confirmed_answer_becomes_a_few_shot(corpus):
    block = learned_examples.block_for("how many invoices are still unpaid this month?")
    assert "Invoice_Status__c = 'Not Paid'" in block
    assert "thumbs-up" in block
    # The unrelated example stays out.
    assert "Session__c" not in block


def test_no_resemblance_means_no_examples_and_an_unchanged_prompt(corpus):
    assert learned_examples.block_for("candidates placed per niche") == ""


def test_learning_can_be_switched_off(corpus, monkeypatch):
    monkeypatch.setattr(settings, "learned_examples_enabled", False)
    assert learned_examples.block_for("how many invoices are unpaid?") == ""


def test_a_database_failure_degrades_to_no_examples(monkeypatch):
    def boom(limit=200):
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "list_confirmed_sql_examples", boom)
    learned_examples.invalidate()
    assert learned_examples.block_for("how many invoices are unpaid?") == ""


# ---------------------------------------------------------------------------
# Learn-from-chat (database level — what counts as confirmed)
# ---------------------------------------------------------------------------


def _seed_rated_answer(user_id, conv, question, sql, feedback):
    db.create_conversation(user_id, conv, "t")
    db.add_message(user_id, conv, "user", question, None)
    row = db.add_message(user_id, conv, "assistant", "answer", {"route": "sql", "sql": sql})
    db.set_message_feedback(user_id, conv, row["id"], feedback)


def test_only_thumbs_up_sql_answers_are_confirmed(as_user):
    user = as_user("alice")
    _seed_rated_answer(user["id"], "c-up", "unpaid invoices?", "SELECT 1", "up")
    _seed_rated_answer(user["id"], "c-down", "sessions today?", "SELECT 2", "down")
    db.create_conversation(user["id"], "c-none", "t")
    db.add_message(user["id"], "c-none", "assistant", "prose answer", {"route": "rag"})

    examples = db.list_confirmed_sql_examples()
    assert [(e["question"], e["sql"]) for e in examples] == [("unpaid invoices?", "SELECT 1")]


def test_a_thumbs_down_anywhere_disqualifies_that_sql_globally(as_user):
    user = as_user("alice")
    _seed_rated_answer(user["id"], "c1", "unpaid invoices?", "SELECT 1", "up")
    _seed_rated_answer(user["id"], "c2", "unpaid invoices again?", "SELECT 1", "down")
    assert db.list_confirmed_sql_examples() == []


# ---------------------------------------------------------------------------
# The shipped pack
# ---------------------------------------------------------------------------


@pytest.fixture()
def shipped_packs(monkeypatch):
    from pathlib import Path

    packs_dir = Path(__file__).resolve().parents[2] / "brain" / "packs"
    monkeypatch.setattr(settings, "brain_dir", str(packs_dir))
    monkeypatch.setattr(brain, "_cache", None)
    return {p["name"]: p for p in brain.load()}


def test_the_shipped_qb_invoicing_pack_parses_and_triggers(shipped_packs):
    pack = shipped_packs["qb-invoicing"]
    assert pack["rules"] and pack["metrics"] and pack["knowledge"]
    # No metric lost to the required-keys filter — a skipped metric here
    # means a YAML editing mistake, not a preference.
    assert len(pack["metrics"]) == 3
    # The rollout trap: recurring questions must carry the not-live caveat.
    grounding = org_brief.grounding_for("how many recurring emi plans are active?")
    assert "not" in grounding.lower() and "sandbox" in grounding.lower()
    # Conceptual questions retrieve documentation.
    assert brain.knowledge_for("how does a chargeback dispute get detected?") != ""


def test_the_shipped_training_module_pack_parses_and_triggers(shipped_packs):
    pack = shipped_packs["training-module"]
    assert pack["rules"] and pack["metrics"] and pack["knowledge"]
    assert len(pack["metrics"]) == 3
    # The deliverable-status semantics must reach SQL grounding — 'Not
    # Active' counted as pending work is the wrong-number trap here.
    grounding = org_brief.grounding_for("how many deliverables are overdue?")
    assert "Locked" in grounding and "Not Active" in grounding
    # Session-analysis questions must be steered to "not populated yet",
    # never an average over 3,101 empty rows.
    grounding2 = org_brief.grounding_for("what is the average integrity score of sessions?")
    assert "not live" in grounding2.lower() or "not populated" in grounding2.lower()
    # The window-occupancy metric must carry the over-capacity caveat.
    hint = org_brief.metric_hint("which session windows have seats left?")
    assert "Booked_Count__c" in hint and "capacity" in hint.lower()
    # Conceptual retrieval: the drop cascade comes from the handbook.
    assert "Drop_Date" in brain.knowledge_for(
        "what happens when a candidate's training is dropped?"
    )


def test_the_shipped_background_check_pack_parses_and_triggers(shipped_packs):
    pack = shipped_packs["background-check"]
    assert pack["rules"] and pack["metrics"] and pack["knowledge"]
    assert len(pack["metrics"]) == 3
    # The misspelled record type is the SQL trap on this object — it must
    # reach grounding for track questions.
    grounding = org_brief.grounding_for("how many background checks are on the offer received track?")
    assert "Offer_Recived" in grounding
    # The formula-date trap: "when was it signed" must be steered to history.
    grounding2 = org_brief.grounding_for("when was the promissory note signed for this candidate?")
    assert "TODAY()" in grounding2 and "history" in grounding2.lower()
    # The swapped-error-message defect is retrievable for save questions.
    assert "swapped" in brain.knowledge_for(
        "why does the background check save show the wrong error message?"
    ).lower() or "shuffled" in brain.knowledge_for(
        "why does the background check save show the wrong error message?"
    ).lower()


def test_the_shipped_docusign_pack_parses_and_triggers(shipped_packs):
    pack = shipped_packs["docusign-integration"]
    assert pack["rules"] and pack["metrics"] and pack["knowledge"]
    assert len(pack["metrics"]) == 3
    # The environment split is THE trap here: production has no automatic
    # writeback today — that must reach grounding for writeback questions.
    grounding = org_brief.grounding_for("does docusign update salesforce when the envelope is signed?")
    assert "by hand" in grounding or "NOT been executed" in grounding
    # Envelope analytics must be steered to the real dfsle table.
    hint = org_brief.metric_hint("how many docusign envelopes were voided?")
    assert "dfsle__EnvelopeStatus__c" in hint
    # The DS_* spelling drift must reach grounding for those fields.
    grounding2 = org_brief.grounding_for("what is the ds debit amount on the background check envelope?")
    assert "Formate" in grounding2
    # Conceptual retrieval: the collaborative-correction mechanism.
    assert "void" in brain.knowledge_for(
        "how does the cs person fix a wrong bank account number the candidate typed?"
    ).lower()


def test_the_docusign_field_reference_addition_retrieves(shipped_packs):
    """The 2026-08-18 field-reference companion: usage rules + signer
    validation must be retrievable, not just filed."""
    assert "PersonEmail" in brain.knowledge_for(
        "which email field do we use for the candidate recipient in docusign?"
    )
    assert "9 digits" in brain.knowledge_for(
        "why does docusign say the bank routing number is invalid?"
    )


def test_the_shipped_apex_reference_pack_is_knowledge_only(shipped_packs):
    pack = shipped_packs["apex-reference"]
    assert pack["knowledge"] and not pack["rules"] and not pack["metrics"]
    # Retrieval: the practical question this pack exists to answer.
    block = brain.knowledge_for("how long do zoom recording download links stay valid?")
    assert "480" in block or "8 hours" in block
    # And it must never leak into unrelated grounding (knowledge-only pack).
    assert brain.rules_for("zoom recording portal settings") == "" or \
        "apex-reference" not in [p["name"] for p in brain.matched_packs("zoom recording portal settings") if p["rules"]]


def test_the_shipped_internal_interview_pack_parses_and_triggers(shipped_packs):
    pack = shipped_packs["internal-interview"]
    assert pack["rules"] and pack["metrics"] and pack["knowledge"]
    assert len(pack["metrics"]) == 3
    # The reschedule double-count trap must reach SQL grounding for mock
    # questions — it is the single biggest overstatement risk on this object.
    grounding = org_brief.grounding_for("how many mocks were completed last month?")
    assert "Rescheduled" in grounding
    # The deprecated-column trap: question-by-niche counts must be steered to
    # the junction, and the canonical SQL must reach the metric hint.
    hint = org_brief.metric_hint("how many questions per niche do we have?")
    assert "Niche_Question__c" in hint
    # Conceptual retrieval: question-selection mechanics come from the doc.
    assert "shuffle" in brain.knowledge_for(
        "how does magic mode pick the questions for an interview?"
    ).lower()


def test_a_pack_over_the_rules_cap_is_cut_at_a_rule_boundary(tmp_path, monkeypatch, caplog):
    """`rules[:_RULES_CAP]` cut mid-word, which is exactly what this module's
    own comment forbids: "a truncated rule reads as a complete one, which is
    worse than a missing one".

    Live consequence: the internal-interview pack grew past the cap and its
    rule about which object an interviewer lives on ended at "a CANDIDATE or
    an INTERVIEW" — leaving the model to finish the sentence itself.
    """
    long_rule = "- " + "x" * 400
    body = "\n".join([long_rule] * 20)  # comfortably over the cap
    (tmp_path / "packs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "packs" / "big.yaml").write_text(
        "name: big\ntriggers:\n  - bigsubject\nrules: |\n"
        + "\n".join(f"  {line}" for line in body.split("\n"))
    )
    monkeypatch.setattr(settings, "brain_dir", str(tmp_path / "packs"))
    brain._cache = None

    rules = brain.load()[0]["rules"]
    assert len(rules) <= brain._RULES_CAP
    # Every kept rule is whole: nothing ends mid-token.
    for line in rules.split("\n"):
        assert line == "" or line.startswith("- ")
    assert rules.rstrip().endswith("x")
    assert len(rules.split("\n")) < 20  # something really was dropped


def test_the_shipped_internal_interview_pack_fits_its_budget(shipped_packs):
    """The pack that taught the interviewer join must actually REACH the
    prompt. It was silently over the cap, so the rule was cut off."""
    pack = shipped_packs["internal-interview"]
    assert len(pack["rules"]) <= brain._RULES_CAP
    # The rule survives intact, ending in a complete sentence.
    assert "WHO A NAMED PERSON IS" in pack["rules"]
    assert "which reading you used." in pack["rules"]
    # Interviewer -> Recruiter__c is the whole point of it.
    assert "Recruiter__c" in pack["rules"]
    # …and Recruiter__c is pinned, so its columns are in the slice when a
    # person is named.
    assert "Recruiter__c" in pack["tables"]


def test_the_shipped_training_portal_implementation_pack_is_knowledge_first(shipped_packs):
    """The feature-map pack (2026-08-19): 166 chunks of Dev9 REPO
    documentation plus a rules block whose only job is to stop the model
    treating repo metadata as columns."""
    pack = shipped_packs["training-portal-implementation"]
    assert pack["knowledge"] and pack["rules"]
    # Knowledge-first: no metrics, no field notes. Field notes would collide
    # with kb-training-lms/training-module, and the LAST pack alphabetically
    # silently wins that collision in sf_dictionary.merge.
    assert pack["metrics"] == []
    assert pack["field_notes"] == {}
    assert len(pack["knowledge"]) > 150
    assert len(pack["rules"]) <= brain._RULES_CAP
    # Every chunk is inside the loader's per-chunk cap, so nothing is
    # truncated mid-sentence on the way into a prompt.
    assert all(len(c["text"]) <= brain._KNOWLEDGE_CHUNK_CAP for c in pack["knowledge"])

    # The provenance rule is the point of the whole pack.
    assert "SANDBOX" in pack["rules"] and "never columns" in pack["rules"]

    # Retrieval: the questions this pack exists to answer.
    assert "S3" in brain.knowledge_for("how does a candidate submit a deliverable file?")
    assert brain.knowledge_for("why do i have to republish the experience site?") != ""


def test_the_feature_map_pack_caveats_every_sandbox_field_it_names(shipped_packs):
    """knowledge_for labels these chunks "authoritative", so a repo field
    list production lacks would read as a schema. Chunks that name one carry
    their own caveat — this is what keeps validate_packs.py at 0 advisories."""
    pack = shipped_packs["training-portal-implementation"]
    caveated = [c for c in pack["knowledge"]
                if c["text"].startswith("NOT IN THE PRODUCTION WAREHOUSE")]
    assert len(caveated) >= 30
    # The known phantoms must be inside a caveat header, never bare.
    for phantom in ("Secret_Token__c", "Status_Message__c", "S3_Object_Key__c",
                    "Human_Score__c", "Password__c"):
        naming = [c for c in pack["knowledge"] if phantom in c["text"]]
        assert naming, f"{phantom} vanished from the pack"
        for chunk in naming:
            header = chunk["text"].split("\n\n", 1)[0]
            assert phantom in header, f"{phantom} named without a caveat in {chunk['title']!r}"


def test_the_training_module_pack_fits_its_budget_after_the_feature_map(shipped_packs):
    """The 2026-08-19 additions took this pack to within a few chars of the
    per-pack cap. If it goes over, `_capped_rules` drops whole rules at a
    boundary and logs — the knowledge is gone from the prompt silently."""
    pack = shipped_packs["training-module"]
    assert len(pack["rules"]) <= brain._RULES_CAP
    # The last-added rule must still be present, i.e. nothing was trimmed.
    assert "SCHEDULED day" in pack["rules"]


def test_the_feature_map_corrections_reach_grounding(shipped_packs):
    """Facts the feature-map ingestion proved against production. Each one
    has to actually reach a prompt, which is a different question from
    whether it is written down somewhere."""
    # Deadline maths: the open work carries no due date at all.
    overdue = org_brief.grounding_for("how many deliverables are overdue?")
    assert "997" in overdue and "Due_Date_Sort__c" in overdue


def test_the_two_org_wide_training_traps_reach_every_training_question():
    """These two live in org_brief.TRAINING_RULES, not in a pack, and the
    reason is mechanical: `training`, `trainings` and `candidates` are
    org_brief trigger words, so a pack carrying them would never fire on
    "how many candidates are in training?" — which is exactly the question
    the counting trap exists to stop getting wrong."""
    for question in (
        "how many candidates are in training?",
        "how many trainings are active?",
        "how many modules has this candidate completed?",
        "list the candidates in training",
    ):
        grounding = org_brief.grounding_for(question)
        assert "COUNT(DISTINCT Candidate__c)" in grounding, question
        assert "Interview Readiness Training" in grounding, question


def test_the_portal_is_no_longer_described_as_unrolled_out(shipped_packs):
    """kb-portal-auth told the model "zero/tiny production rows = not rolled
    out". Production holds 310 credentials and 878 sessions, so that rule
    made a real, answerable metric unanswerable."""
    rules = shipped_packs["kb-portal-auth"]["rules"]
    # The old PRESCRIPTION is gone; the phrase survives only inside the
    # correction that quotes it, which is deliberate provenance.
    assert "zero/tiny production rows = not rolled out" not in rules
    assert "the portal IS live in production" in rules
    assert "310" in rules and "878" in rules
    # And the training-module pack no longer claims the token is unsynced.
    portal = shipped_packs["training-module"]
    tokens = [c for c in portal["knowledge"] if "session token" in c["text"]]
    assert tokens, "the portal knowledge chunk disappeared"
    assert not any("token field itself is not synced" in c["text"] for c in tokens)


def test_the_cs_sop_packs_parse_and_reach_their_own_questions(shipped_packs):
    """The two Customer Success SOPs (2026-08-19). Both sources refuse to name
    a Salesforce field on purpose, so these packs are process knowledge plus a
    rules block carrying the mappings that were verified separately against
    production."""
    for name, chunks in (("cs-candidate-lifecycle", 15),
                         ("cs-internal-operations", 18)):
        pack = shipped_packs[name]
        assert len(pack["knowledge"]) == chunks
        assert len(pack["rules"]) <= brain._RULES_CAP
        assert all(len(c["text"]) <= brain._KNOWLEDGE_CHUNK_CAP
                   for c in pack["knowledge"])
        # Both metrics survived the required-keys filter in _normalise.
        assert len(pack["metrics"]) == 2

    # The questions these packs exist to answer, each reaching its own chunk.
    for question, expected in (
        ("can marketing share new credentials as soon as an offer arrives?",
         "Phase 7"),
        ("what happens if training fails?", "Phase 4"),
        ("when does marketing start?", "Phase 5"),
        ("how does the warning process work before termination?",
         "progressive warning"),
        ("what happens if EMI is not received?", "Accounting confirms"),
        ("when should an internal issue be escalated to management?",
         "Case resolution"),
    ):
        assert expected in brain.knowledge_for(question), question

    # "I-983" tokenises to NOTHING (_WORD_RE wants 2+ chars starting with a
    # letter), so retrieval cannot reach it. The glossary regex-matches the
    # raw question text, which is what saves the answer.
    assert "I-983" in brain.glossary_for("what is the I-983 process?")


#: Multi-word `keywords` that already shipped before the rule below was known.
#: A ratchet, not a target: the count may fall, never rise.
_DEAD_KEYWORDS_BASELINE = 25


def test_no_pack_gains_a_keyword_that_can_never_match(shipped_packs):
    """`knowledge_for` stems each keyword as a WHOLE STRING and matches it
    against the question's individual word tokens, so a multi-word keyword
    ("out of training", "phase 1") can never match anything — it is silently
    dead weight. Caught 2026-08-19: the CS Phase 5 chunk's "ready to start
    marketing" could not fire, so "when does marketing start?" was answered
    from the Phase 1 chunk.

    Six older packs carry 25 of these between them (qb-invoicing has 9). They
    are inert rather than wrong, and splitting them would move retrieval for
    questions nobody asked about here, so they are a ratcheted baseline: any
    NEW one fails, and the number can only go down."""
    dead = [
        (pack["name"], chunk["title"], keyword)
        for pack in shipped_packs.values()
        for chunk in pack["knowledge"]
        for keyword in chunk["keywords"]
        if " " in keyword.strip()
    ]
    # Packs written since the rule was known carry none at all.
    assert not [d for d in dead if d[0].startswith("cs-")], dead
    assert len(dead) <= _DEAD_KEYWORDS_BASELINE, (
        f"new dead keyword(s): {len(dead)} > {_DEAD_KEYWORDS_BASELINE}\n"
        + "\n".join(f"  {p} / {t!r}: {k!r}" for p, t, k in dead)
    )


def test_the_cs_packs_never_shadow_another_packs_field_notes(shipped_packs):
    """field_notes are global and last-write-wins in FILENAME order, and
    `cs-*` sorts before every other pack — so a note here would lose to the
    owning pack, and a note here for a field nobody else claims must be a
    deliberate addition, not an accident."""
    theirs = {
        (obj, field)
        for name, pack in shipped_packs.items() if not name.startswith("cs-")
        for obj, fields in pack["field_notes"].items()
        if isinstance(fields, dict) for field in fields
    }
    mine = {
        (obj, field)
        for name, pack in shipped_packs.items() if name.startswith("cs-")
        for obj, fields in pack["field_notes"].items()
        if isinstance(fields, dict) for field in fields
    }
    assert not (mine & theirs), f"shadowed field notes: {mine & theirs}"
    # The one deliberate addition: the Team Lead the SOP requires within 24h.
    assert mine == {("Account", "Assigned_Marketing_Team_Lead__c")}


def test_the_cs_packs_carry_the_verified_status_mappings(shipped_packs):
    """The SOPs say "change the Salesforce status to Service Agreement" while
    stating they cannot name the field. Verifying that against production is
    what made them worth ingesting — and it also found the spelling trap."""
    lifecycle = shipped_packs["cs-candidate-lifecycle"]["rules"]
    internal = shipped_packs["cs-internal-operations"]["rules"]

    # The SOP status ladder IS a real picklist.
    assert "'Welcome Call'" in internal and "'Service Agreement'" in internal
    assert "'Resume Creation'" in internal and "Onboarding__c.Status__c" in internal

    # The trap: the SOPs say "Terminated"; production stores 'Terminate'.
    for rules in (lifecycle, internal):
        assert "'Terminate'" in rules
        assert "Candidate_Status_Change_Reason__c" in rules

    # The lifecycle phases are separated by the Interview_Type__c lookup NAME,
    # not by anything on the record itself.
    assert "'Intake'" in lifecycle and "'OOT'" in lifecycle
    assert "Interview_Type__c.Id" in lifecycle
    # Phase 7's credential gate.
    assert "ACH_Authorization_Status__c" in lifecycle
