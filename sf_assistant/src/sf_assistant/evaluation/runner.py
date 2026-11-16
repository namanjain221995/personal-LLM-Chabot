"""Evaluation runner: ask a level of the sets, grade every answer, write the report, compare with the last run.

    python -m sf_assistant.evaluation run --set all --level quick         # ~35 questions, merged
    python -m sf_assistant.evaluation run --set lookup --mode both        # every lookup question, both modes
    python -m sf_assistant.evaluation run --level full                    # every set in the modes of the rounds

It replaces the notebook and the ``./ask.sh --file`` rounds. For each (set, mode) of the plan (:func:`sets.plan`) it
writes the questions to a file and runs ``python -m sf_assistant ask --file ... [--records-only] --today ...`` (the
same cli, prompts, record session and sidecar as the rounds), then grades every sidecar row with
:mod:`sf_assistant.evaluation.autograde` (no model call in the grading; the expected SQL is re-run read-only on the same
warehouse). The answers follow ``SFA_RESOLVER`` as every cli run does (the spelling help, on by default;
``SFA_RESOLVER=off`` answers without it); the golden tool-call replay of the grading never uses it.
One folder per run under ``sets.yaml`` ``results_dir`` (``.private/runs/eval/<run id>/``, git-ignored):

* ``results.jsonl`` - one line per (question, mode): verdict, reasons, checks, the answer, seconds, record queries;
* ``report.md`` - scores, latency medians, the comparison with the previous run of the same set / level / mode,
  the questions to review and every question with its reasons;
* ``summary.json`` - the counts and latencies the next run compares with;
* ``<set>_<mode>.txt`` / ``.jsonl`` / ``.log`` - the cli transcript, its sidecar and its output.

Everything there holds real names and record data: it stays in ``.private``.
"""
from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from sf_assistant.evaluation.autograde import NOT_RUN, VERDICTS, Grade, answer_text, grade
from sf_assistant.evaluation.sets import EvalItem, SetsConfig, plan

__all__ = ["AnswerBatch", "RunResult", "cli_batch", "run", "grade_rows", "write_outputs", "find_previous",
           "compare", "render_report", "latency"]

#: ``answer_batch(questions, mode, out_base) -> rows``: answers the questions in order and returns their sidecar rows
#: (the JSONL records of ``ask --file``; ``out_base`` is the path stem for its transcript files)
AnswerBatch = Callable[[list[str], str, Path], list[dict]]

ASK_CMD = (sys.executable, "-m", "sf_assistant", "ask")


