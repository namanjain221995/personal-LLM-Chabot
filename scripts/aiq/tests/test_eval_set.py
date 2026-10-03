"""B-02: the upgrade evaluation set, its deterministic checks, and their control.

  * the set covers the nine situations of MASTER_PROMPT §11 and the seven
    request-understanding cases of §13, in Fast, Think and Max, with
    synthetic inputs only;
  * every case's hand-written good answer passes every one of its checks,
    and every bad answer fails exactly the checks it names, so each check
    type is shown both to accept and to reject;
  * the checks are opt-in, so the frozen QA suite (cases.CASES) scores as
    before.

Nothing here calls a model, a service or the network. The sandbox tests run
the hand-written answers' code (never model output) through code_sandbox, and
skip where the sandbox cannot isolate code, as tests/test_aiq_harness.py does.
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path

import pytest

AIQ = Path(__file__).resolve().parents[1]
if str(AIQ) not in sys.path:
    sys.path.append(str(AIQ))

import answer_checks as AC  # noqa: E402
import cases as C  # noqa: E402
import code_sandbox as CS  # noqa: E402
import eval_set as ES  # noqa: E402
import eval_set_answers as EA  # noqa: E402
import harness as H  # noqa: E402

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="the sandbox refuses to run code as root")
SANDBOX_CHECKS = {"code_present", "code_runs", "code_correct"}
BAD_IDS = [(cid, b["label"]) for cid, bads in EA.BAD.items() for b in bads]


def _res(rec: dict) -> dict:
    """A turn record in the shape run.py builds, for a hand-written answer."""
    answer = rec["answer"]
    return {"answer": answer, "md": H.md_metrics(answer), "reasoning_events": 0,
            "meta": {"sources": rec.get("sources") or []}, "source_passages": rec.get("passages")}


def _score(case: dict, rec: dict, code_result: dict | None = None) -> list:
    expect = dict(case["turns"][0]["expect"])
    res = _res(rec)
    if code_result is None:
        expect.pop("code", None)  # the sandbox half is scored by the sandbox tests
    else:
        res["code_result"] = code_result
    return H.check_turn(expect, res, effort=case["effort"], fail_text=ES.FAIL_TEXT, prev_file=None,
                        upload_path=None)


def _failed(checks: list) -> list:
    return sorted(c["check"] for c in checks if not c["ok"])


# ---------------------------------------------------------------- the set --

def test_the_set_covers_section_11_and_section_13():
    assert [c["kind"] for c in ES.SECTION_11] == [
        "a greeting", "rewriting supplied text", "an informal multi-part request",
        "a factual question that needs fresh evidence", "a question about an uploaded document",
        "a coding task that needs complete files", "a long-conversation follow-up",
        "a moderately complex Think task", "a deep Max research task"]
    assert [c["kind"] for c in ES.SECTION_13] == [
        "Explain this code; do not modify it.", "Use only the uploaded document.", "Make the answer names only.",
        "Research the current information and cite it.", "Make full code, not an outline.",
        "a rewriting task with the word 'today' inside quoted text",
        "a follow-up that refers to 'the model discussed earlier'"]
    # the §13 phrasings are sent verbatim, not paraphrased
    for c in ES.SECTION_13[:5]:
        assert c["kind"] in c["turns"][0]["message"], c["id"]
    assert "today" in ES.BY_ID["RQ06"]["turns"][0]["message"].split('"')[1]
    assert "the model discussed earlier" in ES.BY_ID["RQ07"]["turns"][0]["message"]


def test_the_set_is_structurally_valid():
    assert ES.validate() == []


def test_the_modes_are_exercised_as_the_cases_need():
    efforts = {c["id"]: c["effort"] for c in ES.EVAL_SET_CASES}
    assert set(efforts.values()) == {"fast", "think", "max"}
    assert efforts["EV08"] == "think" and efforts["EV06"] == "think"
    ev09 = ES.BY_ID["EV09"]
    assert ev09["effort"] == "max" and ev09["deep_research"] and ev09["web_search"] == "on"
    assert [c["id"] for c in ES.EVAL_SET_CASES if c["deep_research"]] == ["EV09"]
    # fresh-evidence cases use what the browser sends by default, so routing is under test
    assert ES.BY_ID["EV04"]["web_search"] == "auto" and ES.BY_ID["RQ04"]["web_search"] == "auto"


def test_document_cases_attach_a_synthetic_document():
    for cid in ("EV05", "RQ02"):
        (att,) = ES.BY_ID[cid]["attachments"]
        assert att["purpose"] == "document" and os.path.isfile(os.path.join(ES.FIXTURES, att["fixture"]))


def test_the_long_follow_up_buries_its_decisions_behind_a_long_paste():
    history = ES.BY_ID["EV07"]["history"]
    chars = sum(len(m["content"]) for m in history)
    assert chars >= 160_000, "at four characters a token the history must pass the 40,000-token compaction mark"
    revision = next(i for i, m in enumerate(history) if "budget is now EUR 48,000" in m["content"])
    catering = next(i for i, m in enumerate(history) if "vegan" in m["content"])
    assert len(history) - revision > 8 and len(history) - catering > 8, "the facts must sit outside 8 recent turns"
    assert ES.supplier_log() == ES.supplier_log(), "the paste is deterministic"
    assert "48,000" not in ES.supplier_log() and "Casa Ribeira" not in ES.supplier_log()


def test_the_set_stays_out_of_the_gated_suite():
    gated = {c["id"] for c in C.CASES}
    assert not gated & set(ES.BY_ID)
    assert len(C.CASES) == 66


_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_HOST = re.compile(r"https?://([^/\s)\"'<>`\]]+)", re.I)
_DOCUMENTATION_NETS = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def _all_text() -> str:
    parts = []
    for name in sorted(os.listdir(ES.EVALSET)):
        parts.append(Path(ES.EVALSET, name).read_text(encoding="utf-8"))
    for c in ES.EVAL_SET_CASES:
        parts += [t["message"] for t in c["turns"]] + [m["content"] for m in c["history"]]
    for rec in list(EA.GOOD.values()) + [b for bads in EA.BAD.values() for b in bads]:
        parts.append(rec["answer"])
        parts += [s["url"] for s in rec.get("sources") or []] + list((rec.get("passages") or {}).values())
    return "\n".join(parts)


def test_the_inputs_and_answers_carry_no_address_mail_or_real_url():
    text = _all_text()
    for ip in _IPV4.findall(text):
        assert any(ipaddress.ip_address(ip) in net for net in _DOCUMENTATION_NETS), ip
    assert _EMAIL.findall(text) == []
    for host in _URL_HOST.findall(text):
        assert re.fullmatch(r"(?:[\w-]+\.)*example\.(?:com|org|net)", host.lower()), host


# ------------------------------------------------- good and bad answers ----

def test_every_case_has_one_good_and_at_least_one_bad_answer():
    assert set(EA.GOOD) == set(ES.BY_ID) == set(EA.BAD)
    assert all(EA.BAD[cid] for cid in ES.BY_ID)


@pytest.mark.parametrize("cid", list(ES.BY_ID))
def test_the_good_answer_passes_every_check(cid):
    checks = _score(ES.BY_ID[cid], EA.GOOD[cid])
    assert checks, "a case with no checks measures nothing"
    assert _failed(checks) == [], [(c["check"], c["detail"]) for c in checks if not c["ok"]]


@pytest.mark.parametrize("cid,label", BAD_IDS, ids=[f"{c}-{lbl}" for c, lbl in BAD_IDS])
def test_the_bad_answer_fails_exactly_the_checks_it_names(cid, label):
    bad = next(b for b in EA.BAD[cid] if b["label"] == label)
    checks = _score(ES.BY_ID[cid], bad)
    assert _failed(checks) == sorted(bad["fails"]), [(c["check"], c["detail"]) for c in checks if not c["ok"]]


def test_every_check_type_is_used_by_the_set_and_rejects_a_bad_answer():
    emitted = {name for names in AC.CHECKS.values() for name in names}
    used = set()
    for c in ES.EVAL_SET_CASES:
        for key in c["turns"][0]["expect"]:
            used |= set(AC.CHECKS.get(key, ()))
    assert emitted <= used, f"never used: {sorted(emitted - used)}"
    rejected = {f for bads in EA.BAD.values() for b in bads for f in b["fails"] + b.get("sandbox_fails", [])}
    assert emitted <= rejected, f"no bad answer fails: {sorted(emitted - rejected)}"
    assert {"code_runs", "code_correct"} <= rejected


def test_the_new_checks_are_opt_in_for_the_frozen_suite():
    keys = {k for c in C.CASES for t in c["turns"] for k in t["expect"]}
    assert not keys & AC.EXPECT_KEYS
    expect = C.BASELINE_CASES[0]["turns"][0]["expect"]
    checks = H.check_turn(expect, _res({"answer": "x"}), effort="fast", fail_text=C.FAIL_TEXT, prev_file=None,
                          upload_path=None)
    assert not {c["check"] for c in checks} & set(AC.DIMENSION)


# ---------------------------------------------------- the checks, closer --

def test_names_only_accepts_the_usual_list_shapes():
    spec = {"allowed": ES.ROSTER, "required": ES.PLATFORM, "min_items": 3, "max_items": 3}
    for answer in ("- Avery Quinn\n- Chen Okafor\n- Eli Navarro", "1. Avery Quinn\n2. Chen Okafor\n3. Eli Navarro",
                   "Avery Quinn, Chen Okafor, Eli Navarro", "**Avery Quinn**\n**Chen Okafor**\n**Eli Navarro**"):
        ok, why = AC.check_names_only(answer, spec)
        assert ok, (answer, why)
    assert not AC.check_names_only("", spec)[0]


def test_numbers_are_read_the_way_people_write_them():
    assert AC.numbers_in("EUR 120,000 or 120k or 120000") == {"120000"}
    assert AC.numbers_in("1. first item\n2. second [3] item") == set()
    assert AC.numbers_in("22.20 C and 0.4 s") == {"22.2", "0.4"}
    assert {"2", "4"} <= AC.numbers_in("two of the four", words=True)
    assert AC.numbers_in("two of the four") == set(), "an answer's number words are not numbers it invented"
    ok, why = AC.check_numbers_grounded("It costs 48k.", {"source": "budget EUR 48,000"})
    assert ok, why


def test_citations_resolve_links_grouped_markers_and_refuse_unread_rows():
    rows = [EA.src(1, "https://example.org/a"), EA.src(2, "https://www.example.org/b/")]
    res = {"meta": {"sources": rows}}
    (name, ok, why), = AC.check_citations("Fact one [1, 2]. See [b](https://example.org/b).", res, {"min_distinct": 2})
    assert ok, why
    no_flag = {"meta": {"sources": [{"n": 1, "url": "https://example.org/a"}]}}
    (_, ok, why), = AC.check_citations("Fact [1].", no_flag, {})
    assert not ok and "never read" in why, "a row that does not say it was read cannot back a citation"


def test_quoting_or_using_the_code_is_not_modifying_it():
    original = ES.TOKEN_BUCKET
    whole = "Here it is again, unchanged:\n\n```python\n" + original + "```"
    assert AC.check_code_unmodified(whole, {"original": original})[0]
    usage = "Use it like this:\n\n```python\nbucket = TokenBucket(2, 4)\nprint(bucket.allow())\n```"
    assert AC.check_code_unmodified(usage, {"original": original})[0]
    elided = "```python\nclass TokenBucket:\n    ...\n    def allow(self, cost: float = 1.0) -> bool:\n```"
    assert AC.check_code_unmodified(elided, {"original": original})[0]
    # a changed copy is caught whatever the fence's info string says
    changed = "```python token_bucket.py\n" + original.replace("min(", "max(") + "```"
    ok, why = AC.check_code_unmodified(changed, {"original": original})
    assert not ok and "max(" in why


def test_quoted_spans_survive_curly_quotes_and_line_breaks():
    answer = "The sign says “Closed today\nfor calibration”."
    assert AC.check_quoted_verbatim(answer, ["Closed today for calibration"])[0]
    assert not AC.check_quoted_verbatim("The sign says closed today for calibration.",
                                        ["Closed today for calibration"])[0]


def test_a_gap_is_recognised_in_its_common_phrasings():
    for answer in ("Tokyo isn't covered by the policy.", "The document has no information on Tokyo.",
                   "Tokyo: not specified in the document."):
        assert AC.check_gap_stated(answer, {"topic_any": ["tokyo"]})[0], answer
    assert not AC.check_gap_stated("The policy does not cover alcohol.", {"topic_any": ["tokyo"]})[0]


def test_an_abstract_method_is_not_a_placeholder():
    body = ("from abc import ABC, abstractmethod\n\n\nclass Store(ABC):\n    @abstractmethod\n"
            "    def get(self, key):\n        ...\n")
    answer = "`store.py`:\n\n```python\n" + body.replace("        ...\n", "        pass\n") + "```"
    results = {n: ok for n, ok, _ in AC.check_complete_code(answer, {"files": ["store.py"]})}
    assert results["code_no_placeholders"] and results["code_compiles"]


def test_named_files_are_found_wherever_answers_name_them():
    answer = ("### inventory.py\n\n```python\nA = 1\n```\n\n"
              "```python test_inventory.py\nB = 2\n```\n\n"
              "```python\n# file: helpers.py\nC = 3\n```\n\n"
              "Both `a.py` and `b.py` below:\n\n```python\nD = 4\n```\n\n"
              "`../escape.py`:\n\n```python\nE = 5\n```\n")
    found = CS.extract_named_files(answer)
    assert found == {"inventory.py": "A = 1\n", "test_inventory.py": "B = 2\n",
                     "helpers.py": "# file: helpers.py\nC = 3\n"}
    nested = "**src/inventory.py**\n\n```python\nA = 1\n```\n"
    assert CS.answer_files(nested, ["inventory.py"]) == {"inventory.py": "A = 1\n"}
    unnamed = "```python\nprint('one file')\n```"
    assert CS.answer_files(unnamed, ["wordfreq.py"]) == {"wordfreq.py": "print('one file')\n"}
    assert CS.answer_files(unnamed, ["a.py", "b.py"]) == {}


# ------------------------------------------------------------ the sandbox --

@pytest.fixture(scope="module")
def toolchain(tmp_path_factory):
    return CS.Toolchain(str(tmp_path_factory.mktemp("eval-set-code")))


CODE_CASES = [c["id"] for c in ES.EVAL_SET_CASES if "code" in c["turns"][0]["expect"]]
CODE_BADS = [(cid, b["label"]) for cid in CODE_CASES for b in EA.BAD[cid] if b.get("sandbox_fails")]


def _spec(cid):
    return ES.BY_ID[cid]["turns"][0]["expect"]["code"]


@NOT_ROOT
@pytest.mark.parametrize("cid", CODE_CASES)
def test_the_good_code_builds_and_passes_its_checker(cid, tmp_path, toolchain):
    available, why = toolchain.available("python")
    if not available:
        pytest.skip(why)
    rec = CS.run_code(_spec(cid), EA.GOOD[cid]["answer"], str(tmp_path), toolchain)
    assert rec["ok"], [(s["name"], s["rc"], s["tail"]) for s in rec["steps"]]
    assert _failed(_score(ES.BY_ID[cid], EA.GOOD[cid], code_result=rec)) == []


@NOT_ROOT
@pytest.mark.parametrize("cid,label", CODE_BADS, ids=[f"{c}-{lbl}" for c, lbl in CODE_BADS])
def test_the_bad_code_fails_where_it_is_run(cid, label, tmp_path, toolchain):
    available, why = toolchain.available("python")
    if not available:
        pytest.skip(why)
    bad = next(b for b in EA.BAD[cid] if b["label"] == label)
    rec = CS.run_code(_spec(cid), bad["answer"], str(tmp_path), toolchain)
    assert not rec.get("ok")
    failed = _failed(_score(ES.BY_ID[cid], bad, code_result=rec))
    assert failed == sorted(bad["fails"] + bad["sandbox_fails"]), [(s["name"], s["tail"]) for s in rec["steps"]]


@NOT_ROOT
def test_a_missing_file_fails_the_build_without_running_anything(tmp_path, toolchain):
    available, why = toolchain.available("python")
    if not available:
        pytest.skip(why)
    answer = "`inventory.py`:\n\n```python\n" + EA.INVENTORY_PY + "```\n"
    rec = CS.run_code(_spec("EV06"), answer, str(tmp_path), toolchain)
    assert [s["name"] for s in rec["steps"]] == ["files present"]
    assert "test_inventory.py" in rec["steps"][0]["tail"] and rec["steps"][0]["rc"] is None
    assert not (tmp_path / "inventory.py").exists() and not (tmp_path / "check.py").exists()
