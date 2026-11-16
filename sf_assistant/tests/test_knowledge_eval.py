"""``python -m sf_assistant.evaluation knowledge``: the single-object knowledge evaluation (evaluation/knowledge_eval.py,
ported from the notebook folder's run_local.py). Offline: ``--dry`` (MockClient) or a patched LLM, on the frozen
knowledge fixture; no model, no network, nothing written outside ``tmp_path``.

The parity tests run the notebook folder's run_local.py and this command on the fixture with the same options. The
first compares the exit code, stdout, stderr and every output file; the second pins the deliberate differences on bad
input (the same stdout, then one stderr line and exit 2 where run_local.py shows a traceback and exits 1). They run
only when ``notebooks/object_knowledge_eval`` (or ``SFA_PARITY_OLD``) is there and skip otherwise; they only read that
folder (no bytecode, ``--out`` in tmp_path).
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import sf_assistant.agent.llm as llm_mod
from sf_assistant.agent.mock_llm import MockClient
from sf_assistant.config import HOME, KNOWLEDGE_DIR
from sf_assistant.evaluation import __main__ as eval_main
from sf_assistant.evaluation import knowledge_eval

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "Internal_Interview__c.json"
OUTPUTS = ["config.json", "coverage.json", "questions.jsonl", "results.csv", "results.jsonl", "summary.md"]
OLD_DIR = Path(os.environ.get("SFA_PARITY_OLD") or ROOT.parent / "notebooks" / "object_knowledge_eval")
RUN_LOCAL = OLD_DIR / "run_local.py"

#: run_local.py's options, in its order
RUN_LOCAL_OPTIONS = ["--knowledge", "--base-url", "--model", "--judge-model", "--enable-thinking", "--max-tool-steps",
                     "--sample", "--workers", "--judge", "--no-judge", "--llm-questions", "--paraphrase-n",
                     "--paraphrase-sample", "--questions", "--qids", "--rerun-failed", "--regrade", "--out", "--label",
                     "--dry", "--quiet"]


def _knowledge(argv: list[str], capsys) -> tuple[int, str, str]:
    code = eval_main.main(["knowledge", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------------------------------------------ options and paths
def test_options_and_defaults_are_run_local_s():
    a = knowledge_eval.parse_args([])
    assert a.knowledge == str(KNOWLEDGE_DIR / "Internal_Interview__c.json") == str(knowledge_eval.DEFAULT_KNOWLEDGE)
    assert a.out == str(HOME / "reports" / "knowledge_eval") == str(knowledge_eval.DEFAULT_OUT)
    assert (a.max_tool_steps, a.sample, a.workers, a.judge, a.llm_questions, a.paraphrase_n, a.paraphrase_sample) == \
        (8, None, 6, True, 2, 1, 30)
    assert all(getattr(a, k) is None for k in ("base_url", "model", "judge_model", "questions", "qids", "rerun_failed",
                                               "regrade", "label"))
    assert (a.enable_thinking, a.dry, a.quiet) == (False, False, False)
    assert knowledge_eval.parse_args(["--no-judge"]).judge is False
    sub = vars(eval_main._parser().parse_args(["knowledge"]))
    assert sub.pop("command") == "knowledge" and sub.pop("sets") is None and sub == vars(a)


def test_help_lists_every_run_local_option():
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKEVAL_", "SFA_"))}
    p = subprocess.run([sys.executable, "-B", "-m", "sf_assistant.evaluation", "knowledge", "--help"],
                       capture_output=True, text=True, timeout=60, env=env)
    assert p.returncode == 0 and p.stdout.startswith("usage: python -m sf_assistant.evaluation knowledge ")
    found = re.findall(r"^  (--[a-z-]+)", p.stdout, re.M)
    assert found == RUN_LOCAL_OPTIONS
    top = subprocess.run([sys.executable, "-B", "-m", "sf_assistant.evaluation", "--help"], capture_output=True,
                         text=True, timeout=60, env=env).stdout
    assert "knowledge" in top and "run_local.py" in top


def test_resolve_knowledge(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rel = "tests/fixtures/Internal_Interview__c.json"
    assert knowledge_eval.resolve_knowledge(rel) == str(HOME / rel)          # not here: read from the sf_assistant folder
    (tmp_path / "x.json").write_text("{}", encoding="utf-8")
    assert knowledge_eval.resolve_knowledge("x.json") == "x.json"            # a file here wins
    assert knowledge_eval.resolve_knowledge(str(FIXTURE)) == str(FIXTURE)
    assert knowledge_eval.resolve_knowledge("nope/Nope__c.json") == "nope/Nope__c.json"
    if (KNOWLEDGE_DIR / "Internal_Interview__c.json").is_file() and KNOWLEDGE_DIR == HOME / "knowledge" / "objects":
        assert knowledge_eval.resolve_knowledge("knowledge/objects/Internal_Interview__c.json") == \
            str(HOME / "knowledge" / "objects" / "Internal_Interview__c.json")


def test_run_label():
    assert re.fullmatch(r"Internal_Interview__c_\d{8}_\d{6}", knowledge_eval.run_label("Internal_Interview__c", False))
    assert re.fullmatch(r"QMS_Setting__mdt_\d{8}_\d{6}_dry", knowledge_eval.run_label("QMS_Setting__mdt", True))
    assert re.fullmatch(r"a_b_\d{8}_\d{6}", knowledge_eval.run_label("a/b", False))
    assert re.fullmatch(r"object_\d{8}_\d{6}", knowledge_eval.run_label("..", False))


def _git_ok() -> bool:
    if shutil.which("git") is None:
        return False
    p = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    return p.returncode == 0 and p.stdout.strip() == "true"


@pytest.mark.skipif(not _git_ok(), reason="git or the repository work tree is not there")
def test_run_folders_are_git_ignored():
    paths = [f"reports/knowledge_eval/Internal_Interview__c_20261021_201500_dry/{f}" for f in OUTPUTS]
    paths.append("reports/knowledge_eval/x_regraded/summary.md")
    p = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "--no-index", "--stdin"], input="\n".join(paths) + "\n",
                       capture_output=True, text=True)
    assert p.returncode in (0, 1), p.stderr
    assert sorted(set(paths) - set(p.stdout.split())) == []


def test_module_imports_only_the_knowledge_side():
    """Static: what knowledge_eval imports (also inside functions) is config and the knowledge side, never record code;
    at import time only config (``python -m sf_assistant.evaluation`` builds its parser with it for every command)."""
    allowed = {"sf_assistant.config", "sf_assistant.agent.knowledge_agent", "sf_assistant.agent.llm",
               "sf_assistant.agent.mock_llm", "sf_assistant.agent.question", "sf_assistant.evaluation.grading",
               "sf_assistant.evaluation.questions", "sf_assistant.evaluation.report", "sf_assistant.knowledge.search",
               "sf_assistant.knowledge.store", "sf_assistant.tools.knowledge_tools"}
    tree = ast.parse(Path(knowledge_eval.__file__).read_text(encoding="utf-8"))
    mods = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert sorted(m for m in mods if m.startswith("sf_assistant") and m not in allowed) == []
    assert "duckdb" not in mods
    code = ("import sys\nimport sf_assistant.evaluation.__main__\n"
            "print(sorted(m for m in ('pandas', 'duckdb', 'openai', 'sf_assistant.agent.knowledge_agent', "
            "'sf_assistant.evaluation.report') if m in sys.modules))")
    p = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True, timeout=60, check=True)
    assert p.stdout.strip() == "[]"


# ------------------------------------------------------------------------------------------------ runs (mock model)
def test_dry_run_writes_the_six_files(tmp_path, capsys):
    code, out, err = _knowledge(["--dry", "--knowledge", str(FIXTURE), "--out", str(tmp_path), "--label", "r",
                                 "--sample", "30", "--quiet"], capsys)
    run_dir = tmp_path / "r"
    assert code == 0, err
    assert sorted(p.name for p in run_dir.iterdir()) == OUTPUTS
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert (cfg["base_url"], cfg["model"], cfg["api_key"], cfg["knowledge_path"]) == \
        ("http://dry-run.invalid/v1", "mock-model", "EMPTY", str(FIXTURE))
    qs, rows = _rows(run_dir / "questions.jsonl"), _rows(run_dir / "results.jsonl")
    assert len(qs) == len(rows) == 30 and [q["qid"] for q in qs] == [r["qid"] for r in rows]
    assert len({q["category"] for q in qs}) >= 20                              # stratified: every category first
    assert all(r["tool_calls"] and r["error"] is None for r in rows)
    head = (run_dir / "results.csv").read_text(encoding="utf-8").split("\n", 1)[0].split(",")
    assert head == list(rows[0])
    md = (run_dir / "summary.md").read_text(encoding="utf-8")
    assert md.startswith("# Object knowledge evaluation summary") and "## Per category" in md and "## Failures" in md
    cov = json.loads((run_dir / "coverage.json").read_text(encoding="utf-8"))
    assert {"touched", "never_touched", "ratio", "question_coverage", "attribute_coverage",
            "knowledge_inconsistencies"} <= set(cov)
    assert out.startswith("config: {") and "object Internal_Interview__c: 72 fields, 138 chunks; 0 error(s)" in out
    assert "per-category summary:" in out and "knowledge gaps:" in out and f"run dir: {run_dir}" in out
    assert "[agent]" not in out                                                # --quiet


def test_default_folder_is_object_and_time(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(knowledge_eval, "DEFAULT_OUT", tmp_path / "reports" / "knowledge_eval")
    code, out, _ = _knowledge(["--dry", "--knowledge", str(FIXTURE), "--sample", "5", "--llm-questions", "0",
                               "--paraphrase-n", "0", "--no-judge", "--quiet"], capsys)
    assert code == 0
    (run_dir,) = list((tmp_path / "reports" / "knowledge_eval").iterdir())
    assert re.fullmatch(r"Internal_Interview__c_\d{8}_\d{6}_dry", run_dir.name)
    assert sorted(p.name for p in run_dir.iterdir()) == OUTPUTS and f"run dir: {run_dir}" in out


def test_model_run_masks_the_key(tmp_path, capsys, monkeypatch):
    """Without --dry: the connectivity check and every call go to the model (here a MockClient patched in); the key
    is in no output, and the folder has no _dry."""
    real = llm_mod.LLM
    monkeypatch.setattr(llm_mod, "LLM", lambda cfg, client=None: real(cfg, client=MockClient()))
    monkeypatch.setenv("OKEVAL_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("OKEVAL_API_KEY", "sk-test-not-a-real-key-0001")
    monkeypatch.setattr(knowledge_eval, "DEFAULT_OUT", tmp_path)
    code, out, err = _knowledge(["--knowledge", str(FIXTURE), "--sample", "12", "--quiet"], capsys)
    assert code == 0, err
    (run_dir,) = list(tmp_path.iterdir())
    assert re.fullmatch(r"Internal_Interview__c_\d{8}_\d{6}", run_dir.name)
    assert "endpoint ok (" in out
    cfg = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
    assert cfg["api_key"] == "***" and cfg["base_url"] == "http://127.0.0.1:9/v1"
    texts = [out, err] + [(run_dir / f).read_text(encoding="utf-8") for f in OUTPUTS]
    assert not any("sk-test-not-a-real-key-0001" in t for t in texts)


def test_unreachable_endpoint_exits_3(tmp_path, capsys, monkeypatch):
    class Refusing:
        class chat:
            class completions:
                @staticmethod
                def create(**_kw):
                    raise RuntimeError("connection refused (test)")

    real = llm_mod.LLM
    monkeypatch.setattr(llm_mod, "LLM", lambda cfg, client=None: real(cfg, client=Refusing()))
    code, out, err = _knowledge(["--knowledge", str(FIXTURE), "--base-url", "http://127.0.0.1:9/v1",
                                 "--out", str(tmp_path), "--label", "x"], capsys)
    assert code == 3 and "endpoint NOT reachable: RuntimeError connection refused (test)" in err
    assert not (tmp_path / "x").exists()


def _bad_knowledge_files(folder: Path) -> dict[str, tuple[Path, str]]:
    """Knowledge files that cannot be read or walked: name -> (path, the exception it raises)."""
    folder.mkdir(parents=True, exist_ok=True)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    contents: dict[str, tuple[bytes | None, str]] = {
        "missing": (None, "FileNotFoundError"),
        "empty": (b"", "JSONDecodeError"),
        "badjson": (b"{", "JSONDecodeError"),
        "list": (b"[1, 2]", "TypeError"),
        "latin1": (b'{"object": "\xe9"}', "UnicodeDecodeError"),
        "folder": (b"", "IsADirectoryError"),
        "fields_dict": (json.dumps({**data, "fields": {"x": 1}}).encode(), "AttributeError"),     # raised by validate()
        "object_str": (json.dumps({**data, "object": "x"}).encode(), "AttributeError"),           # raised by the load
    }
    out = {}
    for name, (content, exc) in contents.items():
        path = folder / f"{name}.json"
        if name == "folder":
            path.mkdir(exist_ok=True)
        elif content is not None:
            path.write_bytes(content)
        out[name] = (path, exc)
    return out


def test_bad_knowledge_files_exit_2(tmp_path, capsys):
    """One stderr line and exit 2 for every file that cannot be read or walked (run_local.py: a traceback, exit 1),
    and exit 2 with run_local.py's lines for a file whose checks find errors; never a run folder."""
    for name, (path, exc) in _bad_knowledge_files(tmp_path / "bad").items():
        code, out, err = _knowledge(["--dry", "--knowledge", str(path), "--out", str(tmp_path / "out")], capsys)
        assert code == 2, (name, err)
        assert err.startswith(f"cannot read the knowledge file {path}: {exc}: ") and err.count("\n") == 1, (name, err)
        assert out.startswith("config: {") and out.count("\n") == 1, (name, out)        # the config line only
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    data["fields"].append(dict(data["fields"][0]))                          # a duplicate field api_name: an error
    dup = tmp_path / "Dup__c.json"
    dup.write_text(json.dumps(data), encoding="utf-8")
    code, out, err = _knowledge(["--dry", "--knowledge", str(dup), "--out", str(tmp_path / "out")], capsys)
    assert code == 2 and err == "fix the knowledge JSON before evaluating it\n"
    assert "2 error(s)" in out and "  - duplicate field api_name 'name' (2 times)\n" in out
    assert not (tmp_path / "out").exists()


