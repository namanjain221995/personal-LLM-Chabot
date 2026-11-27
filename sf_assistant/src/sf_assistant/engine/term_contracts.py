"""Business terms enforced by code (business-term contracts of 2026-11-24): what a record tool call must hold when the
question names a term the owner defined, decided by code before the call runs.

Three terms (``prompts/business_terms.md`` holds their definitions):

- **candidates** (candidate words, misspellings and Hinglish included): candidates are Person Accounts. Code ADDS the
  filter :data:`CANDIDATE_PREDICATE` (record type Person Account) to a call that counts, groups, lists or filters
  candidates: object Account, or a path through a candidate lookup to Account (:func:`is_candidate_lookup`; the filter
  goes on that lookup, ``Candidate__c.RecordTypeId``). Nothing is added when the call names one person on that lookup
  (a Name or Id filter), wants the lookup blank, contrasts record types itself (RecordTypeId or IsPersonAccount in
  part_filters, segments, group_by or split_by), or already holds the record type. A plain or any_of filter that
  puts the candidates on another record type (or IsPersonAccount false) is REFUSED (T1) unless the question names
  other account types; so is a call that counts Lead or Contact records (or groups or lists by a Lead lookup) for a
  question that names no leads or contacts.
- **placement** (``business_terms.TERM_WORDS``): a call that measures placement from Candidate_Status__c 'Placed' (its
  own filters or a related child's) or from Onboarding__c (object, path hop or related child) is REFUSED (T2) unless
  the question names that field or object; a Candidate_Status__c breakdown gets a note. Placements are dated by the
  interview date (owner's decision of 2026-11-25), and so are the client interviews a placement rate is taken over:
  a dated call on Interview__c (outcome filter or not), a call on another object that measures placements through a
  lookup to Interview__c, or a related Interview__c child that keeps 'Offer Received', dated by another field than
  Date_of_Interview__c (its period's or trend's date_field, or a date condition in its filters) is REFUSED (T2) with
  the call to send instead; a call with no date is unchanged (:func:`_placement_date`).
- **support person performance** (support-person words): a count, group or other measuring call (a list too when
  the question has performance words) on Session__c (object, path hop or related child), on the preferred or
  previous support person, or on the legacy support person of internal mock interviews is REFUSED (T3) unless the
  question names sessions (or those fields) or the call names one record; with performance words, ``group_records
  Interview__c by Interview_Support_Person__c`` is COMPLETED with ``split_by Interview_Outcome__c`` (or already has
  it), and code adds the offer rate per group from that split after the call (:func:`offer_rates`).

The words a model reads (notes, error texts, the answer's code line) are the prompt file ``prompts/term_contracts.md``
(:data:`TERM_CONTRACTS_TEXT`, one ``key: text`` line each). Pure functions over plain data: the record tools pass a
view of their catalog (:class:`View`: object and path resolution, record types), so this module never imports the
catalog, the warehouse or duckdb. ``tools/term_enforce.py`` applies the plan inside ``RecordTools.call``;
``agent/term_notes.py`` keeps the answer's scope claims true to what ran.

Standard library, :mod:`sf_assistant.prompts` and :mod:`sf_assistant.engine.business_terms` only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from sf_assistant import prompts as _prompt_files
from sf_assistant.engine import business_terms as _bt

__all__ = ["TERM_CONTRACTS_TEXT", "TEXTS", "CANDIDATE_PREDICATE", "FLAG_PREDICATE", "PERSON_TYPE", "OUTCOME",
           "OFFER", "Contract", "CONTRACTS", "Terms", "NO_TERMS", "Step", "PathInfo", "View", "Plan", "detect",
           "is_candidate_lookup", "plan", "offer_rates", "blank_outcomes", "BLANK_SHARE_NOTE", "text", "edit_distance",
           "ENFORCED_TOOLS", "INTERVIEW", "INTERVIEW_DATE"]

TERM_CONTRACTS_TEXT = _prompt_files.text("term_contracts")
TEXTS: dict[str, str] = dict(ln.split(": ", 1) for ln in TERM_CONTRACTS_TEXT.split("\n") if ": " in ln)

#: The developer name of the candidates' record type (the owner's "record type PersonAccount").
PERSON_TYPE = "PersonAccount"
#: The filter item code adds for candidates (``field`` gets the candidate lookup's path in front on another object).
CANDIDATE_PREDICATE: dict[str, Any] = {"field": "RecordTypeId", "op": "eq", "value": PERSON_TYPE}
#: Only when the record type cannot be read but the flag column can (it also keeps the employees' own person accounts).
FLAG_PREDICATE: dict[str, Any] = {"field": "IsPersonAccount", "op": "eq", "value": True}
#: The outcome field and value of support person performance (the owner's offer rate).
OUTCOME, OFFER = "Interview_Outcome__c", "Offer Received"
#: The client interview and its interview date: placements are dated, filtered and trended by it (owner's decision of
#: 2026-11-25; review finding R2-4, owner item O1).
INTERVIEW, INTERVIEW_DATE = "Interview__c", "Date_of_Interview__c"
#: The share of a support call's interviews with a blank outcome from which code says how many are blank.
BLANK_SHARE_NOTE = 0.2
#: The record query tools a contract applies to (search, people, history and orientation tools never).
ENFORCED_TOOLS = ("count_records", "aggregate_records", "group_records", "trend_records", "compare_records",
                  "percentage_of_records", "list_records")


def text(key: str, **fill: Any) -> str:
    """The prompt line ``key`` of :data:`TEXTS` with ``<<NAME>>`` filled (``prompts.fill``)."""
    return _prompt_files.fill(TEXTS[key], **{k: str(v) for k, v in fill.items()})


# ------------------------------------------------------------------------------------------------ reading the question
#: Candidate nouns as typed (plural and possessive forms), job seekers and the Hinglish words. The boundaries are
#: letters, not \b, so an API name in the question names the term too ("group by Candidate__c"; review fixes of
#: 2026-11-24). "person account(s)" is no candidate word (a literal count of person accounts stays unscoped); generic
#: people words (people, who, anyone) are none either: they also mean employees.
_CANDIDATE_RE = re.compile(
    r"(?<![A-Za-z])(?:candidates?|students?|consultants?|trainees?|applicants?|job\s*seekers?|"
    rf"{_bt.HINGLISH_CANDIDATES})(?![A-Za-z])", re.IGNORECASE)
#: The canonical candidate words a misspelt token is compared with (:func:`edit_distance`).
_CANONICAL = ("candidate", "candidates", "student", "students", "consultant", "consultants", "trainee", "trainees",
              "applicant", "applicants")
#: Words within the edit distance of a candidate word that mean something else (trainers are employees; the English
#: words of the system word list within the distance, review fixes of 2026-11-24: candidacy ... strainers).
_STOP = frozenset({"consultancy", "consulting", "consultation", "consulted", "candid", "candidly", "candidature",
                   "applicable", "application", "applications", "trainer", "trainers", "trained", "training",
                   "trainings", "consultations", "studied", "studies", "studios", "students'", "constant",
                   "constants", "candidacy", "candidacies", "supplicant", "supplicants", "strainers"})
#: Words that put the question on other account types: B2B, a type word right before "(person) account(s)"; a bare
#: "recruiter" (candidates per recruiter) or "client companies" (Company__c, not Account) is no override.
_OTHER_ACCOUNTS_RE = re.compile(
    r"\bb2b\b|\b(?:client|business|company|corporate|vendor|implementation\s+partner|employee|internal|recruiter)s?"
    r"['’]?\s+(?:person\s+)?accounts?\b", re.IGNORECASE)
#: Record type names whose plural is an everyday word, not an account type (Salesforce's default type).
_NO_TYPE_NAMES = frozenset({"master"})
#: Words that ask for every account type: nothing is scoped. "regardless of type" alone is no such word (review fixes
#: of 2026-11-24: "had interviews this month regardless of type" is about the interview's type).
_ALL_TYPES_RE = re.compile(
    r"\b(?:all|every|any)\s+(?:the\s+)?(?:account\s+types?|types?\s+of\s+accounts?|record\s+types?)\b"
    r"|\bregardless\s+of\s+(?:the\s+)?(?:account|record)\s+types?\b"
    r"|\bincluding\s+(?:the\s+)?(?:recruiter|employee|client|business|b2b)s?\s+accounts?\b"
    r"|\bnon[-\s]?person\s+accounts?\b|\bIsPersonAccount\s*(?:=|is)?\s*false\b", re.IGNORECASE)
#: Support-person words: a support word with a person noun at most two words after it, supporter(s), interview support.
_SUPPORT = r"(?:support|suport|supprt|sopport|suppport|supoort)"
_SUPPORT_RE = re.compile(
    rf"\b{_SUPPORT}\b(?:\W+\w+){{0,2}}?\W+(?:pe?rso?ns?|people|team|staff|members?|executives?|wale|wala|waale)\b"
    rf"|\bsupporters?\b|\binterview\s+{_SUPPORT}\b", re.IGNORECASE)
#: Performance words; "top", "best", "rank(ing)" and "results" count only together with one of these.
_PERFORMANCE_RE = re.compile(
    r"\b(?:performance|perform(?:s|ing|ed)?|offer\s+rate|success\s+rate|conversion|how\s+(?:good|well)|worst|"
    r"leaderboard|score\s*cards?|outcomes?|kaisa|kaisi|kaam)\b", re.IGNORECASE)
_SESSION_RE = re.compile(r"\b(?:sessions?|sesion|zoom|meetings?|host(?:s|ed|ing)?|session\s+roles?)\b", re.IGNORECASE)
_PREFERRED_RE = re.compile(r"\b(?:preferred|previous|prev)\b", re.IGNORECASE)
_MOCK_RE = re.compile(r"\b(?:mock|internal)\b", re.IGNORECASE)
#: Lead / Contact words: the nouns only ("did we contact" is a verb; review fixes of 2026-11-24).
_LEADS_RE = re.compile(r"\b(?:leads?|website\s+applicants?|unconverted|contacts|contact\s+(?:records?|objects?|rows?|"
                       r"table|list))\b", re.IGNORECASE)
_ONBOARDING_RE = re.compile(r"\bonboard\w*|\bpsa\b|\bservices?\s+agreements?\b", re.IGNORECASE)
#: The question names the status field: Candidate_Status, or "status" right next to "placed" ("status is Placed",
#: "with status 'Placed'", "in placed status"); review fixes of 2026-11-24: "the interview status of placed
#: candidates" names another status.
_PLACED_STATUS_RE = re.compile(
    r"candidate[_\s]?status|\bstatus\s*(?:(?:is|was|as|=|:|==)\s*)?['\"`]?placed\b|\bplaced['\"`]?\s+status\b",
    re.IGNORECASE)
#: The question asks for records with no candidate ("interviews with no candidate", "candidate is blank"): code adds
#: no candidate filter where a blank group or row would show.
_NO_CANDIDATE_RE = re.compile(
    r"\b(?:no|without(?:\s+an?)?|blank|missing|empty)\s+candidates?\b(?![\s_]*(?:status|stage|type|owner|id|name)\b)"
    r"|\bcandidates?\s+(?:is\s+|are\s+)?(?:blank|missing|empty|not\s+set)\b", re.IGNORECASE)
#: A field of the preferred or previous support person (Interview__c).
_PREFERRED_FIELD_RE = re.compile(r"(?:preferred|previous)_support_person__c$", re.IGNORECASE)


def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein distance (optimal string alignment: an adjacent swap counts 1)."""
    la, lb = len(a), len(b)
    d = [[0] * (lb + 1) for _ in range(la + 1)]
    for i in range(la + 1):
        d[i][0] = i
    for j in range(lb + 1):
        d[0][j] = j
    for i in range(1, la + 1):
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[la][lb]


