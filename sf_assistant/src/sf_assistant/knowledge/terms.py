"""The knowledge part of the resolver's vocabulary (docs/resolver.md): the knowledge files and the English word list.

Moved from ``engine/vocabulary.py`` (decision A1 of docs/resolver_integration.md) so the knowledge side (the knowledge
tools, the BM25 search, a ``--no-records`` run) can build a resolver without importing engine code. The snapshot part
(stored values, person names, :class:`~sf_assistant.engine.vocabulary.SnapshotPolicy`) stays in
``engine/vocabulary.py``, which builds on these functions.

* :func:`load_knowledge`, :func:`add_knowledge`: objects, fields, picklist values, record types and components of the
  knowledge files, and every word of their text as a known-correct word;
* :func:`load_lexicon`, :func:`add_lexicon`: a generic English word list (by default the system lists
  :data:`LEXICON_PATHS`), known-correct words;
* :func:`knowledge_vocabulary`: both, for a knowledge folder or for knowledge documents already loaded;
* :func:`knowledge_resolver`: that vocabulary indexed (:func:`sf_assistant.knowledge.resolver.build`), with its
  start-up lines (:func:`spelling_status`); never raises.

Standard library and :mod:`sf_assistant.knowledge.resolver` only: no engine import, no duckdb.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from sf_assistant.knowledge.resolver import KINDS, Resolver, Vocabulary, build, split_words

__all__ = ["load_knowledge", "add_knowledge", "load_lexicon", "add_lexicon", "start_vocabulary", "finish_vocabulary",
           "knowledge_vocabulary", "knowledge_resolver", "lexicon_arg", "word_list_problem", "spelling_status",
           "word_set_hash", "LEXICON_PATHS", "SPOKEN_WORDS_PATH"]

LEXICON_PATHS = ("/usr/share/dict/american-english", "/usr/share/dict/british-english")
#: Romanized Hindi and Gujarati question words ("kaunse", "kitne", "mahine"), known words next to every English word
#: list: the team writes Hinglish questions, and without them such words were read as misspelt stored values.
SPOKEN_WORDS_PATH = Path(__file__).with_name("spoken_words.txt")
_APEX_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)?")
_PROCESS_CODE_RE = re.compile(r"^[A-Z]{1,3}\d+[a-z]?(\s*[-/]\s*[A-Z]{0,3}\d+[a-z]?)*\s+")


# ----------------------------------------------------------------------------------------------------- knowledge
def load_knowledge(knowledge_dir: str | Path) -> list[dict[str, Any]]:
    """The object knowledge documents of a folder, sorted by file name (``*.raw.json`` and non-object files skipped)."""
    docs = []
    for path in sorted(Path(knowledge_dir).glob("*.json")):
        if path.name.endswith(".raw.json"):
            continue
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and isinstance(d.get("object"), dict) and d["object"].get("api_name"):
            docs.append(d)
    return docs


def _strings(x: Any) -> Iterable[str]:
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def add_knowledge(vocab: Vocabulary, docs: list[dict[str, Any]]) -> None:
    """Objects, fields, picklist values, record types and components of the knowledge documents, and the words of
    their text as known words."""
    for d in docs:
        o = d["object"]
        api = str(o["api_name"])
        vocab.add("object", api, api, api, "api")
        for src in ("label", "plural_label"):
            if o.get(src):
                vocab.add("object", api, o[src], api, "label" if src == "label" else "plural")
        for f in d.get("fields") or []:
            if not isinstance(f, dict) or not f.get("api_name"):
                continue
            fapi = str(f["api_name"])
            target = f"{api}.{fapi}"
            vocab.add("field", target, fapi, api, "api")
            if f.get("label"):
                vocab.add("field", target, f["label"], api, "label")
            pl = f.get("picklist")
            if isinstance(pl, dict):
                for v in pl.get("values") or []:
                    if not isinstance(v, dict):
                        continue
                    value = v.get("value")
                    if value is None or str(value).strip() == "":
                        continue
                    vt = f"{target}={value}"
                    vocab.add("value", vt, value, api, "picklist")
                    if v.get("label") and str(v["label"]) != str(value):
                        vocab.add("value", vt, v["label"], api, "picklist_label")
        for rt in d.get("record_types") or []:
            if isinstance(rt, dict) and rt.get("developer_name"):
                t = f"{api}.RecordType={rt['developer_name']}"
                vocab.add("record_type", t, rt["developer_name"], api, "api")
                if rt.get("label"):
                    vocab.add("record_type", t, rt["label"], api, "label")
        auto = d.get("automation") or {}
        for kind in ("flows", "apex"):
            for c in auto.get(kind) or []:
                if not isinstance(c, dict) or not c.get("name"):
                    continue
                m = _APEX_NAME_RE.match(str(c["name"]).strip())
                if m:
                    tag = "flow" if kind == "flows" else "apex"
                    vocab.add("component", f"{tag}:{m.group(0)}", m.group(0), api, tag)
        for vr in d.get("validation_rules") or []:
            if isinstance(vr, dict) and vr.get("name"):
                vocab.add("component", f"validation_rule:{api}.{vr['name']}", vr["name"], api, "validation_rule")
        for bp in d.get("business_process_usage") or []:
            if not isinstance(bp, dict) or not (bp.get("process_id") or bp.get("process_name")):
                continue
            pid = str(bp.get("process_id") or bp.get("process_name"))
            if bp.get("process_name"):
                name = _PROCESS_CODE_RE.sub("", str(bp["process_name"])).strip()
                vocab.add("component", f"process:{pid}", name or bp["process_name"], api, "process")
            if bp.get("process_id"):
                vocab.add("component", f"process:{pid}", bp["process_id"], api, "api")
        words: set[str] = set()
        for s in _strings(d):
            words.update(w for w in split_words(s) if w.isalpha() and len(w) >= 2)
        vocab.add_known(words)


# ------------------------------------------------------------------------------------------------------- lexicon
def load_lexicon(paths: Iterable[str | Path] | None = None) -> tuple[set[str], list[str]]:
    """Lower-case words of plain word-list files (one word per line; possessives and multi-word lines skipped), and
    the files that could be read. Generic English, no business words."""
    words: set[str] = set()
    used: list[str] = []
    for p in (LEXICON_PATHS if paths is None else paths):
        try:
            text = Path(p).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        used.append(str(p))
        for line in text.splitlines():
            w = line.strip()
            if not w or "'" in w or " " in w:
                continue
            if w.isascii():
                if w.isalpha():
                    words.add(w.lower())
                continue
            ws = split_words(w)
            if len(ws) == 1 and ws[0].isalpha():
                words.add(ws[0])
    return words, used




def word_set_hash(words: Iterable[str]) -> str:
    """A short hash (12 hex digits) of a word set, for the start-up line: the same words give the same hash whatever
    the files' line order, so a rebuilt image with other word lists shows (web/Dockerfile checks the files)."""
    return hashlib.sha256("\n".join(sorted(words)).encode("utf-8")).hexdigest()[:12]


