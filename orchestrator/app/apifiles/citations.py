"""Citations back into the document: markers in the answer → `file_citation`
annotations, but only for content the model was actually shown (2026-09-13).

THE MARKERS (the system addendum in context.py tells the model to use them):

    [<label> p.<n>]        pdf pages
    [<label> §<n>]         docx / text / html sections
    [<label> slide <n>]    presentations
    [<label> rows <a>-<b>] spreadsheets and tabular row blocks
    [<label> <m:ss>]       audio / video (h:mm:ss past an hour)

`<label>` is the file's display name in this request (context.py makes it
unique when two files share a filename).

WHY VALIDATED AGAINST THE SUPPLIED CONTEXT. A model asked about a 1,000-page
report that was shown 40 retrieved pages will still, sometimes, cite a page it
never saw — and a developer rendering "p.842" as a link would send a reader to
text that does not say what the answer claims. So a marker becomes an
annotation only when its (label, unit, value) resolves to a pair recorded in
the `CitationIndex` while the context was built. Everything else stays plain
text and is counted as `citations_unresolved` in usage meta.

WHERE `index` POINTS. The UTF-16 code-unit offset of the marker's opening
bracket in the output text (design §3.4 caveat 9: chosen for JavaScript
consumers, whose string indices are UTF-16). Python's `str` indices are code
points, so an answer containing an emoji or CJK outside the BMP would place a
naive Python offset one unit early per astral character.

MEDIA TOLERANCE. A timestamp resolves when it falls within ±5 s of a span the
model was given (a transcript block, a screen span, or a frame time) —
models round `8:05.4` to `8:05`, and speech blocks start at the first word.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

UNIT_PAGE = "page"
UNIT_SECTION = "section"
UNIT_SLIDE = "slide"
UNIT_ROWS = "rows"
UNIT_TIME = "time"

#: ±5 s (module docstring, MEDIA TOLERANCE).
TIME_TOLERANCE_S = 5.0

#: WHY TWO LINEAR PASSES, NOT ONE REGEX (2026-09-13 review). The first
#: version was one pattern, `\[(?P<label>[^\[\]\n]{1,255}?)\s+(?:unit)\]`:
#: the lazy label and `\s+` both match spaces, so every `[` followed by ~255
#: spaces cost ~255² steps. Measured on the module: page text of lines
#: `"[" + 254 spaces + "x"` (257,000 chars) took 1.25 s to escape ON THE EVENT
#: LOOP, and a 412,800-char answer of that shape took 2.06 s to annotate. Now:
#: `_BRACKET_RE` finds a bracketed span (each `[` scans only up to the next
#: bracket or newline, so the spans are disjoint and the scan is linear), and
#: `_UNIT_RE` is searched inside that span only (≤ 300 chars, each start
#: position failing within a few characters). The same text takes
#: milliseconds (tests/test_apifiles_citations.py pins it).
_BRACKET_RE = re.compile(r"\[([^\[\]\n]{1,300})\]")
_UNIT_RE = re.compile(
    r"\s(?:"
    r"(?P<page>p\.\s?(?P<page_n>\d{1,6}))"
    r"|(?P<section>§\s?(?P<section_n>\d{1,6}))"
    r"|(?P<slide>slide\s(?P<slide_n>\d{1,6}))"
    r"|(?P<rows>rows\s(?P<rows_a>\d{1,9})\s?[-–]\s?(?P<rows_b>\d{1,9}))"
    r"|(?P<time>(?P<t1>\d{1,2}):(?P<t2>\d{2})(?::(?P<t3>\d{2}))?)"
    r")\s*\Z"
)


@dataclass(frozen=True)
class Marker:
    """One marker found in a text: `start`/`end` are code-point offsets of
    the brackets, `label` is stripped, `value` is an int, a (first, last)
    row pair, or seconds as a float."""

    start: int
    end: int
    label: str
    unit: str
    value: Any


def _parse_inside(inside: str) -> Optional[Tuple[str, str, Any]]:
    unit_match = _UNIT_RE.search(inside)
    if unit_match is None:
        return None
    label = inside[: unit_match.start()].strip()
    if not label:
        return None
    if unit_match.group("page"):
        return label, UNIT_PAGE, int(unit_match.group("page_n"))
    if unit_match.group("section"):
        return label, UNIT_SECTION, int(unit_match.group("section_n"))
    if unit_match.group("slide"):
        return label, UNIT_SLIDE, int(unit_match.group("slide_n"))
    if unit_match.group("rows"):
        return label, UNIT_ROWS, (int(unit_match.group("rows_a")), int(unit_match.group("rows_b")))
    if unit_match.group("t3") is not None:
        seconds = int(unit_match.group("t1")) * 3600 + int(unit_match.group("t2")) * 60 + int(unit_match.group("t3"))
    else:
        seconds = int(unit_match.group("t1")) * 60 + int(unit_match.group("t2"))
    return label, UNIT_TIME, float(seconds)


def iter_markers(text: str) -> Iterator[Marker]:
    """Every marker-shaped span of `text`, in order, in linear time. Used for
    the answer (`annotate`) and for file text (context.py neutralises every
    one it finds, wherever it sits on a line)."""
    if not text or "[" not in text:
        return
    for bracket in _BRACKET_RE.finditer(text):
        parsed = _parse_inside(bracket.group(1))
        if parsed is not None:
            yield Marker(bracket.start(), bracket.end(), *parsed)


def unit_for_kind(kind: str) -> str:
    return {
        "pdf": UNIT_PAGE,
        "presentation": UNIT_SLIDE,
        "spreadsheet": UNIT_ROWS,
        "tabular": UNIT_ROWS,
        "audio": UNIT_TIME,
        "video": UNIT_TIME,
    }.get(kind, UNIT_SECTION)


def fmt_ts(seconds: float) -> str:
    """`m:ss` under an hour, `h:mm:ss` above: `video.artifacts.fmt_ts`'s shape,
    restated here so this module imports nothing from the video stack."""
    total = max(0, int(round(float(seconds))))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def marker(label: str, unit: str, value: Any) -> str:
    """The marker text context.py prints beside supplied content."""
    if unit == UNIT_PAGE:
        return f"[{label} p.{int(value)}]"
    if unit == UNIT_SLIDE:
        return f"[{label} slide {int(value)}]"
    if unit == UNIT_ROWS:
        return f"[{label} rows {int(value[0])}-{int(value[1])}]"
    if unit == UNIT_TIME:
        return f"[{label} {fmt_ts(float(value))}]"
    return f"[{label} §{int(value)}]"


@dataclass
class _FileEntry:
    file_id: Optional[str]
    filename: str
    unit: str
    numbers: Dict[int, int] = field(default_factory=dict)          # page/section/slide → page
    row_blocks: List[Tuple[int, int, int]] = field(default_factory=list)  # (a, b, page)
    spans: List[Tuple[float, float]] = field(default_factory=list)


class CitationIndex:
    """The (label, unit, value) pairs whose content was supplied to the model."""

    def __init__(self) -> None:
        self._by_label: Dict[str, _FileEntry] = {}

    def register(self, label: str, *, file_id: Optional[str], filename: str, unit: str) -> None:
        """One label, one file. A label registered again for a DIFFERENT file
        raises: the second file's pages would otherwise be recorded under the
        first file's id, and a citation would send a reader to the wrong file
        (2026-09-13 review: `report (2).pdf` next to two `report.pdf`)."""
        existing = self._by_label.get(label)
        if existing is None:
            self._by_label[label] = _FileEntry(file_id=file_id, filename=filename, unit=unit)
            return
        if existing.file_id != file_id or existing.filename != filename or existing.unit != unit:
            raise ValueError("a citation label may name only one file")

    def add_page(self, label: str, page: int) -> None:
        entry = self._by_label[label]
        entry.numbers[int(page)] = int(page)

    def add_rows(self, label: str, first: int, last: int, page: int) -> None:
        self._by_label[label].row_blocks.append((int(first), int(last), int(page)))

    def add_span(self, label: str, start_s: float, end_s: Optional[float] = None) -> None:
        start = float(start_s)
        end = float(end_s if end_s is not None else start_s)
        self._by_label[label].spans.append((min(start, end), max(start, end)))

    def labels(self) -> List[str]:
        return list(self._by_label)

    def supplied_pages(self, label: str) -> List[int]:
        entry = self._by_label.get(label)
        return sorted(entry.numbers) if entry else []

    def resolve(self, label: str, unit: str, value: Any) -> Optional[Dict[str, Any]]:
        """The annotation fields for a marker, or None when it names content
        that was not supplied (or a unit that is not this file's)."""
        entry = self._by_label.get(label)
        if entry is None:
            return None
        base: Dict[str, Any] = {"file_id": entry.file_id, "filename": entry.filename}
        if unit == UNIT_TIME:
            if entry.unit != UNIT_TIME:
                return None
            t = float(value)
            for start, end in entry.spans:
                if start - TIME_TOLERANCE_S <= t <= end + TIME_TOLERANCE_S:
                    return {**base, "timestamp_s": float(t)}
            return None
        if unit == UNIT_ROWS:
            if entry.unit != UNIT_ROWS:
                return None
            first, last = int(value[0]), int(value[1])
            for a, b, page in entry.row_blocks:
                if first <= b and last >= a:
                    return {**base, "page": int(page)}
            return None
        # page / section / slide: the number must be one that was shown. The
        # unit word must match the file's kind, except that `p.` is accepted
        # for any paged kind — models say "p.3" for a slide.
        if unit != entry.unit and not (unit == UNIT_PAGE and entry.unit in (UNIT_SECTION, UNIT_SLIDE)):
            return None
        number = int(value)
        if number in entry.numbers:
            return {**base, "page": entry.numbers[number]}
        return None


def utf16_offsets(text: str) -> List[int]:
    """offsets[i] = UTF-16 code units before code point i (len(text)+1 long)."""
    out = [0] * (len(text) + 1)
    units = 0
    for i, ch in enumerate(text):
        out[i] = units
        units += 2 if ord(ch) > 0xFFFF else 1
    out[len(text)] = units
    return out


def utf16_index(text: str, index: int) -> int:
    units = 0
    for ch in text[:index]:
        units += 2 if ord(ch) > 0xFFFF else 1
    return units


@dataclass
class Annotated:
    annotations: List[Dict[str, Any]]
    resolved: int
    unresolved: int

    def meta(self) -> Dict[str, int]:
        return {"citations": self.resolved, "citations_unresolved": self.unresolved}


def annotate(text: str, index: CitationIndex) -> Annotated:
    """Scan an answer for markers; resolved ones become annotations in order.

    `text` must be exactly the text of the ONE `output_text` part the
    annotations will be attached to: `index` offsets count from its start.

    A resolved marker whose file has no id (inline `file_data`, which is not a
    File) is counted as resolved but produces no annotation: `file_citation.
    file_id` is a required string in both SDKs' types."""
    annotations: List[Dict[str, Any]] = []
    resolved = unresolved = 0
    if not text or "[" not in text:
        return Annotated(annotations, 0, 0)
    offsets: Optional[List[int]] = None
    for found in iter_markers(text):
        label, unit, value = found.label, found.unit, found.value
        fields = index.resolve(label, unit, value)
        if fields is None:
            unresolved += 1
            continue
        resolved += 1
        if not fields.get("file_id"):
            continue
        if offsets is None:
            offsets = utf16_offsets(text)
        annotation: Dict[str, Any] = {
            "type": "file_citation",
            "file_id": fields["file_id"],
            "filename": fields["filename"],
            "index": offsets[found.start],
        }
        if "page" in fields:
            annotation["page"] = fields["page"]
        if "timestamp_s" in fields:
            annotation["timestamp_s"] = fields["timestamp_s"]
        annotations.append(annotation)
    return Annotated(annotations, resolved, unresolved)


# ------------------------------------------------------------------ wire --


def annotation_added_events(
    annotations: Sequence[Mapping[str, Any]],
    *,
    item_id: str,
    output_index: int = 0,
    content_index: int = 0,
    first_sequence_number: int,
) -> List[Dict[str, Any]]:
    """`response.output_text.annotation.added` events, one per annotation —
    an event type both SDKs already parse (design §5.6)."""
    return [
        {
            "type": "response.output_text.annotation.added",
            "item_id": item_id,
            "output_index": int(output_index),
            "content_index": int(content_index),
            "annotation_index": i,
            "annotation": dict(annotation),
            "sequence_number": int(first_sequence_number) + i,
        }
        for i, annotation in enumerate(annotations)
    ]


def splice_annotation_events(
    events: Sequence[Mapping[str, Any]], annotations: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Put the annotation events after `response.output_text.done` and before
    the terminal event, renumbering what follows, and carry the annotations
    on the terminal Response's `output_text` part.

    For the streaming integration (publicapi/streaming.py): the events of a
    finished generation in, the events a client receives out. A stream with
    no `output_text.done` (failed before any text) is returned unchanged."""
    out: List[Dict[str, Any]] = [dict(e) for e in events]
    if not annotations:
        return out
    position = next((i for i, e in enumerate(out) if e.get("type") == "response.output_text.done"), None)
    if position is None:
        return out
    done = out[position]
    added = annotation_added_events(
        annotations,
        item_id=str(done.get("item_id") or ""),
        output_index=int(done.get("output_index") or 0),
        content_index=int(done.get("content_index") or 0),
        first_sequence_number=int(done.get("sequence_number") or 0) + 1,
    )
    tail = out[position + 1:]
    shift = len(added)
    content_index = int(done.get("content_index") or 0)
    for event in tail:
        if isinstance(event.get("sequence_number"), int):
            event["sequence_number"] = int(event["sequence_number"]) + shift
        response = event.get("response")
        if isinstance(response, Mapping):
            event["response"] = with_annotations(response, annotations)
        # `content_part.done` and `output_item.done` repeat the finished part.
        # Clients that build items from them (agent frameworks commonly do)
        # must see the same annotations `response.completed` carries.
        part = event.get("part")
        if event.get("type") == "response.content_part.done" and isinstance(part, Mapping) and part.get("type") == "output_text":
            event["part"] = {**part, "annotations": [dict(a) for a in annotations]}
        item = event.get("item")
        if event.get("type") == "response.output_item.done" and isinstance(item, Mapping):
            content = list(item.get("content") or [])
            if 0 <= content_index < len(content) and isinstance(content[content_index], Mapping) \
                    and content[content_index].get("type") == "output_text":
                content[content_index] = {**content[content_index], "annotations": [dict(a) for a in annotations]}
                event["item"] = {**item, "content": content}
    return out[: position + 1] + added + tail


def with_annotations(response: Mapping[str, Any], annotations: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """A Response object whose first output_text part carries `annotations`."""
    copy: Dict[str, Any] = dict(response)
    output = []
    placed = False
    for item in copy.get("output") or []:
        item = dict(item)
        content = []
        for part in item.get("content") or []:
            part = dict(part)
            if not placed and part.get("type") == "output_text":
                part["annotations"] = [dict(a) for a in annotations]
                placed = True
            content.append(part)
        if "content" in item:
            item["content"] = content
        output.append(item)
    if "output" in copy:
        copy["output"] = output
    return copy


def chat_message_annotations(annotations: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """`choices[0].message.annotations` (and the final chunk's
    `delta.annotations`) for Chat Completions: the same objects."""
    return [dict(a) for a in annotations]
