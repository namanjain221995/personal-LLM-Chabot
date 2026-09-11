"""Which formats a request gets, and what kind of artifact it is, when the
person did not say — the format-selection policy, as a table with reasons.

    Request                                        Kind          Formats
    professional document / report / SOP / memo    document      docx + pdf
    final printable / shareable document           document      pdf
    presentation / deck / pitch / CEO presentation presentation  pptx (+ pdf preview)
    spreadsheet / tracker / model / calculator     workbook      xlsx
    one-page executive brief                       document      pdf + docx
    proposal / policy / letter                     document      docx + pdf
    visual handout                                 document      pdf
    "best format"                                  decided from the task words
    "all deliverables"                             only the formats that are useful

An EXPLICIT format always wins over the table. Nothing here is a guess in
natural language: `decide()` returns a `FormatDecision` with the rule that
fired, which is stored in the job's metadata so a person can see why a deck
came back as a .pptx.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from . import types as T

# Order matters: the first rule whose words match decides the kind.
_KIND_RULES: Tuple[Tuple[str, str, str], ...] = (
    # (kind, rule name, pattern)
    ("presentation", "deck words", r"\b(presentation|slides?|slide ?deck|deck|pitch ?deck|powerpoint|pptx|keynote)\b"),
    ("workbook", "spreadsheet words", r"\b(spreadsheet|workbook|excel|xlsx|tracker|budget|calculator|financial model|pivot|dashboard sheet|data extract|csv)\b"),
    ("document", "document words", r"\b(document|report|sop|standard operating procedure|memo|brief|one[- ]pager|one[- ]page|proposal|policy|letter|handout|summary|write[- ]?up|whitepaper|white paper|guide|manual|plan|pdf|docx|word)\b"),
)

_EXPLICIT_FORMAT: Tuple[Tuple[str, str], ...] = (
    ("pdf", r"\bpdf\b"),
    ("docx", r"\b(docx|word (?:document|file|doc|version|copy|format)|ms word|microsoft word|\.docx|in word|as word|to word)\b"),
    # Format NAMES only. "deck", "slides", "spreadsheet" are kind words: they
    # choose the kind below, and the kind's default formats follow.
    ("pptx", r"\b(pptx|powerpoint|power point|\.pptx)\b"),
    ("xlsx", r"\b(xlsx|excel|\.xlsx)\b"),
)

_PRINTABLE = re.compile(r"\b(printable|print|final|shareable|share with|send to|handout|read[- ]only)\b", re.I)
_EDITABLE = re.compile(r"\b(editable|edit later|so (?:i|we) can edit|track changes|word)\b", re.I)
_BRIEF = re.compile(r"\b(one[- ]pager|one[- ]page|executive brief|exec brief|brief)\b", re.I)
_TEMPLATE_HINTS: Tuple[Tuple[str, str], ...] = (
    ("sop", r"\b(sop|standard operating procedure|procedure|runbook|checklist)\b"),
    ("brief", r"\b(one[- ]pager|one[- ]page|executive brief|exec brief|brief)\b"),
    ("technical_report", r"\b(technical|architecture|design doc|engineering|specification|spec\b)"),
    ("research_report", r"\b(research|literature|market study|landscape|survey of)\b"),
    ("proposal", r"\b(proposal|pitch|quote|quotation|statement of work|sow|offer)\b"),
    ("meeting_summary", r"\b(meeting|minutes|notes|standup|stand-up|call summary|transcript)\b"),
    ("executive_report", r"\b(executive|ceo|board|leadership|quarterly|annual|status report|business review)\b"),
)
_PRESENTATION_HINTS: Tuple[Tuple[str, str], ...] = (
    ("ceo", r"\b(ceo|board|executive|leadership|investor|pitch)\b"),
    ("quarterly_review", r"\b(quarterly|qbr|q[1-4]\b|annual review|business review|results)\b"),
    ("training", r"\b(training|onboarding|workshop|tutorial|lesson|course|how[- ]to)\b"),
)
_WORKBOOK_HINTS: Tuple[Tuple[str, str], ...] = (
    ("dashboard", r"\b(dashboard|kpi|metrics|summary sheet|overview)\b"),
    ("tracker", r"\b(tracker|track|log|register|checklist|budget|plan)\b"),
    ("data", r"\b(data|extract|export|records|rows|table|list)\b"),
)


@dataclass
class FormatDecision:
    kind: str
    formats: List[str]
    template_id: str
    #: Which rule fired, for the job metadata: "explicit: pdf" / "deck words".
    reason: str
    explicit: bool = False
    warnings: List[str] = field(default_factory=list)


def explicit_formats(text: str) -> List[str]:
    """Formats the person NAMED. Order follows first mention."""
    found: List[Tuple[int, str]] = []
    low = text or ""
    for fmt, pattern in _EXPLICIT_FORMAT:
        m = re.search(pattern, low, re.I)
        if m:
            found.append((m.start(), fmt))
    found.sort()
    out: List[str] = []
    for _, fmt in found:
        if fmt not in out:
            out.append(fmt)
    return out


def kind_for(text: str, explicit: Sequence[str]) -> Tuple[str, str]:
    """(kind, rule). An explicit format implies its kind; otherwise the
    first kind-rule that matches; otherwise a document."""
    if explicit:
        first = explicit[0]
        for kind, fmts in T.FORMATS_FOR_KIND.items():
            if first in fmts and (first != "pdf" or kind == "document"):
                return kind, f"explicit: {first}"
    for kind, rule, pattern in _KIND_RULES:
        if re.search(pattern, text or "", re.I):
            return kind, rule
    return "document", "default"


def template_for(kind: str, text: str) -> str:
    hints = {"document": _TEMPLATE_HINTS, "presentation": _PRESENTATION_HINTS, "workbook": _WORKBOOK_HINTS}[kind]
    for template_id, pattern in hints:
        if re.search(pattern, text or "", re.I):
            return template_id
    return "generic"


def decide(text: str, *, explicit_only: Optional[Sequence[str]] = None) -> FormatDecision:
    """The policy, applied to one request.

    `explicit_only` lets a caller that already knows the formats (an edit
    of an existing artifact keeps its formats; a conversion names one) skip
    the text rules for the format while still classifying the template.
    """
    text = text or ""
    explicit = list(explicit_only) if explicit_only is not None else explicit_formats(text)
    kind, rule = kind_for(text, explicit)
    allowed = T.FORMATS_FOR_KIND[kind]
    warnings: List[str] = []

    if explicit:
        formats = [f for f in explicit if f in allowed]
        dropped = [f for f in explicit if f not in allowed]
        if dropped:
            warnings.append(f"{', '.join(dropped)} cannot be produced for a {kind}; it was not made")
        if not formats:
            formats = list(_default_formats(kind, text))
            reason = f"explicit formats unsupported for {kind}; defaulted"
        else:
            # A presentation named as pptx still gets its PDF preview file —
            # the viewer needs it and it is what "share the deck" wants.
            if kind == "presentation" and "pdf" not in formats:
                formats.append("pdf")
            reason = f"explicit: {', '.join(explicit)}"
        return FormatDecision(kind, formats, template_for(kind, text), reason, explicit=True, warnings=warnings)

    formats = list(_default_formats(kind, text))
    return FormatDecision(kind, formats, template_for(kind, text), rule, explicit=False, warnings=warnings)


def _default_formats(kind: str, text: str) -> Sequence[str]:
    if kind == "presentation":
        return ("pptx", "pdf")
    if kind == "workbook":
        return ("xlsx",)
    # document
    if _BRIEF.search(text):
        return ("pdf", "docx")
    if _PRINTABLE.search(text) and not _EDITABLE.search(text):
        return ("pdf",)
    if _EDITABLE.search(text) and not _PRINTABLE.search(text):
        return ("docx", "pdf")
    return ("docx", "pdf")


def formats_for_conversion(kind: str, requested: Sequence[str]) -> Tuple[List[str], List[str]]:
    """(formats we will make, formats we refuse) for "convert it to X"."""
    allowed = T.FORMATS_FOR_KIND[kind]
    ok = [f for f in requested if f in allowed]
    bad = [f for f in requested if f not in allowed]
    return ok, bad