def _misspelt(question: str, canonical: tuple[str, ...]) -> bool:
    """A token of 6 or more letters within distance 1 (2 from 9 letters) of a ``canonical`` word, stop words aside."""
    for tok in re.findall(r"[a-z]+", question.lower()):
        if len(tok) < 6 or tok in _STOP or tok in canonical:
            continue
        limit = 2 if len(tok) >= 9 else 1
        if any(abs(len(tok) - len(w)) <= limit and edit_distance(tok, w) <= limit for w in canonical):
            return True
    return False


@dataclass(frozen=True)
class Contract:
    """One business term: its key (the ``prompts/business_terms.md`` line), its question words (``words``, and
    ``fuzzy``: the canonical words a misspelt token is compared with) and whether its definition goes into the record
    hints (``hint``; candidates: no, code enforces it and the record agent's system prompt says it). :func:`detect`
    reads every entry; the rules themselves are the plan functions below, one per term."""

    key: str
    hint: bool
    words: re.Pattern | None = None   # None: the words are business_terms.TERM_WORDS[key]
    fuzzy: tuple[str, ...] = ()        # canonical words a misspelt token is compared with
    not_the_term: frozenset[tuple[str, str, str]] = frozenset()

    def names(self, question: str) -> bool:
        """``question`` names this term (its words, or a misspelling of a ``fuzzy`` word)."""
        words = self.words if self.words is not None else _bt.TERM_WORDS.get(self.key)
        return bool(words is not None and words.search(question)) or bool(self.fuzzy and _misspelt(question,
                                                                                                    self.fuzzy))


