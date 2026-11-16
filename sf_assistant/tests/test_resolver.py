"""sf_assistant.knowledge.resolver, .knowledge.terms and .engine.vocabulary: the typo-tolerant term resolver
(docs/resolver.md).

Every vocabulary here is made up (objects, values and people); no real names or stored values.
"""

from __future__ import annotations

import ast
import json
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from sf_assistant.engine.vocabulary import SnapshotPolicy, build_vocabulary
from sf_assistant.knowledge import resolver as R
from sf_assistant.knowledge.resolver import Vocabulary, build
from sf_assistant.knowledge.terms import knowledge_resolver, knowledge_vocabulary

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "src" / "sf_assistant" / "engine"
KNOWLEDGE = ROOT / "src" / "sf_assistant" / "knowledge"


# ------------------------------------------------------------------------------------------------ fake vocabulary
def fake_vocab() -> Vocabulary:
    v = Vocabulary()
    objs = {
        "Account": ("Account", "Accounts"),
        "Gadget_Order__c": ("Gadget Order", "Gadget Orders"),
        "Gadget_Order_Line__c": ("Gadget Order Line", "Gadget Order Lines"),
        "Internal_Review__c": ("Internal Review", "Internal Reviews"),
        "Internal_Review_Question_Log__c": ("Internal Review Question Log", "Internal Review Question Logs"),
        "Internal_Review_Section_Log__c": ("Internal Review Section Log", "Internal Review Section Logs"),
        "Field_Visit__c": ("Field Visit", "Field Visits"),
        "Deliverable__c": ("Deliverable", "Deliverables"),
    }
    for api, (label, plural) in objs.items():
        v.add("object", api, api, api, "api")
        v.add("object", api, label, api, "label")
        v.add("object", api, plural, api, "plural")
    fields = [
        ("Gadget_Order__c", "Status__c", "Status"), ("Gadget_Order__c", "Delivery_Date__c", "Delivery Date"),
        ("Gadget_Order__c", "Support_Person__c", "Support Person"),
        ("Gadget_Order__c", "Readiness_Person__c", "Readiness Person"),
        ("Field_Visit__c", "Status__c", "Status"), ("Field_Visit__c", "Visit_Subject__c", "Visit Subject"),
        ("Account", "Niche__c", "Niche"), ("Gadget_Order_Line__c", "Gadget_Order__c", "Gadget Order"),
    ]
    for obj, api, label in fields:
        v.add("field", f"{obj}.{api}", api, obj, "api")
        v.add("field", f"{obj}.{api}", label, obj, "label")
    for obj, fld, vals in [("Gadget_Order__c", "Status__c", ["Scheduled", "Completed", "Cancelled"]),
                           ("Field_Visit__c", "Status__c", ["Scheduled", "Open"]),
                           ("Field_Visit__c", "Visit_Subject__c", ["Physics", "Chemistry"]),
                           ("Account", "Niche__c", ["AI ML Engineer", "Data Analyst"])]:
        for val in vals:
            v.add("value", f"{obj}.{fld}={val}", val, obj, "picklist")
    v.add("value", "Gadget_Order__c.Status__c=Scheduled", "Scheduled", "Gadget_Order__c", "stored", 40)
    v.add("value", "Field_Visit__c.Status__c=Scheduled", "Scheduled", "Field_Visit__c", "stored", 3)
    v.add("record_type", "Gadget_Order__c.RecordType=Bulk_Order", "Bulk_Order", "Gadget_Order__c", "api")
    v.add("record_type", "Gadget_Order__c.RecordType=Bulk_Order", "Bulk Order", "Gadget_Order__c", "label")
    v.add("component", "flow:Gadget_Order_Reminder_Flow", "Gadget_Order_Reminder_Flow", "Gadget_Order__c", "flow")
    v.add("component", "apex:GadgetOrderRouter", "GadgetOrderRouter", "Gadget_Order__c", "apex")
    people = [("Zorvan Quillby", "Employee__c/e1"), ("Zorvan Quillby", "Account/a1"), ("Mirela Tuskova", "Lead/l1"),
              ("Davor Quell", "Employee__c/e2"), ("Divor Paskel", "Employee__c/e3"), ("Talvin Orrek", "Lead/l2"),
              ("Telvin Brask", "Lead/l3"), ("Orlen Vashti", "Account/a2"), ("Kestra Moon", "Account/a3")]
    for name, ref in people:
        obj = ref.split("/")[0]
        ws = name.split()
        v.add("person", f"person:{ref}", name, obj, "full")
        v.add("person", f"person:{ref}", ws[0], obj, "first")
        v.add("person", f"person:{ref}", ws[-1], obj, "last")
    # the knowledge text: correct words that are never corrected and never suggested
    v.add_known("please describe how the handler works and who can override the placeholder value of a "
                "responsible reviewer when the delivery is late".split())
    # a few words of a generic English word list (the real one is /usr/share/dict or a vendored list)
    v.add_known(FAKE_LEXICON)
    return v


#: Generic English words standing in for the English word list (no names, no stored values).
FAKE_LEXICON = ("assigned scheduled scheduling complete completed incomplete verified trainee trainer male gender "
                "sender order orders shows show told run runs created list section session season employer "
                "employee").split()


@pytest.fixture(scope="module")
def r() -> R.Resolver:
    return build(fake_vocab())


def res(r: R.Resolver, text: str, **kw) -> tuple[str, str | None, str | None]:
    out = r.resolve_term(text, **kw)
    return out.decision, out.suggestion, out.tier


# ------------------------------------------------------------------------------------------------------- tiers
@pytest.mark.parametrize("text", ["gadget order", "Gadget Orders", "GADGET_ORDER__C", "Gadget_Order__c", "GadgetOrder",
                                  "gadget order's", "Gàdget  Order!", "gadgetorder"])
def test_exact_tier_normalises_case_accents_punctuation_api_camel_plural_possessive(r, text):
    out = r.resolve_term(text, kinds=["object"])
    assert out.decision == "exact" and out.tier == "exact"
    assert out.candidates[0].target == "Gadget_Order__c"


def test_exact_api_input_gets_the_api_spelling(r):
    assert r.resolve_term("gadget_order__c", kinds=["object"]).suggestion == "Gadget_Order__c"
    assert r.resolve_term("gadget order", kinds=["object"]).suggestion == "Gadget Order"


def test_normalized_tier_collapses_doubled_letters(r):
    assert res(r, "zorrvan") == ("corrected", "Zorvan", "normalized")
    assert res(r, "zorvvan quilllby") == ("corrected", "Zorvan Quillby", "normalized")
    assert res(r, "Delivverable", kinds=["object"])[1] == "Deliverable"


