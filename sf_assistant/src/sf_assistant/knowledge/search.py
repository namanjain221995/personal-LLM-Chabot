"""BM25 keyword search over knowledge chunks.

Uses ``rank_bm25.BM25Okapi`` when the package is installed and falls back to a small
built-in Okapi BM25 implementation otherwise, so results never depend on an optional
dependency being present (the scores differ slightly between the two, rankings rarely do).

Tokenisation is tuned for Salesforce metadata: API names are split on camelCase and
underscores, the ``__c`` suffix is dropped, text is lower-cased, stopwords are removed and
a light plural stemmer folds ``interviews`` onto ``interview``.

Spelling help (docs/resolver_integration.md (2)): ``KnowledgeIndex.expand`` is None by default, and then every search
is exactly as without it. With an expander (:func:`resolver_expander`, attached by the knowledge tools'
``attach_resolver`` when ``SFA_RESOLVER=on``), a query with a token that no chunk has is looked up by the term
resolver, and the index words of its reading are added to the query once each (``backgrond`` -> ``background``);
:meth:`KnowledgeIndex.search_read_as` also says what was added. A query whose tokens are all index words never
reaches the expander.
"""

from __future__ import annotations

import difflib
import math
import re
from collections import Counter
from typing import Any, Callable, Iterable

__all__ = ["KnowledgeIndex", "tokenize", "STOPWORDS"]

#: The term kinds that are metadata (no person name): the expander of a record catalog's indexes keeps to these.
METADATA_KINDS: tuple[str, ...] = ("object", "field", "value", "record_type", "component")
#: Readings one expander keeps (the knowledge tools search the same query several times: search_fields per section).
EXPAND_CACHE_MAX = 256

#: ``(query, unknown tokens) -> [(typed text, [texts whose tokens to add])]``: see :func:`resolver_expander`.
Expander = Callable[[str, list], list]

STOPWORDS: frozenset[str] = frozenset(
    """a an and are as at be been being by can could did do does for from had has have how i if in
    into is it its may me might my of on or our should so than that the their them then there these
    they this those to was we were what when where which while who whom whose why will with would you
    your about any all also each just only other over such via per not no yes c r""".split()
)

_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_SUFFIX_RE = re.compile(r"__(c|r|mdt|e|x|b)\b", re.IGNORECASE)


def _stem(tok: str) -> str:
    """Very light plural folding; deliberately conservative."""
    if len(tok) > 4 and tok.endswith("ies"):
        return tok[:-3] + "y"
    if len(tok) > 3 and tok.endswith("s") and not tok.endswith(("ss", "us", "is")):
        return tok[:-1]
    return tok


def tokenize(text: str) -> list[str]:
    """Split text (including API names) into normalised search tokens."""
    text = _SUFFIX_RE.sub(" ", text or "")
    out: list[str] = []
    for raw in _SPLIT_RE.split(text):
        if not raw:
            continue
        for part in _CAMEL_RE.sub(" ", raw).split():
            tok = part.lower()
            if tok in STOPWORDS or len(tok) < 2 and not tok.isdigit():
                continue
            out.append(_stem(tok))
    return out


class _SimpleBM25:
    """Okapi BM25 used when ``rank_bm25`` is not importable."""

    def __init__(self, corpus: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.docs = [Counter(d) for d in corpus]
        self.lens = [len(d) for d in corpus]
        self.avgdl = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        n = len(corpus)
        df: Counter[str] = Counter()
        for d in corpus:
            df.update(set(d))
        # BM25+ style idf that is always positive.
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def get_scores(self, query: list[str]) -> list[float]:
        scores = []
        for doc, dl in zip(self.docs, self.lens):
            s = 0.0
            for t in query:
                tf = doc.get(t, 0)
                if not tf:
                    continue
                denom = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1.0))
                s += self.idf.get(t, 0.0) * tf * (self.k1 + 1) / denom
            scores.append(s)
        return scores


def _make_bm25(corpus: list[list[str]]):
    try:  # optional dependency
        from rank_bm25 import BM25Okapi  # type: ignore

        return BM25Okapi(corpus), "rank_bm25"
    except Exception:  # ImportError or a broken install
        return _SimpleBM25(corpus), "builtin"


