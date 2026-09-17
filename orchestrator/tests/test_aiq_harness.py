"""scripts/aiq: the acceptance harness, its scoring, its sandbox and its gate.

These tests keep the harness itself honest:

  * the committed baseline (runs/baseline-20260917, the production image
    sha256:cec61feca1f7 = main 9ef4602) still re-scores to the numbers the
    round was planned against, so a change to a check that silently moves the
    baseline is caught here rather than in a comparison months later;
  * the 40 baseline cases are frozen, and every check added this round is
    keyed on an `expect` none of them carries;
  * the stack refuses the head, the production ports and a production-looking
    name BEFORE it creates anything;
  * the code sandbox really runs code, and really fails wrong code.

Nothing here talks to an orchestrator, a model or the network.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AIQ = REPO / "scripts" / "aiq"
# APPENDED, not prepended: these are plain top-level module names ("cases",
# "run", "gate"), and this suite must not be able to shadow anything the rest
# of the session imports.
if str(AIQ) not in sys.path:
    sys.path.append(str(AIQ))

import cases as C  # noqa: E402
import code_sandbox as CS  # noqa: E402
import gate as G  # noqa: E402
import harness as H  # noqa: E402
import reference_solutions as RS  # noqa: E402
import run as R  # noqa: E402

BASELINE = AIQ / "runs" / "baseline-20260917"
#: what the round was planned against (plan.json, baseline_scores)
BASELINE_OVERALL = 0.761
BASELINE_CATEGORIES = {"howto": 1.000, "tables": 1.000, "fast_factual": 0.933, "format_rewrite": 0.821,
                       "followup_edit": 0.765, "big_report": 0.737, "csv_plot": 0.363}
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="the sandbox refuses to run model-written code as root")


@pytest.fixture(scope="module")
def baseline_summary():
    results = json.load(open(BASELINE / "results.json"))
    return R.summarise(R.rescore(results))


# ------------------------------------------------------------- the baseline --

def test_the_committed_baseline_rescores_to_its_published_numbers(baseline_summary):
    assert baseline_summary["headline"]["cases"] == 40
    assert baseline_summary["headline"]["overall_score"] == BASELINE_OVERALL
    got = {k: v["mean_score"] for k, v in baseline_summary["by_category"].items() if k in BASELINE_CATEGORIES}
    assert got == BASELINE_CATEGORIES


def test_the_baseline_still_shows_the_failures_the_owner_reported(baseline_summary):
    h = baseline_summary["headline"]
    # every CSV plot job failed with "The artifact has no charts to draw as images"
    assert baseline_summary["by_category"]["csv_plot"]["mean_score"] <= 0.45
    assert h["no_charts_to_draw"] > 0, "the reported chart failure must still be visible in the baseline"
    # two Fast turns thought (the adaptive grant)
    assert h["fast_turns_thinking"] == 2 and h["fast_thinking_case_ids"] == ["Q04", "Q05"]
    # F01, the Chat B shape, came back as run-on plain lines
    f01 = next(c for c in json.load(open(BASELINE / "results.json"))["cases"] if c["id"] == "F01")
    md = H.md_metrics(f01["turns"][0]["result"]["answer"])
    assert md["headings"] == 0 and md["bullets"] == 0 and md["bold"] == 0
    assert md["runon_ratio"] >= 0.9
    # documents stayed small
    assert h["doc_pages_median"] <= 3


def test_the_forty_baseline_cases_are_frozen():
    assert len(C.BASELINE_CASES) == 40
    assert [c["id"] for c in C.BASELINE_CASES][:3] == ["F01", "F02", "F03"]
    assert len(C.ACCEPTANCE_CASES) == 6
    assert {c["id"] for c in C.ACCEPTANCE_CASES} == {"C01", "E07", "R07", "P09", "F07", "K01"}
    assert len(C.CODING_CASES) == 20
    assert len({c["id"] for c in C.CASES}) == 66


def test_no_baseline_case_uses_a_check_added_this_round():
    """Why the baseline still re-scores: every new check is opt-in."""
    added = {"doc_max_pages", "doc_max_words", "doc_max_growth", "chart_after_heading", "chart_hues_max",
             "chart_subject_hues_min", "status_colours", "no_chart_claimed", "code"}
    used = {k for c in C.BASELINE_CASES for t in c["turns"] for k in t["expect"]}
    assert used & added == set()
    assert all(H.DIMENSION.get(k) for k in added - {"code"})


def test_every_case_is_english_and_fast():
    for case in C.CASES:
        assert case["effort"] == "fast", case["id"]
        for turn in case["turns"]:
            turn["message"].encode("ascii", "ignore")  # no smart quotes needed, but ASCII must survive
            assert turn["message"].strip()


# -------------------------------------------------------- the added checks --

def _check(expect, res, **kw):
    kw.setdefault("effort", "fast")
    kw.setdefault("fail_text", C.FAIL_TEXT)
    kw.setdefault("prev_file", None)
    kw.setdefault("upload_path", None)
    return {c["check"]: c for c in H.check_turn(expect, res, **kw)}


def _turn(answer="", **res):
    out = {"answer": answer, "md": H.md_metrics(answer), "reasoning_events": 0, "meta": {}}
    out.update(res)
    return out


def _art(files=None, hues=None, **kw):
    art = {"ref": {"artifact_id": "a" * 32, "version": 1, "status": "completed", "job_id": "j" * 32},
           "files": files or [], "charts": None, "hues": hues or [], "spec_blocks": {}}
    art.update(kw)
    return art


def test_a_short_brief_that_grows_fails_doc_max_pages_and_growth():
    prev = _art(files=[{"format": "pdf", "pages": 1, "words": 400}])
    now = _art(files=[{"format": "pdf", "pages": 6, "words": 3000}])
    checks = _check({"doc_max_pages": 2, "doc_max_words": 900, "doc_max_growth": 1.25},
                    _turn("Here it is.", artifact=now), prev_file=prev)
    assert not checks["doc_max_pages"]["ok"] and not checks["doc_max_words"]["ok"]
    assert not checks["doc_max_growth"]["ok"]
    same = _art(files=[{"format": "pdf", "pages": 1, "words": 410}])
    ok = _check({"doc_max_pages": 2, "doc_max_words": 900, "doc_max_growth": 1.25},
                _turn("Retitled.", artifact=same), prev_file=prev)
    assert all(c["ok"] for c in ok.values())


def test_a_chart_appended_at_the_end_does_not_count_as_added_to_a_section():
    blocks = [{"type": "heading", "level": 1, "text": "Sales report", "": ""},
              {"type": "heading", "level": 2, "text": "Overview"},
              {"type": "paragraph", "level": 0, "text": "…"},
              {"type": "heading", "level": 2, "text": "Regional performance"},
              {"type": "paragraph", "level": 0, "text": "…"},
              {"type": "heading", "level": 2, "text": "Outlook"},
              {"type": "chart", "level": 0, "text": "Revenue by region"}]
    ok, why = H.chart_follows_heading(blocks, ["regional performance"])
    assert not ok and "Regional performance" in why
    blocks.insert(4, {"type": "chart", "level": 0, "text": "Revenue by region"})
    ok, why = H.chart_follows_heading(blocks, ["regional performance"])
    assert ok, why


def test_a_missing_heading_is_a_failure_not_a_crash():
    ok, why = H.chart_follows_heading([{"type": "heading", "level": 1, "text": "Report"}], ["regional performance"])
    assert not ok and "no heading matching" in why


def test_an_edit_that_claims_a_chart_without_adding_one_is_caught():
    assert H.claims_chart_added("I've added a chart of revenue by region to the document.")
    assert H.claims_chart_added("A bar chart has been added under Regional performance.")
    assert not H.claims_chart_added("The document already has a chart; I renamed the title.")
    prev = _art(charts=[{"type": "bar"}])
    now = _art(charts=[{"type": "bar"}])
    checks = _check({"no_chart_claimed": True}, _turn("I added a chart of revenue by region.", artifact=now),
                    prev_file=prev)
    assert not checks["no_chart_claimed"]["ok"]
    grew = _art(charts=[{"type": "bar"}, {"type": "pie"}])
    checks = _check({"no_chart_claimed": True}, _turn("I added a chart of revenue by region.", artifact=grew),
                    prev_file=prev)
    assert checks["no_chart_claimed"]["ok"]


def _png(colours, size=(120, 60)):
    """A band of solid colours, as a PNG — one band per colour."""
    from PIL import Image
    im = Image.new("RGB", size, "white")
    w = size[0] // max(1, len(colours))
    for i, rgb in enumerate(colours):
        for x in range(i * w, (i + 1) * w):
            for y in range(size[1]):
                im.putpixel((x, y), rgb)
    import io
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def test_one_subject_is_one_hue_and_two_subjects_are_two():
    blue, orange = (47, 111, 178), (224, 123, 0)
    one = H.hue_profile(_png([blue]))
    assert one["hues"] == 1
    checks = _check({"chart_hues_max": 1}, _turn("", artifact=_art(hues=[one])))
    assert checks["chart_hues_max"]["ok"]
    two = [H.hue_profile(_png([blue])), H.hue_profile(_png([orange]))]
    checks = _check({"chart_subject_hues_min": 2}, _turn("", artifact=_art(hues=two)))
    assert checks["chart_subject_hues_min"]["ok"]
    same_twice = [H.hue_profile(_png([blue])), H.hue_profile(_png([blue]))]
    checks = _check({"chart_subject_hues_min": 2}, _turn("", artifact=_art(hues=same_twice)))
    assert not checks["chart_subject_hues_min"]["ok"], "two charts in the same blue are the reported defect"


def test_a_status_chart_must_show_the_success_and_danger_hues():
    green, red, amber, blue = (30, 107, 52), (155, 28, 28), (224, 160, 0), (47, 111, 178)
    status = [H.hue_profile(_png([green, amber, red]), min_saturation=0.18)]
    ok, why = H.status_hue_verdict(status)
    assert ok, why
    all_blue = [H.hue_profile(_png([blue]), min_saturation=0.18)]
    ok, why = H.status_hue_verdict(all_blue)
    assert not ok and "success" in why
    checks = _check({"status_colours": True}, _turn("", artifact=_art(hues=[], hues_soft=all_blue)))
    assert not checks["status_colours"]["ok"]


# ------------------------------------------------------------ the sandbox --

PARSE_DURATION_GOOD = '''\
Here you go.

```python
import re

_UNITS = {"h": 3600, "m": 60, "s": 1}
_PART = re.compile(r"(\\d+)([hms])")


def parse_duration(text):
    text = (text or "").strip().lower()
    if not text or not re.fullmatch(r"(\\d+[hms])+", text):
        raise ValueError(f"not a duration: {text!r}")
    return sum(int(n) * _UNITS[u] for n, u in _PART.findall(text))
```
'''

PARSE_DURATION_BAD = '''\
```python
def parse_duration(text):
    total = 0
    number = ""
    for ch in text:
        if ch.isdigit():
            number += ch
        else:
            total += int(number or 0) * {"h": 3600, "m": 60, "s": 1}.get(ch, 0)
            number = ""
    return total
```
'''


@pytest.fixture(scope="module")
def toolchain(tmp_path_factory):
    return CS.Toolchain(str(tmp_path_factory.mktemp("aiq-code")))


def _case(cid):
    return next(c for c in C.CODING_CASES if c["id"] == cid)["turns"][0]["expect"]["code"]


def test_the_code_block_of_the_right_language_is_what_runs():
    answer = "```text\nnot code\n```\n\n```python\nx = 1\n```\n\n```python\nprint(x)  # a longer example block\n```"
    source, blocks = CS.extract_code(answer, "python")
    assert blocks == 2 and source.strip() == "print(x)  # a longer example block"
    assert CS.extract_code("no code at all", "python") == ("", 0)


@NOT_ROOT
def test_a_correct_python_answer_passes_and_a_wrong_one_fails(tmp_path, toolchain):
    good = CS.run_code(_case("CD01"), PARSE_DURATION_GOOD, str(tmp_path / "good"), toolchain)
    assert good["ok"], good["steps"]
    checks = _check({"code": _case("CD01")}, _turn(PARSE_DURATION_GOOD, code_result=good))
    assert all(checks[k]["ok"] for k in ("code_present", "code_runs", "code_correct"))

    bad = CS.run_code(_case("CD01"), PARSE_DURATION_BAD, str(tmp_path / "bad"), toolchain)
    assert not bad["ok"], "an answer that never raises ValueError must not pass"
    checks = _check({"code": _case("CD01")}, _turn(PARSE_DURATION_BAD, code_result=bad))
    assert checks["code_runs"]["ok"] and not checks["code_correct"]["ok"]
    assert "ValueError" in checks["code_correct"]["detail"]


@NOT_ROOT
def test_an_answer_with_no_code_at_all_fails_every_code_check(tmp_path, toolchain):
    rec = CS.run_code(_case("CD01"), "I would use a regular expression for this.", str(tmp_path), toolchain)
    checks = _check({"code": _case("CD01")}, _turn("I would use a regular expression for this.", code_result=rec))
    assert not checks["code_present"]["ok"] and not checks["code_runs"]["ok"] and not checks["code_correct"]["ok"]


@NOT_ROOT
def test_a_sql_answer_is_executed_against_the_case_schema(tmp_path, toolchain):
    good = """```sql
SELECT c.country,
       COALESCE(SUM(o.amount), 0) AS total,
       COUNT(o.id) AS orders
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.id AND o.created_at >= '2025-01-01' AND o.created_at < '2026-01-01'
GROUP BY c.country
ORDER BY total DESC, c.country ASC;
```"""
    rec = CS.run_code(_case("CD09"), good, str(tmp_path / "good"), toolchain)
    assert rec["ok"], rec["steps"]
    wrong = """```sql
SELECT c.country, SUM(o.amount), COUNT(o.id)
FROM customers c
JOIN orders o ON o.customer_id = c.id
WHERE o.created_at LIKE '2025%'
GROUP BY c.country
ORDER BY 2 DESC;
```"""
    rec = CS.run_code(_case("CD09"), wrong, str(tmp_path / "wrong"), toolchain)
    assert not rec["ok"], "an inner join drops the countries with no 2025 order"


@NOT_ROOT
def test_a_bash_answer_is_run_against_a_fixture_directory(tmp_path, toolchain):
    good = r"""```bash
#!/usr/bin/env bash
set -euo pipefail
dir=${1:-}
if [ -z "$dir" ] || [ ! -d "$dir" ]; then
  echo "usage: $0 <directory>" >&2
  exit 1
fi
find "$dir" -maxdepth 1 -type f -name '*.log' -printf '%s\t%f\n' \
  | sort -rn | head -3 | awk -F'\t' '{ printf "%s\t%s\n", $2, $1 }'
```"""
    rec = CS.run_code(_case("CD17"), good, str(tmp_path / "good"), toolchain)
    assert rec["ok"], rec["steps"]
    unquoted = r"""```bash
#!/bin/bash
for f in `ls $1/*.log`; do
  echo -e "$f\t$(stat -c%s $f)"
done | sort -k2 -rn | head -3
```"""
    rec = CS.run_code(_case("CD17"), unquoted, str(tmp_path / "unquoted"), toolchain)
    assert not rec["ok"], "a name with a space must break this answer, and the checker must see it"


@NOT_ROOT
def test_a_typescript_answer_is_type_checked_and_run(tmp_path, toolchain):
    if not toolchain.available("typescript")[0]:
        pytest.skip(toolchain.available("typescript")[1])
    good = """```ts
export function chunk<T>(items: T[], size: number): T[][] {
  if (!Number.isInteger(size) || size < 1) {
    throw new RangeError(`size must be a whole number of at least 1, got ${size}`);
  }
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(items.slice(i, i + size));
  }
  return out;
}
```"""
    rec = CS.run_code(_case("CD16"), good, str(tmp_path / "good"), toolchain)
    assert rec["ok"], rec["steps"]
    no_throw = """```ts
export function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(items.slice(i, i + size));
  }
  return out;
}
```"""
    rec = CS.run_code(_case("CD16"), no_throw, str(tmp_path / "no_throw"), toolchain)
    assert not rec["ok"], "a size of 0 must throw RangeError, and the checker must see that it did not"


@NOT_ROOT
@pytest.mark.parametrize("case", C.CODING_CASES, ids=[c["id"] for c in C.CODING_CASES])
def test_every_coding_checker_accepts_a_correct_answer(case, tmp_path, toolchain):
    """The control: a checker that rejects good code would score every model
    unfairly, and the failure would read as a model regression."""
    spec = case["turns"][0]["expect"]["code"]
    available, why = toolchain.available(spec["lang"])
    if not available:
        pytest.skip(why)
    rec = CS.run_code(spec, RS.REFERENCE[case["id"]], str(tmp_path), toolchain)
    assert rec["ok"], [(s["name"], s["rc"], s["tail"]) for s in rec["steps"]]


def test_every_coding_case_has_a_reference_answer():
    assert {c["id"] for c in C.CODING_CASES} == set(RS.REFERENCE)


@NOT_ROOT
def test_code_that_never_finishes_is_killed_and_fails(tmp_path, toolchain):
    spec = {"lang": "python", "timeout": 5, "files": {"check.py": "import solution\nprint('ok')\n"}}
    rec = CS.run_code(spec, "```python\nwhile True:\n    pass\n```", str(tmp_path), toolchain)
    assert not rec["ok"]
    assert any(s["timed_out"] for s in rec["steps"]), rec["steps"]


# ----------------------------------------- the sandbox really contains (Q-1) --
#
# Measured on the integrated tree before the fix, on BOTH Sparks: `unshare -rn
# true` fails with "write failed /proc/self/uid_map: Operation not permitted",
# so the namespace prefix was always empty. A coding case's own check.py then
# printed, with the step still reporting ok=True:
#     NETWORK REACHED: the head's vLLM port answered
#     docker on PATH: /usr/bin/docker | socket exists: True
#     DOCKER DAEMON ANSWERED: b'HTTP/1.1 200 OK'
#     parent filesystem: [(…/orchestrator/app/main.py, True), (/etc/passwd, True)]
#     WROTE OUTSIDE THE WORKDIR: /tmp/aiq-escape.txt   (and the file was there)
#     fork refused after 842 children
# The QA user is in the `docker` group, so that code could have removed the
# worker's vLLM container, which is rank 1 of the production TP=2 model.

#: a checker that does nothing but import what the model wrote, so the hostile
#: snippet below is the whole of the turn
IMPORT_ONLY = "import solution\nprint('checked')\n"


def _hostile(tmp_path, toolchain, body, timeout=30):
    spec = {"lang": "python", "timeout": timeout, "files": {"check.py": IMPORT_ONLY}}
    rec = CS.run_code(spec, f"```python\n{body}\n```", str(tmp_path), toolchain)
    assert not rec.get("unavailable"), rec.get("unavailable")
    out = "\n".join((s["stdout"] or "") + (s["stderr"] or "") for s in rec["steps"])
    return rec, out


def _contained(toolchain):
    """Skip only where docker genuinely cannot hold the code on this host.

    Deliberately asks docker itself rather than the module under test: a
    sandbox that quietly stopped containing would otherwise skip its own
    containment tests, which is how this defect survived a green suite.
    """
    docker = shutil.which("docker")
    images = getattr(CS, "PYTHON_IMAGES", ["python:3.12-slim", "python:3.11-slim"])
    if docker and any(subprocess.run([docker, "image", "inspect", name.strip()],
                                     capture_output=True, timeout=60).returncode == 0 for name in images):
        return
    pytest.skip(f"no docker or no sandbox image on this host (tried {', '.join(images)})")


@NOT_ROOT
def test_the_sandbox_gives_model_written_code_no_network(tmp_path, toolchain):
    _contained(toolchain)
    rec, out = _hostile(tmp_path, toolchain, """
import socket, urllib.request
for label, probe in (("head vLLM", lambda: socket.create_connection(("10.100.184.1", 8000), 3)),
                     ("internet", lambda: urllib.request.urlopen("http://1.1.1.1", timeout=3))):
    try:
        probe()
        print("REACHED", label)
    except Exception as exc:
        print("blocked", label, type(exc).__name__)
""")
    assert "REACHED" not in out, out
    assert "blocked head vLLM" in out and "blocked internet" in out, out
    assert rec["toolchain"]["isolated_network"] is True


@NOT_ROOT
def test_the_sandbox_keeps_model_written_code_away_from_the_docker_daemon(tmp_path, toolchain):
    _contained(toolchain)
    rec, out = _hostile(tmp_path, toolchain, """
import os, shutil, socket
print("docker on PATH:", shutil.which("docker"))
print("socket exists:", os.path.exists("/var/run/docker.sock"))
try:
    s = socket.socket(socket.AF_UNIX); s.connect("/var/run/docker.sock")
    s.sendall(b"GET /containers/json HTTP/1.1\\r\\nHost: d\\r\\n\\r\\n")
    print("DAEMON ANSWERED", s.recv(120).split(b"\\r\\n")[0])
except Exception as exc:
    print("daemon blocked", type(exc).__name__)
""")
    assert "DAEMON ANSWERED" not in out, out
    assert "docker on PATH: None" in out and "socket exists: False" in out, out
    assert "daemon blocked" in out, out
    assert rec["toolchain"]["isolated_docker"] is True


@NOT_ROOT
def test_the_sandbox_shows_the_code_nothing_of_this_machine(tmp_path, toolchain):
    _contained(toolchain)
    escape = tmp_path / "escaped-outside-the-workdir.txt"
    rec, out = _hostile(tmp_path, toolchain, f"""
import glob, os
print("repo file:", os.path.exists({str(REPO / 'orchestrator' / 'app' / 'main.py')!r}))
print("home entries:", len(glob.glob({str(Path.home() / '*')!r})))
print("workdir writable:", os.access(".", os.W_OK))
try:
    open({str(escape)!r}, "w").write("escaped")
    print("WROTE OUTSIDE THE WORKDIR")
except Exception as exc:
    print("outside write blocked", type(exc).__name__)
""")
    assert "repo file: False" in out and "home entries: 0" in out, out
    assert "workdir writable: True" in out, "the scratch directory is the one writable place"
    assert not escape.exists(), "a write outside the working directory reached this machine"
    assert rec["toolchain"]["read_only_root"] is True


@NOT_ROOT
def test_a_fork_bomb_dies_inside_the_sandbox(tmp_path, toolchain):
    _contained(toolchain)
    before = len([n for n in os.listdir("/proc") if n.isdigit()])
    rec, out = _hostile(tmp_path, toolchain, """
import os, time
n, end = 0, time.time() + 6
try:
    while time.time() < end:
        if os.fork() == 0:
            time.sleep(4); os._exit(0)
        n += 1
except OSError as exc:
    print("fork refused after", n, "children")
else:
    print("FORKED", n, "children without refusal")
""")
    assert "FORKED" not in out, out
    refused = int(out.split("fork refused after")[1].split()[0])
    assert refused <= CS.SANDBOX_PIDS, f"{refused} tasks is above the container's --pids-limit"
    after = len([n for n in os.listdir("/proc") if n.isdigit()])
    assert after - before < CS.SANDBOX_PIDS, "the fork bomb reached this machine's process table"


@NOT_ROOT
def test_a_runaway_allocation_dies_inside_the_memory_cap(tmp_path, toolchain):
    _contained(toolchain)
    rec, out = _hostile(tmp_path, toolchain, """
block, held = bytearray(32 * 1024 * 1024), []
for i in range(400):                      # 12.5 GiB if nothing stops it
    held.append(bytearray(block))
    print("held", (i + 1) * 32, "MiB", flush=True)
print("ALLOCATED 12 GiB")
""", timeout=120)
    assert "ALLOCATED" not in out, out
    assert not rec["ok"], "a step that was killed for memory must not pass"
    held = [int(line.split()[1]) for line in out.splitlines() if line.startswith("held ")]
    assert held and max(held) < 4096, f"reached {max(held)} MiB under a {CS.SANDBOX_MEMORY} cap"


@NOT_ROOT
def test_the_container_argv_carries_every_guard(toolchain, tmp_path):
    _contained(toolchain)
    argv = CS._docker_argv(toolchain, "python", str(tmp_path), "aiq-sandbox-test", ["python3", "solution.py"])
    line = " ".join(argv)
    for guard in ("--network none", "--cap-drop ALL", "--security-opt no-new-privileges", "--read-only",
                  f"--memory {CS.SANDBOX_MEMORY}", f"--cpus {CS.SANDBOX_CPUS}", f"--pids-limit {CS.SANDBOX_PIDS}",
                  f"--user {os.getuid()}:{os.getgid()}"):
        assert guard in line, f"{guard} is missing from {line}"
    assert "docker.sock" not in line, "the sandbox must never see the daemon socket"
    assert f"{tmp_path}:{CS.WORK}" in line and line.count("--volume") == 1, "only the scratch directory is bound"


def test_a_host_that_cannot_isolate_runs_no_model_code(monkeypatch, tmp_path):
    """Failing closed is the whole fix: before it, an unisolated host ran the
    code anyway and only recorded isolated_network: False."""
    monkeypatch.setenv("AIQ_SANDBOX_BACKEND", CS.HOST)
    monkeypatch.delenv("AIQ_ALLOW_UNISOLATED_CODE", raising=False)
    tc = CS.Toolchain(str(tmp_path / "root"))
    for lang in ("python", "sql", "bash", "typescript"):
        ok, why = tc.available(lang)
        assert not ok and "NOT run" in why, (lang, ok, why)
    rec = CS.run_code({"lang": "python", "files": {"check.py": IMPORT_ONLY}},
                      "```python\nopen('/tmp/aiq-should-never-exist', 'w').write('x')\n```",
                      str(tmp_path / "work"), tc)
    assert rec["unavailable"] and rec["steps"] == []
    assert not os.path.exists(os.path.join(str(tmp_path / "work"), "solution.py")), "it wrote the source anyway"
    assert not os.path.exists("/tmp/aiq-should-never-exist")


@NOT_ROOT
def test_running_on_the_host_has_to_be_asked_for_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("AIQ_SANDBOX_BACKEND", CS.HOST)
    monkeypatch.setenv("AIQ_ALLOW_UNISOLATED_CODE", "1")
    tc = CS.Toolchain(str(tmp_path / "root"))
    assert tc.available("python") == (True, "")
    rec = CS.run_code({"lang": "python", "files": {"check.py": IMPORT_ONLY}},
                      "```python\nvalue = 1\n```", str(tmp_path / "work"), tc)
    assert rec["ok"], rec["steps"]
    assert rec["toolchain"]["backend"] == CS.HOST and rec["toolchain"]["isolated_docker"] is False
    assert "docker socket reachable" in rec["warning"]


def test_the_host_fallback_still_takes_the_network_away():
    """`unshare -rn` is refused on both Sparks; `unshare -Un` is not, and an
    unmapped user namespace keeps the real uid for filesystem checks."""
    tc = CS.Toolchain(tempfile.mkdtemp(prefix="aiq-net-"))
    if not tc.net_prefix:
        pytest.skip("this host allows no network namespace at all")
    probe = "import socket\ntry:\n socket.create_connection(('10.100.184.1', 8000), 3); print('REACHED')\nexcept OSError as e:\n print('blocked', e.errno)"
    p = subprocess.run([*tc.net_prefix, sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert "REACHED" not in p.stdout, p.stdout
    assert "blocked" in p.stdout, (p.stdout, p.stderr)


# ---------------------------------------------------------------- the gate --

def _summary(overall, categories, dimensions, **headline):
    head = {"overall_score": overall, "no_charts_to_draw": 0, "table_not_available": 0, "fast_turns_thinking": 0,
            "false_chart_claims": 0}
    head.update(headline)
    return {"headline": head,
            "by_category": {k: {"cases": 1, "mean_score": v, "all_pass": 1} for k, v in categories.items()},
            "by_dimension": {k: {"passed": 1, "total": 1, "rate": v} for k, v in dimensions.items()},
            "cases": [{"id": "P01", "category": "csv_plot", "score": 1.0, "all_pass": True, "failed": []}]}


# Every category the case list defines and every dimension a run produces: a
# gate row is derived for each, so a summary that simply omits one is a run
# that did not measure it, and "meets every threshold" has to mean all of them.
GOOD_CATEGORIES = {"csv_plot_without_p06": 0.85, "csv_plot": 0.82, "followup_edit": 0.88, "big_report": 0.84,
                   "format_rewrite": 0.93, "howto": 1.0, "tables": 1.0, "fast_factual": 0.95, "coding": 0.9}
GOOD_DIMENSIONS = {"charts": 0.9, "structure": 0.95, "length": 0.85, "thinking": 1.0, "code": 0.9,
                   "fidelity": 0.92, "deliverable": 0.97}


def test_the_gate_passes_a_run_that_meets_every_threshold():
    report = G.gate_report(_summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS))
    assert report["passed"], [r for r in report["rows"] if not r["ok"]]


def test_the_gate_fails_on_one_fast_turn_that_thought():
    report = G.gate_report(_summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS, fast_turns_thinking=1))
    assert not report["passed"]
    assert [c["count"] for c in report["counts"] if not c["ok"]] == ["fast_turns_thinking"]


def test_the_gate_fails_on_the_sentence_the_owner_saw():
    report = G.gate_report(_summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS, no_charts_to_draw=2))
    assert not report["passed"]


def test_the_gate_fails_a_category_below_its_worker_baseline_even_when_it_clears_the_floor():
    baseline = _summary(0.9, {**GOOD_CATEGORIES, "big_report": 0.95}, GOOD_DIMENSIONS)
    report = G.gate_report(_summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS), baseline)
    row = next(r for r in report["rows"] if r["metric"] == "category:big_report")
    assert row["value"] == 0.84 and row["baseline"] == 0.95 and not row["ok"]
    assert not report["passed"]


def test_the_coding_rows_are_reported_but_do_not_block():
    report = G.gate_report(_summary(0.9, {**GOOD_CATEGORIES, "coding": 0.1}, {**GOOD_DIMENSIONS, "code": 0.1}))
    assert report["passed"]
    assert [r["metric"] for r in report["rows"] if not r["ok"]] == ["category:coding", "dimension:code"]


# ------------------------------------------- the gate cannot be blind (Q-2) --

def test_a_run_where_only_fast_factual_regresses_fails_the_gate():
    """Measured on the integrated tree before this fix: gate_report returned
    passed=True with fast_factual 0.95 -> 0.40, csv_plot 0.86 -> 0.10 and
    fidelity 0.90 -> 0.10, and no row named any of them. Fast no longer thinks,
    so fast_factual is the category that measures this round's own risk."""
    base = _summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS)
    report = G.gate_report(_summary(0.9, {**GOOD_CATEGORIES, "fast_factual": 0.40}, GOOD_DIMENSIONS), base)
    row = next(r for r in report["rows"] if r["metric"] == "category:fast_factual")
    assert row["value"] == 0.40 and row["baseline"] == 0.95 and row["blocking"] and not row["ok"]
    assert not report["passed"]


@pytest.mark.parametrize("metric,key,field", [("category:csv_plot", "by_category", "mean_score"),
                                              ("dimension:fidelity", "by_dimension", "rate"),
                                              ("dimension:deliverable", "by_dimension", "rate")])
def test_every_category_and_dimension_the_run_produces_is_read(metric, key, field):
    base = _summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS)
    bad = _summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS)
    bad[key][metric.split(":", 1)[1]][field] = 0.10
    report = G.gate_report(bad, base)
    row = next(r for r in report["rows"] if r["metric"] == metric)
    assert row["value"] == 0.10 and row["blocking"] and not row["ok"]
    assert not report["passed"]


def test_a_category_added_to_the_case_list_is_gated_without_editing_the_gate(monkeypatch):
    """The point of deriving the rows: a new category cannot be ungated."""
    monkeypatch.setattr(G, "CASES", list(G.CASES) + [{"id": "ZZ1", "category": "brand_new", "turns": []}])
    metrics = [m for m, _, _ in G.gate_rows(_summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS))]
    assert "category:brand_new" in metrics
    report = G.gate_report(_summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS))
    row = next(r for r in report["rows"] if r["metric"] == "category:brand_new")
    assert row["value"] is None and row["blocking"] and not row["ok"], "a category with no result must not pass"
    assert not report["passed"]


def test_the_gate_reads_every_category_the_suite_has():
    metrics = {m for m, _, _ in G.gate_rows(_summary(0.9, GOOD_CATEGORIES, GOOD_DIMENSIONS))}
    assert {f"category:{c['category']}" for c in C.CASES} <= metrics


def test_a_baseline_run_scored_by_todays_checks_passes_no_gate(baseline_summary):
    """The gate is not a formality: the image the owner complained about fails it."""
    report = G.gate_report(baseline_summary)
    assert not report["passed"]
    failed = {r["metric"] for r in report["rows"] if not r["ok"] and r["blocking"]}
    assert {"overall", "category:csv_plot_without_p06", "dimension:charts"} <= failed


# --------------------------------------------------------------- the stack --

STACK = AIQ / "aiq-stack.sh"


def _stack(tmp_path, *args, **env):
    """Run aiq-stack.sh with a PATH whose `docker` records that it was called."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    marker = tmp_path / "docker-was-called"
    (bindir / "docker").write_text(f"#!/bin/sh\necho \"$@\" >> {marker}\nexit 1\n")
    (bindir / "docker").chmod(0o755)
    environ = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "AIQ_RUNTIME": str(tmp_path / "runtime")}
    environ.update(env)
    proc = subprocess.run(["bash", str(STACK), *args], capture_output=True, text=True, timeout=120, env=environ)
    return proc, marker


def test_the_stack_refuses_to_start_on_the_head(tmp_path):
    proc, marker = _stack(tmp_path, "up", AIQ_HEAD_HOST=os.uname().nodename)
    assert proc.returncode != 0
    assert "must not take its memory" in proc.stderr, proc.stderr
    assert not marker.exists(), "it reached docker before refusing"


def test_the_stack_refuses_production_ports_and_names(tmp_path):
    here = os.uname().nodename
    proc, marker = _stack(tmp_path, "up", AIQ_WORKER_HOST=here, AIQ_HEAD_HOST="not-this-host", AIQ_ORCH_PORT="8080")
    assert proc.returncode != 0 and "8080" in proc.stderr
    assert not marker.exists()

    proc, marker = _stack(tmp_path, "up", AIQ_WORKER_HOST=here, AIQ_HEAD_HOST="not-this-host", AIQ_ORCH_PORT="8081")
    assert proc.returncode != 0 and "8081" in proc.stderr

    proc, _ = _stack(tmp_path, "up", AIQ_PROJECT="sf-local-ai-e2e-aiq", AIQ_WORKER_HOST=here,
                     AIQ_HEAD_HOST="not-this-host")
    assert proc.returncode != 0 and "looks like production" in proc.stderr

    proc, _ = _stack(tmp_path, "up", AIQ_PROJECT="aiq-not-isolated", AIQ_WORKER_HOST=here)
    assert proc.returncode != 0 and "e2e" in proc.stderr


def test_the_stack_keeps_its_secrets_out_of_the_repository(tmp_path):
    here = os.uname().nodename
    proc, marker = _stack(tmp_path, "up", AIQ_WORKER_HOST=here, AIQ_HEAD_HOST="not-this-host",
                          AIQ_RUNTIME=str(AIQ / ".runtime"))
    assert proc.returncode != 0 and "OUTSIDE the repository" in proc.stderr
    assert not marker.exists()


def test_the_stack_prints_the_tunnel_instead_of_opening_one(tmp_path):
    proc, marker = _stack(tmp_path, "tunnel")
    assert proc.returncode == 0
    assert "-R 8002:127.0.0.1:8002" in proc.stdout and "-R 8005:127.0.0.1:8005" in proc.stdout
    assert not marker.exists()