CANDIDATES = Contract("candidates", hint=False, words=_CANDIDATE_RE, fuzzy=_CANONICAL)
PLACEMENT = Contract("placement", hint=True)
SUPPORT_PERFORMANCE = Contract("support_performance", hint=True, words=_SUPPORT_RE,
                               not_the_term=frozenset({("Session__c", "Session_Role__c", "Support")}))
CONTRACTS: tuple[Contract, ...] = (CANDIDATES, PLACEMENT, SUPPORT_PERFORMANCE)

# the hinted terms with words here: the hints show their definition (business_terms.term_lines)
for _c in CONTRACTS:
    if _c.hint and _c.words is not None:
        _bt.register(_c.key, _c.words, _c.not_the_term)


@dataclass(frozen=True)
class Terms:
    """The terms a question names (``keys``) and the words that change what a contract allows."""

    question: str = ""
    keys: frozenset[str] = frozenset()
    other_accounts: bool = False   # other account types named (B2B, client accounts, a record type name)
    all_types: bool = False        # every account type asked for: nothing is scoped
    leads: bool = False            # leads or contacts named: Lead / Contact calls are allowed
    sessions: bool = False         # sessions named: Session__c calls are allowed
    performance: bool = False      # performance words: the support call is completed
    preferred: bool = False        # preferred / previous support person named
    mock: bool = False             # mock / internal interviews named
    placed_status: bool = False    # the status field named (Candidate_Status, "status placed")
    onboarding: bool = False       # onboarding (or its services agreement) named
    no_candidate: bool = False     # records with no candidate asked for ("interviews with no candidate")


NO_TERMS = Terms()


def detect(question: Any, other_record_types: Any = ()) -> Terms:
    """The terms ``question`` names (each :data:`CONTRACTS` entry's words) and its override words.
    ``other_record_types``: the names (and developer names) of the Account record types other than Person Account;
    such a name in the plural ("recruiters"), before "(person) account(s)" / "record type", or after "record type",
    names other account types. "" (raw MCP tool calls, tool-level tests) names nothing."""
    q = question if isinstance(question, str) else ""
    if not q.strip():
        return NO_TERMS
    keys = {c.key for c in CONTRACTS if c.key in _bt.DEFINITIONS and c.names(q)}
    other = bool(_OTHER_ACCOUNTS_RE.search(q))
    for name in other_record_types or ():
        words = [w for w in re.split(r"[\s_]+", str(name or "").strip()) if w]
        if not words or "".join(words).lower() == PERSON_TYPE.lower():
            continue
        n = r"[\s_]*".join(re.escape(w) for w in words)
        plural = "" if "".join(words).lower() in _NO_TYPE_NAMES else rf"\b{n}s\b|"
        if re.search(rf"{plural}\b{n}\s+(?:person\s+)?(?:accounts?|record\s*types?)\b"
                     rf"|\brecord\s*types?\s+['\"`]?{n}\b", q, re.IGNORECASE):
            other = True
            break
    perf = bool(_PERFORMANCE_RE.search(q))
    return Terms(question=q, keys=frozenset(keys), other_accounts=other, all_types=bool(_ALL_TYPES_RE.search(q)),
                 leads=bool(_LEADS_RE.search(q)), sessions=bool(_SESSION_RE.search(q)), performance=perf,
                 preferred=bool(_PREFERRED_RE.search(q)), mock=bool(_MOCK_RE.search(q)),
                 placed_status=bool(_PLACED_STATUS_RE.search(q)), onboarding=bool(_ONBOARDING_RE.search(q)),
                 no_candidate=bool(_NO_CANDIDATE_RE.search(q)))


# ------------------------------------------------------------------------------------------------ the call's paths
@dataclass(frozen=True)
class Step:
    """One lookup of a resolved path: ``src.field`` points at ``target`` (None: not one queryable object)."""

    src: str
    field: str
    target: str | None
    role: str = ""


@dataclass(frozen=True)
class PathInfo:
    """A resolved field or lookup path: ``text`` canonical ("Candidate__c.RecordTypeId"), ``refs`` the lookups in
    order (each hop, then the last field when it is a lookup), ``field`` the last field's API name."""

    text: str
    refs: tuple[Step, ...]
    field: str

    @property
    def parts(self) -> list[str]:
        return self.text.split(".")


class View(Protocol):
    """What the plan reads from the record tools' catalog (``tools/term_enforce.py`` builds it)."""

    def object(self, name: Any) -> str | None: ...   # the object's API name, None when unknown
    def path(self, obj: str, text: Any) -> PathInfo | None: ...   # None when the path does not resolve
    def record_type(self, obj: str, value: Any) -> str | None: ...   # developer name of a record type value
    def person_type(self, obj: str) -> str | None: ...   # the label of the Person Account record type, None: none
    def has_field(self, obj: str, api: str) -> bool: ...
    # (label, developer name) of each record type: a contains / starts_with filter on the record type's Name or
    # DeveloperName is read through it (a view without it leaves such a filter "unknown")
    def record_types(self, obj: str) -> list[tuple[str, str]]: ...
    # the date field a date_field argument (or a filter's field) names, read as the record tools read a date_field
    # (CreatedDate's words, a temporal key such as interview_date); None when it does not resolve or is no date or
    # date/time field (a view without it: the placement date rule does not apply)
    def date_path(self, obj: str, text: Any) -> PathInfo | None: ...