def test_a_saved_run_needs_its_own_knowledge_file(tmp_path, capsys):
    """--rerun-failed, --regrade and --questions read a run made with one knowledge file: with another file name the
    command stops (exit 2) before it loads the knowledge or calls a model; the same file name elsewhere is fine."""
    other = tmp_path / "Other__c.json"
    shutil.copyfile(FIXTURE, other)
    out_dir = tmp_path / "out"
    base = ["--dry", "--out", str(out_dir), "--quiet", "--no-judge"]
    assert _knowledge([*base, "--knowledge", str(other), "--label", "first", "--sample", "20"], capsys)[0] == 0
    first = out_dir / "first"
    for extra in (["--rerun-failed", str(first)], ["--regrade", str(first)],
                  ["--questions", str(first / "questions.jsonl")]):
        for knowledge in (["--knowledge", str(FIXTURE)], []):                 # another file, the default file
            code, out, err = _knowledge([*base, *knowledge, "--label", "again", *extra], capsys)
            assert code == 2, (extra, knowledge, err)
            assert err.startswith(f"{first} was made with the knowledge file {other}, not ") and \
                err.endswith(": pass --knowledge with that run's file\n"), err
            assert out.startswith("config: {") and "object " not in out and not (out_dir / "again").exists()
    # the same file name in another folder (a relative path, the notebook folder's copy) is the same knowledge file
    ns = knowledge_eval.parse_args(["--regrade", str(first)])
    assert knowledge_eval.saved_run_mismatch(ns, str(tmp_path / "elsewhere" / "Other__c.json")) is None
    assert knowledge_eval.saved_run_mismatch(ns, "Other__c.json") is None
    assert knowledge_eval.saved_run_mismatch(knowledge_eval.parse_args(["--rerun-failed", str(tmp_path)]),
                                             str(FIXTURE)) is None        # no config.json there: not checked
    code, out, _ = _knowledge([*base, "--knowledge", str(other), "--label", "again", "--regrade", str(first)], capsys)
    assert code == 0 and f"re-grading 20 saved answers from {first}" in out


