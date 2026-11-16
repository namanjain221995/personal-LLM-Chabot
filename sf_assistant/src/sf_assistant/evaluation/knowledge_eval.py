"""The single-object knowledge evaluation, end to end: ``python -m sf_assistant.evaluation knowledge``.

Ported from the notebook folder's ``run_local.py`` (``notebooks/object_knowledge_eval``), which does what
``Object_Knowledge_Eval.ipynb`` does in its sections 5 to 12, without Jupyter. It tests ONE object knowledge file on
its own:

1. load + validate the knowledge JSON (aborts on errors, prints ``WARN:`` lines),
2. build the BM25 index and the MCP-style tool registry,
3. generate questions: deterministic gold + adversarial, plus LLM-written questions per
   category and paraphrases of a random gold sample (both optional),
4. answer every question with the tool-calling agent against the real model,
5. grade (deterministic checks + optional LLM judge),
6. print the per-category summary, coverage and failures, and export everything to the run folder
   (results.csv, results.jsonl, summary.md, questions.jsonl, config.json, coverage.json).

The options, their defaults, the printed lines and the six files are those of ``run_local.py``, with these deliberate
differences:

* Two defaults point into this package: the knowledge file is ``knowledge/objects/Internal_Interview__c.json``, and a
  run folder is ``reports/knowledge_eval/<object>_<YYYYmmdd_HHMMSS>/`` (``_dry`` added for a ``--dry`` run), which
  git ignores: the files quote the knowledge text, which stays local like the knowledge files.
* ``--knowledge`` takes any path; a relative path that is not a file from the current folder is read from the
  ``sf_assistant`` folder, so ``--knowledge knowledge/objects/<Object>.json`` works from anywhere.
* A knowledge file that cannot be read or walked (missing, a folder, not UTF-8, not JSON, not a JSON object, a
  section of the wrong type) prints one line, ``cannot read the knowledge file <path>: <error>``, and exits 2;
  run_local.py shows a traceback and exits 1. A file whose checks find errors exits 2 on both, with the same lines.
* ``--rerun-failed``, ``--regrade`` and ``--questions`` read a saved run, which belongs to one knowledge file: when
  the ``config.json`` next to it names a knowledge file of another file name, the command says so and exits 2 before
  any model call (run_local.py goes on with the wrong object).
* When no question is left to answer or grade (``--qids`` kept none, the ``--rerun-failed`` run has no failures, a
  negative ``--sample``), the command says so and exits 2 without a run folder (run_local.py stops with a
  ``KeyError`` after it wrote an empty questions.jsonl).
* ``--regrade <run> --no-judge`` makes no model call: the deterministic grading needs none, so the connectivity
  check is skipped (run_local.py sends it, and exits 3 when the endpoint is down).

The spelling help (``SFA_RESOLVER``) is not used: the tools are the plain knowledge tools, as in the notebook.

Examples (from the ``sf_assistant`` folder)::

    .venv/bin/python -m sf_assistant.evaluation knowledge --dry          # the mock model: no network, scores mean nothing
    .venv/bin/python -m sf_assistant.evaluation knowledge                # everything, judge on, 6 workers
    .venv/bin/python -m sf_assistant.evaluation knowledge --knowledge knowledge/objects/Session__c.json
    .venv/bin/python -m sf_assistant.evaluation knowledge --sample 40 --no-judge     # quick stratified smoke pass
    K=knowledge/objects/Session__c.json      # a saved run is read with the --knowledge it was made with
    .venv/bin/python -m sf_assistant.evaluation knowledge --knowledge $K \\
        --questions reports/knowledge_eval/x/questions.jsonl \\
        --qids gold-field_lookup-001,adversarial-unanswerable_record_data-001
    .venv/bin/python -m sf_assistant.evaluation knowledge --knowledge $K --rerun-failed reports/knowledge_eval/x
    .venv/bin/python -m sf_assistant.evaluation knowledge --knowledge $K --regrade reports/knowledge_eval/x \\
        --label x_regraded

Endpoint settings come from the ``OKEVAL_*`` env vars (or their ``SFA_*`` aliases, see ``sf_assistant/config.py``)
or the flags. The API key is never printed. Without ``--dry`` every question, paraphrase and judged grade is a model
call: run it only when the owner asks (docs/runbook.md, Rules); ``--regrade <run> --no-judge`` calls no model.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from typing import TYPE_CHECKING, Iterable

# Only the standard library and config at import: ``python -m sf_assistant.evaluation`` builds its parser from
# add_arguments for every command. pandas and the knowledge side are imported in the functions that use them.
from sf_assistant.config import HOME, KNOWLEDGE_DIR, LIMITS, load_config

if TYPE_CHECKING:  # type hints only: imported where they are used
    from sf_assistant.agent.knowledge_agent import AgentResult
    from sf_assistant.agent.llm import LLM
    from sf_assistant.agent.question import Question
    from sf_assistant.knowledge.search import KnowledgeIndex
    from sf_assistant.knowledge.store import ObjectKnowledge

__all__ = ["DEFAULT_KNOWLEDGE", "DEFAULT_OUT", "add_arguments", "parse_args", "resolve_knowledge", "run_label",
           "sample_questions", "build_questions", "load_run_for_regrade", "saved_run_mismatch", "run", "main"]

#: ``--knowledge`` default (run_local.py: ``knowledge/Internal_Interview__c.json`` of the notebook folder).
DEFAULT_KNOWLEDGE = KNOWLEDGE_DIR / "Internal_Interview__c.json"
#: ``--out`` default, the parent of the run folders (run_local.py: ``runs/`` of the notebook folder); git-ignored.
DEFAULT_OUT = HOME / "reports" / "knowledge_eval"


def sample_questions(qs: list["Question"], n: int | None = None, seed: int = 7) -> list["Question"]:
    """Stratified sample: round-robin over categories so every category is represented.

    Identical to the helper in notebook section 9, so ``--sample N`` picks the same set.
    """
    if not n or n >= len(qs):
        return list(qs)
    rng = random.Random(seed)
    by_cat: dict[str, list["Question"]] = {}
    for q in qs:
        by_cat.setdefault(q.category, []).append(q)
    for lst in by_cat.values():
        rng.shuffle(lst)
    picked: list["Question"] = []
    cats = sorted(by_cat)
    while len(picked) < n:
        for c in cats:
            if by_cat[c] and len(picked) < n:
                picked.append(by_cat[c].pop())
    return picked


def add_arguments(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """The options of run_local.py with its defaults (the knowledge file and the run folder: this package's)."""
    p.add_argument("--knowledge", default=str(DEFAULT_KNOWLEDGE),
                   help="knowledge JSON to evaluate (default: knowledge/objects/Internal_Interview__c.json; a "
                        "relative path that is not a file here is read from the sf_assistant folder)")
    p.add_argument("--base-url", help="OpenAI-compatible endpoint ending in /v1 (overrides OKEVAL_BASE_URL)")
    p.add_argument("--model", help="model id (overrides OKEVAL_MODEL)")
    p.add_argument("--judge-model", help="separate grading model (default: same as --model)")
    p.add_argument("--enable-thinking", action="store_true", help="send enable_thinking=True (Qwen/vLLM)")
    p.add_argument("--max-tool-steps", type=int, default=LIMITS.knowledge_max_steps,
                   help="model calls per question before one final answer is forced (default: %(default)s)")
    p.add_argument("--sample", type=int, default=None,
                   help="stratified sample size to answer (default: all questions)")
    p.add_argument("--workers", type=int, default=6, help="parallel requests (default: %(default)s)")
    p.add_argument("--judge", dest="judge", action="store_true", default=True, help="use the LLM judge (default)")
    p.add_argument("--no-judge", dest="judge", action="store_false", help="deterministic grading only")
    p.add_argument("--llm-questions", type=int, default=2, metavar="PER_CATEGORY",
                   help="LLM-written questions per category; 0 disables (default: %(default)s)")
    p.add_argument("--paraphrase-n", type=int, default=1,
                   help="rewrites per sampled gold question; 0 disables (default: %(default)s)")
    p.add_argument("--paraphrase-sample", type=int, default=30,
                   help="gold questions to paraphrase (default: %(default)s)")
    p.add_argument("--questions", help="load a saved questions.jsonl instead of generating")
    p.add_argument("--qids", help="comma-separated qids to keep (after loading/generating)")
    p.add_argument("--rerun-failed", metavar="RUN_DIR",
                   help="re-answer only the failed questions of a previous run (uses its questions.jsonl "
                        "unless --questions is given; regenerates gold/adversarial from current code; needs "
                        "that run's --knowledge)")
    p.add_argument("--regrade", metavar="RUN_DIR",
                   help="grade a previous run's saved answers again with the current grading code "
                        "(gold/adversarial expected facts refreshed when the question text is unchanged; needs "
                        "that run's --knowledge; with --no-judge no model call)")
    p.add_argument("--out", default=str(DEFAULT_OUT),
                   help="parent directory for run folders (default: reports/knowledge_eval in the sf_assistant folder)")
    p.add_argument("--label", default=None,
                   help="run folder name (default: <object>_<YYYYmmdd_HHMMSS>, with _dry for --dry)")
    p.add_argument("--dry", action="store_true", help="use the offline MockClient (no network)")
    p.add_argument("--quiet", action="store_true", help="no per-question progress")
    return p


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    """The options on a parser of their own (the ``knowledge`` command adds the same ones to its subparser)."""
    p = argparse.ArgumentParser(prog="python -m sf_assistant.evaluation knowledge",
                                description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    return add_arguments(p).parse_args(None if argv is None else list(argv))


def resolve_knowledge(path: str) -> str:
    """``path`` when it is absolute or a file (a relative one from the current folder); otherwise the same relative
    path in the ``sf_assistant`` folder when that is a file; otherwise ``path`` unchanged (the load then says so)."""
    if os.path.isabs(path) or os.path.isfile(path):
        return path
    in_home = os.path.join(str(HOME), path)
    return in_home if os.path.isfile(in_home) else path


def run_label(api_name: str, dry: bool) -> str:
    """The default run folder name: ``<object>_<YYYYmmdd_HHMMSS>``, ``_dry`` added for a dry run."""
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", api_name).strip("._") or "object"
    return f"{name}_{time.strftime('%Y%m%d_%H%M%S')}" + ("_dry" if dry else "")


def build_questions(args: argparse.Namespace, kn: "ObjectKnowledge", llm: "LLM", index: "KnowledgeIndex",
                    seed: int) -> list["Question"]:
    """Generate (or load) the question set exactly as notebook section 8 does."""
    from sf_assistant.evaluation.questions import (adversarial_questions, dedupe, gold_questions, llm_questions,
                                                   load_questions, paraphrase)

    if args.rerun_failed:
        return _failed_questions(args, kn)
    if args.questions:
        return load_questions(args.questions)
    gold = gold_questions(kn)
    adv = adversarial_questions(kn)
    extra: list["Question"] = []
    if args.llm_questions > 0:
        t0 = time.time()
        extra += llm_questions(kn, llm, index, per_category=args.llm_questions)
        print(f"  llm questions: {len(extra)} in {time.time() - t0:.1f}s")
    if args.paraphrase_n > 0 and args.paraphrase_sample > 0:
        t0 = time.time()
        rng = random.Random(seed)
        pick = rng.sample(gold, min(args.paraphrase_sample, len(gold)))
        para = paraphrase(pick, llm, n=args.paraphrase_n, knowledge=kn)
        extra += para
        print(f"  paraphrases: {len(para)} in {time.time() - t0:.1f}s")
    print(f"  gold: {len(gold)}  adversarial: {len(adv)}  extra: {len(extra)}")
    return dedupe(gold + adv + extra)


def _failed_questions(args: argparse.Namespace, kn: "ObjectKnowledge") -> list["Question"]:
    """Questions that failed in ``args.rerun_failed``.

    Gold/adversarial items are regenerated from the current code (so fixes to questions.py take
    effect); llm/paraphrase items come from the run's saved questions.jsonl.
    """
    import pandas as pd

    from sf_assistant.evaluation.questions import load_questions

    run_dir = args.rerun_failed
    df = pd.read_json(os.path.join(run_dir, "results.jsonl"), lines=True)
    failed = set(df.loc[~df["passed"].astype(bool), "qid"])
    saved = load_questions(args.questions or os.path.join(run_dir, "questions.jsonl"))
    fresh = _fresh_by_qid(kn)
    out = []
    for q in saved:
        if q.qid in failed:
            out.append(fresh.get(q.qid, q) if q.source in ("gold", "adversarial") else q)
    print(f"  re-running {len(out)} failed questions from {run_dir}")
    return out


def _fresh_by_qid(kn: "ObjectKnowledge") -> dict[str, "Question"]:
    """Deterministic questions regenerated from the current code, by qid."""
    from sf_assistant.evaluation.questions import adversarial_questions, dedupe, gold_questions

    return {q.qid: q for q in dedupe(gold_questions(kn) + adversarial_questions(kn))}


def load_run_for_regrade(run_dir: str, kn: "ObjectKnowledge") -> tuple[list["Question"], list["AgentResult"]]:
    """Saved questions + agent results of ``run_dir``.

    A gold/adversarial question is replaced by its regenerated version only when the question
    text is identical (so fixes to expected facts apply); otherwise the saved question is kept,
    because the saved answer belongs to that wording.
    """
    from sf_assistant.agent.knowledge_agent import AgentResult
    from sf_assistant.evaluation.questions import load_questions

    saved = load_questions(os.path.join(run_dir, "questions.jsonl"))
    fresh = _fresh_by_qid(kn)
    qs = [fresh[q.qid] if q.qid in fresh and fresh[q.qid].question == q.question else q for q in saved]
    results: list[AgentResult] = []
    with open(os.path.join(run_dir, "results.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            results.append(AgentResult(
                qid=row["qid"], question=row["question"], answer=row.get("answer") or "",
                tool_calls=row.get("tool_calls") or [], steps=int(row.get("steps") or 0),
                usage={"prompt_tokens": int(row.get("prompt_tokens") or 0),
                       "completion_tokens": int(row.get("completion_tokens") or 0)},
                latency_s=float(row.get("latency_s") or 0.0), error=row.get("error")))
    return qs, results


def saved_run_mismatch(args: argparse.Namespace, knowledge_path: str) -> str | None:
    """Why the saved run that ``--rerun-failed``, ``--regrade`` or ``--questions`` reads does not fit
    ``knowledge_path``, or None.

    A run belongs to the knowledge file it was made with: its ``config.json`` holds that ``knowledge_path``. The file
    names are compared (the folder may differ: an absolute or a relative path, the notebook folder's copy). A folder
    without a readable ``config.json`` is not checked. run_local.py has no such check and goes on with the wrong
    object (a deliberate difference)."""
    folders = [d for d in (args.rerun_failed, args.regrade) if d]
    if args.questions:
        folders.append(os.path.dirname(args.questions) or ".")
    for folder in folders:
        try:
            with open(os.path.join(folder, "config.json"), encoding="utf-8") as fh:
                saved = json.load(fh).get("knowledge_path")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(saved, str) and saved and os.path.basename(saved) != os.path.basename(knowledge_path):
            return (f"{folder} was made with the knowledge file {saved}, not {knowledge_path}: "
                    f"pass --knowledge with that run's file")
    return None


def _no_questions(args: argparse.Namespace, n_before: int, filtered: bool) -> int:
    """No question left to answer or grade: say why (stderr) and stop with exit 2, before a run folder is made.

    ``n_before`` is the number of questions before ``--qids`` and ``--sample``, ``filtered`` whether ``--qids`` named
    any. run_local.py goes on here and stops with ``KeyError: 'passed'`` after writing an empty questions.jsonl (a
    deliberate difference)."""
    if n_before == 0:
        why = (f"{args.regrade} has no saved question" if args.regrade
               else f"{args.rerun_failed} has no failed question" if args.rerun_failed
               else f"{args.questions} holds no question" if args.questions else "the question set is empty")
    elif filtered:
        why = f"--qids kept none of the {n_before} questions"
    else:
        why = f"--sample {args.sample} keeps none of the {n_before} questions"
    print(f"no questions to {'grade' if args.regrade else 'answer'}: {why}; nothing written", file=sys.stderr)
    return 2


def run(args: argparse.Namespace) -> int:
    """One evaluation run with the options of :func:`add_arguments`; prints its progress and returns the exit code:
    0 done; 2 the knowledge file cannot be read or has errors, the saved run it reads was made with another knowledge
    file, or no question is left; 3 the endpoint is not reachable."""
    import pandas as pd

    from sf_assistant.agent.knowledge_agent import answer_all
    from sf_assistant.agent.llm import LLM
    from sf_assistant.agent.mock_llm import MockClient
    from sf_assistant.evaluation.grading import grade_all
    from sf_assistant.evaluation.questions import category_counts, save_questions
    from sf_assistant.evaluation.report import coverage, export, knowledge_gaps, results_frame, summary
    from sf_assistant.knowledge.search import KnowledgeIndex
    from sf_assistant.knowledge.store import ObjectKnowledge
    from sf_assistant.tools.knowledge_tools import ToolRegistry

    overrides: dict = {"knowledge_path": resolve_knowledge(args.knowledge), "max_tool_steps": args.max_tool_steps,
                       "enable_thinking": args.enable_thinking}
    for key in ("base_url", "model", "judge_model"):
        if getattr(args, key):
            overrides[key] = getattr(args, key)
    if args.dry:
        overrides.update(base_url="http://dry-run.invalid/v1", api_key="EMPTY", model="mock-model")
    cfg = load_config(**overrides)
    print("config:", json.dumps(cfg.safe_dict()))          # api_key masked
    mismatch = saved_run_mismatch(args, cfg.knowledge_path)
    if mismatch:                                           # before any model call (run_local.py: no check)
        print(mismatch, file=sys.stderr)
        return 2

    # --- 5. load + validate -------------------------------------------------------------------
    # One line and exit 2 for a file that cannot be read or walked (run_local.py: a traceback and exit 1).
    try:
        kn = ObjectKnowledge.load(cfg.knowledge_path)
        problems = kn.validate()
        n_fields, n_chunks = len(kn.fields()), len(kn.chunks())
    except (OSError, ValueError, TypeError, AttributeError, LookupError) as exc:
        print(f"cannot read the knowledge file {cfg.knowledge_path}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    errors = [p for p in problems if not p.startswith("WARN:")]
    print(f"object {kn.api_name}: {n_fields} fields, {n_chunks} chunks; "
          f"{len(errors)} error(s), {len(problems) - len(errors)} warning(s)")
    for prob in problems:
        print("  -", prob)
    if errors:
        print("fix the knowledge JSON before evaluating it", file=sys.stderr)
        return 2

    index = KnowledgeIndex(kn.chunks())
    registry = ToolRegistry(kn, index)
    llm = LLM(cfg, client=MockClient()) if args.dry else LLM(cfg)

    # Connectivity check (same as notebook section 4). Not for --regrade --no-judge: the deterministic grading calls
    # no model (run_local.py sends it in that mode too).
    if not args.dry and not (args.regrade and not args.judge):
        try:
            r = llm.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=20)
            print(f"endpoint ok ({r['latency_s']:.2f}s): {(r['content'] or '').strip()[:40]}")
        except Exception as exc:  # noqa: BLE001 - report and stop
            print("endpoint NOT reachable:", type(exc).__name__, str(exc)[:300], file=sys.stderr)
            return 3

    label = args.label or run_label(kn.api_name, args.dry)
    run_dir = os.path.join(args.out, label)
    keep = {s.strip() for s in (args.qids or "").split(",") if s.strip()}

    if args.regrade:
        # --- re-grade saved answers only ----------------------------------------------------------
        run_qs, results = load_run_for_regrade(args.regrade, kn)
        n_before = len(run_qs)
        if keep:
            run_qs = [q for q in run_qs if q.qid in keep]
        print(f"re-grading {len(run_qs)} saved answers from {args.regrade}")
        if not run_qs:
            return _no_questions(args, n_before, bool(keep))
        os.makedirs(run_dir, exist_ok=True)
        save_questions(run_qs, os.path.join(run_dir, "questions.jsonl"))
    else:
        # --- 8. questions -------------------------------------------------------------------------
        print("generating questions ...")
        questions = build_questions(args, kn, llm, index, cfg.seed)
        n_before = len(questions)
        if keep:
            questions = [q for q in questions if q.qid in keep]
        run_qs = sample_questions(questions, args.sample, seed=cfg.seed)
        print(f"{len(run_qs)} questions to answer; per category: {category_counts(run_qs)}")
        if not run_qs:
            return _no_questions(args, n_before, bool(keep))
        os.makedirs(run_dir, exist_ok=True)
        save_questions(run_qs, os.path.join(run_dir, "questions.jsonl"))

        # --- 10. answer ---------------------------------------------------------------------------
        t0 = time.time()
        results = answer_all(run_qs, llm, registry, cfg, workers=args.workers, progress=not args.quiet)
        n_err = sum(r.error is not None for r in results)
        print(f"answered {len(results)} in {time.time() - t0:.1f}s; errors: {n_err}; tokens: {llm.total_usage}")

    # --- 11. grade ----------------------------------------------------------------------------
    t0 = time.time()
    grades = grade_all(run_qs, results, llm, cfg, kn, use_judge=args.judge, workers=args.workers)
    df = results_frame(run_qs, results, grades)
    print(f"graded in {time.time() - t0:.1f}s; pass rate {df['passed'].mean():.1%}; tokens: {llm.total_usage}")

    # --- 12. report + export ------------------------------------------------------------------
    with pd.option_context("display.width", 200, "display.max_columns", 20, "display.max_colwidth", 60):
        print("\nper-category summary:")
        print(summary(df).to_string())
        cov = coverage(kn, results, questions=run_qs, grades=grades)
        print(f"\ntool coverage: {cov['ratio']:.1%} ({len(cov['touched'])} touched, "
              f"{len(cov['never_touched'])} never touched)")
        for cid in cov["never_touched"]:
            print("  never touched:", cid)
        qc, ac = cov["question_coverage"], cov["attribute_coverage"]
        print(f"question coverage: {qc['ratio']:.1%} ({len(qc['covered'])} chunks verified by a passed, "
              f"non-tautological question; untested: {qc['untested'] or 'none'})")
        print(f"attribute coverage: {ac['ratio']:.1%} "
              + ", ".join(f"{k} {v['covered']}/{v['total']}" for k, v in sorted(ac["by_kind"].items())))
        for w in cov.get("knowledge_inconsistencies") or []:
            print("  inconsistency:", w.replace("WARN: consistency: ", ""))
        gaps = knowledge_gaps(df, kn)
        print(f"\nknowledge gaps: {len(gaps)} rows; failures: {int((~df['passed'].astype(bool)).sum())}")

    paths = export(df, run_dir, coverage_info=cov)
    with open(os.path.join(run_dir, "config.json"), "w", encoding="utf-8") as fh:
        json.dump(cfg.safe_dict(), fh, indent=2)                # api_key masked
    with open(os.path.join(run_dir, "coverage.json"), "w", encoding="utf-8") as fh:
        json.dump(cov, fh, indent=2)
    print("\nwritten:", *paths, os.path.join(run_dir, "questions.jsonl"), sep="\n  ")
    print("run dir:", run_dir)
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    """:func:`run` with the options parsed from ``argv`` (as the ``knowledge`` command of ``python -m
    sf_assistant.evaluation``)."""
    return run(parse_args(argv))