class KnowledgeIndex:
    """BM25 index over ``ObjectKnowledge.chunks()``.

    The indexed document for each chunk is ``title`` (counted twice, as a boost) +
    ``text`` + the chunk id (so API names in ids are searchable).
    """

    def __init__(self, chunks: list[dict]) -> None:
        self.chunks: list[dict] = [dict(c) for c in chunks]
        self._docs: list[list[str]] = [
            tokenize(f"{c.get('title', '')} {c.get('title', '')} {c.get('text', '')} {c.get('id', '')}")
            for c in self.chunks
        ]
        self._bm25, self.backend = _make_bm25(self._docs) if self._docs else (None, "empty")
        self._by_id = {c["id"]: c for c in self.chunks}
        self.words: set[str] = set().union(*self._docs)   # every indexed token: no other query token can score
        self.expand: Expander | None = None   # the spelling help (resolver_expander); None: searches as without it

    def sections(self) -> list[str]:
        """Distinct section names, in first-seen order."""
        return list(dict.fromkeys(c.get("section", "") for c in self.chunks))

    def get(self, chunk_id: str) -> dict | None:
        c = self._by_id.get(chunk_id)
        return dict(c) if c else None

    def search(self, query: str, k: int = 8, section: str | None = None) -> list[dict]:
        """Return up to ``k`` chunks most relevant to ``query``, highest score first.

        Each result is a copy of the chunk dict plus ``"score": float``. Chunks with zero
        score are dropped. ``section`` restricts results to one section; it matches the
        chunk's ``section`` or the id prefix before ``:`` (so ``"child"`` and
        ``"child_relationship"`` both work), case-insensitively. With an expander
        (:attr:`expand`), a misspelt query word is searched as it reads (:meth:`search_read_as`).
        """
        return self.search_read_as(query, k, section)[0]

    def search_read_as(self, query: str, k: int = 8, section: str | None = None) -> tuple[list[dict], dict[str, str]]:
        """``(hits, read_as)``: the hits of :meth:`search` and ``{typed text: the words added for it}``.

        Without an expander (:attr:`expand` None), or when every query token is an index word, the expander is never
        called, ``read_as`` is ``{}`` and the hits are exactly those of a search without the spelling help. Otherwise
        the expander reads the tokens no chunk has (:func:`resolver_expander`), and the index words of its readings
        that the query lacks are added to the query once each; ``read_as`` lists them per typed text. Never raises
        because of the expander (a failing one adds nothing).
        """
        q = tokenize(query)
        read_as: dict[str, str] = {}
        if q and self.expand is not None and self._bm25 is not None:
            q, read_as = self._expand_query(query, q)
        if not q or self._bm25 is None:
            return [], read_as
        scores = list(self._bm25.get_scores(q))
        # Small bonus for chunks whose id contains a query token verbatim (API name hits).
        qset = set(q)
        for i, c in enumerate(self.chunks):
            if scores[i] > 0 and qset & set(tokenize(c["id"])):
                scores[i] *= 1.15
        sec = section.lower().strip() if section else None
        ranked = []
        for i, c in enumerate(self.chunks):
            if scores[i] <= 0:
                continue
            if sec:
                prefix = c["id"].split(":", 1)[0].split(".", 1)[0].lower()
                if sec not in (str(c.get("section", "")).lower(), prefix):
                    continue
            ranked.append((scores[i], i))
        ranked.sort(key=lambda t: (-t[0], t[1]))
        out = []
        for score, i in ranked[: max(1, int(k))]:
            item = dict(self.chunks[i])
            item["score"] = round(float(score), 4)
            out.append(item)
        return out, read_as

    def _expand_query(self, query: str, q: list[str]) -> tuple[list[str], dict[str, str]]:
        """``q`` plus the index words of the expander's readings of its unknown tokens, and the ``read_as`` map."""
        unknown = [t for t in dict.fromkeys(q) if t not in self.words]
        if not unknown:
            return q, {}
        try:
            pairs = list(self.expand(str(query), unknown) or [])   # type: ignore[misc]
        except Exception:  # noqa: BLE001 - the spelling help is optional: the query stays as typed
            pairs = []
        have = set(q)
        added: list[str] = []
        read_as: dict[str, str] = {}
        for typed, texts in pairs:
            new = [t for t in dict.fromkeys(tokenize(" ".join(str(x) for x in texts or ())))
                   if t in self.words and t not in have]
            if not new:
                continue
            have.update(new)
            added += new
            read_as[str(typed)] = " ".join(new)
        return (q + added, read_as) if added else (q, {})


# ------------------------------------------------------------------------------------------------- spelling help
def _closest_words(typed: str, texts: list[str]) -> list[str]:
    """``texts`` cut to one word per typed word: a reading may spell a longer term (``ereference`` read inside
    ``Candidate Preference``), and only the words the typed ones stand for belong in the search (the closest one to
    each typed word, in the reading's order)."""
    want = tokenize(typed)
    words = [t for t in dict.fromkeys(tokenize(" ".join(texts)))]
    if not want or len(words) <= len(want):
        return texts
    keep: set[str] = set()
    for t in want:
        left = [w for w in words if w not in keep]
        if left:
            keep.add(max(left, key=lambda w: (difflib.SequenceMatcher(None, t, w).ratio(), -words.index(w))))
    return [" ".join(w for w in words if w in keep)]