def test_no_question_left_exits_2(tmp_path, capsys):
    """Nothing to answer or grade: one stderr line, exit 2, no run folder (run_local.py: KeyError 'passed', exit 1,
    and a folder with an empty questions.jsonl)."""
    base = ["--dry", "--knowledge", str(FIXTURE), "--out", str(tmp_path), "--quiet", "--no-judge"]
    assert _knowledge([*base, "--label", "first", "--sample", "40"], capsys)[0] == 0
    first = tmp_path / "first"
    allpass = tmp_path / "allpass"                                          # a run without failures
    shutil.copytree(first, allpass)
    (allpass / "results.jsonl").write_text("".join(json.dumps({**r, "passed": True}) + "\n"
                                                   for r in _rows(first / "results.jsonl")), encoding="utf-8")
    cases = [
        (["--rerun-failed", str(allpass)], f"no questions to answer: {allpass} has no failed question"),
        (["--questions", str(first / "questions.jsonl"), "--qids", "no-such-qid"],
         "no questions to answer: --qids kept none of the 40 questions"),
        (["--regrade", str(first), "--qids", "no-such-qid,"],
         "no questions to grade: --qids kept none of the 40 questions"),
        (["--sample", "-3", "--llm-questions", "0", "--paraphrase-n", "0"],
         "no questions to answer: --sample -3 keeps none"),
    ]
    for extra, message in cases:
        code, out, err = _knowledge([*base, "--label", "x", *extra], capsys)
        assert code == 2 and err.startswith(message) and err.endswith("; nothing written\n") and err.count("\n") == 1, \
            (extra, err)
        assert "answered " not in out and not (tmp_path / "x").exists()


