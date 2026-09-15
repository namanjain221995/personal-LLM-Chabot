"""Markdown (or an uploaded Word file) → a DocumentSpec, by code.

    "give it in docs" after a 40,000-character audit answer
        -> markdown_to_document(previous_answer_md)   0 model calls
        -> DocumentSpec: every heading, paragraph, list item and table cell

WHY NOT THE MODEL. Asking the model to retype a long answer into the spec
schema costs a whole generation and drifts: cells are paraphrased, rows are
dropped, headings renamed. An export is a CONVERSION, so it is done by a
deterministic importer that is faithful by construction; the model may add
only what the answer does not have (a cover subtitle, a title when the answer
has no heading).

WHAT MAPS TO WHAT.
    # / ## / ### (and setext)   Heading 1-3 (#### and deeper -> 3)
    first H1                     the title (and not repeated as a heading)
    paragraphs                   Paragraph (split at 6,000 chars on sentences)
    - * + / 1. 2.                Bullets / Numbered (nesting flattened to 2
                                 levels: a nested item is prefixed "– ")
    GFM pipe table               Table; numeric columns inferred by code;
                                 > 12 columns or > 200 rows split into parts
    ``` fenced code              Callout "Code" (text kept verbatim)
    ```mermaid                   Callout "Diagram omitted" — never executed
    > quote                      Callout quote
    **bold**, _italic_, `code`   plain text (the spec has no inline runs)
    [text](url)                  "text (url)" for http/https/mailto only;
                                 javascript:, file:, data: and the rest keep
                                 the text and drop the URL
    ![alt](src)                  the alt text; the image is never fetched
    raw HTML                     tags removed, text kept

SECURITY. Nothing here fetches, executes or embeds: no image is downloaded,
no HTML is passed through, and a Word upload is read as TEXT AND TABLES ONLY
(python-docx over paragraphs and tables, after the zip-bomb caps): no XML
part, relationship, field code (INCLUDEPICTURE, HYPERLINK instrText),
altChunk, OLE object or external image of the upload reaches the new file.
"""
from __future__ import annotations

import re
from typing import Any, List, Optional, Sequence, Tuple

from . import spec as S
from . import types as T

#: Validator ceilings from spec.py, mirrored so the importer splits instead of
#: letting pydantic clip (a clipped list item is lost text).
_PARA_MAX = 6000
_ITEM_MAX = 600
_ITEMS_PER_LIST = 40
_CALLOUT_MAX = 2000
_HEADING_MAX = 200
_TITLE_MAX = 120
_COL_NAME_MAX = 80
_BLOCKS_MAX = 400

_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})\s*([\w+-]*)")
_ATX_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT_RE = re.compile(r"^\s{0,3}(=+|-+)\s*$")
_BULLET_RE = re.compile(r"^(\s*)([-*+•])\s+(.*)$")
_NUMBER_RE = re.compile(r"^(\s*)(\d{1,4})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_HR_RE = re.compile(r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")
_TABLE_ROW_RE = re.compile(r"^\s*\|?.*\|.*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")

#: A link target may hold one level of balanced parentheses ("javascript:alert(1)").
_URL_PART = r"(?:[^()\s]|\([^()\s]*\))+"
_IMAGE_RE = re.compile(rf"!\[([^\]]*)\]\(\s*({_URL_PART})?(?:\s+\"[^\"]*\")?\s*\)")
_LINK_RE = re.compile(rf"\[([^\]]+)\]\(\s*({_URL_PART})(?:\s+\"[^\"]*\")?\s*\)")
_AUTOLINK_RE = re.compile(r"<((?:https?|mailto):[^>\s]+)>", re.I)
_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")
_HTML_CODE_RE = re.compile(r"<(script|style)\b[^<>]{0,500}>[^<]{0,20000}</\1\s*>", re.I)
_SAFE_URL_RE = re.compile(r"^(?:https?://|mailto:)", re.I)
_NUMERIC_RE = re.compile(r"^[\s(]*[-+−]?\s*[₹$€£]?\s*[-+−]?\d[\d,]*(?:\.\d+)?\s*(?:%|[kKmMbB]|cr|lakh|crore)?[\s)]*$")


_BOLD_RE = re.compile(r"(\*\*|__)(?=\S)((?:(?!\1)[^\n]){1,1000}?)(?<=\S)\1")
_STAR_RE = re.compile(r"(?<![\w*])\*(?=\S)([^*\n]{1,1000}?)(?<=\S)\*(?![\w*])")
_UNDER_RE = re.compile(r"(?<![\w_])_(?=\S)([^_\n]{1,1000}?)(?<=\S)_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)((?:(?!~~)[^\n]){1,1000}?)(?<=\S)~~")