#: The lookups to Account that are not the candidate (client account, parent, referrer, the employee's own account and
#: the standard lookups of Case, Opportunity and Contact).
_NOT_CANDIDATE = frozenset({("Interview__c", "Client_Account__c"), ("Account", "ParentId"),
                            ("Account", "Candidate_Referral__c"), ("Recruiter__c", "Recruiter_Contact__c"),
                            ("Case", "AccountId"), ("Opportunity", "AccountId"), ("Contact", "AccountId")})
#: Candidate lookups whose name and role do not say "candidate".
_ALSO_CANDIDATE = frozenset({("Onboarding__c", "Account__c")})


def is_candidate_lookup(step: Step) -> bool:
    """``step`` is a lookup to Account that holds the candidate: its API name or business role says "candidate" (or
    it is listed), and it is not one of the lookups that hold another account."""
    key = (step.src, step.field)
    if step.target != "Account" or key in _NOT_CANDIDATE:
        return False
    return key in _ALSO_CANDIDATE or "candidate" in step.field.lower() or "candidate" in (step.role or "").lower()


@dataclass
class Plan:
    """What code does to one call: ``add`` filter items (``(args key, item)``), ``set_args``, a ``reject`` (code,
    text), ``notes`` for the result (``added_notes``: those that state an added item), ``unapplied`` (the notes when
    the added items cannot run: the join cap) and ``completed`` (support performance: code adds the offer rate after
    the call)."""

    add: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    set_args: dict[str, Any] = field(default_factory=dict)
    reject: tuple[str, str] | None = None
    notes: list[str] = field(default_factory=list)
    added_notes: list[str] = field(default_factory=list)
    unapplied: list[str] = field(default_factory=list)
    completed: bool = False

    @property
    def changes(self) -> bool:
        return bool(self.add or self.set_args)


def _filter_key(tool: str) -> str:
    return "base_filters" if tool == "percentage_of_records" else "filters"


def _items(value: Any) -> list[dict]:
    return [x for x in value or [] if isinstance(x, dict)] if isinstance(value, list) else []


def _parts(tool: str, args: dict) -> list[tuple[str, Any, dict | None]]:
    """``(kind, path text, filter item)`` of every field or path the call uses on its object. Kinds: ``filter`` (the
    plain filters: ``base_filters`` of a percentage, the shared filters of a compare), ``any_of``, ``part``,
    ``segment``, ``group``, ``split``, ``measure`` (count_distinct, a measure_field of another measure than count,
    aggregate's field), ``column`` and ``order`` (list_records). The filters of ``related`` child records are
    :func:`_related_parts` (their paths start on the child object)."""
    out: list[tuple[str, Any, dict | None]] = []
    for it in _items(args.get(_filter_key(tool))):
        out.append(("filter", it.get("field"), it))
    for it in _items(args.get("any_of")):
        out.append(("any_of", it.get("field"), it))
    for it in _items(args.get("part_filters")):
        out.append(("part", it.get("field"), it))
    for seg in _items(args.get("segments")):
        for it in _items(seg.get("filters")):
            out.append(("segment", it.get("field"), it))
    if args.get("group_by"):
        out.append(("group", args["group_by"], None))
    if args.get("split_by"):
        out.append(("split", args["split_by"], None))
    if args.get("measure_field") and args.get("measure") not in (None, "count"):
        out.append(("measure", args["measure_field"], None))
    if args.get("count_distinct"):
        out.append(("measure", args["count_distinct"], None))
    if tool == "aggregate_records" and args.get("field"):
        out.append(("measure", args["field"], None))
    for f in args.get("fields") or [] if isinstance(args.get("fields"), list) else []:
        out.append(("column", f, None))
    if args.get("order_by"):
        out.append(("order", args["order_by"], None))
    return out


def _related_parts(args: dict) -> list[tuple[Any, Any, dict]]:
    """``(child object, path text, filter item)`` of the filters of every ``related`` child (review fixes of
    2026-11-24: a 'Placed' status or a Session__c hop inside them is checked like the call's own filters)."""
    return [(r.get("child_object"), it.get("field"), it) for r in _items(args.get("related"))
            for it in _items(r.get("filters"))]


def _values(item: dict | None) -> list[Any]:
    if not item:
        return []
    if item.get("op") in ("in", "not_in"):
        return list(item.get("values") or [])
    return [item.get("value")] if item.get("value") is not None else []


#: How an applied filter text writes each operator (engine/query_builder.py ``filter_frag``).
_OP_TEXT = {"eq": "=", "ne": "<>", "in": "in", "not_in": "not in", "contains": "contains", "starts_with": "starts with",
            "not_contains": "does not contain", "is_blank": "is blank", "is_not_blank": "is not blank", "gt": ">",
            "gte": ">=", "lt": "<", "lte": "<="}


def _item_text(path: PathInfo | None, item: dict) -> str:
    """A filter item as the applied texts write it ("RecordTypeId = 'Recruiter'", "X__c in ('A', 'B')")."""
    vals = _values(item)
    shown = ", ".join(f"'{v}'" if isinstance(v, str) else str(v).lower() if isinstance(v, bool) else str(v)
                      for v in vals)
    name = path.text if path is not None else str(item.get("field"))
    op = str(item.get("op") or "")
    if op in ("in", "not_in"):
        shown = f"({shown})"
    return f"{name} {_OP_TEXT.get(op, op)} {shown}".strip()


def _bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    return True if s in ("true", "yes", "1", "checked", "y") else False if s in ("false", "no", "0", "unchecked", "n") \
        else None


def _candidate_prefix(path: PathInfo | None) -> str | None:
    """The path up to its first candidate lookup ("Candidate__c", "Candidate_Training__c.Candidate__c"), else None."""
    if path is None:
        return None
    for i, s in enumerate(path.refs):
        if is_candidate_lookup(s):
            return ".".join(r.field for r in path.refs[: i + 1])
    return None