def test_regrade_without_the_judge_calls_no_model(tmp_path, capsys, monkeypatch):
    """--regrade <run> --no-judge needs no model: no connectivity check either (run_local.py sends one)."""
    base = ["--knowledge", str(FIXTURE), "--out", str(tmp_path), "--quiet"]
    assert _knowledge(["--dry", *base, "--label", "first", "--sample", "12", "--no-judge"], capsys)[0] == 0
    calls: list[dict] = []

    class Refusing:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    calls.append(kw)
                    raise RuntimeError("connection refused (test)")

    real = llm_mod.LLM
    monkeypatch.setattr(llm_mod, "LLM", lambda cfg, client=None: real(cfg, client=Refusing()))
    url = ["--base-url", "http://127.0.0.1:9/v1"]
    code, out, err = _knowledge([*base, *url, "--label", "rg", "--regrade", str(tmp_path / "first"), "--no-judge"],
                                capsys)
    assert code == 0, err
    assert calls == [] and "endpoint ok" not in out and "endpoint NOT" not in err
    assert "tokens: {'prompt_tokens': 0, 'completion_tokens': 0, 'calls': 0}" in out
    pick = ("qid", "answer", "passed", "fact_recall")
    assert [[r[k] for k in pick] for r in _rows(tmp_path / "rg" / "results.jsonl")] == \
        [[r[k] for k in pick] for r in _rows(tmp_path / "first" / "results.jsonl")]
    code, _, err = _knowledge([*base, *url, "--label", "rg2", "--regrade", str(tmp_path / "first")], capsys)
    assert code == 3 and "endpoint NOT reachable" in err and len(calls) == 1      # the judge needs the model
    assert not (tmp_path / "rg2").exists()