def add_lexicon(vocab: Vocabulary, lexicon: Any = "auto") -> None:
    """The words of an English word list as known words; ``vocab.info["lexicon"]`` says which files were read, how
    many words they gave and (with no words) a warning. ``lexicon``: ``"auto"`` (the system lists in
    :data:`LEXICON_PATHS` that exist), a path or a list of paths, or None (no word list). With an English word list
    the romanized Hindi and Gujarati question words of :data:`SPOKEN_WORDS_PATH` are known words too
    (``info["lexicon"]["spoken_words"]``); they never count as the English word list."""
    t1 = time.perf_counter()
    if lexicon is None:
        words, used = set(), []
    elif lexicon == "auto":
        words, used = load_lexicon()
    elif isinstance(lexicon, (str, Path)):
        words, used = load_lexicon([lexicon])
    else:
        words, used = load_lexicon(lexicon)
    vocab.add_known(words)
    spoken = load_lexicon([SPOKEN_WORDS_PATH])[0] if words else set()
    vocab.add_known(spoken)
    vocab.info["lexicon"] = {"files": used, "words": len(words), "spoken_words": len(spoken),
                             "sha": word_set_hash(words), "s": round(time.perf_counter() - t1, 3)}
    if not words:
        vocab.info["lexicon"]["warning"] = "no English word list: ordinary words may be taken for typos"


