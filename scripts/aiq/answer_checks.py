"""Deterministic answer checks for the upgrade evaluation set (eval_set.py).

harness.check_turn scores formatting, files and charts. These checks add the
request-understanding constraints of MASTER_PROMPT §11 and §13: names only,
explain-without-modifying, quoted text left verbatim, citations that point at
sources the run actually read, complete code rather than an outline, numbers
that come from the supplied material, and a stated gap instead of filler.

Every check is a pure function of the answer text and the turn record; none
calls a model, a service or the network, and none executes the answer. The
Python checks only COMPILE the answer's files (compile() never runs them);
running code is code_sandbox.run_code's job, behind its isolation.

Each `expect` key below is opt-in, so no existing case changes score. The
turn record (`res`) is the one run.py builds; these checks read:

  res["answer"]            the streamed answer text
  res["md"]                harness.md_metrics(answer)
  res["meta"]["sources"]   the sources panel: [{n, url, read, ...}]; `read`
                           False (or missing) means the page was never
                           inspected (a search snippet), so it cannot back a
                           citation
  res["source_passages"]   {n: text} of each cited source, for
                           citations.passage_support only; a runner that does
                           not capture passages fails that check, it never
                           passes it by default

Keys (all optional):

  names_only        {required, allowed?, min_items?, max_items?, max_words?}
                    the answer is a bare list of names and nothing else
  code_unmodified   {original}  any block that re-presents the supplied code is
                    a verbatim excerpt of it; no diff, no "here is the fixed version"
  quoted_verbatim   [span, ...]  each quoted span appears exactly (quote marks
                    and whitespace normalised, case kept)
  regex_all         [pattern, ...]  each must match (case-insensitive, multiline)
  regex_none        [pattern, ...]  none may match
  min_matches       {options, min}  at least `min` distinct options appear as words
  citations         {min_distinct, require_read?, support?, passage_support?}
  sources_used      {min?, max?}  how many READ sources meta.sources lists
  no_citations      True  no [n] marker and no URL
  complete_code     {lang, files, symbols?}  every file is present, has no
                    placeholder, compiles, and defines the named symbols
  numbers_grounded  {source, allow?}  every number in the answer occurs in the
                    source text (digits or number words)
  gap_stated        {topic_any}  one sentence says the material does not cover
                    one of these topics
  required_sections [[alternatives], ...]  heading lines, in this order
  section_bullets   {heading: [min, max]}  bullets under that heading
  max_headings / max_code_blocks / max_tables   ceilings on the markdown
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Set, Tuple
from urllib.parse import urlsplit

import code_sandbox

#: expect key -> the check names it emits, and the dimension each is scored in.
CHECKS: Dict[str, Tuple[str, ...]] = {
    "names_only": ("names_only",),
    "code_unmodified": ("code_unmodified",),
    "quoted_verbatim": ("quoted_verbatim",),
    "regex_all": ("regex_all",),
    "regex_none": ("regex_none",),
    "min_matches": ("min_matches",),
    "citations": ("citations", "citation_passages"),
    "sources_used": ("sources_used",),
    "no_citations": ("no_citations",),
    "complete_code": ("code_files_present", "code_no_placeholders", "code_compiles", "code_symbols"),
    "numbers_grounded": ("numbers_grounded",),
    "gap_stated": ("gap_stated",),
    "required_sections": ("required_sections",),
    "section_bullets": ("section_bullets",),
    "max_headings": ("max_headings",),
    "max_code_blocks": ("max_code_blocks",),
    "max_tables": ("max_tables",),
}
EXPECT_KEYS = frozenset(CHECKS)

DIMENSION = {
    "names_only": "constraints", "code_unmodified": "constraints", "section_bullets": "constraints",
    "quoted_verbatim": "fidelity", "regex_all": "fidelity", "regex_none": "fidelity", "min_matches": "fidelity",
    "numbers_grounded": "fidelity", "gap_stated": "fidelity",
    "citations": "evidence", "citation_passages": "evidence", "sources_used": "evidence", "no_citations": "evidence",
    "code_files_present": "code", "code_no_placeholders": "code", "code_compiles": "code", "code_symbols": "code",
    "required_sections": "structure", "max_headings": "structure", "max_code_blocks": "structure",
    "max_tables": "structure",
}

# ============================================================== helpers ==

_QUOTES = str.maketrans({"\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"', "\u2033": '"',
                         "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
                         "\u00a0": " ", "\u2009": " ", "\u202f": " "})


def normalise(text: str) -> str:
    """Straight quotes, single spaces. Case is kept."""
    return re.sub(r"\s+", " ", (text or "").translate(_QUOTES)).strip()


def strip_code(text: str) -> str:
    """The answer with every fenced block removed (prose only)."""
    return re.sub(r"^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$", "", text or "", flags=re.S | re.M)


def sentences(text: str) -> List[str]:
    """Sentences and list lines. Splits on . ! ? before whitespace, and on newlines."""
    out: List[str] = []
    for line in (text or "").splitlines():
        for part in re.split(r"(?<=[.!?])\s+", line):
            if part.strip():
                out.append(part.strip())
    return out


_HEADING_ATX = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_HEADING_BOLD = re.compile(r"^\s*(?:\*\*|__)([^*_\n]{1,80})(?:\*\*|__)\s*:?\s*$")
_HEADING_LABEL = re.compile(r"^\s*([A-Za-z][A-Za-z /&-]{0,40}):\s*$")
_LIST_ITEM = re.compile(r"^\s*(?:[-*+\u2022]|\d{1,3}[.)])\s+(.*)$")


def heading_lines(text: str) -> List[Tuple[int, str]]:
    """(line index, lower-case heading text) for ATX headings, bold-only lines
    and short 'Label:' lines, outside fenced code."""
    out = []
    lines = strip_code(text).splitlines()
    for i, ln in enumerate(lines):
        m = _HEADING_ATX.match(ln) or _HEADING_BOLD.match(ln) or _HEADING_LABEL.match(ln)
        if m:
            out.append((i, m.group(1).strip().strip("*_:").lower()))
    return out


_NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}
_NUMBER = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(\.\d+)?(?:(k|m)\b|\s?(thousand|million)\b)?", re.I)
_MARKER = re.compile(r"\[(\d{1,3}(?:\s*[,;]\s*\d{1,3})*)\]")
_LIST_MARKER = re.compile(r"^\s*\d{1,3}[.)]\s", re.M)
_URL = re.compile(r"https?://[^\s<>\"'`\])]+", re.I)


def _canon(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else repr(round(float(value), 6)).rstrip("0").rstrip(".")


def numbers_in(text: str, *, words: bool = False) -> Set[str]:
    """Canonical numbers in the text: 120,000 / 120000 / 120k -> '120000'; 22.20 -> '22.2'.

    List markers at the start of a line and citation markers are not numbers.
    With `words`, number words (two, forty) count as well — used for the
    SOURCE side, so an answer may write a digit the source spelled out.
    """
    text = _LIST_MARKER.sub(" ", _MARKER.sub(" ", text or ""))
    out: Set[str] = set()
    for m in _NUMBER.finditer(text):
        whole, frac = m.group(1).replace(",", ""), m.group(2) or ""
        scale = (m.group(3) or m.group(4) or "").lower()
        value = float(whole + frac)
        out.add(_canon(value * {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6}.get(scale, 1)))
        if scale and words:
            # the SOURCE side is the reference, so it is read both ways: "300m"
            # may be metres, and an answer that writes 300 must not fail for it
            out.add(_canon(value))
    if words:
        for w in re.findall(r"[a-z]+", text.lower()):
            if w in _NUMBER_WORDS:
                out.add(str(_NUMBER_WORDS[w]))
    return out


def _norm_url(url: str) -> str:
    url = url.strip().rstrip(".,;:!?")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    return f"{host}{path}" + (f"?{parts.query}" if parts.query else "")


def _markers(text: str) -> List[int]:
    out: List[int] = []
    for group in _MARKER.findall(text or ""):
        for n in re.split(r"\s*[,;]\s*", group):
            if n.isdigit() and int(n) not in out:
                out.append(int(n))
    return out


def _sources(res: dict) -> List[dict]:
    rows = ((res.get("meta") or {}).get("sources")) or []
    return [r for r in rows if isinstance(r, dict)]


def _matches(text: str, alt: Any) -> bool:
    """A claim alternative: a plain string (case-insensitive substring) or {"regex": pattern}."""
    if isinstance(alt, dict):
        return re.search(alt["regex"], text, re.I) is not None
    return str(alt).lower() in text.lower()


# =============================================================== checks ==

def check_names_only(answer: str, spec: dict) -> Tuple[bool, str]:
    text = (answer or "").strip()
    if not text:
        return False, "empty answer"
    if "```" in text or "~~~" in text:
        return False, "the answer holds a code block"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    items: List[str] = []
    for ln in lines:
        m = _LIST_ITEM.match(ln)
        if m:
            items.append(m.group(1))
        elif len(lines) == 1:
            items.extend(p for p in re.split(r"\s*(?:,|;|\band\b|&)\s*", ln) if p.strip())
        else:
            items.append(ln)
    max_words = int(spec.get("max_words", 4))
    allowed = {a.casefold() for a in spec.get("allowed") or []}
    problems = []
    cleaned = []
    for raw in items:
        item = re.sub(r"[*_`]", "", raw).strip().rstrip(".,;")
        cleaned.append(item)
        if re.search(r":|\s[-\u2013\u2014]\s|\(|\)", item):
            problems.append(f"not a bare name: {item[:60]!r}")
        elif len(item.split()) > max_words:
            problems.append(f"more than {max_words} words: {item[:60]!r}")
        elif not item[:1].isupper():
            problems.append(f"does not read as a name: {item[:60]!r}")
        elif allowed and item.casefold() not in allowed:
            problems.append(f"not a name from the material: {item[:60]!r}")
    have = {c.casefold() for c in cleaned}
    missing = [r for r in spec.get("required") or [] if r.casefold() not in have]
    if missing:
        problems.append(f"missing {missing}")
    lo, hi = spec.get("min_items"), spec.get("max_items")
    if lo is not None and len(cleaned) < lo:
        problems.append(f"{len(cleaned)} items, need >= {lo}")
    if hi is not None and len(cleaned) > hi:
        problems.append(f"{len(cleaned)} items, max {hi}")
    return not problems, "; ".join(problems) or f"{len(cleaned)} names"


_REWRITE_CLAIMS = re.compile(
    r"\b(?:here(?:'s| is) (?:the|a|an|your) (?:fixed|corrected|updated|modified|refactored|improved|rewritten|cleaner)"
    r"|i(?:'ve| have)? (?:fixed|modified|updated|refactored|rewrote|rewritten|changed) (?:the|your|this) code"
    r"|(?:improved|refactored|corrected|fixed) version)\b", re.I)
_ELISION = re.compile(r"^\s*(?:#|//)?\s*(?:\.\.\.|\u2026)\s*$")
_DEF_NAME = re.compile(r"^\s*(?:async\s+)?(?:def|class|function)\s+([A-Za-z_]\w*)")


def check_code_unmodified(answer: str, spec: dict) -> Tuple[bool, str]:
    """Quoting the code is explaining it; re-presenting it changed is modifying it.

    A block re-presents the code when it (re)defines a name the original
    defines, or when more than half its lines come from the original. Such a
    block must then be a verbatim excerpt (indentation and blank lines aside,
    `...` elision lines allowed). A diff/patch block, or a sentence offering
    "the fixed version", fails outright. A usage example that defines nothing
    of the original's is not a modification.
    """
    original = spec["original"]
    orig_lines = {normalise(ln) for ln in original.splitlines() if ln.strip()}
    orig_names = {m.group(1) for m in map(_DEF_NAME.match, original.splitlines()) if m}
    problems = []
    for tag, _info, body, _at in code_sandbox.fences(answer):
        lines = [ln for ln in body.splitlines() if ln.strip() and not _ELISION.match(ln)]
        diff_lines = [ln for ln in lines if re.match(r"^[+-](?![+-])", ln)]
        if tag in ("diff", "patch") or len(diff_lines) >= 2:
            problems.append("a diff of the code")
            continue
        foreign = [ln for ln in lines if normalise(ln) not in orig_lines]
        defines = {m.group(1) for m in map(_DEF_NAME.match, lines) if m}
        represents = bool(defines & orig_names) or (bool(lines) and len(foreign) < len(lines) / 2)
        if represents and foreign:
            problems.append(f"a changed copy of the code: {foreign[0].strip()[:80]!r}")
    claim = _REWRITE_CLAIMS.search(strip_code(answer))
    if claim:
        problems.append(f"offers a rewrite: {claim.group(0)!r}")
    return not problems, "; ".join(problems) or "no modified copy of the code"


def check_quoted_verbatim(answer: str, spans: Iterable[str]) -> Tuple[bool, str]:
    text = normalise(answer)
    missing = [s for s in spans if normalise(s) not in text]
    return not missing, f"missing verbatim: {missing}" if missing else "every quoted span kept"


def check_regex_all(answer: str, patterns: Iterable[str]) -> Tuple[bool, str]:
    missing = [p for p in patterns if not re.search(p, answer or "", re.I | re.M)]
    return not missing, f"no match for {missing}" if missing else "all patterns matched"


def check_regex_none(answer: str, patterns: Iterable[str]) -> Tuple[bool, str]:
    hits = []
    for p in patterns:
        m = re.search(p, answer or "", re.I | re.M)
        if m:
            hits.append(f"{p} -> {m.group(0)!r}")
    return not hits, f"forbidden match: {hits}" if hits else "no forbidden pattern"


def check_min_matches(answer: str, spec: dict) -> Tuple[bool, str]:
    found = [o for o in spec["options"] if re.search(rf"\b{re.escape(o)}\b", answer or "", re.I)]
    return len(found) >= spec["min"], f"found {found} need >= {spec['min']}"


def check_citations(answer: str, res: dict, spec: dict) -> List[Tuple[str, bool, str]]:
    """Citations present, resolvable, and pointing at sources the run READ.

    [n] markers resolve against meta.sources[].n, inline URLs against
    meta.sources[].url. `support` lists claims (a string or {"regex": ...});
    EVERY sentence stating one must carry a citation. `passage_support` adds a
    second check: every figure in a cited sentence must occur in the passage
    of a source that sentence cites (res["source_passages"]).
    """
    out: List[Tuple[str, bool, str]] = []
    rows = _sources(res)
    by_n = {int(r["n"]): r for r in rows if str(r.get("n", "")).isdigit()}
    by_url = {_norm_url(r["url"]): r for r in rows if r.get("url")}
    require_read = spec.get("require_read", True)
    prose = strip_code(answer)
    markers = _markers(prose)
    urls = [u.rstrip(".,;:!?") for u in _URL.findall(prose)]
    problems: List[str] = []
    cited: Set[str] = set()
    if not markers and not urls:
        problems.append("no citation in the answer")
    for n in markers:
        row = by_n.get(n)
        if row is None:
            problems.append(f"[{n}] points at no source")
        elif require_read and row.get("read") is not True:
            problems.append(f"[{n}] cites a source that was never read")
        else:
            cited.add(_norm_url(row.get("url") or f"n:{n}"))
    for u in urls:
        row = by_url.get(_norm_url(u))
        if row is None:
            problems.append(f"cites a URL the run did not inspect: {_norm_url(u)[:80]}")
        elif require_read and row.get("read") is not True:
            problems.append(f"cites a URL that was never read: {_norm_url(u)[:80]}")
        else:
            cited.add(_norm_url(u))
    need = int(spec.get("min_distinct", 1))
    if len(cited) < need:
        problems.append(f"{len(cited)} distinct read source(s) cited, need >= {need}")
    sents = [s for s in sentences(prose) if not _HEADING_ATX.match(s)]
    for group in spec.get("support") or []:
        alts = group if isinstance(group, list) else [group]
        claim_sents = [s for s in sents if any(_matches(s, a) for a in alts)]
        bare = [s for s in claim_sents if not (_markers(s) or _URL.search(s))]
        if not claim_sents:
            problems.append(f"claim {alts} is missing")
        elif bare:
            problems.append(f"claim {alts} stated without a citation: {bare[0][:80]!r}")
    out.append(("citations", not problems, "; ".join(problems)[:300] or f"{len(cited)} read source(s) cited"))

    if spec.get("passage_support"):
        passages = res.get("source_passages") or {}
        issues: List[str] = []
        checked = 0
        for s in sents:
            ns = _markers(s)
            if not ns or _URL.search(s):  # a reference-list entry, not a claim
                continue
            figures = numbers_in(s)
            if not figures:
                continue
            texts = [passages.get(n, passages.get(str(n))) for n in ns]
            if any(t is None for t in texts):
                gone = [n for n, t in zip(ns, texts) if t is None]
                issues.append(f"no passage captured for {gone}: support cannot be shown")
                continue
            checked += 1
            have = set().union(*(numbers_in(t, words=True) for t in texts))
            unsupported = sorted(figures - have)
            if unsupported:
                issues.append(f"{unsupported} in {s[:60]!r} not in cited passage(s) {ns}")
        if not checked and not issues:
            issues.append("no cited sentence carries a figure to check")
        out.append(("citation_passages", not issues, "; ".join(issues)[:300] or f"{checked} cited figure sentence(s) supported"))
    return out


def check_sources_used(res: dict, spec: dict) -> Tuple[bool, str]:
    read = [r for r in _sources(res) if r.get("read") is True]
    lo, hi = spec.get("min"), spec.get("max")
    ok = (lo is None or len(read) >= lo) and (hi is None or len(read) <= hi)
    return ok, f"read sources={len(read)} listed={len(_sources(res))} min={lo} max={hi}"


def check_no_citations(answer: str) -> Tuple[bool, str]:
    prose = strip_code(answer)
    found = [f"[{n}]" for n in _markers(prose)] + _URL.findall(prose)
    return not found, f"found {found[:5]}" if found else "no citation"


_PLACEHOLDER_CODE = [
    (re.compile(r"^\s*\.\.\.\s*$", re.M), "a bare `...` body"),
    (re.compile(r"#\s*(?:TODO|FIXME|XXX)\b", re.I), "a TODO comment"),
    (re.compile(r"(?:#|//|/\*)[^\n]*\b(?:implement|implementation|your code|your logic)\b[^\n]*\b(?:here|later|this|goes)\b", re.I),
     "an 'implement here' comment"),
    (re.compile(r"(?:#|//|/\*)\s*(?:\.\.\.|\u2026)", re.I), "an elided `# ...` section"),
    (re.compile(r"(?:#|//)[^\n]*\b(?:rest of (?:the )?(?:code|implementation|file|class)|existing code|"
                r"remaining (?:code|methods|logic)|same as (?:before|above)|"
                r"(?:everything else|the rest|other methods?) (?:is |are |stays? |remains? )?unchanged)\b", re.I),
     "an elided section"),
    (re.compile(r"\braise\s+NotImplementedError\b"), "raise NotImplementedError"),
    (re.compile(r"<\s*(?:your|insert|add|fill)[^>\n]*>", re.I), "a <fill in> placeholder"),
]
_PLACEHOLDER_PROSE = re.compile(
    r"\b(?:left as an exercise|fill in the (?:rest|details|remaining)|implement the (?:rest|remaining)|"
    r"rest of the implementation|you(?:'ll| will| would)? need to implement|add your own (?:logic|code)|"
    r"(?:here(?:'s| is) )?(?:a |an )?(?:high-level )?outline of|skeleton (?:code|implementation)|pseudo-?code)\b", re.I)


def _python_stub_functions(tree: Any) -> List[str]:
    """Functions whose whole body is pass / ... / a docstring, not abstract."""
    import ast
    stubs = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if any((isinstance(d, ast.Name) and d.id == "abstractmethod")
               or (isinstance(d, ast.Attribute) and d.attr == "abstractmethod") for d in node.decorator_list):
            continue
        body = [b for b in node.body
                if not (isinstance(b, ast.Expr) and isinstance(getattr(b, "value", None), ast.Constant)
                        and isinstance(b.value.value, str))]
        if not body or all(isinstance(b, ast.Pass) or (isinstance(b, ast.Expr) and isinstance(b.value, ast.Constant)
                                                       and b.value.value is Ellipsis) for b in body):
            stubs.append(node.name)
    return stubs


def check_complete_code(answer: str, spec: dict) -> List[Tuple[str, bool, str]]:
    """Every requested file is in the answer, complete, compiling, and defines what was asked."""
    import ast
    lang = spec.get("lang", "python")
    wanted = list(spec["files"])
    found = code_sandbox.answer_files(answer, wanted, lang)
    out: List[Tuple[str, bool, str]] = []
    missing = [w for w in wanted if len([ln for ln in found.get(w, "").splitlines() if ln.strip()]) < 3]
    out.append(("code_files_present", not missing,
                f"missing or empty: {missing}" if missing else f"files: {sorted(found)}"))

    holes = []
    for name, body in found.items():
        for rx, why in _PLACEHOLDER_CODE:
            if rx.search(body):
                holes.append(f"{name}: {why}")
                break
    prose = _PLACEHOLDER_PROSE.search(strip_code(answer))
    if prose:
        holes.append(f"prose: {prose.group(0)!r}")
    trees: Dict[str, Any] = {}
    errors = []
    if lang == "python":
        for name, body in found.items():
            if not name.endswith(".py"):
                continue
            try:
                compile(body, name, "exec", dont_inherit=True)  # compiles only; nothing is executed
                trees[name] = ast.parse(body, filename=name)
            except (SyntaxError, ValueError, RecursionError, MemoryError) as exc:
                errors.append(f"{name}: {type(exc).__name__}: {getattr(exc, 'msg', exc)} (line {getattr(exc, 'lineno', '?')})")
        for name, tree in trees.items():
            stubs = _python_stub_functions(tree)
            if stubs:
                holes.append(f"{name}: stub function(s) {stubs}")
    out.append(("code_no_placeholders", bool(found) and not holes,
                "; ".join(holes)[:300] if holes else ("no code" if not found else "no placeholder")))
    if lang == "python":
        out.append(("code_compiles", bool(trees) and not errors and len(trees) == len([n for n in found if n.endswith(".py")]),
                    "; ".join(errors)[:300] if errors else ("no python file" if not trees else f"{len(trees)} file(s) compile")))
    lacking = []
    for name, symbols in (spec.get("symbols") or {}).items():
        tree = trees.get(name)
        defined = set()
        if tree is not None:
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        absent = [s for s in symbols if s not in defined]
        if absent:
            lacking.append(f"{name}: {absent}")
    out.append(("code_symbols", not lacking, "; ".join(lacking) or "every requested symbol defined"))
    return out


def check_numbers_grounded(answer: str, spec: dict) -> Tuple[bool, str]:
    source = numbers_in(spec["source"], words=True) | {str(a) for a in spec.get("allow") or []}
    invented = sorted(numbers_in(strip_code(answer)) - source, key=lambda s: (len(s), s))
    return not invented, f"numbers not in the source: {invented[:8]}" if invented else "every number from the source"


_GAP = re.compile(
    r"\b(?:not (?:in|covered|mentioned|listed|specified|stated|included|provided|given|addressed|found|available)"
    r"|(?:does|do|did)(?: not|n't) (?:mention|cover|list|include|say|specify|state|give|provide|address|contain)"
    r"|(?:is|are)(?: not|n't) (?:mentioned|covered|listed|specified|stated|included|provided|given|available|in)"
    r"|no (?:information|mention|figure|rate|cap|amount|data|details?)"
    r"|(?:can(?:'t|not)|could(?: not|n't)) find|silent on|only (?:covers|lists|gives))\b", re.I)


def check_gap_stated(answer: str, spec: dict) -> Tuple[bool, str]:
    topics = [t.lower() for t in spec.get("topic_any") or []]
    for s in sentences(strip_code(answer)):
        if _GAP.search(s) and (not topics or any(t in s.lower() for t in topics)):
            return True, f"gap stated: {s[:120]!r}"
    return False, f"no sentence says the material does not cover {topics or 'the question'}"


def check_required_sections(answer: str, groups: List[List[str]]) -> Tuple[bool, str]:
    heads = heading_lines(answer)
    positions = []
    for g in groups:
        hit = next((i for i, h in heads if any(a.lower() in h for a in g)), None)
        positions.append(hit)
    missing = [g for g, p in zip(groups, positions) if p is None]
    if missing:
        return False, f"no heading for {missing}; headings={[h for _, h in heads][:10]}"
    if positions != sorted(positions):
        return False, f"headings out of order: {positions}"
    return True, f"{len(groups)} sections in order"


def check_section_bullets(answer: str, spec: Dict[str, List[int]]) -> Tuple[bool, str]:
    lines = strip_code(answer).splitlines()
    heads = heading_lines(answer)
    problems = []
    for heading, (lo, hi) in spec.items():
        start = next((i for i, h in heads if heading.lower() in h), None)
        if start is None:
            problems.append(f"no {heading!r} heading")
            continue
        end = next((i for i, _ in heads if i > start), len(lines))
        n = sum(1 for ln in lines[start + 1:end] if _LIST_ITEM.match(ln))
        if not lo <= n <= hi:
            problems.append(f"{heading!r}: {n} bullets, need {lo}-{hi}")
    return not problems, "; ".join(problems) or "bullet counts within bounds"


# ================================================================ entry ==

def check(exp: dict, res: dict) -> List[dict]:
    """The checks this module owns, for the keys `exp` carries, in harness format."""
    answer = res.get("answer") or ""
    md = res.get("md") or {}
    results: List[Tuple[str, bool, str]] = []
    if "names_only" in exp:
        results.append(("names_only", *check_names_only(answer, exp["names_only"])))
    if "code_unmodified" in exp:
        results.append(("code_unmodified", *check_code_unmodified(answer, exp["code_unmodified"])))
    if "quoted_verbatim" in exp:
        results.append(("quoted_verbatim", *check_quoted_verbatim(answer, exp["quoted_verbatim"])))
    if "regex_all" in exp:
        results.append(("regex_all", *check_regex_all(answer, exp["regex_all"])))
    if "regex_none" in exp:
        results.append(("regex_none", *check_regex_none(answer, exp["regex_none"])))
    if "min_matches" in exp:
        results.append(("min_matches", *check_min_matches(answer, exp["min_matches"])))
    if "citations" in exp:
        results.extend(check_citations(answer, res, exp["citations"]))
    if "sources_used" in exp:
        results.append(("sources_used", *check_sources_used(res, exp["sources_used"])))
    if exp.get("no_citations"):
        results.append(("no_citations", *check_no_citations(answer)))
    if "complete_code" in exp:
        results.extend(check_complete_code(answer, exp["complete_code"]))
    if "numbers_grounded" in exp:
        results.append(("numbers_grounded", *check_numbers_grounded(answer, exp["numbers_grounded"])))
    if "gap_stated" in exp:
        results.append(("gap_stated", *check_gap_stated(answer, exp["gap_stated"])))
    if "required_sections" in exp:
        results.append(("required_sections", *check_required_sections(answer, exp["required_sections"])))
    if "section_bullets" in exp:
        results.append(("section_bullets", *check_section_bullets(answer, exp["section_bullets"])))
    for key, metric in (("max_headings", "headings"), ("max_code_blocks", "code_blocks"), ("max_tables", "tables")):
        if key in exp:
            v = int(md.get(metric, 0))
            results.append((key, v <= exp[key], f"{metric}={v} max={exp[key]}"))
    return [{"check": name, "dimension": DIMENSION.get(name, "other"), "ok": bool(ok), "detail": str(detail)[:300]}
            for name, ok, detail in results]
