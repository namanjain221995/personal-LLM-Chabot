"""LIVE end-to-end (opt-in: AS3_LIVE_E2E=1 and OPENAI_BASE_URL pointing at the
engine): the three paraphrased production prompts after a long audit answer,
through the real /chat route, the real composer and the real renderer. Each
must end in a completed DOCX file card, with at most 10 engine calls in all.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(os.environ.get("AS3_LIVE_E2E") != "1", reason="live engine calls are opt-in (AS3_LIVE_E2E=1)")

REPORT = (
    "# Sales Onboarding Audit Report\n\n## 1. Executive summary\nOnboarding for new sales hires takes 19 working days on average; "
    "the target is 10.\n\n## 2. Findings\n| # | Area | Finding | Severity |\n|---|---|---|---|\n"
    "| 1 | Accounts | Laptop and CRM access arrive on day 6 | High |\n| 2 | Training | Product modules are not sequenced | Medium |\n"
    "| 3 | Buddy program | Only 40% of hires get a buddy | Medium |\n\n## 3. Recommendations\n1. Provision access before day 1.\n"
    "2. Sequence the modules.\n3. Assign a buddy at offer acceptance.\n\n## 4. Conclusion\nFixing access alone saves about five days.\n"
)
PROMPTS = [
    "can u just give it in docs.. in standard n classy formatt? provide dox",
    "just give it in docs in a standard and classy format, provide a dox file",
    "isko docx me dedo classy format me",
]


def _events(text):
    out = []
    for block in text.strip().split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) >= 2 and lines[0].startswith("event: "):
            out.append((lines[0][7:], json.loads(lines[1][6:])))
    return out


def test_live_the_three_production_prompts_end_in_a_docx_card(tmp_path, monkeypatch):
    from app import llm
    from app.artifacts import pipeline
    from app.config import settings
    from app.main import app

    monkeypatch.setattr(settings, "reports_dir", str(tmp_path / "reports"))
    monkeypatch.setattr(settings, "artifact_min_free_mb", 1)
    monkeypatch.setattr(settings, "video_pace_max_wait_s", 0.0)
    calls = {"json": 0, "stream": 0}
    real_json, real_stream = llm.json_completion, llm.stream_chat_events

    async def counted_json(*a, **k):
        calls["json"] += 1
        return await real_json(*a, **k)

    async def counted_stream(*a, **k):
        calls["stream"] += 1
        async for ev in real_stream(*a, **k):
            yield ev

    monkeypatch.setattr(llm, "json_completion", counted_json)
    monkeypatch.setattr(llm, "stream_chat_events", counted_stream)
    results = []
    with TestClient(app) as client:
        for i, prompt in enumerate(PROMPTS):
            body = {"messages": [{"role": "user", "content": "audit our sales onboarding process and write a detailed report"},
                                 {"role": "assistant", "content": REPORT}, {"role": "user", "content": prompt}],
                    "mode": "assistant", "conversation_id": f"as3-live-{i}", "intent_id": f"int-as3-live-{i}", "effort": "fast",
                    "web_search": "off"}
            resp = client.post("/chat", json=body)
            assert resp.status_code == 200
            events = _events(resp.text)
            final = [d for k, d in events if k == "meta"][-1]
            tokens = "".join(d["text"] for k, d in events if k == "token")
            results.append({"prompt": prompt, "route": final.get("route"), "tokens": tokens,
                            "files": [f["format"] for f in (final.get("artifacts") or [{}])[0].get("files", [])],
                            "status": (final.get("artifacts") or [{}])[0].get("status")})
    pipeline.reset_for_tests()
    out = os.environ.get("AS3_LIVE_E2E_OUT")
    if out:
        json.dump({"results": results, "calls": calls}, open(out, "w"), indent=1, ensure_ascii=False)
    for r in results:
        assert r["route"] == "artifact" and r["status"] in ("completed", "completed_with_warnings") and "docx" in r["files"], r
    assert calls["json"] + calls["stream"] <= 10, calls