def _object_docs(docs: Iterable[Any]) -> list[dict[str, Any]]:
    """The object knowledge documents among parsed ones (the rule of :func:`load_knowledge`), in the given order."""
    return [d for d in docs if isinstance(d, dict) and isinstance(d.get("object"), dict) and d["object"].get("api_name")]


#: The knowledge and word-list part of the last vocabularies :func:`start_vocabulary` built from a folder, by what it
#: was read from (the folder's ``*.json`` names, sizes and change times; the word-list files' too): a record session
#: reopened after a read-copy swap reads the knowledge files and the word lists again only when one of them changed
#: (about 0.7 s of the spelling help's start-up; docs/resolver.md, "Cost"). Two entries at most.
_START_CACHE: dict[tuple, tuple[list, frozenset, list, dict]] = {}
_START_CACHE_MAX = 2
_START_LOCK = threading.Lock()


def _stat_key(path: Any) -> tuple:
    try:
        st = Path(path).stat()
    except OSError:
        return (str(path), None)
    return (str(path), st.st_size, st.st_mtime_ns)


def _start_key(knowledge: Any, lexicon: Any) -> tuple | None:
    """The cache key of :func:`start_vocabulary` for a knowledge folder, or None (parsed documents: never cached)."""
    if not isinstance(knowledge, (str, Path)):
        return None
    folder = Path(knowledge)
    try:
        files = tuple(_stat_key(p) for p in sorted(folder.glob("*.json")))
        where = str(folder.resolve())
    except OSError:
        return None
    if lexicon is None:
        lex: tuple = ("none",)
    elif lexicon == "auto":
        lex = ("auto",) + tuple(_stat_key(p) for p in LEXICON_PATHS)
    else:
        lex = ("paths",) + tuple(_stat_key(p) for p in ([lexicon] if isinstance(lexicon, (str, Path)) else lexicon))
    return (where, files, lex, _stat_key(SPOKEN_WORDS_PATH))


def start_vocabulary(knowledge: Any, lexicon: Any = "auto") -> tuple[Vocabulary, list[dict[str, Any]], float]:
    """``(vocabulary, knowledge documents, start time)``: the terms and words of the knowledge and the word list,
    before :func:`finish_vocabulary`. ``knowledge``: a folder (:func:`load_knowledge`) or parsed knowledge documents
    (``ObjectKnowledge.raw``). For a folder whose files and word lists did not change since a previous call the
    result is a fresh copy of that call's (:data:`_START_CACHE`; the documents are shared and only read)."""
    t0 = time.perf_counter()
    key = _start_key(knowledge, lexicon)
    with _START_LOCK:
        hit = _START_CACHE.get(key) if key is not None else None
    if hit is not None:
        terms, known, docs, info = hit
        return Vocabulary(list(terms), set(known), copy.deepcopy(info)), docs, t0
    vocab = Vocabulary()
    docs = load_knowledge(knowledge) if isinstance(knowledge, (str, Path)) else _object_docs(knowledge)
    add_knowledge(vocab, docs)
    vocab.info["objects"] = len(docs)
    vocab.info["knowledge_s"] = round(time.perf_counter() - t0, 3)
    add_lexicon(vocab, lexicon)
    if key is not None:
        with _START_LOCK:
            while len(_START_CACHE) >= _START_CACHE_MAX:
                _START_CACHE.pop(next(iter(_START_CACHE)))
            _START_CACHE[key] = (list(vocab.terms), frozenset(vocab.known_words), docs, copy.deepcopy(vocab.info))
    return vocab, docs, t0


def finish_vocabulary(vocab: Vocabulary, t0: float) -> Vocabulary:
    """Counts of ``vocab.info``: terms by kind, known words and the seconds since ``t0``."""
    by_kind: dict[str, int] = {}
    for t in vocab.terms:
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
    vocab.info["terms"] = by_kind
    vocab.info["known_words"] = len(vocab.known_words)
    vocab.info["total_s"] = round(time.perf_counter() - t0, 3)
    return vocab


