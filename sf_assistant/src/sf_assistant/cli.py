#!/usr/bin/env python3
"""Ask the object-knowledge agent a question from the terminal (./ask.sh, or python -m sf_assistant ask).

The agent answers ONLY from the object-knowledge JSON files, through the same MCP-style tools
the notebook evaluates, using the local Qwen model (or any OpenAI-compatible endpoint).

By default every knowledge file in knowledge/objects/ is loaded and the agent picks the object(s) each
question is about. --knowledge limits it to the given file(s); with exactly one file it runs the
single-object tools the notebook evaluates.

    ./ask.sh "How do I stop the background check reminders?"
    ./ask.sh                                    # interactive: type questions, empty line or 'exit' quits
    ./ask.sh -v "What values can Status take?"  # also print each tool call and its result
    ./ask.sh --knowledge knowledge/objects/Internal_Interview__c.json "..."   # one object only

Records (local only, sf_assistant engine/tools/agent): a record agent answers the same question at the same time
from a local, read-only DuckDB snapshot of the warehouse (.private/okeval_run/warehouse.duckdb, a docker cp
copy of the orchestrator's /data/warehouse.read.duckdb: preprod sandbox records) through 14 typed
record tools. Code builds every SQL statement with bound parameters; the model never writes SQL.
When the record agent used records, one more model call merges both answers, and code checks
that every number of the merged answer appears in the record results (otherwise the record
agent's answer is printed). The route call comes first: for a record question (RECORDS) the knowledge
agent runs lean (agent/lean.py: field meanings only, at most 3 tool calls and 3 model calls, a short facts
block for the merge); a metadata-only question (NO_RECORDS) or an unclear route gets the full knowledge
agent. The route call is part of the record session's triage (RECORDS_SPEC §22): code spots stored person names
and one small names call runs next to the route call; a question that names a stored person (or a name no stored
person has) goes to the records whatever the route said, and when the record agent gives no answer about them,
code writes the record part from the about_people tool ("-- records: ... code wrote the record part"). Code also
spots the stored record values the question names (value spots: Names, codes, short text of every object); a strong
one sends a NO_RECORDS question to the records too, next to the full knowledge agent ("-- route: ... overruled by
..."), and the record agent's hints say where each named record is stored and which records link to it. When a person count used a lookup whose role does not fit the
question's verb while the knowledge answer names the one that does, a FIELD CHECK goes to the
merge ("-- field check: ..."); an answer that does not name the lookup its person count used gets
a "Lookup used: ..." footer from code. The knowledge answer reaches the merge without what it says
about fetching records (SOQL, "no record data"); a merge that calls an object or field "not
available" while the warehouse holds it is rejected; a final answer without a Source line gets one
from code. The RECORDS block under the answer shows the source and its freshness, then every
query: what was applied, the SQL, the bound params and the rows. -v also prints every record tool
result and the knowledge agent's own answer.

    ./ask.sh --no-records "..."                 # knowledge answer only (no record code, no duckdb)
    ./ask.sh --records-only "..."               # record agent only, no merge
    ./ask.sh --full-knowledge "..."             # merged, but the full knowledge agent for record questions too
    ./ask.sh --refresh-warehouse                # copy a fresh snapshot from the container, then exit
    ./ask.sh --today 2026-10-13 --tz America/New_York "..."   # pin today / the business time zone
    ./ask.sh --warehouse PATH "..."             # another local snapshot file
    ./ask.sh --file .private/questions/records.txt       # every question in a file (+ a .jsonl sidecar)
    ./ask.sh --file .private/questions/records.txt --out .private/runs/my.txt   # choose the output file
    ./ask.sh --max-rows 5 "..."                 # rows shown per query (default 20)

With records on, the --file transcript and its .jsonl sidecar hold record data (names, SQL params, rows),
so they stay in the git-ignored .private/runs/ or .private/okeval_run/: a relative --out elsewhere in the
repository is written under .private/runs/, an absolute one is refused.

Endpoint: OKEVAL_BASE_URL / OKEVAL_MODEL / OKEVAL_API_KEY env vars, default
http://localhost:8000/v1 and Qwen/Qwen3.6-35B-A3B-NVFP4. Records need an endpoint on this machine
(localhost); with any other endpoint they are off. Record settings: OKEVAL_WAREHOUSE,
OKEVAL_RECORDS_TZ, OKEVAL_RECORDS_TODAY, OKEVAL_RECORDS_TIMEOUT_S, OKEVAL_RECORDS_MAX_STEPS,
OKEVAL_RECORDS_MAX_AGE_H (see docs/spec/SPEC.md, "Records (local only, okrec)").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

from sf_assistant import config
from sf_assistant.config import LIMITS, load_settings
from sf_assistant.knowledge.store import knowledge_files
from sf_assistant import pipeline
from sf_assistant.pipeline import (Outcome, _lookup_checks, _queries, _record_facts, _rows_shown,  # noqa: F401
                                   answer, build, result_row, run_question)

HERE = str(config.HOME)                        # the sf_assistant folder (was the notebook folder)
KNOWLEDGE_DIR = str(config.KNOWLEDGE_DIR)      # tests and the web page may point it elsewhere


def open_record_session(cfg, rcfg, knowledge_dir: str | None = None, resolver=None):
    """:func:`sf_assistant.pipeline.open_record_session` with this module's :data:`KNOWLEDGE_DIR` as the default, read
    at call time: as in the notebook folder's ask.py, one ``KNOWLEDGE_DIR`` sets both the knowledge agent's files
    (:func:`main`) and the folder the record catalog reads, so pointing ``cli.KNOWLEDGE_DIR`` elsewhere moves both.
    ``resolver``: the spelling help settings (:func:`main` passes ``settings.resolver``; None: the environment)."""
    if resolver is None:
        return pipeline.open_record_session(cfg, rcfg, knowledge_dir or KNOWLEDGE_DIR)
    return pipeline.open_record_session(cfg, rcfg, knowledge_dir or KNOWLEDGE_DIR, resolver=resolver)
PRIVATE_DIRS = config.TRANSCRIPT_DIRS  # git-ignored (.gitignore: .private/): the only places record transcripts go


def _repo_root(path: str) -> str | None:
    """The git work tree that contains ``path``, or None."""
    d = path
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return None
        d = parent


def transcript_path(out: str | None, question_file: str, records: bool,
                    dirs: tuple[str, ...] | None = None) -> tuple[str, str | None]:
    """``(path, notice)`` of the --file transcript; the .jsonl sidecar goes next to it.

    Default .private/runs/ask_<name>_<time>.txt. With ``records`` the transcript holds record data, so a path
    inside this git repository must be under .private/runs/ or .private/okeval_run/ (git-ignored): a relative
    --out elsewhere is moved under .private/runs/ (``notice`` says so) and an absolute one raises ValueError.
    ``dirs``: those folders, relative to :data:`HERE` (default :data:`PRIVATE_DIRS`; :func:`main` passes
    ``settings.transcript_dirs``).
    """
    dirs = PRIVATE_DIRS if dirs is None else dirs
    if not out:
        return os.path.join(HERE, dirs[0], "ask_{}_{}.txt".format(
            os.path.splitext(os.path.basename(question_file))[0], time.strftime("%Y%m%d-%H%M%S"))), None
    full = os.path.realpath(out)
    if not records:
        return full, None
    if any(full.startswith(os.path.realpath(os.path.join(HERE, d)) + os.sep) for d in dirs):
        return full, None
    root = _repo_root(os.path.realpath(HERE))
    if root is None or not full.startswith(root + os.sep):
        return full, None
    if os.path.isabs(out):
        raise ValueError(f"--out {out} is inside the git repository but not in .private/runs/ or .private/okeval_run/; "
                         "with records on the transcript holds record data. Use a path under .private/runs/ "
                         "(or --no-records).")
    moved = os.path.join(HERE, dirs[0], os.path.basename(out))
    return moved, (f"note: --out {out} is not in .private/runs/ or .private/okeval_run/ (git-ignored); transcripts "
                   f"with records hold record data, so it is written to {os.path.relpath(moved, HERE)}")


def _rows_of(session):
    """Record count of an object, for the RECORDS tables line (None when unknown)."""
    def rows(name: str):
        spec = getattr(getattr(session.tools, "catalog", None), "objects", {}).get(name)
        return getattr(spec, "rows", None)
    return rows


def _quer(n: int) -> str:
    return "query" if n == 1 else "queries"


def ask(text: str, cfg, registry, system, llm, n: int, verbose: bool, session=None, max_rows: int = LIMITS.max_rows,
        records_only: bool = False, records_wanted: bool = False, lean_knowledge: bool = True) -> dict:
    """Answer one question and print it. Returns the summary row (and the JSONL record) for --file runs.

    ``session`` is a ``sf_assistant.agent.record_session.RecordSession`` (records on) or None (records off).
    ``records_wanted`` marks a session where records were asked for but could not be opened.
    ``lean_knowledge`` (merged mode only): make the route call first and, when it says RECORDS, run the lean
    knowledge step (``sf_assistant.agent.lean``, RECORDS_SPEC §21.19) instead of the full knowledge agent. False
    (--full-knowledge) keeps the full knowledge agent for every question, with the route call inside the record
    session.
    """
    o = answer(text, cfg, registry, system, llm, n, session, records_only=records_only,
               lean_knowledge=lean_knowledge)
    print_outcome(o, session, verbose, max_rows, records_wanted=records_wanted)
    return result_row(o, session)


def print_outcome(o: Outcome, session, verbose: bool, max_rows: int = LIMITS.max_rows,
                  records_wanted: bool = False) -> None:
    """Print one answered question: the answer, the status line, field checks, the knowledge agent's tool
    calls (-v: with their results) and the RECORDS block. The print part of ask.py's ``ask()``, unchanged, but for
    one status line of the lean-mode merge by code (2026-10-29): when it kept no field-meaning bullet
    (``Outcome.merge_by`` "code", no error), the line says so instead of "could not merge (unknown error)"; since the
    step-2 fixes of 2026-10-30 it also says when code added only its counted line, or nothing to a Hinglish answer.
    A chart (``SFA_CHARTS=on``, ``Outcome.charts``) prints as text under the answer: its heading, notes and rows.
    """
    text, shown, r, rec, m, status = o.text, o.shown, o.r, o.rec, o.m, o.status
    not_found, checks, kind, k_answer, mode = o.not_found, o.checks, o.kind, o.k_answer, o.mode
    lean_error, shown_registry, records_only, rq, used = o.lean_error, o.shown_registry, o.records_only, o.rq, o.used
    print("\n" + "=" * 72)
    print(f"Q: {text}")
    print("=" * 72)
    print(shown)
    if o.charts:
        from sf_assistant.agent import charts as _charts
        print("\n".join(_charts.text_lines(o.charts)))
    if r is not None and r.error:
        print(f"\nerror: {r.error}")

    status_line = None
    if session is None:
        status_line = "-- records: off" if records_wanted else None
    elif rec.no_records and not_found:
        what = ("the record agent's reply was replaced" if "code wrote the not-found answer" in str(rec.error or "")
                else "the record agent's reply gave a count and was dropped" if getattr(rec, "dropped_answer", "")
                else "the record agent replied NO_RECORDS")
        status_line = (f"-- records: the name was not found ({rec.error}); {what}, so the answer is built from the "
                       "not-found result")
    elif getattr(rec, "code_answer", False):
        status_line = (f"-- records: the record agent gave no usable answer ({rec.agent_error or 'no answer'}); code "
                       "wrote the record part from about_people")
        if m is not None and m.merged:
            status_line += f"; merged with the knowledge answer ({m.seconds:.1f} s)"
    elif rec.no_records and kind == "records":   # pipeline.choose_answer's people answer (evaluation fix 2)
        status_line = (f"-- records: the record agent gave no usable answer ({rec.error or 'no answer'}); code wrote "
                       "the record part from about_people")
    elif rec.no_records:
        status_line = ("-- records: not needed for this question" if not rec.error else
                       f"-- records: no answer from the records ({rec.error})"
                       + ("" if records_only else "; knowledge answer shown"))
    elif records_only:
        status_line = f"-- answer from the record agent only: {used} record {_quer(used)} ({rec.seconds:.1f} s)"
    elif m is not None and m.merged:
        status_line = f"-- answer merged from the knowledge agent and {used} record {_quer(used)} ({m.seconds:.1f} s)"
    elif m is not None and m.rejected_numbers:
        status_line = (f"-- merge rejected (numbers not in the record results: {', '.join(m.rejected_numbers)}); "
                       "the record agent's answer is shown")
    elif m is not None and o.merge_by == "code" and not m.error and kind == "records":   # nothing to add (2026-10-29)
        # step-2 fixes of 2026-10-30: code may still have added its counted line (F8), or added nothing to a Hinglish
        # record answer (F11); a records run has loaded the merge module already
        from sf_assistant.agent import merge

        if merge.code_merge_lines(m.text, rec.answer):
            status_line = ("-- no field-meaning bullet kept (merge by code); code added the line that says what was "
                           "counted")
        elif merge._hinglish(rec.answer, getattr(rec, "table_blocks", None)):
            status_line = ("-- no field-meaning bullet added (merge by code): the record answer is Hinglish and the field "
                           "meanings are English; the record agent's answer is shown")
        else:
            status_line = ("-- no field meaning of the knowledge agent fits the answer (merge by code); the record "
                           "agent's answer is shown")
    elif m is not None:   # what pipeline.choose_answer showed (evaluation fix 2: an empty record answer is never shown)
        shown_what = {"knowledge": "the knowledge answer is shown", "none": "no answer is shown"}.get(
            kind, "the record agent's answer is shown")
        status_line = f"-- could not merge ({m.error or 'unknown error'}); {shown_what}"
    if status_line:
        print("\n" + status_line)
    for c in checks:
        if c.get("kind") == "unsynced":
            print(f"-- field check: the question's word fits {c['object']}.{c['other_field']}, which is not in the "
                  f"warehouse; the record count used {c['field']} (a proxy; passed to the merge)")
            continue
        print(f"-- field check: the question's verb fits {c['object']}.{c['other_field']}; the record count used "
              f"{c['field']} (passed to the merge)")
    if rec is not None and rec.answer and rec.unsupported_numbers and kind == "records":
        print("-- warning: the record answer has numbers not found in the record results: "
              + ", ".join(rec.unsupported_numbers))
    knowledge_block = m is not None and r is not None and (not m.merged or verbose)
    if knowledge_block:
        print("\nKnowledge answer:\n" + (k_answer or "(no answer)"))

    if r is not None:
        head = {"lean": "-- knowledge agent (lean): ", "lean+full": "-- knowledge agent (lean step, then full): "}.get(
            mode, "-- knowledge agent: ") if rec is not None else "-- "
        lead = "\n" if (status_line is None or knowledge_block) else ""
        print(f"{lead}{head}{len(r.tool_calls)} tool call(s), {r.steps} model call(s), {r.latency_s:.1f} s, "
              f"{r.usage.get('prompt_tokens', 0)}+{r.usage.get('completion_tokens', 0)} tokens")
        if lean_error:
            print(f"   the lean step failed ({lean_error}); the full knowledge agent answered")
        for c in r.tool_calls:
            print(f"   {c['name']}({json.dumps(c.get('arguments'), ensure_ascii=False)})"
                  f"  ok={c.get('ok')}  chunks={len(c.get('chunk_ids') or [])}")
            if verbose:
                if mode == "lean" and not c.get("ok"):  # e.g. past the tool budget: it did not run
                    print("     error: " + str(c.get("error") or "")[:300])
                    continue
                res = shown_registry.call(c["name"], c.get("arguments") or {})
                print("     " + json.dumps(res.get("result"), ensure_ascii=False)[:1500])
    if rec is not None:
        from sf_assistant.agent.render import format_records_block, record_trace_lines

        print("\n".join(record_trace_lines(rec, verbose)))
        block = format_records_block(rec, status, max_rows, verbose=verbose, rows_of=_rows_of(session))
        if block:
            print(block)


class _Tee:
    """stdout that also writes to a file (the --file transcript)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, text):
        for s in self.streams:
            s.write(text)
        return len(text)

    def flush(self):
        for s in self.streams:
            s.flush()