def _expansion_texts(decision: str, suggestion: str | None, suggestions: Iterable[str], candidates: Iterable[Any],
                     kinds: frozenset[str] | None, missing: Iterable[str] = ()) -> list[str]:
    """What a resolver reading adds to a search: ``corrected`` -> its spelling, without the ``missing`` words (the
    words of the matched term the typed text does not stand for: ``ereference`` read as part of a longer term adds
    that word only); ``ambiguous`` -> the words every suggestion shares (``persn`` -> ``person``), or nothing;
    ``exact`` / ``none`` -> nothing. With ``kinds``, only readings with a candidate of these kinds count (a person
    name is no search word of a catalog index)."""
    cands = [c for c in candidates or () if getattr(c, "kind", None)]
    if kinds is not None and cands:
        allowed = {str(c.text).lower() for c in cands if c.kind in kinds}
        if not allowed:
            return []
    else:
        allowed = None
    if decision == "corrected" and suggestion:
        drop = set(tokenize(" ".join(str(w) for w in missing or ())))
        words = [t for t in tokenize(str(suggestion)) if t not in drop]
        return [" ".join(words)] if words else []
    if decision != "ambiguous":
        return []
    sugg = [str(s) for s in suggestions or () if s]
    if allowed is not None:
        sugg = [s for s in sugg if s.lower() in allowed]
    if not sugg:
        return []
    first = tokenize(sugg[0])
    shared = set(first)
    for s in sugg[1:]:
        shared &= set(tokenize(s))
    words = [t for t in dict.fromkeys(first) if t in shared]
    return [" ".join(words)] if words else []


def resolver_expander(resolver: Any, kinds: Iterable[str] | None = None) -> Expander:
    """The expander of :attr:`KnowledgeIndex.expand` over a term resolver (``sf_assistant.knowledge.resolver.
    Resolver``, duck-typed: ``spell_correct`` and ``resolve_term``).

    First ``spell_correct(query)``: each span that covers an unknown token gives its reading (a run of words such as
    ``backgrond chech`` is read as one term). Then ``resolve_term(token, wide=False)`` for each unknown token no span
    covered and that is no correct word (``is_known_word``: an English or knowledge word, even one this index lacks):
    the strict scoring of the question hints, because a search word is no tool argument. A ``corrected``
    reading adds its spelling, an ``ambiguous`` one only the words all its suggestions share, ``exact`` and ``none``
    nothing; a reading never adds more words than were typed (the closest one each, :func:`_closest_words`).
    ``kinds``: only readings with a candidate of these kinds (the catalog's indexes: :data:`METADATA_KINDS`); None (the
    knowledge tools' resolver, which has no person and no stored value): every kind. Read-only, so one expander can
    serve every thread."""
    ks = frozenset(kinds) if kinds is not None else None
    cache: dict[tuple[str, tuple[str, ...]], list[tuple[str, list[str]]]] = {}
    is_known = getattr(resolver, "is_known_word", None)

    def _known(tok: str) -> bool:
        """A correct English or knowledge word (``Resolver.is_known_word``) is no typo, even when this index lacks it:
        the catalog's object and field indexes are small, so many right words of a question are not in them
        ("receive", "resulted", "core" were read as "received", "result", "score")."""
        try:
            return bool(is_known(tok)) if is_known is not None else False
        except Exception:  # noqa: BLE001 - the spelling help is optional
            return False

    def expand(query: str, unknown: list) -> list[tuple[str, list[str]]]:
        key = (query, tuple(unknown))
        hit = cache.get(key)
        if hit is None:
            hit = read(query, unknown)
            if len(cache) >= EXPAND_CACHE_MAX:
                cache.clear()
            cache[key] = hit
        return [(typed, list(texts)) for typed, texts in hit]

    def read(query: str, unknown: list) -> list[tuple[str, list[str]]]:
        want = set(unknown)
        covered: set[str] = set()
        out: list[tuple[str, list[str]]] = []
        for c in resolver.spell_correct(query):
            toks = set(tokenize(c.text))
            if not toks & want:
                continue
            covered |= toks
            texts = _expansion_texts(c.decision, c.suggestion, c.suggestions, c.candidates, ks)
            if texts:
                out.append((c.text, _closest_words(c.text, texts)))
        for tok in unknown:
            if tok in covered or _known(tok):
                continue
            res = resolver.resolve_term(tok, kinds=sorted(ks) if ks is not None else None, wide=False)
            texts = _expansion_texts(res.decision, res.suggestion, res.suggestions, res.candidates, ks,
                                     getattr(res, "missing_words", ()))
            if texts:
                out.append((tok, _closest_words(tok, texts)))
        return out

    return expand