def test_questions_qids_rerun_failed_and_regrade(tmp_path, capsys):
    base = ["--dry", "--knowledge", str(FIXTURE), "--out", str(tmp_path), "--quiet"]
    assert _knowledge([*base, "--label", "first", "--sample", "40"], capsys)[0] == 0
    first = _rows(tmp_path / "first" / "results.jsonl")
    failed = [r["qid"] for r in first if not r["passed"]]
    passed = [r["qid"] for r in first if r["passed"]]
    assert failed and passed

    keep = [passed[0], failed[0]]
    code, out, _ = _knowledge([*base, "--label", "picked", "--questions", str(tmp_path / "first" / "questions.jsonl"),
                               "--qids", ",".join(keep)], capsys)
    assert code == 0 and "2 questions to answer" in out
    assert sorted(r["qid"] for r in _rows(tmp_path / "picked" / "results.jsonl")) == sorted(keep)

    code, out, _ = _knowledge([*base, "--label", "again", "--rerun-failed", str(tmp_path / "first")], capsys)
    assert code == 0 and f"re-running {len(failed)} failed questions from {tmp_path / 'first'}" in out
    assert [r["qid"] for r in _rows(tmp_path / "again" / "results.jsonl")] == failed

    code, out, _ = _knowledge([*base, "--label", "regraded", "--regrade", str(tmp_path / "first"), "--no-judge"], capsys)
    assert code == 0 and f"re-grading 40 saved answers from {tmp_path / 'first'}" in out
    assert "generating questions" not in out and "answered " not in out     # no question asked again
    regraded = _rows(tmp_path / "regraded" / "results.jsonl")
    assert [(r["qid"], r["answer"]) for r in regraded] == [(r["qid"], r["answer"]) for r in first]
    assert all(r["judge"] is None for r in regraded)