def read_questions(path: str) -> list[str]:
    """Non-empty lines that do not start with '#'."""
    with open(path, encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.lstrip().startswith("#")]


def run_file(questions: list[str], ask_one, jsonl_path: str | None = None) -> None:
    """Ask every question, then print a summary table. With ``jsonl_path``: one JSON line per question."""
    rows = []
    sidecar = open(jsonl_path, "w", encoding="utf-8") if jsonl_path else None
    try:
        for n, text in enumerate(questions, 1):
            print(f"\n##### {n}/{len(questions)}")
            s = ask_one(text, n)
            rows.append(s)
            if sidecar is not None and isinstance(s.get("jsonl"), dict):
                sidecar.write(json.dumps({"n": n, **s["jsonl"]}, ensure_ascii=False, default=str) + "\n")
                sidecar.flush()
    finally:
        if sidecar is not None:
            sidecar.close()
    print("\n" + "=" * 110 + "\nSUMMARY\n" + "=" * 110)
    print(f"{'#':>3}  {'answer':9}  {'records':15}  {'rq':>2}  {'audit':5}  {'ktools':>6}  {'rtools':>6}  "
          f"{'s':>6}  question")
    for n, s in enumerate(rows, 1):
        flag = "  <- error: " + s["error"] if s.get("error") else ""
        if s.get("record_error"):
            flag += "  <- record error: " + s["record_error"]
        print(f"{n:>3}  {s.get('answer', 'none'):9}  {s.get('records', 'off'):15}  {s.get('rq', 0):>2}  "
              f"{s.get('audit', '-'):5}  {s.get('ktools', s.get('tools', 0)):>6}  {s.get('rtools', 0):>6}  "
              f"{s.get('seconds', 0.0):>6.1f}  {s.get('question', '')}{flag}")
    count = lambda key, value: sum(s.get(key) == value for s in rows)  # noqa: E731
    total = sum(s.get("seconds", 0.0) for s in rows)
    print(f"\n{len(rows)} questions, {count('answer', 'merged')} merged with records, "
          f"{count('answer', 'records')} answered from records only, {count('answer', 'knowledge')} knowledge only, "
          f"{count('records', 'error')} record failures, {count('answer', 'none')} without an answer, "
          f"avg {total / max(1, len(rows)):.1f} s")


