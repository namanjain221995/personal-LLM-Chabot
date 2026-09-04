"""The PDF exporter must not fetch anything.

WeasyPrint resolves every reference in the HTML pandoc hands it, using its own
HTTP stack — not `core.net.safe_fetch`, which guards every other outbound
request in this application. PROVEN on 2026-09-04 inside the running
container: a three-line report produced three GET requests to a loopback
listener.

    ![probe](http://127.0.0.1:PORT/INTERNAL-SSRF)   -> GET /INTERNAL-SSRF
    ![probe2](http://127.0.0.1:PORT/second)         -> GET /second
    <img src="http://127.0.0.1:PORT/rawhtml">       -> GET /rawhtml

`file:///etc/passwd` and `http://169.254.169.254/latest/meta-data/` are the
same code path. It matters because report markdown is not ours alone: a Deep
Research report is written from web pages, and a page that gets a marker into
the text gets a fetch out of the exporter.
"""
from __future__ import annotations

import pytest

from app.core.report_render import sanitise_for_render


# ---------------------------------------------------------------------------
# Everything the renderer would load
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "http://127.0.0.1:8080/admin",
        "http://169.254.169.254/latest/meta-data/",   # cloud metadata
        "https://evil.test/beacon.png",
        "file:///etc/passwd",
        "//evil.test/protocol-relative.png",
        "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
        "/etc/hostname",
        "../../../../etc/passwd",
        "sub/dir/chart.png",                          # any path, not just ours
    ],
)
def test_a_markdown_image_that_is_not_a_local_file_is_removed(target):
    out = sanitise_for_render(f"Text\n\n![alt]({target})\n")
    assert target not in out
    assert "image omitted" in out


@pytest.mark.parametrize(
    "tag",
    [
        '<img src="http://127.0.0.1:9/x">',
        "<IMG SRC='http://169.254.169.254/'>",
        '< img  src="http://evil.test/x" >',
        '<link rel="stylesheet" href="http://evil.test/a.css">',
        '<style>@import url("http://evil.test/a.css");</style>',
        '<script src="http://evil.test/a.js"></script>',
        '<iframe src="http://127.0.0.1:8080/"></iframe>',
        '<object data="http://evil.test/x"></object>',
        '<embed src="http://evil.test/x">',
        '<source srcset="http://evil.test/x">',
    ],
)
def test_raw_html_that_pulls_a_resource_is_removed(tag):
    out = sanitise_for_render(f"Text\n\n{tag}\n")
    assert "evil.test" not in out
    assert "127.0.0.1" not in out
    assert "169.254" not in out


def test_the_tag_boundary_is_a_real_boundary():
    """This is the specific bug that made the guard inert.

    Written as `\\b` through a shell heredoc, the boundary became a literal
    backspace (0x08) in the compiled pattern — so the expression required a
    control character after the tag name and matched NOTHING. The markdown
    tests above all passed while raw HTML sailed through.
    """
    from app.core.report_render import _HTML_FETCHERS

    assert "\x08" not in _HTML_FETCHERS.pattern
    assert _HTML_FETCHERS.search('<img src="http://x/y">')
    # A word merely starting with a tag name is not a tag.
    assert not _HTML_FETCHERS.search("<images-are-nice>")
    assert not _HTML_FETCHERS.search("<linkage>")


# ---------------------------------------------------------------------------
# ...and everything legitimate survives
# ---------------------------------------------------------------------------


def test_a_chart_this_app_just_wrote_still_renders():
    """`report.py` emits `![title](chart-abc.png)` — a bare filename resolved
    through pandoc's --resource-path. That is the one legitimate case."""
    md = "![Revenue by region](chart-8f3a21.png)"
    assert sanitise_for_render(md) == md


def test_citations_are_untouched():
    """A link is not fetched by the renderer, and in a research report the
    links ARE the product. Removing them would fix the vulnerability by
    destroying the document."""
    md = "Revenue rose [[1]](https://openai.com/about) per [NPR](https://npr.org/x)."
    assert sanitise_for_render(md) == md


def test_ordinary_prose_and_code_are_untouched():
    md = "Use `arr[0]` and see <https://example.com>.\n\n```html\n<img src=x>\n```\n"
    out = sanitise_for_render(md)
    assert "arr[0]" in out
    assert "https://example.com" in out


def test_it_is_applied_by_the_renderer_not_left_to_callers(tmp_path):
    """A guard a caller can forget is not a guard: `_run_pandoc` sanitises the
    file itself, and there are two copies of that function in the tree."""
    import inspect

    from app.core.report_render import _run_pandoc as core_pandoc
    from app.engines.report import _run_pandoc as engine_pandoc

    for fn in (core_pandoc, engine_pandoc):
        assert "sanitise_for_render" in inspect.getsource(fn), (
            f"{fn.__module__}._run_pandoc renders without sanitising"
        )