# ------------------------------------------------------------------------------------------------ parity with OLD
#: The clock frozen in both processes (time.perf_counter, time.time), so latency_s, avg_latency_s and the "X.Xs" of
#: the progress lines are 0 on both sides: the files must then be byte-identical, with only the folders replaced.
_FREEZE = "import runpy, sys, time\ntime.perf_counter = lambda: 1000.0\ntime.time = lambda: 1700000000.0\n"


def _old_command(args: list[str]) -> list[str]:
    code = _FREEZE + f"sys.argv = [{str(RUN_LOCAL)!r}] + sys.argv[1:]\nrunpy.run_path({str(RUN_LOCAL)!r}, run_name='__main__')\n"
    return [sys.executable, "-B", "-c", code, *args]


def _new_command(args: list[str]) -> list[str]:
    code = _FREEZE + ("sys.argv = ['sf_assistant.evaluation'] + sys.argv[1:]\n"
                      "runpy.run_module('sf_assistant.evaluation', run_name='__main__', alter_sys=True)\n")
    return [sys.executable, "-B", "-c", code, "knowledge", *args]


def _side(cmd: list[str], cwd: Path, out: Path, label: str) -> dict:
    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKEVAL_", "SFA_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    run_dir = out / label

    def norm(text: str) -> str:
        return text.replace(str(out), "<OUT>")

    res = {"exit": p.returncode, "stdout": norm(p.stdout), "stderr": norm(p.stderr),
           "files": sorted(x.name for x in run_dir.iterdir()) if run_dir.is_dir() else []}
    for f in OUTPUTS:
        res[f] = norm((run_dir / f).read_text(encoding="utf-8")) if (run_dir / f).is_file() else None
    return res


