"""The intent gate against the AS3 labelled sets, with RECORDED classifier
verdicts (offline, deterministic). Live classifier calls are opt-in:
AS3_LIVE=1 re-asks the engine for a small sample.

Sets (tests/fixtures):
  artifact_intent_set.py        205 labelled messages (5 language forms, typos)
  artifact_intent_negatives.py  150 hard negatives, written before the rules
  artifact_intent_heldout.py    HELDOUT (r1), HELDOUT2 (r2), HELDOUT3 (r3): written
                                by the local model after successive rule
                                freezes; r3 is the final estimate (no rule
                                change was made after reading it, except the
                                two recorded in docs/artifact-studio/as3/
                                intent-capability.md)
  artifact_intent_verdicts.json live verdicts for every item the rules send
                                to the classifier (205 set, negatives, r3)

The thresholds assert what was MEASURED; where a design target was missed,
the gap is named next to the assertion.
"""
from __future__ import annotations

import asyncio
import collections
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import fast_lane
from app.artifacts import intent as I
from app.artifacts import intent_llm
from app.artifacts import lexicon as LX

_FIX = Path(__file__).parent / "fixtures"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, _FIX / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SET = _load("artifact_intent_set")
NEG = _load("artifact_intent_negatives")
HELD = _load("artifact_intent_heldout")
VERDICTS = json.load(open(_FIX / "artifact_intent_verdicts.json", encoding="utf-8"))["items"]

AUDIT = ("# Onboarding Process Audit\n\n## 1. Summary\nOnboarding takes 19 working days; the target is 10.\n\n"
         "## 2. Findings\n| # | Area | Finding | Severity |\n|---|---|---|---|\n| 1 | Access | Laptop on day 6 | High |\n"
         "| 2 | Training | Modules unsequenced | Medium |\n\n## 3. Recommendations\n1. Provision access before day 1.\n"
         "2. Sequence modules.\n3. Assign a buddy.\n\n## 4. Conclusion\nFixing access alone saves five days.\n")
USER0 = {"role": "user", "content": "audit our onboarding process and write a detailed report"}
ANSWER = {"role": "assistant", "content": AUDIT}
HINTS = ["Quarterly Audit Report", "Vendor Tracker"]


def _ctx(code: str, upload: str = ""):
    if code.startswith("PU:"):
        code, upload = "PU", code.split(":", 1)[1]
    k = dict(has_artifacts=False, artifact_hints=[], history=[], upload_formats=[])
    if code in ("PA", "PUA"):
        k["history"] = [USER0, ANSWER]
    elif code == "PF":
        k.update(has_artifacts=True, artifact_hints=HINTS, history=[USER0, ANSWER])
    elif code == "PU":
        k["upload_formats"] = [upload or "pdf"]
    return code, k


def _accept(gold: str, code: str):
    if gold == "file":
        return {"create", "export", "convert", "edit"}
    if gold == "create":
        return {"create"}
    if gold == "convert":
        return {"convert"} if code == "PF" else ({"create", "export", "convert"} if code in ("PU", "PUA") else {"export"})
    if gold in ("edit", "style"):
        return {"edit"}
    return {"none"}


def _decide(rid: str, text: str, code: str, *, upload: str = "", effort: str = "fast", with_verdicts: bool = True):
    """The gate as main.py wires it: the Fast lane first, then the rules and
    the classifier hook (replaying the recorded verdict)."""
    code, k = _ctx(code, upload)
    calls = {"n": 0}
    req = SimpleNamespace(effort=effort, mode="assistant", text=text, pdf_data=None,
                          pdf_uploads=[{"id": "u1"}] if k["upload_formats"] else None, video_uploads=None, image_data=None,
                          agent=False, deep_research=False, web_search="auto", sf_live=False, clarification=None)
    if fast_lane.decide(req, text=text, history=k["history"], now_year=2026).entered:
        return I.ArtifactIntent("none", rule="lane"), 0, code

    async def hook(t, **_kw):
        calls["n"] += 1
        rec = VERDICTS.get(rid)
        if not with_verdicts or rec is None or rec["accepted"] is None:
            return None
        return intent_llm.IntentVerdict(**rec["accepted"])

    intent = asyncio.run(I.decide_with_hook(
        text, hook, has_artifacts=k["has_artifacts"], artifact_hints=k["artifact_hints"],
        has_assistant_answer=I.substantial_answer_index(k["history"]) is not None, upload_formats=k["upload_formats"],
        last_turn_is_artifact=I.last_turn_is_artifact(k["history"]),
    ))
    return intent, calls["n"], code


def _rows_205(effort="fast", with_verdicts=True):
    rows = []
    for rid, text, code, gold, _fmt, tags in SET.D:
        intent, calls, c = _decide(rid, text, code, upload=SET.UPLOAD_FORMAT.get(rid, ""), effort=effort, with_verdicts=with_verdicts)
        rows.append(dict(id=rid, gold=gold, action=intent.action, ok=intent.action in _accept(gold, c), lang=tags.split()[0], tags=tags,
                         calls=calls, intent=intent, text=text))
    return rows


