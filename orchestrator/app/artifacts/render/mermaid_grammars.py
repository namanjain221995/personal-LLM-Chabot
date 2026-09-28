"""Readers for the mermaid families a chat answer can carry into a document.

ONE READER PER GRAMMAR. `diagrams.parse_mermaid` reads flowcharts. This
module reads the other five families DIAGRAM_INSTRUCTION teaches or the
model writes unprompted — sequenceDiagram, erDiagram, classDiagram,
stateDiagram(-v2), mindmap, timeline — each with its own grammar, because
they ARE different grammars: `A->>B: msg` is an ordered message between two
lifelines, `CUSTOMER ||--o{ ORDER : places` is a relation with a cardinality
glyph at each end, and an indented mindmap line is a child of the nearest
shallower line. None of them is a flowchart edge, and none is read by the
flowchart's rules.

THE PROMISE, unchanged from diagrams.py: a source is drawn correctly or
refused whole. Every reader returns None on the first line it does not
understand, and the caller keeps the "Diagram omitted" callout. There is no
best-effort path and no half-picture. In particular:

  * a construct mermaid draws but this module cannot lay out faithfully —
    activation bars, `critical`/`break`/`rect`/`box` frames, composite
    states, `<<choice>>`/`<<fork>>` pseudo-states, namespaces, notes on a
    class or state, mindmap icons, `cloud`/`bang` shapes — REFUSES rather
    than being dropped, because a picture missing a construct the author
    wrote is a wrong picture;
  * a line that reads as a statement but carries a piece of another
    (a second arrow inside a label, an HTML tag) refuses;
  * a keyword this module does not read at all (`gantt`, `pie`, `journey`,
    `gitGraph`, `quadrantChart`, ...) refuses at the header, and so does a
    mid-source line that is only a keyword. Before 2026-09-28 that line fell
    through to the flowchart's node rule and put a box labelled
    "classDiagram" in a person's PDF (measured in the running container:
    `classDiagram\\nA --> B\\nB --> C` drew FOUR boxes).

WHAT IS READ IS WHAT mermaid 11.17.0 READS, checked against its own parser
rather than its documentation (tests/test_mermaid_grammars.py runs
`mermaidAPI.getDiagramFromText` under node + jsdom when both are present and
compares the structure read here with the db mermaid built). Three of those
facts decided the grammar below and would have been guessed wrong:

  * a timeline splits events on EVERY colon — `1990 : 1990: WorldWideWeb`
    is a period with TWO events, "1990" and "WorldWideWeb" — which is what
    the browser shows, so it is what the paper shows;
  * a `Note over X` on an undeclared X CREATES the participant, in that
    position, so notes are read in order with the messages;
  * a class method is displayed as `+name(params) : ReturnType`, with a
    generic `~T~` shown as `<T>`.

NOTHING IS EXECUTED AND NOTHING IS FETCHED. These are regular expressions
over closed grammars; `%%{init}%%` and every `style`/`classDef`/`click`/
`link` directive refuse; labels are carried as text and escaped by the
renderers. A `<br/>` in a label is the one HTML mermaid renders as a line
break and is read as one here; any other tag refuses.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Tuple

#: Every diagram keyword mermaid 11.17.0 detects, so an unhandled one refuses
#: at the header instead of becoming a node. Longer forms precede their
#: prefixes because the alternation is ordered.
MERMAID_KEYWORDS: Tuple[str, ...] = (
    "flowchart-elk", "flowchart", "graph", "sequenceDiagram", "classDiagram-v2", "classDiagram",
    "stateDiagram-v2", "stateDiagram", "erDiagram", "mindmap", "timeline", "gantt", "pie", "journey",
    "gitGraph", "quadrantChart", "requirementDiagram", "requirement", "C4Context", "C4Container",
    "C4Component", "C4Dynamic", "C4Deployment", "sankey-beta", "sankey", "xychart-beta", "xychart",
    "block-beta", "block", "packet-beta", "packet", "kanban", "architecture-beta", "architecture", "zenuml",
    "radar-beta", "treemap-beta", "treemap", "info", "swimlane-beta", "treeView-beta", "venn-beta",
    "wardley-beta", "cynefin-beta", "eventmodeling", "ishikawa-beta", "ishikawa", "railroad-ebnf-beta",
    "railroad-abnf-beta", "railroad-peg-beta", "railroad-beta",
)
_KEYWORD_RE = re.compile(r"^(?P<kw>" + "|".join(re.escape(k) for k in MERMAID_KEYWORDS) + r")(?![\w-])", re.IGNORECASE)
_KEYWORDS_LOWER = frozenset(k.lower() for k in MERMAID_KEYWORDS)

#: The families this module reads, by the lowercase header keyword.
FAMILY_OF_KEYWORD: Dict[str, str] = {
    "sequencediagram": "sequence",
    "erdiagram": "er",
    "classdiagram": "class",
    "classdiagram-v2": "class",
    "statediagram": "state",
    "statediagram-v2": "state",
    "mindmap": "mindmap",
    "timeline": "timeline",
    "journey": "journey",
    "kanban": "kanban",
    "packet-beta": "packet",
    "packet": "packet",
}

#: TYPES THAT ARE REALLY CHARTS. These six draw NUMBERS: a pie's shares, an
#: xychart's series, a radar's scores, a sankey's flows, a quadrant's
#: coordinates, a treemap's sizes. In a document a chart is drawn by
#: render/charts.py from a bound table — code computes the values, the model
#: chooses the type — and a mermaid fence carries only numbers the model
#: typed. Reproducing them as a picture would present invented values as a
#: chart of data, which is the owner's "flow chart" complaint from the other
#: side. They refuse to a callout that says so (md_import), and
#: DIAGRAM_INSTRUCTION tells the model where numbers go.
CHART_KEYWORDS: Dict[str, str] = {
    "pie": "pie", "xychart-beta": "xychart", "xychart": "xychart", "radar-beta": "radar",
    "sankey-beta": "sankey", "sankey": "sankey", "quadrantchart": "quadrant-chart",
    "treemap-beta": "treemap", "treemap": "treemap",
}

#: EXCLUDED, EACH WITH ITS REASON. A keyword here refuses at the header to
#: the plain "Diagram omitted" callout. The reason is the argument for why a
#: faithful drawing is not on offer today; none is "not yet". Keys are the
#: grammar names mermaid registers (renderer variants folded).
EXCLUDED_REASONS: Dict[str, str] = {
    "architecture": "groups nest and edges attach to a named SIDE of an icon (L/R/T/B); the icon is the meaning "
                    "and the layout is the grammar, so a boxes-and-lines rendering would be a different picture",
    "block": "a grid grammar (columns, spans, nested blocks, space) whose meaning is the cell geometry; "
             "reproducing it needs the grid solver, and a Sugiyama layout of the same blocks is a wrong picture",
    "c4": "Person/System/Container/Component inside nested Boundary blocks with Rel arguments up to six deep; "
          "the nesting is the meaning and cannot be flattened to boxes without drawing a different model",
    "cynefin": "a fixed four-domain frame whose statement grammar is not verifiable on this box (no DOM for "
               "mermaid's parser); an unverified grammar is a guess, and a guess is refused",
    "eventmodeling": "swimlanes of commands, events and views with typed connectors; the lane geometry is "
                     "the meaning and the grammar is new in 11.x and unverified here",
    "gantt": "date arithmetic: `after a b`, durations in d/w/h, `dateFormat` in dayjs tokens, `excludes weekends`, "
             "`tickInterval`; a bar placed one day wrong is a wrong schedule, and a schedule is the whole picture",
    "git": "branch lanes, merges, cherry-picks and commit ordering are a layout engine of their own; a merge "
           "drawn into the wrong lane is a wrong history",
    "info": "prints the mermaid version; it has no content to draw in a document",
    "ishikawa": "a fishbone has a fixed spine-and-rib geometry; the same tree drawn as boxes is a different picture",
    "railroad": "syntax diagrams from EBNF/ABNF/PEG need the railroad layout; a grammar drawn as a graph is wrong",
    "requirement": "typed blocks with six relation kinds (contains, copies, derives, satisfies, verifies, refines, "
                   "traces) whose arrowheads carry the meaning; not drawn until each head is",
    "swimlanes": "new in 11.x, grammar unverified on this box; refused rather than guessed",
    "treeView": "a file-tree outline; the grammar is new in 11.x and unverified on this box",
    "venn": "two or three sets draw as circles; four or more cannot be drawn faithfully with circles at all, "
            "and the areas are numbers the model typed",
    "wardley": "components at [visibility, evolution] coordinates: positions are numbers the model typed",
}

#: The grammar each excluded keyword belongs to.
EXCLUDED_OF_KEYWORD: Dict[str, str] = {
    "architecture-beta": "architecture", "architecture": "architecture", "block-beta": "block", "block": "block",
    "c4context": "c4", "c4container": "c4", "c4component": "c4", "c4dynamic": "c4", "c4deployment": "c4",
    "cynefin-beta": "cynefin", "eventmodeling": "eventmodeling", "gantt": "gantt", "gitgraph": "git", "info": "info",
    "ishikawa-beta": "ishikawa", "ishikawa": "ishikawa", "railroad-beta": "railroad", "railroad-ebnf-beta": "railroad",
    "railroad-abnf-beta": "railroad", "railroad-peg-beta": "railroad", "requirementdiagram": "requirement",
    "requirement": "requirement", "swimlane-beta": "swimlanes", "treeview-beta": "treeView", "venn-beta": "venn",
    "wardley-beta": "wardley", "zenuml": "sequence",
}


def header_kind(source: str) -> Tuple[str, str]:
    """(kind, name) for a source's first statement.

    kind is "flowchart" (or headerless, which the flowchart reader owns),
    "family" with the family read here, "chart" with the chart type refused
    as a chart, "excluded" with the grammar's name, or "unknown" when the
    first statement is no mermaid header at all.
    """
    lines = (source or "").splitlines()
    i = 0
    if lines and lines[0].strip() == "---":
        end = next((k for k in range(1, len(lines)) if lines[k].strip() == "---"), None)
        i = 0 if end is None else end + 1
    first = next((l.strip().rstrip(";") for l in lines[i:] if l.strip() and not l.strip().startswith("%%")), "")
    kw = header_keyword(first)
    if kw is None:
        return ("flowchart", "flowchart") if not first or _DECLARATION_LIKE.match(first) else ("unknown", "")
    if kw in ("flowchart", "graph", "flowchart-elk"):
        return ("flowchart", "flowchart")
    if kw in FAMILY_OF_KEYWORD:
        return ("family", FAMILY_OF_KEYWORD[kw])
    if kw in CHART_KEYWORDS:
        return ("chart", CHART_KEYWORDS[kw])
    return ("excluded", EXCLUDED_OF_KEYWORD.get(kw, kw))


_DECLARATION_LIKE = re.compile(r"^[A-Za-z_]")

_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[A-Za-z/!][^>]*>")
_ID = r"[A-Za-z_][\w-]{0,39}"
#: mermaid's own actor/state id class: anything but the characters a link,
#: a label or a statement separator is made of.
_LOOSE_ID = r"[^\s\"'`<>|;:,+\-\[\]{}()][^\"'`<>|;:,+\[\]{}()]{0,47}?"


def header_keyword(line: str) -> Optional[str]:
    """The lowercase mermaid keyword a stripped line starts with, or None."""
    m = _KEYWORD_RE.match(line)
    return m.group("kw").lower() if m else None


def is_keyword(word: str) -> bool:
    return word.lower() in _KEYWORDS_LOWER


def _text(raw: Optional[str]) -> Optional[str]:
    """A label as mermaid would show it, or None when it holds markup.

    `<br/>` becomes a newline; any other tag refuses (an HTML label is not
    text, and the browser's sanitiser is not here to make it safe).
    """
    if raw is None:
        return ""
    s = _BR_RE.sub("\n", raw)
    if _TAG_RE.search(s):
        return None
    s = "\n".join(part.strip() for part in s.split("\n"))
    return s.strip()


def _lines(source: str) -> Optional[List[str]]:
    """Statement lines: stripped, comments dropped, `%%{init}%%` refused."""
    out: List[str] = []
    for raw in source.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("%%"):
            if line.startswith("%%{"):
                return None
            continue
        out.append(raw.rstrip())
    return out


# ------------------------------------------------------------- sequence --

_SEQ_PARTICIPANT_RE = re.compile(rf"^(?P<kind>participant|actor)\s+(?P<id>{_LOOSE_ID})\s*(?:\s+as\s+(?P<alias>.+?))?\s*$")
#: Longest arrow first: `-->>` before `-->`, `->>` before `->`.
_SEQ_ARROW = r"(?P<arrow><<-->>|<<->>|-->>|->>|--x|-x|--\)|-\)|-->|->)"
_SEQ_MESSAGE_RE = re.compile(rf"^(?P<a>{_LOOSE_ID})\s*{_SEQ_ARROW}\s*(?P<act>[+-])?\s*(?P<b>{_LOOSE_ID})\s*:\s*(?P<text>.*)$")
_SEQ_NOTE_RE = re.compile(rf"^[Nn]ote\s+(?P<pos>left of|right of|over)\s+(?P<ids>[^:]+?)\s*:\s*(?P<text>.*)$")
_SEQ_FRAME_RE = re.compile(r"^(?P<kw>loop|opt|alt|par|critical|break|rect|box|else|and|option|end)(?:\s+(?P<text>.*))?$")
_SEQ_REFUSED_RE = re.compile(r"^(activate|deactivate|links?|properties|details|create|destroy|accTitle|accDescr)\b", re.IGNORECASE)
_SEQ_ARROWS: Dict[str, Tuple[str, str]] = {
    "->": ("solid", "none"), "-->": ("dashed", "none"),
    "->>": ("solid", "filled"), "-->>": ("dashed", "filled"),
    "-x": ("solid", "cross"), "--x": ("dashed", "cross"),
    "-)": ("solid", "open"), "--)": ("dashed", "open"),
    "<<->>": ("solid", "both"), "<<-->>": ("dashed", "both"),
}


def parse_sequence(lines: List[str]) -> Optional[Dict[str, Any]]:
    participants: List[Dict[str, Any]] = []
    known: Dict[str, int] = {}
    steps: List[Dict[str, Any]] = []
    title = ""
    autonumber = False
    depth: List[str] = []

    def declare(pid: str, label: Optional[str] = None, actor: bool = False) -> bool:
        pid = pid.strip()
        if not pid:
            return False
        if pid in known:
            return True
        known[pid] = len(participants)
        participants.append({"id": pid, "label": (label or pid).strip(), "actor": actor})
        return True

    for raw in lines:
        line = raw.strip()
        if line.lower() == "autonumber":
            autonumber = True
            continue
        if line.lower().startswith("autonumber"):
            return None  # `autonumber 10 2`, `autonumber off`: not read here
        if line.lower().startswith("title "):
            t = _text(line[6:])
            if t is None or "\n" in t:
                return None
            title = t
            continue
        if _SEQ_REFUSED_RE.match(line):
            return None
        m = _SEQ_PARTICIPANT_RE.match(line)
        if m:
            # `participant S` has no alias and is the common form; only an
            # alias that IS written and cannot be read refuses. (Until
            # 2026-09-28 a missing alias refused too, so every sequence
            # written without `as` was lost; tests/test_mermaid_grammars.py.)
            alias = None
            if m.group("alias"):
                alias = _text(m.group("alias"))
                if not alias or "\n" in alias:
                    return None
            pid = m.group("id").strip()
            if pid in known:
                return None  # declared twice: mermaid keeps the first, we do not guess
            if not declare(pid, alias or None, m.group("kind") == "actor"):
                return None
            continue
        m = _SEQ_FRAME_RE.match(line)
        if m:
            kw, text = m.group("kw"), _text(m.group("text") or "")
            if text is None or "\n" in text:
                return None
            if kw in ("loop", "opt", "alt", "par"):
                depth.append(kw)
                steps.append({"kind": "frame_open", "frame": kw, "text": text})
            elif kw in ("else", "and"):
                if not depth or (kw == "else" and depth[-1] != "alt") or (kw == "and" and depth[-1] != "par"):
                    return None
                steps.append({"kind": "frame_divide", "text": text})
            elif kw == "end":
                if not depth:
                    return None
                depth.pop()
                steps.append({"kind": "frame_close"})
            else:
                return None  # critical / break / rect / box / option: not drawn here
            continue
        m = _SEQ_NOTE_RE.match(line)
        if m:
            ids = [i.strip() for i in m.group("ids").split(",")]
            pos = m.group("pos").split()[0]
            if not ids or len(ids) > 2 or any(not i for i in ids) or (len(ids) == 2 and pos != "over"):
                return None
            text = _text(m.group("text"))
            if not text:
                return None
            for i in ids:
                if not re.fullmatch(_LOOSE_ID, i) or not declare(i):
                    return None
            steps.append({"kind": "note", "position": pos, "ids": ids, "text": text})
            continue
        m = _SEQ_MESSAGE_RE.match(line)
        if m:
            if m.group("act"):
                return None  # activation bars are not drawn here; refuse, never drop
            text = _text(m.group("text"))
            if text is None:
                return None
            a, b = m.group("a").strip(), m.group("b").strip()
            if not declare(a) or not declare(b):
                return None
            line_style, head = _SEQ_ARROWS[m.group("arrow")]
            steps.append({"kind": "message", "source": a, "target": b, "text": text, "line": line_style, "head": head})
            continue
        return None
    if depth or not participants or not any(s["kind"] == "message" for s in steps):
        return None
    return {"family": "sequence", "title": title, "participants": participants, "steps": steps, "autonumber": autonumber}


# ------------------------------------------------------------------- er --

_ER_CARD_LEFT = {"|o": "zero_or_one", "||": "exactly_one", "}o": "zero_or_more", "}|": "one_or_more"}
_ER_CARD_RIGHT = {"o|": "zero_or_one", "||": "exactly_one", "o{": "zero_or_more", "|{": "one_or_more"}
_ER_REL_RE = re.compile(
    rf"^(?P<a>{_ID})\s+(?P<lc>\|o|\|\||\}}o|\}}\|)(?P<line>--|\.\.)(?P<rc>o\||\|\||o\{{|\|\{{)\s+(?P<b>{_ID})\s*:\s*(?P<label>\"[^\"]{{0,48}}\"|[^\s\"]{{1,48}})\s*$"
)
_ER_ENTITY_OPEN_RE = re.compile(rf"^(?P<id>{_ID})\s*\{{\s*$")
_ER_ATTR_RE = re.compile(
    r"^(?P<type>[A-Za-z_][\w\[\]\(\)~,]{0,23})\s+(?P<name>[A-Za-z_][\w-]{0,39})"
    r"(?:\s+(?P<keys>(?:PK|FK|UK)(?:\s*,\s*(?:PK|FK|UK)){0,2}))?(?:\s+\"(?P<comment>[^\"]{0,60})\")?\s*$"
)
_ER_DECL_RE = re.compile(rf"^{_ID}$")
_DIRECTION_RE = re.compile(r"^direction\s+(TD|TB|LR|RL|BT)$", re.IGNORECASE)


def parse_er(lines: List[str]) -> Optional[Dict[str, Any]]:
    entities: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    relations: List[Dict[str, Any]] = []
    direction = "TD"

    def entity(eid: str) -> Dict[str, Any]:
        if eid not in entities:
            entities[eid] = {"id": eid, "label": eid, "attributes": []}
            order.append(eid)
        return entities[eid]

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        m = _DIRECTION_RE.match(line)
        if m:
            direction = "LR" if m.group(1).upper() in ("LR", "RL") else "TD"
            continue
        m = _ER_REL_RE.match(line)
        if m:
            label = m.group("label").strip('"').strip()
            label = _text(label)
            if label is None or "\n" in label:
                return None
            a, b = m.group("a"), m.group("b")
            if is_keyword(a) or is_keyword(b):
                return None
            entity(a)
            entity(b)
            relations.append({
                "source": a, "target": b,
                "source_card": _ER_CARD_LEFT[m.group("lc")], "target_card": _ER_CARD_RIGHT[m.group("rc")],
                "identifying": m.group("line") == "--", "label": label,
            })
            continue
        m = _ER_ENTITY_OPEN_RE.match(line)
        if m:
            eid = m.group("id")
            if is_keyword(eid):
                return None
            ent = entity(eid)
            closed = False
            while i < len(lines):
                inner = lines[i].strip()
                i += 1
                if inner == "}":
                    closed = True
                    break
                am = _ER_ATTR_RE.match(inner)
                if not am:
                    return None
                keys = ", ".join(k.strip() for k in (am.group("keys") or "").split(",") if k.strip())
                ent["attributes"].append({"type": am.group("type"), "name": am.group("name"), "keys": keys,
                                          "comment": (am.group("comment") or "").strip()})
            if not closed:
                return None
            continue
        if _ER_DECL_RE.match(line):
            if is_keyword(line):
                return None
            entity(line)
            continue
        return None
    if not order:
        return None
    return {"family": "er", "direction": direction, "entities": [entities[k] for k in order], "relations": relations}


# ---------------------------------------------------------------- class --

_CLASS_OPEN_RE = re.compile(rf"^class\s+(?P<id>{_ID})(?:\s*\[\"(?P<label>[^\"]{{1,48}})\"\])?\s*(?P<brace>\{{)?\s*$")
_CLASS_MEMBER_LINE_RE = re.compile(rf"^(?P<id>{_ID})\s*:\s*(?P<member>.+?)\s*$")
_CLASS_ANNOTATION_RE = re.compile(rf"^<<(?P<ann>[^<>]{{1,24}})>>\s*(?P<id>{_ID})?\s*$")
_CLASS_HEADS_LEFT = {"<|": "inheritance", "*": "composition", "o": "aggregation", "<": "arrow"}
_CLASS_HEADS_RIGHT = {"|>": "inheritance", "*": "composition", "o": "aggregation", ">": "arrow"}
_CLASS_REL_RE = re.compile(
    rf"^(?P<a>{_ID})(?:\s+\"(?P<ca>[^\"]{{0,12}})\")?\s*(?P<lh><\||\*|o|<)?(?P<line>--|\.\.)(?P<rh>\|>|\*|o|>)?"
    rf"(?:\s*\"(?P<cb>[^\"]{{0,12}})\")?\s+(?P<b>{_ID})\s*(?::\s*(?P<label>.*?))?\s*$"
)
_CLASS_REFUSED_RE = re.compile(r"^(namespace|note|link|click|callback|cssClass|style|classDef|accTitle|accDescr)\b", re.IGNORECASE)
_GENERIC_RE = re.compile(r"~([^~]*)~")
_METHOD_RE = re.compile(r"^(?P<vis>[+\-#~])?\s*(?P<name>[^()]+?)\s*\((?P<params>.*)\)\s*(?P<cls>[*$])?\s*(?P<ret>.*?)\s*$")
_ATTR_RE = re.compile(r"^(?P<vis>[+\-#~])?\s*(?P<body>.+?)\s*(?P<cls>[*$])?$")


def _class_member(text: str) -> Optional[Tuple[str, str]]:
    """("attributes"|"methods", display text) for one member as mermaid
    shows it, or None when the member cannot be read."""
    text = text.strip()
    if not text or "<" in text or ">" in text or '"' in text:
        return None
    text = _GENERIC_RE.sub(r"<\1>", text)
    if "~" in text:
        return None
    if "(" in text:
        m = _METHOD_RE.match(text)
        if not m or ")" not in text:
            return None
        vis = m.group("vis") or ""
        ret = m.group("ret")
        shown = f"{vis}{m.group('name')}({m.group('params')})" + (f" : {ret}" if ret else "")
        return ("methods", shown)
    m = _ATTR_RE.match(text)
    if not m or ")" in text:
        return None
    return ("attributes", f"{m.group('vis') or ''}{m.group('body')}")


def parse_class(lines: List[str]) -> Optional[Dict[str, Any]]:
    classes: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    relations: List[Dict[str, Any]] = []
    direction = "TD"

    def cls(cid: str) -> Optional[Dict[str, Any]]:
        if is_keyword(cid):
            return None
        if cid not in classes:
            classes[cid] = {"id": cid, "label": cid, "annotation": "", "attributes": [], "methods": []}
            order.append(cid)
        return classes[cid]

    def add_member(c: Dict[str, Any], text: str) -> bool:
        if text.startswith("<<") and text.endswith(">>"):
            ann = text[2:-2].strip()
            if not ann or len(ann) > 24 or c["annotation"]:
                return False
            c["annotation"] = ann
            return True
        got = _class_member(text)
        if got is None:
            return False
        c[got[0]].append(got[1])
        return True

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        m = _DIRECTION_RE.match(line)
        if m:
            direction = "LR" if m.group(1).upper() in ("LR", "RL") else "TD"
            continue
        if _CLASS_REFUSED_RE.match(line) or ":::" in line:
            return None
        m = _CLASS_OPEN_RE.match(line)
        if m:
            c = cls(m.group("id"))
            if c is None:
                return None
            if m.group("label"):
                c["label"] = m.group("label").strip()
            if m.group("brace"):
                closed = False
                while i < len(lines):
                    inner = lines[i].strip()
                    i += 1
                    if inner == "}":
                        closed = True
                        break
                    if not add_member(c, inner):
                        return None
                if not closed:
                    return None
            continue
        m = _CLASS_ANNOTATION_RE.match(line)
        if m:
            if not m.group("id"):
                return None
            c = cls(m.group("id"))
            if c is None or not add_member(c, f"<<{m.group('ann')}>>"):
                return None
            continue
        m = _CLASS_REL_RE.match(line)
        if m:
            a, b = cls(m.group("a")), cls(m.group("b"))
            if a is None or b is None:
                return None
            label = _text(m.group("label") or "")
            if label is None or "\n" in label:
                return None
            relations.append({
                "source": m.group("a"), "target": m.group("b"),
                "line": "solid" if m.group("line") == "--" else "dashed",
                "source_head": _CLASS_HEADS_LEFT.get(m.group("lh") or "", "none"),
                "target_head": _CLASS_HEADS_RIGHT.get(m.group("rh") or "", "none"),
                "source_card": (m.group("ca") or "").strip(), "target_card": (m.group("cb") or "").strip(),
                "label": label,
            })
            continue
        m = _CLASS_MEMBER_LINE_RE.match(line)
        if m:
            c = cls(m.group("id"))
            if c is None or not add_member(c, m.group("member")):
                return None
            continue
        return None
    if not order:
        return None
    return {"family": "class", "direction": direction, "classes": [classes[k] for k in order], "relations": relations}


# ---------------------------------------------------------------- state --

_STATE_END_ID = r"\[\*\]"
_STATE_TRANS_RE = re.compile(rf"^(?P<a>{_STATE_END_ID}|{_ID})\s*-->\s*(?P<b>{_STATE_END_ID}|{_ID})\s*(?::\s*(?P<label>.*?))?\s*$")
_STATE_ALIAS_RE = re.compile(rf"^state\s+\"(?P<label>[^\"]{{1,48}})\"\s+as\s+(?P<id>{_ID})\s*$")
_STATE_DECL_RE = re.compile(rf"^state\s+(?P<id>{_ID})\s*$")
_STATE_DESC_RE = re.compile(rf"^(?P<id>{_ID})\s*:\s*(?P<text>.+?)\s*$")
_STATE_REFUSED_RE = re.compile(r"^(note|classDef|class|style|accTitle|accDescr)\b|<<|^--\s*$|\{", re.IGNORECASE)


def parse_state(lines: List[str]) -> Optional[Dict[str, Any]]:
    from ..spec import STATE_END, STATE_START

    states: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    transitions: List[Dict[str, Any]] = []
    direction = "TD"

    def state(sid: str) -> Optional[Dict[str, Any]]:
        if is_keyword(sid):
            return None
        if sid not in states:
            states[sid] = {"id": sid, "label": sid, "lines": []}
            order.append(sid)
        return states[sid]

    for raw in lines:
        line = raw.strip()
        m = _DIRECTION_RE.match(line)
        if m:
            direction = "LR" if m.group(1).upper() in ("LR", "RL") else "TD"
            continue
        if _STATE_REFUSED_RE.search(line) or ":::" in line:
            return None
        m = _STATE_TRANS_RE.match(line)
        if m:
            a, b = m.group("a"), m.group("b")
            if a == "[*]" and b == "[*]":
                return None
            label = _text(m.group("label") or "")
            if label is None or "\n" in label:
                return None
            for sid in (a, b):
                if sid != "[*]" and state(sid) is None:
                    return None
            transitions.append({"source": STATE_START if a == "[*]" else a,
                                "target": STATE_END if b == "[*]" else b, "label": label})
            continue
        m = _STATE_ALIAS_RE.match(line)
        if m:
            s = state(m.group("id"))
            if s is None:
                return None
            s["label"] = m.group("label").strip()
            continue
        m = _STATE_DECL_RE.match(line)
        if m:
            if state(m.group("id")) is None:
                return None
            continue
        m = _STATE_DESC_RE.match(line)
        if m:
            s = state(m.group("id"))
            text = _text(m.group("text"))
            if s is None or not text or "\n" in text:
                return None
            s["lines"].append(text)
            continue
        return None
    if not order or not transitions:
        return None
    return {"family": "state", "direction": direction, "states": [states[k] for k in order], "transitions": transitions}


# -------------------------------------------------------------- mindmap --

_MIND_SHAPES: Tuple[Tuple[str, str, str], ...] = (
    ("))", "((", "bang"), ("((", "))", "circle"), (")", "(", "cloud"),
    ("{{", "}}", "hexagon"), ("[", "]", "square"), ("(", ")", "rounded"),
)
_MIND_LINE_RE = re.compile(rf"^(?P<id>{_ID})?(?P<rest>.*)$")


def _mind_node(text: str) -> Optional[Tuple[str, str, str]]:
    """(id, label, shape) for one mindmap line's text, or None."""
    if "::" in text or ":::" in text:
        return None  # `::icon(...)` and `:::class` are not drawn here
    m = _MIND_LINE_RE.match(text)
    if not m:
        return None
    nid, rest = m.group("id") or "", m.group("rest")
    if not rest:
        return (nid, nid, "default") if nid else None
    for open_, close, shape in _MIND_SHAPES:
        if rest.startswith(open_) and rest.endswith(close) and len(rest) >= len(open_) + len(close):
            if shape in ("bang", "cloud"):
                return None
            label = _text(rest[len(open_):-len(close)])
            if not label:
                return None
            return (nid or label, label, shape)
    if nid and not any(ch in rest for ch in "()[]{}"):
        label = _text(nid + rest)
        return (label, label, "default") if label else None
    return None


_MIND_ID_BAD_RE = re.compile(r"[^\w-]+")


def _safe_mind_id(text: str) -> str:
    """An identifier `spec.MindNode` accepts, from any line of text. Only the
    shape of the id changes; the label it came from is kept as written."""
    out = _MIND_ID_BAD_RE.sub("_", text).strip("_")
    if not out or not out[0].isalpha():
        out = "n" + out
    return out[:36] or "n"


def parse_mindmap(lines: List[str]) -> Optional[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    stack: List[Tuple[int, str]] = []
    ids_seen: Dict[str, int] = {}
    for raw in lines:
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            return None
        indent = len(raw) - len(raw.lstrip(" "))
        got = _mind_node(raw.strip())
        if got is None:
            return None
        nid, label, shape = got
        # A PLAIN LINE'S ID IS ITS TEXT, and spec.MindNode wants an identifier:
        # it must start with a letter and hold only letters, digits, `_` and
        # `-`, and it caps at 40 while a label may be 48. The engine writes
        # sentences ("Create social media teasers to build anticipation."), so
        # a 17-node mindmap of plain lines refused as a whole -- 59 validation
        # errors, one per space and full stop. The id is INTERNAL, a parent
        # reference nobody reads, so it is made safe here and kept unique by
        # the suffix below; THE LABEL IS NEVER TOUCHED.
        nid = _safe_mind_id(nid)
        if nid in ids_seen:
            ids_seen[nid] += 1
            nid = f"{nid}__{ids_seen[nid]}"
        else:
            ids_seen[nid] = 0
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if nodes and not stack:
            return None  # a second root: mermaid refuses this too
        parent = stack[-1][1] if stack else None
        nodes.append({"id": nid, "label": label, "parent": parent, "shape": shape})
        stack.append((indent, nid))
    if len(nodes) < 2:
        return None
    return {"family": "mindmap", "nodes": nodes}


# ------------------------------------------------------------- timeline --

_TL_TITLE_RE = re.compile(r"^title\s+(?P<text>.+?)\s*$")
_TL_SECTION_RE = re.compile(r"^section\s+(?P<text>.+?)\s*$")


def parse_timeline(lines: List[str]) -> Optional[Dict[str, Any]]:
    periods: List[Dict[str, Any]] = []
    title = ""
    section = ""
    for raw in lines:
        line = raw.strip()
        m = _TL_TITLE_RE.match(line)
        if m:
            t = _text(m.group("text"))
            if t is None or "\n" in t:
                return None
            title = t
            continue
        m = _TL_SECTION_RE.match(line)
        if m:
            t = _text(m.group("text"))
            if not t or "\n" in t:
                return None
            section = t
            continue
        if line.startswith(":"):
            if not periods:
                return None
            events = [e.strip() for e in line[1:].split(":")]
            if any(not e for e in events):
                return None
            for e in events:
                t = _text(e)
                if not t or "\n" in t:
                    return None
                periods[-1]["events"].append(t)
            continue
        if is_keyword(line.split(":")[0].strip()):
            return None
        parts = [p.strip() for p in line.split(":")]
        time, events = parts[0], parts[1:]
        if not time or any(not e for e in events):
            return None
        time_t = _text(time)
        if not time_t or "\n" in time_t:
            return None
        clean: List[str] = []
        for e in events:
            t = _text(e)
            if not t or "\n" in t:
                return None
            clean.append(t)
        periods.append({"time": time_t, "section": section, "events": clean})
    if not periods:
        return None
    return {"family": "timeline", "title": title, "periods": periods}


# -------------------------------------------------------------- journey --

_JN_TITLE_RE = re.compile(r"^title\s+(?P<text>.+?)\s*$")
_JN_SECTION_RE = re.compile(r"^section\s+(?P<text>.+?)\s*$")
#: `Task name: score: Actor, Actor` — the actors are optional, the score is not.
_JN_TASK_RE = re.compile(r"^(?P<name>[^:]+?)\s*:\s*(?P<score>-?\d+)\s*(?::\s*(?P<actors>.*?))?\s*$")
_JN_REFUSED_RE = re.compile(r"^(accTitle|accDescr)\b", re.IGNORECASE)


def parse_journey(lines: List[str]) -> Optional[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    title = ""
    section = ""
    for raw in lines:
        line = raw.strip()
        if _JN_REFUSED_RE.match(line):
            return None
        m = _JN_TITLE_RE.match(line)
        if m:
            t = _text(m.group("text"))
            if t is None or "\n" in t:
                return None
            title = t
            continue
        m = _JN_SECTION_RE.match(line)
        if m:
            t = _text(m.group("text"))
            if not t or "\n" in t:
                return None
            section = t
            continue
        m = _JN_TASK_RE.match(line)
        if m:
            name = _text(m.group("name"))
            if not name or "\n" in name or is_keyword(name):
                return None
            score = int(m.group("score"))
            if not 1 <= score <= 5:
                return None  # mermaid draws five faces; a sixth is not a face
            actors: List[str] = []
            for a in (m.group("actors") or "").split(","):
                a = _text(a)
                if a is None or "\n" in a:
                    return None
                if a:
                    actors.append(a)
            tasks.append({"name": name, "section": section, "score": score, "actors": actors})
            continue
        return None
    if not tasks:
        return None
    return {"family": "journey", "title": title, "tasks": tasks}


# --------------------------------------------------------------- kanban --

_KB_NODE_RE = re.compile(rf"^(?:(?P<id>{_ID})\s*\[(?P<label>[^\[\]]{{1,120}})\]|(?P<bare>[^\[\]{{}}()@]{{1,120}}))\s*$")


def parse_kanban(lines: List[str]) -> Optional[Dict[str, Any]]:
    columns: List[Dict[str, Any]] = []
    col_indent: Optional[int] = None
    card_indent: Optional[int] = None
    ids: Dict[str, int] = {}
    for raw in lines:
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            return None
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if "@{" in line or ":::" in line:
            return None  # `@{ ticket: ..., assigned: ... }` metadata is content; not drawn, so not dropped
        m = _KB_NODE_RE.match(line)
        if not m:
            return None
        text = _text(m.group("label") if m.group("label") is not None else m.group("bare"))
        if not text or "\n" in text or is_keyword(text):
            return None
        nid = m.group("id") or text
        if col_indent is None:
            col_indent = indent
        if indent == col_indent:
            if nid in ids:
                return None
            ids[nid] = 1
            columns.append({"id": nid, "label": text, "cards": []})
            card_indent = None
            continue
        if indent < col_indent or not columns:
            return None
        if card_indent is None:
            card_indent = indent
        if indent != card_indent:
            return None  # a card under a card: mermaid has no third level
        columns[-1]["cards"].append(text)
    if not columns:
        return None
    return {"family": "kanban", "columns": columns}


# --------------------------------------------------------------- packet --

_PK_TITLE_RE = re.compile(r"^title\s+(?P<text>.+?)\s*$")
_PK_FIELD_RE = re.compile(r"^(?P<start>\d{1,4})(?:\s*-\s*(?P<end>\d{1,4}))?\s*:\s*\"(?P<label>[^\"]{1,64})\"\s*$")


def parse_packet(lines: List[str]) -> Optional[Dict[str, Any]]:
    fields: List[Dict[str, Any]] = []
    title = ""
    expect = 0
    for raw in lines:
        line = raw.strip()
        m = _PK_TITLE_RE.match(line)
        if m:
            t = _text(m.group("text"))
            if t is None or "\n" in t:
                return None
            title = t
            continue
        m = _PK_FIELD_RE.match(line)
        if m:
            start = int(m.group("start"))
            end = int(m.group("end")) if m.group("end") is not None else start
            label = _text(m.group("label"))
            if not label or "\n" in label or end < start or start != expect:
                return None  # a gap or an overlap is a parse error in mermaid too
            expect = end + 1
            fields.append({"start": start, "end": end, "label": label})
            continue
        return None  # `+N: "x"` (relative) and anything else: not read here
    if not fields:
        return None
    return {"family": "packet", "title": title, "fields": fields}


# ------------------------------------------------------------- dispatch --

READERS: Dict[str, Callable[[List[str]], Optional[Dict[str, Any]]]] = {
    "sequence": parse_sequence,
    "er": parse_er,
    "class": parse_class,
    "state": parse_state,
    "mindmap": parse_mindmap,
    "timeline": parse_timeline,
    "journey": parse_journey,
    "kanban": parse_kanban,
    "packet": parse_packet,
}


def read_family(family: str, source_lines: List[str]) -> Optional[Dict[str, Any]]:
    """Read `source_lines` (the header already removed) as `family`, or None.

    Every reader returns a dict for the matching `spec.*Diagram` model or
    None; the spec's own validation is the second gate, and a dict it refuses
    is also None to the caller (md_import treats an exception there as a
    bug worth a log line, so the readers keep to the spec's caps on their
    own where a cap is a refusal rather than an error).
    """
    reader = READERS.get(family)
    if reader is None:
        return None
    lines = _lines("\n".join(source_lines))
    if lines is None:
        return None
    return reader(lines)


__all__ = ["MERMAID_KEYWORDS", "FAMILY_OF_KEYWORD", "CHART_KEYWORDS", "EXCLUDED_REASONS", "EXCLUDED_OF_KEYWORD",
           "READERS", "header_keyword", "header_kind", "is_keyword", "read_family",
           "parse_sequence", "parse_er", "parse_class", "parse_state", "parse_mindmap", "parse_timeline",
           "parse_journey", "parse_kanban", "parse_packet"]
