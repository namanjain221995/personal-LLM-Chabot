"""Score one answer against the checklist. Markdown in, Score out.

Every check returns what it OBSERVED, not just a boolean, so a failure
names the number that failed and a builder can argue with it.
"""
from __future__ import annotations

import re
from typing import Dict, List, Tuple

from . import checklist as K
from .checklist import Result, Score
from .normalise import DIAGRAM_ROLES


# ------------------------------------------------------------- the parser --

FENCE_RE = re.compile(r"^(?P<ticks>`{3,}|~{3,})[ \t]*(?P<lang>[A-Za-z0-9_+-]*)[ \t]*$")
HEADING_RE = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*#*\s*$")
SETEXT_H1_RE = re.compile(r"^=+\s*$")
SETEXT_H2_RE = re.compile(r"^-{2,}\s*$")
BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+\S")
NUMBERED_RE = re.compile(r"^\s{0,3}\d+[.)]\s+\S")
TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-{2,}[\s:|-]*\|?\s*$")
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__", re.S)
QUOTE_RE = re.compile(r"^\s{0,3}>")

#: Fence languages that are a DIAGRAM, not code. `text`/`plain` count only
#: when the fence actually draws boxes -- a plain fence of prose is not a
#: diagram (checked in `_is_ascii_diagram`).
DIAGRAM_LANGS = {"mermaid", "plantuml", "graphviz", "dot", "d2"}
BOX_CHARS = set("─│┌┐└┘├┤┬┴┼"
                "═║╔╗╚╝╠╣╦╩╬"
                "→←↑↓▶▼◀▲+|-")

#: Languages that are never source code even in a fence.
NON_CODE_LANGS = DIAGRAM_LANGS | {"", "text", "plain", "txt", "diagram", "ascii"}

#: A mermaid node carrying a ROLE class: `A["Label"]:::store`.
#:
#: THIS REPLACED A COLOUR-DIRECTIVE CHECK, and the replacement is the point.
#: The old regex matched `style|classDef|linkStyle|fill:|stroke:|%%{init` --
#: the directives that put colour in a diagram by hand. That check is
#: unpassable by construction after this release: the prompt forbids the
#: model to write any of them, and the sanitiser strips them if it does. A
#: check no correct answer can pass is not a bar, it is a permanent red mark
#: that teaches people to ignore the scorer.
#:
#: What replaces it is what a Markdown scorer can actually see. Colour is now
#: the renderer's job, driven off a per-node role class, and this regex asks
#: the question the scorer is entitled to ask: did the answer tag its nodes
#: so the renderer has something to colour FROM. The colour itself is
#: asserted where colour exists -- the renderer's tests and the frontend's
#: token tests -- and deliberately not here, because a Markdown scorer cannot
#: see a colour and should not pretend to.
DIAGRAM_ROLE_RE = re.compile(
    r":::\s*(" + "|".join(re.escape(r) for r in DIAGRAM_ROLES) + r")\b")


class Block:
    __slots__ = ("kind", "lang", "lines", "heading_level", "heading_text")

    def __init__(self, kind, lang="", lines=None, heading_level=0, heading_text=""):
        self.kind = kind
        self.lang = lang
        self.lines = lines or []
        self.heading_level = heading_level
        self.heading_text = heading_text

    def text(self) -> str:
        return "\n".join(self.lines)


def _is_ascii_diagram(lines: List[str]) -> bool:
    """A fence of plain text is a diagram when it DRAWS: at least three
    lines whose non-space content is mostly box-drawing or arrow glyphs."""
    drawn = 0
    for ln in lines:
        stripped = [c for c in ln if not c.isspace()]
        if len(stripped) < 3:
            continue
        if sum(1 for c in stripped if c in BOX_CHARS) / len(stripped) >= 0.5:
            drawn += 1
    return drawn >= 3


