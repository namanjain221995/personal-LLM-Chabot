"""The resolver's vocabulary, built from data only (docs/resolver.md): no hand-written lists of business words.

The knowledge and word-list part lives one layer down, in :mod:`sf_assistant.knowledge.terms` (the knowledge side
builds its own resolver from it without engine code); this module adds the snapshot part and re-exports
:func:`load_knowledge`, :func:`add_knowledge`, :func:`load_lexicon` and :data:`LEXICON_PATHS`.

:func:`build_vocabulary` reads

* the knowledge files (``<knowledge_dir>/*.json``, one object each; ``*.raw.json`` skipped): objects (API name, label,
  plural label), fields (API name, label), picklist values and labels per field, record types (developer name,
  label), components (flows, Apex, validation rules, business processes), and every word of the knowledge text as a
  known-correct word (never corrected, never suggested);
* a generic English word list (``lexicon``; by default the system lists ``/usr/share/dict/american-english`` and
  ``british-english`` when present): known-correct words, so ordinary words (``male``, ``gender``, ``trainees``) are
  never "corrected" into vocabulary;
* optionally the snapshot (``snapshot``): stored values per field and person names. The snapshot is read through a
  query function or an open DuckDB connection or cursor that the caller owns (in the app: the record warehouse's
  guarded, read-only connection and its catalog policy, :class:`SnapshotPolicy`); a path opens a separate read-only
  connection, for offline use only (the benchmark, tests).

What is read from the snapshot: picklist values (multipicklists split on ``;``); the ``Name`` of small reference
objects (niches, programs, interview types ...); short text fields whose values repeat (at most ``max_distinct``
distinct values and at most half the filled rows; values held by one record only, values with a digit and values that
are a person's name are dropped). Never read: sensitive and system objects, columns the catalog policy blocks,
secret and contact columns, identifier/integration/security/audit/system/configuration fields (by
their knowledge semantic roles), free text (summaries, remarks, reasons, notes, errors, titles, display names) and
Id/number/key/token fields. Person names come from ``Recruiter__c`` (employees), human ``User`` s, person ``Account``
s and ``Lead`` s (full name, first word, last word, first + last word of longer names; titles, initials and test or
system names left out).

Stored values and names live in memory only. duckdb is imported only when a path is given, so importing this module
stays light.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from sf_assistant.knowledge.resolver import HONORIFICS, STOPWORDS, Vocabulary, split_words
from sf_assistant.knowledge.terms import (LEXICON_PATHS, add_knowledge, finish_vocabulary,  # noqa: F401 - re-exported
                                          load_knowledge, load_lexicon, start_vocabulary)

__all__ = ["build_vocabulary", "load_knowledge", "load_lexicon", "add_knowledge", "add_snapshot", "SnapshotPolicy",
           "NAME_SOURCES", "MAX_DISTINCT", "LEXICON_PATHS", "SENSITIVE_OBJECTS", "NEVER_OBJECTS"]

#: Objects whose records are people (``Contact`` is left out: it mirrors person accounts).
NAME_SOURCES = ("Recruiter__c", "User", "Account", "Lead")
#: ``User.UserType`` values of non-human users (site guests, automated process and integration users).
NON_HUMAN_USER_TYPES = ("Guest", "AutomatedProcess", "CsnOnly", "CloudIntegrationUser", "LicenseManager",
                        "SelfService")
#: Words that mark a test, demo or system record rather than a person (the whole name is skipped).
_NOT_A_PERSON = frozenset({"test", "testing", "demo", "dummy", "sample", "unknown", "integration", "automated",
                           "guest", "noreply", "admin", "administrator", "bot", "sandbox"})
#: The same object sets as engine/catalog.py (that module cannot be imported here; pass its policy as
#: :class:`SnapshotPolicy` to stay in step).
SENSITIVE_OBJECTS = frozenset({"Candidate_Portal_Credential__c", "Candidate_Portal_Session__c"})
NEVER_OBJECTS = frozenset({"Organization", "FieldPermissions", "ObjectPermissions", "PermissionSet",
                           "PermissionSetAssignment", "Profile", "SetupAuditTrail", "LoginHistory", "AuthSession"})
_SYSTEM_SUFFIXES = ("History", "Share", "Feed")
#: Knowledge semantic roles of fields never read (the catalog's profile drop roles) ...
DROP_ROLES = frozenset({"contact_attribute", "security_attribute", "integration_attribute", "audit_attribute",
                        "configuration_attribute", "system_attribute", "identifier"})
#: ... for picklists only these (a configuration or integration picklist is still a list of values) ...
PICKLIST_DROP_ROLES = frozenset({"contact_attribute", "security_attribute"})
#: ... and, for text fields, roles of free text, names, links, scores and times.
FREE_TEXT_ROLES = frozenset({"display_name", "descriptive_name", "feedback", "ai_analysis", "work_notes",
                             "attendee_identity", "url_attribute", "link", "score", "temporal_attribute",
                             "schedule_mirror", "docusign_merge_field"})
MAX_DISTINCT = 200          #: a text field with more distinct stored values is free text, not a value list
MAX_TEXT_SHARE = 0.5        #: ... or with more distinct values than this share of its filled rows
MIN_TEXT_COUNT = 2          #: a text value held by fewer records is not indexed (one record's free text or typo)
VALUE_LIMIT = 501           #: stored values read per field
MAX_VALUE_CHARS = 60        #: longer stored values are sentences, not names of anything
MAX_VALUE_WORDS = 6
_PICKLIST_TYPES = frozenset({"picklist", "multipicklist"})
_TEXT_TYPES = frozenset({"string", "text_area", "text"})
_SKIP_TYPES = frozenset({"email", "phone", "url", "address", "auto_number", "encrypted_string"})
#: Offline connection settings (no external access, no extension loading).
OFFLINE_CONFIG = {"enable_external_access": False, "autoload_known_extensions": False,
                  "autoinstall_known_extensions": False}

#: Columns never read (credentials, identity documents, bank data, contact details): the record catalog's secret and
#: contact patterns, plus identity numbers, login names, session ids, salary and transaction data. Contact values stay
#: out in phase 1 too (2026-10-26): they are no words users type as terms, and the record tools show them only when a
#: question asks for contact details.
_SECRET = (r"pass(word|wd|code)|(^|_)ssn(_|$)|social_?security|passport|secret|token|credential"
           r"|(^|_)bank_(name|account|routing)|account_?number|routing_?number|security_?(question|answer)"
           r"|api_?key|session_?key|(^|_)otp(_|$)"
           r"|access_?key|verification_?code|integrity_?hash|(^|_)signature__c"
           r"|identification_?number|issuing_?authority|session_?id|user_?name|(^|_)ach(_|$)|salary|transaction")
_CONTACT = (r"(e_?mail|phone|mobile|fax|street|postal|zip|address|latitude|longitude|linkedin|ip_address|whatsapp"
            r"|user_?agent|(^|_)ip(_|$)|(^|_)device(_|$)|contact_?number)")
BLOCKED_RE = re.compile(f"({_SECRET}|{_CONTACT})", re.IGNORECASE)
#: Text fields that hold free text or a person's name, by API name.
_FREE_TEXT_NAME_RE = re.compile(
    r"summary|remark|reason|description|comment|(^|_)notes?(_|$)|error|title|message|body|explanation|feedback"
    r"|(first|middle|last|full|display|candidate|participant|attendee|recruiter|interviewer|ref_?\d*|reference_?\d*"
    r"|vendor|contact|manager|owner|poc|spoc|trainer|mentor|employee|user|person)"
    r"_?name|(^|_)city(_|$)", re.IGNORECASE)
#: Last word of an identifier-like field API name.
_ID_WORDS = frozenset({"id", "ids", "number", "num", "no", "token", "key", "hash", "uuid", "guid"})
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
_NUMBERISH_RE = re.compile(r"^[\d\s.,:/%+\-()]+$")
_MAIL_URL_RE = re.compile(r"@|https?://|www\.", re.IGNORECASE)

Query = Callable[[str], Iterable[Sequence[Any]]]


@dataclass(frozen=True)
class SnapshotPolicy:
    """What :func:`add_snapshot` may read: the record catalog's guard policy as plain data (engine/catalog.py builds
    it; this module does not import it). ``allowed_tables``: the ``main`` tables the guard allows (None: any table
    not excluded here); ``blocked_columns``: lower-case column names never read in any table; ``blocked_fields``:
    lower-case ``(table, column)`` pairs never read; ``sensitive_objects`` and ``never_objects``: tables never read."""

    allowed_tables: frozenset[str] | None = None
    blocked_columns: frozenset[str] = frozenset()
    blocked_fields: frozenset[tuple[str, str]] = frozenset()
    sensitive_objects: frozenset[str] = SENSITIVE_OBJECTS
    never_objects: frozenset[str] = NEVER_OBJECTS

    @classmethod
    def from_guard(cls, allowed: Iterable[tuple[str, str]], blocked_columns: Iterable[str] = (),
                   blocked_fields: Iterable[tuple[str, str]] = (),
                   sensitive_objects: Iterable[str] = SENSITIVE_OBJECTS) -> "SnapshotPolicy":
        """From the warehouse guard policy (``Catalog.allowed`` ``(schema, table)`` pairs, ``blocked_columns``,
        ``blocked_fields``)."""
        return cls(frozenset(t for s, t in allowed if s == "main"), frozenset(c.lower() for c in blocked_columns),
                   frozenset((t.lower(), c.lower()) for t, c in blocked_fields), frozenset(sensitive_objects))

    def table_ok(self, table: str) -> bool:
        if table in self.sensitive_objects or table in self.never_objects or table.endswith(_SYSTEM_SUFFIXES):
            return False
        return self.allowed_tables is None or table in self.allowed_tables

    def column_ok(self, table: str, column: str) -> bool:
        c = column.lower()
        return c not in self.blocked_columns and (table.lower(), c) not in self.blocked_fields \
            and not BLOCKED_RE.search(column)


# ------------------------------------------------------------------------------------------------------- snapshot
def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _value_ok(v: Any) -> bool:
    s = str(v).strip()
    if not s or len(s) > MAX_VALUE_CHARS or len(s.split()) > MAX_VALUE_WORDS:
        return False
    if _ID_RE.match(s) and any(ch.isdigit() for ch in s):
        return False
    if _NUMBERISH_RE.match(s) or _MAIL_URL_RE.search(s):
        return False
    return any(ch.isalpha() for ch in s)


def _name_ok(v: Any) -> bool:
    s = str(v or "").strip()
    return bool(s) and len(s) <= 80 and not any(ch.isdigit() for ch in s) and "@" not in s


def _name_token(tok: str) -> str | None:
    """A whole first or last name from one whitespace token: the token without edge punctuation, when it is a word
    of 3+ letters that is no title or function word (initials such as ``K.`` and titles such as ``Dr.`` give None)."""
    t = tok.strip(".,;:()[]\"'")
    ws = split_words(t)
    joined = "".join(ws)
    if not joined.isalpha() or len(joined) < 3 or joined in HONORIFICS or joined in STOPWORDS:
        return None
    return t


def _connect(snapshot: Any, connect_config: Mapping[str, Any] | None) -> tuple[Query, Callable[[], None]]:
    """A query function for the snapshot, and how to release it."""
    if hasattr(snapshot, "execute"):          # an open DuckDB connection or cursor: the caller owns it
        return (lambda sql: snapshot.execute(sql).fetchall()), (lambda: None)
    if callable(snapshot):                    # query(sql) -> rows, e.g. the record warehouse's guarded run
        return snapshot, (lambda: None)
    if isinstance(snapshot, (str, Path)):     # offline only: a separate read-only connection
        import duckdb

        con = duckdb.connect(str(snapshot), read_only=True, config=dict(connect_config or OFFLINE_CONFIG))
        return (lambda sql: con.execute(sql).fetchall()), con.close
    raise TypeError("snapshot: a query function, a DuckDB connection or cursor, or a path")


def add_snapshot(vocab: Vocabulary, snapshot: Any, docs: list[dict[str, Any]], *,
                 columns: Mapping[str, Iterable[str]] | None = None, policy: SnapshotPolicy | None = None,
                 connect_config: Mapping[str, Any] | None = None, max_distinct: int = MAX_DISTINCT,
                 people: bool = True, stored_values: bool = True) -> None:
    """Person names and stored values from the snapshot (``main`` schema). ``snapshot``: a ``query(sql) -> rows``
    function, an open DuckDB connection or cursor (read through, never closed), or a path (a separate read-only
    connection with ``connect_config``; offline only). ``columns``: ``{table: column names}`` (default: read from
    ``information_schema``). ``policy``: what may be read (default: the built-in sensitive and system objects and
    column patterns). A statement that fails (for example one the warehouse guard refuses) skips that field."""
    query, release = _connect(snapshot, connect_config)
    pol = policy or SnapshotPolicy()
    info = vocab.info.setdefault("snapshot", {"value_fields": 0, "values": 0, "people": 0, "people_skipped": 0,
                                              "skipped_fields": {}, "dropped_values": {}, "query_errors": 0})

    def rows_of(sql: str) -> list[Sequence[Any]] | None:
        try:
            return list(query(sql))
        except Exception:                     # noqa: BLE001 - a refused or failing statement skips that field only
            info["query_errors"] += 1
            return None

    def skip(reason: str, n: int = 1) -> None:
        info["skipped_fields"][reason] = info["skipped_fields"].get(reason, 0) + n

    def drop(reason: str) -> None:
        info["dropped_values"][reason] = info["dropped_values"].get(reason, 0) + 1

    try:
        if columns is None:
            cols: dict[str, set[str]] = {}
            for t, c in rows_of("SELECT table_name, column_name FROM information_schema.columns "
                                "WHERE table_schema = 'main'") or []:
                cols.setdefault(str(t), set()).add(str(c))
        else:
            cols = {str(t): {str(c) for c in cs} for t, cs in columns.items()}
        name_words: set[str] = set()
        full_names: set[str] = set()
        if people:
            _add_people(vocab, rows_of, cols, pol, info, name_words, full_names)
        if stored_values:
            for d in docs:
                _add_stored(vocab, rows_of, d, cols, pol, max_distinct, info, skip, drop, name_words, full_names)
    finally:
        release()


def _add_people(vocab: Vocabulary, rows_of: Callable[[str], list | None], cols: dict[str, set[str]],
                pol: SnapshotPolicy, info: dict, name_words: set[str], full_names: set[str]) -> None:
    for o in NAME_SOURCES:
        have = cols.get(o)
        if not have or "Name" not in have or "Id" not in have or not pol.table_ok(o) or not pol.column_ok(o, "Name"):
            continue
        where = ['"Name" IS NOT NULL']
        if o == "Account":
            if "IsPersonAccount" not in have:
                continue                       # no way to tell people from client companies
            where.append('"IsPersonAccount"')
        if o == "User" and "UserType" in have:
            where.append('COALESCE("UserType", \'\') NOT IN (' + ", ".join(_lit(x) for x in NON_HUMAN_USER_TYPES)
                         + ")")
        if "IsDeleted" in have:
            where.append('NOT COALESCE("IsDeleted", false)')
        for rid, name in rows_of(f'SELECT "Id", trim("Name") FROM main.{_q(o)} WHERE {" AND ".join(where)} '
                                 'ORDER BY "Id"') or []:
            words = split_words(name)
            if not _name_ok(name) or not words or set(words) & _NOT_A_PERSON:
                info["people_skipped"] += 1
                continue
            target = f"person:{o}/{rid}"
            text = str(name).strip()
            vocab.add("person", target, text, o, "full")
            name_words.update(words)
            if len(words) >= 2:
                full_names.add(" ".join(words))
            toks = text.split()
            core = [t for t in (_name_token(x) for x in toks) if t]
            if len(core) >= 2:
                vocab.add("person", target, core[0], o, "first")
                vocab.add("person", target, core[-1], o, "last")
                fl = f"{core[0]} {core[-1]}"
                if split_words(fl) != words:
                    vocab.add("person", target, fl, o, "first_last")
                    full_names.add(" ".join(split_words(fl)))
            info["people"] += 1


def _is_person_name(value: str, name_words: set[str], full_names: set[str]) -> bool:
    """A stored text value that is a person's name, or holds one (people come from the person kind)."""
    ws = split_words(value)
    if not ws:
        return False
    if all(w in name_words for w in ws):
        return True
    for n in (2, 3, 4):
        for i in range(0, len(ws) - n + 1):
            if " ".join(ws[i:i + n]) in full_names:
                return True
    return False


