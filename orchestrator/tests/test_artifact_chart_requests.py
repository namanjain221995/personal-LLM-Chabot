"""The 71 authored chart requests: every oracle binding resolves to values that
equal ground truth (offline, no model). The live run — the model writes the
binding from prompt_guide + the request, code computes the values, `score`
checks VALUES — is opt-in with AS3_LIVE=1 and a reachable engine
(AS3_LLM_BASE_URL, default http://127.0.0.1:8000/v1)."""
from __future__ import annotations

import collections
import json
import os

import pytest

from app.artifacts import chart_data as CD
from app.artifacts import chart_spec as CS
from tests.fixtures import chart_requests as CR
from tests.fixtures.charts import loader

GT = loader.ground_truth()


@pytest.fixture(scope="module")
def tables():
    return [loader.table(n) for n in loader.FILES]


def test_request_set_shape():
    assert len(CR.REQUESTS) == 77
    assert len({r["id"] for r in CR.REQUESTS}) == 77
    langs = collections.Counter(r["lang"] for r in CR.REQUESTS)
    assert sum(n for lang, n in langs.items() if lang != "en") >= 26
    covered = {r["oracle"]["type"] for r in CR.REQUESTS}
    assert covered == set(CS.CHART_TYPES) - {"stacked_horizontal_bar", "stacked_area"} or covered >= set(CS.TIER2_TYPES)


@pytest.mark.parametrize("req", CR.REQUESTS, ids=[r["id"] for r in CR.REQUESTS])
def test_oracle_binding_values_equal_ground_truth(req, tables):
    chart, notes, msg = CD.resolve_chart(CS.Chart.model_validate({"title": req["id"], **req["oracle"]}), tables)
    assert chart is not None, msg
    ok, why = CR.score(req, chart, GT, tables)
    assert ok, why


def test_score_rejects_wrong_values_and_wrong_types(tables):
    req = CR.REQUESTS[0]
    chart, _, _ = CD.resolve_chart(CS.Chart.model_validate({"title": "x", **req["oracle"]}), tables)
    tampered = chart.model_copy(update={"series": [chart.series[0].model_copy(update={"values": [25.0] + chart.series[0].values[1:]})]})
    assert CR.score(req, tampered, GT, tables)[0] is False
    assert CR.score(req, chart.model_copy(update={"type": "line"}), GT, tables)[0] is False


LIVE = os.environ.get("AS3_LIVE") == "1"

SYSTEM = (
    "You write ONE chart specification as JSON for a document tool. The chart is drawn by code from a table; "
    "you choose the table, the columns, the aggregation, the chart type and any style the person asked for. "
    "Never write numbers for categories or series.\n\n{guide}\n\nReturn only the JSON object."
)


def live_chart(req, tables, *, base_url, model, timeout=60.0):
    import httpx

    table = next(t for t in tables if t.id == req["table"])
    guide = CS.prompt_guide("document", [table])
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM.format(guide=guide)}, {"role": "user", "content": req["text"]}],
        "temperature": 0.0,
        "max_tokens": 700,
        "response_format": {"type": "json_schema", "json_schema": {"name": "chart", "schema": CS.guided_schema(tables=[table])}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    r = httpx.post(f"{base_url}/chat/completions", json=body, timeout=timeout)
    r.raise_for_status()
    text = r.json()["choices"][0]["message"]["content"]
    raw = json.loads(text)
    chart, _style_notes = CS.chart_from_model(raw)
    chart, _repair_notes = CD.repair_binding(chart, tables, req["text"])
    resolved, notes, msg = CD.resolve_chart(chart, tables)
    return raw, resolved, msg


@pytest.mark.skipif(not LIVE, reason="live engine evaluation is opt-in (AS3_LIVE=1)")
def test_live_requests_work_with_values_checked(tables):
    base_url = os.environ.get("AS3_LLM_BASE_URL", "http://127.0.0.1:8000/v1")
    model = os.environ.get("AS3_LLM_MODEL", "Qwen/Qwen3.6-35B-A3B-NVFP4")
    limit = int(os.environ.get("AS3_LIVE_LIMIT", "60"))
    results = []
    # Every non-English/typo request first, then English, up to the call budget.
    ordered = [r for r in CR.REQUESTS if r["lang"] != "en"] + [r for r in CR.REQUESTS if r["lang"] == "en"]
    for req in ordered[:limit]:
        try:
            raw, chart, msg = live_chart(req, tables, base_url=base_url, model=model)
            ok, why = CR.score(req, chart, GT, tables) if chart is not None else (False, msg)
        except Exception as exc:  # a malformed answer is a miss, not a crash
            ok, why = False, f"{type(exc).__name__}: {str(exc)[:120]}"
        results.append((req["id"], req["lang"], ok, why))
    out = os.environ.get("AS3_LIVE_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=1, ensure_ascii=False)
    works = sum(1 for r in results if r[2])
    assert works >= 0.77 * len(results), results