def _rest(path: PathInfo | None, prefix: str) -> list[str] | None:
    """The parts of ``path`` after ``prefix`` ("" = the base object), None when the path does not start with it."""
    if path is None:
        return None
    parts = path.parts
    if not prefix:
        return parts
    pre = prefix.split(".")
    return parts[len(pre):] if parts[: len(pre)] == pre else None


def _type_use(rest: list[str] | None) -> str | None:
    """"rt" when the rest of a path is the record type (RecordTypeId, RecordTypeId.Name / DeveloperName), "flag" for
    IsPersonAccount, "rt?" for another field of the record type, None otherwise."""
    if not rest:
        return None
    head = rest[0].lower()
    if head == "ispersonaccount" and len(rest) == 1:
        return "flag"
    if head in ("recordtypeid", "recordtype"):
        return "rt" if len(rest) == 1 or rest[1].lower() in ("name", "developername") else "rt?"
    return None


def _kept_state(kept: list[Any]) -> str | None:
    """"present" when every record type a filter keeps is Person Account, "conflict" when it keeps another, None when
    it keeps none."""
    if not kept:
        return None
    return "present" if all(str(d).lower() == PERSON_TYPE.lower() for d in kept) else "conflict"


def _types_kept(view: View, rest: list[str], op: str, vals: list[Any]) -> list[str] | None:
    """The developer names of the Account record types a contains / starts_with / not_contains filter on the record
    type's Name or DeveloperName keeps (None: code cannot read it; a bare RecordTypeId refuses those ops itself, E4f)."""
    lister = getattr(view, "record_types", None)
    if lister is None or len(rest) < 2 or not vals or not str(vals[0]).strip():
        return None
    v, label = str(vals[0]).casefold(), rest[1].lower() == "name"
    kept = []
    for name, dev in lister("Account") or []:
        s = str((name if label else dev) or "").casefold()
        hit = s.startswith(v) if op == "starts_with" else v in s
        if hit != (op == "not_contains"):
            kept.append(dev)
    return kept


def _own_state(item: dict, rest: list[str] | None, view: View) -> str | None:
    """What a plain (or any_of) filter on the record type or the flag of the candidate's account does: "present" (it
    keeps only candidates), "conflict" (it keeps other accounts), "unknown" (a value code cannot read: the handler
    raises its own error, E4) or None (neither, so code adds the candidate filter: "RecordTypeId ne 'B2B Client'").

    Review fixes of 2026-11-24: ``IsPersonAccount = true`` is "present" only when the Person Account record type does
    not resolve (the flag fallback); otherwise it also keeps the employees' own person accounts (the Recruiter type),
    so code adds the record type too. A contains / starts_with filter on the record type's Name or DeveloperName is
    read against the record types; another field of the record type ("rt?") gets the candidate filter as well."""
    use = _type_use(rest)
    op = item.get("op")
    vals = _values(item)
    if use == "flag":
        if op in ("is_blank", "is_not_blank"):
            return None
        b = _bool(vals[0]) if vals else None
        if b is None or op not in ("eq", "ne"):
            return "unknown"
        if (op == "eq") != b:
            return "conflict"
        return "present" if view.person_type("Account") is None else None
    if use != "rt" or op in ("is_blank", "is_not_blank"):
        return None
    if op in ("contains", "starts_with", "not_contains"):
        kept = _types_kept(view, rest or [], op, vals)
        return "unknown" if kept is None else _kept_state(kept)
    if op not in ("eq", "in", "ne", "not_in") or not vals:
        return "unknown"
    devs = [view.record_type("Account", v) for v in vals]
    if any(d is None for d in devs):
        return "unknown"
    person = [str(d).lower() == PERSON_TYPE.lower() for d in devs]
    if op in ("eq", "in"):
        return "present" if all(person) else "conflict"
    return "conflict" if any(person) else None


def plan(terms: Terms, tool: str, args: Any, view: View) -> Plan:
    """What code does to the call ``tool(args)`` (validated arguments) for the terms of the question; an empty plan
    when no term applies. Rejections first (T3, T2, T1), then the candidate filter, then the support completion."""
    out = Plan()
    if not terms.keys or not isinstance(args, dict):
        return out
    obj = view.object(args.get("object"))
    if obj is None:
        return out
    if tool == "get_field_values":
        path = view.path(obj, args.get("field"))
        if "placement" in terms.keys and path is not None and path.field == "Candidate_Status__c":
            out.notes.append(text("placement_status_note"))
        return out
    if tool not in ENFORCED_TOOLS:
        return out
    parts = [(kind, raw, item, view.path(obj, raw)) for kind, raw, item in _parts(tool, args)]
    related = [view.object(r.get("child_object")) for r in _items(args.get("related"))]
    for child, raw, item in _related_parts(args):   # kind "related": a path on the child object
        child_obj = view.object(child)
        parts.append(("related", raw, item, view.path(child_obj, raw) if child_obj else None))
    if "support_performance" in terms.keys:
        _support(terms, tool, obj, parts, related, out)
    if out.reject is None and "placement" in terms.keys:
        _placement(terms, tool, obj, args, parts, related, view, out)
    if out.reject is None and "candidates" in terms.keys:
        _leads(terms, obj, parts, out)
    if out.reject is None and "candidates" in terms.keys:
        _candidates(terms, tool, obj, args, parts, view, out)
    if out.reject is None and "support_performance" in terms.keys and terms.performance:
        _complete(tool, obj, args, parts, view, out)
    if out.reject is not None:
        out.add, out.set_args, out.notes, out.added_notes, out.completed = [], {}, [], [], False
    return out


def _names_one_record(parts: list) -> bool:
    """A plain filter names one record of the call's object by Name or Id (eq / in)."""
    return any(kind == "filter" and item is not None and p is not None and p.parts in (["Name"], ["Id"])
               and item.get("op") in ("eq", "in") and _values(item) for kind, _r, item, p in parts)


