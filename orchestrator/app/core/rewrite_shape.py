"""The Markdown shape of a rewrite into a pasted sample's format (hotfix 1.2, P3).

THE REPORT. A job description pasted as plain lines, then "change it in the same
way" as a pasted plain-lines sample ("Job Title: ...", "Key Skills", item
lines). The chat UI renders Markdown, and the prompt says how a plain sample
maps onto it; on Fast the model followed that mapping in 1 of 3 runs at best
(harness live-out/pasted-format): section names came back as bold lines or as
plain text, item lines stayed plain, and `Job Title: X` became `## Job Title: X`.
The mapping is fully decided by the sample, so a rule applies it instead of the
model:

  * a `Label: value` line is never a heading: `## Label: value` and a plain
    `Label: value` both become `**Label:** value`;
  * a section name becomes a `## ` heading: a line that is only bold text, or
    a plain line that repeats one of the sample's own section-name lines;
  * two or more consecutive short plain lines under a section become `- `
    bullets (one line on its own stays prose: an "About" paragraph).

Lines already in Markdown (headings, bullets, numbered items, quotes, tables)
and everything inside a code fence pass through untouched. Only a rewrite INTO
A PASTED SAMPLE gets this: `for_message` returns None for every other turn, so
no other answer is touched. Pure and streaming: `feed` takes deltas and returns
the text that is now decided (at most one line is held back), `finish` returns
the rest.
"""
from __future__ import annotations

import re
from typing import FrozenSet, List, Optional

from . import pasted

#: The asks whose answer is a SHAPE taken from a sample.
_SHAPE_ASK = re.compile(
    r"\b(?:sample|template|re-?format(?:s|ted|ting)?|format(?:s|ted|ting)?|re-?structur(?:e|es|ed|ing)|"
    r"layout|same\s+(?:way|shape|structure|pattern|manner|style))\b",
    re.I,
)
#: ...unless the shape asked for is one Markdown shaping would break.
_NOT_MARKDOWN = re.compile(
    r"\b(?:csv|tsv|json|yaml|xml|html|sql|code|table|plain\s+text|no\s+markdown|"
    r"without\s+(?:any\s+)?(?:markdown|formatting))\b",
    re.I,
)
_LABEL = r"[A-Za-z][A-Za-z /&()'’-]{0,38}?"
#: `Label: value`, the label at most five words.
_PLAIN_LABEL = re.compile(rf"^({_LABEL})\s*:\s+(\S.*)$")
#: A sample's label line, with or without a value (a skeleton sample leaves
#: `Job Title:` empty for the rewrite to fill).
_SAMPLE_LABEL = re.compile(rf"^({_LABEL})\s*:(?:\s+\S.*)?\s*$")
#: `## Label: value` (optionally with the label bolded).
_HEADING_LABEL = re.compile(rf"^#{{1,6}}\s+(?:\*\*)?({_LABEL})\s*:\s*(?:\*\*)?\s*(\S.*)$")
#: Already a bold label: `**Label:** value` or `**Label**: value`.
_BOLD_LABEL = re.compile(r"^\*\*[^*\n]{1,60}?(?::\*\*|\*\*\s*:)")
#: A line that is only bold text: `**Key Skills**`, `**Key Skills:**`.
_BOLD_ONLY = re.compile(r"^\*\*([^*\n]{1,50}?)\s*:?\s*\*\*\s*:?\s*$")
#: Already Markdown: heading, bullet, numbered item, quote, table, rule.
_MARKDOWN = re.compile(r"^(?:#{1,6}\s|[-*+]\s|\d+[.)]\s|>|\||-{3,}\s*$|\*{3,}\s*$)")
_FENCE = re.compile(r"^\s*(?:```|~~~)")
#: A plain line longer than this is a paragraph, never a bullet.
ITEM_MAX_CHARS = 220
#: A sample block holds at least this many `Label: value` lines.
SAMPLE_MIN_LABELS = 2


def _words(text: str) -> int:
    return len(text.split())


def _is_section_name(line: str) -> bool:
    s = line.strip()
    return (
        0 < len(s) <= 50
        and _words(s) <= 6
        and ":" not in s
        and "," not in s
        and not re.search(r"[.;!?]$", s)
        and s[0].isalpha()
    )


def _norm(text: str) -> str:
    return " ".join(text.lower().strip().rstrip(":").split())