def test_a_number_is_never_read_as_another_number():
    """The doubled-letter tier keeps digits ('0155pm' is no '015pm'), and no tier corrects a text into one with other
    numbers: '01:55 PM' is another time than '01:15 PM', '1:30 PM' than '11:30 PM', 'H11B' another class than 'H1B'.
    Doubled letters around the numbers are still read ('Rooundd 2' -> 'Round 2')."""
    v = Vocabulary()
    for val in ("01:15 PM", "11:30 PM", "03:30 PM", "H1B", "Round 2", "Completed"):
        v.add("value", f"Obj__c.Slot__c={val}", val, "Obj__c", "picklist")
    v.add_known(FAKE_LEXICON)
    rr = build(v)
    for typed in ("01:55 PM", "1:30 PM", "0:30 PM", "H11B", "Round 3", "Rond"):
        out = rr.resolve_term(typed, kinds=["value"], within="Obj__c.Slot__c=")
        assert out.decision != "corrected", (typed, out.decision, out.suggestion, out.tier)
    assert res(rr, "Rooundd 2", kinds=["value"])[:2] == ("corrected", "Round 2")
    assert res(rr, "Compleeted", kinds=["value"])[:2] == ("corrected", "Completed")
    assert R.numbers("09:30 AM") == R.numbers("9:30 AM") == (9, 30) and R.numbers("H1B") == (1,)
    assert R.dedup_letters("h11bb") == "h11b" and R.dedup("1100") == "10"


def test_romanized_hindi_question_words_are_known_words(tmp_path):
    """A Hinglish question: 'kaunse' (which) is a correct word, never read as the stored value 'Kansas'; the spoken
    words come with every English word list (knowledge/spoken_words.txt), never without one."""
    from sf_assistant.knowledge.terms import SPOKEN_WORDS_PATH, add_lexicon

    words = tmp_path / "words.txt"
    words.write_text("\n".join(FAKE_LEXICON + ["candidates", "interviews", "which"]) + "\n", encoding="utf-8")
    v = Vocabulary()
    v.add("value", "Account.State__c=Kansas", "Kansas", "Account", "picklist")
    v.add("value", "Account.State__c=Maine", "Maine", "Account", "picklist")
    bare = Vocabulary(list(v.terms))
    add_lexicon(bare, [str(words)])
    bare.known_words -= {w.strip() for w in SPOKEN_WORDS_PATH.read_text().splitlines() if w and " " not in w}
    q = "kaunse candidates ke interviews scheduled hai, is mahine"
    assert {c.text for c in build(bare).spell_correct(q)} >= {"kaunse"}       # what the review found
    add_lexicon(v, [str(words)])
    assert {"kaunse", "kitne", "mahine"} <= v.known_words and v.info["lexicon"]["spoken_words"] > 100
    assert build(v).spell_correct(q) == []
    none = Vocabulary()
    add_lexicon(none, None)
    assert "kaunse" not in none.known_words and none.info["lexicon"]["spoken_words"] == 0
    assert len(v.info["lexicon"]["sha"]) == 12


def test_text_fields_named_for_people_never_give_stored_values():
    """A vendor's, trainer's, owner's or contact's name field holds people's names: like the candidate and recruiter
    name fields, it gives no value terms, so a filter never binds a misspelt person through it."""
    from sf_assistant.engine.vocabulary import _FREE_TEXT_NAME_RE

    for api in ("Vendor_Name__c", "Trainer_Name__c", "Assigned_Trainer_Name__c", "Candidate_Owner_Name__c",
                "Contact_Name__c", "Manager_Name__c", "POC_Name__c", "SPOC_Name__c", "Candidate_Name__c"):
        assert _FREE_TEXT_NAME_RE.search(api), api
    for api in ("Company_Name__c", "Program_Name__c", "Niche__c", "Status__c", "Visa_Status__c"):
        assert not _FREE_TEXT_NAME_RE.search(api), api


def test_the_knowledge_and_word_list_part_is_read_once_per_folder(tmp_path, monkeypatch):
    """A record session reopened on the same knowledge folder and word lists reuses the knowledge part (no second
    read; the same vocabulary); a changed file is read again."""
    from sf_assistant.knowledge import terms as T

    kn = tmp_path / "kn"
    kn.mkdir()
    doc = {"object": {"api_name": "Gadget__c", "label": "Gadget"}, "fields": [{"api_name": "Colour__c", "label": "Colour"}]}
    (kn / "Gadget__c.json").write_text(json.dumps(doc), encoding="utf-8")
    words = tmp_path / "words.txt"
    words.write_text("gadget colour\n", encoding="utf-8")
    monkeypatch.setattr(T, "_START_CACHE", {})
    reads: list[str] = []
    real = T.load_knowledge
    monkeypatch.setattr(T, "load_knowledge", lambda d: (reads.append(str(d)), real(d))[1])
    a = T.knowledge_vocabulary(kn, [str(words)])
    b = T.knowledge_vocabulary(kn, [str(words)])
    assert len(reads) == 1 and a.terms == b.terms and a.known_words == b.known_words and a.terms is not b.terms
    b.terms.append(b.terms[0])
    assert len(T.knowledge_vocabulary(kn, [str(words)]).terms) == len(a.terms)      # a copy: the cache is unchanged
    doc["fields"].append({"api_name": "Size__c", "label": "Size"})
    (kn / "Gadget__c.json").write_text(json.dumps(doc), encoding="utf-8")
    c = T.knowledge_vocabulary(kn, [str(words)])
    assert len(reads) == 2 and len(c.terms) == len(a.terms) + 2
    T.knowledge_vocabulary([doc], [str(words)])                                      # parsed documents: no cache
    assert len(reads) == 2 and len(T._START_CACHE) <= T._START_CACHE_MAX


def test_sound_tier(r):
    d, s, tier = res(r, "fysics", kinds=["value"])            # ph/f: two edits apart, one sound
    assert (d, s, tier) == ("corrected", "Physics", "sound")
    d, s, tier = res(r, "zorwan", kinds=["person"])           # w/v
    assert (d, s) == ("corrected", "Zorvan") and tier == "sound"


def test_sound_match_beyond_the_edit_limit_only_when_it_sounds_the_same():
    v = Vocabulary()
    v.add("person", "person:Lead/s1", "Zalakshi", "Lead", "first")
    v.add("person", "person:Lead/s2", "Calviese", "Lead", "last")
    r2 = build(v)
    assert R.osa_distance("zalaxi", "zalakshi") == 3 and R.phonetic("zalaxi") == R.phonetic("zalakshi")
    assert res(r2, "zalaxi") == ("corrected", "Zalakshi", "sound")
    # same consonant skeleton (klvs), three edits, and it does not sound the same: no match at all
    assert R.sound_key("kalvish") == R.sound_key("calviese") and R.osa_distance("kalvish", "calviese") == 3
    assert r2.resolve_term("kalvish").decision == "none"


def test_edit_tier_one_edit_for_short_two_for_long(r):
    assert res(r, "Completd", kinds=["value"])[:2] == ("corrected", "Completed")
    assert res(r, "Cmopleted", kinds=["value"])[:2] == ("corrected", "Completed")     # adjacent swap
    assert res(r, "Delievrable", kinds=["object"])[:2] == ("corrected", "Deliverable")  # two edits, 11 letters
    assert r.resolve_term("Ospn", kinds=["value"]).decision == "none"                   # two edits, 4 letters


def test_phrase_tier_missing_word_is_ambiguous_between_the_terms_that_have_it(r):
    out = r.resolve_term("intrnal reveiw logs", kinds=["object"])
    assert out.decision == "ambiguous" and out.tier == "phrase"
    assert set(out.suggestions) == {"Internal Review Question Log", "Internal Review Section Log"}


