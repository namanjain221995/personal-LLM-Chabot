"""LIVE evaluation of the self-check (opt-in: AS3_LIVE=1, a reachable engine
at OPENAI_BASE_URL, the test DSN). Two parts, concurrency 1, a hard cap on
model calls:

  * test_live_proposer_ablation — the 40 labelled requests through the rule
    extractor alone and through rule + the live proposer (Think): recall,
    precision, contested share. <= 40 calls.
  * test_live_end_to_end_jobs — 20 jobs through the real composer at Fast
    and the real renderer; each version's files, selfcheck.json and a
    request.json are copied to AS3_SAMPLES (default: a tmp dir) for
    scripts/as3_selfcheck_score.py, the independent scorer. <= 40 calls.

Invented requests only.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("AS3_LIVE") != "1", reason="live engine evaluation is opt-in (AS3_LIVE=1)")

from app import llm  # noqa: E402
from app.artifacts import pipeline, requirements as RQ, selfcheck as SC, store  # noqa: E402
from app.artifacts.render import render_version  # noqa: E402
from app.config import settings  # noqa: E402
from tests.fixtures.selfcheck import files as F  # noqa: E402
from tests.fixtures.selfcheck import requests_labelled as L  # noqa: E402
from tests.test_artifact_jobs import _accept, _run, isolated, owner  # noqa: E402,F401

CALL_CAP = int(os.environ.get("AS3_CALL_CAP", "80"))
_calls = {"n": 0}


@pytest.fixture(autouse=True)
def counted_engine(monkeypatch):
    real = llm.json_completion

    async def counted(*a, **k):
        _calls["n"] += 1
        if _calls["n"] > CALL_CAP:
            raise RuntimeError("live call cap reached")
        return await real(*a, **k)

    monkeypatch.setattr(llm, "json_completion", counted)
    monkeypatch.setattr(settings, "artifact_selfcheck", True)
    monkeypatch.setattr(settings, "artifact_stage_timeout_s", 600.0)
    yield


def test_live_proposer_ablation():
    source = "# Audit\n\n## Findings\n\n| Area | Status |\n|---|---|\n| Keys | Open |\n"
    rows = []
    for r in L.REQUESTS:
        base = dict(instruction=r["instruction"], kind=r["kind"], formats=[], operation=r["operation"], source_markdown=source if r["previous_answer"] else "")
        rule = asyncio.run(RQ.build(None, effort="fast", **base))
        both = asyncio.run(RQ.build(None, effort="think", timeout_s=30.0, **base))
        rows.append((r, rule, both))

    def score(pick):
        hit = total = ok = extra = contested = items = 0
        for r, rule, both in rows:
            cl = pick(rule, both)
            got = {(i.category, i.target, i.property, RQ._norm_expected(i.expected)) for i in cl.items if not i.contested}
            exp = {(c, t, p, RQ._norm_expected(e)) for c, t, p, e, _ in r["expected"]}
            total += len(exp)
            hit += len(exp & got)
            for g in got:
                if g[0] in ("style", "format", "layout", "chart"):
                    extra += 1
                    ok += g in exp
            contested += sum(i.contested for i in cl.items)
            items += len(cl.items)
        return {"recall": round(hit / total, 3), "precision": round(ok / max(1, extra), 3), "contested_share": round(contested / max(1, items), 3)}

    result = {"rule_only": score(lambda a, b: a), "rule_plus_model": score(lambda a, b: b),
              "model_calls": sum(b.model_calls for _, _, b in rows), "model_skipped": sorted({b.model_skipped for _, _, b in rows})}
    out = Path(os.environ.get("AS3_SAMPLES", "/tmp")) / "live-ablation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    assert result["rule_only"]["recall"] >= 0.75


JOBS = [
    ("j01", "Make a Word report on an onboarding checklist for new engineers, landscape", "document", ["docx"], None, False),
    ("j02", "PDF on password policy with sections: Purpose, Scope, Rules", "document", ["pdf"], None, False),
    ("j03", "Excel sheet of 25 rows of support tickets with ID, Status and Owner columns", "workbook", ["xlsx"], None, False),
    ("j04", "Word document covering Background, Risks and Next Steps for a data centre move", "document", ["docx"], None, False),
    ("j05", "pdf bana do team offsite agenda ka, page numbers ke saath", "document", ["pdf"], None, False),
    ("j06", "A4 portrait PDF summarising a backup strategy for a small office", "document", ["pdf"], None, False),
    ("j07", "CSV of 15 rows of inventory items with Item, Qty and Location", "workbook", ["csv"], None, False),
    ("j08", "a ppt deck on quarterly results for a bakery chain, 4 slides", "presentation", ["pptx"], None, False),
    ("j09", "letter size word doc about travel reimbursement rules", "document", ["docx"], None, True),
    ("j10", "export the previous answer as a word file", "document", ["docx"], "audit", False),
    ("j11", "Word report on data retention with purple headings", "document", ["docx"], None, True),
    ("j12", "PDF with a red title about fire drill procedure", "document", ["pdf"], None, True),
    ("j13", "excel tracker of 10 tasks with a yellow header row", "workbook", ["xlsx"], None, True),
    ("j14", "docx on code review guidelines, body font Georgia 12pt", "document", ["docx"], None, True),
    ("j15", "presentation on a hiring plan with maroon slide titles", "presentation", ["pptx"], None, True),
    ("j16", "word doc on remote work policy with narrow margins", "document", ["docx"], None, True),
    ("j17", "pdf about lab safety with a table of hazards, table header dark green with white text", "document", ["pdf"], None, True),
    ("j18", "मेरे लिए कार्यालय सुरक्षा पर एक वर्ड फाइल बनाओ जिसमें शीर्षक लाल हो", "document", ["docx"], None, True),
    ("j19", "excel sheet of 12 expenses, make row 5 yellow", "workbook", ["xlsx"], None, True),
    ("j20", "isko word file me de do, professional format", "document", ["docx"], "audit", False),
]


def test_live_end_to_end_jobs(owner, monkeypatch):  # noqa: F811
    from app.engines import artifact as engine

    async def render(work_dir, spec, formats, title_slug, version, effort, **kw):
        report = await asyncio.to_thread(render_version, spec, formats, work_dir, title_slug=title_slug, version=version, effort=effort)
        return report.to_json()

    monkeypatch.setattr(pipeline, "_render_in_subprocess", render)
    pipeline.set_composer(engine.compose_for_pipeline)
    samples = Path(os.environ.get("AS3_SAMPLES", "/tmp/as3-live")) / "live"
    samples.mkdir(parents=True, exist_ok=True)
    md = F.synthetic_audit_markdown(8_000)
    summary = []
    for jid, instruction, kind, formats, source, expect_unmet in JOBS:
        before = _calls["n"]
        material = {"history_text": f"user: {instruction}", "previous_answer": md if source else ""}
        row = _accept(owner, instruction=instruction, kind=kind, formats=formats, format_reason="live eval", generation_id=jid,
                      template_id="generic", material=material)
        fresh = _run(row["id"])
        entry = {"job": jid, "status": fresh["status"], "calls": _calls["n"] - before}
        vdir = Path(store.version_dir(owner, row["artifact_id"], 1))
        if vdir.is_dir():
            dest = samples / jid
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(vdir, dest, ignore=shutil.ignore_patterns("previews"))
            spec = json.loads((vdir / "spec.json").read_text())
            body = spec.get(spec["kind"]) or {}
            headers = [c for b in body.get("blocks") or [] if b.get("type") == "table" for c in b["table"]["columns"]]
            (dest / "request.json").write_text(json.dumps({"instruction": instruction, "expect_unmet": expect_unmet,
                                                           "title": body.get("title", ""), "table_headers": headers[:4]}, ensure_ascii=False))
            report = json.loads((vdir / SC.SELFCHECK_NAME).read_text())
            entry.update(outcome=report["outcome"], unmet=report["unmet"], passed=report["passed"], failed=report["failed"],
                         unverifiable=report["unverifiable"], seconds=report["seconds"], repair=report.get("repair"))
        summary.append(entry)
        print(json.dumps(entry, ensure_ascii=False))
    (samples.parent / "live-jobs.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    assert _calls["n"] <= CALL_CAP