def _support(terms: Terms, tool: str, obj: str, parts: list, related: list, out: Plan) -> None:
    """T3: support person performance measured from Session__c, the preferred / previous support person or the
    legacy support person of internal mock interviews. Review fixes of 2026-11-24 (a change to design section 5): a
    list_records call is refused only for a question with performance words, and a call that names one record (Name
    or Id filter) never is: "who is the support person of interview I-1" is a lookup, not a performance measure."""
    if (tool == "list_records" and not terms.performance) or _names_one_record(parts):
        return
    if not terms.sessions:
        hop = next((p.text for _k, _r, _i, p in parts if p is not None
                    and any(s.target == "Session__c" for s in p.refs)), None)
        if obj == "Session__c" or "Session__c" in related or hop:
            where = ("object Session__c" if obj == "Session__c" else hop if hop
                     else "related child records of Session__c")
            out.reject = ("T3", text("support_reject", FIELD=where))
            return
    if not terms.preferred:
        for kind, _raw, _item, p in parts:
            if p is not None and kind in ("group", "split", "measure", "column") and (
                    _PREFERRED_FIELD_RE.search(p.field) or any(_PREFERRED_FIELD_RE.search(s.field) for s in p.refs)):
                out.reject = ("T3", text("support_reject", FIELD=f"{obj}.{p.text}"))
                return
    if not terms.mock and obj == "Internal_Interview__c":
        for kind, _raw, _item, p in parts:
            if p is not None and kind != "related" and "Interview_Support_Person__c" in p.parts:
                out.reject = ("T3", text("support_reject", FIELD="Internal_Interview__c.Interview_Support_Person__c"))
                return


def _placed_value(op: Any, value: str) -> bool:
    """A Candidate_Status__c filter value that keeps 'Placed': the value holds "placed", or (contains / starts_with)
    it is a part or a start of "placed" of 3 or more letters ("Plac", review fixes of 2026-11-24)."""
    v = value.casefold().strip()
    if "placed" in v:
        return True
    if op not in ("contains", "starts_with") or len(v) < 3:
        return False
    return "placed".startswith(v) if op == "starts_with" else v in "placed"


def _placement(terms: Terms, tool: str, obj: str, args: dict, parts: list, related: list, view: View,
               out: Plan) -> None:
    """T2: placement measured from Candidate_Status__c 'Placed' (the call's filters, or a related child's) or from
    Onboarding__c, or placements dated by another field than the interview date (:func:`_placement_date`); a note on
    a status breakdown."""
    if not terms.placed_status:
        q = terms.question.casefold()
        for kind, _raw, item, p in parts:
            if p is None or item is None or kind not in ("filter", "any_of", "part", "segment", "related") \
                    or p.field != "Candidate_Status__c" or item.get("op") not in ("eq", "in", "contains", "starts_with"):
                continue
            vals = [str(v) for v in _values(item)]
            placed = [v for v in vals if _placed_value(item.get("op"), v)]
            if not placed:
                continue
            others = [v for v in vals if v not in placed]
            if item.get("op") == "in" and any(re.search(rf"\b{re.escape(v.casefold())}\b", q) for v in others):
                continue   # "active or placed candidates": a status list the question names
            out.reject = ("T2", text("placement_reject", FIELD=_item_text(p, item)))
            return
    if not terms.onboarding:
        hop = next((p.text for _k, _r, _i, p in parts if p is not None
                    and any(s.target == "Onboarding__c" for s in p.refs)), None)
        if obj == "Onboarding__c" or "Onboarding__c" in related or hop:
            where = ("object Onboarding__c" if obj == "Onboarding__c" else hop if hop
                     else "related child records of Onboarding__c")
            out.reject = ("T2", text("placement_reject", FIELD=where))
            return
    dated = _placement_date(tool, obj, args, parts, view)
    if dated is not None:
        out.reject = ("T2", text("placement_date_reject", FIELD=dated[0], DATE=dated[1]))
        return
    if tool in ("group_records", "trend_records") and any(
            p is not None and kind in ("group", "split") and p.field == "Candidate_Status__c"
            for kind, _r, _i, p in parts):
        out.notes.append(text("placement_status_note"))


def _offer_value(op: Any, value: Any) -> bool:
    """An Interview_Outcome__c filter value that keeps 'Offer Received', compared as the record tools compare stored
    values (case, spaces, hyphens and underscores aside): eq / in the value itself; contains / starts_with a part or a
    start of it of 3 or more letters ("offer")."""
    v, offer = (re.sub(r"[\s_-]+", "", str(x).casefold()) for x in (value, OFFER))
    if op in ("eq", "in"):
        return v == offer
    if op not in ("contains", "starts_with") or len(v) < 3:
        return False
    return offer.startswith(v) if op == "starts_with" else v in offer


def _measures_offers(parts: list, prefix: str = "") -> bool:
    """``parts`` measure placements on the interview at ``prefix`` ("" = the call's object is Interview__c; else the
    path of a lookup to Interview__c, :func:`_interview_prefix`): a filter (plain, any_of, part, segment) on its
    Interview_Outcome__c that keeps 'Offer Received', or a group or split by that outcome."""
    for kind, _raw, item, p in parts:
        if _rest(p, prefix) != [OUTCOME]:
            continue
        if kind in ("group", "split"):
            return True
        if item is not None and kind in ("filter", "any_of", "part", "segment") \
                and any(_offer_value(item.get("op"), v) for v in _values(item)):
            return True
    return False


def _interview_prefix(path: PathInfo | None) -> str | None:
    """The path up to its first lookup to Interview__c ("Interview__c", "Evaluation__c.Interview__c"), else None."""
    if path is None:
        return None
    for i, s in enumerate(path.refs):
        if s.target == INTERVIEW:
            return ".".join(r.field for r in path.refs[: i + 1])
    return None


def _date_arg(view: View, obj: str, raw: Any) -> PathInfo | None:
    """The date field ``raw`` names on ``obj`` (:meth:`View.date_path`), None when it names none."""
    getter = getattr(view, "date_path", None)
    if getter is None or not isinstance(raw, str) or not raw.strip():
        return None
    found = getter(obj, raw)
    if isinstance(found, str):   # a view whose date_path gives the path text (query_state.PlanView): resolve it
        return view.path(obj, found)
    return found