def parse(md: str) -> List[Block]:
    """Markdown to a flat block list. Fences are opaque: nothing inside a
    fence is ever read as a heading, a bullet or a table."""
    blocks: List[Block] = []
    lines = md.replace("\r\n", "\n").split("\n")
    i, n = 0, len(lines)
    para: List[str] = []

    def flush_para():
        nonlocal para
        if any(p.strip() for p in para):
            blocks.append(Block("paragraph", lines=[p for p in para]))
        para = []

    while i < n:
        line = lines[i]
        m = FENCE_RE.match(line.strip())
        if m:
            flush_para()
            open_ticks = m.group("ticks")
            char, need = open_ticks[0], len(open_ticks)
            lang = m.group("lang").lower()
            close_re = re.compile(rf"^\s*{re.escape(char)}{{{need},}}\s*$")
            body: List[str] = []
            i += 1
            while i < n and not close_re.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # step past the closing fence (or off the end, if unclosed)
            kind = "diagram" if (lang in DIAGRAM_LANGS or _is_ascii_diagram(body)) else (
                "code" if lang not in NON_CODE_LANGS else "prose_fence")
            blocks.append(Block(kind, lang=lang, lines=body))
            continue

        hm = HEADING_RE.match(line)
        if hm:
            flush_para()
            blocks.append(Block("heading", heading_level=len(hm.group("hashes")),
                                heading_text=hm.group("text").strip()))
            i += 1
            continue

        # setext headings
        if i + 1 < n and line.strip() and SETEXT_H1_RE.match(lines[i + 1]):
            flush_para()
            blocks.append(Block("heading", heading_level=1, heading_text=line.strip()))
            i += 2
            continue
        if i + 1 < n and line.strip() and SETEXT_H2_RE.match(lines[i + 1]) and not BULLET_RE.match(line):
            flush_para()
            blocks.append(Block("heading", heading_level=2, heading_text=line.strip()))
            i += 2
            continue

        if TABLE_ROW_RE.match(line) and i + 1 < n and TABLE_SEP_RE.match(lines[i + 1]):
            flush_para()
            rows = [line]
            i += 1
            while i < n and (TABLE_ROW_RE.match(lines[i]) or TABLE_SEP_RE.match(lines[i])):
                rows.append(lines[i])
                i += 1
            blocks.append(Block("table", lines=rows))
            continue

        if BULLET_RE.match(line):
            flush_para()
            items = []
            while i < n and (BULLET_RE.match(lines[i]) or (lines[i].startswith("  ") and lines[i].strip())):
                items.append(lines[i])
                i += 1
            blocks.append(Block("bullets", lines=items))
            continue

        if NUMBERED_RE.match(line):
            flush_para()
            items = []
            while i < n and (NUMBERED_RE.match(lines[i]) or (lines[i].startswith("  ") and lines[i].strip())):
                items.append(lines[i])
                i += 1
            blocks.append(Block("numbered", lines=items))
            continue

        if QUOTE_RE.match(line):
            flush_para()
            q = []
            while i < n and (QUOTE_RE.match(lines[i]) or lines[i].strip() == ""):
                if lines[i].strip() == "" and (i + 1 >= n or not QUOTE_RE.match(lines[i + 1])):
                    break
                q.append(lines[i])
                i += 1
            blocks.append(Block("quote", lines=q))
            continue

        if not line.strip():
            flush_para()
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    return blocks


# ------------------------------------------------------------- the checks --

def _norm(s: str) -> str:
    """Compare headings by their words: '## 4. AI Inference Layer' and
    'AI Inference Layer' are the same section."""
    s = re.sub(r"^\s*\d+(\.\d+)*[.)]?\s*", "", s.strip())
    s = re.sub(r"[*_`#]", "", s)
    s = re.sub(r"[^a-z0-9]+", " ", s.lower())
    return " ".join(s.split())


def _prose_words(blocks: List[Block]) -> int:
    """Words a reader reads: prose, lists, table cells, callouts. Code and
    diagram fences are NOT prose -- a report is not long because it pasted
    a config file."""
    w = 0
    for b in blocks:
        if b.kind in ("paragraph", "bullets", "numbered", "quote", "table", "prose_fence"):
            w += len(re.sub(r"[|>*_`-]", " ", b.text()).split())
    return w


def _sections(blocks: List[Block]) -> List[Tuple[int, str, List[Block]]]:
    """(index, heading text, body blocks) for each TOP-LEVEL section.

    Top level = the shallowest heading level that is used more than twice,
    ignoring a single document title above it. That makes '# title / ## 1..15'
    and '# title / # 1..15' both read as 15 sections, which is the only way
    a chat answer and a file can be compared.
    """
    heads = [(i, b) for i, b in enumerate(blocks) if b.kind == "heading"]
    if not heads:
        return []
    levels: Dict[int, int] = {}
    for _, b in heads:
        levels[b.heading_level] = levels.get(b.heading_level, 0) + 1
    candidates = sorted(lvl for lvl, count in levels.items() if count >= 3)
    top = candidates[0] if candidates else min(levels)
    out = []
    marks = [(i, b) for i, b in heads if b.heading_level == top]
    for n, (i, b) in enumerate(marks):
        end = marks[n + 1][0] if n + 1 < len(marks) else len(blocks)
        out.append((i, b.heading_text, blocks[i + 1:end]))
    return out


def _shingles(text: str, k: int = 8) -> set:
    w = _norm(text).split()
    return {" ".join(w[i:i + k]) for i in range(max(0, len(w) - k + 1))}


