"""Report chart isolation.

THE BUG: `_sql_section` called `render_chart_png` inline, so any exception
in matplotlib propagated out and took the section's prose, table and
heading with it. One unlucky chart cost the reader a whole chapter of a
report they will send to someone.

A chart is an addition to a section, never a precondition for it.
"""
import asyncio

import pytest

from app.engines import report as report_engine

COLUMNS = ["industry", "total"]
ROWS = [["Retail", 10], ["Technology", 7], ["Healthcare", 3]]

SECTION = {
    "title": "Pipeline",
    "kind": "sql",
    "instruction": "total by industry",
    "chart": True,
}


@pytest.fixture()
def offline(monkeypatch):
    async def fake_sql(instruction, **kwargs):
        return "SELECT industry, total FROM accounts", COLUMNS, ROWS

    async def fake_chat(messages, **kwargs):
        system = messages[0].get("content", "")
        if "chart" in system.lower():
            return '{"type":"bar","x_key":"industry","y_keys":["total"],"title":"By industry"}'
        return "Prose about the pipeline."

    monkeypatch.setattr(report_engine, "generate_and_run_sql", fake_sql)
    monkeypatch.setattr(report_engine.llm, "chat_completion", fake_chat)


def run_section(tmp_path, section=None):
    return asyncio.run(
        report_engine._sql_section(section or dict(SECTION), 1, tmp_path)
    )


def joined(parts):
    return "\n".join(parts)


def test_a_healthy_section_has_prose_table_and_a_chart(offline, tmp_path):
    parts = run_section(tmp_path)
    text = joined(parts)
    assert "Prose about the pipeline." in text
    assert "Retail" in text  # the markdown table
    assert "![" in text  # the chart image
    assert (tmp_path / "chart-1.png").exists()


def test_a_matplotlib_exception_leaves_the_prose_and_table_intact(
    offline, tmp_path, monkeypatch
):
    def boom(*args, **kwargs):
        raise RuntimeError("matplotlib fell over")

    monkeypatch.setattr(report_engine, "render_chart_png", boom)
    parts = run_section(tmp_path)
    text = joined(parts)
    assert "Prose about the pipeline." in text
    assert "Retail" in text
    assert "![" not in text  # no image, and no exception either


def test_a_failing_chart_model_leaves_the_section_intact(offline, tmp_path, monkeypatch):
    async def fake_chat(messages, **kwargs):
        if "chart" in messages[0].get("content", "").lower():
            raise RuntimeError("vLLM refused")
        return "Prose about the pipeline."

    monkeypatch.setattr(report_engine.llm, "chat_completion", fake_chat)
    text = joined(run_section(tmp_path))
    assert "Prose about the pipeline." in text
    assert "Retail" in text


def test_an_unsupported_chart_type_yields_a_table_not_a_blank_image(
    offline, tmp_path, monkeypatch
):
    """A funnel has no truthful matplotlib rendering. The section keeps the
    ordered table; nothing blank is embedded."""
    from app.core.chart_pipeline import ChartResult
    from app.core.chart_spec import ChartSpec

    async def fake_build(*args, **kwargs):
        return ChartResult(
            ChartSpec(type="funnel", x_key="industry", y_keys=["total"], title="Funnel"),
            COLUMNS,
            [list(r) for r in ROWS],
        )

    monkeypatch.setattr(report_engine, "build_chart", fake_build)
    parts = run_section(tmp_path)
    text = joined(parts)
    assert "Retail" in text
    assert "![" not in text
    assert not list(tmp_path.glob("*.png"))


def test_an_empty_result_produces_no_chart_and_no_crash(offline, tmp_path, monkeypatch):
    async def fake_sql(instruction, **kwargs):
        return "SELECT 1", COLUMNS, []

    monkeypatch.setattr(report_engine, "generate_and_run_sql", fake_sql)
    text = joined(run_section(tmp_path))
    assert "Prose about the pipeline." in text
    assert "![" not in text


def test_a_section_without_a_chart_flag_never_renders_one(offline, tmp_path):
    section = dict(SECTION)
    section["chart"] = False
    text = joined(run_section(tmp_path, section))
    assert "Prose about the pipeline." in text
    assert "![" not in text


def test_a_zero_byte_png_is_not_embedded(offline, tmp_path, monkeypatch):
    """A renderer that "succeeds" but writes nothing is the blank-image bug
    wearing a different hat."""

    def write_nothing(spec, columns, rows, out_path):
        from pathlib import Path

        Path(out_path).write_bytes(b"")
        return Path(out_path)

    monkeypatch.setattr(report_engine, "render_chart_png", write_nothing)
    text = joined(run_section(tmp_path))
    assert "Retail" in text
    assert "![" not in text


def test_report_charts_do_not_need_the_user_to_say_the_word_chart(offline, tmp_path):
    """The section instruction is written by the planner. The planner's own
    `chart: true` is the intent signal, and the wording check is bypassed
    for it — otherwise reports would never chart at all."""
    section = dict(SECTION)
    section["instruction"] = "Aggregate account totals grouped by industry"
    text = joined(run_section(tmp_path, section))
    assert "![" in text
