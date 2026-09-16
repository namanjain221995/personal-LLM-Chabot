"""A chart named as the subject is a chart request.

Measured 2026-09-16 on the understanding harness: "histogram of hours worked",
"scatter of salary vs experience" and "pie of sales by region" were answered
with a Word file and a PDF, because the ask verbs alone decided the shape.
"""
import pytest

from app.artifacts import formats as F


@pytest.mark.parametrize("text", [
    "histogram of hours worked",
    "scatter of salary vs experience",
    "pie of sales by region",
    "heat map of usage by day",
    "a pareto of defects by cause",
    "waterfall showing revenue by quarter",
    "treemap of spend by team",
    "sunburst of sales by region and rep",
    "candlestick of prices",
    "violin plot of scores by class",
    "bar chart of tickets per week",
])
def test_a_chart_named_as_the_subject_is_a_chart(text: str) -> None:
    assert F.decide(text).formats == ["png"], text


@pytest.mark.parametrize("text,formats", [
    # A document is still a document, and the chart is one of its blocks.
    ("make a report with a pie chart", ["docx", "pdf"]),
    ("make a report with a waterfall chart", ["docx", "pdf"]),
    # An explicit format still decides.
    ("pie chart of sales as pdf", ["pdf"]),
    ("give me a bar chart in excel", ["xlsx"]),
    # The word names something else entirely.
    ("plot of the movie Inception", ["docx", "pdf"]),
    ("the plot of this novel by chapter", ["docx", "pdf"]),
    ("write a report on the plot of Inception", ["docx", "pdf"]),
    ("bullet points on climate change", ["docx", "pdf"]),
    ("a line of business summary", ["docx", "pdf"]),
])
def test_what_must_not_become_a_chart_image(text: str, formats: list) -> None:
    assert F.decide(text).formats == formats, text