def score(md: str, label: str) -> Score:
    blocks = parse(md)
    secs = _sections(blocks)
    sec_names = [_norm(t) for _, t, _ in secs]
    required = [_norm(s) for s in K.REQUIRED_SECTIONS]
    s = Score(label=label)

    def add(cid: str, ok: bool, observed: str):
        s.results.append(Result(cid, ok, observed, K.CHECKS_BY_ID[cid].group))

    # title
    all_text = md
    add("title", K.REQUIRED_TITLE.lower() in all_text.lower()
        or _norm(K.REQUIRED_TITLE) in _norm(all_text),
        f"title string {'found' if K.REQUIRED_TITLE.lower() in all_text.lower() else 'NOT found'}")

    # sections present
    found_map = {}
    for r, raw in zip(required, K.REQUIRED_SECTIONS):
        hit = next((n for n, nm in enumerate(sec_names) if nm == r or r in nm or nm in r), None)
        found_map[raw] = hit
    missing = [k for k, v in found_map.items() if v is None]
    add("sections_present", not missing,
        f"{len(K.REQUIRED_SECTIONS) - len(missing)}/15 present"
        + (f"; missing {missing}" if missing else "")
        + f"; top-level headings found: {len(secs)}")

    # order
    idxs = [v for v in found_map.values() if v is not None]
    in_order = idxs == sorted(idxs)
    add("sections_in_order", in_order and not missing,
        f"positions {idxs}" if idxs else "none found")

    # substantive
    short = []
    for raw, pos in found_map.items():
        if pos is None:
            continue
        w = _prose_words(secs[pos][2])
        if w < K.SECTION_WORD_FLOOR:
            short.append((raw, w))
    add("sections_substantive", not short and not missing,
        f"{len(short)} section(s) under {K.SECTION_WORD_FLOOR} words"
        + (f": {short[:6]}{'...' if len(short) > 6 else ''}" if short else ""))

    # total words
    total = _prose_words(blocks)
    add("total_words", K.TOTAL_WORDS_MIN <= total <= K.TOTAL_WORDS_MAX,
        f"{total:,} words (band {K.TOTAL_WORDS_MIN:,}-{K.TOTAL_WORDS_MAX:,})")

    # headings
    n_head = sum(1 for b in blocks if b.kind == "heading")
    add("headings", n_head >= K.HEADINGS_MIN,
        f"{n_head} headings (need {K.HEADINGS_MIN})")

    # subheadings: sections with at least one deeper heading
    deep = 0
    for _, _, body in secs:
        if any(b.kind == "heading" for b in body):
            deep += 1
    add("subheadings", deep >= K.SECTIONS_WITH_SUBHEADING_MIN,
        f"{deep}/{len(secs)} sections carry a subheading "
        f"(need {K.SECTIONS_WITH_SUBHEADING_MIN})")

    n_tab = sum(1 for b in blocks if b.kind == "table")
    add("tables", n_tab >= K.TABLES_MIN, f"{n_tab} tables (need {K.TABLES_MIN})")

    n_bul = sum(1 for b in blocks if b.kind == "bullets")
    add("bullets", n_bul >= K.BULLET_LISTS_MIN, f"{n_bul} bullet lists (need {K.BULLET_LISTS_MIN})")

    n_num = sum(1 for b in blocks if b.kind == "numbered")
    add("numbered", n_num >= K.NUMBERED_LISTS_MIN, f"{n_num} numbered lists (need {K.NUMBERED_LISTS_MIN})")

    # bold: only outside code/diagram fences
    prose = "\n".join(b.text() for b in blocks
                      if b.kind in ("paragraph", "bullets", "numbered", "quote", "table"))
    prose += "\n" + "\n".join(b.heading_text for b in blocks if b.kind == "heading")
    n_bold = len(BOLD_RE.findall(prose))
    add("bold", n_bold >= K.BOLD_RUNS_MIN, f"{n_bold} bold runs (need {K.BOLD_RUNS_MIN})")

    n_code = sum(1 for b in blocks if b.kind == "code")
    add("code_blocks", n_code >= K.CODE_BLOCKS_MIN,
        f"{n_code} code fences (need {K.CODE_BLOCKS_MIN})")

    # callouts: a blockquote, or a paragraph opening with a warning/note word
    callouts = [b for b in blocks if b.kind == "quote"]
    # The request names two kinds, "warnings, notes", so the check is
    # `CALLOUTS_MIN` callouts of which `WARNINGS_MIN` are warnings. Only the
    # warning pattern is needed: anything that is a callout and is not a
    # warning is the "note" half by elimination, and a second pattern that
    # nothing reads is a rule a reader believes is enforced when it is not.
    warn_re = re.compile(r"\b(warning|caution|danger|important|do not|never)\b", re.I)
    n_warn = sum(1 for b in callouts if warn_re.search(b.text()))
    add("callouts", len(callouts) >= K.CALLOUTS_MIN and n_warn >= K.WARNINGS_MIN,
        f"{len(callouts)} callouts, {n_warn} of them warnings "
        f"(need {K.CALLOUTS_MIN} / {K.WARNINGS_MIN})")

    rec = len(re.findall(r"\brecommend(?:ation|ations|ed|s)?\b", md, re.I))
    add("recommendations", rec >= K.RECOMMENDATION_MENTIONS_MIN,
        f"{rec} 'recommend*' mentions (need {K.RECOMMENDATION_MENTIONS_MIN})")

    # repeats
    dup_heads = [h for h in set(sec_names) if sec_names.count(h) > 1]
    paras = [b.text() for b in blocks if b.kind == "paragraph" and len(b.text().split()) >= 25]
    dup_paras = 0
    sh = [_shingles(p) for p in paras]
    for a in range(len(sh)):
        for bidx in range(a + 1, len(sh)):
            if not sh[a] or not sh[bidx]:
                continue
            ov = len(sh[a] & sh[bidx]) / min(len(sh[a]), len(sh[bidx]))
            if ov >= K.REPEAT_SHINGLE_OVERLAP:
                dup_paras += 1
    add("no_repeats", not dup_heads and dup_paras == 0,
        f"{len(dup_heads)} duplicate headings, {dup_paras} near-duplicate paragraph pairs")

    low = md.lower()
    used = [c for c in K.CONTEXT_ITEMS if c.lower() in low]
    add("context_used", len(used) >= K.CONTEXT_ITEMS_MIN,
        f"{len(used)}/{len(K.CONTEXT_ITEMS)} context items used"
        + (f"; missing {[c for c in K.CONTEXT_ITEMS if c not in used]}" if len(used) < len(K.CONTEXT_ITEMS) else ""))

    # markdown_clean: literal markers that reached the reader as text.
    # Only meaningful for a RENDERED artifact; for raw Markdown this is
    # trivially true, so it is reported with what it saw.
    stray = len(re.findall(r"(?<!\*)\*\*(?!\*)", prose)) % 2
    add("markdown_clean", stray == 0, f"unbalanced '**' runs: {stray}")

    # extras (NOT from the prompt)
    diagrams = [b for b in blocks if b.kind == "diagram"]
    add("diagrams", len(diagrams) >= K.DIAGRAMS_MIN, f"{len(diagrams)} diagrams")
    roled = 0
    roled_nodes = 0
    for b in diagrams:
        if b.lang not in DIAGRAM_LANGS:
            continue
        hits = DIAGRAM_ROLE_RE.findall(b.text())
        if len(hits) >= K.DIAGRAM_ROLED_NODES_MIN:
            roled += 1
            roled_nodes += len(hits)
    add("diagram_roles", len(diagrams) > 0 and roled > 0,
        f"{roled}/{len(diagrams)} diagrams tag their nodes with a role class "
        f"({roled_nodes} tagged node(s); vocabulary {list(DIAGRAM_ROLES)})")

    s.stats = {
        "words": total, "blocks": len(blocks), "sections": len(secs),
        "headings": n_head, "tables": n_tab, "code_fences": n_code,
        "diagrams": len(diagrams), "bullets": n_bul, "numbered": n_num,
        "bold_runs": n_bold, "callouts": len(callouts),
    }
    return s


def report(s: Score) -> str:
    lines = [f"=== {s.label} ===",
             "  " + "  ".join(f"{k}={v}" for k, v in s.stats.items()),
             f"  PROMPT CHECKS: {s.passed}/{s.total}"]
    for r in s.results:
        tag = "PASS" if r.passed else "FAIL"
        extra = "  (not asked in the prompt)" if r.group == "extra" else ""
        lines.append(f"  [{tag}] {r.check_id:22s} {r.observed}{extra}")
    return "\n".join(lines)


# ------------------------------------------------------------- hand-scoring --

def _main(argv: list) -> int:
    """`python -m tests.parity.score runs/prod_spec.json [...]` from orchestrator/.

    A convenience, not a gate: the gate is `test_parity.py` and
    `test_parity_gate.py`. This exists so a builder can score one candidate
    without writing the three-line import dance every time.
    """
    from . import normalise

    if not argv:
        print(__doc__)
        return 2
    for path in argv:
        print(report(score(normalise.load(path), path.rsplit("/", 1)[-1])))
    return 0


if __name__ == "__main__":  # pragma: no cover -- a CLI, exercised by hand
    import sys as _sys

    raise SystemExit(_main(_sys.argv[1:]))
