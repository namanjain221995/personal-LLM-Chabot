"""The artifact-intent probe, committed so the next verifier measures the same rows.

TWO ROUNDS OF THIS WORK WERE VERIFIED WITH A SCRATCHPAD SCRIPT that was
deleted with the session, and both verifiers had to rebuild it before they
could reproduce a single number ("there is no score_intent.py in the tree",
QA 2026-09-27; rebuilt again 2026-09-28). The rules gate is a pure function of
text and context, so the probe is fifty lines and there is no excuse for it
living anywhere but the repository.

WHAT IT DOES. Runs `artifacts.intent.decide` over a corpus of turns in the
four conversation shapes the gate behaves differently in, and writes one JSON
row per (context, turn). A second invocation on another checkout produces a
comparable file, and `--diff` classifies every row that moved as GAINED_FILE,
LOST_FILE or RELABEL. LOST_FILE against the merge target is the number that
matters: it is a file the person asked for and did not get, and it is the one
failure this gate cannot show you any other way.

    # on each arm, from orchestrator/
    python tools/artifact_intent_probe.py run \\
        tests/fixtures/artifact_intent_probe_corpus.txt /tmp/dev.json
    python tools/artifact_intent_probe.py diff /tmp/dev.json /tmp/branch.json \\
        --label-a dev --label-b branch --only LOST_FILE

No model, no network, no database: `decide` is the rules path only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.artifacts import intent as I  # noqa: E402

#: An artifact title, so `artifact_hints` is the shape production passes.
TRACKER = "TechSara AI Engineering Workflow Tracker"
#: The four conversation shapes. P0 fresh; PA an assistant answer and no
#: files; PC a file card as the last turn; PF a file earlier plus an answer.
#: The gate reads all four differently, and a regression in one of them is
#: invisible in the other three -- which is how 32 rows were lost twice.
CONTEXTS = {
    "P0": dict(has_artifacts=False, last_turn_is_artifact=False, has_assistant_answer=False),
    "PA": dict(has_artifacts=False, last_turn_is_artifact=False, has_assistant_answer=True),
    "PC": dict(has_artifacts=True, last_turn_is_artifact=True, has_assistant_answer=True,
               artifact_hints=(TRACKER,)),
    "PF": dict(has_artifacts=True, last_turn_is_artifact=False, has_assistant_answer=True,
               artifact_hints=(TRACKER,)),
}


def load_corpus(path: Path) -> list[str]:
    """One turn per line. Blank lines and `#` comments are skipped."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.rstrip()
        if text.strip() and not text.lstrip().startswith("#"):
            out.append(text)
    return out


def run(corpus: Path, out: Path) -> int:
    texts = load_corpus(corpus)
    rows = {}
    for text in texts:
        for name, ctx in CONTEXTS.items():
            got = I.decide(text, **ctx)
            rows[f"{name}\t{text}"] = {"action": got.action, "rule": got.rule,
                                       "wants_file": got.wants_file}
    out.write_text(json.dumps(rows, indent=0, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rows)} rows ({len(texts)} turns x {len(CONTEXTS)} contexts) -> {out}")
    return 0


def diff(a_path: Path, b_path: Path, label_a: str, label_b: str, only: str) -> int:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    shared = [k for k in a if k in b]
    counts = {"GAINED_FILE": 0, "LOST_FILE": 0, "RELABEL": 0}
    lines = []
    for key in shared:
        x, y = a[key], b[key]
        if x == y:
            continue
        if x["wants_file"] and not y["wants_file"]:
            kind = "LOST_FILE"
        elif not x["wants_file"] and y["wants_file"]:
            kind = "GAINED_FILE"
        else:
            kind = "RELABEL"
        counts[kind] += 1
        lines.append(f"{kind}\t{key}\t{label_a}={x['action']}/{x['rule']}"
                     f"\t{label_b}={y['action']}/{y['rule']}")
    for line in sorted(lines):
        if not only or line.startswith(only):
            print(line)
    print(f"# {label_a} -> {label_b}: {len(shared)} rows compared, "
          f"GAINED_FILE {counts['GAINED_FILE']}, LOST_FILE {counts['LOST_FILE']}, "
          f"RELABEL {counts['RELABEL']}"
          + (f", {len(a) - len(shared)} rows only in {label_a}" if len(a) != len(shared) else ""))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="decide() over a corpus, into a JSON file")
    p_run.add_argument("corpus", type=Path)
    p_run.add_argument("out", type=Path)
    p_diff = sub.add_parser("diff", help="classify every row that moved between two runs")
    p_diff.add_argument("a", type=Path)
    p_diff.add_argument("b", type=Path)
    p_diff.add_argument("--label-a", default="a")
    p_diff.add_argument("--label-b", default="b")
    p_diff.add_argument("--only", default="", choices=["", "GAINED_FILE", "LOST_FILE", "RELABEL"])
    args = parser.parse_args(argv)
    if args.cmd == "run":
        return run(args.corpus, args.out)
    return diff(args.a, args.b, args.label_a, args.label_b, args.only)


if __name__ == "__main__":
    raise SystemExit(main())