def _old_state() -> dict:
    """What a run could leave in OLD: its default run folders and bytecode next to the modules."""
    def names(p: Path) -> list[str]:
        return sorted(os.listdir(p)) if p.is_dir() else []
    return {"runs": names(OLD_DIR / "runs"), "pycache": names(OLD_DIR / "okeval" / "__pycache__"),
            "top_pycache": names(OLD_DIR / "__pycache__")}


@pytest.mark.skipif(not (RUN_LOCAL.is_file() and (OLD_DIR / "okeval" / "agent.py").is_file()),
                    reason="the notebook folder notebooks/object_knowledge_eval (run_local.py) is not there")
def test_parity_with_the_notebook_run_local(tmp_path):
    before = _old_state()
    outs = {"old": tmp_path / "old" / "out", "new": tmp_path / "new" / "out"}
    full = {s: str(outs[s] / "full") for s in outs}

    def args(side: str, *extra: str) -> list[str]:
        return ["--dry", "--knowledge", str(FIXTURE), "--out", str(outs[side]), *extra]

    def both(label: str, extra) -> dict:
        with ThreadPoolExecutor(2) as pool:
            futs = {s: pool.submit(_side, (_old_command if s == "old" else _new_command)(args(s, *extra(s), "--label", label)),
                                   tmp_path / s / "cwd", outs[s], label) for s in outs}
            return {s: f.result() for s, f in futs.items()}

    results = {"full": both("full", lambda s: [])}
    rows = _rows(outs["old"] / "full" / "results.jsonl")
    qids = ",".join([r["qid"] for r in rows if not r["passed"]][:2] + [r["qid"] for r in rows if r["passed"]][:2]
                    + [r["qid"] for r in rows if r["source"] == "paraphrase"][:1])
    scenarios = {
        "sample_nojudge": lambda s: ["--sample", "40", "--no-judge"],
        "questions_qids": lambda s: ["--questions", full[s] + "/questions.jsonl", "--qids", qids],
        "rerun_failed": lambda s: ["--rerun-failed", full[s]],
        "regrade": lambda s: ["--regrade", full[s], "--no-judge"],
        "regrade_judge_qids": lambda s: ["--regrade", full[s], "--qids", qids],
        "opts": lambda s: ["--llm-questions", "1", "--paraphrase-n", "2", "--paraphrase-sample", "5",
                           "--max-tool-steps", "2", "--workers", "1", "--quiet", "--judge-model", "judge-x",
                           "--enable-thinking"],
    }
    for label, extra in scenarios.items():
        results[label] = both(label, extra)
    diffs = []
    for label, res in results.items():
        assert res["old"]["exit"] == 0, (label, res["old"]["stderr"][-2000:])
        assert res["new"]["files"] == OUTPUTS, (label, res["new"]["stderr"][-2000:])
        for key in ("exit", "stdout", "stderr", "files", *OUTPUTS):
            if res["old"][key] != res["new"][key]:
                diffs.append(f"{label}: {key}")
    assert diffs == []
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OKEVAL_", "SFA_"))}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    old_help = subprocess.run([sys.executable, "-B", str(RUN_LOCAL), "--help"], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=60).stdout
    assert re.findall(r"^  (--[a-z-]+)", old_help, re.M) == RUN_LOCAL_OPTIONS
    assert _old_state() == before                    # nothing written into the notebook folder


@pytest.mark.skipif(not (RUN_LOCAL.is_file() and (OLD_DIR / "okeval" / "agent.py").is_file()),
                    reason="the notebook folder notebooks/object_knowledge_eval (run_local.py) is not there")