def test_phrase_tier_reordered_and_anchored_short_word(r):
    assert res(r, "order gadget", kinds=["object"])[:2] == ("corrected", "Gadget Order")
    # "lne" has 3 letters: corrected only because the other words of the term anchor it
    assert res(r, "gadget order lne", kinds=["object"])[:2] == ("corrected", "Gadget Order Line")
    assert r.resolve_term("lne", kinds=["object"]).decision == "none"


def test_multi_word_typos_in_two_words(r):
    assert res(r, "backgrond", kinds=["object"])[0] == "none"            # not in this vocabulary
    assert res(r, "Delivary Dat", kinds=["field"])[:2] == ("corrected", "Delivery Date")
    assert res(r, "AI ML Enginer", kinds=["value"])[:2] == ("corrected", "AI ML Engineer")


# ------------------------------------------------------------------------------------------------ guard rails
@pytest.mark.parametrize("text,why", [("2026", "number"), ("1,250.50", "number"), ("2026-10-13", "date"),
                                      ("13/10/2026", "date"), ("001Ab00000XyZ12AAA", "id"),
                                      ("someone@example.com", "email or url"),
                                      ("https://example.com/x", "email or url")])
def test_numbers_dates_ids_and_addresses_are_never_corrected(r, text, why):
    out = r.resolve_term(text)
    assert out.decision == "none" and out.reason == f"protected: {why}"


def test_short_words_are_never_corrected(r):
    for text in ("zor", "dvr", "qu", "ord"):
        assert r.resolve_term(text).decision == "none"
    assert r.spell_correct("who is zor") == []


def test_correct_words_stay_unchanged_in_questions(r):
    questions = [
        "please describe how the handler works and who can override the placeholder",
        "which handlers run when an order is unassigned or rescheduled?",      # suffix / prefix of known words
        "how many gadget orders were completed last month?",
        "what does the support person field on a gadget order mean",
        "is the responsible reviewer told when the delivery is late",
        "list orders created on 2026-10-13 by someone@example.com with id 001Ab00000XyZ12AAA",
    ]
    for q in questions:
        assert r.spell_correct(q) == [], q


def test_only_vocabulary_is_ever_suggested(r):
    vocab_texts = {t.text for t in r.terms}
    for q in ["what is a delivrable", "who is zorvn quilby", "how many Completd orders", "intrnal reveiw logs",
              "gadjet ordr status"]:
        for c in r.spell_correct(q):
            assert all(s in vocab_texts for s in c.suggestions), (q, c)


def test_a_word_close_to_many_words_only_loosely_is_not_corrected():
    v = Vocabulary()
    for i, name in enumerate(["barov", "berov", "birov", "borov", "burov", "byrov", "barev", "barax"]):
        v.add("person", f"person:Lead/x{i}", name, "Lead", "first")
    r2 = build(v)
    out = r2.resolve_term("bxrov")
    assert out.decision in ("ambiguous", "none")
    assert all(c.decision != "corrected" for c in r2.spell_correct("who is bxrov"))


def test_known_word_is_not_corrected_but_a_vocabulary_typo_is(r):
    assert r.is_known_word("placeholder") and r.is_known_word("handlers") and r.is_known_word("unassigned")
    assert r.is_known_word("incomplete") and r.is_known_word("uncompleted")     # in- / un- + a known word
    assert not r.is_known_word("completd") and not r.is_known_word("enginer")


def test_english_words_are_never_corrected(r):
    """Words of the English word list are correct: never taken for typos of vocabulary (``gender`` is no ``sender``,
    ``male`` no person name), in questions or as tool arguments."""
    v = fake_vocab()
    v.add("field", "Gadget_Order__c.Sender__c", "Sender", "Gadget_Order__c", "label")
    v.add("person", "person:Lead/m1", "Malen", "Lead", "first")
    with_list = build(v)
    assert with_list.spell_correct("show the gender breakdown of male candidates") == []
    v.known_words -= {"gender", "male"}
    without = build(v)
    assert {c.text for c in without.spell_correct("show the gender breakdown of male candidates")} >= {"gender"}


def test_a_correct_word_or_an_affix_is_a_did_you_mean_never_a_correction(r):
    """Tool mode: a typed word that is itself a word (``Incomplete``, ``Trainee``, ``Employer``) or differs by a whole
    affix (``Uncompleted``) is never bound to the other value: ambiguous, with the value as suggestion."""
    v = fake_vocab()
    v.add("value", "Gadget_Order__c.Role__c=Trainer", "Trainer", "Gadget_Order__c", "picklist")
    v.add("value", "Gadget_Order__c.Role__c=Employee", "Employee", "Gadget_Order__c", "picklist")
    v.add("value", "Gadget_Order__c.State__c=Complete", "Complete", "Gadget_Order__c", "picklist")
    r2 = build(v)
    for typed, want in [("Uncompleted", "Completed"), ("Incomplete", "Complete"), ("Trainee", "Trainer"),
                        ("Employer", "Employee")]:
        out = r2.resolve_term(typed, kinds=["value"])
        assert out.decision == "ambiguous" and out.suggestion is None and out.suggestions[0] == want, typed
        assert out.reason.startswith("not corrected")
    assert R._affix_variant("uncompleted", "completed") and R._affix_variant("trainee", "trainer")
    assert r2.resolve_term("Trainr", kinds=["value"]).suggestion == "Trainer"        # a real typo still is


def test_person_names_are_not_singularised():
    """``Kelm`` and ``Kelms`` are two names: neither is an exact match for the other (metadata plurals still are)."""
    v = Vocabulary()
    v.add("person", "person:Lead/k1", "Kelm", "Lead", "first")
    v.add("person", "person:Lead/k2", "Kelms", "Lead", "last")
    v.add("object", "Ledger__c", "Ledger", "Ledger__c", "label")
    r2 = build(v)
    one, two = r2.resolve_term("Kelm", kinds=["person"]), r2.resolve_term("Kelms", kinds=["person"])
    assert one.decision == "exact" and one.targets == ["person:Lead/k1"]
    assert two.decision == "exact" and two.targets == ["person:Lead/k2"]
    assert r2.resolve_term("ledgers").decision == "exact"
    assert R.singular("niches", {"niche"}) == "niche" and R.singular("niches") == "nich"
    assert R.singular("statuses", {"status"}) == "status" and R.singular("categories", {"category"}) == "category"


def test_a_typo_of_a_plural_is_compared_as_typed_too(r):
    out = r.resolve_term("delivrables", kinds=["object"])       # singular() alone would give "delivrable"
    assert out.decision == "corrected" and out.suggestion == "Deliverable"
    out = r.resolve_term("reveiws", kinds=["object"])
    assert out.decision != "corrected" or "Review" in out.suggestion