def _dated_by(tool: str, args: dict, parts: list, view: View, obj: str, prefix: str = "") -> str | None:
    """What dates the interviews of ``args`` (on ``obj``; ``prefix`` the path to the interview, "" when ``obj`` is
    Interview__c) by another field than the interview's Date_of_Interview__c: the date_field of a period (the call's,
    a compare segment's, a related item's own) or of a trend's buckets, or a date condition in the filters on another
    date field of the call's object or of the interview itself (not is_blank / is_not_blank, not a comparison with
    other_field; a date of another linked record, such as the candidate's, is a condition, not the date of the
    count); None when nothing does. A date_field with no period (list_records' sort order) dates nothing."""
    want = (prefix.split(".") if prefix else []) + [INTERVIEW_DATE]
    dated = bool(args.get("period")) or tool == "trend_records" or any(
        seg.get("period") for seg in _items(args.get("segments")))
    if dated and args.get("date_field"):
        d = _date_arg(view, obj, args["date_field"])
        if d is not None and d.parts != want:
            return f"date_field {d.text}"
    for kind, raw, item, p in parts:
        if p is None or p.parts == want or item is None or kind not in ("filter", "any_of", "part", "segment"):
            continue
        rest = _rest(p, prefix) if prefix else None
        if p.refs and not (rest is not None and len(rest) == 1):
            continue
        if item.get("other_field") or item.get("op") in ("is_blank", "is_not_blank"):
            continue
        if _date_arg(view, obj, raw) is not None:
            return f"the filter {_item_text(p, item)}"
    return None


def _placement_date(tool: str, obj: str, args: dict, parts: list, view: View) -> tuple[str, str] | None:
    """The owner's decision of 2026-11-25 (review finding R2-4, owner item O1): placements are dated and trended by
    Date_of_Interview__c, and so are the client interviews a placement rate is taken over. For a question that names
    placement, ``(what dates the interviews by another field, the date field to use)`` for the T2 text, or None when
    the call is fine:

    - every dated call on Interview__c, whether or not it filters the outcome (the rate's denominator: all client
      interviews, by interview month);
    - a call on another object that measures placements through a lookup to Interview__c (``Interview__c.
      Interview_Outcome__c`` 'Offer Received', or a group or split by it) must date them by ``<lookup>.
      Date_of_Interview__c``;
    - a related Interview__c child item whose filters keep 'Offer Received' must date its children by
      Date_of_Interview__c (a related child that measures no placement counts the parent's records, never the
      rate's interviews, and is unchanged).

    A call with no date is unchanged. Refused, not rewritten, as the other contracts treat a field the model chose
    wrongly (T1 record type, T2 status, T3 session): code adds only what is missing (the candidate filter, the
    support split), and a date condition in the filters has no safe rewrite into a period."""
    own = [x for x in parts if x[0] != "related"]
    if obj == INTERVIEW:
        where = _dated_by(tool, args, own, view, obj)
        if where is not None:
            return where, INTERVIEW_DATE
    else:
        for pre in dict.fromkeys(x for x in (_interview_prefix(p) for _k, _r, _i, p in own) if x is not None):
            if _measures_offers(own, pre):
                where = _dated_by(tool, args, own, view, obj, pre)
                if where is not None:
                    return where, f"{pre}.{INTERVIEW_DATE}"
    for r in _items(args.get("related")):
        if view.object(r.get("child_object")) != INTERVIEW:
            continue
        rparts = [("filter", it.get("field"), it, view.path(INTERVIEW, it.get("field"))) for it in
                  _items(r.get("filters"))]
        if not _measures_offers(rparts):
            continue
        where = _dated_by("", r, rparts, view, INTERVIEW)
        if where is not None:
            return f"{where} of the related {INTERVIEW} records", INTERVIEW_DATE
    return None


def _leads(terms: Terms, obj: str, parts: list, out: Plan) -> None:
    """T1: candidates counted from Lead or Contact (object), or grouped / listed by a Lead lookup."""
    if terms.leads or terms.all_types:
        return
    if obj in ("Lead", "Contact"):
        out.reject = ("T1", text("leads_reject", FIELD=f"{obj} records"))
        return
    for kind, _raw, _item, p in parts:
        if p is not None and kind in ("group", "split", "measure", "column") and any(s.target == "Lead" for s in p.refs):
            out.reject = ("T1", text("leads_reject", FIELD=f"Lead records through {obj}.{p.text}"))
            return


