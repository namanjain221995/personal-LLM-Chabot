"""The whole app must import OFFLINE with no torch/transformers/weasyprint/
lancedb installed — heavy deps are lazy (spec §8 test requirements)."""
import subprocess
import sys
import textwrap
from pathlib import Path


def test_app_imports_without_heavy_deps():
    # This contract must run in a fresh interpreter. Other tests legitimately
    # exercise LanceDB before this file is collected, and sys.modules is
    # process-global; checking the shared pytest interpreter makes the result
    # order-dependent instead of proving that application imports are lazy.
    script = textwrap.dedent(
        """
        import sys
        import app.config
        import app.core.chart_spec
        import app.core.charts_png
        import app.core.citations
        import app.core.exports
        import app.core.report_paths
        import app.core.schema_cache
        import app.core.sql_guard
        import app.engines.rag
        import app.engines.report
        import app.engines.router
        import app.engines.sql
        import app.engines.vision
        import app.graph
        import app.llm
        import app.main
        import app.memory
        import app.sse

        banned = ("torch", "transformers", "weasyprint", "lancedb", "matplotlib.pyplot")
        loaded = [name for name in banned if name in sys.modules]
        if loaded:
            raise SystemExit("heavy modules imported eagerly: " + ", ".join(loaded))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_graph_compiles():
    from app.graph import get_graph

    graph = get_graph()
    assert graph is not None
