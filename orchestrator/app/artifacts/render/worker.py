"""The render subprocess: `python -m app.artifacts.render.worker <job.json>`.

WHY A SUBPROCESS. matplotlib, WeasyPrint, python-docx/pptx and openpyxl are
CPU-bound and synchronous; run on the orchestrator's event loop they recur
the documented TTFT collapse (memory: "Fast mode CPU-bound pre-pass", 0.7 →
11.7 s). A thread would keep the loop free but shares the address space, so
a page-count bomb would take the API down with it. The job runner therefore
starts THIS module with an argv array (never a shell string), cwd = the
version's working directory, an environment limited to PATH/HOME/LANG/
PYTHONPATH/MPLCONFIGDIR/FONTCONFIG_FILE, `preexec_fn` setting RLIMIT_AS to
artifact_render_memory_mb and RLIMIT_CPU to artifact_render_timeout_s, and
`asyncio.wait_for` killing the process group on the wall clock.

PROTOCOL. job.json: {spec, formats, out_dir, title_slug, version, effort,
transform?} — `transform` is the caller's report of what code did to a
pasted table (CONTRACT-2 §6), optional, echoed in the report and read by the
tabular document's methodology note.
On success `<out_dir>/render-report.json` holds RenderReport.to_json() and
the exit code is 0. On a RenderError the same file holds
{error: {category, message}} and the exit code is 1; the traceback, when
there is one, goes to stderr for the job's diagnostic log and never into the
file. Any other exception is reported as renderer_failure with a fixed
sentence — a person never sees a path or a stack trace.

MEASURED (2026-09-11, aarch64 host, under RLIMIT_AS = 2048 MB and RLIMIT_CPU
= 180 s exactly as the job runner sets them): a 10-section executive report
to PDF + DOCX exits 0 in 1.6 s at 144 MB peak RSS; a 12-slide CEO deck to
PPTX + PDF in 1.4 s at 141 MB. The 2 GB address-space limit therefore has an
order of magnitude of headroom for WeasyPrint's page tree on a 60-page
document and is not the constraint an ordinary render meets.

MATPLOTLIB. `MPLCONFIGDIR` is pointed at a private temporary directory when
the parent did not set one: matplotlib otherwise writes its font cache under
$HOME, which the scrubbed environment may not make writable, and a font-cache
build inside a memory-limited process is the last thing a render needs. The
job runner always sets it (pipeline.py); the directory made here is for
tests and manual runs, and is removed when the process is done with it.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Dict, Optional

REPORT_NAME = "render-report.json"


def _prepare_environment() -> Optional[str]:
    """Returns the matplotlib config directory this process created, so
    main() can remove it — or None when the parent supplied one."""
    os.environ.setdefault("MPLBACKEND", "Agg")
    if os.environ.get("MPLCONFIGDIR"):
        return None
    made = tempfile.mkdtemp(prefix="mpl-")
    os.environ["MPLCONFIGDIR"] = made
    return made


def run_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one job dict; return the report dict (success) or the error
    dict (failure). Importable so tests can round-trip without a process."""
    from . import RenderError, render_version
    from .. import spec as S

    try:
        spec = S.load(job["spec"])
        formats = [str(f) for f in job.get("formats") or []]
        out_dir = str(job["out_dir"])
        title_slug = str(job.get("title_slug") or "document")
        version = int(job.get("version") or 1)
        effort = str(job.get("effort") or "fast")
        transform = job.get("transform") if isinstance(job.get("transform"), dict) else None
    except Exception as exc:  # a malformed job is the caller's bug, but still a sentence
        return {"error": {"category": "invalid_request", "message": "The render job could not be read."}, "detail": type(exc).__name__}
    try:
        report = render_version(spec, formats, out_dir, title_slug=title_slug, version=version, effort=effort, transform=transform)
    except RenderError as exc:
        return {"error": {"category": exc.category, "message": exc.message}}
    except MemoryError:
        return {"error": {"category": "renderer_failure", "message": "The render ran out of memory."}}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return {"error": {"category": "renderer_failure", "message": "The files could not be built."}}
    return report.to_json()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m app.artifacts.render.worker <job.json>", file=sys.stderr)
        return 2
    made = _prepare_environment()
    try:
        return _run(Path(argv[0]))
    finally:
        if made:
            shutil.rmtree(made, ignore_errors=True)


def _run(job_path: Path) -> int:
    try:
        job = json.loads(job_path.read_text(encoding="utf-8"))
    except Exception:
        print("the job file could not be read", file=sys.stderr)
        return 2
    out_dir = Path(str(job.get("out_dir") or job_path.parent))
    result = run_job(job)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / REPORT_NAME).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        print("the render report could not be written", file=sys.stderr)
        return 1
    if "error" in result:
        print(f"render failed: {result['error']['category']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through a subprocess in the tests
    sys.exit(main())
