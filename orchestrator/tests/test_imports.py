"""The whole app must import OFFLINE with no torch/transformers/weasyprint/
lancedb installed — heavy deps are lazy (spec §8 test requirements)."""
import sys


def test_app_imports_without_heavy_deps():
    import app.config  # noqa: F401
    import app.core.chart_spec  # noqa: F401
    import app.core.charts_png  # noqa: F401
    import app.core.citations  # noqa: F401
    import app.core.exports  # noqa: F401
    import app.core.report_paths  # noqa: F401
    import app.core.schema_cache  # noqa: F401
    import app.core.sql_guard  # noqa: F401
    import app.engines.rag  # noqa: F401
    import app.engines.report  # noqa: F401
    import app.engines.router  # noqa: F401
    import app.engines.sql  # noqa: F401
    import app.engines.vision  # noqa: F401
    import app.graph  # noqa: F401
    import app.llm  # noqa: F401
    import app.main  # noqa: F401
    import app.memory  # noqa: F401
    import app.sse  # noqa: F401

    for banned in ("torch", "transformers", "weasyprint", "lancedb"):
        assert banned not in sys.modules, f"{banned} must be imported lazily"
    # matplotlib renders only inside render_chart_png (Agg), never at import.
    assert "matplotlib.pyplot" not in sys.modules


def test_graph_compiles():
    from app.graph import get_graph

    graph = get_graph()
    assert graph is not None