def knowledge_vocabulary(knowledge: Any, lexicon: Any = "auto") -> Vocabulary:
    """The vocabulary of the knowledge (a folder or parsed documents) and an English word list: what
    :func:`sf_assistant.engine.vocabulary.build_vocabulary` gives without a snapshot (no stored values, no person
    names)."""
    vocab, _docs, t0 = start_vocabulary(knowledge, lexicon)
    return finish_vocabulary(vocab, t0)


# ------------------------------------------------------------------------------------------- the spelling help
def lexicon_arg(paths: Iterable[str] | None) -> Any:
    """The ``lexicon`` argument for word-list paths (``ResolverConfig.lexicon``): None -> ``"auto"`` (the system
    lists), else the list of paths."""
    return "auto" if paths is None else [str(p) for p in paths]


def word_list_problem(vocab: Vocabulary, lexicon: Any = "auto") -> str | None:
    """Why the spelling help must stay off for this vocabulary, or None: without an English word list ordinary words
    are taken for typos (docs/resolver.md, "Controls"), so a resolver is not built without one."""
    if (vocab.info.get("lexicon") or {}).get("words"):
        return None
    where = ", ".join(LEXICON_PATHS) if lexicon == "auto" else \
        ", ".join(map(str, [lexicon] if isinstance(lexicon, (str, Path)) else (lexicon or []))) or "none given"
    return f"no English word list could be read ({where}; set SFA_LEXICON): ordinary words would be taken for typos"


def spelling_status(resolver: Resolver, scope: str, seconds: float) -> list[str]:
    """Start-up lines of a built resolver: ``spelling help on (<scope>): <n> names (object .., field .., ...), word
    list <n> words (sha <word set hash>), <s> s``, then a WARNING line for snapshot statements that were cut (``truncated``) or failed
    (``query_errors``). Counts only: no name or value is printed."""
    kinds: dict[str, int] = {}
    for t in resolver.terms:
        kinds[t.kind] = kinds.get(t.kind, 0) + 1
    parts = ", ".join(f"{k} {kinds[k]:,}" for k in KINDS if kinds.get(k))
    lex = resolver.info.get("lexicon") or {}
    words = lex.get("words", 0)
    sha = f" (sha {lex['sha']})" if lex.get("sha") else ""
    lines = [f"spelling help on ({scope}): {len(resolver.terms):,} names ({parts}), word list {words:,} words{sha}, "
             f"{seconds:.1f} s"]
    snap = resolver.info.get("snapshot") or {}
    if snap.get("truncated"):
        lines.append(f"WARNING spelling help: {snap['truncated']} snapshot statement(s) hit the row or size limit; "
                     "some stored values or names are left out")
    if snap.get("query_errors"):
        lines.append(f"WARNING spelling help: {snap['query_errors']} snapshot statement(s) failed; their fields are "
                     "left out")
    return lines


def knowledge_resolver(knowledge: Any, lexicon: Any = "auto") -> tuple[Resolver | None, list[str]]:
    """``(resolver, start-up lines)`` of the knowledge (a folder or parsed documents) and the word list: the knowledge
    resolver of docs/resolver_integration.md ("Build"), for the knowledge tools, the knowledge search and the
    records-off hints. Never raises: on any problem the resolver is None and the one line says
    ``spelling help off: <reason>``."""
    t0 = time.perf_counter()
    try:
        vocab = knowledge_vocabulary(knowledge, lexicon)
        problem = word_list_problem(vocab, lexicon)
        if problem:
            return None, [f"spelling help off: {problem}"]
        resolver = build(vocab)
    except Exception as exc:  # noqa: BLE001 - the spelling help is optional, never fatal
        first = (str(exc).splitlines() or [""])[0][:300]
        return None, [f"spelling help off: the knowledge vocabulary could not be built: {type(exc).__name__}: {first}"]
    return resolver, spelling_status(resolver, "knowledge files", time.perf_counter() - t0)