def test_parity_bad_input_one_line_and_exit_2(tmp_path):
    """The deliberate differences on bad input, pinned against run_local.py. Where run_local.py stops with a traceback
    (exit 1), this command prints the same stdout and exits 2 with one stderr line naming the same exception.

    A knowledge file that cannot be read or walked: ``cannot read the knowledge file <path>: <exception>``. No
    question left (a --rerun-failed run without failures): run_local.py prints one more line (``answered 0 ...``),
    stops with ``KeyError: 'passed'`` and leaves a folder with an empty questions.jsonl; this command writes nothing."""
    before = _old_state()
    bad = _bad_knowledge_files(tmp_path / "bad")
    outs = {"old": tmp_path / "old" / "out", "new": tmp_path / "new" / "out"}

    def both(label: str, extra) -> dict:
        with ThreadPoolExecutor(2) as pool:
            futs = {s: pool.submit(_side, (_old_command if s == "old" else _new_command)(
                        ["--dry", *extra(s), "--out", str(outs[s]), "--label", label]), tmp_path / s / "cwd", outs[s],
                        label) for s in outs}
            return {s: f.result() for s, f in futs.items()}

    def last_exception(stderr: str) -> str:
        name, _, msg = stderr.strip().splitlines()[-1].partition(": ")
        return f"{name.rsplit('.', 1)[-1]}: {msg}"           # json.decoder.JSONDecodeError -> JSONDecodeError

    with ThreadPoolExecutor(4) as pool:
        res = dict(zip(bad, pool.map(lambda kv: both(kv[0], lambda s, p=kv[1][0]: ["--knowledge", str(p)]),
                                     bad.items())))
    for name, (path, exc) in bad.items():
        old, new = res[name]["old"], res[name]["new"]
        assert old["exit"] == 1 and "Traceback (most recent call last)" in old["stderr"], (name, old["stderr"][-500:])
        assert last_exception(old["stderr"]).startswith(f"{exc}: "), (name, old["stderr"][-500:])
        assert new["exit"] == 2 and new["stdout"] == old["stdout"] and new["files"] == old["files"] == [], name
        assert new["stderr"] == f"cannot read the knowledge file {path}: {last_exception(old['stderr'])}\n", name

    assert both("full", lambda s: ["--knowledge", str(FIXTURE), "--sample", "20", "--no-judge"])["old"]["exit"] == 0
    for s in outs:                                            # the same run with every question passed
        shutil.copytree(outs[s] / "full", outs[s] / "allpass")
        rows = _rows(outs[s] / "allpass" / "results.jsonl")
        (outs[s] / "allpass" / "results.jsonl").write_text(
            "".join(json.dumps({**r, "passed": True}) + "\n" for r in rows), encoding="utf-8")
    r = both("again", lambda s: ["--knowledge", str(FIXTURE), "--rerun-failed", str(outs[s] / "allpass"), "--no-judge"])
    old, new = r["old"], r["new"]
    assert old["exit"] == 1 and old["stderr"].strip().endswith("KeyError: 'passed'"), old["stderr"][-500:]
    assert old["files"] == ["questions.jsonl"] and old["questions.jsonl"] == ""
    assert new["exit"] == 2 and new["files"] == [], new["stderr"]
    head = old["stderr"].split("Traceback (most recent call last)")[0]   # the questions' log lines, on both sides
    assert new["stderr"] == head + "no questions to answer: <OUT>/allpass has no failed question; nothing written\n"
    assert "  re-running 0 failed questions from <OUT>/allpass\n" in new["stdout"]
    rest = old["stdout"][len(new["stdout"]):]
    assert old["stdout"].startswith(new["stdout"]) and rest.startswith("answered 0 in 0.0s; errors: 0; tokens: ") \
        and rest.count("\n") == 1, rest
    assert _old_state() == before                    # nothing written into the notebook folder