def _add_stored(vocab: Vocabulary, rows_of: Callable[[str], list | None], d: dict[str, Any],
                cols: dict[str, set[str]], pol: SnapshotPolicy, max_distinct: int, info: dict,
                skip: Callable[..., None], drop: Callable[[str], None], name_words: set[str],
                full_names: set[str]) -> None:
    api = str(d["object"]["api_name"])
    have = cols.get(api)
    if not have:
        return
    if not pol.table_ok(api):
        skip("sensitive or system object", len(d.get("fields") or []))
        return
    pick: list[tuple[str, bool]] = []
    texts: list[str] = []
    names: list[str] = []
    for f in d.get("fields") or []:
        if not isinstance(f, dict):
            continue
        fapi = str(f.get("api_name") or "")
        ftype = str(f.get("type") or "")
        if fapi not in have or fapi == "Id" or ftype in _SKIP_TYPES:
            continue
        if not pol.column_ok(api, fapi):
            skip("blocked column")
            continue
        roles = set((f.get("semantic") or {}).get("roles") or [])
        if ftype in _PICKLIST_TYPES:
            if roles & PICKLIST_DROP_ROLES:
                skip("security or contact role")
                continue
            pick.append((fapi, ftype == "multipicklist"))
        elif ftype in _TEXT_TYPES:
            if fapi == "Name":
                if api not in NAME_SOURCES:
                    names.append(fapi)        # record names of reference objects (niches, programs ...)
                continue
            last = (split_words(fapi) or [""])[-1]
            if roles & DROP_ROLES:
                skip("identifier, integration, security, audit, system or configuration role")
            elif roles & FREE_TEXT_ROLES or _FREE_TEXT_NAME_RE.search(fapi):
                skip("free text, name, link, score or time")
            elif last in _ID_WORDS:
                skip("identifier-like name")
            else:
                texts.append(fapi)
    counts: dict[str, tuple[int, int]] = {}
    sized = texts + names
    def count(part: list[str]) -> list[Sequence[Any]] | None:
        return rows_of("SELECT " + ", ".join(f"COUNT({_q(c)}), COUNT(DISTINCT {_q(c)})" for c in part)
                       + f" FROM main.{_q(api)}")
    for i in range(0, len(sized), 40):
        part = sized[i:i + 40]
        row = count(part)
        if not row and len(part) > 1:         # one refused column fails the batch: count the columns one by one
            for c in part:
                one = count([c])
                if one:
                    counts[c] = (int(one[0][0] or 0), int(one[0][1] or 0))
            continue
        if not row:
            continue
        vals = list(row[0])
        for j, c in enumerate(part):
            counts[c] = (int(vals[2 * j] or 0), int(vals[2 * j + 1] or 0))
    keep: list[tuple[str, str]] = [(c, "pick") for c, _ in pick]
    for c in names:
        n, nd = counts.get(c, (0, 0))
        if 0 < nd <= max_distinct:
            keep.append((c, "name"))
        elif nd:
            skip("too many distinct values")
    for c in texts:
        n, nd = counts.get(c, (0, 0))
        if not nd:
            continue
        if nd > max_distinct or nd > MAX_TEXT_SHARE * n:
            skip("too many distinct values")
            continue
        keep.append((c, "text"))
    multi = {c for c, m in pick if m}
    for fapi, how in keep:
        col = _q(fapi)
        rows = rows_of(f"SELECT CAST({col} AS VARCHAR), COUNT(*) FROM main.{_q(api)} WHERE {col} IS NOT NULL "
                       f"GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {VALUE_LIMIT}")
        if rows is None:
            continue
        agg: dict[str, int] = {}
        for v, n in rows:
            if v is None:
                continue
            parts = str(v).split(";") if fapi in multi else [str(v)]
            for p in parts:
                p = p.strip()
                if p:
                    agg[p] = agg.get(p, 0) + int(n or 0)
        if agg and all(_ID_RE.match(v) for v in agg):
            skip("identifier values")
            continue
        info["value_fields"] += 1
        source = "stored" if how == "pick" else "stored_text"
        for v in sorted(agg, key=lambda x: (-agg[x], x)):
            n = agg[v]
            if not _value_ok(v):
                drop("not a name of anything")
                continue
            if how == "text":
                if n < MIN_TEXT_COUNT:
                    drop("one record only")
                    continue
                if any(ch.isdigit() for ch in v):
                    drop("has a digit")
                    continue
            if how != "pick" and name_words and _is_person_name(v, name_words, full_names):
                drop("a person's name")
                continue
            vocab.add("value", f"{api}.{fapi}={v}", v, api, source, float(n))
            info["values"] += 1


