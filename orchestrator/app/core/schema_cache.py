"""DuckDB schema cache used to ground SQL generation prompts.

duckdb is imported lazily and the database is always opened read_only.
"""
from __future__ import annotations

import re
import time
from typing import Dict, List, Sequence, Tuple

#: How long a schema read waits for the sync worker's write lock. The worker
#: is mid-cycle for a large part of every interval, so this is the difference
#: between answering from the warehouse and falling through to live Salesforce.
#: Kept slightly above engines/sql.py's execution wait: the schema read happens
#: first, and giving up here wastes the executor's patience too.
LOCK_WAIT_SECONDS = 6.0
LOCK_WAIT_STEP = 0.25


class SchemaCache:
    """TTL cache of {table: [(column, type), ...]} keyed by database path."""

    def __init__(self, ttl_seconds: float = 300.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[float, Dict[str, List[Tuple[str, str]]]]] = {}

    def get(self, db_path: str, force_refresh: bool = False) -> Dict[str, List[Tuple[str, str]]]:
        now = time.monotonic()
        hit = self._cache.get(db_path)
        if hit is not None and not force_refresh and (now - hit[0]) < self.ttl_seconds:
            return hit[1]
        try:
            schema = self._load(db_path)
        except Exception:
            # A refresh can fail transiently — most commonly the sync-worker
            # holding the file's write lock for a batch. The schema changes
            # rarely; a stale copy grounds the SQL prompt far better than an
            # error. Only ever raise when there is nothing cached at all.
            if hit is not None:
                return hit[1]
            raise
        self._cache[db_path] = (now, schema)
        return schema

    def invalidate(self, db_path: str | None = None) -> None:
        if db_path is None:
            self._cache.clear()
        else:
            self._cache.pop(db_path, None)

    @staticmethod
    def _load(db_path: str) -> Dict[str, List[Tuple[str, str]]]:
        import duckdb  # lazy

        # Same lockdown config as the sql engine's _execute: introspection
        # needs no external access, and DuckDB rejects concurrent
        # connections to one file whose configs differ.
        #
        # And the same lock wait. The sync worker writes across 1,023 objects
        # almost continuously — a read connect fails roughly half the time —
        # so connecting once and giving up sent the question to live
        # Salesforce, where it was answered off the wrong object entirely.
        # engines/sql.py:_execute already waited; this did not, which is why
        # the failure looked intermittent and unrelated to SQL.
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                con = duckdb.connect(
                    db_path,
                    read_only=True,
                    config={
                        "enable_external_access": False,
                        "autoinstall_known_extensions": False,
                        "autoload_known_extensions": False,
                    },
                )
                break
            except duckdb.Error as exc:
                if "lock" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(LOCK_WAIT_STEP)
        try:
            rows = con.execute(
                "SELECT table_name, column_name, data_type "
                "FROM information_schema.columns "
                "WHERE table_schema = 'main' "
                "ORDER BY table_name, ordinal_position"
            ).fetchall()
        finally:
            con.close()

        schema: Dict[str, List[Tuple[str, str]]] = {}
        for table, column, dtype in rows:
            schema.setdefault(table, []).append((column, dtype))
        return schema