def cli_batch(today: str | None = None, warehouse: str | None = None, cmd: Iterable[str] = ASK_CMD,
              env: dict | None = None, timeout_s: float | None = None) -> AnswerBatch:
    """The production :data:`AnswerBatch`: one ``ask --file`` subprocess per (set, mode), as the rounds were run.

    ``cmd`` is the cli command (tests pass a wrapper that runs the same cli with the offline mock LLM)."""
    def answer_batch(questions: list[str], mode: str, out_base: Path) -> list[dict]:
        qfile = out_base.with_suffix(".questions.txt")
        qfile.write_text("# questions of an evaluation run (python -m sf_assistant.evaluation)\n"
                         + "".join(q + "\n" for q in questions), encoding="utf-8")
        args = [*cmd, "--file", str(qfile), "--out", str(out_base.with_suffix(".txt"))]
        if mode == "records":
            args.append("--records-only")
        if today:
            args += ["--today", today]
        if warehouse:
            args += ["--warehouse", warehouse]
        with open(out_base.with_suffix(".log"), "w", encoding="utf-8") as log:
            proc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, env=env, timeout=timeout_s, check=False)
        sidecar = out_base.with_suffix(".jsonl")
        if proc.returncode != 0 or not sidecar.is_file():
            raise RuntimeError(f"ask --file exited {proc.returncode} without a sidecar (records off?); see "
                               f"{out_base.with_suffix('.log')}")
        with open(sidecar, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    return answer_batch


# ------------------------------------------------------------------------------------------------ grading
@dataclass
class RunResult:
    """One run (or grading of saved sidecars): its folder, one result per (item, mode) and the summary."""

    run_dir: Path
    results: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    previous: dict | None = None
    comparison: dict | None = None


def _match_rows(items: list[EvalItem], rows: list[dict]) -> list[dict | None]:
    """The sidecar row of each item: by position (``n``) when the text matches, else by exact question text."""
    by_text: dict[str, dict] = {}
    for r in rows:
        by_text.setdefault(r.get("question", ""), r)
    out: list[dict | None] = []
    for k, it in enumerate(items):
        row = next((r for r in rows if r.get("n") == k + 1), None)
        if row is None or row.get("question") != it.question:
            row = by_text.get(it.question)
        out.append(row)
    return out


def grade_rows(items: list[EvalItem], rows: list[dict], mode: str, con=None, tools=None) -> list[dict]:
    """Grade the sidecar ``rows`` of ``items`` (one mode) into result records."""
    out = []
    for it, row in zip(items, _match_rows(items, rows)):
        g: Grade = grade(it, row, mode, con=con, tools=tools)
        queries = (row or {}).get("queries") or []
        out.append({"set": it.set, "id": it.id, "n": it.n, "mode": mode, "question": it.question,
                    "verdict": g.verdict, "reasons": g.reasons, "checks": g.checks, "data": g.data,
                    "answer_kind": (row or {}).get("answer_kind"), "seconds": (row or {}).get("seconds"),
                    "record_queries": sum(1 for q in queries if q.get("tool") not in (None, "")),
                    "answer": answer_text(row) if row else None})
    return out


def latency(results: list[dict]) -> dict:
    """median / mean / p90 (nearest rank) / max seconds and the median record queries of ``results``."""
    secs = sorted(float(r["seconds"]) for r in results if isinstance(r.get("seconds"), (int, float)))
    if not secs:
        return {}
    p90 = secs[min(len(secs) - 1, max(0, -(-9 * len(secs) // 10) - 1))]
    qs = [r.get("record_queries") or 0 for r in results]
    return {"n": len(secs), "median": round(statistics.median(secs), 1), "mean": round(statistics.fmean(secs), 1),
            "p90": round(p90, 1), "max": round(secs[-1], 1), "queries_median": statistics.median(qs) if qs else 0}


def _groups(results: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in results:
        out.setdefault(f"{r['set']}/{r['mode']}", []).append(r)
    return out


def _counts(rs: list[dict]) -> dict:
    """Verdict counts; ``n`` counts the answered questions only (a ``not_run`` question is in no score)."""
    not_run = sum(r["verdict"] == NOT_RUN for r in rs)
    return {v: sum(r["verdict"] == v for r in rs) for v in VERDICTS} | {"not_run": not_run, "n": len(rs) - not_run}


def _summary(results: list[dict], meta: dict) -> dict:
    groups = _groups(results)
    counts = {k: _counts(rs) for k, rs in groups.items()}
    return {**meta, "counts": counts, "total": _counts(results), "latency": {k: latency(rs) for k, rs in groups.items()}}


# ------------------------------------------------------------------------------------------------ comparison
def find_previous(results_root: Path, set_name: str, level: str, mode: str | None, before: str,
                  kind: str = "run") -> dict | None:
    """The newest earlier summary of the same ``kind``, set argument, level and mode argument (None when none). A run
    with a failed group (``errors``) is no baseline: its questions were not all answered."""
    best = None
    if not results_root.is_dir():
        return None
    for path in results_root.glob("*/summary.json"):
        try:
            s = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (s.get("kind"), s.get("set"), s.get("level"), s.get("mode")) != (kind, set_name, level, mode):
            continue
        if s.get("run_id", "") >= before or s.get("errors"):
            continue
        if best is None or s["run_id"] > best["run_id"]:
            best = s | {"_dir": str(path.parent)}
    return best


_RANK = {"correct": 3, "needs_review": 2, "partial": 1, "wrong": 0}


def compare(previous: dict, results: list[dict]) -> dict:
    """Per-group counts before / now, latency medians before / now and every question whose verdict changed."""
    prev_results: dict[tuple, str] = {}
    path = Path(previous.get("_dir", "")) / "results.jsonl"
    if path.is_file():
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    r = json.loads(line)
                    prev_results[(r["set"], r["id"], r["mode"])] = r["verdict"]
    changes = []
    for r in results:
        before = prev_results.get((r["set"], r["id"], r["mode"]))
        if NOT_RUN in (before, r["verdict"]):   # not answered on one side: no change to report
            continue
        if before is not None and before != r["verdict"]:
            direction = "better" if _RANK[r["verdict"]] > _RANK[before] else "worse"
            changes.append({"set": r["set"], "id": r["id"], "mode": r["mode"], "before": before,
                            "now": r["verdict"], "direction": direction})
    now = _summary(results, {})
    return {"previous_run": previous.get("run_id"), "counts_before": previous.get("counts", {}),
            "counts_now": now["counts"], "latency_before": previous.get("latency", {}), "latency_now": now["latency"],
            "changes": changes, "new": sum(1 for r in results if (r["set"], r["id"], r["mode"]) not in prev_results),
            "not_run": sum(1 for r in results if r["verdict"] == NOT_RUN)}


# ------------------------------------------------------------------------------------------------ report
def _cell(text: str, limit: int = 300) -> str:
    t = " ".join(str(text).split()).replace("|", "\\|")
    return t if len(t) <= limit else t[: limit - 3] + "..."


def _fmt(d: dict) -> str:
    return f"{d['correct']} / {d['partial']} / {d['wrong']} / {d.get('needs_review', 0)}" + (
        f" (+{d['not_run']} not run)" if d.get("not_run") else "")


def render_report(summary: dict, results: list[dict], comparison: dict | None) -> str:
    """``report.md`` of a run."""
    lines = [f"# Evaluation {summary.get('run_id')}", "",
             "LOCAL ONLY (git-ignored .private): this report holds real names and record data.", ""]
    meta = [("kind", summary.get("kind")), ("set", summary.get("set")), ("level", summary.get("level")),
            ("mode", summary.get("mode") or "the level's modes"), ("today", summary.get("today")),
            ("warehouse", summary.get("warehouse")), ("model", summary.get("model")),
            ("started", summary.get("started")), ("finished", summary.get("finished")),
            ("questions", summary.get("total", {}).get("n"))]
    lines += [f"- {k}: {v}" for k, v in meta if v not in (None, "")]
    if summary.get("errors"):
        lines += ["", "**Failed groups** (their questions are `not_run`, in no score; this run is no baseline for the "
                  "next one):", ""] + [f"- {e}" for e in summary["errors"]]
    lines += ["", "## Scores", "", "| set / mode | n | correct | partial | wrong | needs_review | not run | correct % | "
              "median s | p90 s | max s | queries (median) |", "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for key, c in summary.get("counts", {}).items():
        lat = summary.get("latency", {}).get(key, {})
        pct = f"{100 * c['correct'] / c['n']:.0f}%" if c["n"] else "-"
        lines.append(f"| {key} | {c['n']} | {c['correct']} | {c['partial']} | {c['wrong']} | {c['needs_review']} | "
                     f"{c.get('not_run', 0)} | {pct} | {lat.get('median', '-')} | {lat.get('p90', '-')} | "
                     f"{lat.get('max', '-')} | {lat.get('queries_median', '-')} |")
    t = summary.get("total", {})
    if t:
        lines.append(f"| **all** | {t['n']} | {t['correct']} | {t['partial']} | {t['wrong']} | {t['needs_review']} | "
                     f"{t.get('not_run', 0)} | {100 * t['correct'] / max(1, t['n']):.0f}% | | | | |")
    if summary.get("agreement"):
        lines += ["", "## Agreement with the hand grades", "", "| set / mode | n | agree | disagree | needs_review | "
                  "agreement (decided) |", "|---|---|---|---|---|---|"]
        for key, a in summary["agreement"].items():
            lines.append(f"| {key} | {a['n']} | {a['agree']} | {a['disagree']} | {a['needs_review']} | "
                         f"{a['agreement']} |")
    lines += ["", "## Compared with the previous run", ""]
    if not comparison:
        lines.append("No earlier run of the same set, level and mode.")
    else:
        lines += [f"Previous run: {comparison['previous_run']}; {len(comparison['changes'])} verdict(s) changed, "
                  f"{comparison['new']} question(s) not in it.", "",
                  "| set / mode | before c / p / w / r | now c / p / w / r | median s before -> now |", "|---|---|---|---|"]
        for key, c in comparison["counts_now"].items():
            b = comparison["counts_before"].get(key)
            lb = comparison["latency_before"].get(key, {}).get("median", "-")
            ln = comparison["latency_now"].get(key, {}).get("median", "-")
            lines.append(f"| {key} | {_fmt(b) if b else '-'} | {_fmt(c)} | {lb} -> {ln} |")
        if comparison["changes"]:
            lines += ["", "| id | set / mode | before | now | |", "|---|---|---|---|---|"]
            lines += [f"| {ch['id']} | {ch['set']}/{ch['mode']} | {ch['before']} | {ch['now']} | {ch['direction']} |"
                      for ch in comparison["changes"]]
    review = [r for r in results if r["verdict"] == "needs_review"]
    lines += ["", "## Needs review", ""]
    lines += [f"- {r['id']} ({r['set']}/{r['mode']}): {_cell('; '.join(r['reasons']), 400)}" for r in review] or \
        ["None."]
    lines += ["", "## Every question", "", "| id | set / mode | verdict | reasons | s | queries | question |",
              "|---|---|---|---|---|---|---|"]
    for r in results:
        hand = f" (hand: {r['hand']})" if r.get("hand") else ""
        secs = f"{r['seconds']:.1f}" if isinstance(r.get("seconds"), (int, float)) else "-"
        lines.append(f"| {r['id']} | {r['set']}/{r['mode']} | {r['verdict']}{hand} | {_cell('; '.join(r['reasons']))} "
                     f"| {secs} | {r.get('record_queries', 0)} | {_cell(r['question'], 120)} |")
    return "\n".join(lines) + "\n"


def write_outputs(run_dir: Path, results: list[dict], summary: dict, comparison: dict | None) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with open(run_dir / "results.jsonl", "w", encoding="utf-8") as fh:
        for r in results:
            fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False, default=str),
                                          encoding="utf-8")
    (run_dir / "report.md").write_text(render_report(summary, results, comparison), encoding="utf-8")


def _make_run_dir(cfg: SetsConfig, set_name: str, level: str, mode: str | None, kind: str) -> tuple[str, Path]:
    root = cfg.results_path()
    root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_id = f"{stamp}_{kind}_{set_name}_{level}" + (f"_{mode}" if mode else "")
    run_dir = root / run_id
    k = 1
    while run_dir.exists():
        k += 1
        run_dir = root / f"{run_id}_{k}"
    run_dir.mkdir(mode=0o700)
    return run_dir.name, run_dir


# ------------------------------------------------------------------------------------------------ run
def open_grading(warehouse: str | None, with_tools: bool, today: str | None = None, knowledge_dir: str | None = None):
    """``(read-only connection, record tools or None, notes)`` for grading; both None (with a note) when the
    warehouse cannot be opened. The tools replay the golden set's tool calls."""
    from sf_assistant import config
    from sf_assistant.evaluation import expected_data

    notes: list[str] = []
    path = warehouse or str(config.load_records_config().warehouse)
    tools = None
    if with_tools:   # first: the record tools set up and lock the shared database instance of this file
        try:
            from sf_assistant.tools.open_records import open_records

            rcfg = config.load_records_config(warehouse=path, **({"today": today} if today else {}))
            # the reference replay never follows the spelling help being evaluated (SFA_RESOLVER, on by default):
            # exact tools only, so the spelling help is switched off here explicitly
            tools = open_records(rcfg, knowledge_dir or str(config.KNOWLEDGE_DIR), config.ResolverConfig(enabled=False))
        except Exception as exc:  # noqa: BLE001
            notes.append(f"golden tool calls not replayed ({type(exc).__name__}: {exc})")
    try:
        con = expected_data.open_readonly(path)
    except Exception as exc:  # noqa: BLE001 - grading still runs, the SQL checks are skipped
        notes.append(f"expected SQL not re-run ({type(exc).__name__}: {exc})")
        con = None
    return con, tools, notes


def run(cfg: SetsConfig, set_name: str = "all", mode: str | None = None, level: str = "standard", *,
        today: str | None = None, warehouse: str | None = None, answer_batch: AnswerBatch | None = None,
        progress: Callable[[str], None] = print, grade_data: bool = True, knowledge_dir: str | None = None
        ) -> RunResult:
    """Ask and grade a level of the sets; write ``results.jsonl``, ``summary.json`` and ``report.md``.

    ``answer_batch`` (default :func:`cli_batch`) answers one (set, mode) group; ``grade_data`` re-runs the expected
    SQL and the golden tool calls on ``warehouse`` (default: the records config's snapshot)."""
    pairs = plan(cfg, set_name, mode, level)
    today = today or cfg.today
    run_id, run_dir = _make_run_dir(cfg, set_name, level, mode, "run")
    batch = answer_batch or cli_batch(today=today, warehouse=warehouse)
    groups: dict[tuple[str, str], list[EvalItem]] = {}
    for it, m in pairs:
        groups.setdefault((it.set, m), []).append(it)
    started = time.strftime("%Y-%m-%d %H:%M:%S")
    progress(f"run {run_id}: {len(pairs)} answers in {len(groups)} group(s) -> {run_dir}")
    # 1. answer every group (the grading connections open afterwards: an answer batch in this process opens the same
    #    snapshot, and one database instance per file and process is configured once)
    answered: dict[tuple[str, str], list[dict]] = {}
    errors: list[str] = []
    for (set_name_g, m), items in groups.items():
        t0 = time.perf_counter()
        progress(f"  {set_name_g} / {m}: {len(items)} question(s) ...")
        try:
            answered[(set_name_g, m)] = batch([it.question for it in items], m, run_dir / f"{set_name_g}_{m}")
            progress(f"  {set_name_g} / {m}: answered ({time.perf_counter() - t0:.0f} s)")
        except Exception as exc:  # noqa: BLE001 - one failed group does not stop the run
            errors.append(f"{set_name_g}/{m}: {type(exc).__name__}: {exc}")
            progress(f"  {set_name_g} / {m}: FAILED ({exc})")
            answered[(set_name_g, m)] = []
    # 2. grade (no model call): the expected SQL and the golden tool calls on the same snapshot
    con = tools = None
    notes: list[str] = []
    if grade_data:
        con, tools, notes = open_grading(warehouse, any(s == "golden" for s, _ in groups), today, knowledge_dir)
        for n in notes:
            progress("note: " + n)
    results: list[dict] = []
    try:
        for (set_name_g, m), items in groups.items():
            graded = grade_rows(items, answered[(set_name_g, m)], m, con=con, tools=tools)
            results += graded
            c = _counts(graded)
            progress(f"  {set_name_g} / {m}: {c['correct']} correct, {c['partial']} partial, {c['wrong']} wrong, "
                     f"{c['needs_review']} to review" + (f", {c['not_run']} not run" if c["not_run"] else ""))
    finally:
        if tools is not None:
            tools.close()
        if con is not None:
            con.close()
    meta = {"kind": "run", "run_id": run_id, "set": set_name, "level": level, "mode": mode, "today": today,
            "warehouse": warehouse or "(records config default)", "started": started,
            "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "notes": notes, "errors": errors,
            "model": os.environ.get("OKEVAL_MODEL") or os.environ.get("SFA_MODEL") or "(config default)"}
    summary = _summary(results, meta)
    previous = find_previous(cfg.results_path(), set_name, level, mode, run_id)
    comparison = compare(previous, results) if previous else None
    write_outputs(run_dir, results, summary, comparison)
    progress(f"report: {run_dir / 'report.md'}")
    return RunResult(run_dir=run_dir, results=results, summary=summary, previous=previous, comparison=comparison)


def grade_saved(cfg: SetsConfig, set_name: str, mode: str, sidecar: str, *, hand: str | None = None,
                warehouse: str | None = None, today: str | None = None, progress: Callable[[str], None] = print,
                grade_data: bool = True, knowledge_dir: str | None = None) -> RunResult:
    """Grade a saved ``ask --file`` sidecar of one set and mode (no model call), optionally against hand grades
    (a TSV with ``id`` or ``n`` and ``grade`` columns). Writes a run folder of kind ``grade``."""
    from sf_assistant.evaluation.sets import load_items

    if set_name not in cfg.sets:
        raise ValueError(f"unknown set {set_name!r}")
    items = load_items(cfg.sets[set_name], cfg)
    with open(sidecar, encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    asked = {r.get("question") for r in rows}
    items = [it for it in items if it.question in asked]
    run_id, run_dir = _make_run_dir(cfg, set_name, "saved", mode, "grade")
    con = tools = None
    notes: list[str] = []
    if grade_data:
        con, tools, notes = open_grading(warehouse, set_name == "golden", today or cfg.today, knowledge_dir)
    try:
        results = grade_rows(items, rows, mode, con=con, tools=tools)
    finally:
        if tools is not None:
            tools.close()
        if con is not None:
            con.close()
    meta = {"kind": "grade", "run_id": run_id, "set": set_name, "level": "saved", "mode": mode, "sidecar": sidecar,
            "today": today or cfg.today, "notes": notes}
    summary = _summary(results, meta)
    if hand:
        summary["agreement"] = {f"{set_name}/{mode}": agreement(results, read_hand_grades(hand, cfg.sets[set_name]
                                                                                          .id_prefix))}
    previous = find_previous(cfg.results_path(), set_name, "saved", mode, run_id, kind="grade")
    comparison = compare(previous, results) if previous else None
    write_outputs(run_dir, results, summary, comparison)
    progress(f"graded {len(results)} answer(s) -> {run_dir / 'report.md'}")
    return RunResult(run_dir=run_dir, results=results, summary=summary, previous=previous, comparison=comparison)


def read_hand_grades(path: str, id_prefix: str) -> dict[str, str]:
    """``{question id: grade}`` of a hand-grade TSV (columns ``id`` or ``n``, and ``grade``)."""
    import csv

    out: dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            key = (row.get("id") or "").strip() or f"{id_prefix}{int(row['n']):02d}"
            out[key] = (row.get("grade") or "").strip()
    return out


def agreement(results: list[dict], hand: dict[str, str]) -> dict:
    """Agreement of the automatic verdicts with hand grades; ``needs_review`` counts as neither (it is reported)."""
    agree = disagree = review = 0
    for r in results:
        h = hand.get(r["id"])
        if h is None or r["verdict"] == NOT_RUN:
            continue
        r["hand"] = h
        if r["verdict"] == "needs_review":
            review += 1
        elif r["verdict"] == h:
            agree += 1
        else:
            disagree += 1
    decided = agree + disagree
    return {"n": agree + disagree + review, "agree": agree, "disagree": disagree, "needs_review": review,
            "agreement": f"{100 * agree / decided:.1f}%" if decided else "-"}