def build_vocabulary(knowledge_dir: str | Path, snapshot: Any = None, *, lexicon: Any = "auto",
                     columns: Mapping[str, Iterable[str]] | None = None, policy: SnapshotPolicy | None = None,
                     connect_config: Mapping[str, Any] | None = None, max_distinct: int = MAX_DISTINCT,
                     people: bool = True, stored_values: bool = True) -> Vocabulary:
    """The vocabulary of one knowledge folder, an English word list and (optionally) the snapshot.

    ``lexicon``: ``"auto"`` (the system word lists in :data:`LEXICON_PATHS` that exist), a path or a list of paths
    of word-list files, or None (no word list: ordinary English words may then be taken for typos; ``info`` says
    so). ``snapshot``, ``columns``, ``policy``, ``connect_config``: see :func:`add_snapshot`. ``vocab.info`` holds
    counts and timings."""
    vocab, docs, t0 = start_vocabulary(knowledge_dir, lexicon)   # knowledge/terms.py: the knowledge and the word list
    if snapshot is not None:
        t2 = time.perf_counter()
        add_snapshot(vocab, snapshot, docs, columns=columns, policy=policy, connect_config=connect_config,
                     max_distinct=max_distinct, people=people, stored_values=stored_values)
        vocab.info["snapshot_s"] = round(time.perf_counter() - t2, 3)
    return finish_vocabulary(vocab, t0)
