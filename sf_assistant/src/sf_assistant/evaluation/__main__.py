"""``python -m sf_assistant.evaluation <command>``: run and grade the evaluation sets (no notebook, no ask.sh rounds), or
evaluate one object knowledge file on its own (``knowledge``).

    python -m sf_assistant.evaluation list
    python -m sf_assistant.evaluation run --set <lookup|golden|easy|people_dev|people_holdout|controls|all>
                                          --mode <merged|records|both> --level <quick|standard|full>
    python -m sf_assistant.evaluation run --level quick --dry-run          # the plan only, nothing is asked
    python -m sf_assistant.evaluation grade --set people_dev --mode records --sidecar .private/runs/x.jsonl \
                                            [--hand grades.tsv]            # grade saved answers, no model call
    python -m sf_assistant.evaluation knowledge [--knowledge knowledge/objects/<Object>.json] [--dry] ...
                                            # one object knowledge file on its own (was run_local.py)
    python -m sf_assistant.evaluation timeline [LOG] [--ticket N | --at UTC | --last N | --slowest N] [--stats]
                                            # where the seconds of an answer went (page log or ask --file sidecar)

Levels (``sets.yaml``): quick = a fixed sample of every set, merged (35 questions); standard = every question, merged
(157, the default); full = every question in the modes of the hand-graded rounds (284 answers). ``--mode`` replaces
the level's modes (sets that cannot run in a mode are left out). ``run`` asks the model through the ask cli (the
OKEVAL_* / SFA_* endpoint settings of ./ask.sh apply); ``grade`` and ``--dry-run`` never call a model.
Results: ``.private/runs/eval/<run id>/`` (results.jsonl, report.md, summary.json, transcripts); they hold real names.

``knowledge`` is the single-object knowledge evaluation of the notebook folder (run_local.py, notebook sections 5 to
12): questions built from one knowledge file, the knowledge agent answers them with the knowledge tools, grades,
coverage and gaps. Same options and output files as run_local.py (``knowledge --help``); results in
``reports/knowledge_eval/<object>_<time>/`` (git-ignored). It calls the model unless ``--dry`` (the mock model) or
``--regrade <run> --no-judge`` (deterministic grades only).

``timeline`` prints the waterfall of an answer's timeline (stages, model calls, tool calls, checks; latency logging,
2026-10-29) from the page log (default ``.private/web_log.jsonl``) or an ``ask --file`` sidecar; ``--stats`` the medians
per stage, model-call stage and tool. Numbers, names of stages and tools and fixed words only; no model call.
"""
from __future__ import annotations

import argparse
import sys

from sf_assistant.evaluation import knowledge_eval, timeline_report
from sf_assistant.evaluation.sets import LEVELS, load_sets, load_items, plan


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m sf_assistant.evaluation", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sets", metavar="FILE", help="another sets.yaml (default: the package's)")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="the sets, their files and the size of every level")
    r = sub.add_parser("run", help="ask and grade a level of the sets")
    r.add_argument("--set", default="all", help="a set of sets.yaml or 'all' (default)")
    r.add_argument("--mode", choices=("merged", "records", "both"), help="default: the level's modes")
    r.add_argument("--level", choices=LEVELS, default="standard")
    r.add_argument("--today", help="pin today (default: sets.yaml today)")
    r.add_argument("--warehouse", metavar="PATH", help="local snapshot (default: the records config's)")
    r.add_argument("--dry-run", action="store_true", help="print the plan and stop (no model call)")
    g = sub.add_parser("grade", help="grade a saved ask --file sidecar (no model call)")
    g.add_argument("--set", required=True)
    g.add_argument("--mode", choices=("merged", "records"), required=True)
    g.add_argument("--sidecar", required=True, metavar="JSONL")
    g.add_argument("--hand", metavar="TSV", help="hand grades (id or n, grade) to report the agreement")
    g.add_argument("--today")
    g.add_argument("--warehouse", metavar="PATH")
    k = sub.add_parser("knowledge", help="evaluate one object knowledge file on its own (was run_local.py)",
                       description=knowledge_eval.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    knowledge_eval.add_arguments(k)
    t = sub.add_parser("timeline", help="where the seconds of an answer went (page log or ask --file sidecar)",
                       description=timeline_report.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    timeline_report.add_arguments(t)
    return p


def main(argv: list[str] | None = None) -> int:
    a = _parser().parse_args(argv)
    if a.command == "knowledge":   # one knowledge file: no sets.yaml
        return knowledge_eval.run(a)
    if a.command == "timeline":    # reads a log: no sets.yaml
        return timeline_report.run(a)
    try:
        cfg = load_sets(a.sets)
    except (OSError, ValueError) as exc:
        print(f"sets.yaml: {exc}", file=sys.stderr)
        return 2
    if a.command == "list":
        for name in cfg.order:
            spec = cfg.sets[name]
            try:
                n = len(load_items(spec, cfg))
            except OSError:
                n = "missing"
            print(f"{name:15} {spec.grader:8} questions={n!s:7} modes={','.join(spec.modes):15} "
                  f"full={','.join(spec.full):15} quick={spec.quick}  {spec.questions}")
        for level in LEVELS:
            try:
                print(f"level {level:9} {len(plan(cfg, 'all', None, level))} answers")
            except OSError as exc:
                print(f"level {level:9} ({exc})")
        return 0
    if a.command == "run":
        try:
            pairs = plan(cfg, a.set, a.mode, a.level)
        except (OSError, ValueError) as exc:
            print(f"plan: {exc}", file=sys.stderr)
            return 2
        if a.dry_run:
            groups: dict[str, int] = {}
            for it, m in pairs:
                groups[f"{it.set}/{m}"] = groups.get(f"{it.set}/{m}", 0) + 1
            for k, v in groups.items():
                print(f"{k:25} {v}")
            print(f"{len(pairs)} answers (level {a.level}, today {a.today or cfg.today})")
            return 0
        from sf_assistant.evaluation.runner import run

        res = run(cfg, a.set, a.mode, a.level, today=a.today, warehouse=a.warehouse)
        t = res.summary.get("total", {})
        print(f"{t.get('n', 0)} answers: {t.get('correct', 0)} correct, {t.get('partial', 0)} partial, "
              f"{t.get('wrong', 0)} wrong, {t.get('needs_review', 0)} to review")
        return 1 if res.summary.get("errors") else 0
    if a.command == "grade":
        from sf_assistant.evaluation.runner import grade_saved

        try:
            res = grade_saved(cfg, a.set, a.mode, a.sidecar, hand=a.hand, warehouse=a.warehouse, today=a.today)
        except (OSError, ValueError) as exc:
            print(f"grade: {exc}", file=sys.stderr)
            return 2
        t = res.summary.get("total", {})
        print(f"{t.get('n', 0)} answers: {t.get('correct', 0)} correct, {t.get('partial', 0)} partial, "
              f"{t.get('wrong', 0)} wrong, {t.get('needs_review', 0)} to review")
        for key, ag in (res.summary.get("agreement") or {}).items():
            print(f"agreement {key}: {ag['agree']} agree, {ag['disagree']} disagree, {ag['needs_review']} to review "
                  f"({ag['agreement']} of the decided)")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