def format_schema(schema: Dict[str, List[Tuple[str, str]]]) -> str:
    """Render a schema dict as compact `table(col TYPE, ...)` lines."""
    lines = []
    for table, cols in schema.items():
        cols_txt = ", ".join(f"{c} {t}" for c, t in cols)
        lines.append(f"{table}({cols_txt})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Grounding selection: the warehouse mirrors the WHOLE org (owner request,
# 2026-08-06) — hundreds of Share/History/Feed shadows and setup objects
# alongside the business tables. Dumping 900+ tables into every SQL prompt
# would bury the ~160 the model actually needs, so grounding is selective:
# business tables always, everything else only when the question asks for it.
# ---------------------------------------------------------------------------

#: Shadow suffixes: per-record plumbing generated from a base object.
_SHADOW_SUFFIXES = ("Share", "History", "Feed", "ChangeEvent", "__hd")

#: Standard objects that hold this org's business data (matches the curated
#: sync import). Custom objects (__c) are recognized by suffix instead.
_STANDARD_BUSINESS = frozenset({
    "Account", "AccountContactRelation", "Asset", "Campaign", "CampaignMember",
    "Case", "CaseComment", "CaseStatus", "Contact", "ContentDocument",
    "ContentVersion", "Contract", "EmailMessage", "Event", "Group", "Lead",
    "LeadStatus", "Note", "Opportunity", "OpportunityContactRole",
    "OpportunityLineItem", "OpportunityStage", "Order", "OrderItem",
    "Pricebook2", "PricebookEntry", "Product2", "Profile", "Quote",
    "QuoteLineItem", "RecordType", "SocialPersona", "SocialPost", "Task",
    "TaskStatus", "User", "UserRole",
})

#: At most this many question-matched non-business tables join the prompt —
#: a question containing "share" must not drag in 500 Share shadows.
_MAX_MATCHED_EXTRAS = 40

#: How many tables the prompt may carry in total.
#:
#: Measured before this cap existed: "how many candidates completed the
#: training from slot 128 and how many failed the mock in that" shipped 146
#: tables / 5,090 columns / 141,978 characters — about 41,000 tokens, of which
#: the five tables the question needed accounted for 11,357. The model then had
#: to find the question inside the haystack on a 6,000-token answer budget, and
#: intermittently spent that budget reasoning and returned NOTHING. Empty SQL
#: reads to the engine as "not in the warehouse", which sends the question to
#: live Salesforce, which answers it off whatever object the field dictionary
#: suggested. Every wrong answer of that shape starts here.
_MAX_TABLES = 40  # was 24 (2026-08-18)
#: Raised deliberately, not removed. The 146-table prompt that motivated the
#: original cap was ~41k tokens; 40 tables of this warehouse is ~7k against a
#: 237k usable input budget, so the cost is negligible — but "send everything"
#: is still wrong: a model that cannot FIND the question in the prompt writes
#: the wrong query, and that failure is silent.

#: Columns per table before trimming kicks in (see `_column_budget`).
_MAX_COLUMNS = 140  # was 70 (2026-08-18)
#: Internal_Interview__c alone has 66 columns and Account 269; at 70 a wide
#: object lost the very field a question named. Keys are never dropped either
#: way (see _trim_columns).

#: Never trimmed away — a correct query needs these whether or not the
#: question mentions them.
_KEY_COLUMNS = frozenset({
    "id", "name", "recordtypeid", "createddate", "lastmodifieddate",
    "isdeleted", "ownerid", "systemmodstamp",
})

#: Always present regardless of the question. RecordType is the important one:
#: nearly every correct query in this org joins it to separate candidates from
#: recruiters, or interviews from initial calls, and the question never says
#: the word "record type".
_ALWAYS = ("RecordType",)


def _is_business_table(table: str) -> bool:
    if table in _STANDARD_BUSINESS:
        return True
    return table.endswith("__c") and not table.endswith(
        ("__Share", "__History", "__Feed")
    )


def _stem(word: str) -> str:
    """Crude singularisation, applied to both sides of every comparison.

    Without it "how many interviews" scored Interview__c at zero and the
    ranking fell back to alphabetical order, handing the model
    AccountContactRelation and Campaign for a question about interviews.
    Same rule as core/sf_dictionary._stem.
    """
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _table_tokens(table: str) -> set:
    return {
        _stem(t.lower())
        for t in re.split(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])", table)
        if t
    }


#: Per-record plumbing. Same list as core/sf_dictionary._SHADOW_SUFFIXES.
_SHADOW_SUFFIXES = ("Share", "History", "Feed", "ChangeEvent", "__hd")
_SHADOW_WORDS = {"share", "history", "feed", "changeevent", "change"}