def test_one_single_name_is_never_turned_into_another():
    """A name 1 edit from a stored one may be someone else (absent from the data): a did-you-mean. Doubled letters
    and identical sounds are corrected; a full name whose every word aligns is corrected; a changed first sound
    never is."""
    v = Vocabulary()
    for i, (first, last) in enumerate([("Varnik", "Oslo"), ("Kwina", "Tarvel"), ("Davor", "Quell")]):
        v.add("person", f"person:Lead/n{i}", f"{first} {last}", "Lead", "full")
        v.add("person", f"person:Lead/n{i}", first, "Lead", "first")
        v.add("person", f"person:Lead/n{i}", last, "Lead", "last")
    r2 = build(v)
    out = r2.resolve_term("Varnek", kinds=["person"])
    assert out.decision == "ambiguous" and out.suggestions[0] == "Varnik" and out.reason == "not corrected: single name"
    assert r2.resolve_term("Varrnik", kinds=["person"]).decision == "corrected"      # doubled letter
    assert r2.resolve_term("Kweena", kinds=["person"]).suggestion == "Kwina"         # ee = i: same sound
    assert r2.resolve_term("varnek oslo", kinds=["person"]).decision == "corrected"  # the surname settles it
    out = r2.resolve_term("barnik oslo", kinds=["person"])
    assert out.decision == "ambiguous" and out.reason == "not corrected: first sound changed"
    assert [c.decision for c in r2.spell_correct("who is Varnek")] == ["ambiguous"]


def test_missing_or_extra_words_are_a_did_you_mean(r):
    out = r.resolve_term("Order Line", kinds=["object"])
    assert out.decision == "ambiguous" and out.suggestions[0] == "Gadget Order Line"
    assert out.missing_words == ("gadget",) and out.reason == "not corrected: words missing"
    assert r.resolve_term("Delivery", kinds=["field"]).decision != "corrected"


def test_filler_words_around_an_argument_are_ignored(r):
    out = r.resolve_term("gadget order records", kinds=["object"])
    assert (out.decision, out.suggestion, out.extra_words) == ("exact", "Gadget Order", ("records",))
    out = r.resolve_term("the gadjet order", kinds=["object"])
    assert out.decision == "corrected" and out.suggestion == "Gadget Order" and "the" in out.extra_words
    out = r.resolve_term("Mr. Zorvvan", kinds=["person"])
    assert out.decision == "corrected" and out.suggestion == "Zorvan"


def test_a_negation_is_never_dropped():
    v = Vocabulary()
    v.add("value", "Obj__c.Stage__c=Not Started", "Not Started", "Obj__c", "picklist")
    v.add("value", "Obj__c.Stage__c=Started", "Started", "Obj__c", "picklist")
    r2 = build(v)
    assert r2.resolve_term("not startd", kinds=["value"]).suggestion == "Not Started"
    assert r2.resolve_term("Not Started", kinds=["value"]).targets == ["Obj__c.Stage__c=Not Started"]


def test_words_written_together_are_exact(r):
    for text in ("GadgetOrders", "gadgetorder", "InternalReviews", "Gadget Order Line"):
        assert r.resolve_term(text, kinds=["object"]).decision == "exact", text
    assert r.spell_correct("how many InternalReviews happened") == []


def test_a_runner_up_with_fewer_edits_ties_a_sound_match():
    v = Vocabulary()
    for api, label in [("Session__c", "Session"), ("Section__c", "Section")]:
        v.add("object", api, label, api, "label")
    out = build(v).resolve_term("secion", kinds=["object"])
    assert out.decision == "ambiguous" and set(out.suggestions) == {"Session", "Section"}


def test_slip_cost_prices_likely_slips_lower():
    """The noisy-channel cost: a letter left out or two letters swapped are cheap, any other letter is dear, a slip on
    the first letter costs a little more, and a word written as it sounds costs one slip."""
    c = R.slip_cost
    assert c("velora", "velora") == 0.0
    assert c("velora", "velra") == R.SLIP_COSTS["del_other"] < c("velora", "velxra") == R.SLIP_COSTS["sub_other"]
    assert c("velora", "vlerora") > c("velora", "vleora") == R.SLIP_COSTS["swap"]
    assert c("kavelo", "avelo") == R.SLIP_COSTS["del_other"] + R.SLIP_COSTS["first"] > c("kavelo", "kavel")
    assert c("kelvo", "celvo") == R.SLIP_COSTS["sub_sound"] + R.SLIP_COSTS["first"] < c("kelvo", "xelvo")
    assert c("zorrvan", "zorvan") == R.SLIP_COSTS["del_double"] < c("zorvan", "zorvvan")
    assert c("phalor", "falor") == c("session", "secion") == c("zorvan", "zorvvan") == R.SOUND_SPELLING_COST
    assert c("phalor", "falor") < R.SLIP_COSTS["spelling"] + R.SLIP_COSTS["first"]       # same phonetic form
    assert R.slip_conf(2.2) > R.slip_conf(4.4) and R.wide_edits("vshte", "vashti") == 2 == R.max_edits("vshte",
                                                                                                         "vashti") + 1


def test_tool_lookup_ranks_close_names_by_their_slips():
    """Two stored names one edit apart from the typed one: the likelier slip comes first (a swap of the last two
    letters over a letter added), whatever their Jaro-Winkler order."""
    v = Vocabulary()
    for name, ref in [("Quenra Torvels", "Lead/t1"), ("Ivral Torvel", "Lead/t2")]:
        v.add("person", f"person:{ref}", name, "Lead", "full")
        v.add("person", f"person:{ref}", name.split()[-1], "Lead", "last")
    r2 = build(v)
    out = r2.resolve_term("Torvesl", kinds=["person"])
    assert out.decision == "ambiguous" and out.suggestions[0] == "Torvels"    # a did-you-mean: single name
    assert r2.resolve_term("Torvesl", kinds=["person"], wide=False).suggestions[0] == "Torvel"


def test_tool_lookup_lists_distant_spellings_as_did_you_means_only(r):
    """Two slips in a 5-letter word, a first-letter slip the Jaro-Winkler floor drops, two letters left out: a tool
    lookup lists the term as a did-you-mean (never `corrected`); the strict lookup and spell_correct stay silent."""
    for typed, kinds, want in [("Vshte", ["person"], "Vashti"), ("Prlen", ["person"], "Orlen"),
                               ("Phscs", ["value"], "Physics"), ("Phscs", None, "Physics")]:
        out = r.resolve_term(typed, kinds=kinds)
        assert out.decision == "ambiguous" and out.suggestion is None and out.suggestions[0] == want, typed
        assert out.reason.startswith("not corrected")
        assert r.resolve_term(typed, kinds=kinds, wide=False).decision == "none", typed
    assert r.spell_correct("who is vshte or prlen") == [] and r.spell_correct("show phscs visits") == []
    assert all(m.wide for m in r.word_candidates("vshte", ["person"], wide=True) if m.word == "vashti")


def test_second_sound_key_finds_but_never_decides():
    v = Vocabulary()
    v.add("person", "person:Lead/b1", "Vijalo", "Lead", "first")
    out = build(v).resolve_term("Bijalo", kinds=["person"])
    assert out.decision == "ambiguous" and out.suggestions == ("Vijalo",)
    assert R.sound_key("rowvan") == R.sound_key("rouvan") and R.split_words("Søren Straße") == ["soren", "strasse"]