def _inline(text: str, notes: List[str]) -> str:
    """Inline markdown → plain text, keeping every visible word."""
    t = text
    t = _IMAGE_RE.sub(lambda m: m.group(1).strip() or "image", t)

    def link(m: "re.Match[str]") -> str:
        label, url = m.group(1).strip(), m.group(2).strip()
        if _SAFE_URL_RE.match(url):
            return f"{label} ({url})" if url not in label else label
        if url and not url.startswith("#"):
            notes.append("A link with an unsupported address was kept as text without its address.")
        return label

    t = _LINK_RE.sub(link, t)
    t = _AUTOLINK_RE.sub(lambda m: m.group(1), t)
    # The CONTENT of a script/style element is code, not visible text (verifier 2026-09-15).
    t = _HTML_CODE_RE.sub("", t)
    t = _HTML_TAG_RE.sub("", t)
    # Emphasis and code spans: the markers go, the words stay.
    # Each span stops at the next marker and is bounded, so a paragraph of
    # unmatched markers ("*a *a *a …", 60k chars) is linear, not quadratic
    # (verifier 2026-09-15: 11 s for 60k chars; a 400k TXT upload held the
    # GIL for minutes).
    t = _BOLD_RE.sub(r"\2", t)
    t = _STAR_RE.sub(r"\1", t)
    t = _UNDER_RE.sub(r"\1", t)
    t = _STRIKE_RE.sub(r"\1", t)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\\([\\`*_{}\[\]()#+\-.!|>~])", r"\1", t)
    return t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").strip()


def _split_text(text: str, limit: int) -> List[str]:
    """Chunks of at most `limit` characters, cut at sentence or word
    boundaries — no character is dropped."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: List[str] = []
    rest = text
    while len(rest) > limit:
        window = rest[:limit]
        cut = max(window.rfind(". "), window.rfind("? "), window.rfind("! "), window.rfind("\n"))
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 4:
            cut = limit - 1
        out.append(rest[:cut + 1].strip())
        rest = rest[cut + 1:].strip()
    if rest:
        out.append(rest)
    return out


def _cells(line: str) -> List[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    parts = re.split(r"(?<!\\)\|", s)
    return [p.strip().replace("\\|", "|") for p in parts]


def _numeric_columns(rows: Sequence[Sequence[Any]], width: int) -> List[int]:
    out: List[int] = []
    for c in range(width):
        values = [str(r[c]).strip() for r in rows if c < len(r) and r[c] not in (None, "") and str(r[c]).strip() not in ("-", "—", "n/a", "N/A")]
        if values and sum(1 for v in values if _NUMERIC_RE.match(v)) >= max(1, int(0.8 * len(values) + 0.999)):
            out.append(c)
    return out


def _table_blocks(header: List[str], rows: List[List[str]], notes: List[str], caption: str = "") -> List[S.TableBlock]:
    width = max([len(header)] + [len(r) for r in rows]) if rows else len(header)
    header = header + [""] * (width - len(header))
    fixed: List[List[str]] = []
    for r in rows:
        fixed.append(r + [""] * (width - len(r)))
    for i, h in enumerate(header):
        if len(h) > _COL_NAME_MAX:
            notes.append(f"A table heading longer than {_COL_NAME_MAX} characters was shortened: {h}")
        if not h.strip():
            header[i] = f"Column {i + 1}"
    col_chunks: List[List[int]] = []
    max_cols = T.MAX_TABLE_COLUMNS
    if width <= max_cols:
        col_chunks = [list(range(width))]
    else:
        # The first column labels every part.
        rest = list(range(1, width))
        step = max_cols - 1
        col_chunks = [[0] + rest[i:i + step] for i in range(0, len(rest), step)]
        notes.append(f"A table with {width} columns was split into {len(col_chunks)} tables of at most {max_cols} columns.")
    row_chunks = [fixed[i:i + T.MAX_TABLE_ROWS] for i in range(0, len(fixed), T.MAX_TABLE_ROWS)] or [[]]
    if len(row_chunks) > 1:
        notes.append(f"A table with {len(fixed)} rows was split into {len(row_chunks)} parts of at most {T.MAX_TABLE_ROWS} rows.")
    blocks: List[S.TableBlock] = []
    for ci, cols in enumerate(col_chunks):
        for ri, chunk in enumerate(row_chunks):
            sub_rows = [[r[c] if r[c] != "" else None for c in cols] for r in chunk]
            part = ""
            if len(col_chunks) > 1 or len(row_chunks) > 1:
                part = f" (part {ci * len(row_chunks) + ri + 1} of {len(col_chunks) * len(row_chunks)})"
            blocks.append(S.TableBlock(table=S.Table(
                columns=[header[c] for c in cols],
                rows=sub_rows,
                numeric_columns=_numeric_columns(sub_rows, len(cols)),
                caption=(caption + part).strip()[:300],
            )))
    return blocks


def _heading_text(text: str) -> str:
    return text.strip()[:_HEADING_MAX] or "Section"


class _Builder:
    def __init__(self) -> None:
        self.blocks: List[Any] = []
        self.notes: List[str] = []
        self.title: str = ""

    def heading(self, level: int, text: str) -> None:
        if not text.strip():
            return
        if len(text) > _HEADING_MAX:
            self.notes.append("A heading longer than 200 characters continues in the paragraph below it.")
            self.blocks.append(S.Heading(level=min(3, max(1, level)), text=text[:_HEADING_MAX]))
            self.paragraph(text[_HEADING_MAX:])
            return
        self.blocks.append(S.Heading(level=min(3, max(1, level)), text=_heading_text(text)))

    def paragraph(self, text: str) -> None:
        for chunk in _split_text(text, _PARA_MAX):
            self.blocks.append(S.Paragraph(text=chunk))

    def items(self, numbered: bool, items: List[str]) -> None:
        flat: List[str] = []
        for item in items:
            # Room for the "– " a continuation carries.
            pieces = _split_text(item, _ITEM_MAX - 2)
            flat.append(pieces[0] if pieces else item)
            for more in pieces[1:]:
                flat.append("– " + more if not more.startswith("– ") else more)
        flat = [i for i in flat if i.strip()]
        cls = S.Numbered if numbered else S.Bullets
        for i in range(0, len(flat), _ITEMS_PER_LIST):
            self.blocks.append(cls(items=flat[i:i + _ITEMS_PER_LIST]))

    def callout(self, kind: str, title: str, text: str) -> None:
        for chunk in _split_text(text, _CALLOUT_MAX):
            self.blocks.append(S.Callout(kind=kind, title=title[:120], text=chunk))

    def compact(self) -> None:
        """Past the block ceiling, adjacent paragraphs under one heading are
        joined (text kept), then adjacent lists of the same kind."""
        if len(self.blocks) <= _BLOCKS_MAX:
            return
        merged: List[Any] = []
        for b in self.blocks:
            prev = merged[-1] if merged else None
            if isinstance(b, S.Paragraph) and isinstance(prev, S.Paragraph) and len(prev.text) + len(b.text) + 2 <= _PARA_MAX:
                merged[-1] = S.Paragraph(text=prev.text + "\n\n" + b.text)
                continue
            if (type(b) is type(prev) and isinstance(b, S.Bullets) and len(prev.items) + len(b.items) <= _ITEMS_PER_LIST):
                merged[-1] = type(b)(items=list(prev.items) + list(b.items))
                continue
            merged.append(b)
        if len(merged) > _BLOCKS_MAX:
            self.notes.append(f"The answer has more than {_BLOCKS_MAX} blocks; the rest is joined into the last section.")
            head, tail = merged[:_BLOCKS_MAX - 1], merged[_BLOCKS_MAX - 1:]
            text = "\n\n".join(_block_text(b) for b in tail)
            merged = head + [S.Paragraph(text=text[:_PARA_MAX])]
            if len(text) > _PARA_MAX:
                self.notes.append("Text past the document's block ceiling was clipped.")
        self.notes.append("Adjacent paragraphs were joined to stay within the document's block ceiling.")
        self.blocks = merged


def _block_text(b: Any) -> str:
    if isinstance(b, S.Heading):
        return b.text
    if isinstance(b, (S.Paragraph, S.Callout)):
        return b.text
    if isinstance(b, S.Bullets):
        return "\n".join(b.items)
    if isinstance(b, S.TableBlock):
        rows = [" | ".join(b.table.columns)] + [" | ".join("" if c is None else str(c) for c in r) for r in b.table.rows]
        return "\n".join(rows)
    return ""


def markdown_to_document(md: str, *, title_hint: str = "") -> Tuple[S.DocumentSpec, List[str]]:
    """A deterministic import of markdown. Raises ValueError on empty input."""
    text = (md or "").replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        raise ValueError("there is no text to import")
    b = _Builder()
    lines = text.split("\n")
    i = 0
    para: List[str] = []
    list_kind: Optional[bool] = None  # True numbered, False bullets
    list_items: List[str] = []
    list_base_indent = 0

    def flush_para() -> None:
        if para:
            # A markdown hard break (two trailing spaces or a backslash) is a
            # line break; every other newline inside a paragraph is a space.
            joined = ""
            prev_hard = False
            for p in para:
                piece = p.strip().rstrip("\\").strip()
                if not piece:
                    continue
                if joined:
                    joined += "\n" if prev_hard else " "
                joined += piece
                prev_hard = p.endswith("  ") or p.rstrip().endswith("\\")
            if joined:
                b.paragraph(_inline(joined, b.notes))
            para.clear()

    def flush_list() -> None:
        nonlocal list_kind, list_items
        if list_items:
            b.items(bool(list_kind), [_inline(x, b.notes) for x in list_items])
        list_kind, list_items = None, []

    first_h1_taken = False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        fence = _FENCE_RE.match(line)
        if fence:
            flush_para(); flush_list()
            marker, lang = fence.group(1), (fence.group(2) or "").lower()
            body: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker[:3]):
                body.append(lines[i])
                i += 1
            i += 1
            code = "\n".join(body).strip("\n")
            if lang == "mermaid":
                b.callout("note", "Diagram omitted", "A diagram in the answer was not reproduced in this document.")
                b.notes.append("A mermaid diagram was omitted (diagrams are never executed while importing).")
            elif code.strip():
                b.callout("note", f"Code ({lang})" if lang else "Code", code)
            continue
        if not stripped:
            flush_para(); flush_list()
            i += 1
            continue
        atx = _ATX_RE.match(line)
        if atx:
            flush_para(); flush_list()
            level = len(atx.group(1))
            htext = _inline(atx.group(2), b.notes)
            if level == 1 and not first_h1_taken and not b.title and not b.blocks:
                b.title = htext
                first_h1_taken = True
            elif htext:
                b.heading(level, htext)
            i += 1
            continue
        if i + 1 < len(lines) and para == [] and list_kind is None and _SETEXT_RE.match(lines[i + 1]) and not _TABLE_ROW_RE.match(line) \
                and not _BULLET_RE.match(line) and not _HR_RE.match(line):
            level = 1 if lines[i + 1].strip().startswith("=") else 2
            htext = _inline(stripped, b.notes)
            if level == 1 and not b.title and not b.blocks:
                b.title = htext
            else:
                b.heading(level, htext)
            i += 2
            continue
        if _HR_RE.match(line):
            flush_para(); flush_list()
            i += 1
            continue
        if _TABLE_ROW_RE.match(line) and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_para(); flush_list()
            header = [_inline(c, b.notes) for c in _cells(line)]
            i += 2
            rows: List[List[str]] = []
            while i < len(lines) and lines[i].strip() and "|" in lines[i]:
                cells = [_inline(c, b.notes) for c in _cells(lines[i])]
                if len(cells) > len(header):
                    cells = cells[:len(header) - 1] + [" | ".join(cells[len(header) - 1:])] if header else cells
                rows.append(cells)
                i += 1
            for block in _table_blocks(header, rows, b.notes):
                b.blocks.append(block)
            continue
        quote = _QUOTE_RE.match(line)
        if quote:
            flush_para(); flush_list()
            body = []
            while i < len(lines) and _QUOTE_RE.match(lines[i]):
                body.append(_QUOTE_RE.match(lines[i]).group(1))
                i += 1
            qtext = _inline(" ".join(x.strip() for x in body if x.strip()), b.notes)
            if qtext:
                b.callout("quote", "", qtext)
            continue
        bullet = _BULLET_RE.match(line)
        number = _NUMBER_RE.match(line)
        if bullet or number:
            flush_para()
            m = bullet or number
            indent = len(m.group(1).replace("\t", "    "))
            kind = number is not None
            if list_kind is None:
                list_kind, list_base_indent = kind, indent
            elif indent <= list_base_indent and kind != list_kind:
                flush_list()
                list_kind, list_base_indent = kind, indent
            item = m.group(3).strip()
            if indent > list_base_indent + 1 and list_items:
                if list_kind:
                    # A nested item under a NUMBERED item joins its parent, so
                    # the numbering of the items after it does not shift.
                    list_items[-1] = list_items[-1] + " – " + item
                else:
                    list_items.append("– " + item)
            else:
                list_items.append(item)
            i += 1
            continue
        if list_kind is not None and line.startswith((" ", "\t")) and list_items:
            # A wrapped continuation of the last item.
            list_items[-1] = list_items[-1] + " " + stripped
            i += 1
            continue
        flush_list()
        para.append(line)
        i += 1
    flush_para(); flush_list()

    title = (b.title or title_hint or "").strip()
    if not title:
        first = next((getattr(x, "text", "") for x in b.blocks if isinstance(x, (S.Heading, S.Paragraph))), "")
        title = re.split(r"(?<=[.!?])\s", first.strip(), 1)[0] if first else "Document"
    if len(title) > _TITLE_MAX:
        b.notes.append("The title was shortened to 120 characters; the full line is kept as the first paragraph.")
        b.blocks.insert(0, S.Paragraph(text=title))
        title = title[:_TITLE_MAX - 1].rstrip() + "…"
    # "A first H1 identical to the title is dropped" (already taken as title);
    # a leading H2 that repeats the title is dropped too.
    if b.blocks and isinstance(b.blocks[0], S.Heading) and b.blocks[0].text.strip().casefold() == title.strip().casefold():
        b.blocks.pop(0)
    if not b.blocks:
        b.blocks.append(S.Paragraph(text=title))
    b.compact()
    doc = S.DocumentSpec(title=title, blocks=b.blocks)
    return doc, list(dict.fromkeys(b.notes))


def docx_to_document(path: str, *, title_hint: str = "") -> Tuple[S.DocumentSpec, List[str]]:
    """An uploaded .docx → a DocumentSpec from its TEXT AND TABLES ONLY.

    The zip caps run first (an .docx is a zip). Paragraph styles decide the
    block: Title, Heading 1-9, List Bullet/Number (and any style whose name
    says "List"), everything else a paragraph. Tables keep every cell's text.
    Nothing else of the file — images, fields, relationships, embedded
    objects, headers/footers — is read."""
    from ..core import archive

    archive.check_zip_container(str(path), label="document")
    import docx  # python-docx
    from docx.table import Table as _DocxTable
    from docx.text.paragraph import Paragraph as _DocxParagraph

    document = docx.Document(str(path))
    body = document.element.body
    lines: List[str] = []
    notes: List[str] = []
    title = ""

    def esc(s: str) -> str:
        return s.replace("|", "\\|").replace("\n", " ").strip()

    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = _DocxParagraph(child, document)
            text = (p.text or "").strip()
            if not text:
                lines.append("")
                continue
            style = (getattr(p.style, "name", "") or "").strip()
            low = style.lower()
            # A leading marker is escaped so a Normal paragraph stays a
            # paragraph. A number is escaped at its dot ("1\\. "): a backslash
            # before the digit is not a markdown escape and leaked into the
            # document as "\\1." (verifier 2026-09-15).
            text_md = re.sub(r"^([#>*+\-])(\s)", r"\\\1\2", text)
            text_md = re.sub(r"^(\d+)([.)])(\s)", r"\1\\\2\3", text_md)
            if low == "title" and not title:
                title = text
                continue
            m = re.match(r"heading\s*(\d)", low)
            if m:
                lines.extend(["", "#" * min(3, max(1, int(m.group(1)))) + " " + text, ""])
                continue
            if low == "subtitle":
                lines.extend(["", text_md, ""])
                continue
            if "list" in low:
                marker = "1." if "number" in low else "-"
                lines.append(f"{marker} {text}")
                continue
            lines.extend([text_md, ""])
        elif tag == "tbl":
            t = _DocxTable(child, document)
            grid: List[List[str]] = []
            for row in t.rows:
                seen: List[Any] = []
                cells: List[str] = []
                for cell in row.cells:
                    if any(cell._tc is s for s in seen):
                        # A horizontally merged cell repeats once per grid
                        # column; its text is kept once and the column stays
                        # (blank), so the cells after it stay under their own
                        # headings (verifier 2026-09-15: they shifted left).
                        cells.append("")
                        continue
                    seen.append(cell._tc)
                    cells.append(esc(cell.text))
                grid.append(cells)
            if not grid:
                continue
            lines.append("")
            lines.append("| " + " | ".join(grid[0]) + " |")
            lines.append("|" + "---|" * max(1, len(grid[0])))
            for r in grid[1:]:
                lines.append("| " + " | ".join(r) + " |")
            lines.append("")
    if title:
        # The Title paragraph leads, so the first "#" is the title and the
        # document's own Heading 1s stay headings.
        lines = ["# " + title, ""] + lines
    md = "\n".join(lines)
    doc, more = markdown_to_document(md, title_hint=title_hint)
    return doc, notes + more


__all__ = ["markdown_to_document", "docx_to_document"]