def _column_budget(cols: Sequence[Tuple[str, str]], words: set) -> List[Tuple[str, str]]:
    """Trim a very wide table to the columns this question could plausibly use.

    Account has 269 columns and Interview__c 265. Sending every one of them for
    a question that touches three is what made the prompt unreadable. Keys and
    audit columns always stay — a query needs Id and RecordTypeId whether or
    not the question says so.
    """
    if len(cols) <= _MAX_COLUMNS:
        return list(cols)
    keep: List[Tuple[str, str]] = []
    rest: List[Tuple[str, str]] = []
    for column, dtype in cols:
        lowered = column.lower()
        if (
            lowered in _KEY_COLUMNS
            or words & _table_tokens(column)
            or lowered.endswith(("__c", "date", "status"))
            and words & _table_tokens(column)
        ):
            keep.append((column, dtype))
        else:
            rest.append((column, dtype))
    # Top up to the budget so a question that matched few columns still sees a
    # usable table rather than four keys.
    return (keep + rest)[:_MAX_COLUMNS]


def relevant_schema(
    schema: Dict[str, List[Tuple[str, str]]],
    question: str,
    must_include: Sequence[str] = (),
) -> Dict[str, List[Tuple[str, str]]]:
    """The slice of the warehouse worth grounding this question on.

    Being a business table earns a table CANDIDACY, not a place. Including all
    of them shipped 146 tables and ~41k tokens for a five-table question, and a
    model that cannot find the question in the prompt writes the wrong query or
    none at all — see `_MAX_TABLES`.

    So candidates are ranked and the best `_MAX_TABLES` are kept:
      * tables the question names outright score highest,
      * then tables whose COLUMNS match the question's words,
      * `must_include` (the tables a matched metric needs) is never dropped,
      * `_ALWAYS` (RecordType) is never dropped.

    Shadow and setup tables still have to be named to appear at all: short
    words (4-5 chars) must equal a whole name token — "this" must not light up
    Accoun[this]tory — while longer words also match as substrings of the
    collapsed name, so "permissionset" finds PermissionSet.
    """
    words = {w for w in re.findall(r"[a-z0-9_]+", question.lower()) if len(w) >= 4}

    def named(table: str) -> bool:
        tokens = _table_tokens(table)
        collapsed = re.sub(r"[^a-z0-9]", "", table.lower())
        return any(w in tokens or (len(w) >= 6 and w in collapsed) for w in words)

    def score(table: str, cols: Sequence[Tuple[str, str]]) -> int:
        # Naming the table is worth more than sharing a column name with it:
        # "slot" must pull Cohort__c ahead of every table with a Status column.
        points = 8 * len(words & _table_tokens(table))
        for column, _dtype in cols:
            if words & _table_tokens(column):
                points += 1
        return points

    # A shadow only appears when the question asks for one. Without this,
    # "training" pulled in Candidate_Training__History alongside the real
    # table and spent a slot on it.
    wants_shadow = bool({_stem(w) for w in words} & {_stem(w) for w in _SHADOW_WORDS})

    pinned = {t for t in (*_ALWAYS, *must_include) if t in schema}
    candidates: Dict[str, List[Tuple[str, str]]] = {}
    extras = 0
    for table, cols in schema.items():
        if table.startswith("_"):
            continue  # _sync_meta and friends: internal bookkeeping
        if table.endswith(_SHADOW_SUFFIXES) and not wants_shadow:
            continue
        if _is_business_table(table):
            candidates[table] = cols
        elif extras < _MAX_MATCHED_EXTRAS and named(table):
            candidates[table] = cols
            extras += 1

    keep = {t: schema[t] for t in pinned}
    room = _MAX_TABLES - len(keep)
    if room > 0:
        ranked = sorted(
            ((score(t, c), t) for t, c in candidates.items() if t not in keep),
            key=lambda pair: (-pair[0], pair[1]),
        )
        # A question that matches nothing still needs something to read: fall
        # back to the widest business tables rather than an empty schema, which
        # would raise NoSuchTable and divert the question to live Salesforce.
        if not ranked or ranked[0][0] == 0:
            ranked = sorted(
                ((len(c), t) for t, c in candidates.items() if t not in keep),
                key=lambda pair: (-pair[0], pair[1]),
            )
        for _points, table in ranked[:room]:
            keep[table] = candidates[table]
    return {t: _column_budget(c, words) for t, c in keep.items()} or dict(schema)


schema_cache = SchemaCache()