def sample_sections(message: str) -> Optional[FrozenSet[str]]:
    """The pasted sample's section-name lines (normalised), or None when this
    message is not a rewrite into a pasted sample's format."""
    read = pasted.read(message)
    if read is None:
        return None
    ask = " ".join(read.asks)
    if not _SHAPE_ASK.search(ask) or _NOT_MARKDOWN.search(ask):
        return None
    if len(read.material) < 2:
        # One pasted block is the source itself: there is no sample to take a
        # shape from, and the prompt's own format rule already serves it.
        return None
    best: Optional[List[str]] = None
    best_share = 0.0
    for block in read.material:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        labels = sum(1 for ln in lines if _SAMPLE_LABEL.match(ln) and _words(ln.split(":")[0]) <= 5)
        share = labels / len(lines) if lines else 0.0
        if labels >= SAMPLE_MIN_LABELS and share > best_share:
            best, best_share = lines, share
    if best is None:
        return None
    return frozenset(_norm(ln) for ln in best if _is_section_name(ln))


class Shaper:
    """Streams an answer, applying the sample's Markdown mapping line by line."""

    def __init__(self, sections: FrozenSet[str]):
        self.sections = sections
        self._partial = ""
        self._fenced = False
        #: A single short plain line waiting to learn whether a list follows.
        self._held: Optional[str] = None
        self._in_list = False

    # -- line classification -------------------------------------------------

    def _item_candidate(self, line: str) -> bool:
        s = line.strip()
        return bool(s) and len(s) <= ITEM_MAX_CHARS and not _MARKDOWN.match(s) and not _BOLD_ONLY.match(s)

    def _shape_line(self, line: str) -> Optional[str]:
        """The line rewritten as a heading or a bold label, or None when it is
        not one (an item candidate, a blank line, or Markdown already)."""
        s = line.strip()
        m = _HEADING_LABEL.match(s)
        if m and _words(m.group(1)) <= 5:
            return f"**{m.group(1).strip()}:** {m.group(2).strip().strip('*').strip()}"
        m = _BOLD_ONLY.match(s)
        if m:
            return f"## {m.group(1).strip()}"
        if _MARKDOWN.match(s) or _BOLD_LABEL.match(s):
            return s
        m = _PLAIN_LABEL.match(s)
        if m and _words(m.group(1)) <= 5:
            return f"**{m.group(1).strip()}:** {m.group(2).strip()}"
        if _is_section_name(s.rstrip(":")) and _norm(s) in self.sections:
            return f"## {s.rstrip(':').strip()}"
        return None

    # -- the held line -------------------------------------------------------

    def _release(self, as_item: bool) -> List[str]:
        if self._held is None:
            return []
        out = [f"- {self._held.strip()}" if as_item else self._held]
        self._held = None
        return out

    def _line(self, line: str) -> List[str]:
        if _FENCE.match(line):
            out = self._release(self._in_list)
            self._in_list = False
            self._fenced = not self._fenced
            return out + [line]
        if self._fenced:
            return [line]
        if not line.strip():
            # A blank line ends a list: two short paragraphs are prose, not
            # two bullets.
            out = self._release(self._in_list)
            self._in_list = False
            return out + [line]
        shaped = self._shape_line(line)
        if shaped is not None:
            out = self._release(self._in_list)
            self._in_list = False
            return out + [shaped]
        if not self._item_candidate(line):
            out = self._release(self._in_list)
            self._in_list = False
            return out + [line]
        # A short plain line: a list item when another one follows it.
        if self._in_list:
            return [f"- {line.strip()}"]
        if self._held is not None:
            out = self._release(True)
            self._in_list = True
            return out + [f"- {line.strip()}"]
        self._held = line
        return []

    # -- streaming -----------------------------------------------------------

    def feed(self, text: str) -> str:
        self._partial += text
        if "\n" not in self._partial:
            return ""
        *complete, self._partial = self._partial.split("\n")
        out: List[str] = []
        for line in complete:
            out.extend(self._line(line))
        return "".join(ln + "\n" for ln in out)

    def finish(self) -> str:
        out: List[str] = []
        if self._partial:
            out.extend(self._line(self._partial))
            self._partial = ""
        out.extend(self._release(self._in_list))
        return "\n".join(out)


def for_message(message: str) -> Optional[Shaper]:
    """A Shaper for a rewrite into a pasted sample's format, else None."""
    sections = sample_sections(message)
    return None if sections is None else Shaper(sections)


def shape(message: str, answer: str) -> str:
    """The whole-answer form of the same rule (a non-streamed answer)."""
    shaper = for_message(message)
    if shaper is None:
        return answer
    return shaper.feed(answer) + shaper.finish()
