"""Turn a question into grounded Salesforce components.

This is the seam between the four build artifacts. The catalog knows what
exists, the graph knows what relates to what, the cards know how to describe
it, and the lexicon knows what your team calls it. Alone each is a file; joined
they answer "which object and which field does this question mean?".

Three outcomes, and the middle one matters most:

  resolved     one component clearly wins.
  ambiguous    several plausible candidates, or a term the lexicon holds under
               review. The caller is expected to ASK rather than pick. A wrong
               silent choice here is the failure mode the evaluation dataset
               exists to catch -- "placed" meaning Interview_Outcome__c =
               'Offer Received' or Candidate_Status__c = 'Placed' is not a
               coin flip the machine should call on its own.
  unresolved   nothing matched. Say so; do not invent an object.

Read-only throughout. Nothing here writes to Salesforce.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Any, Iterable

MAX_NGRAM = 4

# Hybrid scoring. The two signals are independent and fail differently: the
# lexicon knows vocabulary someone wrote down and is blind to paraphrase;
# similarity handles paraphrase and cannot know that "placed" means a
# particular picklist value. So neither is allowed to dominate, and agreement
# between them counts for more than either alone.
SEMANTIC_WEIGHT = 0.85      # a pure-similarity hit never outranks a curated one
AGREEMENT_BONUS = 0.08      # both signals found it
# Measured against this bundle on 2026-09-24, not guessed. Off-domain queries
# ("purple monkey dishwasher", "recipe for chocolate cake", "capital of
# France") top out at 0.567; real questions start at 0.695. 0.62 sits in the
# gap, so nonsense returns nothing instead of a confident-looking wrong field.
# Re-measure if the embedding model changes.
SEMANTIC_MIN = 0.62
SEMANTIC_POOL = 40          # how many similarity hits to consider

# How much of the final score the cross-encoder contributes. NOT 1.0, because
# the reranker is not strictly better than retrieval on this data -- measured
# 2026-09-24 it moved Recruiter__c.Active_For_Mock_Interview__c from rank 12 to
# rank 1 (a real win retrieval could not reach) and, on another query, promoted
# ActionableListMember from outside the top 14 to rank 1 by matching the word
# "members". Blending keeps a strong retrieval hit strong when the reranker is
# wrong, and lets a well-judged one climb. Treat the value as provisional until
# the 78-case dataset can measure it.
RERANK_WEIGHT = 0.5

# A parent object is only implied by a field the question named. Matching one
# of a field's VALUES is not naming the field: "training" is a value of
# Case_Tags__c, and deriving object:Case from that put Case above the training
# object the question was actually about.
PARENT_FROM_TIERS = {"curated", "user_choice", "generated_api", "generated_label"}

# When a question names an object and then describes fields on it -- "which
# fields on Interview__c store the date, start time and end time" -- the words
# "date", "start" and "end" belong to that object's own fields. Without this,
# retrieval matched the phrase "Interview__c" to the six foreign-key fields
# NAMED Interview__c on other objects and never looked inside the object the
# question was actually about.
FIELD_EXPANSION_MIN_OBJECT_SCORE = 0.85   # only from a confidently named object
FIELD_EXPANSION_BASE = 0.55               # below a directly matched field
FIELD_EXPANSION_PER_TOKEN = 0.06          # each extra matching word helps
FIELD_EXPANSION_MAX = 12                  # an object can have 257 fields

# A question that NAMES an object is asking about that object's fields. Without
# this, "fields on Interview__c storing the start time" ranked
# Availability__c.Start_Time__c, BatchJob.StartTime and Employee_Leave__c
# .StartTime__c above Interview__c.StartTime__c -- five identical names tied at
# the same score, ordered by nothing but the alphabet.
OBJECT_SCOPE_BONUS = 0.09

# The complement of the bonus. When a query NAMES an object outright, a field
# on some unrelated object is not a weaker answer, it is the wrong one:
# "fields on Interview__c storing the start time" should not return
# Availability__c.Start_Time__c or BatchJob.StartTime at all. Applied only
# when an object was named directly -- never from a derived parent, or every
# query would punish fields for belonging somewhere.
OUT_OF_SCOPE_PENALTY = 0.10
DIRECT_OBJECT_TIERS = {"curated", "user_choice", "generated_api", "generated_label",
                       "generated_plural", "schema_label"}

# How far below its field a derived parent sits. It was 0.01, which made an
# object merely IMPLIED by a field match nearly indistinguishable from one the
# question named outright: "fields on Interview__c" ranked Availability__c,
# BatchJob and BatchJobPart above Interview__c, because each owns a field
# called Start_Time__c and 0.95 - 0.01 beat the 0.94 of the named object.
DERIVED_PARENT_PENALTY = 0.12

# A multi-word hit is far better evidence than a single word: "mock interview"
# naming one object beats "interview" naming forty.
NGRAM_BONUS = 0.05

# Below this a candidate is noise rather than a suggestion.
MIN_CONFIDENCE = 0.45

# Two candidates this close together are not separable by score alone.
AMBIGUITY_MARGIN = 0.08

# Interrupting the user has a cost, so only a genuinely strong near-tie earns a
# question. A cluster of weak single-token hits is noise: "week" matching
# RefreshDayOfWeek and Week_Number__c is not an ambiguity anyone wants to
# arbitrate.
CLARIFY_MIN_SCORE = 0.6

# Words that carry time or quantity, not entity. They match field names by
# accident and would otherwise dominate an n-gram sweep.
QUERY_STOPWORDS = {
    "how", "many", "much", "what", "which", "who", "when", "where", "why",
    "show", "list", "give", "find", "get", "tell", "count", "total",
    "last", "next", "this", "past", "recent", "current", "today", "yesterday",
    "tomorrow", "day", "days", "week", "weeks", "month", "months", "year",
    "years", "quarter", "ago",
    # "date" and "time" are NOT here. They were, to stop "last week" style
    # noise, but the calendar words above already do that -- and a question
    # like "which fields store the interview date and start time" needs both
    # words to find Date_of_Interview__c and StartTime__c.
    "all", "any", "some", "the", "and", "for", "with", "from", "that", "have",
    "has", "had", "are", "was", "were", "been", "being", "our", "their",
    "is", "be", "am", "does", "do", "did", "will", "would", "can", "could",
    "should", "may", "might", "must", "of", "on", "in", "at", "to", "by",
    "a", "an", "or", "not", "no", "yes", "me", "my", "we", "us", "you",
    "happened", "happen", "there", "here", "about", "into", "over", "under",
}

_WORD = re.compile(r"[A-Za-z0-9_]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SUFFIX = re.compile(r"__(c|mdt|e|b|x|kav)$")


def _name_tokens(api_name: str) -> set[str]:
    """The words inside an API name: Date_of_Interview__c -> {date, of, interview}.

    Splits on underscores AND camel case. _WORD keeps underscores inside a
    token, which is right for matching an API name whole but wrong here: it
    returned {date_of_interview} as a single word, so a question saying "date"
    never overlapped with Date_of_Interview__c and the field was unreachable.
    """
    bare = _SUFFIX.sub("", api_name)
    return {w.lower() for w in re.split(r"[^A-Za-z0-9]+", _CAMEL.sub(" ", bare)) if w}


def _variants(phrase: str) -> list[str]:
    """The phrase, plus a singular reading of its last word.

    People type "mock interviews" and the lexicon holds "mock interview".
    Without this the curated entry is missed and the query silently falls
    through to a weaker, more general match -- which is worse than a miss,
    because it looks like an answer. Deliberately crude: English plurals are
    irregular, and a wrong singular simply fails to match rather than
    resolving to the wrong thing.
    """
    out = [phrase]
    head, _, last = phrase.rpartition(" ")
    for suffix, replacement in (("ies", "y"), ("ses", "s"), ("s", "")):
        if last.endswith(suffix) and len(last) > len(suffix) + 2:
            singular = last[: -len(suffix)] + replacement
            out.append(f"{head} {singular}".strip())
            break
    return out


class ResolverError(RuntimeError):
    """Raised when a bundle artifact is missing or unreadable."""


@dataclass
class Candidate:
    component_id: str
    kind: str
    api_name: str
    label: str | None
    score: float
    tier: str
    matched: str
    implied_filter: str | None = None
    source: str = ""
    note: str | None = None
    needs_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, "")}


@dataclass
class Resolution:
    surface: str
    status: str
    candidates: list[Candidate] = dataclass_field(default_factory=list)
    chosen: Candidate | None = None
    question: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"surface": self.surface, "status": self.status,
                               "candidates": [c.as_dict() for c in self.candidates]}
        if self.chosen:
            out["chosen"] = self.chosen.as_dict()
        if self.question:
            out["question"] = self.question
        return out


@dataclass
class Discovery:
    query: str
    resolutions: list[Resolution] = dataclass_field(default_factory=list)
    objects: list[Candidate] = dataclass_field(default_factory=list)
    fields: list[Candidate] = dataclass_field(default_factory=list)
    # Everything else the query reached: flows, permission sets, list views,
    # quick actions. "who can conduct mock interviews" is answered by a
    # permission set, not by an object, and splitting the result into two
    # buckets threw those away.
    other: list[Candidate] = dataclass_field(default_factory=list)
    needs_clarification: list[Resolution] = dataclass_field(default_factory=list)
    # Stage records in the shape query_trace_events expects. Emitted, never
    # persisted here: the bundle has no database handle and does not know
    # whether it is running in-process or behind HTTP.
    trace: list[Any] = dataclass_field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "objects": [c.as_dict() for c in self.objects],
            "candidate_fields": [c.as_dict() for c in self.fields],
            "other_components": [c.as_dict() for c in self.other],
            "resolutions": [r.as_dict() for r in self.resolutions],
            "needs_clarification": [r.as_dict() for r in self.needs_clarification],
            "trace": [e.as_dict() for e in self.trace],
        }


class Bundle:
    """Open the knowledge bundle once and answer questions against it.

    Connections are THREAD-LOCAL. A SQLite connection may only be used from the
    thread that created it, and any threaded caller -- an ASGI server running
    sync handlers in a worker pool, for one -- would otherwise fail on the
    second request with "SQLite objects created in a thread can only be used in
    that same thread". The vector cache is deliberately NOT thread-local: it is
    immutable once loaded and costs 122 ms and 20 MB, so every thread shares
    the one copy.
    """

    def __init__(self, directory: str | Path) -> None:
        base = Path(directory)
        required = ("catalog.sqlite", "lexicon.sqlite", "cards.sqlite")
        missing = [n for n in required if not (base / n).is_file()]
        if missing:
            raise ResolverError(
                f"{base}: missing {', '.join(missing)}. Run `graphrag catalog`, "
                f"`graphrag lexicon` and `graphrag cards` first.")
        self.base = base
        self.has_graph = (base / "graph.sqlite").is_file()
        self.vectors_path = base / "vectors.sqlite"
        self.has_vectors = self.vectors_path.is_file()
        self._vectors: list[tuple[str, tuple[float, ...]]] | None = None
        self._vectors_lock = threading.Lock()
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self.db  # open one now, so a broken bundle fails here, not mid-request

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{self.base / 'catalog.sqlite'}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        for name in ("lexicon", "cards"):
            connection.execute(f"ATTACH DATABASE ? AS {name}",
                               (str(self.base / f"{name}.sqlite"),))
        if self.has_graph:
            connection.execute("ATTACH DATABASE ? AS graph",
                               (str(self.base / "graph.sqlite"),))
        with self._connections_lock:
            self._connections.append(connection)
        return connection

    @property
    def db(self) -> sqlite3.Connection:
        connection = getattr(self._local, "db", None)
        if connection is None:
            connection = self._connect()
            self._local.db = connection
        return connection

    def close(self) -> None:
        with self._connections_lock:
            for connection in self._connections:
                try:
                    connection.close()
                except sqlite3.Error:
                    pass
            self._connections.clear()
        self._local = threading.local()

    def __enter__(self) -> "Bundle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- semantic search --------------------------------------------------
    def _load_vectors(self) -> list[tuple[str, tuple[float, ...]]]:
        """Read every vector once and keep it. Loading costs more than scanning."""
        if self._vectors is None:
            import struct
            with self._vectors_lock:
                if self._vectors is None:
                    db = sqlite3.connect(
                        f"file:{self.vectors_path}?mode=ro", uri=True)
                    try:
                        self._vectors = [
                            (row[0], struct.unpack(f"<{len(row[1]) // 4}f", row[1]))
                            for row in db.execute(
                                "SELECT component_id, data FROM vector")]
                    finally:
                        db.close()
        return self._vectors

    def semantic_search(self, query: str, limit: int = SEMANTIC_POOL,
                        endpoint: str | None = None) -> list[tuple[str, float]]:
        """Components whose card means something similar to the question.

        Stored vectors are unit length, so cosine similarity is a plain dot
        product. A brute-force scan over 4,484 vectors is exact and takes about
        120 ms; an approximate index would add a dependency and a build step to
        save time that the embedding API call spends anyway.
        """
        if not self.has_vectors:
            return []
        from .embed import embed_query
        kwargs = {"endpoint": endpoint} if endpoint else {}
        vector = embed_query(query, **kwargs)
        scored = [
            (component_id, sum(a * b for a, b in zip(vector, stored)))
            for component_id, stored in self._load_vectors()]
        scored.sort(key=lambda item: -item[1])
        return [(cid, score) for cid, score in scored[:limit] if score >= SEMANTIC_MIN]

    def rerank_candidates(self, query: str, candidates: list[Candidate],
                          top_n: int = 25, endpoint: str | None = None
                          ) -> list[Candidate]:
        """Reorder a shortlist by reading each card against the question."""
        from .rerank import CURATED_FLOOR, PINNED_TIERS, rerank

        pinned = [c for c in candidates if c.tier.split("+", 1)[0] in PINNED_TIERS]
        rest = [c for c in candidates if c.tier.split("+", 1)[0] not in PINNED_TIERS]
        shortlist, tail = rest[:top_n], rest[top_n:]
        if not shortlist:
            return candidates

        documents = []
        for candidate in shortlist:
            row = self.db.execute(
                "SELECT compact FROM cards.card WHERE component_id=?",
                (candidate.component_id,)).fetchone()
            documents.append(row[0] if row else candidate.component_id)

        kwargs = {"endpoint": endpoint} if endpoint else {}
        try:
            scored = rerank(query, documents, **kwargs)
        except Exception:
            # Reranking sharpens an order that is already usable. A sidecar
            # that is down, slow or restarting must cost the ranking it would
            # have improved, never the answer itself.
            return candidates
        reordered: list[Candidate] = []
        for item in scored:
            candidate = shortlist[item.index]
            # Blend, never replace: the retrieval score carries evidence the
            # cross-encoder never sees (that the lexicon and the embeddings
            # independently agreed, that a human curated the term).
            score = (candidate.score * (1.0 - RERANK_WEIGHT)
                     + item.score * RERANK_WEIGHT)
            if candidate.tier.split("+", 1)[0] == "curated":
                score = max(score, CURATED_FLOOR)
            candidate.score = round(score, 4)
            candidate.tier = f"{candidate.tier}+reranked"
            reordered.append(candidate)
        reordered.sort(key=lambda c: (-c.score, c.component_id))
        return pinned + reordered + tail

    # -- phrase extraction ------------------------------------------------
    @staticmethod
    def _ngrams(query: str) -> list[tuple[str, int]]:
        """Every phrase worth looking up, longest first."""
        words = [w.lower() for w in _WORD.findall(query)]
        words = [w for w in words if w not in QUERY_STOPWORDS]
        out: list[tuple[str, int]] = []
        for size in range(min(MAX_NGRAM, len(words)), 0, -1):
            for start in range(len(words) - size + 1):
                out.append((" ".join(words[start:start + size]), size))
        return out

    # -- lexicon lookup ---------------------------------------------------
    def _lookup(self, phrase: str, size: int) -> tuple[list[Candidate], list[Candidate]]:
        """Return (usable, held_for_review) candidates for one phrase."""
        forms = _variants(phrase)
        rows = self.db.execute(
            "SELECT e.surface, e.component_id, e.target_kind, e.tier, e.confidence,"
            "       e.implied_filter, e.note, e.source, e.needs_review,"
            "       c.api_name, c.label, c.kind"
            " FROM lexicon.entry e"
            " LEFT JOIN component c ON c.id = e.component_id"
            f" WHERE e.surface IN ({','.join('?' * len(forms))})"
            "   AND e.component_id IS NOT NULL",
            forms).fetchall()
        # Querying both the plural and singular form can return one component
        # twice, under two surfaces. Keep its best score, or a component ends
        # up listed as its own rival.
        usable_by_id: dict[str, Candidate] = {}
        held_by_id: dict[str, Candidate] = {}
        bonus = NGRAM_BONUS * (size - 1)
        for row in rows:
            candidate = Candidate(
                component_id=row["component_id"],
                kind=row["kind"] or row["target_kind"],
                api_name=row["api_name"] or "",
                label=row["label"],
                score=round(row["confidence"] + bonus, 4),
                tier=row["tier"],
                matched=phrase,
                implied_filter=row["implied_filter"],
                source=row["source"],
                note=row["note"],
                needs_review=bool(row["needs_review"]),
            )
            bucket = held_by_id if candidate.needs_review else usable_by_id
            current = bucket.get(candidate.component_id)
            if current is None or candidate.score > current.score:
                bucket[candidate.component_id] = candidate
        return list(usable_by_id.values()), list(held_by_id.values())

    # -- resolution -------------------------------------------------------
    def resolve(self, phrase: str, size: int = 1,
                choices: dict[str, str] | None = None) -> Resolution:
        # A term the user has already disambiguated is settled. Re-asking is
        # not caution, it is forgetting.
        if choices and phrase in choices:
            component_id = choices[phrase]
            row = self.db.execute(
                "SELECT kind, api_name, label FROM component WHERE id=?",
                (component_id,)).fetchone()
            if row is not None:
                chosen = Candidate(
                    component_id=component_id, kind=row["kind"],
                    api_name=row["api_name"], label=row["label"], score=1.0,
                    tier="user_choice", matched=phrase,
                    implied_filter=self._chosen_filter(phrase, component_id),
                    source="answered earlier in this conversation")
                return Resolution(surface=phrase, status="resolved",
                                  candidates=[chosen], chosen=chosen)

        usable, held = self._lookup(phrase, size)

        strong_enough = any(c.score >= CLARIFY_MIN_SCORE for c in usable)

        if held:
            # The lexicon knows this term and deliberately refuses to answer.
            # Surfacing the note is the whole point: the caller can ask the
            # user the same question a Salesforce owner would be asked.
            options = held + [c for c in usable if c.score >= MIN_CONFIDENCE]
            return Resolution(
                surface=phrase, status="ambiguous", candidates=options[:6],
                question=held[0].note or
                f'"{phrase}" has more than one meaning in this org. Which did you mean?')

        usable = [c for c in usable if c.score >= MIN_CONFIDENCE]
        usable.sort(key=lambda c: (-c.score, c.component_id))
        if not usable:
            return Resolution(surface=phrase, status="unresolved")

        best = usable[0]
        # Only a rival of the SAME kind is a competing meaning. A field named
        # Interview__c is a foreign key pointing at object:Interview__c, not a
        # second reading of the word -- the object boost already settles that,
        # and asking about it would interrupt on every ordinary noun. Two
        # objects (Invoice and Invoice__c) genuinely do compete.
        rivals = [c for c in usable[1:]
                  if c.kind == best.kind and best.score - c.score <= AMBIGUITY_MARGIN]
        # A curated entry is a human decision; it wins outright rather than
        # being second-guessed by a near-tie with a generated one. And a tie
        # among weak matches is not worth a question.
        if rivals and best.tier != "curated" and strong_enough:
            return Resolution(surface=phrase, status="ambiguous",
                              candidates=[best] + rivals[:5],
                              question=f'"{phrase}" could mean several things. Which did you mean?')
        return Resolution(surface=phrase, status="resolved",
                          candidates=usable[:6], chosen=best)

    def _expand_object_fields(self, query: str,
                              best: dict[str, Candidate]) -> None:
        """Admit fields OF a confidently identified object that the query names."""
        words = {w for w in (w.lower() for w in _WORD.findall(query))
                 if w not in QUERY_STOPWORDS and len(w) > 2}
        if not words:
            return
        objects = [c for c in list(best.values())
                   if c.kind == "object" and c.score >= FIELD_EXPANSION_MIN_OBJECT_SCORE]
        for parent in objects:
            scored: list[tuple[float, str, sqlite3.Row]] = []
            for row in self.db.execute(
                    "SELECT id, api_name, label FROM component"
                    " WHERE parent_id=? AND kind='field'", (parent.component_id,)):
                if row["id"] in best:
                    continue
                tokens = _name_tokens(row["api_name"])
                if row["label"]:
                    tokens |= {w.lower() for w in _WORD.findall(row["label"])}
                overlap = tokens & words
                # The object's own name is not evidence: every field on
                # Interview__c would otherwise match the word "interview".
                overlap -= _name_tokens(parent.api_name)
                if overlap:
                    scored.append((len(overlap), row["id"], row))
            scored.sort(key=lambda item: (-item[0], item[1]))
            for count, component_id, row in scored[:FIELD_EXPANSION_MAX]:
                best[component_id] = Candidate(
                    component_id=component_id, kind="field",
                    api_name=row["api_name"], label=row["label"],
                    score=round(min(0.88, FIELD_EXPANSION_BASE
                                    + FIELD_EXPANSION_PER_TOKEN * count), 4),
                    tier="object_field", matched=parent.api_name,
                    source=f"field of {parent.component_id}")

    def _apply_object_scope(self, best: dict[str, Candidate]) -> None:
        """Lift fields on an object the query named; push down fields elsewhere."""
        named = {c.component_id for c in best.values()
                 if c.kind == "object" and c.score >= FIELD_EXPANSION_MIN_OBJECT_SCORE}
        if not named:
            return
        directly_named = any(
            c.kind == "object" and c.tier.split("+", 1)[0] in DIRECT_OBJECT_TIERS
            and c.score >= FIELD_EXPANSION_MIN_OBJECT_SCORE
            for c in best.values())
        for candidate in best.values():
            if candidate.kind != "field":
                continue
            row = self.db.execute(
                "SELECT parent_id FROM component WHERE id=?",
                (candidate.component_id,)).fetchone()
            parent = row["parent_id"] if row is not None else None
            if parent in named:
                candidate.score = round(candidate.score + OBJECT_SCOPE_BONUS, 4)
                candidate.tier = f"{candidate.tier}+scoped"
            elif directly_named:
                candidate.score = round(
                    max(0.05, candidate.score - OUT_OF_SCOPE_PENALTY), 4)
                candidate.tier = f"{candidate.tier}+offscope"

    def _chosen_filter(self, phrase: str, component_id: str) -> str | None:
        """Carry the implied filter of a curated entry into a user's choice."""
        row = self.db.execute(
            "SELECT implied_filter FROM lexicon.entry"
            " WHERE surface=? AND component_id=? AND implied_filter IS NOT NULL LIMIT 1",
            (phrase, component_id)).fetchone()
        return row[0] if row else None

    # -- discover_schema --------------------------------------------------
    def discover_schema(self, query: str, limit: int = 10,
                        choices: dict[str, str] | None = None,
                        semantic: bool = False,
                        endpoint: str | None = None,
                        rerank: bool = False,
                        rerank_endpoint: str | None = None) -> Discovery:
        """Rank the components a question is probably about.

        Longest phrases win: once "mock interview" matches, its words are not
        looked up again on their own, so one strong phrase is not diluted by
        the forty objects that merely contain "interview".
        """
        from . import tracing

        started = time.perf_counter()
        found = Discovery(query=query)
        consumed: set[str] = set()
        best_by_component: dict[str, Candidate] = {}

        # Resolve every phrase of a given length before consuming any of them,
        # then take them strongest first. Consuming in positional order let an
        # accidental earlier phrase win: "is interview" matching IsInterview__c
        # swallowed the word "interview" before "interview outcome" was ever
        # tried, and the better match never surfaced.
        by_size: dict[int, list[tuple[str, Resolution]]] = {}
        for phrase, size in self._ngrams(query):
            resolution = self.resolve(phrase, size, choices)
            if resolution.status != "unresolved":
                by_size.setdefault(size, []).append((phrase, resolution))

        def strength(item: tuple[str, Resolution]) -> float:
            return max((c.score for c in item[1].candidates), default=0.0)

        for size in sorted(by_size, reverse=True):
            for phrase, resolution in sorted(by_size[size], key=strength, reverse=True):
                words = set(phrase.split())
                if words & consumed:
                    continue
                consumed |= words
                found.resolutions.append(resolution)
                if resolution.status == "ambiguous":
                    found.needs_clarification.append(resolution)
                for candidate in resolution.candidates:
                    current = best_by_component.get(candidate.component_id)
                    if current is None or candidate.score > current.score:
                        best_by_component[candidate.component_id] = candidate

        if semantic and self.has_vectors:
            for component_id, similarity in self.semantic_search(query, endpoint=endpoint):
                row = self.db.execute(
                    "SELECT kind, api_name, label FROM component WHERE id=?",
                    (component_id,)).fetchone()
                if row is None:
                    continue
                existing = best_by_component.get(component_id)
                if existing is not None:
                    # Both signals agree. That is better evidence than either
                    # alone, so the lexicon score is raised rather than replaced.
                    existing.score = round(min(1.0, existing.score + AGREEMENT_BONUS), 4)
                    existing.tier = f"{existing.tier}+semantic"
                    continue
                best_by_component[component_id] = Candidate(
                    component_id=component_id, kind=row["kind"],
                    api_name=row["api_name"], label=row["label"],
                    score=round(similarity * SEMANTIC_WEIGHT, 4),
                    tier="semantic", matched=query,
                    source=f"cosine {similarity:.3f}")

        # An object implies its fields, when the question used words that name
        # them. The reverse of the parent derivation below.
        self._expand_object_fields(query, best_by_component)
        self._apply_object_scope(best_by_component)

        # A field implies its object. "background check status" resolves to a
        # field, and any query built on that field still needs the object it
        # lives on -- so surface the parent rather than making the caller
        # re-derive it. Scored just below the field so it never outranks a
        # directly matched object.
        for candidate in list(best_by_component.values()):
            # Only from a field the lexicon actually matched. A semantic hit on
            # a list view should not inject that view's object at nearly the
            # same score -- the similarity was to the view, not the object.
            # The agreement bonus rewrites tier to "generated_api+semantic",
            # so compare the base tier, not the decorated one.
            base_tier = candidate.tier.split("+", 1)[0]
            if candidate.kind != "field" or base_tier not in PARENT_FROM_TIERS:
                continue
            row = self.db.execute(
                "SELECT p.id, p.api_name, p.label FROM component c"
                " JOIN component p ON p.id = c.parent_id WHERE c.id = ?",
                (candidate.component_id,)).fetchone()
            if row is None or row["id"] in best_by_component:
                continue
            best_by_component[row["id"]] = Candidate(
                component_id=row["id"], kind="object", api_name=row["api_name"],
                label=row["label"],
                score=round(candidate.score - DERIVED_PARENT_PENALTY, 4),
                tier="derived_parent", matched=candidate.matched,
                source=f"parent of {candidate.component_id}")

        resolve_ms = int((time.perf_counter() - started) * 1000)
        found.trace.append(tracing.entity_resolved(found.resolutions, resolve_ms))

        ranked = sorted(best_by_component.values(), key=lambda c: (-c.score, c.component_id))
        reranked = False
        if rerank and ranked:
            reranked = True
            ranked = self.rerank_candidates(query, ranked, endpoint=rerank_endpoint)
        found.objects = [c for c in ranked if c.kind == "object"][:limit]
        found.fields = [c for c in ranked if c.kind == "field"][:limit]
        found.other = [c for c in ranked
                       if c.kind not in ("object", "field")][:limit]
        found.trace.append(tracing.schema_discovered(
            found, duration_ms=int((time.perf_counter() - started) * 1000),
            semantic=semantic, reranked=reranked))
        return found

    # -- describe ---------------------------------------------------------
    def _card(self, component_id: str, column: str) -> str | None:
        row = self.db.execute(
            f"SELECT {column} FROM cards.card WHERE component_id = ?",
            (component_id,)).fetchone()
        return row[0] if row else None

    def _normalise(self, name: str) -> str | None:
        """Accept 'Interview__c', 'object:Interview__c' or 'Interview__c.Status__c'."""
        if ":" in name:
            return name if self.db.execute(
                "SELECT 1 FROM component WHERE id=?", (name,)).fetchone() else None
        for candidate in (f"object:{name}", f"field:{name}"):
            if self.db.execute("SELECT 1 FROM component WHERE id=?", (candidate,)).fetchone():
                return candidate
        row = self.db.execute(
            "SELECT id FROM component WHERE api_name = ? COLLATE NOCASE"
            " ORDER BY CASE kind WHEN 'object' THEN 0 ELSE 1 END LIMIT 1",
            (name,)).fetchone()
        return row[0] if row else None

    def describe_object(self, name: str,
                        trace: list[Any] | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        component_id = self._normalise(name)
        if component_id is None:
            payload = {"found": False, "requested": name,
                       "reason": "no component with that API name in this org"}
            if trace is not None:
                from . import tracing
                trace.append(tracing.component_described(
                    name, payload, int((time.perf_counter() - started) * 1000)))
            return payload
        row = self.db.execute("SELECT * FROM component WHERE id=?", (component_id,)).fetchone()
        out: dict[str, Any] = {
            "found": True,
            "component_id": component_id,
            "kind": row["kind"],
            "api_name": row["api_name"],
            "label": row["label"],
            "plural_label": row["plural_label"],
            "description": row["description"],
            "is_custom": bool(row["is_custom"]),
            "is_stub": bool(row["is_stub"]),
            "properties": json.loads(row["properties"]),
            "source_path": row["source_path"],
            "detail": self._card(component_id, "detail"),
        }
        if row["kind"] == "object":
            out["fields"] = [
                {"api_name": r["api_name"], "label": r["label"],
                 "type": json.loads(r["properties"]).get("type"),
                 "description": r["description"]}
                for r in self.db.execute(
                    "SELECT api_name, label, description, properties FROM component"
                    " WHERE parent_id=? AND kind='field' ORDER BY api_name", (component_id,))]
            out["record_types"] = [r[0] for r in self.db.execute(
                "SELECT api_name FROM component WHERE parent_id=? AND kind='record_type'"
                " ORDER BY api_name", (component_id,))]
            out["validation_rules"] = [r[0] for r in self.db.execute(
                "SELECT api_name FROM component WHERE parent_id=? AND kind='validation_rule'"
                " ORDER BY api_name", (component_id,))]
        values = [dict(value=r["value"], label=r["label"]) for r in self.db.execute(
            "SELECT value, label FROM picklist_value WHERE field_id=? ORDER BY value",
            (component_id,))]
        if values:
            out["picklist_values"] = values
        if self.has_graph:
            out["relationships"] = self._relationships(component_id)
        if trace is not None:
            from . import tracing
            trace.append(tracing.component_described(
                component_id, out, int((time.perf_counter() - started) * 1000)))
        return out

    describe_metadata = describe_object

    def _relationships(self, component_id: str) -> dict[str, Any]:
        outbound = [dict(kind=r["kind"], target=r["target"]) for r in self.db.execute(
            "SELECT kind, target FROM graph.edge_ids WHERE source=? AND tier=1"
            " AND kind IN ('lookup_to','master_detail_to','parent_of','targets',"
            "'triggers_on','for_object') ORDER BY kind, target", (component_id,))]
        inbound = [dict(kind=r["kind"], source=r["source"]) for r in self.db.execute(
            "SELECT kind, source FROM graph.edge_ids WHERE target=? AND tier=1"
            " AND kind IN ('lookup_to','master_detail_to','writes_field','reads_field',"
            "'references_field','displays_field') ORDER BY kind, source LIMIT 60",
            (component_id,))]
        return {"outbound": outbound, "inbound": inbound}