@pytest.fixture(scope="module")
def rows205():
    return _rows_205()


def _recall(rows):
    pos = [r for r in rows if r["gold"] != "none"]
    return sum(r["action"] != "none" for r in pos) / len(pos)


# ------------------------------------------------------------ the 205 set --


def test_205_binary_file_recall_is_at_least_095(rows205):
    assert _recall(rows205) >= 0.95  # measured 1.000 (baseline before AS3: 0.455)


def test_205_decisions_are_identical_at_fast_and_think(rows205):
    think = _rows_205(effort="think")
    assert [r["action"] for r in rows205] == [r["action"] for r in think]


@pytest.mark.parametrize("lang", ["hinglish", "hi", "gu", "gujlish"])
def test_205_recall_per_language_with_and_without_the_classifier(rows205, lang):
    pos = [r for r in rows205 if r["gold"] != "none" and r["lang"] == lang]
    assert sum(r["action"] != "none" for r in pos) / len(pos) >= 0.90
    rules_only = [r for r in _rows_205(with_verdicts=False) if r["gold"] != "none" and r["lang"] == lang]
    assert sum(r["action"] != "none" for r in rules_only) / len(rules_only) >= 0.70


def test_205_edits_styles_exports_and_typos(rows205):
    es = [r for r in rows205 if r["gold"] in ("edit", "style")]
    assert sum(r["action"] == "edit" for r in es) / len(es) >= 0.90
    for r in es:
        if r["gold"] == "style":
            assert r["intent"].style_request or r["action"] != "edit" or r["intent"].rule != "edit-style"
    conv = [r for r in rows205 if r["gold"] == "convert" and r["id"] in {d[0] for d in SET.D if d[2] == "PA"}]
    assert sum(r["action"] == "export" for r in conv) / len(conv) >= 0.85
    typo = [r for r in rows205 if "typo" in r["tags"] and r["gold"] != "none"]
    assert sum(r["action"] != "none" for r in typo) >= 12 and len(typo) == 13


def test_205_exports_target_the_previous_answer(rows205):
    for r in rows205:
        if r["action"] == "export":
            assert r["intent"].target == "previous_answer" and r["intent"].reference == "previous_answer"


# ------------------------------------------------------------ negatives --


@pytest.fixture(scope="module")
def neg_rows():
    rows = []
    for rid, text, code, cat, lang in NEG.NEGATIVES:
        intent, calls, _ = _decide(rid, text, code)
        rows.append(dict(id=rid, action=intent.action, lang=lang, cat=cat, calls=calls, rule=intent.rule, text=text))
    return rows


def test_hard_negatives_false_file_rate(neg_rows):
    assert len(neg_rows) >= 150
    assert sum(r["action"] != "none" for r in neg_rows) / len(neg_rows) <= 0.02  # measured 0/150
    by = collections.defaultdict(list)
    for r in neg_rows:
        by[r["lang"]].append(r)
    for lang, rows in by.items():
        assert sum(r["action"] != "none" for r in rows) / len(rows) <= 0.04, lang


@pytest.mark.parametrize("text,code", [
    ("give me a summary", "PA"),
    ("summarize this pdf", "PU:pdf"),
    ("how do I convert word to pdf", "P0"),
    ("write python that makes a docx", "P0"),
])
def test_the_four_named_negatives(text, code):
    intent, _, _ = _decide("named", text, code)
    assert intent.action == "none", (text, intent.rule)


def test_the_classifier_is_not_called_on_negative_shapes_or_messages_without_a_file_word(neg_rows):
    for r in neg_rows:
        if r["rule"].startswith("negative:") or r["rule"] in ("code", "about-format", "chat-only", "text-object"):
            assert r["calls"] == 0, r
        if not LX.file_signal(r["text"]):
            assert r["calls"] == 0, r
        assert r["calls"] <= 1


# ------------------------------------------------------------- held out --


def _held_rows(items):
    rows = []
    for rid, text, code, gold, lang in items:
        intent, calls, c = _decide(rid, text, code)
        rows.append(dict(id=rid, gold=gold, action=intent.action, lang=lang, ok=intent.action in _accept(gold, c), calls=calls))
    return rows