def _candidates(terms: Terms, tool: str, obj: str, args: dict, parts: list, view: View, out: Plan) -> None:
    """The candidate filter on every prefix that counts, groups, lists or filters candidates (see the module text).

    Review fixes of 2026-11-24: a record-type or flag filter inside ``any_of`` is read like a plain one (a branch on
    another record type is refused, T1; otherwise the filter is added unless every branch keeps only Person
    Accounts); a filter that wants the candidate lookup blank ("Candidate__c is_blank") leaves its prefix alone (it
    counts records with no candidate); and a question that asks for records with no candidate keeps the blank group
    or row of a prefix the call only groups, splits, lists or orders by (``candidates_blank``)."""
    if terms.all_types:
        return
    parts = [x for x in parts if x[0] != "related"]   # related child filters are no candidate prefix of this call
    prefixes: dict[str, set[str]] = {}
    if obj == "Account":
        prefixes[""] = {"object"}
    any_pre = []
    for kind, _raw, _item, p in parts:
        pre = _candidate_prefix(p)
        if pre is None:
            continue
        if kind == "any_of":
            any_pre.append(pre)
        else:
            prefixes.setdefault(pre, set()).add(kind)
    n_any = len(_items(args.get("any_of")))
    if any_pre:
        if len(any_pre) == n_any and len(set(any_pre)) == 1:
            prefixes.setdefault(any_pre[0], set()).add("any_of")
        else:
            for pre in dict.fromkeys(any_pre):
                if pre not in prefixes:
                    out.notes.append(text("candidates_any_of", FIELD=pre))
    key = _filter_key(tool)
    for pre in prefixes:
        rests = [(kind, item, p, _rest(p, pre)) for kind, _raw, item, p in parts]
        named = any(kind == "filter" and item is not None and r is not None and item.get("op") in ("eq", "in")
                    and _values(item) and ((pre and r == []) or r in (["Name"], ["Id"]))
                    for kind, item, _p, r in rests)
        if named:
            continue   # one named person: the class rule does not apply (lookup checks name a non-candidate)
        if pre and any(kind == "filter" and item is not None and r == [] and item.get("op") == "is_blank"
                       for kind, item, _p, r in rests):
            continue   # records with no candidate: the candidate filter would leave none of them
        if pre and terms.no_candidate and prefixes[pre] <= {"group", "split", "column", "order"}:
            out.notes.append(text("candidates_blank", FIELD=pre))
            continue   # the question asks for the records with no candidate: their group or row stays
        if any(kind in ("part", "segment", "group", "split") and _type_use(r) for kind, _i, _p, r in rests):
            continue   # the call contrasts record types itself
        typed = [(kind, item, p, _own_state(item, r, view)) for kind, item, p, r in rests
                 if kind in ("filter", "any_of") and item is not None and _type_use(r)]
        shown = f"{pre}." if pre else ""
        bad = next((_item_text(p, item) for _k, item, p, state in typed if state == "conflict"), None)
        if bad is not None:
            if terms.other_accounts:
                continue   # the question names other account types: the call's own record-type filter stands
            out.reject = ("T1", text("candidates_reject", FIELD=bad))
            return
        own = [state for kind, _i, _p, state in typed if kind == "filter"]
        either = [state for kind, _i, _p, state in typed if kind == "any_of"]
        if "present" in own or "unknown" in own or "unknown" in either:
            continue   # already scoped, or a record-type value code cannot read (the handler raises its own E4)
        if either and len(either) == n_any and all(state == "present" for state in either):
            continue   # every branch of the OR group keeps only candidates
        label = view.person_type("Account")
        if label:
            item = dict(CANDIDATE_PREDICATE, field=shown + CANDIDATE_PREDICATE["field"])
            note = text("candidates_note", FILTER=f"{shown}RecordTypeId = '{label}'")
        elif view.has_field("Account", "IsPersonAccount"):
            item = dict(FLAG_PREDICATE, field=shown + FLAG_PREDICATE["field"])
            note = text("candidates_flag_note", FILTER=f"{shown}IsPersonAccount = true")
        else:
            out.notes.append(text("candidates_unapplied", FIELD=pre or "Account"))
            continue
        out.notes.append(note)
        out.added_notes.append(note)
        out.add.append((key, item))
        out.unapplied.append(text("candidates_unapplied", FIELD=pre or "Account"))


def _fixes_outcome(kind: str, item: dict | None, p: PathInfo | None) -> bool:
    """A filter of the call that leaves the offer rate no meaning: it fixes Interview_Outcome__c (eq, in, is_blank, a
    pattern) or leaves 'Offer Received' out (ne / not_in it). ``ne 'Ghosted'`` and is_not_blank do not (review fixes
    of 2026-11-24); an OR group on the outcome does, as before."""
    if p is None or p.parts != [OUTCOME] or kind not in ("filter", "any_of"):
        return False
    if kind == "any_of":
        return True
    op = (item or {}).get("op")
    if op == "is_not_blank":
        return False
    if op in ("ne", "not_in"):
        return any(str(v).casefold() == OFFER.casefold() for v in _values(item))
    return True


def _complete(tool: str, obj: str, args: dict, parts: list, view: View, out: Plan) -> None:
    """Support person performance: group_records Interview__c by Interview_Support_Person__c (count) gets split_by
    Interview_Outcome__c when it has none and its filters do not fix the outcome; code then adds the offer rate.
    Review fixes of 2026-11-24: a call that already splits by Interview_Outcome__c (what the hint and the T3 text
    tell the model to send) gets the offer rate too, with nothing set; another split gets none."""
    if tool != "group_records" or obj != "Interview__c" or args.get("measure") not in (None, "count"):
        return
    group = next((p for kind, _r, _i, p in parts if kind == "group"), None)
    if group is None or group.parts not in (["Interview_Support_Person__c"], ["Interview_Support_Person__c", "Name"]):
        return
    if args.get("split_by"):
        split = next((p for kind, _r, _i, p in parts if kind == "split"), None)
        if split is None or split.parts != [OUTCOME]:
            return
    elif view.path(obj, OUTCOME) is None:
        return
    if any(_fixes_outcome(kind, item, p) for kind, _r, item, p in parts):
        return
    if not args.get("split_by"):
        out.set_args["split_by"] = OUTCOME
    out.completed = True


def _pct(part: float, base: float) -> float | None:
    """The rounding of the record tools' rates (tools/records.py ``_pct``): 2 places under 1%, else 1."""
    if not base:
        return None
    p = part * 100.0 / base
    return round(p, 2) if abs(p) < 1 else round(p, 1)


def offer_rates(groups: Any, split_totals: Any) -> float | None | bool:
    """Add ``offers`` and ``offer_rate_pct`` to each group (its ``by`` split by Interview_Outcome__c) and return the
    overall rate from ``split_totals``; False (nothing added) when 'Offer Received' was folded into '(other)'."""
    totals = split_totals if isinstance(split_totals, dict) else {}
    key = next((k for k in totals if str(k).casefold() == OFFER.casefold()), None)
    if key is None and "(other)" in totals:
        return False
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        by = g.get("by") if isinstance(g.get("by"), dict) else {}
        k = next((x for x in by if str(x).casefold() == OFFER.casefold()), None)
        offers = int(by.get(k) or 0) if k is not None else 0
        value = g.get("value") if isinstance(g.get("value"), (int, float)) else 0
        g["offers"] = offers
        g["offer_rate_pct"] = _pct(offers, value)
    whole = sum(v for v in totals.values() if isinstance(v, (int, float)))
    return _pct(int(totals.get(key) or 0) if key is not None else 0, whole)


def blank_outcomes(split_totals: Any) -> tuple[int, int]:
    """(interviews with a blank Interview_Outcome__c, all interviews) of a completed support call's split totals."""
    totals = split_totals if isinstance(split_totals, dict) else {}
    whole = sum(v for v in totals.values() if isinstance(v, (int, float)))
    blank = totals.get("(blank)")
    return (int(blank) if isinstance(blank, (int, float)) else 0), int(whole)