def test_context_is_matched_on_a_name_boundary(r):
    assert r._in_context("Obj__c", "Obj__c.Name", "Obj__c.Name") and r._in_context(None, "Obj__c.Name=X", "Obj__c.Name")
    assert not r._in_context("Obj__c", "Obj__c.Name_Link__c", "Obj__c.Name")
    assert not r._in_context("Obj2__c", "Obj2__c", "Obj")


def test_long_tokens_and_long_texts_are_cheap(r):
    t0 = time.perf_counter()
    r.spell_correct("who is " + "q" * 1000)
    r.spell_correct("who is " + "".join(random.Random(1).choice("abcdefghij") for _ in range(1000)))
    assert time.perf_counter() - t0 < 0.05
    assert r.resolve_term(" ".join(["gadget"] * 300)).reason == "too many words"


def test_a_word_of_multi_word_terms_only_lists_those_terms(r):
    out = r.spell_correct("what does persn mean here")
    assert [(c.text, c.decision) for c in out] == [("persn", "ambiguous")]
    assert set(out[0].suggestions) == {"Readiness Person", "Support Person"}


def test_a_short_word_with_another_sound_is_not_corrected_in_questions():
    v = Vocabulary()
    v.add("value", "Obj__c.Kind__c=Check", "Check", "Obj__c", "picklist")
    out = build(v).spell_correct("what is a chech")
    assert [(c.text, c.decision) for c in out] == [("chech", "ambiguous")]


def test_stopwords_are_never_suggested():
    v = Vocabulary()
    v.add("field", "Obj__c.Check_With_Lead__c", "Check With Lead", "Obj__c", "label")
    r2 = build(v)
    assert all(m.word != "with" for m in r2.word_candidates("whit"))


def test_a_rare_stored_typo_does_not_hide_the_same_typo_in_a_question():
    v = Vocabulary()
    v.add("object", "Deliverable__c", "Deliverable", "Deliverable__c", "label")
    v.add("value", "Deliverable__c.Name=Deliverble Plan", "Deliverble Plan", "Deliverable__c", "stored_text", 2)
    v.add("value", "Deliverable__c.Name=Zentrix", "Zentrix", "Deliverable__c", "stored_text", 2)
    r2 = build(v)
    out = r2.spell_correct("what is a deliverble")
    assert [(c.text, c.decision, c.suggestions) for c in out] == [("deliverble", "ambiguous",
                                                                   ("Deliverable", "Deliverble Plan"))]
    assert r2.spell_correct("what is zentrix") == []                 # a stored word with no correct word near it
    assert r2.resolve_term("Deliverble Plan", kinds=["value"]).decision == "exact"    # still a target


def test_run_is_extended_when_the_next_word_belongs_to_longer_terms(r):
    out = r.spell_correct("what are intrnal reveiw logs")
    assert [(c.text, c.decision) for c in out] == [("intrnal reveiw logs", "ambiguous")]
    assert set(out[0].suggestions) == {"Internal Review Question Log", "Internal Review Section Log"}


def test_a_surname_that_is_someone_elses_is_checked_as_a_full_name():
    v = Vocabulary()
    for i, name in enumerate(["Mirela Tuskova", "Orlen Tuskovo", "Orlen Govardi Vashti"]):
        ws = name.split()
        v.add("person", f"person:Lead/t{i}", name, "Lead", "full")
        v.add("person", f"person:Lead/t{i}", ws[0], "Lead", "first")
        v.add("person", f"person:Lead/t{i}", ws[-1], "Lead", "last")
    r2 = build(v)
    out = r2.spell_correct("who is mirela tuskovo")       # both words stored, not as one person
    assert [(c.text, c.decision) for c in out] == [("mirela tuskovo", "ambiguous")]
    assert out[0].suggestions[0] == "Mirela Tuskova"
    assert r2.spell_correct("who is orlen govardi vashti") == []
    assert r2.spell_correct("what did orlen govardi do") == []                    # words of one stored name


# --------------------------------------------------------------------------------------------- ambiguity
def test_two_names_equally_close_are_ambiguous(r):
    out = r.resolve_term("tilvin", kinds=["person"])
    assert out.decision == "ambiguous"
    assert set(out.suggestions[:2]) == {"Talvin", "Telvin"}
    assert out.suggestion is None


def test_doubled_letter_tie_is_ambiguous():
    v = Vocabulary()
    v.add("person", "person:Lead/x1", "Vorun", "Lead", "first")
    v.add("person", "person:Lead/x2", "Vorrun", "Lead", "first")
    v.add("person", "person:Lead/x3", "Voruun", "Lead", "first")
    out = build(v).resolve_term("vorruun")
    assert out.decision == "ambiguous" and set(out.suggestions) == {"Vorrun", "Voruun"}


def test_full_name_decides_between_close_first_names(r):
    alone = r.resolve_term("dvor", kinds=["person"])
    assert alone.decision == "ambiguous" and {"Davor", "Divor"} <= set(alone.suggestions)
    out = r.spell_correct("who is dvor quell")
    assert [(c.text, c.decision, c.suggestion) for c in out] == [("dvor quell", "corrected", "Davor Quell")]
    assert out[0].candidates[0].target == "person:Employee__c/e2"


def test_weak_match_is_none_with_the_closest_names(r):
    out = r.resolve_term("persn", kinds=["field"])        # a word of two field labels, no field on its own
    assert out.decision == "none" and out.suggestion is None and out.reason == "weak match only"
    assert set(out.suggestions) == {"Readiness Person", "Support Person"}
    assert r.resolve_term("intrvw persn", kinds=["field"]).suggestions == ()


# ---------------------------------------------------------------------------------------------- context, kinds
def test_context_object_ranks_its_terms_first(r):
    plain = r.resolve_term("status", kinds=["field"])
    assert plain.decision == "exact" and len(plain.candidates) == 2
    ctx = r.resolve_term("status", kinds=["field"], context_object="Field_Visit__c")
    assert ctx.candidates[0].target == "Field_Visit__c.Status__c"
    ctx = r.resolve_term("Sheduled", kinds=["value"], context_object="Field_Visit__c")
    assert ctx.decision == "corrected" and ctx.candidates[0].target == "Field_Visit__c.Status__c=Scheduled"


def test_stored_record_counts_order_targets_of_one_spelling(r):
    out = r.resolve_term("Scheduled", kinds=["value"])
    assert out.candidates[0].target == "Gadget_Order__c.Status__c=Scheduled"   # 40 records vs 3


def test_within_limits_targets_to_one_field(r):
    out = r.resolve_term("Opne", kinds=["value"], within="Gadget_Order__c.Status__c=")
    assert out.decision == "none"
    out = r.resolve_term("Opne", kinds=["value"], within="Field_Visit__c.Status__c=")
    assert out.decision == "corrected" and out.suggestion == "Open"


def test_kinds_filter(r):
    assert r.resolve_term("zorvan", kinds=["object"]).decision == "none"
    out = r.resolve_term("zorvan", kinds=["person"])
    assert out.decision == "exact" and {c.target for c in out.candidates} == {"person:Employee__c/e1",
                                                                             "person:Account/a1"}
    with pytest.raises(ValueError):
        r.resolve_term("x", kinds=["nonsense"])


