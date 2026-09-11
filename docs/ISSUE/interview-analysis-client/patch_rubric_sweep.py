"""Patch scripts/rubric_sweep.py in interview-analysis-v2:

1. Per-job logs were 0 bytes (all 172 of the 1-2 September sweep) because
   `contextlib.redirect_stdout/stderr` only captures writes to sys.stdout /
   sys.stderr, and every `logging` handler holds the ORIGINAL stderr object it
   was created with. A logging handler bound to the buffer for the duration of
   the job restores the diagnostic trail.
2. A job stranded at state `preprocessed` (its analysis phase died inside an
   engine outage) made `run_cs_refresh` exit with "cannot refresh candidate
   analysis from state 'preprocessed'". The sweep now runs Phase 3
   (`run_analysis`, idempotent) for such a job and refreshes again, instead of
   recording the refusal.

Run from the repository root: .venv/bin/python patch_rubric_sweep.py
"""
import pathlib
import sys

p = pathlib.Path("scripts/rubric_sweep.py")
s = p.read_text()
if "_capture_logging" in s:
    print("already patched")
    sys.exit(0)

old = '''def run_one(out_dir: str, log_dir: str, tolerance: float, with_tester: bool) -> dict:
    """Worker: refresh (+ legacy prep + SF) then the tester. Never raises."""
    from interview_analysis.candidate_rubric.tester import run_tester
    from interview_analysis.scheduling.pipeline import run_cs_refresh

    out = Path(out_dir)
    log = Path(log_dir) / f"{out.name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    row: dict = {"job": out.name, "out_dir": str(out), "started": time.time()}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            t0 = time.time()
            res = run_cs_refresh(
                out, check_endpoint=False, own_lease=False,
                settings_overrides=SF_OVERRIDES, prepare_legacy=True,
            )
'''
new = '''@contextlib.contextmanager
def _capture_logging(buf: io.StringIO):
    """Route `logging` into the job's buffer for the duration of the job.

    redirect_stdout/stderr do not reach logging handlers, which keep the
    original stderr they were created with — which is why every per-job log
    of the 1-2 September sweep was 0 bytes. Attached to the root logger so
    every `interview_analysis.*` logger is captured; removed on exit."""
    import logging

    handler = logging.StreamHandler(buf)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    try:
        yield
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


_STRANDED = "from state 'preprocessed'"


def _refresh(out: Path):
    """run_cs_refresh, recovering a job stranded at `preprocessed` first.

    A job whose analysis phase died inside an engine outage sits at
    `preprocessed` with its (expensive) preprocessing intact. Phase 3 is
    idempotent over that directory, so run it and refresh again rather than
    recording the refusal as the job's result."""
    from interview_analysis.scheduling.pipeline import run_analysis, run_cs_refresh

    res = run_cs_refresh(
        out, check_endpoint=False, own_lease=False,
        settings_overrides=SF_OVERRIDES, prepare_legacy=True,
    )
    if res.ok or _STRANDED not in (res.error or ""):
        return res, False
    print(f"{out.name}: stranded at 'preprocessed'; running the analysis phase first", flush=True)
    ana = run_analysis(out, check_endpoint=False, own_lease=False, settings_overrides=SF_OVERRIDES)
    if not ana.ok:
        res.error = f"analysis phase failed while recovering a stranded job: {ana.error}"
        return res, True
    return run_cs_refresh(
        out, check_endpoint=False, own_lease=False,
        settings_overrides=SF_OVERRIDES, prepare_legacy=True,
    ), True


def run_one(out_dir: str, log_dir: str, tolerance: float, with_tester: bool) -> dict:
    """Worker: refresh (+ legacy prep + SF) then the tester. Never raises."""
    from interview_analysis.candidate_rubric.tester import run_tester

    out = Path(out_dir)
    log = Path(log_dir) / f"{out.name}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    row: dict = {"job": out.name, "out_dir": str(out), "started": time.time()}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf), _capture_logging(buf):
            t0 = time.time()
            res, recovered = _refresh(out)
            row["recovered_from_preprocessed"] = recovered
'''
assert old in s, "run_one head not found (already changed upstream?)"
s = s.replace(old, new)
p.write_text(s)
print("rubric_sweep.py patched")