def main(argv: list[str] | None = None, prog: str | None = None) -> int:
    """Run the cli on ``argv`` (default ``sys.argv[1:]``); ``prog`` names it in the usage line (``python -m
    sf_assistant ask`` passes its own name; default: the script name, as argparse does)."""
    p = argparse.ArgumentParser(prog=prog, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("question", nargs="*", help="question text; omit for interactive mode")
    p.add_argument("--knowledge", action="append", metavar="FILE",
                   help="knowledge JSON to load (repeatable); default: every file in knowledge/objects/")
    p.add_argument("-v", "--verbose", action="store_true", help="print each tool result")
    p.add_argument("--no-records", action="store_true", help="knowledge answer only (no record agent, no RECORDS block)")
    p.add_argument("--records-only", action="store_true", help="record agent only (no knowledge agent, no merge)")
    p.add_argument("--full-knowledge", action="store_true",
                   help="merged mode: the full knowledge agent also for record questions (no lean knowledge step)")
    p.add_argument("--max-rows", type=int, default=None, metavar="N",   # None: settings.limits.max_rows
                   help="rows printed per statement in the RECORDS block (default 20)")
    p.add_argument("--warehouse", metavar="PATH",
                   help="local warehouse snapshot (default .private/okeval_run/warehouse.duckdb)")
    p.add_argument("--refresh-warehouse", action="store_true",
                   help="copy a fresh snapshot from the container first (exits after it without a question)")
    p.add_argument("--today", metavar="YYYY-MM-DD", help="pin today for record periods")
    p.add_argument("--tz", metavar="ZONE", help="business time zone for record periods (default: the org's)")
    p.add_argument("--file", metavar="FILE", help="ask every question in FILE (one per line, # = comment)")
    p.add_argument("--out", metavar="FILE",
                   help="with --file: transcript path (default .private/runs/ask_<name>_<time>.txt)")
    a = p.parse_args(argv)
    if a.no_records and a.records_only:
        sys.exit("give either --no-records or --records-only")
    if a.full_knowledge and (a.no_records or a.records_only):
        sys.exit("--full-knowledge is for merged mode; drop --no-records / --records-only")
    if a.no_records and a.refresh_warehouse:
        sys.exit("--refresh-warehouse needs records; drop --no-records")
    if a.file:
        if not os.path.isfile(a.file):
            sys.exit(f"no such question file: {a.file}")
        if a.question:
            sys.exit("give either --file or a question, not both")

    try:   # one Settings object for the run: the model endpoint, the record settings (None with --no-records),
        # the folders (this module's KNOWLEDGE_DIR / PRIVATE_DIRS, read now: tests and the web page point them
        # elsewhere) and the limits; everything below reads the run's values from it
        settings = load_settings(records=not a.no_records, warehouse=a.warehouse, tz=a.tz, today=a.today,
                                 knowledge_dir=KNOWLEDGE_DIR, transcript_dirs=PRIVATE_DIRS)
    except ValueError as exc:
        sys.exit(f"ask.py: {exc}")
    rcfg = settings.records
    knowledge_dir = str(settings.knowledge_dir)
    max_rows = settings.limits.max_rows if a.max_rows is None else a.max_rows
    warmup_s = settings.limits.warmup_timeout_s
    if a.refresh_warehouse:
        from sf_assistant.engine.snapshot import refresh_snapshot

        try:
            refresh_snapshot(rcfg, progress=print)
            refreshed = True
        except Exception as exc:  # noqa: BLE001 - SnapshotError carries the one-line reason
            print(f"refresh failed: {exc}")
            refreshed = False
        if not a.question and not a.file:
            return 0 if refreshed else 1

    paths = a.knowledge or knowledge_files(knowledge_dir)
    if not paths:
        sys.exit("no knowledge files found in knowledge/objects/")
    cfg, objects, registry, system, llm = build(paths, settings)
    print(f"objects: {', '.join(objects)}   model: {cfg.model} @ {cfg.base_url}")
    for line in registry.resolver_lines:   # the spelling help of the knowledge agent (none when it is off)
        print(line)

    session = None
    interactive = not a.file and not a.question
    warmup: dict = {}
    warm_thread = None
    if rcfg is not None:
        if interactive:  # open the snapshot while the first question is typed
            warm_thread = threading.Thread(target=lambda: warmup.update(r=open_record_session(
                cfg, rcfg, knowledge_dir, resolver=settings.resolver)), daemon=True)
            warm_thread.start()
            print("records: opening the snapshot in the background ...")
        else:
            session, lines = open_record_session(cfg, rcfg, knowledge_dir, resolver=settings.resolver)
            print("\n".join(lines))
    if a.records_only and rcfg is not None and not interactive and session is None:
        print("records are off, so --records-only cannot answer")
        return 1
    wanted = rcfg is not None

    def one(text: str, n: int) -> dict:
        return ask(text, cfg, registry, system, llm, n, a.verbose, session, max_rows,
                   records_only=a.records_only and session is not None, records_wanted=wanted,
                   lean_knowledge=not a.full_knowledge)

    try:
        if a.file:
            questions = read_questions(a.file)
            try:
                out, notice = transcript_path(a.out, a.file, records=rcfg is not None, dirs=settings.transcript_dirs)
            except ValueError as exc:
                print(f"ask.py: {exc}")
                return 2
            if notice:
                print(notice)
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            sidecar = (out[:-4] if out.endswith(".txt") else out) + ".jsonl"
            print(f"questions : {len(questions)}  (from {a.file})\nresults   : {out}")
            if session is not None:
                print(f"records   : {sidecar}")
            real = sys.stdout
            with open(out, "w", encoding="utf-8") as fh:
                sys.stdout = _Tee(real, fh)
                try:
                    print(f"questions : {len(questions)}  (from {a.file})\n"
                          f"started   : {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    run_file(questions, one, sidecar if session is not None else None)
                    print(f"finished  : {time.strftime('%Y-%m-%d %H:%M:%S')}")
                finally:
                    sys.stdout = real
            print(f"\nAll answers saved to: {out}")
            return 0
        if a.question:
            one(" ".join(a.question), 1)
            return 0
        n = 0
        while True:
            try:
                text = input("\nquestion> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not text or text.lower() in {"exit", "quit"}:
                break
            if warm_thread is not None:
                warm_thread.join(warmup_s)
                if warm_thread.is_alive():
                    print(f"records off: the snapshot did not open within {warmup_s:.0f} s")
                else:
                    session, lines = warmup.get("r") or (None, ["records off: the snapshot could not be opened"])
                    print("\n".join(lines))
                    if a.records_only and session is None:
                        print("records are off, so --records-only cannot answer")
                        return 1
                warm_thread = None
            n += 1
            one(text, n)
        return 0
    finally:
        if session is not None:
            session.close()


if __name__ == "__main__":
    raise SystemExit(main())