# ------------------------------------------------------------------------------------------------ spell_correct
def test_spell_correct_spans_and_owner_style_questions(r):
    q = "how does the gadjet ordr remindr flow work for Completd orders"
    out = r.spell_correct(q)
    assert [q[c.start:c.end] for c in out] == [c.text for c in out]
    got = {c.text: (c.decision, c.suggestion) for c in out}
    assert got == {"gadjet ordr remindr flow": ("corrected", "Gadget_Order_Reminder_Flow"),
                   "Completd": ("corrected", "Completed")}
    assert r.spell_correct("who is zorrvan quillby")[0].suggestion == "Zorvan Quillby"
    assert r.spell_correct("what is a delivrable")[0].suggestion == "Deliverable"
    assert r.spell_correct("candidates in AI ML Enginer niche")[0].suggestion == "AI ML Engineer"


def test_spell_correct_api_like_token(r):
    out = r.spell_correct("what sets Gadget_Ordr__c.Status__c")
    assert out and out[0].text == "Gadget_Ordr__c" and out[0].suggestion == "Gadget_Order__c"


def test_spell_correct_possessive_and_plural(r):
    out = r.spell_correct("zorvvan's gadjet orders")
    assert [(c.text, c.decision, c.suggestion) for c in out] == [("zorvvan's", "corrected", "Zorvan"),
                                                                 ("gadjet orders", "corrected", "Gadget Order")]


def test_include_exact_lists_correct_terms_too(r):
    out = r.spell_correct("how many gadget orders does zorvan quillby have", include_exact=True)
    exact = {c.text: c for c in out if c.decision == "exact"}
    assert exact["gadget orders"].candidates[0].target == "Gadget_Order__c"
    assert exact["zorvan quillby"].kinds == ("person",)


def test_results_are_json_serialisable(r):
    json.dumps(r.resolve_term("Completd").as_dict())
    json.dumps([c.as_dict() for c in r.spell_correct("intrnal reveiw logs of dvor quell")])


# --------------------------------------------------------------------------------------- determinism, threads
def _answers(r: R.Resolver) -> list:
    qs = ["Completd", "tilvin", "intrnal reveiw logs", "dvor quell", "Delivary Dat", "status", "fysics", "Scheduled",
          "Sheduled"]
    return [r.resolve_term(q).as_dict() for q in qs] + [[c.as_dict() for c in r.spell_correct(
        "who is dvor quell and what is a delivrable")]]


def test_deterministic_whatever_the_term_order(r):
    v = fake_vocab()
    random.Random(3).shuffle(v.terms)
    assert _answers(build(v)) == _answers(r)
    assert _answers(build(fake_vocab())) == _answers(r)
    v.terms.reverse()
    sources = {c.source for c in build(v).resolve_term("Scheduled", kinds=["value"]).candidates
               if c.target == "Gadget_Order__c.Status__c=Scheduled"}
    assert sources == {"picklist"}           # the same text from the picklist and from stored data: picklist wins


def test_deterministic_across_hash_seeds():
    code = ("import json, sys; sys.path.insert(0, %r); import test_resolver as t; from sf_assistant.knowledge.resolver "
            "import build; print(json.dumps(t._answers(build(t.fake_vocab()))))" % str(ROOT / "tests"))
    outs = {subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                           env={"PYTHONHASHSEED": seed, "PATH": ""}).stdout for seed in ("0", "1", "4242")}
    assert len(outs) == 1


def test_thread_safe_read_only_lookups(r):
    want = _answers(r)
    barrier = threading.Barrier(8)

    def work(_):
        barrier.wait()
        return [_answers(r) for _ in range(5)]
    with ThreadPoolExecutor(8) as ex:
        for got in ex.map(work, range(8)):
            assert all(g == want for g in got)


# ------------------------------------------------------------------------------------------------------- speed
def _synthetic(n_people: int = 6000, n_fields: int = 3000, seed: int = 5) -> Vocabulary:
    rng = random.Random(seed)
    syll = ["ka", "ri", "zo", "ven", "ta", "lo", "mi", "dra", "su", "pek", "nor", "bel", "ash", "qui", "tor", "ela"]

    def word(k: int) -> str:
        return "".join(rng.choice(syll) for _ in range(k))
    v = Vocabulary()
    for i in range(n_fields):
        obj = f"Obj{i % 60}__c"
        label = " ".join(word(rng.randint(2, 3)) for _ in range(rng.randint(1, 3)))
        v.add("field", f"{obj}.F{i}__c", label, obj, "label")
        if i % 3 == 0:
            v.add("value", f"{obj}.F{i}__c={word(2)}", word(2).title(), obj, "picklist")
    for i in range(n_people):
        name = f"{word(2).title()} {word(rng.randint(2, 3)).title()}"
        v.add("person", f"person:Lead/p{i}", name, "Lead", "full")
        v.add("person", f"person:Lead/p{i}", name.split()[0], "Lead", "first")
        v.add("person", f"person:Lead/p{i}", name.split()[1], "Lead", "last")
    return v


def test_build_and_lookup_are_fast():
    v = _synthetic()
    assert len(v.terms) > 20_000
    t0 = time.perf_counter()
    r2 = build(v)
    assert time.perf_counter() - t0 < 2.0
    rng = random.Random(9)
    texts = [t.text for t in r2.terms]
    times = []
    for _ in range(300):
        w = rng.choice(texts).split()[0].lower()
        i = rng.randrange(1, len(w))
        typo = w[:i] + w[i + 1:] if rng.random() < 0.5 else w[:i] + w[i] + w[i:]
        t0 = time.perf_counter()
        r2.resolve_term(typo)
        times.append(time.perf_counter() - t0)
    assert sum(times) / len(times) < 0.005
    assert max(times) < 0.05


# ------------------------------------------------------------------------------------------------- vocabulary
def _knowledge(tmp: Path) -> Path:
    kdir = tmp / "knowledge"
    kdir.mkdir()
    doc = {
        "object": {"api_name": "Gadget_Order__c", "label": "Gadget Order", "plural_label": "Gadget Orders",
                   "description": "An order for gadgets; the router handles overrides."},
        "record_types": [{"developer_name": "Bulk_Order", "label": "Bulk Order"}],
        "fields": [
            {"api_name": "Status__c", "label": "Status", "type": "picklist",
             "picklist": {"values": [{"value": "Scheduled", "label": "Scheduled"},
                                     {"value": "Done", "label": "Completed"}]}},
            {"api_name": "Colour__c", "label": "Colour", "type": "string"},
            {"api_name": "Notes__c", "label": "Notes", "type": "string"},
            {"api_name": "Contact_Email__c", "label": "Contact Email", "type": "email"},
            {"api_name": "Portal_Password__c", "label": "Portal Password", "type": "string"},
            {"api_name": "Tags__c", "label": "Tags", "type": "multipicklist", "picklist": None},
        ],
        "automation": {"flows": [{"name": "Gadget_Order_Reminder_Flow"}],
                       "apex": [{"name": "GadgetOrderRouter (managed): sends things"}]},
        "validation_rules": [{"name": "Require_Status"}],
        "business_process_usage": [{"process_id": "gadget_ordering", "process_name": "G1 Gadget ordering"}],
    }
    (kdir / "Gadget_Order__c.json").write_text(json.dumps(doc), encoding="utf-8")
    (kdir / "Gadget_Order__c.raw.json").write_text(json.dumps({"object": {"api_name": "Skip__c"}}), encoding="utf-8")
    (kdir / "notes.json").write_text(json.dumps(["not an object"]), encoding="utf-8")
    return kdir