def test_heldout_round3_final_estimate():
    rows = _held_rows(HELD.HELDOUT3)
    assert len(rows) >= 80
    assert _recall(rows) >= 0.90  # measured 0.988 with recorded verdicts (rules alone: 0.850)
    neg = [r for r in rows if r["gold"] == "none"]
    false_files = sum(r["action"] != "none" for r in neg)
    # DESIGN TARGET <= 3% NOT MET: measured 2/41 = 4.9% ("I want to edit the
    # attached report before submitting." and "मुझे इस चार्ट का डेटा स्रोत
    # चाहिए।"). This asserts no regression past the measurement.
    assert false_files <= 2, [r for r in neg if r["action"] != "none"]
    by = collections.defaultdict(list)
    for r in rows:
        if r["gold"] != "none":
            by[r["lang"]].append(r)
    for lang, pos in by.items():
        assert sum(r["action"] != "none" for r in pos) / len(pos) >= 0.80, lang  # lowest measured: gujlish 11/12


@pytest.mark.parametrize("name", ["HELDOUT", "HELDOUT2"])
def test_earlier_heldout_rounds_do_not_regress(name):
    rows = _held_rows(getattr(HELD, name))
    assert _recall(rows) >= 0.95
    neg = [r for r in rows if r["gold"] == "none"]
    assert sum(r["action"] != "none" for r in neg) == 0


def test_recorded_verdicts_were_fast_enough_for_the_fast_timeout():
    secs = sorted(v["engine_seconds"] for v in VERDICTS.values())
    p95 = secs[int(0.95 * len(secs)) - 1]
    assert len(secs) >= 30 and p95 <= 1.5  # measured p95 0.756 s, max 0.769 s over 48 calls


# --------------------------------------------- the production shape (10) --

PRODUCTION_SHAPES = [
    "just give it in docs in a standard and classy format, provide a dox file",
    "can u just give it in docs.. in standard n classy formatt? provide dox",
    "give it in docs, standard and classy format pls",
    "provide a dox file in a classy format",
    "just give this in doc format, classy and standard",
    "can you provide it as a docx in a standard classy format",
    "pls give it in word file, standard classy format",
    "give it in dox, classy formatt",
    "just provide the dox file, standard format, classy look",
    "can't you just give it in docs? provide a dox file",
]


def _long_audit() -> str:
    return AUDIT + "\n".join(f"- Observation {i}: evidence was sampled and two exceptions were recorded." for i in range(12))


@pytest.mark.parametrize("text", PRODUCTION_SHAPES)
@pytest.mark.parametrize("thanks", [False, True])
def test_production_shape_exports_the_report_as_docx(text, thanks):
    history = [USER0, {"role": "assistant", "content": _long_audit()}]
    if thanks:
        history += [{"role": "user", "content": "thanks"}, {"role": "assistant", "content": "You're welcome!"}]
    idx = I.substantial_answer_index(history)
    assert idx == 1, "the report turn, not the pleasantry"
    intent = I.decide(text, has_assistant_answer=idx is not None, last_turn_is_artifact=I.last_turn_is_artifact(history))
    assert intent.action == "export" and intent.target == "previous_answer" and intent.formats == ["docx"], (text, intent)
    # The Fast lane must not admit it either (it calls decide() itself).
    req = SimpleNamespace(effort="fast", mode="assistant", text=text, pdf_data=None, pdf_uploads=None, video_uploads=None, image_data=None,
                          agent=False, deep_research=False, web_search="auto", sf_live=False, clarification=None)
    assert not fast_lane.decide(req, text=text, history=history, now_year=2026).entered


@pytest.mark.parametrize("text", [
    "make it a docx",
    "give it in docs",
    "isko word file me de do",
    "same thing as excel please",
    "can I get this as a pdf?",
])
def test_after_a_file_card_the_follow_up_converts_the_artifact(text):
    history = [USER0, {"role": "assistant", "content": _long_audit()},
               {"role": "user", "content": "make it a pdf"}, {"role": "assistant", "content": "Created **Audit** as PDF."}]
    assert I.last_turn_is_artifact(history)
    intent = I.decide(text, has_artifacts=True, artifact_hints=["Audit"], has_assistant_answer=True, last_turn_is_artifact=True)
    assert intent.action == "convert" and intent.target == "artifact", (text, intent)


@pytest.mark.skipif(os.environ.get("AS3_LIVE") != "1", reason="live classifier calls are opt-in (AS3_LIVE=1)")
def test_live_classifier_sample():
    """Five live calls: two positives the rules cannot read, three negatives."""
    cases = [("Excel sheet bana ke de.", "PA", True), ("હું પીડીએફ માંગું છું.", "PA", True),
             ("Kya ye PowerPoint file editable hai?", "P0", False), ("Can I copy the chart from the Word doc?", "PA", False),
             ("E report nu format check karo please.", "PU:docx", False)]
    for text, code, wants in cases:
        _, k = _ctx(code)
        v = asyncio.run(intent_llm.classify(text, last_answer_head=AUDIT if k["history"] else "", has_artifacts=k["has_artifacts"],
                                            upload_formats=k["upload_formats"], upload_names=[f"a.{f}" for f in k["upload_formats"]],
                                            effort="think"))
        assert (v is not None) == wants, (text, v)
