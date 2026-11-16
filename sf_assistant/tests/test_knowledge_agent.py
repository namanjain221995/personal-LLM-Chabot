"""Offline pytest suite for the knowledge agent harness (was notebooks/object_knowledge_eval/tests/test_okeval.py).

Every test runs without network access: the LLM is always :class:`sf_assistant.agent.mock_llm.MockClient`.
Run from the ``object_knowledge_eval`` directory::

    python -m pytest -q tests
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pandas as pd
import pytest

from sf_assistant.config import KNOWLEDGE_DIR

ROOT = Path(__file__).resolve().parents[1]

from sf_assistant.agent import knowledge_prompts as prompts
from sf_assistant.evaluation.questions import CATEGORIES, adversarial_questions, dedupe, gold_questions, llm_questions, paraphrase
from sf_assistant.agent.llm import LLM
from sf_assistant.agent.knowledge_agent import AgentResult, answer_all, answer_question
from sf_assistant.knowledge.search import KnowledgeIndex
from sf_assistant.agent.mock_llm import MockClient
from sf_assistant.knowledge.store import ObjectKnowledge
from sf_assistant.agent.question import Question
from sf_assistant.tools.knowledge_tools import ToolRegistry
from sf_assistant.evaluation.report import coverage, export, knowledge_gaps, results_frame, summary
from sf_assistant.evaluation.grading import grade_all, grade_deterministic
from sf_assistant.config import load_config

# Frozen copy of the first Internal Interview knowledge file: the harness tests pin its known gaps,
# so they must not follow the evolving knowledge/ file.
CLEAN = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
RAW = KNOWLEDGE_DIR / "Internal_Interview__c.raw.json"

# Sample arguments for every SPEC tool; each call must return ok=True.
TOOL_SAMPLE_ARGS: dict[str, dict] = {
    "describe_object": {},
    "list_fields": {"type": "picklist"},
    "get_field": {"name": "Status__c"},
    "search_fields": {"query": "who conducts the interview", "k": 5},
    "get_picklist_values": {"field": "Status__c"},
    "get_relationships": {},
    "get_child_relationships": {},
    "find_relationship": {"concept": "candidate"},
    "get_temporal_semantics": {},
    "get_validation_rules": {},
    "get_automation": {"kind": "flow"},
    "get_business_processes": {},
    "search_knowledge": {"query": "interview status", "k": 5},
    "get_provenance": {},
    "get_record_types": {},
}


# ----------------------------------------------------------------------------- fixtures
@pytest.fixture(scope="session")
def cfg():
    """Config pointing at a fake endpoint (never contacted: the client is mocked)."""
    return load_config(base_url="http://mock.invalid/v1", api_key="test-key", max_tool_steps=6)


@pytest.fixture(scope="session")
def kn() -> ObjectKnowledge:
    return ObjectKnowledge.load(str(CLEAN))


@pytest.fixture(scope="session")
def index(kn) -> KnowledgeIndex:
    return KnowledgeIndex(kn.chunks())


@pytest.fixture(scope="session")
def registry(kn, index) -> ToolRegistry:
    return ToolRegistry(kn, index)


@pytest.fixture()
def llm(cfg) -> LLM:
    """Fresh mock-backed LLM per test (MockClient keeps a request log)."""
    return LLM(cfg, client=MockClient())


@pytest.fixture(scope="session")
def gold(kn) -> list[Question]:
    return gold_questions(kn)


@pytest.fixture(scope="session")
def adversarial(kn) -> list[Question]:
    return adversarial_questions(kn)


def _errors(problems: list[str]) -> list[str]:
    """Hard errors only (warnings carry the ``WARN:`` prefix)."""
    return [p for p in problems if not p.startswith("WARN:")]


# ----------------------------------------------------------------------------- knowledge
def test_clean_knowledge_loads_and_validates(kn):
    assert kn.api_name == "Internal_Interview__c"
    assert kn.fields(), "no fields loaded"
    assert _errors(kn.validate()) == []


def test_clean_file_has_no_removed_object_keys(kn):
    # Keys "to remove" may only show up (as warnings) in the raw file.
    assert not [p for p in kn.validate() if "should be removed" in p]


@pytest.mark.skipif(not RAW.is_file(), reason="the git-ignored knowledge file knowledge/objects/Internal_Interview__c.raw.json is not there")
def test_raw_knowledge_only_warnings(kn):
    raw = ObjectKnowledge.load(str(RAW))
    problems = raw.validate()
    assert _errors(problems) == []
    assert any("should be removed" in p for p in problems), "raw file should warn about removed keys"


def test_validate_reports_broken_file(kn):
    data = json.loads(json.dumps(kn.raw))
    data["fields"].append(dict(data["fields"][0]))  # duplicate api_name
    data.pop("temporal_semantics", None)           # missing required key
    assert len(_errors(ObjectKnowledge(data).validate())) >= 2


def test_chunk_ids_unique(kn):
    ids = [c["id"] for c in kn.chunks()]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(kn.section_ids())
    for c in kn.chunks():
        assert {"id", "section", "title", "text", "data"} <= set(c)


def test_lookup_case_insensitive(kn):
    assert kn.field("status__c")["api_name"] == "Status__c"
    label = kn.field("Status__c").get("label")
    if label:
        assert kn.field(label.upper())["api_name"] in {f["api_name"] for f in kn.fields_with_label(label)}
    assert kn.field("definitely_not_a_field__c") is None


def test_search_ranks_api_name(index):
    hits = index.search("Status__c", k=3)
    assert hits and all("score" in h for h in hits)
    assert hits == sorted(hits, key=lambda h: -h["score"])
    assert any(h["id"] in {"field:Status__c", "picklist:Status__c"} for h in hits)


# ----------------------------------------------------------------------------- tools
def test_all_spec_tools_registered(registry):
    assert set(TOOL_SAMPLE_ARGS) <= set(registry.tools)
    listed = registry.list_tools()
    assert all({"name", "description", "inputSchema"} <= set(t) for t in listed)
    oa = registry.openai_tools()
    assert all(t["type"] == "function" and {"name", "description", "parameters"} <= set(t["function"]) for t in oa)


@pytest.mark.parametrize("name", sorted(TOOL_SAMPLE_ARGS))
def test_every_tool_callable(registry, name):
    out = registry.call(name, TOOL_SAMPLE_ARGS[name])
    assert set(out) >= {"ok", "result", "error", "chunk_ids"}
    assert out["ok"] is True, out["error"]
    json.dumps(out)  # MCP results must be JSON-serialisable


def test_unknown_field_suggestions(registry):
    out = registry.call("get_field", {"name": "Statsu__c"})
    assert out["ok"] is False
    assert out["error"].startswith("no field named Statsu__c; closest:")
    assert "Status__c" in out["error"]
    suggestions = re.search(r"closest: \[(.*)\]", out["error"]).group(1).split(",")
    assert 1 <= len(suggestions) <= 5


def test_registry_never_raises(registry):
    assert registry.call("no_such_tool", {})["ok"] is False
    assert registry.call("get_field", {})["ok"] is False
    assert registry.call("get_field", {"_raw": "{bad json"})["ok"] is False


# ----------------------------------------------------------------------------- questions
def test_gold_questions_volume_and_coverage(gold):
    assert len(gold) >= 150
    covered = {q.category for q in gold}
    assert set(CATEGORIES) - covered == set(), f"categories without gold: {set(CATEGORIES) - covered}"
    assert len({q.qid for q in gold}) == len(gold)


def test_gold_questions_well_formed(kn, gold):
    ids = set(kn.section_ids())
    for q in gold:
        assert q.category in CATEGORIES and q.source == "gold"
        assert q.expected_tools, q.qid
        assert set(q.gold_chunk_ids) <= ids, q.qid
        if q.answerable:
            assert q.expected_facts, q.qid


def test_adversarial_questions(adversarial):
    assert len(adversarial) >= 15
    assert all(not q.answerable and q.expected_facts == [] for q in adversarial)


def test_llm_questions_and_paraphrase_with_mock(kn, index, llm, gold):
    gen = llm_questions(kn, llm, index, per_category=1)
    assert gen and all(q.source == "llm" and q.category in CATEGORIES for q in gen)
    para = paraphrase(gold[:3], llm, n=2)
    assert para and all(q.source == "paraphrase" for q in para)
    merged = dedupe(gold + gold[:5] + para)
    assert len({q.qid for q in merged}) == len(merged)
    assert len(merged) < len(gold) + 5 + len(para)  # exact duplicates were dropped


# ----------------------------------------------------------------------------- llm + agent
def test_llm_retries_then_succeeds(cfg):
    client = MockClient(fail_first=2)
    llm = LLM(cfg, client=client)
    llm.backoff_base_s = 0  # no sleeping in tests
    out = llm.chat([{"role": "user", "content": "hello"}])
    assert out["content"] and len(client.requests) == 3
    assert set(out) >= {"content", "tool_calls", "usage", "latency_s", "raw"}


def test_agent_loop_terminates_and_records_tools(llm, registry, cfg, gold):
    q = next(q for q in gold if q.category == "picklist_values")
    r = answer_question(q, llm, registry, cfg)
    assert isinstance(r, AgentResult) and r.error is None
    assert r.answer
    assert 1 <= r.steps <= cfg.max_tool_steps + 1
    assert r.tool_calls and all({"name", "arguments", "ok", "chunk_ids", "latency_s"} <= set(tc) for tc in r.tool_calls)
    assert any(tc["chunk_ids"] for tc in r.tool_calls)


def test_agent_refuses_record_data(llm, registry, cfg, adversarial):
    q = next(q for q in adversarial if q.category == "unanswerable_record_data")
    r = answer_question(q, llm, registry, cfg)
    assert r.error is None and "record data" in r.answer.lower()


def test_answer_all_keeps_order(llm, registry, cfg, gold):
    qs = gold[:12]
    results = answer_all(qs, llm, registry, cfg, workers=4, progress=False)
    assert [r.qid for r in results] == [q.qid for q in qs]


# ----------------------------------------------------------------------------- grading
def _answerable_q(gold: list[Question]) -> Question:
    return next(q for q in gold if q.answerable and q.category == "picklist_values")


def test_grade_perfect_answer(kn, gold):
    q = _answerable_q(gold)
    r = AgentResult(qid=q.qid, question=q.question, answer="The values are: " + "; ".join(q.expected_facts),
                    tool_calls=[{"name": q.expected_tools[0], "arguments": {}, "ok": True,
                                 "chunk_ids": q.gold_chunk_ids, "latency_s": 0.0}],
                    steps=2, usage={}, latency_s=0.1, error=None)
    g = grade_deterministic(q, r, kn)
    assert g.fact_recall == 1.0 and g.facts_missing == [] and g.tool_hit and g.passed
    assert g.refusal_correct is None


def test_grade_wrong_answer(kn, gold):
    q = _answerable_q(gold)
    r = AgentResult(qid=q.qid, question=q.question, answer="I think it is purple.",
                    tool_calls=[], steps=1, usage={}, latency_s=0.1, error=None)
    g = grade_deterministic(q, r, kn)
    assert g.fact_recall < 0.5 and g.facts_missing and not g.passed


def test_grade_unanswerable(kn, adversarial):
    q = adversarial[0]
    ok = AgentResult(qid=q.qid, question=q.question,
                     answer="That is not available from object knowledge — needs record data.",
                     tool_calls=[], steps=1, usage={}, latency_s=0.0, error=None)
    bad = AgentResult(qid=q.qid, question=q.question, answer="There are 42 records.",
                      tool_calls=[], steps=1, usage={}, latency_s=0.0, error=None)
    assert grade_deterministic(q, ok, kn).refusal_correct is True
    assert grade_deterministic(q, ok, kn).passed
    assert not grade_deterministic(q, bad, kn).passed


# ----------------------------------------------------------------------------- report
def test_report_end_to_end(tmp_path, kn, llm, registry, cfg, gold, adversarial):
    qs = gold[:15] + adversarial[:5]
    results = answer_all(qs, llm, registry, cfg, workers=4, progress=False)
    grades = grade_all(qs, results, llm, cfg, kn, use_judge=True, workers=4)
    assert [g.qid for g in grades] == [q.qid for q in qs]
    assert all(g.judge is not None for g in grades)

    df = results_frame(qs, results, grades)
    assert isinstance(df, pd.DataFrame) and len(df) == len(qs)
    summ = summary(df)
    assert {"category", "n", "pass_rate", "fact_recall", "tool_hit"} <= set(summ.columns)

    cov = coverage(kn, results)
    assert set(cov) >= {"touched", "never_touched", "ratio"} and 0 < cov["ratio"] <= 1
    assert set(cov["touched"]).isdisjoint(cov["never_touched"])

    assert isinstance(knowledge_gaps(df), pd.DataFrame)
    paths = export(df, str(tmp_path), coverage_info=cov)
    assert len(paths) == 3 and all(Path(p).exists() for p in paths)
    assert len(pd.read_csv(paths[0])) == len(qs)


# ----------------------------------------------------------------------------- prompts
def test_prompts_render_fails_loudly():
    for name in ("QUESTION_GEN_SYSTEM", "QUESTION_GEN_USER", "PARAPHRASE_USER", "AGENT_SYSTEM",
                 "JUDGE_SYSTEM", "JUDGE_USER"):
        assert isinstance(getattr(prompts, name), str)
    with pytest.raises(KeyError):
        prompts.render("PARAPHRASE_USER", question="x")
    assert "needs record data" in prompts.AGENT_SYSTEM


# ----------------------------------------------------------------------------- grading regressions
from sf_assistant.evaluation.grading import fact_in_answer, is_refusal  # noqa: E402


def _answer(q: Question, text: str) -> AgentResult:
    return AgentResult(qid=q.qid, question=q.question, answer=text, tool_calls=[], steps=1, usage={},
                       latency_s=0.0, error=None)


def test_echo_answer_never_passes(kn, gold):
    """Repeating the question back must not earn recall on any answerable gold question."""
    passing = [q.qid for q in gold if q.answerable
               and grade_deterministic(q, _answer(q, "I don't know. " + q.question), kn).passed]
    assert passing == []


@pytest.mark.parametrize("answer", [
    "There are 1,240 Internal Interview records; this figure is not intended as exact.",
    "The interviewer was John Smith. Note: the Lead lookup is not indexed.",
    "The record count is 57 (not including deleted rows).",
    "Yes, the field is required. The object contains no rollup fields.",
])
def test_refusal_regex_rejects_hallucinated_answers(answer):
    assert not is_refusal(answer)


@pytest.mark.parametrize("answer", [
    "The knowledge file doesn’t contain record counts.",
    "That isn't available from object knowledge.",
    "Record counts are unavailable in the object metadata.",
    "I can’t determine that from the object knowledge.",
    "There is no field called Salary__c on Internal Interview.",
    "The file has no such field; Salary__c does not exist.",
])
def test_refusal_regex_accepts_real_refusals(answer):
    assert is_refusal(answer)


@pytest.mark.parametrize("fact,answer", [
    ("Postion__c", "It is spelled Position__c."),
    ("Final_Descion__c", "The API name is Final_Decision__c."),
    ("Selected_Time_Zone_Start_Tim__c", "It's Selected_Time_Zone_Start_Time__c."),
    ("Date__c", "Use Scheduled_Date__c, the Scheduled Date field."),
    ("Name", "The field is called Internal Interview Id."),
    ("Name", "The API name of that field is Internal_Interview_Id__c."),
])
def test_exact_api_facts_reject_near_misses(kn, fact, answer):
    assert not fact_in_answer(fact, answer, kn, exact=True)


def test_exact_api_facts_accept_the_real_name(kn):
    assert fact_in_answer("Postion__c", "It is **`Postion__c`** (sic).", kn, exact=True)
    assert fact_in_answer("Name", "The API name is `Name`.", kn, exact=True)


def test_plain_facts_are_not_expanded_to_fields(kn, gold):
    assert not fact_in_answer("Lead", "Lead__c is a lookup that points to the Account object.", kn)
    assert fact_in_answer("Lead", "Lead__c points to the standard Lead object.", kn)
    q = next(q for q in gold if q.category == "reference_target" and "Lead__c" in q.question)
    assert not grade_deterministic(q, _answer(q, "Lead__c is a lookup that points to the Account object."), kn).passed


def test_picklist_values_are_exact(kn, gold):
    assert not fact_in_answer("Hire", "AI Decision has only one value: No Hire.", kn, exact=True, siblings=["No Hire"])
    assert not fact_in_answer("Hire", "Final Decision allows: Hired, NoHire.", kn, exact=True, siblings=["NoHire"])
    q = next(q for q in gold if q.category == "picklist_values" and q.expected_facts == ["Hire", "No Hire"])
    assert not grade_deterministic(q, _answer(q, "AI Decision has only one value: No Hire."), kn).passed
    assert grade_deterministic(q, _answer(q, "AI Decision values: Hire, No Hire."), kn).passed


@pytest.mark.parametrize("fact,answer", [
    ("AI evaluation does not recommend hiring the candidate",
     "No Hire means the AI evaluation does recommend hiring the candidate."),
    ("locked from further modification",
     "Once Completed, the record is not locked and stays open to further modification."),
    ("Interview Readiness feedback", "This field holds the interview date; readiness feedback is elsewhere."),
    ("active", "No, the rule is not active."),
    ("string", "The field type is rich text area."),
])
def test_negated_or_contradicting_answers_do_not_match(kn, fact, answer):
    assert not fact_in_answer(fact, answer, kn)


def test_descriptive_facts_still_match(kn):
    assert fact_in_answer("AI evaluation does not recommend hiring the candidate",
                          "**No Hire**: the AI evaluation does not recommend hiring the candidate.", kn)
    assert fact_in_answer("string", "It is a Text (string) field.", kn)
    assert fact_in_answer("many to one", "cardinality: many_to_one", kn)
    assert fact_in_answer("131072", "The maximum length is **131,072** characters.", kn, exact=True)


def test_forbidden_fact_fails_the_answer(kn, gold):
    q = next(q for q in gold if q.category == "typo_api_names" and "Position__c" in q.forbidden_facts)
    bad = grade_deterministic(q, _answer(q, "Postion__c is in the schema. Use Position__c in SOQL."), kn)
    good = grade_deterministic(q, _answer(q, "It is Postion__c, not Position__c (schema typo)."), kn)
    assert bad.forbidden_found == ["Position__c"] and not bad.passed
    assert good.forbidden_found == [] and good.passed


def test_legacy_questions_without_successor_do_not_ask_what_replaced_it(gold):
    """The file names no successor for the legacy end-time field: the question must not invite a guess."""
    qs = [q for q in gold if q.category == "legacy_vs_current"
          and q.expected_facts and q.expected_facts[0] == "Selected_Timezone_End_Time__c"]
    assert qs and all("say what replaced" in q.question for q in qs)
    assert all("Selected_Time_Zone_End_Time__c" in q.forbidden_facts for q in qs)


def test_knowledge_consistency_warnings(kn):
    warnings = kn.consistency_warnings()
    assert all(w.startswith("WARN: consistency:") for w in warnings)
    assert any("Selected_Time_Zone_Start_Tim__c" in w for w in warnings)
    assert any("Session__c.Internal_Interview__c" in w for w in warnings)


# ----------------------------------------------------------------------------- agent / llm / prompts regressions
class _ToolsForeverClient:
    """Fake client that always asks for a tool and never answers."""

    def __init__(self):
        from types import SimpleNamespace as NS
        self.NS = NS
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        NS = self.NS
        call = NS(id="c1", function=NS(name="describe_object", arguments="{}"))
        return NS(choices=[NS(message=NS(content=None, tool_calls=[call]))],
                  usage=NS(prompt_tokens=1, completion_tokens=1))


def test_agent_reports_empty_final_answer(cfg, registry, gold):
    r = answer_question(gold[0], LLM(cfg, client=_ToolsForeverClient()), registry, cfg)
    assert r.answer == "" and r.error


def test_question_gen_honours_template_edits(kn, index, cfg):
    client = MockClient()
    original = prompts.TEMPLATES["QUESTION_GEN_SYSTEM"]
    try:
        prompts.TEMPLATES["QUESTION_GEN_SYSTEM"] = "EDITED SYSTEM PROMPT " + original
        llm_questions(kn, LLM(cfg, client=client), index, per_category=1, categories=["field_lookup"])
    finally:
        prompts.TEMPLATES["QUESTION_GEN_SYSTEM"] = original
    systems = [m["content"] for req in client.requests for m in req.get("messages", []) if m.get("role") == "system"]
    assert any(s.startswith("EDITED SYSTEM PROMPT") for s in systems)


def test_temperature_omitted_for_openai_reasoning_models():
    client = MockClient()
    cfg_o = load_config(base_url="https://api.openai.com/v1", api_key="x", model="o4-mini")
    LLM(cfg_o, client=client).chat([{"role": "user", "content": "hi"}])
    assert "temperature" not in client.requests[-1]
    cfg_g = load_config(base_url="https://api.openai.com/v1", api_key="x", model="gpt-4o-mini")
    LLM(cfg_g, client=client).chat([{"role": "user", "content": "hi"}])
    assert "temperature" in client.requests[-1]


def test_paraphrase_problem_detects_drift(kn, gold):
    from sf_assistant.evaluation.questions import paraphrase_problem
    q = next(q for q in gold if q.category == "field_type" and "Has_Resume__c" in q.question)
    assert paraphrase_problem(q, "What type is the Resume Attached checkbox on an interview?", kn)
    assert paraphrase_problem(q, "What data type is the Has Resume flag on an interview?", kn) is None


def test_coverage_reports_question_and_attribute_coverage(kn, gold):
    qs = gold[:40]
    results = [AgentResult(qid=q.qid, question=q.question, answer="", tool_calls=[
        {"name": "list_fields", "arguments": {}, "ok": True, "latency_s": 0.0,
         "chunk_ids": [f"field:{f['api_name']}" for f in kn.fields()]}], steps=1, usage={}, latency_s=0.0,
        error=None) for q in qs]
    cov = coverage(kn, results, questions=qs)
    # One unfiltered list_fields call must not mark every field as touched.
    assert len([c for c in cov["touched"] if c.startswith("field:")]) < len(kn.fields())
    assert {"question_coverage", "attribute_coverage", "knowledge_inconsistencies"} <= set(cov)
    assert 0 < cov["attribute_coverage"]["ratio"] <= 1


# ----------------------------------------------------------------------------- records stay outside the knowledge side
class _MessageLog:
    """Fake LLM: answers without tools first (``nudge``), or asks for a tool while tools are offered."""

    def __init__(self, mode: str):
        self.mode, self.calls = mode, []

    def chat(self, messages, tools=None, **_):
        self.calls.append([dict(m) for m in messages])
        if self.mode == "nudge":
            return {"content": "answer" if len(self.calls) > 1 else "early", "tool_calls": [], "usage": {}}
        if tools:
            return {"content": None, "tool_calls": [{"id": f"c{len(self.calls)}", "name": "describe_object",
                                                     "arguments": {}}], "usage": {}}
        return {"content": "final", "tool_calls": [], "usage": {}}


def test_answer_question_messages_default_and_custom(cfg, registry, gold):
    from sf_assistant.agent.knowledge_agent import FORCE_ANSWER_MESSAGE, REQUIRE_TOOLS_MESSAGE

    llm = _MessageLog("nudge")
    answer_question(gold[0], llm, registry, cfg, require_tools=True)
    assert llm.calls[1][-1] == {"role": "user", "content": REQUIRE_TOOLS_MESSAGE}
    llm = _MessageLog("nudge")
    answer_question(gold[0], llm, registry, cfg, require_tools=True, require_tools_message="CUSTOM NUDGE")
    assert llm.calls[1][-1] == {"role": "user", "content": "CUSTOM NUDGE"}

    llm = _MessageLog("tools")
    r = answer_question(gold[0], llm, registry, cfg)
    assert r.answer == "final" and llm.calls[-1][-1] == {"role": "user", "content": FORCE_ANSWER_MESSAGE}
    llm = _MessageLog("tools")
    answer_question(gold[0], llm, registry, cfg, force_answer_message="CUSTOM FORCE")
    assert llm.calls[-1][-1] == {"role": "user", "content": "CUSTOM FORCE"}


def test_answer_question_check_answer_retries_once(cfg, registry, gold):
    seen = []

    def check(draft):
        seen.append(draft)
        return "CHECK AGAIN"

    llm = _MessageLog("nudge")  # first reply "early", every later one "answer"
    r = answer_question(gold[0], llm, registry, cfg, check_answer=check)
    assert seen == ["early"] and r.answer == "answer" and r.steps == 2
    assert llm.calls[1][-2:] == [{"role": "assistant", "content": "early"}, {"role": "user", "content": "CHECK AGAIN"}]

    llm = _MessageLog("nudge")
    r = answer_question(gold[0], llm, registry, cfg, check_answer=lambda draft: None)
    assert r.answer == "early" and r.steps == 1

    def broken(draft):
        raise ValueError("boom")

    llm = _MessageLog("nudge")
    r = answer_question(gold[0], llm, registry, cfg, check_answer=broken)
    assert r.answer == "early" and r.error is None


def test_answer_question_max_checks_checks_a_later_answer_only_when_asked(cfg, registry, gold):
    """The default (1) is unchanged: only the first answer is checked. max_checks=2 (the record agent) checks the
    answer after a retry once more; an accepted answer ends the loop either way."""
    seen = []

    def check(draft):
        seen.append(draft)
        return "CHECK AGAIN"

    llm = _MessageLog("nudge")
    r = answer_question(gold[0], llm, registry, cfg, check_answer=check)
    assert seen == ["early"] and r.answer == "answer" and r.steps == 2
    seen.clear()
    llm = _MessageLog("nudge")
    r = answer_question(gold[0], llm, registry, cfg, check_answer=check, max_checks=2)
    assert seen == ["early", "answer"] and r.answer == "answer" and r.steps == 3
    seen.clear()
    llm = _MessageLog("nudge")
    r = answer_question(gold[0], llm, registry, cfg, check_answer=lambda d: seen.append(d), max_checks=2)
    assert seen == ["early"] and r.answer == "early" and r.steps == 1   # nothing sent back: no second check


def test_import_okeval_does_not_import_duckdb():
    import subprocess

    code = ("import sys; import sf_assistant.knowledge.store, sf_assistant.knowledge.search, "
            "sf_assistant.tools.knowledge_tools, sf_assistant.agent.knowledge_agent, sf_assistant.agent.knowledge_prompts, "
            "sf_assistant.agent.llm, sf_assistant.agent.mock_llm, sf_assistant.agent.question, "
            "sf_assistant.evaluation.questions, sf_assistant.evaluation.grading, sf_assistant.evaluation.report; "
            "print('duckdb' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


def test_okeval_and_ask_have_no_bridge_to_the_other_pipeline():
    for path in sorted((ROOT / "src" / "sf_assistant").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for word in ("scripts/ask.sh", "--jsonl", "@@SFK_JSON@@", "RecordsClient"):
            assert word not in text, f"{path.name} mentions {word}"
    assert not (ROOT / "src" / "sf_assistant" / "knowledge" / "records.py").exists()