def test_vocabulary_from_knowledge(tmp_path):
    v = build_vocabulary(_knowledge(tmp_path))
    got = {(t.kind, t.target, t.text, t.source) for t in v.terms}
    assert ("object", "Gadget_Order__c", "Gadget Orders", "plural") in got
    assert ("field", "Gadget_Order__c.Status__c", "Status", "label") in got
    assert ("value", "Gadget_Order__c.Status__c=Done", "Completed", "picklist_label") in got
    assert ("record_type", "Gadget_Order__c.RecordType=Bulk_Order", "Bulk Order", "label") in got
    assert ("component", "apex:GadgetOrderRouter", "GadgetOrderRouter", "apex") in got
    assert ("component", "validation_rule:Gadget_Order__c.Require_Status", "Require_Status", "validation_rule") in got
    assert ("component", "process:gadget_ordering", "Gadget ordering", "process") in got
    assert not any(t.target.startswith("Skip__c") for t in v.terms)
    assert {"router", "overrides", "gadgets", "handles"} <= v.known_words
    r2 = build(v)
    assert r2.resolve_term("Gadjet Ordr Remindr Flow", kinds=["component"]).suggestion == \
        "Gadget_Order_Reminder_Flow"


def _fake_snapshot(db: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(str(db))
    con.execute('CREATE TABLE "Gadget_Order__c" ("Id" VARCHAR, "Status__c" VARCHAR, "Colour__c" VARCHAR, '
                '"Notes__c" VARCHAR, "Contact_Email__c" VARCHAR, "Portal_Password__c" VARCHAR, "Tags__c" VARCHAR, '
                '"Batch_Code__c" VARCHAR, "Courier_Id__c" VARCHAR, "Handler_Label__c" VARCHAR, "Packer__c" VARCHAR)')
    rows = []
    for i in range(30):
        rows.append((f"a{i:017d}", "Scheduled" if i % 2 else "Done", ["Teal", "Amber", "Teal"][i % 3] if i else "Mauve",
                     f"free text note {i}", f"x{i}@example.com", "hunter2", "Red;Blue", f"Unique code {chr(65 + i)}",
                     ["Vexo", "Trafa"][i % 2], ["Fastlane", "Slowlane"][i % 2], ["Kestra Moonvale", "Orlen"][i % 2]))
    con.executemany('INSERT INTO "Gadget_Order__c" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', rows)
    con.execute('CREATE TABLE "Gadget_Vault__c" ("Id" VARCHAR, "Name" VARCHAR, "Vault_Phrase__c" VARCHAR)')
    con.executemany('INSERT INTO "Gadget_Vault__c" VALUES (?, ?, ?)', [(f"v{i}", "Vault", "opensesame")
                                                                      for i in range(5)])
    con.execute('CREATE TABLE "Account" ("Id" VARCHAR, "Name" VARCHAR, "IsPersonAccount" BOOLEAN)')
    con.executemany('INSERT INTO "Account" VALUES (?, ?, ?)', [("acc1", "Kestra Moonvale", True),
                                                               ("acc2", "Gizmo Holdings Ltd", False),
                                                               ("acc3", "Test Person 7", True),
                                                               ("acc4", "Dr. Pelor K. Imbrisk", True),
                                                               ("acc5", "Demo Candidate", True)])
    con.execute('CREATE TABLE "Lead" ("Id" VARCHAR, "Name" VARCHAR)')
    con.execute("INSERT INTO \"Lead\" VALUES ('lead1', 'Orlen Govardi Vashti')")
    con.execute('CREATE TABLE "User" ("Id" VARCHAR, "Name" VARCHAR, "UserType" VARCHAR)')
    con.executemany('INSERT INTO "User" VALUES (?, ?, ?)', [("u1", "Talvo Brenn", "Standard"),
                                                            ("u2", "Site Guest User", "Guest"),
                                                            ("u3", "Quorra Flint", "AutomatedProcess")])
    con.execute('CREATE TABLE "Contact" ("Id" VARCHAR, "Name" VARCHAR)')
    con.execute("INSERT INTO \"Contact\" VALUES ('c1', 'Kestra Moonvale')")
    con.close()


def _snapshot_knowledge(tmp: Path) -> Path:
    kdir = _knowledge(tmp)
    doc = json.loads((kdir / "Gadget_Order__c.json").read_text(encoding="utf-8"))
    doc["fields"] += [
        {"api_name": "Batch_Code__c", "label": "Batch Code", "type": "string"},
        {"api_name": "Courier_Id__c", "label": "Courier Id", "type": "string"},
        {"api_name": "Handler_Label__c", "label": "Handler Label", "type": "string",
         "semantic": {"roles": ["integration_attribute"]}},
        {"api_name": "Packer__c", "label": "Packer", "type": "string"},
    ]
    (kdir / "Gadget_Order__c.json").write_text(json.dumps(doc), encoding="utf-8")
    vault = {"object": {"api_name": "Gadget_Vault__c", "label": "Gadget Vault"},
             "fields": [{"api_name": "Name", "label": "Name", "type": "string"},
                        {"api_name": "Vault_Phrase__c", "label": "Vault Phrase", "type": "string"}]}
    (kdir / "Gadget_Vault__c.json").write_text(json.dumps(vault), encoding="utf-8")
    return kdir


def test_vocabulary_from_snapshot(tmp_path):
    db = tmp_path / "w.duckdb"
    _fake_snapshot(db)
    policy = SnapshotPolicy(sensitive_objects=frozenset({"Gadget_Vault__c"}))
    v = build_vocabulary(_snapshot_knowledge(tmp_path), db, max_distinct=20, lexicon=None, policy=policy)
    stored = {t.target: t.weight for t in v.terms if t.source == "stored"}
    text = {t.target: t.weight for t in v.terms if t.source == "stored_text"}
    assert stored["Gadget_Order__c.Status__c=Scheduled"] == 15
    assert {"Gadget_Order__c.Tags__c=Red", "Gadget_Order__c.Tags__c=Blue"} <= set(stored)   # multipicklist split
    assert text["Gadget_Order__c.Colour__c=Teal"] == 19                  # repeated values: a value list
    assert "Gadget_Order__c.Colour__c=Mauve" not in text                 # held by one record only
    assert not any(".Notes__c=" in k for k in text)                      # free text (by name)
    assert not any(".Batch_Code__c=" in k for k in text)                 # every value distinct
    assert not any(".Courier_Id__c=" in k for k in text)                 # identifier-like name
    assert not any(".Handler_Label__c=" in k for k in text)              # integration role
    assert not any(".Packer__c=" in k for k in text)                     # stored person names (people kind)
    assert not any("Email" in k or "Password" in k for k in {**stored, **text})   # contact and secret columns
    assert not any(t.target.startswith("Gadget_Vault__c") and t.source.startswith("stored") for t in v.terms)
    people = {(t.target, t.text, t.source) for t in v.terms if t.kind == "person"}
    assert ("person:Account/acc1", "Kestra", "first") in people
    assert ("person:Lead/lead1", "Orlen Vashti", "first_last") in people
    assert ("person:Account/acc4", "Pelor", "first") in people and ("person:Account/acc4", "Imbrisk", "last") in people
    assert ("person:Account/acc4", "Pelor Imbrisk", "first_last") in people    # title and initial left out
    assert not any(ref in p[0] for p in people for ref in ("acc2", "acc3", "acc5", "u2", "u3", "Contact/"))
    assert ("person:User/u1", "Talvo Brenn", "full") in people
    r2 = build(v)
    assert r2.resolve_term("kestrra moonvale", kinds=["person"]).candidates[0].target == "person:Account/acc1"


def test_snapshot_through_the_callers_connection_and_policy(tmp_path):
    """The app passes its own open, read-only connection (or a guarded query function) and its catalog policy: no
    second connection is opened, so a connection with another configuration does not clash."""
    duckdb = pytest.importorskip("duckdb")
    db = tmp_path / "w.duckdb"
    _fake_snapshot(db)
    kdir = _snapshot_knowledge(tmp_path)
    con = duckdb.connect(str(db), read_only=True, config={"enable_external_access": False, "threads": 2,
                                                          "max_memory": "512MB"})
    try:
        con.execute("SET lock_configuration=true")
        v1 = build_vocabulary(kdir, con.cursor(), lexicon=None, max_distinct=20)
        seen: list[str] = []

        def query(sql: str):
            seen.append(sql)
            if "Packer" in sql:
                raise RuntimeError("guard: blocked column")     # a refused statement skips that field only
            return con.execute(sql).fetchall()
        cols = {t: [c for (c,) in con.execute("SELECT column_name FROM information_schema.columns "
                                               f"WHERE table_name = '{t}'").fetchall()]
                for t in ("Gadget_Order__c", "Account", "Lead", "User")}
        pol = SnapshotPolicy.from_guard([("main", "Gadget_Order__c"), ("main", "Account")],
                                        blocked_fields=[("Gadget_Order__c", "Colour__c")])
        v2 = build_vocabulary(kdir, query, lexicon=None, max_distinct=20, columns=cols, policy=pol)
    finally:
        con.close()
    assert any(t.target == "Gadget_Order__c.Colour__c=Teal" for t in v1.terms)
    assert not any(".Colour__c=" in t.target for t in v2.terms if t.kind == "value")    # blocked by the policy
    assert not any(t.target.startswith("person:Lead/") for t in v2.terms)                 # table not allowed
    assert any(t.target == "person:Account/acc1" for t in v2.terms)
    assert not any("information_schema" in q for q in seen)                               # columns were given
    assert v2.info["snapshot"]["query_errors"] >= 1


def test_lexicon_words_are_known_and_reported(tmp_path):
    words = tmp_path / "words.txt"
    words.write_text("Gender\nmale\nmale's\nsender\nétude\ntwo words\n", encoding="utf-8")
    v = build_vocabulary(_knowledge(tmp_path), lexicon=words)
    assert {"gender", "male", "sender", "etude"} <= v.known_words and "male's" not in v.known_words
    assert v.info["lexicon"]["words"] == 4 and "warning" not in v.info["lexicon"]
    assert "warning" in build_vocabulary(tmp_path / "knowledge", lexicon=None).info["lexicon"]


def _timeless(info: dict) -> dict:
    return {k: ({kk: vv for kk, vv in v.items() if kk != "s"} if isinstance(v, dict) else v)
            for k, v in info.items() if not k.endswith("_s")}


def test_knowledge_vocabulary_is_build_vocabulary_without_a_snapshot(tmp_path):
    """Decision A1: the knowledge part moved to knowledge/terms.py; build_vocabulary builds on it, so both give the
    same terms, known words and counts, from the folder or from the documents already loaded."""
    kdir = _knowledge(tmp_path)
    words = tmp_path / "words.txt"
    words.write_text("gender\nmale\nsender\n", encoding="utf-8")
    full = build_vocabulary(kdir, lexicon=words)
    for k in (knowledge_vocabulary(kdir, words),
              knowledge_vocabulary([json.loads((kdir / "Gadget_Order__c.json").read_text(encoding="utf-8")),
                                    ["not an object"], {"object": {}}], words)):
        assert k.terms == full.terms and k.known_words == full.known_words
        assert _timeless(k.info) == _timeless(full.info)
        assert list(k.info) == list(full.info)        # the same keys, in the same order


def test_knowledge_resolver_needs_a_word_list_and_never_raises(tmp_path):
    kdir = _knowledge(tmp_path)
    r0, lines = knowledge_resolver(kdir, lexicon=None)
    assert r0 is None and len(lines) == 1 and lines[0].startswith("spelling help off: no English word list")
    r0, lines = knowledge_resolver(kdir, lexicon=[str(tmp_path / "missing.txt")])
    assert r0 is None and "missing.txt" in lines[0] and "SFA_LEXICON" in lines[0]
    words = tmp_path / "words.txt"
    words.write_text("gender\nmale\n", encoding="utf-8")
    r1, lines = knowledge_resolver(kdir, lexicon=[str(words)])
    assert r1 is not None and r1.resolve_term("Gadjet Ordr", kinds=["object"]).suggestion == "Gadget Order"
    assert len(lines) == 1 and lines[0].startswith("spelling help on (knowledge files): ")
    assert "object 3, field " in lines[0] and "word list 2 words" in lines[0] and "person" not in lines[0]
    r2, lines = knowledge_resolver(42, lexicon=[str(words)])                 # not a folder, not documents
    assert r2 is None and lines == ["spelling help off: the knowledge vocabulary could not be built: TypeError: "
                                    "'int' object is not iterable"]


# ------------------------------------------------------------------------------------------------------ layering
def test_importing_the_resolver_and_vocabulary_does_not_load_duckdb():
    code = ("import sys; import sf_assistant.knowledge.resolver, sf_assistant.knowledge.terms, "
            "sf_assistant.engine.vocabulary; print('duckdb' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def _imports(path: Path) -> list[tuple[str, int]]:
    out = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out += [(a.name, 0) for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            out.append((node.module or "", node.level))
    return out


def test_resolver_imports_only_the_standard_library():
    for name, level in _imports(KNOWLEDGE / "resolver.py"):
        assert level == 0 and name.split(".")[0] in sys.stdlib_module_names, name


def test_terms_import_only_stdlib_and_the_resolver():
    """knowledge/terms.py (the knowledge part of the vocabulary, decision A1): no engine code, no duckdb."""
    for name, level in _imports(KNOWLEDGE / "terms.py"):
        assert level == 0 and (name.split(".")[0] in sys.stdlib_module_names
                               or name == "sf_assistant.knowledge.resolver"), name


def test_vocabulary_imports_only_stdlib_the_knowledge_part_and_duckdb():
    for name, level in _imports(ENGINE / "vocabulary.py"):
        assert level == 0, name
        assert name.split(".")[0] in sys.stdlib_module_names or name == "duckdb" \
            or name in ("sf_assistant.knowledge.resolver", "sf_assistant.knowledge.terms"), name
