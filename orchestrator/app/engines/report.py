"""Report engine (spec §8).

Plan sections with gpt-oss-120b (planning MAY use REPORT_MAX_CONTEXT=65536;
every other call stays under DEFAULT_MAX_CONTEXT=32768) → fill sections via
the sql/rag engines → render validated chart specs to PNG with matplotlib →
assemble Markdown → pandoc to BOTH .docx and .pdf (PDF via weasyprint, no
LaTeX) in REPORTS_DIR → meta.report_files.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
import time
from pathlib import Path
from typing import Awaitable, Callable, List, Sequence

from .. import llm
from ..config import settings
from ..core.chart_pipeline import build_chart
from ..core.charts_png import PNG_SUPPORTED, render_chart_png
from ..core.citations import build_citations
from ..core.exports import slugify
from .rag import _answer_messages as rag_answer_messages
from .rag import select_context
from .sql import _ask_chart_model, generate_and_run_sql

Emit = Callable[[str, dict], Awaitable[None]]

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S | re.I)

MAX_SECTIONS = 6

_PLAN_SYSTEM = (
    "You plan business reports over Salesforce data. Given a request, design "
    f"up to {MAX_SECTIONS} sections. Respond with ONLY JSON:\n"
    '{"title": "<report title>", "sections": [{"title": "<section title>", '
    '"kind": "sql" or "rag", "instruction": "<what to compute or find>", '
    '"chart": true or false}]}\n'
    'Use "sql" for numeric/tabular analysis (set "chart": true when a chart '
    'helps) and "rag" for narrative sections drawn from record contents.'
)


def _parse_plan(raw: str, fallback_title: str) -> dict:
    text = _THINK_RE.sub("", raw or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    obj: dict = {}
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                obj = parsed
        except (json.JSONDecodeError, ValueError):
            obj = {}

    title = str(obj.get("title") or fallback_title or "Analysis Report").strip()
    sections: List[dict] = []
    for sec in obj.get("sections", []) or []:
        if not isinstance(sec, dict):
            continue
        kind = sec.get("kind") if sec.get("kind") in ("sql", "rag") else "rag"
        instruction = str(sec.get("instruction") or sec.get("title") or "").strip()
        if not instruction:
            continue
        sections.append(
            {
                "title": str(sec.get("title") or instruction[:60]),
                "kind": kind,
                "instruction": instruction,
                "chart": bool(sec.get("chart")),
            }
        )
    if not sections:
        sections = [
            {"title": "Overview", "kind": "rag", "instruction": fallback_title, "chart": False}
        ]
    return {"title": title, "sections": sections[:MAX_SECTIONS]}


def _markdown_table(columns: Sequence[str], rows: Sequence[Sequence], max_rows: int = 20) -> str:
    if not columns:
        return "_No data._"
    header = "| " + " | ".join(str(c) for c in columns) + " |"
    divider = "| " + " | ".join("---" for _ in columns) + " |"
    body = [
        "| " + " | ".join("" if v is None else str(v) for v in row) + " |"
        for row in rows[:max_rows]
    ]
    note = f"\n\n_Showing first {max_rows} of {len(rows)} rows._" if len(rows) > max_rows else ""
    return "\n".join([header, divider, *body]) + note


async def _run_pandoc(md_path: Path, out_path: Path, resource_dir: Path) -> None:
    cmd = [
        "pandoc",
        str(md_path),
        "--standalone",
        "--resource-path",
        str(resource_dir),
        "-o",
        str(out_path),
    ]
    if out_path.suffix.lower() == ".pdf":
        cmd.append("--pdf-engine=weasyprint")  # PDF without LaTeX
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc failed for {out_path.name}: "
            f"{stderr.decode(errors='replace')[:500]}"
        )


async def _sql_section(sec: dict, index: int, tmp_dir: Path) -> List[str]:
    parts: List[str] = []
    sql, columns, rows = await generate_and_run_sql(
        sec["instruction"], fetch_cap=settings.sql_preview_row_cap + 1
    )
    sample = json.dumps(
        {"columns": columns, "rows": [list(r) for r in rows[:30]]}, default=str
    )
    prose = await llm.chat_completion(
        [
            {
                "role": "system",
                "content": (
                    "Write 2-4 sentences of report prose interpreting this query "
                    "result. Use only numbers present in the data."
                ),
            },
            {"role": "user", "content": f"Section: {sec['title']}\nResult: {sample}"},
        ],
        temperature=0.2,
        max_tokens=3000,
    )
    parts += [prose.strip(), "", _markdown_table(columns, rows), ""]

    if sec.get("chart") and columns and rows:
        parts += await _section_chart(sec, index, tmp_dir, columns, rows)
    return parts


async def _section_chart(
    sec: dict, index: int, tmp_dir: Path, columns: Sequence[str], rows: Sequence[Sequence]
) -> List[str]:
    """Render this section's chart, or return [] and keep the section.

    The prose and the table are already in `parts` by the time this runs and
    are returned by the caller regardless — a chart is an addition to a
    report section, never a precondition for it. Previously an exception in
    here propagated out of `_sql_section` and took the section's prose,
    table and heading with it, so one unlucky matplotlib call silently cost
    the reader a whole chapter of their report.
    """
    try:
        result = await build_chart(
            sec["instruction"],
            columns,
            rows[:50],
            mode=settings.chart_trigger_mode,
            ask_model=_ask_chart_model,
            title=sec.get("title", ""),
            # The planner already decided this section wants a chart; the
            # section instruction is not the user's wording, so the
            # wording check would refuse every report chart.
            force=True,
        )
        if result is None:
            return []
        if result.spec.type not in PNG_SUPPORTED:
            # An explicit table-only policy (today: funnel). The section
            # keeps the ordered table above; nothing blank is embedded.
            log.info(
                "report chart type %s has no matplotlib rendering; table only",
                result.spec.type,
            )
            return []
        png_path = tmp_dir / f"chart-{index}.png"
        render_chart_png(result.spec, result.columns, result.rows, png_path)
        if not png_path.exists() or png_path.stat().st_size == 0:
            return []
        return [f"![{result.spec.title or sec['title']}]({png_path.name})", ""]
    except Exception:
        log.warning("report chart failed for section %s; table only", index, exc_info=True)
        return []


async def _rag_section(sec: dict) -> List[str]:
    hits = await select_context(sec["instruction"])
    prose = await llm.chat_completion(
        rag_answer_messages(sec["instruction"], hits, []), temperature=0.2, max_tokens=5000
    )
    parts = [prose.strip(), ""]
    citations = build_citations(hits, base_url=settings.sf_lightning_base_url)
    if citations:
        parts.append(
            "Sources: " + ", ".join(f"[{c['record_id']}]({c['url']})" for c in citations)
        )
        parts.append("")
    return parts


async def run_report_engine(message: str, history: Sequence[dict], emit: Emit) -> str:
    # Planning is the ONE call allowed the large 65536 context (§8). The
    # OpenAI-compatible vLLM server manages context server-side
    # (--max-model-len); the cap is communicated via generation length here.
    plan_raw = await llm.chat_completion(
        [
            {"role": "system", "content": _PLAN_SYSTEM},
            {"role": "user", "content": message},
        ],
        temperature=0.2,
        max_tokens=5000,
    )
    plan = _parse_plan(plan_raw, fallback_title=message[:80])

    stamp = time.strftime("%Y%m%d-%H%M%S")
    base_name = f"{slugify(plan['title'], fallback='report')}-{stamp}"
    reports_dir = Path(settings.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    section_errors: List[str] = []
    with tempfile.TemporaryDirectory(prefix="report-") as tmp:
        tmp_dir = Path(tmp)
        md_lines: List[str] = [
            f"# {plan['title']}",
            "",
            f"_Generated {time.strftime('%Y-%m-%d %H:%M')} by the TechSara Local AI Analysis Platform._",
            "",
        ]
        for index, sec in enumerate(plan["sections"], 1):
            md_lines += [f"## {index}. {sec['title']}", ""]
            try:
                if sec["kind"] == "sql":
                    md_lines += await _sql_section(sec, index, tmp_dir)
                else:
                    md_lines += await _rag_section(sec)
            except Exception as exc:  # keep the report going; note the failure
                section_errors.append(f"{sec['title']}: {exc}")
                md_lines += [f"> Section could not be generated: {exc}", ""]

        md_path = tmp_dir / f"{base_name}.md"
        md_path.write_text("\n".join(md_lines), encoding="utf-8")

        outputs = [reports_dir / f"{base_name}.docx", reports_dir / f"{base_name}.pdf"]
        for out_path in outputs:  # BOTH .docx and .pdf (§8)
            await _run_pandoc(md_path, out_path, tmp_dir)

    # §10: report_files = [{filename, type, size}] — the exact shape FileCards
    # renders; the download URL is derived from filename (/api/reports/<name>).
    report_files = [
        {
            "filename": p.name,
            "type": p.suffix.lstrip(".").lower(),
            "size": p.stat().st_size,
        }
        for p in outputs
        if p.is_file()
    ]
    summary = (
        f"Generated report \"{plan['title']}\" with {len(plan['sections'])} "
        f"section(s): {', '.join(f['filename'] for f in report_files)}."
    )
    if section_errors:
        summary += (
            f" {len(section_errors)} section(s) could not be generated: "
            + "; ".join(section_errors)
        )
    await emit("token", {"text": summary})
    # §10: single final meta before done, carrying only contract keys.
    await emit("meta", {"route": "report", "report_files": report_files})
    return summary
