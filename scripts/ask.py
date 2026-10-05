#!/usr/bin/env python3
"""Ask the new Salesforce pipeline a question, exactly as /chat would.

Runs inside the orchestrator container through `sfk_bridge.try_answer`, the
same seam /chat calls, so what you see is what a user would get. Use
scripts/ask.sh from the host; it stages the code and runs this.

    ask.sh "How many internal interviews are scheduled?"
    ask.sh                       # interactive: type questions, blank line quits
    ask.sh -v "..."              # also print every trace event
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

sys.path.insert(0, "/tmp/vendor")
from app.engines import sfk_bridge                                 # noqa: E402
from graphrag.trace_store import TraceStore                        # noqa: E402

TRACE_DB = os.environ["SFK_TRACE_DB"]
_RUNNER = asyncio.Runner()


class Request:
    sf_live = False
    pdf_data = None
    image_data = None
    test_case_id = None

    def __init__(self, text: str) -> None:
        self.text = text


def latest_trace() -> dict | None:
    store = TraceStore(TRACE_DB)
    try:
        rows = store.recent(1)
        return store.get(rows[0]["trace_id"]) if rows else None
    finally:
        store.close()


def ask(question: str, verbose: bool) -> None:
    emitted: list = []

    async def emit(kind, data):
        emitted.append((kind, data))

    started = time.perf_counter()
    # One event loop for the whole run, reused. asyncio.run() per question
    # built a new loop -- and a new worker thread -- every time.
    outcome = _RUNNER.run(sfk_bridge.try_answer(question, emit=emit,
                                                request=Request(question)))
    seconds = time.perf_counter() - started
    trace = latest_trace()

    print("\n" + "=" * 72)
    print(f"QUESTION  {question}")
    print("=" * 72)

    if outcome is None:
        print("\nNEW PIPELINE DID NOT ANSWER - the existing engine would.")
        if trace:
            print(f"  route   : {trace.get('route')}")
            if trace.get("route") == "NONE":
                print("  why     : not a Salesforce data/metadata question "
                      "(conversational)")
            semantic = (trace.get("grounded_json") or {}).get("semantic") or {}
            if semantic:
                print(f"  linking : decided by {semantic.get('mode')}"
                      f" ({semantic.get('duration_ms')} ms)")
            for failure in trace.get("grounding_failures") or []:
                print(f"  why     : {failure.get('code')} - {failure.get('detail')}")
                if failure.get("candidates"):
                    print(f"            candidates: {failure['candidates'][:5]}")
            if trace.get("record_error"):
                print(f"  why     : record query failed: {trace['record_error']}")
        print(f"  took    : {seconds:.1f} s")
        return

    meta = next((d for k, d in emitted if k == "meta"), {})
    print("\nANSWER")
    for line in outcome.answer.splitlines():
        print(f"  {line}")

    print("\nHOW IT GOT THERE")
    extraction = (trace or {}).get("extraction_json") or {}
    if extraction.get("family"):
        _explain_ir(extraction, trace or {}, meta, seconds, verbose)
        return
    if extraction:
        entities = [e.get("name") for e in extraction.get("business_entities") or []]
        print(f"  1 intent    : {extraction.get('intent')} / {extraction.get('action')}"
              f"   entities={entities}")
    print(f"  2 route     : {meta.get('sfk_route')}")
    print(f"  3-4 object  : {(trace or {}).get('grounded_primary_object')}"
          f"   schema tier={(trace or {}).get('schema_served_from')}")
    grounded = (trace or {}).get("grounded_json") or {}
    semantic = grounded.get("semantic") or {}
    if semantic:
        how = ("retrieval alone (fast path, no model call)"
               if semantic.get("mode") == "fast_path"
               else f"main model, {semantic.get('duration_ms')} ms")
        print(f"    linking   : {semantic.get('linker_mode')} -> decided by {how}")
        for mapping in grounded.get("filter_mappings") or []:
            print(f"    filter    : {mapping.get('object_api_name')}.{mapping.get('field')}"
                  f" {mapping.get('operator')} {mapping.get('value')!r}")
        for mapping in grounded.get("requested_attribute_mappings") or []:
            print(f"    shows     : {mapping.get('field_path')}")
        for mapping in grounded.get("entity_mappings") or []:
            if mapping.get("source_field"):
                print(f"    joins     : {mapping.get('source_field')} -> "
                      f"{mapping.get('target_object')}")
        if semantic.get("rejected"):
            print(f"    rejected  : {semantic['rejected']}")
    print(f"  5 sql       : {(meta.get('sql') or '').replace(chr(10), ' ')}")
    total = meta.get("total_count")
    print(f"    rows      : {meta.get('row_count')}"
          + (f" of {total}" if total is not None else "")
          + ("  (truncated)" if meta.get("truncated") else ""))
    print(f"  6 model     : {meta.get('model')}   grounded={meta.get('grounded')}")
    prov = meta.get("provenance") or {}
    print(f"    data from : {prov.get('environment')} "
          f"(sandbox={prov.get('is_sandbox')}), synced "
          f"{prov.get('age_minutes')} min ago, {prov.get('freshness')}")

    if trace:
        print("\nTIMING (ms)")
        for column in ("extraction_duration_ms", "discovery_duration_ms",
                       "linking_duration_ms", "record_query_duration_ms",
                       "answer_duration_ms"):
            if trace.get(column) is not None:
                print(f"  {column.replace('_duration_ms', ''):14s} {trace[column]:>8.0f}")
        print(f"  {'total':14s} {seconds * 1000:>8.0f}")
        print(f"\ntrace_id  {trace['trace_id']}")
        if verbose:
            print("\nEVENTS")
            for event in trace["events"]:
                duration = "" if event["duration_ms"] is None else f"{event['duration_ms']} ms"
                print(f"  {event['sequence_number']:02d} {event['stage']:40s} "
                      f"{event['status']:8s} {duration}")


def _explain_ir(ir: dict, trace: dict, meta: dict, seconds: float,
                verbose: bool) -> None:
    """The semantic-IR path (step 8): meaning, grounding, subplans."""
    entities = [f"{e.get('ref')}={e.get('concept')}({e.get('role')})"
                for e in ir.get("entities") or []]
    print(f"  1 meaning   : {ir.get('family')} / {ir.get('output_mode')}"
          f"   entities={entities}   ({ir.get('duration_ms')} ms"
          f"{', retried' if ir.get('retried') else ''})")
    for f in ir.get("filters") or []:
        right = f.get("right") or {}
        value = (f"{right.get('entity')}.{right.get('concept')}"
                 if right.get("type") == "field_reference" else repr(right.get("value")))
        print(f"    filter    : {f.get('entity')}.{f.get('concept')} {f.get('operator')} {value}")
    for m in ir.get("measures") or []:
        print(f"    measure   : {m.get('op')}({m.get('entity')}.{m.get('concept') or '*'})")
    for t in ir.get("temporal") or []:
        print(f"    when      : {t.get('expression')!r} on {t.get('entity')}.{t.get('concept') or 'created'}")
    print(f"  2 route     : {meta.get('sfk_route')}   capabilities={ir.get('capabilities')}")
    grounded = trace.get("grounded_json") or {}
    if grounded:
        print(f"  3-4 base    : {grounded.get('base')}   objects={grounded.get('objects')}")
        for ref, hops in (grounded.get("paths") or {}).items():
            print(f"    path      : {ref}: {' / '.join(hops)}")
        for text in grounded.get("filters") or []:
            print(f"    where     : {text}")
        for text in grounded.get("measures") or []:
            print(f"    measure   : {text}")
        for text in grounded.get("dimensions") or []:
            print(f"    group by  : {text}")
        for text in grounded.get("existence") or []:
            print(f"    exists    : {text}")
        for period in grounded.get("periods") or []:
            print(f"    period    : {period}")
        semantic = grounded.get("semantic") or {}
        if semantic:
            print(f"    grounding : {semantic.get('mode')} ({semantic.get('duration_ms')} ms)")
    stages = model_stages(trace)
    if stages:
        print("  model stages (main model decides each):")
        for stage, d in stages.items():
            via = f" via {d['combined_with']}" if d.get("combined_with") else ""
            print(f"    {stage:14s} called={d.get('model_called')} ok={d.get('ok')}"
                  f" {d.get('duration_ms') or 0:>5} ms  candidates={d.get('candidate_count')}"
                  f"  retries={d.get('retries')}{via}")
    for line in (meta.get("sql") or "").split("\n;\n"):
        if line.strip():
            print(f"  5 sql       : {line.replace(chr(10), ' ')}")
    total = meta.get("total_count")
    print(f"    rows      : {meta.get('row_count')}"
          + (f" of {total}" if total is not None else "")
          + ("  (truncated)" if meta.get("truncated") else ""))
    print(f"  6 model     : {meta.get('model')}   grounded={meta.get('grounded')}")
    prov = meta.get("provenance") or {}
    print(f"    data from : {prov.get('environment')} "
          f"(sandbox={prov.get('is_sandbox')}), synced "
          f"{prov.get('age_minutes')} min ago, {prov.get('freshness')}")
    print("\nTIMING (ms)")
    for event in trace.get("events") or []:
        if event["stage"] in ("IR_COMPILED", "IR_GROUNDED") and event["duration_ms"] is not None:
            print(f"  {event['stage'].lower():14s} {event['duration_ms']:>8.0f}")
    for column in ("record_query_duration_ms", "answer_duration_ms"):
        if trace.get(column) is not None:
            print(f"  {column.replace('_duration_ms', ''):14s} {trace[column]:>8.0f}")
    print(f"  {'total':14s} {seconds * 1000:>8.0f}")
    if trace.get("trace_id"):
        print(f"\ntrace_id  {trace['trace_id']}")
    if verbose:
        print("\nEVENTS")
        for event in trace.get("events") or []:
            duration = "" if event["duration_ms"] is None else f"{event['duration_ms']} ms"
            print(f"  {event['sequence_number']:02d} {event['stage']:40s} "
                  f"{event['status']:8s} {duration}")


STAGES = ("intent", "routing", "discovery", "linking", "planning", "interpretation",
          "answer")


def model_stages(trace: dict) -> dict[str, dict]:
    """The MODEL_STAGE records of one trace, last record per stage."""
    out: dict[str, dict] = {}
    for event in trace.get("events") or []:
        if event["stage"] == "MODEL_STAGE":
            out[event["details"].get("stage")] = event["details"]
    return out


def run_file(path: str, verbose: bool) -> int:
    """Every question in a file, one pipeline build, then a summary table."""
    questions = [line.strip() for line in open(path, encoding="utf-8")
                 if line.strip() and not line.lstrip().startswith("#")]
    rows = []
    for number, question in enumerate(questions, 1):
        print(f"\n##### {number}/{len(questions)}")
        started = time.perf_counter()
        try:
            ask(question, verbose)
        except Exception as exc:                        # noqa: BLE001
            print(f"  ERROR {type(exc).__name__}: {exc}")
        trace = latest_trace() or {}
        grounded = trace.get("grounded_json") or {}
        semantic = grounded.get("semantic") or {}
        failures = [f.get("code") for f in trace.get("grounding_failures") or []]
        answered = bool(trace.get("answer_text"))
        signals = trace.get("signals") or {}
        applicable = signals.get("applicable_model_stages") or []
        stages = model_stages(trace)
        called = [s for s in applicable if (stages.get(s) or {}).get("model_called")]
        error = next((e["details"].get("error") for e in trace.get("events") or []
                      if e["stage"] == "PIPELINE_COMPLETED"), None)
        rows.append((number, question, answered,
                     trace.get("grounded_primary_object") or "-",
                     f"{len(called)}/{len(applicable)} model",
                     failures[0] if failures else ("" if answered else error or
                                                   trace.get("route") or ""),
                     int((time.perf_counter() - started) * 1000),
                     applicable, called, stages))

    print("\n" + "=" * 110)
    print("SUMMARY")
    print("=" * 110)
    print(f"{'#':>3}  {'result':9} {'object':24} {'stages':17} {'ms':>6}  question / why")
    for number, question, answered, obj, mode, why, ms, *_ in rows:
        result = "answered" if answered else "declined"
        tail = question if answered else f"{question}   <- {why}"
        print(f"{number:>3}  {result:9} {obj:24} {mode:17} {ms:>6}  {tail}")
    total = len(rows)
    done = sum(1 for r in rows if r[2])
    print("-" * 110)
    print(f"answered {done}/{total}   avg {sum(r[6] for r in rows) // max(1, total)} ms")
    print("main-model participation (called / applicable):")
    for stage in STAGES:
        applicable = sum(1 for r in rows if stage in r[7])
        called = sum(1 for r in rows if stage in r[8])
        ms = [r[9][stage].get("duration_ms") or 0 for r in rows
              if stage in r[9] and not r[9][stage].get("combined_with")]
        retries = sum(int((r[9].get(stage) or {}).get("retries") or 0) for r in rows)
        avg = f"avg {sum(ms) // max(1, len(ms))} ms" if ms else "combined call"
        print(f"  {stage:14s} {called:>4}/{applicable:<4} {avg:16s} retries {retries}")
    bypass = [r[0] for r in rows if set(r[7]) - set(r[8])]
    print(f"questions with an applicable stage NOT decided by the main model: "
          f"{len(bypass)}" + (f"  {bypass[:20]}" if bypass else ""))
    return 0


def main() -> int:
    args = sys.argv[1:]
    verbose = "-v" in args
    args = [a for a in args if a != "-v"]
    if not sfk_bridge.available():
        print("new pipeline unavailable:", json.dumps(sfk_bridge.state(), default=str))
        return 1
    if args and args[0] == "--file":
        return run_file(args[1], verbose)
    if args:
        ask(" ".join(args), verbose)
        return 0
    print("Ask a Salesforce question (blank line to quit).")
    while True:
        try:
            question = input("\n> ").strip()
        except EOFError:
            break
        if not question:
            break
        ask(question, verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
