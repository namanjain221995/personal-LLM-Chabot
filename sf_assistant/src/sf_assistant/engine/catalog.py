"""Catalog: the knowledge files joined with the warehouse schema.

``Catalog.build(warehouse, knowledge_dir)`` decides which objects can be queried, describes every
field (kind, meaning, picklist, lookup targets, temporal keys, sensitivity, data presence), finds
undocumented lookups from Id prefixes, builds the child index, the record types, the value index
(stored values with counts) and the entity index (every stored value a question can name), and sets
the warehouse guard policy. Everything here is read once at open; tools only read the in-memory result.
"""

from __future__ import annotations

import calendar
import contextlib
import contextvars
import difflib
import heapq
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

from sf_assistant.knowledge.resolver import STOPWORDS, split_words
from sf_assistant.knowledge.search import METADATA_KINDS, KnowledgeIndex, resolver_expander, tokenize
from sf_assistant.knowledge.store import ObjectKnowledge, knowledge_files, split_api_name

from . import vocabulary as _vocabulary
from .sql_guard import quote_ident
from .types import Statement

__all__ = ["Catalog", "ObjectSpec", "FieldSpec", "ChildLink", "PathRef", "Hop", "ValueEntry", "RecordError",
           "IdentityLink", "NEVER_OBJECTS", "SENSITIVE_OBJECTS", "OBJECT_NOTES", "BLOCKED_COLUMN_RE",
           "CONTACT_COLUMN_RE", "norm_key", "norm_name", "SYSTEM_ROLES", "PERSON_OBJECTS", "SYSTEM_LOOKUPS",
           "ALMOST_EMPTY", "ROLE_CUES", "PROFILE_KEEP_ROLES", "PROFILE_DROP_ROLES", "STANDARD_PROFILE_FIELDS",
           "FIELD_SENSITIVITY", "SECRET_PARAM_RE", "LABELLED_SECRET_RE", "NO_SECRET_KINDS", "hide_embedded_secrets",
           "PEOPLE_SCAN_SKIP", "PROFILE_SKIP_FIELDS", "EntityEntry", "entity_key", "BLANK_VALUES",
           "ENTITY_BUILD_BUDGET_S", "ENTITY_FIELD_MAX", "ENTITY_MAX_ENTRIES", "ENTITY_NAME_MAX_CHARS",
           "ENTITY_NAME_MAX_WORDS", "ENTITY_PERSON_SHARE", "ENTITY_PERSON_SAMPLE", "ENTITY_NUMBER_MIN_FILLED"]

NEVER_OBJECTS = frozenset({"Organization", "FieldPermissions", "ObjectPermissions", "PermissionSet",
                           "PermissionSetAssignment", "Profile", "SetupAuditTrail", "LoginHistory", "AuthSession"})
SENSITIVE_OBJECTS = frozenset({"Candidate_Portal_Credential__c", "Candidate_Portal_Session__c"})
EXTRA_OBJECTS = ("Group", "RecordType")

OBJECT_NOTES: dict[str, str] = {
    "Account": ("Candidates are Account records with record type 'Person Account' (RecordTypeId DeveloperName "
                "PersonAccount, {person} rows in this snapshot); 'Recruiter' accounts are also person accounts but are "
                "not candidates; 'B2B Client' accounts are client companies."),
    "Recruiter__c": ("Employee records (label 'Employee'): recruiters, interviewers, trainers, support and other staff. "
                     "Objects often have several lookups to Employee, one per role (interviewer, recruiter, host, "
                     "trainer, support person); pick the lookup whose role fits the question, not the one named "
                     "Recruiter__c."),
    "Interview__c": ("Client interview rounds (real interviews with hiring companies). Not the same as "
                     "Internal_Interview__c."),
    "Internal_Interview__c": "Internal mock interviews. Not the same as Interview__c.",
    "Contact": "Mirrors person accounts; query Account instead.",
}
INTERVIEW_NOTE = ("Interview__c = client interviews; Internal_Interview__c = internal mock interviews "
                  "(different objects).")
#: Objects whose records the people tools (about_people, the link finder, the people hints) never count as a
#: person's references, work or links, although their lookups point at person objects (Org Knowledge Base build,
#: 2026-10-27, when both got knowledge files): Contact mirrors person accounts (the person contact of a candidate or
#: employee account is that person; OBJECT_NOTES), and Opportunity holds the unused records that lead conversion
#: leaves on converted candidates (its knowledge). Every other record tool still reads both.
PEOPLE_SCAN_SKIP = frozenset({"Contact", "Opportunity"})

#: The very-high fields, blocked in every record tool (phase 1, owner 2026-10-26: the first users are admins, so only
#: these stay blocked): credentials and secrets (passwords, passcodes, tokens, secrets, credentials, API, access and
#: session keys or ids, one-time passwords, verification codes, integrity hashes, code signatures, security questions
#: and answers), government identity numbers (SSN, social security, passport, identification numbers) and bank details
#: (bank name, account and routing, account numbers).
_SECRET_COLUMNS = (r"pass(word|wd|code)|(^|_)ssn(_|$)|social_?security|passport|secret|token|credential"
                   r"|(^|_)bank_(name|account|routing)|account_?number|routing_?number|security_?(question|answer)"
                   r"|api_?key|session_?key|(^|_)session_?id(?![a-z0-9])|(^|_)otp(_|$)"
                   r"|access_?key|verification_?code|integrity_?hash|(^|_)signature__c|identification_?number")
BLOCKED_COLUMN_RE = re.compile(f"({_SECRET_COLUMNS})", re.IGNORECASE)
#: Field kinds that cannot hold a secret value: a date, time or checkbox whose name has a very-high word
#: (User.LastPasswordChangeDate, User.PasswordExpirationDate, a "token expires" date) is metadata about the secret and
#: stays readable (phase 1, review F8). A text or number field with such a name stays blocked.
NO_SECRET_KINDS = frozenset({"date", "datetime", "time", "boolean"})
#: Very-high values inside a stored text (phase 1): the value of a URL query parameter that holds a passcode, password,
#: token, key or signature. :func:`hide_embedded_secrets` replaces each such value by ``[hidden]``; the rest of the
#: link stays readable.
SECRET_PARAM_RE = re.compile(r"([?&;](?:[\w.-]*?(?:pwd|passw(?:or)?d|passcode|token|secret|signature|api_?key|access_?key)"
                             r"|zak|tk|sig|otp|x-amz-credential)=)[^&\s\"'<>]+", re.IGNORECASE)
#: Very-high values written after a label in free text or JSON (phase 1, review F3): a passcode, password, OTP, secret,
#: token or key after ":" or "=" on the same line ("Passcode: 123456", "Meeting Password: ab12", '"zoom_passcode":
#: "ab12"'; "Passcode:" at a line end hides nothing; "otp" and "ssn" only as whole words), and a government identity
#: or bank number after its label, with or without ":" ("SSN no.: 123", "Passport No X1234567", "A/C number 0123"; the
#: value holds a digit). The label stays, the value becomes ``[hidden]``.
LABELLED_SECRET_RE = re.compile(
    r"((?<![\w])(?:[\w-]*?(?:pass ?code|passw(?:or)?d|pwd|secret|token|api[ _-]?key|access[ _-]?key)[\w-]*|otp|ssn)"
    r"[\"']?[ \t]*[:=][ \t]*[\"']?)(?!\[hidden\])([^\s\"'<>,;&\\]+)"
    r"|((?<![\w])(?:(?:passport|social security|aadhaa?r|ssn|ifsc)(?: ?(?:no\.?|number|num|#|code|card))?"
    r"|(?:pan|(?:bank )?(?:account|a/c|acct)|routing) ?(?:no\.?|number|num|#|card))[\"']?[ \t]*[:=-]?[ \t]*[\"']?)"
    r"(?!\[hidden\])((?=[^\s\"'<>,;&\\]*\d)[^\s\"'<>,;&\\]+)", re.IGNORECASE)
_SECRET_WORDS = ("pass", "pwd", "otp", "token", "secret", "signature", "key", "zak", "tk=", "sig=", "credential", "ssn",
                 "social security", "aadha", "pan", "account", "a/c", "acct", "routing", "ifsc")
#: Contact details; since 2026-10-23 also IP addresses, browsers (user agent) and devices by any name. Shown only when
#: the question asks for contact details (sf_assistant.engine.contact, phase 1). Phase 1 review: WhatsApp and contact
#: numbers by name (F2); an e-mail encoding (User.EmailEncodingKey, a character set) is no contact detail.
CONTACT_COLUMN_RE = re.compile(
    r"(e_?mail(?!_?encoding)|phone|mobile|fax|street|postal|zip|address|latitude|longitude|linkedin|ip_address"
    r"|user_?agent|(^|_)ip(_|$)|(^|_)device(_|$)|whatsapp|contact_?number)",
    re.IGNORECASE)
#: Fields of one object whose names the patterns above cannot take without also taking look-alikes of other objects
#: (2026-10-23): (object, field) -> ("blocked", what the field holds, as the tools say it) or ("contact", "") for text
#: that names people's e-mail addresses next to other details. Phase 1 (2026-10-26): the employee leave reasons and
#: leave document are readable again; three more contact fields whose values are e-mail addresses or LinkedIn profile
#: links under a name no pattern can take (review F2).
FIELD_SENSITIVITY: dict[tuple[str, str], tuple[str, str]] = {
    ("Zoom_Portal_Access_Audit__c", "Event_Details__c"): ("blocked", "credential, passcode or identity document"),
    ("Zoom_Recording_Access_Log__c", "Error_Message__c"): ("contact", ""),
    ("User", "Username"): ("contact", ""),
    ("Onboarding__c", "Professional_Profiles__c"): ("contact", ""),
    ("Job_Submission__c", "Unique_Submission_Key__c"): ("contact", ""),
}

SYSTEM_MEANINGS = {
    "Id": "record Id",
    "Name": "record name",
    "CreatedDate": "when the record was created",
    "LastModifiedDate": "when the record was last changed",
    "CreatedById": "user who created the record",
    "LastModifiedById": "user who last changed the record",
    "OwnerId": "record owner (a user or a queue)",
    "RecordTypeId": "record type of the record",
}
SYSTEM_REFS = {"CreatedById": ["User"], "LastModifiedById": ["User"], "OwnerId": ["User", "Group"],
               "RecordTypeId": ["RecordType"]}
UNDOCUMENTED = "undocumented (no knowledge entry; meaning unknown)"
#: Role, concept and reverse meaning of the system lookups: (role, concept, from_target).
SYSTEM_ROLES = {
    "CreatedById": ("record creator", "user who created the record", "records this user created"),
    "LastModifiedById": ("last editor", "user who last changed the record", "records this user last changed"),
    "OwnerId": ("record owner", "user or queue that owns the record", "records this user or queue owns"),
    "RecordTypeId": ("record type", "record type of the record", "records of this record type"),
}
PERSON_OBJECTS = ("Recruiter__c", "User", "Account", "Lead")   # queryable ones only
SYSTEM_LOOKUPS = ("CreatedById", "LastModifiedById", "OwnerId")
ALMOST_EMPTY = 0.01        # a lookup filled on fewer than 1% of the object's rows is "almost empty"
#: Profile of a person's own record (RECORDS_SPEC §22.4): documented fields with one of these semantic roles are shown;
#: a field with any of the drop roles never is (contact details, credentials, integration and audit data, settings).
PROFILE_KEEP_ROLES = frozenset({"lifecycle_state", "business_status", "categorical_attribute", "hierarchy_attribute",
                                "temporal_attribute"})
PROFILE_DROP_ROLES = frozenset({"contact_attribute", "security_attribute", "integration_attribute", "audit_attribute",
                                "configuration_attribute", "system_attribute", "identifier",
                                "b2b_attribute"})
#: Documented fields of person objects that the profile never shows although their roles would admit them (Org
#: Knowledge Base build, 2026-10-28): unused standard picklists of Account that lead conversion copies onto every
#: candidate; they crowded out work authorisation and visa status (owner 2026-10-26: visa visible). Every other
#: record tool reads them.
PROFILE_SKIP_FIELDS = {"Account": frozenset({"AccountSource", "Industry", "PersonLeadSource", "Rating", "Type"})}
#: Standard fields of person objects that no knowledge file documents (a schema constant): shown in the profile.
STANDARD_PROFILE_FIELDS = {"User": ("IsActive", "UserType", "LastLoginDate"),
                           "Lead": ("Status", "IsConverted", "ConvertedDate", "LeadSource")}
#: Profile fields of one object, in rank order: a safety cap only, far above what a person object has. The profile of
#: a record keeps the first ones filled on it (``tools.people.PROFILE_MAX_FILLED``): a field that is blank on most
#: records never takes the place of a later one, such as a date.
PROFILE_FIELDS_MAX = 40
# (question word stems, role stems when the person is an employee or user, role stems when a candidate or lead)
ROLE_CUES = (
    (("taken", "took", "take", "conduct", "held", "done"), ("interviewer", "conduct", "taker"), ("candidate", "applicant")),
    (("support", "assist"), ("support",), ()),
    (("host",), ("host",), ()),
    (("train", "taught", "teach", "coach"), ("trainer", "train"), ("candidate",)),
    (("attend", "join", "participat"), ("attend", "participant"), ("candidate", "attend", "lead")),
    (("own", "responsib", "handl"), ("owner", "own", "responsib", "recruiter"), ()),
    (("recruit",), ("recruiter", "recruit"), ()),
    (("assign",), ("assign",), ()),
    (("manag",), ("manager", "manag"), ()),
    (("creat", "added", "book"), ("creat",), ()),
    (("modif", "edit", "updat", "chang"), ("editor", "modif", "chang"), ()),
    (("approv",), ("approv",), ()),
    (("upload",), ("upload",), ()),
    (("submit",), ("submit",), ()),
    (("evaluat", "assess", "score", "grade"), ("evaluat", "assess", "scor"), ()),
    (("decid", "decision"), ("decision", "decid"), ()),
    (("refer",), ("refer",), ("refer",)),
    (("invit",), ("invit",), ("invit",)),
)
_ROLE_STOPWORDS = frozenset("this the of for by a an is are was where which that to in on with as its their record "
                            "records row rows".split())
_NAME_WS_RE = re.compile(r"[\t\n\f\r ]+")
AUTO_NUMBER_RE = re.compile(r"^[A-Za-z]{1,6}-?\d{3,}$")
_AUTO_NUMBER_SQL_RE = "[A-Za-z]{1,6}-?[0-9]{3,}"
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")
_MAX_STAT_CHARS = 15_000
VALUE_LIMIT = 501
TEXT_INDEX_MAX_DISTINCT = 200

# ------------------------------------------------------------------ entity index (value spots, 2026-10-28)
#: The entity index (:meth:`Catalog._entity_index`): every stored value a person could name in a question, in every
#: queryable object (Names of the non-person objects, codes, short text, picklists, record type names), with the
#: records that hold it. The record tools spot a question's stored values in it (owner 2026-10-28: record fetching
#: from any record value, in every object and every field; rule R4: every entity of the question). Limits:
ENTITY_BUILD_BUDGET_S = 4.0     #: wall clock of the whole build; objects not read in time are listed as skipped
ENTITY_FIELD_MAX = 50_000       #: distinct values kept per field (the most frequent; the field is flagged as cut)
ENTITY_MAX_ENTRIES = 300_000    #: entries kept in all
ENTITY_NAME_MAX_CHARS = 100     #: Names and codes: longer values are left out (text and picklist values keep the
ENTITY_NAME_MAX_WORDS = 12      #: vocabulary's MAX_VALUE_CHARS and MAX_VALUE_WORDS); also the longest key in words
ENTITY_PERSON_SHARE = 0.2       #: a Name or text field whose sampled values hold a stored person's name at least
ENTITY_PERSON_SAMPLE = 300      #: this often (most frequent values first) holds people: left out (the people path)
ENTITY_NUMBER_MIN_FILLED = 10   #: a ``...Number`` text field gives digits-only codes only when filled on this many
                                #: records (on one or two records "every value is its own" says nothing: a version 1)
#: Whole values that name no record: a blank, a placeholder, a test or a yes/no answer. The index keeps them (they
#: are stored values); spotting never takes one as an entity of a question, and no object takes one as a cue word.
BLANK_VALUES = frozenset({"none", "null", "nil", "na", "n a", "not applicable", "test", "tbd", "unknown", "other",
                          "others", "yes", "no"})
#: Objects whose Names are people or copies of people's names (the people path owns them): the person objects,
#: Contact (mirrors person accounts) and PEOPLE_SCAN_SKIP (Opportunity Names are converted candidates' names).
#: Account Names are read only for accounts that are no person (client companies).
_PERSON_NAMED = frozenset(PERSON_OBJECTS) | {"Contact"} | PEOPLE_SCAN_SKIP
#: Objects whose records are people: their identifier-shaped text (a user alias, a candidate number) is never indexed.
_PERSON_RECORDS = frozenset(PERSON_OBJECTS) | {"Contact"}
#: A whole stored value that is a date (ISO with or without a time, or day/month/year with slashes).
_DATE_VALUE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T].*)?$|^\d{1,2}/\d{1,2}/\d{2,4}$")
_DIGITS_RE = re.compile(r"^[0-9]+$")
_POSSESSIVE_RE = re.compile(r"['\u2019]s\b", re.IGNORECASE)
#: normalize_value's two substitutions as one table: dashes, underscores and slashes read as spaces, the punctuation
#: it removes deleted (entity_key).
_KEY_TABLE = str.maketrans({**{c: " " for c in "-_/\u2013\u2014"}, **{c: None for c in ".,'\"()"}})
_KEY_WORD_RE = re.compile(r"[a-z0-9]+")
_LETTER_RE = re.compile(r"[^\W\d_]")
#: hide_embedded_secrets' own first test (a secret word in the lower-case text) as one pattern: the full test runs only
#: on the values it finds.
_SECRET_WORD_RE = re.compile("|".join(re.escape(w) for w in _SECRET_WORDS))
#: Month and weekday names and abbreviations (the calendar module, English): with digits they make a date or an
#: amount shape of a text value ("Jan 2020").
_CALENDAR_WORDS = frozenset(w.lower() for seq in (calendar.month_name, calendar.month_abbr, calendar.day_name,
                                                  calendar.day_abbr) for w in seq if w)
#: Record type names that are no cue word of their object (a default type, or the records without a type).
_NO_CUE_RECORD_TYPES = frozenset({"master", "(none)"})
#: How the entity index reads a field (Catalog._entity_plan) -> the kind of its entries.
_ENTITY_KINDS = {"name": "name", "code": "code", "code_text": "code", "number": "code", "text": "text",
                 "picklist": "picklist", "multipicklist": "picklist"}
#: The sources whose values keep the longer Name limits (ENTITY_NAME_MAX_CHARS, ENTITY_NAME_MAX_WORDS).
_ENTITY_LONG_SOURCES = frozenset({"name", "code", "code_text", "number"})

_KB_KIND = {
    "picklist": "picklist", "multipicklist": "multipicklist", "reference": "reference", "boolean": "boolean",
    "checkbox": "boolean", "date": "date", "datetime": "datetime", "time": "time", "percent": "percent",
    "currency": "currency", "string": "text", "email": "text", "phone": "text", "url": "text",
    "text_area": "text", "address": "text", "auto_number": "text", "text": "text",
    "long_text_area": "longtext", "rich_text_area": "longtext",
}
_COMPATIBLE_DB = {
    "boolean": ("BOOLEAN",), "date": ("DATE",), "datetime": ("TIMESTAMP",), "time": ("TIME",),
    "number": ("DECIMAL", "DOUBLE", "BIGINT", "INTEGER", "FLOAT", "HUGEINT", "SMALLINT", "TINYINT", "REAL"),
}


class RecordError(Exception):
    """A user-level record tool error; ``str(exc)`` is the model-facing text that names the fix."""

    def __init__(self, message: str, code: str = "E") -> None:
        super().__init__(message)
        self.code = code


# ------------------------------------------------------------------ spelling help (docs/resolver_integration.md (3))
#: The correction channel of the current record tool call. ``RecordTools.call`` sets a fresh list here around the
#: call when the spelling help is on (``catalog.resolver`` is not None); :meth:`Catalog.resolve_object`,
#: :meth:`Catalog.field` and :func:`sf_assistant.engine.values.ground_value` append one entry per argument they read
#: as its closest name (:func:`record_read_as`). Unset (None: the hints, the tests, the catalog build, a search query
#: under :func:`no_read_as`), a close match is never used on its own: the did-you-mean error is raised instead.
READ_AS: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar("READ_AS", default=None)
#: Decisions of the resolver whose suggestions an error lists as did-you-means. Only ``corrected`` is ever used on
#: its own (decision B); ``exact`` here means the resolver's wider normalising (filler words, plurals) found a name
#: the catalog's exact tiers did not, so it is offered, not used.
_SUGGEST = ("corrected", "ambiguous", "exact")
_RESOLUTION_CACHE_MAX = 4096


@contextlib.contextmanager
def no_read_as() -> Iterator[None]:
    """Inside the block no close match is used on its own (a search query is not a tool argument)."""
    token = READ_AS.set(None)
    try:
        yield
    finally:
        READ_AS.reset(token)


def in_tool_call() -> bool:
    """True inside a record tool call with the spelling help on (a close match may then be used, and is stated)."""
    return READ_AS.get() is not None


def record_read_as(typed: str, used: str, kind: str, target: str, how: str, note: str) -> None:
    """Add one correction to the current tool call's channel (:data:`READ_AS`; nothing outside a tool call):
    ``{"arg": None (RecordTools.call fills the argument path), "typed", "used", "kind", "target", "how",
    "note"}``. The same correction twice in one call is kept once."""
    sink = READ_AS.get()
    if sink is None:
        return
    if not any(e["kind"] == kind and e["typed"] == typed and e["target"] == target for e in sink):
        sink.append({"arg": None, "typed": typed, "used": used, "kind": kind, "target": target, "how": how,
                     "note": note})


def with_did_you_mean(err: RecordError, typed: str, kind: str, suggestions: Iterable[str]) -> RecordError:
    """``err`` with ``err.did_you_mean = {"typed", "kind", "suggestions"}`` (QueryEvidence.did_you_mean of the failed
    call); the error code and text are unchanged. Returns ``err``."""
    err.did_you_mean = {"typed": typed, "kind": kind, "suggestions": list(suggestions)}   # type: ignore[attr-defined]
    return err


def _or(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def norm_name(v: Any) -> str:
    """Normalised record name (L9), the Python twin of :func:`okrec.query.name_norm_sql`: non-breaking spaces
    read as spaces, runs of whitespace as one space (the characters DuckDB's ``\\s`` matches), trimmed, lower case."""
    s = str(v if v is not None else "").replace("\xa0", " ")
    return _NAME_WS_RE.sub(" ", s).strip(" ").lower()


def _words(text: Any) -> list[str]:
    """Lower-case whole words of ``text``: a possessive 's and non-letters removed."""
    t = re.sub(r"['\u2019]s\b", "", str(text or "").lower())
    return re.findall(r"[a-z]+", t)


def norm_key(text: str) -> str:
    """Loose key: lower-case alphanumerics only, without a trailing ``__c``/``__r``."""
    t = re.sub(r"__(c|r)$", "", str(text or ""), flags=re.IGNORECASE)
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def _cut(text: Any, n: int) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


@dataclass(eq=False)
class FieldSpec:
    """One queryable column of an object, with its knowledge meaning."""

    api: str
    label: str
    kb_type: str | None
    db_type: str
    kind: str
    meaning: str
    legacy: bool = False
    deprecated: bool = False
    replaced_by: str | None = None
    picklist: list[tuple[str, str, str]] = field(default_factory=list)   # (value, label, meaning)
    ref_targets: list[str] = field(default_factory=list)
    temporal_keys: list[tuple[str, str]] = field(default_factory=list)    # (key, meaning)
    roles: list[str] = field(default_factory=list)
    has_data: bool = False
    filled: int = 0
    sensitivity: str | None = None
    documented: bool = False
    auto_number: bool = False
    system: bool = False
    concept: str = ""
    ref_role: str = ""          # reference.business_role; SYSTEM_ROLES role; "undocumented" (Id prefix); "" otherwise
    ref_meaning: str = ""       # reference.business_meaning (160 characters)
    from_target: str = ""       # relationships[].direction_semantics.from_target (120 characters)
    unresolved: int = 0         # values pointing to no record of the single queryable target (counted at open)
    mirror_of: str = ""         # "<lookup>.<lookup>" path this lookup copies (knowledge field_dependencies), else ""
    mirror_rule: str = ""       # the dependency's meaning (120 characters)
    from_source: str = ""       # relationships[].direction_semantics.from_source (120 characters): the lookup read
    #                             from the record that holds it ("senior recruiter this employee reports to")
    child_copy: tuple[str, str, str] = ()   # (child object C, C's lookup to this object, C's lookup this one copies):
    #                             the lookup copies a lookup of a child object (knowledge field_dependencies)
    copy_source_filled: int = 0   # records of this object with a child_copy row whose copied lookup is set (at open)

    @property
    def role(self) -> str:
        """``ref_role`` with ``_`` read as a space ("undocumented" for a lookup found from the Id prefix)."""
        return self.ref_role.replace("_", " ").strip()

    def role_text(self) -> str:
        """``'<role>: <concept>'``; only the role when the concept is empty or equal; the concept (else the
        meaning, 60 characters) when there is no role; "undocumented" for a lookup found from the Id prefix."""
        if self.ref_role == "undocumented":
            return "undocumented"
        role = self.role
        concept = " ".join(str(self.concept or "").split())
        if role:
            if not concept or concept.lower() == role.lower():
                return role
            return f"{role}: {concept}"
        return concept or _cut(self.meaning, 60)

    @property
    def is_date(self) -> bool:
        return self.kind in ("date", "datetime")

    @property
    def is_number(self) -> bool:
        return self.kind in ("integer", "number", "percent", "currency")

    @property
    def is_texty(self) -> bool:
        return self.kind in ("text", "name", "id", "picklist", "longtext")

    def type_text(self) -> str:
        """``kind``, or ``lookup → Target`` for references."""
        if self.kind == "reference":
            return "lookup → " + (" or ".join(self.ref_targets) if self.ref_targets else "unknown object")
        return self.kind


@dataclass
class ChildLink:
    """A child object that points at a parent through ``via``."""

    child: str
    via: str
    meaning: str


@dataclass(frozen=True)
class IdentityLink:
    """A lookup that ties one person record to another record of the same person (L10)."""

    src: str        # person object that holds the column (e.g. the Employee object)
    col: str        # its lookup column
    dst: str        # linked person object
    dst_col: str    # "Id", or "AccountId" for the standard portal-user link
    label: str      # e.g. "Recruiter_User__c (employee login user)", "portal user (User.AccountId = Recruiter_Contact__c)"


@dataclass
class ValueEntry:
    """Stored values of one field (value index, built at open)."""

    obj: str
    field: str
    values: list[tuple[str, int]]       # (stored value, records), most frequent first
    blank: int
    truncated: bool
    statement: Statement
    multi: bool = False


@dataclass(frozen=True, slots=True)
class EntityEntry:
    """One stored spelling of one field in the entity index (:attr:`Catalog.entities`, built at open)."""

    obj: str                # object API name
    field: str              # field API name ("RecordTypeId" for a record type name)
    kind: str               # "name" | "code" | "text" | "picklist" | "record_type"
    value: str              # the stored value (trimmed)
    records: int            # records whose field holds it (multi-select: records whose selection holds it)
    truncated: bool = False  # the field's index was cut (ENTITY_FIELD_MAX, ENTITY_MAX_ENTRIES): the count may be low


@dataclass(eq=False)
class ObjectSpec:
    """One object the record tools know about."""

    api: str
    label: str
    plural_label: str
    documented: bool
    has_table: bool
    rows: int = 0
    fields: dict[str, FieldSpec] = field(default_factory=dict)
    knowledge: ObjectKnowledge | None = None
    one_line: str = ""
    record_grain: str = ""
    description: str = ""
    key_concepts: list[str] = field(default_factory=list)
    concepts: list[str] = field(default_factory=list)
    not_synced: dict[str, dict] = field(default_factory=dict)
    children: list[ChildLink] = field(default_factory=list)
    sensitive: bool = False
    id_prefix: str | None = None
    history_table: str | None = None
    history_parent: str | None = None
    record_types: list[dict] = field(default_factory=list)
    name_auto: bool = False
    name_prefix: str | None = None
    note: str | None = None
    stat_statements: list[Statement] = field(default_factory=list)
    _field_index: KnowledgeIndex | None = None

    @property
    def queryable(self) -> bool:
        return self.has_table

    def field_index(self) -> KnowledgeIndex:
        """BM25 index over this object's fields (knowledge chunks plus undocumented columns)."""
        if self._field_index is None:
            chunks = []
            for f in self.fields.values():
                pick = " ".join(f"{v} {lab}" for v, lab, _ in f.picklist[:40])
                text = f"{f.label} {split_api_name(f.api)} {f.meaning} {f.concept} {' '.join(f.roles)} {pick}"
                chunks.append({"id": f"field:{f.api}", "section": "field", "title": f"{f.api} {f.label}", "text": text})
            self._field_index = KnowledgeIndex(chunks)
            self._field_index.expand = getattr(self, "_field_expand", None)   # Catalog.attach_resolver; None when off
        return self._field_index

    def date_fields(self) -> list[FieldSpec]:
        """Date and datetime fields with data: temporal-semantics fields first, Created/LastModified last."""
        ordered: list[FieldSpec] = []
        seen: set[str] = set()
        if self.knowledge is not None:
            for _key, v in self.knowledge.temporal_semantics().items():
                f = self.fields.get(str((v or {}).get("field") or "")) if isinstance(v, dict) else None
                if f and f.is_date and f.has_data and f.api not in seen and f.api not in ("CreatedDate", "LastModifiedDate"):
                    ordered.append(f)
                    seen.add(f.api)
        for f in self.fields.values():
            if f.is_date and f.has_data and f.api not in seen and f.api not in ("CreatedDate", "LastModifiedDate", "SystemModstamp"):
                ordered.append(f)
                seen.add(f.api)
        for api in ("CreatedDate", "LastModifiedDate"):
            f = self.fields.get(api)
            if f and f.api not in seen:
                ordered.append(f)
        return ordered


@dataclass
class Hop:
    """One lookup step of a path: ``src.field`` points at ``target``."""

    src: str
    field: FieldSpec
    target: str            # "User|Group" for OwnerId


@dataclass
class PathRef:
    """A resolved field or lookup path."""

    text: str              # canonical display, e.g. "Program_Version__c.Program__c"
    hops: list[Hop]
    obj: str               # object that holds ``field``
    field: FieldSpec
    notes: list[str] = field(default_factory=list)

    @property
    def is_path(self) -> bool:
        return bool(self.hops)

    @property
    def is_reference(self) -> bool:
        return self.field.kind == "reference"


class Catalog:
    """Knowledge + warehouse schema + value index (read-only after build)."""

    def __init__(self) -> None:
        self.objects: dict[str, ObjectSpec] = {}
        self.knowledge: dict[str, ObjectKnowledge] = {}
        self.values: dict[tuple[str, str], ValueEntry] = {}
        self.global_values: dict[str, list[tuple[str, str, str, int]]] = {}
        self.sync: dict[str, Any] = {}
        self.blocked_columns: frozenset[str] = frozenset()
        self.blocked_fields: frozenset[tuple[str, str]] = frozenset()
        self.allowed: frozenset[tuple[str, str]] = frozenset()
        self.prefix_to_object: dict[str, str] = {}
        self.number_prefixes: dict[str, list[str]] = {}
        self.build_seconds: float = 0.0
        self.load_errors: list[str] = []
        self._lower: dict[str, str] = {}
        self._labels: dict[str, str] = {}
        self._object_index: KnowledgeIndex | None = None
        self._object_chunks: list[dict] = []
        self._obj_values: dict[str, dict[str, list[tuple[str, str, int]]]] = {}
        self.links: list[IdentityLink] = []
        self._profile: dict[str, list[FieldSpec]] = {}
        self.warehouse: Any = None
        self._value_word_cache: dict[str, frozenset[str]] = {}
        self.resolver: Any = None   # the full term resolver (knowledge.resolver.Resolver; attach_resolver), None: off
        self._resolutions: dict[tuple, Any] = {}   # spelling(): (text, kinds, context, within) -> Resolution
        # The entity index (_entity_index; value spots, 2026-10-28): entity_key -> every stored spelling of it.
        self.entities: dict[str, tuple[EntityEntry, ...]] = {}
        self.entity_compact: dict[str, tuple[str, ...]] = {}   # a Name or code key without spaces -> its keys
        self.entity_max_words: int = 0
        self.entity_fields: dict[tuple[str, str], dict[str, Any]] = {}   # (obj, field) -> kind, distinct, truncated
        self.entity_info: dict[str, Any] = {}
        self.design_phrases: frozenset[str] = frozenset()
        self.label_phrases: frozenset[str] = frozenset()
        self._entity_cues: dict[str, frozenset[str]] = {}

    # ================================================================== build
    @classmethod
    def build(cls, warehouse: Any, knowledge_dir: str) -> "Catalog":
        """Load knowledge, read the schema, run the open-time statements and set the guard policy."""
        t0 = time.perf_counter()
        cat = cls()
        cat.warehouse = warehouse
        for path in knowledge_files(knowledge_dir):
            try:
                kn = ObjectKnowledge.load(path)
            except (OSError, ValueError, TypeError) as exc:  # a file being rewritten, or not knowledge JSON
                cat.load_errors.append(f"{path}: {type(exc).__name__}: {str(exc)[:120]}")
                continue
            cat.knowledge.setdefault(kn.api_name, kn)
        columns = warehouse.columns()
        views = set(columns)
        targets: set[str] = set()
        for kn in cat.knowledge.values():
            for f in kn.fields():
                ref = f.get("reference") or {}
                targets.update(str(t) for t in (ref.get("target_objects") or []) if t)
            for r in kn.relationships():
                targets.update(str(t) for t in (r.get("target_objects") or []) if t)

        def never(name: str) -> bool:
            return name in NEVER_OBJECTS or name.endswith(("History", "Share", "Feed"))

        names: list[str] = []
        for api in sorted(cat.knowledge):
            if not never(api):
                names.append(api)
        for api in sorted(targets) + list(EXTRA_OBJECTS):
            if api in views and api not in cat.knowledge and not never(api) and api not in names:
                names.append(api)
        for api in names:
            kn = cat.knowledge.get(api)
            has_table = api in views
            if kn is None and not has_table:
                continue
            spec = cat._object_spec(api, kn, has_table)
            cat.objects[api] = spec
        # Fields (first pass, without data presence and prefix detection).
        for spec in cat.objects.values():
            if spec.has_table:
                cat._fields(spec, columns[spec.api])
        # Guard policy: queryable objects, their history tables, the system lookups and sync meta.
        # Credential-like names are blocked in every table; every other blocked field (e.g. the text fields
        # of a sensitive object) only in its own table.
        blocked: set[str] = set()
        blocked_fields: set[tuple[str, str]] = set()
        allowed: set[tuple[str, str]] = {("raw", "_sync_meta")}
        for spec in cat.objects.values():
            if not spec.has_table:
                continue
            allowed.add(("main", spec.api))
            for f in spec.fields.values():
                if f.sensitivity == "blocked":
                    blocked_fields.add((spec.api.lower(), f.api.lower()))
                    if BLOCKED_COLUMN_RE.search(f.api):
                        blocked.add(f.api.lower())
            hist = (spec.api[:-3] + "__History") if spec.api.endswith("__c") else spec.api + "History"
            if hist in views and not spec.sensitive:
                hcols = columns[hist]
                parent = "ParentId" if "ParentId" in hcols else (f"{spec.api}Id" if f"{spec.api}Id" in hcols else None)
                if parent and {"Field", "OldValue", "NewValue", "CreatedDate"} <= set(hcols):
                    spec.history_table, spec.history_parent = hist, parent
                    allowed.add(("main", hist))
        for extra in ("RecordType", "User", "Group"):
            if extra in views:
                allowed.add(("main", extra))
        cat.allowed = frozenset(allowed)
        cat.blocked_columns = frozenset(blocked)
        cat.blocked_fields = frozenset(blocked_fields)
        warehouse.set_policy(cat.allowed, cat.blocked_columns, cat.blocked_fields)
        cat.sync = dict(warehouse.sync_times())
        cat._stats()
        cat._finish_fields()
        cat._mirrors()
        cat._unresolved()
        cat._identity_links()
        cat._children()
        cat._record_types()
        cat._value_index()
        cat._object_search_index()
        cat._entity_index()
        cat._lower = {api.lower(): api for api in cat.objects}
        cat.build_seconds = round(time.perf_counter() - t0, 3)
        return cat

    def _object_spec(self, api: str, kn: ObjectKnowledge | None, has_table: bool) -> ObjectSpec:
        if kn is None:
            label = split_api_name(api).title() if api.endswith("__c") else api
            spec = ObjectSpec(api=api, label=label, plural_label=label + "s", documented=False, has_table=has_table)
        else:
            info = kn.object_info
            summary = info.get("discovery_summary") or {}
            sem = info.get("business_semantics") or {}
            spec = ObjectSpec(
                api=api, label=str(info.get("label") or api), plural_label=str(info.get("plural_label") or ""),
                documented=True, has_table=has_table, knowledge=kn,
                one_line=str(summary.get("one_line") or ""),
                record_grain=str((info.get("record_grain") or {}).get("description") or ""),
                description=str(info.get("description") or ""),
                key_concepts=[str(c) for c in summary.get("key_concepts") or []],
                concepts=[str(c) for c in ([sem.get("primary_concept")] + list(sem.get("concepts") or [])) if c],
            )
        spec.sensitive = api in SENSITIVE_OBJECTS
        spec.note = OBJECT_NOTES.get(api)
        return spec

    def _fields(self, spec: ObjectSpec, cols: dict[str, str]) -> None:
        kn = spec.knowledge
        documented: dict[str, dict] = {}
        temporal: dict[str, list[tuple[str, str]]] = {}
        if kn is not None:
            for f in kn.fields():
                if f.get("api_name"):
                    documented[str(f["api_name"])] = f
            for key, v in kn.temporal_semantics().items():
                if isinstance(v, dict) and v.get("field"):
                    temporal.setdefault(str(v["field"]), []).append((str(key), _cut(v.get("business_meaning"), 160)))
        col_lower = {c.lower(): c for c in cols}
        for api, f in documented.items():
            if api.lower() not in col_lower:
                ref = f.get("reference") if isinstance(f.get("reference"), dict) else {}
                sem = f.get("semantic") if isinstance(f.get("semantic"), dict) else {}
                spec.not_synced[api] = {"label": f.get("label") or api, "meaning": _cut(f.get("business_meaning"), 200),
                                        "replaced_by": f.get("replaced_by"),
                                        "targets": [str(t) for t in ref.get("target_objects") or [] if t],
                                        "role": str(ref.get("business_role") or ""),
                                        "concept": str(sem.get("concept") or "")}
        fields: dict[str, FieldSpec] = {}
        order = [col_lower[a.lower()] for a in documented if a.lower() in col_lower]
        order += [c for c in cols if c not in order]
        for col in order:
            db_type = cols[col]
            kf = documented.get(col) or next((v for k, v in documented.items() if k.lower() == col.lower()), None)
            fields[col] = self._field_spec(spec, col, db_type, kf, temporal.get(col, []))
        spec.fields = fields

    def _field_spec(self, spec: ObjectSpec, col: str, db_type: str, kf: dict | None,
                    temporal: list[tuple[str, str]]) -> FieldSpec:
        kb_type = str(kf.get("type")) if kf and kf.get("type") else None
        kind = self._kind(col, db_type, kf)
        if kf is not None:
            meaning = _cut(kf.get("business_meaning"), 200) or SYSTEM_MEANINGS.get(col, "")
            sem = kf.get("semantic") or {}
            flags = ObjectKnowledge.field_flags(kf)
            pick: list[tuple[str, str, str]] = []
            pl = kf.get("picklist") if isinstance(kf.get("picklist"), dict) else None
            for v in (pl or {}).get("values") or []:
                if isinstance(v, dict):
                    value = v.get("value") if v.get("value") is not None else v.get("label")
                    if value is not None:
                        pick.append((str(value), str(v.get("label") or value), _cut(v.get("business_meaning"), 160)))
                elif v is not None:
                    pick.append((str(v), str(v), ""))
            ref = kf.get("reference") or {}
            targets = [str(t) for t in ref.get("target_objects") or [] if t]
            rel = spec.knowledge.relationship_for_field(col) if spec.knowledge is not None else None
            ds = (rel or {}).get("direction_semantics") if isinstance((rel or {}).get("direction_semantics"), dict) else {}
            fs = FieldSpec(api=col, label=str(kf.get("label") or col), kb_type=kb_type, db_type=db_type, kind=kind,
                           meaning=meaning, legacy=bool(kf.get("legacy")) or flags["legacy"],
                           deprecated=bool(kf.get("deprecated")), replaced_by=kf.get("replaced_by") or None,
                           picklist=pick, ref_targets=targets, temporal_keys=temporal,
                           roles=[str(r) for r in sem.get("roles") or []], documented=True,
                           auto_number=kb_type == "auto_number", concept=str(sem.get("concept") or ""),
                           ref_role=str(ref.get("business_role") or "") if isinstance(ref, dict) else "",
                           ref_meaning=_cut(ref.get("business_meaning"), 160) if isinstance(ref, dict) else "",
                           from_target=_cut((ds or {}).get("from_target"), 120),
                           from_source=_cut((ds or {}).get("from_source"), 120))
        else:
            meaning = SYSTEM_MEANINGS.get(col, UNDOCUMENTED)
            fs = FieldSpec(api=col, label=col, kb_type=None, db_type=db_type, kind=kind, meaning=meaning,
                           temporal_keys=temporal, documented=False)
        if col in SYSTEM_REFS:
            fs.kind = "reference"
            fs.system = True
            if not fs.ref_targets:
                fs.ref_targets = list(SYSTEM_REFS[col])
        if col in SYSTEM_ROLES:
            fs.ref_role, fs.concept, fs.from_target = SYSTEM_ROLES[col]
        if col in SYSTEM_MEANINGS:
            fs.system = True
        if col == "Id":
            fs.kind = "id"
        if fs.kind == "reference" and col not in SYSTEM_REFS:
            fs.ref_targets = fs.ref_targets or []
        # Sensitivity.
        scoped = FIELD_SENSITIVITY.get((spec.api, col), ("", ""))[0]
        if (BLOCKED_COLUMN_RE.search(col) and fs.kind not in NO_SECRET_KINDS) or scoped == "blocked":
            fs.sensitivity = "blocked"
        elif spec.sensitive and fs.kind in ("text", "longtext") and col != "Name":
            fs.sensitivity = "blocked"
        elif scoped == "contact" or (db_type == "VARCHAR" and fs.kind != "reference" and CONTACT_COLUMN_RE.search(col)) or \
                (kb_type in ("email", "phone", "address")):   # a link is no contact detail (phase 1): url left out
            fs.sensitivity = "contact"
        return fs

    @staticmethod
    def _kind(col: str, db_type: str, kf: dict | None) -> str:
        db = db_type.upper()
        db_kind = ("boolean" if db == "BOOLEAN" else "date" if db == "DATE" else "datetime" if db.startswith("TIMESTAMP")
                   else "time" if db == "TIME" else
                   "integer" if db in ("BIGINT", "INTEGER", "HUGEINT", "SMALLINT", "TINYINT") or
                   re.fullmatch(r"DECIMAL\(\d+,0\)", db) else
                   "number" if db.startswith(("DECIMAL", "DOUBLE", "FLOAT", "REAL")) else "text")
        kind: str | None = None
        if kf is not None:
            t = str(kf.get("type") or "").lower()
            if t == "formula":
                t = str(kf.get("formula_return_type") or "").lower() or ""
                if t in ("checkbox",):
                    t = "boolean"
                if t in ("string",):
                    t = "text"
            if t in ("number", "rollup_summary", "double", "int", "integer"):
                scale = kf.get("scale")
                kind = "integer" if scale == 0 or db_kind == "integer" else "number"
            elif t in _KB_KIND:
                kind = _KB_KIND[t]
        if kind is None:
            kind = db_kind
            if col == "Name":
                kind = "name"
        # The warehouse type wins when it cannot hold the knowledge kind.
        for k, prefixes in _COMPATIBLE_DB.items():
            if kind == k and not db.startswith(prefixes):
                kind = db_kind
        if kind in ("integer", "percent", "currency") and not db.startswith(_COMPATIBLE_DB["number"]):
            kind = db_kind
        if kind in ("picklist", "multipicklist", "reference", "text", "longtext") and db != "VARCHAR":
            kind = db_kind
        return kind

    # ------------------------------------------------------------------ open-time statements
    def _stats(self) -> None:
        """Row counts, data presence, Id prefixes, auto-number Names and undocumented-lookup prefixes."""
        for spec in self.objects.values():
            if not spec.has_table:
                continue
            exprs: list[tuple[str, str]] = [('COUNT(*)', "__rows")]
            if "Id" in spec.fields:
                exprs.append(('MIN(substr(t0."Id", 1, 3))', "__idp"))
            if "Name" in spec.fields and spec.fields["Name"].sensitivity != "blocked":
                exprs += [(f"COUNT(*) FILTER (WHERE regexp_full_match(t0.\"Name\", '{_AUTO_NUMBER_SQL_RE}'))", "__name_auto"),
                          (f"MIN(regexp_extract(t0.\"Name\", '^([A-Za-z]{{1,6}}-?)[0-9]', 1))", "__name_pmin"),
                          (f"MAX(regexp_extract(t0.\"Name\", '^([A-Za-z]{{1,6}}-?)[0-9]', 1))", "__name_pmax")]
            for f in spec.fields.values():
                if f.sensitivity == "blocked" or f.api.lower() in self.blocked_columns:
                    continue  # the guard refuses any reference to a blocked column, even COUNT(column)
                q = quote_ident(f.api)
                exprs.append((f"COUNT(t0.{q})", f.api))
                if (not f.documented and f.db_type == "VARCHAR" and f.kind == "text" and f.api not in ("Id", "Name")
                        and (f.api.endswith("Id") or f.api.endswith("__c")) and not f.sensitivity):
                    cand = (f"CASE WHEN length(t0.{q}) IN (15, 18) AND regexp_full_match(t0.{q}, '[A-Za-z0-9]+') "
                            f"THEN substr(t0.{q}, 1, 3) END")
                    exprs += [(f"COUNT(DISTINCT {cand})", f"{f.api}__pn"), (f"MIN({cand})", f"{f.api}__p"),
                              (f"COUNT({cand})", f"{f.api}__pc")]
            values: dict[str, Any] = {}
            batch: list[str] = []
            size = 0
            chunks: list[list[str]] = []
            for expr, alias in exprs:
                part = f"{expr} AS {quote_ident(alias)}"
                if batch and size + len(part) > _MAX_STAT_CHARS:
                    chunks.append(batch)
                    batch, size = [], 0
                batch.append(part)
                size += len(part) + 2
            if batch:
                chunks.append(batch)
            for parts in chunks:
                sql = f"SELECT {', '.join(parts)} FROM main.{quote_ident(spec.api)} AS t0"
                st = self.warehouse.run(sql, [], max_rows=1, timeout_s=60)
                st.cached = True
                spec.stat_statements.append(st)
                if st.rows:
                    values.update(dict(zip(st.columns, st.rows[0])))
            spec.rows = int(values.get("__rows") or 0)
            spec.id_prefix = values.get("__idp") or None
            if spec.id_prefix:
                self.prefix_to_object.setdefault(spec.id_prefix, spec.api)
            for f in spec.fields.values():
                n = values.get(f.api)
                f.filled = int(n or 0)
                f.has_data = f.filled > 0
            name = spec.fields.get("Name")
            if name is not None and name.filled:
                auto = int(values.get("__name_auto") or 0)
                pmin, pmax = values.get("__name_pmin"), values.get("__name_pmax")
                if name.auto_number or auto == name.filled:
                    spec.name_auto = True
                    name.auto_number = True
                    if pmin and pmin == pmax:
                        spec.name_prefix = str(pmin)
                        self.number_prefixes.setdefault(norm_key(str(pmin)), []).append(spec.api)
            spec._prefix_stats = values  # type: ignore[attr-defined]

    def _finish_fields(self) -> None:
        """Prefix detection of undocumented lookups and the Name meaning."""
        for spec in self.objects.values():
            stats = getattr(spec, "_prefix_stats", {})
            for f in spec.fields.values():
                if f"{f.api}__pn" in stats and int(stats.get(f"{f.api}__pn") or 0) == 1 and int(stats.get(f"{f.api}__pc") or 0) > 0:
                    target = self.prefix_to_object.get(str(stats.get(f"{f.api}__p")))
                    if target:
                        f.kind = "reference"
                        f.ref_targets = [target]
                        f.meaning = f"{UNDOCUMENTED}; values are {target} Ids (detected from the Id prefix)"
                        f.ref_role = "undocumented"
                if f.api == "Name" and not f.documented:
                    f.meaning = "record number (auto-number)" if spec.name_auto else "record name"
            if hasattr(spec, "_prefix_stats"):
                del spec._prefix_stats  # type: ignore[attr-defined]

    def _mirrors(self) -> None:
        """Lookups that copy a lookup path of the same record (knowledge ``field_dependencies``: ``F`` depends on
        ``X.Y`` where ``X`` is a lookup of the object and ``Y`` a lookup of ``X``'s target to ``F``'s target), e.g.
        a training's Program that must equal its Program Version's Program. Sets ``mirror_of`` and ``mirror_rule``.

        A lookup that copies a lookup of a child object (``F`` depends on ``C.L`` and ``C.K``, where ``C`` is an object
        whose lookup ``K`` points to this object and ``L`` points to ``F``'s target: a candidate's assigned recruiter
        copied from the candidate's Marketing record) gets ``child_copy`` = (C, K, L), ``mirror_rule`` and
        ``copy_source_filled``: the records with a C row whose L is set (one statement per such lookup, kept in
        ``stat_statements``), to compare with ``filled`` (a partial copy fills fewer)."""
        for spec in self.objects.values():
            kn = spec.knowledge
            raw = getattr(kn, "raw", None) if kn is not None else None
            if not spec.has_table or not isinstance(raw, dict):
                continue
            for dep in raw.get("field_dependencies") or []:
                if not isinstance(dep, dict):
                    continue
                f = spec.fields.get(str(dep.get("field") or ""))
                if f is None or f.kind != "reference" or f.system or f.mirror_of or f.child_copy:
                    continue
                t = self.target_of(f)
                self._child_copy(spec, f, t, dep)
                if f.child_copy:
                    continue
                for path in dep.get("depends_on") or []:
                    parts = str(path).split(".")
                    if t is None or len(parts) != 2:
                        continue
                    g = spec.fields.get(parts[0])
                    u = self.target_of(g) if g is not None and g.kind == "reference" else None
                    us = self.objects.get(u) if u else None
                    h = us.fields.get(parts[1]) if us is not None and us.has_table else None
                    if h is not None and h.kind == "reference" and self.target_of(h) == t and g is not f:
                        f.mirror_of, f.mirror_rule = f"{g.api}.{h.api}", _cut(dep.get("meaning"), 120)
                        break

    def _child_copy(self, spec: ObjectSpec, f: FieldSpec, t: str | None, dep: dict) -> None:
        """:meth:`_mirrors` for a lookup that copies a lookup of a child object: ``depends_on`` names ``C.L`` (to
        ``f``'s target) and ``C.K`` (to ``spec``) of one object ``C`` with a table."""
        if t is None or "Id" not in spec.fields:
            return
        by_obj: dict[str, list[str]] = {}
        for path in dep.get("depends_on") or []:
            parts = str(path).split(".")
            if len(parts) == 2 and parts[0] != spec.api and parts[0] in self.objects:
                by_obj.setdefault(parts[0], []).append(parts[1])
        for c, cols in by_obj.items():
            cs = self.objects[c]
            if not cs.has_table or cs.sensitive:
                continue
            ks = [cs.fields[x] for x in cols if x in cs.fields and cs.fields[x].kind == "reference"
                  and self.target_of(cs.fields[x]) == spec.api and cs.fields[x].sensitivity != "blocked"]
            ls = [cs.fields[x] for x in cols if x in cs.fields and cs.fields[x].kind == "reference"
                  and self.target_of(cs.fields[x]) == t and cs.fields[x].sensitivity != "blocked"]
            if len(ks) != 1 or len(ls) != 1 or ks[0] is ls[0]:
                continue
            k, lk = ks[0], ls[0]
            n = 0
            try:
                st = self.warehouse.run(
                    f"SELECT COUNT(*) AS n FROM main.{quote_ident(spec.api)} AS t0 WHERE t0.\"Id\" IN (SELECT c0."
                    f"{quote_ident(k.api)} FROM main.{quote_ident(c)} AS c0 WHERE c0.{quote_ident(lk.api)} IS NOT NULL)",
                    [], max_rows=1, timeout_s=60)
                st.cached = True
                spec.stat_statements.append(st)
                n = int(st.rows[0][0]) if st.rows else 0
            except Exception:  # noqa: BLE001 - the flag stays without the count
                n = 0
            f.child_copy = (c, k.api, lk.api)
            f.copy_source_filled = n
            f.mirror_rule = _cut(dep.get("meaning"), 120)
            return

    def _unresolved(self) -> None:
        """Per filled lookup with one queryable target: how many values point to no record of that target
        (``OwnerId``: of User or Group). One batched statement per object, kept in ``stat_statements``."""
        for spec in self.objects.values():
            if not spec.has_table or not spec.rows:
                continue
            exprs: list[tuple[str, str]] = []
            for f in spec.fields.values():
                if f.kind != "reference" or f.sensitivity == "blocked" or f.filled <= 0 or \
                        f.api.lower() in self.blocked_columns:
                    continue
                if f.api == "OwnerId":
                    targets = [t for t in ("User", "Group") if t in self.objects and self.objects[t].has_table]
                else:
                    t = self.target_of(f)
                    targets = [t] if t else []
                if not targets:
                    continue
                q = quote_ident(f.api)
                miss = " AND ".join(f"t0.{q} NOT IN (SELECT x.\"Id\" FROM main.{quote_ident(t)} AS x)" for t in targets)
                exprs.append((f"COUNT(*) FILTER (WHERE t0.{q} IS NOT NULL AND {miss})", f"{f.api}__u"))
            if not exprs:
                continue
            values: dict[str, Any] = {}
            for parts in _chunks([f"{e} AS {quote_ident(a)}" for e, a in exprs], _MAX_STAT_CHARS):
                st = self.warehouse.run(f"SELECT {', '.join(parts)} FROM main.{quote_ident(spec.api)} AS t0", [],
                                        max_rows=1, timeout_s=60)
                st.cached = True
                spec.stat_statements.append(st)
                if st.rows:
                    values.update(dict(zip(st.columns, st.rows[0])))
            for f in spec.fields.values():
                n = values.get(f"{f.api}__u")
                if n is not None:
                    f.unresolved = int(n or 0)

    def _identity_links(self) -> None:
        """Identity links (L10) from the knowledge: a synced lookup from one person object to another whose role
        contains ``login_user`` or ``person_account``, plus the standard ``User.AccountId`` portal-user link and the
        standard ``Lead.ConvertedAccountId`` link (a converted lead and the account it became; RECORDS_SPEC §22.4)
        when the column holds data."""
        links: list[IdentityLink] = []
        user = self.objects.get("User")
        has_account_id = bool(user is not None and user.has_table and "AccountId" in user.fields)
        for a in PERSON_OBJECTS:
            spec = self.objects.get(a)
            if spec is None or not spec.has_table:
                continue
            for f in spec.fields.values():
                if f.kind != "reference" or f.sensitivity == "blocked" or f.api in SYSTEM_LOOKUPS:
                    continue
                b = self.target_of(f)
                role = f.ref_role or ""
                if b is None or b == a or b not in PERSON_OBJECTS or not ("login_user" in role or "person_account" in role):
                    continue
                links.append(IdentityLink(a, f.api, b, "Id", f"{f.api} ({role.replace('_', ' ')})"))
                if b == "Account" and has_account_id:
                    links.append(IdentityLink(a, f.api, "User", "AccountId", f"portal user (User.AccountId = {f.api})"))
        lead, acc = self.objects.get("Lead"), self.objects.get("Account")
        conv = lead.fields.get("ConvertedAccountId") if lead is not None and lead.has_table else None
        if conv is not None and conv.filled > 0 and acc is not None and acc.has_table and \
                all(lk.col != "ConvertedAccountId" for lk in links if lk.src == "Lead"):
            links.append(IdentityLink("Lead", "ConvertedAccountId", "Account", "Id",
                                      "ConvertedAccountId (lead converted to this account)"))
        self.links = links

    def _children(self) -> None:
        links: dict[str, dict[tuple[str, str], ChildLink]] = {api: {} for api in self.objects}
        for parent in self.objects.values():
            if parent.knowledge is None:
                continue
            for c in parent.knowledge.child_relationships():
                child, via = str(c.get("child_object") or ""), str(c.get("field") or "")
                cs = self.objects.get(child)
                if cs and cs.has_table and via in cs.fields and parent.has_table:
                    vf = cs.fields[via]
                    if vf.kind != "reference" or (vf.ref_targets and parent.api not in vf.ref_targets):
                        continue  # the warehouse column does not hold Ids of this parent
                    links[parent.api][(child, via)] = ChildLink(child, via, _cut(c.get("business_meaning"), 160))
        for child in self.objects.values():
            if not child.has_table:
                continue
            for f in child.fields.values():
                if f.kind != "reference":
                    continue
                for t in f.ref_targets:
                    if t in links and self.objects[t].has_table and (child.api, f.api) not in links[t]:
                        meaning = f.meaning if f.documented else (SYSTEM_MEANINGS.get(f.api) or f.meaning)
                        links[t][(child.api, f.api)] = ChildLink(child.api, f.api, _cut(meaning, 160))
        for api, d in links.items():
            self.objects[api].children = list(d.values())

    def _record_types(self) -> None:
        rt = self.objects.get("RecordType")
        if rt is None or not rt.has_table:
            return
        st = self.warehouse.run('SELECT rt."Id" AS id, rt."Name" AS name, rt."DeveloperName" AS developer_name, '
                                'rt."SobjectType" AS sobject FROM main."RecordType" AS rt', [], max_rows=5000)
        by_obj: dict[str, list[dict]] = {}
        for rid, name, dev, sobj in st.rows:
            by_obj.setdefault(str(sobj), []).append({"id": rid, "name": name, "developer_name": dev, "records": 0})
        for spec in self.objects.values():
            f = spec.fields.get("RecordTypeId")
            if not spec.has_table or f is None or not f.has_data:
                spec.record_types = [dict(r) for r in by_obj.get(spec.api, [])]
                continue
            cst = self.warehouse.run(f'SELECT t0."RecordTypeId" AS id, COUNT(*) AS n FROM main.{quote_ident(spec.api)} AS t0 '
                                     'GROUP BY 1 ORDER BY 2 DESC', [], max_rows=500)
            counts = {r[0]: int(r[1]) for r in cst.rows}
            rows = [dict(r, records=counts.pop(r["id"], 0)) for r in by_obj.get(spec.api, [])]
            rows.sort(key=lambda r: (-r["records"], str(r["name"])))
            if counts.get(None):
                rows.append({"id": None, "name": "(none)", "developer_name": None, "records": counts.pop(None)})
            spec.record_types = rows
        acc = self.objects.get("Account")
        if acc is not None and acc.note:
            person = next((r["records"] for r in acc.record_types if r.get("developer_name") == "PersonAccount"), 0)
            acc.note = acc.note.replace("{person}", str(person))

    def _value_index(self) -> None:
        for spec in self.objects.values():
            if not spec.has_table or spec.rows == 0:
                continue
            obj_q = quote_ident(spec.api)
            # a link is no value list (phase 1: url fields are no contact data any more, the index still leaves them out)
            texts = [f for f in spec.fields.values()
                     if f.kind == "text" and f.has_data and not f.sensitivity and f.api not in ("Id", "Name")
                     and not f.auto_number and f.kb_type != "url"]
            approx: dict[str, int] = {}
            for i in range(0, len(texts), 60):
                part = texts[i:i + 60]
                sql = "SELECT " + ", ".join(f"approx_count_distinct(t0.{quote_ident(f.api)}) AS {quote_ident(f.api)}"
                                            for f in part) + f" FROM main.{obj_q} AS t0"
                st = self.warehouse.run(sql, [], max_rows=1, timeout_s=60)
                if st.rows:
                    approx.update({c: int(v or 0) for c, v in zip(st.columns, st.rows[0])})
            for f in spec.fields.values():
                multi = f.kind == "multipicklist"
                if f.sensitivity == "blocked":
                    continue
                if f.kind in ("picklist", "multipicklist"):
                    pass
                elif f in texts and 0 < approx.get(f.api, 10 ** 6) <= TEXT_INDEX_MAX_DISTINCT:
                    pass
                else:
                    continue
                q = quote_ident(f.api)
                if multi:
                    sql = (f"SELECT v AS value, COUNT(*) AS n FROM (SELECT unnest(string_split(t0.{q}, ';')) AS v "
                           f"FROM main.{obj_q} AS t0) AS s GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT {VALUE_LIMIT}")
                else:
                    sql = (f"SELECT t0.{q} AS value, COUNT(*) AS n FROM main.{obj_q} AS t0 GROUP BY 1 "
                           f"ORDER BY 2 DESC, 1 LIMIT {VALUE_LIMIT}")
                st = self.warehouse.run(sql, [], max_rows=VALUE_LIMIT, timeout_s=60)
                st.cached = True
                vals = [(str(v), int(n)) for v, n in st.rows if v is not None]
                blank = sum(int(n) for v, n in st.rows if v is None)
                if multi:
                    blank = spec.rows - f.filled
                if f.kind == "text" and vals and all(_ID_RE.match(v) for v, _ in vals):
                    continue  # Id-like values are not a useful value list
                if f.kind == "text" and any(hide_embedded_secrets(v) != v for v, _ in vals):
                    continue  # values with a passcode or token (very high): never a value list (phase 1)
                entry = ValueEntry(spec.api, f.api, vals, blank, len(st.rows) >= VALUE_LIMIT, st, multi)
                self.values[(spec.api, f.api)] = entry
                per = self._obj_values.setdefault(spec.api, {})
                for v, n in vals:
                    per.setdefault(normalize_value(v), []).append((f.api, v, n))
                if f.kind in ("picklist", "multipicklist"):
                    for v, n in vals:
                        self.global_values.setdefault(normalize_value(v), []).append((spec.api, f.api, v, n))

    def _object_search_index(self) -> None:
        chunks = []
        for spec in self.objects.values():
            text = " ".join([spec.label, spec.plural_label, split_api_name(spec.api), spec.description, spec.record_grain,
                             " ".join(spec.key_concepts), " ".join(spec.concepts), spec.note or ""])
            chunks.append({"id": spec.api, "section": "object", "title": f"{spec.label} {spec.plural_label} {spec.api}",
                           "text": text})
        self._object_chunks = chunks
        self._object_index = KnowledgeIndex(chunks)

    # ------------------------------------------------------------------ entity index (value spots, 2026-10-28)
    def _entity_index(self) -> None:
        """The entity index: every stored value a person could name, in every queryable object, by
        :func:`entity_key` (:attr:`entities`, :attr:`entity_compact`, :attr:`entity_fields`, :attr:`entity_info`),
        plus the design phrases, label phrases and cue words the value spots read. Never raises.

        Read: the Names of every object with records that is no person object (Account: client companies only),
        auto-number Names and identifier text of the auto-number shape as codes (digits-only codes of auto-number
        Names and of unique ``...Number`` text fields too), short text fields that are no free text, name, link,
        score, time or identifier field, picklists (multi-select values split), and the record type names (no SQL).
        Never read: sensitive and system objects, blocked and contact fields, long text, links, Ids; values that are
        Id-like, e-mail addresses, links, paths, numbers, dates (text: date or amount shapes), too long or without a
        letter; every field one of whose values holds an embedded secret (left out whole, as the value index does;
        by API name in ``entity_info["secret_fields"]``); and every Name or text field whose values hold stored person
        names (ENTITY_PERSON_SHARE: the people path owns them). One statement per object (more when it would be too
        long), not kept in ``stat_statements``; ENTITY_BUILD_BUDGET_S, ENTITY_FIELD_MAX and ENTITY_MAX_ENTRIES cut it
        (flagged in ``entity_info`` and :meth:`entity_lines`; a cut index is never read as "not stored")."""
        t0 = time.perf_counter()
        deadline = t0 + float(ENTITY_BUILD_BUDGET_S)
        info: dict[str, Any] = {"objects": 0, "fields": 0, "fields_by_kind": {}, "keys": 0, "entries": 0,
                                "entries_by_kind": {}, "compact_keys": 0, "max_words": 0, "statements": 0,
                                "sql_seconds": 0.0, "seconds": 0.0, "cut_fields": [], "skipped_objects": [],
                                "cut_reasons": [], "errors": 0, "person_fields": [], "secret_fields": [], "dropped": {}}
        index: dict[str, tuple[EntityEntry, ...]] = {}
        fields: dict[tuple[str, str], dict[str, Any]] = {}
        picklist_words: dict[str, set[str]] = {}
        specs = [s for s in self.objects.values() if s.has_table and s.rows > 0 and not s.sensitive
                 and s.api not in EXTRA_OBJECTS]
        people = self._entity_person_names(deadline, info)
        for n, spec in enumerate(specs):
            if time.perf_counter() > deadline:
                info["skipped_objects"] += [s.api for s in specs[n:]]
                _add_reason(info, "time budget")
                break
            left = int(ENTITY_MAX_ENTRIES) - info["entries"]
            if left <= 0:
                info["skipped_objects"] += [s.api for s in specs[n:]]
                _add_reason(info, "entry limit")
                break
            try:
                self._entity_object(spec, deadline, left, people, index, fields, picklist_words, info)
            except Exception:  # noqa: BLE001 - the index is optional: a failure skips the object, never the open
                info["errors"] += 1
                info["skipped_objects"].append(spec.api)
                _add_reason(info, "statement failed")
        try:
            self._entity_record_types(specs, index, fields, info)
        except Exception:  # noqa: BLE001 - the record type names are optional too
            info["errors"] += 1
        del people   # the person names are held only while the index is built
        self.entities = index
        self.entity_fields = fields
        self.entity_compact = self._entity_compact_keys()
        longest = max((k.count(" ") + 1 for k in self.entities), default=0)
        self.entity_max_words = min(int(ENTITY_NAME_MAX_WORDS), longest)
        self.design_phrases, self.label_phrases = self._entity_phrases()
        self._entity_cues = {s.api: self._entity_cue_words(s, picklist_words.get(s.api, set()))
                             for s in self.objects.values() if s.has_table}
        by_kind: dict[str, int] = {}
        for e in (e for v in self.entities.values() for e in v):
            by_kind[e.kind] = by_kind.get(e.kind, 0) + 1
        f_kind: dict[str, int] = {}
        for d in fields.values():
            f_kind[d["kind"]] = f_kind.get(d["kind"], 0) + 1
        info.update(keys=len(self.entities), entries=sum(by_kind.values()), entries_by_kind=by_kind,
                    fields=len(fields), fields_by_kind=f_kind, compact_keys=len(self.entity_compact),
                    max_words=self.entity_max_words, budget_s=float(ENTITY_BUILD_BUDGET_S),
                    sql_seconds=round(info["sql_seconds"], 3), seconds=round(time.perf_counter() - t0, 3))
        self.entity_info = info

    def _entity_person_names(self, deadline: float, info: dict[str, Any]) -> frozenset[str]:
        """The stored names of the person rows (Recruiter__c, User, Lead and Contact, person Accounts) as
        :func:`entity_key` n-grams: a name of 2 or 3 words, and the first and last word of a longer one. Held only
        while the index is built (it decides which fields hold people, :data:`ENTITY_PERSON_SHARE`); empty when the
        objects are not there or their statements fail (then no field is left out for people)."""
        out: set[str] = set()
        for api in ("Recruiter__c", "User", "Lead", "Contact", "Account"):
            spec = self.objects.get(api)
            if spec is None or not spec.has_table or not spec.rows or "Name" not in spec.fields or \
                    spec.fields["Name"].sensitivity == "blocked" or time.perf_counter() > deadline:
                continue
            where = ""
            if api == "Account":
                if "IsPersonAccount" not in spec.fields:
                    continue
                where = ' AND COALESCE(t0."IsPersonAccount", false)'
            sql = (f'SELECT DISTINCT trim(CAST(t0."Name" AS VARCHAR)) AS v FROM main.{quote_ident(api)} AS t0 '
                   f'WHERE t0."Name" IS NOT NULL{where}')
            try:
                t1 = time.perf_counter()
                st = self.warehouse.run(sql, [], max_rows=500_000, max_bytes=100_000_000,
                                        timeout_s=max(0.2, deadline - t1))
                info["statements"] += 1
                info["sql_seconds"] += time.perf_counter() - t1
            except Exception:  # noqa: BLE001 - without these names no field is left out for people
                info["errors"] += 1
                continue
            for (name,) in st.rows:
                ws = entity_key(name).split()
                if len(ws) < 2:
                    continue
                if len(ws) <= 3:
                    out.add(" ".join(ws))
                if len(ws) >= 3:
                    out.add(f"{ws[0]} {ws[-1]}")
        return frozenset(out)

    def _entity_plan(self, spec: ObjectSpec) -> list[tuple[FieldSpec, str, str, bool]]:
        """The fields of ``spec`` the entity index reads: ``(field, source, row filter, number)``. Sources: ``name``
        (a Name), ``code`` (an auto-number Name), ``code_text`` (identifier text: only values of the auto-number
        shape), ``number`` (a ``...Number`` text field: only digits-only values), ``text``, ``picklist`` and
        ``multipicklist``. ``number``: the API name ends in ``Number`` (its digits-only values are codes when every
        record has its own value, on at least ENTITY_NUMBER_MIN_FILLED records). Blocked, contact and empty fields are never read."""
        voc = _vocabulary
        out: list[tuple[FieldSpec, str, str, bool]] = []
        person = spec.api in _PERSON_RECORDS
        for f in spec.fields.values():
            if not f.has_data or f.sensitivity or f.api.lower() in self.blocked_columns:
                continue
            if f.api == "Name" and f.kind in ("name", "text"):
                where = ""
                if spec.api in _PERSON_NAMED:
                    if spec.api != "Account" or "IsPersonAccount" not in spec.fields:
                        continue   # people (the people path), or no way to tell people from client companies
                    where = 'NOT COALESCE(t0."IsPersonAccount", false)'
                out.append((f, "code" if (f.auto_number or spec.name_auto) else "name", where, False))
            elif f.kind in ("picklist", "multipicklist"):
                out.append((f, f.kind, "", False))
            elif f.kind == "text" and f.kb_type != "url" and f.api != "Id" and not f.api.lower().endswith("url"):
                roles = set(f.roles)
                number = not person and re.sub(r"__c$", "", f.api, flags=re.IGNORECASE).lower().endswith("number")
                source = ""
                if f.auto_number or voc._FREE_TEXT_NAME_RE.search(f.api) or roles & voc.FREE_TEXT_ROLES:
                    source = ""
                elif roles & voc.DROP_ROLES:
                    if "identifier" in roles and not roles & (voc.DROP_ROLES - {"identifier"}) and not person:
                        source = "code_text"
                elif (split_words(f.api) or [""])[-1] not in voc._ID_WORDS:
                    source = "text"
                if not source and number:
                    source = "number"
                if source:
                    out.append((f, source, "", number))
        return out

    @staticmethod
    def _entity_parts(spec: ObjectSpec, plan: list[tuple[FieldSpec, str, str, bool]]) -> list[str]:
        """The SELECTs of one object's planned fields, ``(f, v)`` rows (``f``: the plan index, ``v``: one trimmed
        value; NULL for a value left out), joined by UNION ALL into one statement per object. The single-valued
        fields share one scan of the table (parallel ``unnest`` of a list of plan indexes and a list of values, at
        most _MAX_STAT_CHARS / 2 characters each); a multi-select field is split on ``;`` in its own SELECT. SQL
        already leaves out the values over the length limit of their kind and identifier text that is not of the
        auto-number shape (fewer rows to fetch; a ``...Number`` field keeps every value, its distinct count tells
        whether every record has its own)."""
        oq = quote_ident(spec.api)
        parts: list[str] = []
        idx: list[str] = []
        exprs: list[str] = []
        size = 0

        def scan() -> None:
            if exprs:
                parts.append(f"SELECT unnest([{', '.join(idx)}]) AS f, unnest([{', '.join(exprs)}]) AS v "
                             f"FROM main.{oq} AS t0")
            idx.clear()
            exprs.clear()

        for i, (f, source, where, number) in enumerate(plan):
            v = f"trim(CAST(t0.{quote_ident(f.api)} AS VARCHAR))"
            if source == "multipicklist":
                parts.append(f"SELECT {i} AS f, trim(unnest(string_split({v}, ';'))) AS v FROM main.{oq} AS t0 "
                             f"WHERE {v} IS NOT NULL" + (f" AND {where}" if where else ""))
                continue
            name_like = source in _ENTITY_LONG_SOURCES
            cond = [f"length({v}) <= {int(ENTITY_NAME_MAX_CHARS if name_like else _vocabulary.MAX_VALUE_CHARS)}"]
            if where:
                cond.append(where)
            if source == "code_text" and not number:
                cond.append(f"regexp_full_match({v}, '{_AUTO_NUMBER_SQL_RE}')")
            expr = f"CASE WHEN {' AND '.join(cond)} THEN {v} END"
            if exprs and size + len(expr) > _MAX_STAT_CHARS // 2:
                scan()
                size = 0
            idx.append(str(i))
            exprs.append(expr)
            size += len(expr) + 2
        scan()
        return parts

    def _entity_object(self, spec: ObjectSpec, deadline: float, left: int, people: frozenset[str],
                       index: dict[str, tuple[EntityEntry, ...]], fields: dict[tuple[str, str], dict[str, Any]],
                       picklist_words: dict[str, set[str]], info: dict[str, Any]) -> None:
        """Read one object's planned fields (one statement, or more when it would be too long) and add their values
        to ``index``. At most ``left`` entries; a failing or timed-out statement skips the object."""
        plan = self._entity_plan(spec)
        if not plan:
            return
        parts = self._entity_parts(spec, plan)
        rows: dict[int, list[tuple[str, int]]] = {}
        fetched = 0
        cut = False                    # the fetch stopped at ``left`` rows (ENTITY_MAX_ENTRIES): every field is cut
        for batch in _chunks(parts, _MAX_STAT_CHARS):
            sql = ("SELECT u.f AS f, u.v AS v, COUNT(*) AS n FROM (" + " UNION ALL ".join(batch) + ") AS u "
                   "WHERE u.v IS NOT NULL GROUP BY u.f, u.v")
            t1 = time.perf_counter()
            if t1 > deadline:
                info["skipped_objects"].append(spec.api)
                _add_reason(info, "time budget")
                return
            try:
                st = self.warehouse.run(sql, [], max_rows=max(1, left - fetched), max_bytes=200_000_000,
                                        timeout_s=max(0.2, deadline - t1))
            except Exception:  # noqa: BLE001 - a refused, failing or stopped statement skips this object
                info["skipped_objects"].append(spec.api)
                if time.perf_counter() > deadline:
                    _add_reason(info, "time budget")   # stopped at the budget: not read in time
                else:
                    info["errors"] += 1
                    _add_reason(info, "statement failed")
                return
            info["statements"] += 1
            info["sql_seconds"] += time.perf_counter() - t1
            cut = cut or st.truncated
            fetched += st.row_count
            for fi, v, n in st.rows:
                rows.setdefault(fi, []).append((v, n))
        info["objects"] += 1
        cap = int(ENTITY_FIELD_MAX)
        dropped = info["dropped"]
        for i, (f, source, _where, number) in enumerate(plan):
            got = rows.get(i)
            if not got:
                continue
            kind = _ENTITY_KINDS[source]
            if any(_SECRET_WORD_RE.search(str(v).lower()) and hide_embedded_secrets(str(v)) != str(v)
                   for v, _n in got if v is not None):
                # a value with an embedded secret (very high, phase 1): the whole field stays out, as the value index
                # leaves it out (review PPI-2: its other values may be bare tokens or passcodes)
                info["secret_fields"].append(f"{spec.api}.{f.api}")
                dropped["secret"] = dropped.get("secret", 0) + len(got)
                continue
            distinct = len(got)
            truncated = cut or distinct > cap
            if distinct > cap:   # the most frequent values of the field
                got = sorted(got, key=lambda r: (-r[1], str(r[0])))[:cap]
            digits = source == "code" or (number and not cut and distinct == f.filled
                                          and f.filled >= int(ENTITY_NUMBER_MIN_FILLED))
            kept: dict[str, tuple[str, int]] = {}     # value -> (key, records); one entry per stored spelling
            for v, n in got:
                s = str(v if v is not None else "").strip()
                key = _entity_value_key(s, source, digits, dropped)
                if key:
                    same = kept.get(s)
                    kept[s] = (key, n + same[1]) if same else (key, n)
            if not kept:
                continue
            if kind in ("name", "text") and people and _holds_people(kept, people):
                info["person_fields"].append(f"{spec.api}.{f.api}")
                continue
            if truncated:
                info["cut_fields"].append(f"{spec.api}.{f.api}")
                _add_reason(info, "entry limit" if cut else "field limit")
            fields[(spec.api, f.api)] = {"kind": kind, "distinct": distinct, "truncated": truncated}
            for s, (key, n) in kept.items():
                entry = EntityEntry(spec.api, f.api, kind, s, n, truncated)
                have = index.get(key)
                index[key] = (entry,) if have is None else have + (entry,)
                if kind == "picklist" and " " not in key:
                    picklist_words.setdefault(spec.api, set()).add(key)
            info["entries"] += len(kept)

    def _entity_record_types(self, specs: list[ObjectSpec], index: dict[str, tuple[EntityEntry, ...]],
                             fields: dict[tuple[str, str], dict[str, Any]], info: dict[str, Any]) -> None:
        """The record type names with records of the indexed objects (``ObjectSpec.record_types``, no SQL)."""
        for spec in specs:
            names = [(str(r.get("name") or "").strip(), int(r.get("records") or 0)) for r in spec.record_types
                     if r.get("id") is not None]
            names = [(s, n) for s, n in names if s and n > 0 and entity_key(s)]
            if not names or "RecordTypeId" not in spec.fields:
                continue
            fields[(spec.api, "RecordTypeId")] = {"kind": "record_type", "distinct": len(names), "truncated": False}
            for s, n in names:
                key = entity_key(s)
                index[key] = index.get(key, ()) + (EntityEntry(spec.api, "RecordTypeId", "record_type", s, n),)
                info["entries"] += 1

    def _entity_compact_keys(self) -> dict[str, tuple[str, ...]]:
        """Name and code keys of 2 or more words without their spaces ("vex orbit" -> "vexorbit"), for a name
        written as one word or a code written without its dash; only forms of 4 or more characters that are no key
        of another kind."""
        out: dict[str, list[str]] = {}
        for key, entries in self.entities.items():
            if " " not in key or not any(e.kind in ("name", "code") for e in entries):
                continue
            compact = key.replace(" ", "")
            if len(compact) < 4:
                continue
            other = self.entities.get(compact)
            if other and any(e.kind not in ("name", "code") for e in other):
                continue
            out.setdefault(compact, []).append(key)
        return {k: tuple(v) for k, v in out.items()}

    def _entity_phrases(self) -> tuple[frozenset[str], frozenset[str]]:
        """``(design phrases, label phrases)`` as :func:`entity_key` keys. Design phrases: object labels and plural
        labels, concepts and key concepts, record type names and labels, the labels, picklist values and picklist
        labels of every knowledge field (synced or not) and of every catalog field, and the stored picklist values
        (``global_values``). Label phrases: object labels, plural labels and field labels."""
        design: set[str] = set()
        labels: set[str] = set()
        for spec in self.objects.values():
            for t in (spec.label, spec.plural_label):
                design.add(entity_key(t))
                labels.add(entity_key(t))
            for t in list(spec.concepts) + list(spec.key_concepts):
                design.add(entity_key(t))
            for r in spec.record_types:
                design.add(entity_key(r.get("name")))
            for f in spec.fields.values():
                design.add(entity_key(f.label))
                labels.add(entity_key(f.label))
                for v, lab, _m in f.picklist:
                    design.update((entity_key(v), entity_key(lab)))
        for kn in self.knowledge.values():
            for kf in kn.fields():
                design.add(entity_key(kf.get("label")))
                pl = kf.get("picklist") if isinstance(kf.get("picklist"), dict) else {}
                vals = pl.get("values") if isinstance(pl.get("values"), list) else []
                for v in vals:
                    if isinstance(v, dict):
                        design.update((entity_key(v.get("value")), entity_key(v.get("label"))))
                    elif v is not None:
                        design.add(entity_key(v))
            for rt in kn.record_types():
                if isinstance(rt, dict):
                    design.add(entity_key(rt.get("label") or rt.get("name")))
        design.update(entity_key(k) for k in self.global_values)
        design.discard("")
        labels.discard("")
        return frozenset(design), frozenset(labels)

    def _entity_cue_words(self, spec: ObjectSpec, picklist_words: set[str]) -> frozenset[str]:
        """Cue words of an object (a value next to one is named as that object's record: "the Zorvane team"): the
        head noun of its label and plural label, its one-word record type names (not "Master" or "(none)") and its
        one-word concepts; at least 3 letters, no function word, no blank value, no stored picklist value of the
        object ("active")."""
        words: set[str] = set()
        for t in (spec.label, spec.plural_label):
            ws = entity_key(t).split()
            if ws:
                words.add(ws[-1])
        for r in spec.record_types:
            name = str(r.get("name") or "")
            ws = entity_key(name).split()
            if len(ws) == 1 and name.strip().lower() not in _NO_CUE_RECORD_TYPES:
                words.add(ws[0])
        for c in spec.concepts:
            ws = entity_key(c).split()
            if len(ws) == 1:
                words.add(ws[0])
        return frozenset(w for w in words if len(w) >= 3 and w not in STOPWORDS and w not in BLANK_VALUES
                         and w not in picklist_words)

    def entity_entries(self, key: str) -> tuple[EntityEntry, ...]:
        """The stored spellings of entity key ``key`` (every object and field holding it); () when none."""
        return self.entities.get(key, ())

    def entity_cues(self, obj: str) -> frozenset[str]:
        """Cue words of object ``obj`` (:meth:`_entity_cue_words`); empty for an object without a table."""
        return self._entity_cues.get(obj, frozenset())

    def field_cues(self, obj: str, field_api: str) -> frozenset[str]:
        """Cue words of a text field: the head noun of its label (Company Name: "name"; Company: "company"), at least
        3 letters and no function word; empty for an unknown field."""
        spec = self.objects.get(obj)
        f = spec.fields.get(field_api) if spec is not None else None
        ws = entity_key(f.label).split() if f is not None else []
        if not ws or len(ws[-1]) < 3 or ws[-1] in STOPWORDS:
            return frozenset()
        return frozenset({ws[-1]})

    def entity_lines(self) -> list[str]:
        """The start-up line of a cut entity index (fields cut at a limit, objects skipped): [] when nothing was cut,
        so the start-up lines stay as they were."""
        info = self.entity_info or {}
        cut, skipped = len(info.get("cut_fields") or []), len(info.get("skipped_objects") or [])
        if not cut and not skipped:
            return []
        reasons = ", ".join(info.get("cut_reasons") or []) or "statement failed"
        return [f"value index cut: {cut} fields, {skipped} objects skipped ({reasons})"]

    def attach_resolver(self, resolver: Any) -> None:
        """Give the catalog the session's term resolver (docs/resolver_integration.md, "Build"): the one entry point,
        called by ``open_records`` right after :meth:`build` when the spelling help is on (``SFA_RESOLVER=on``).
        ``resolver`` is a :class:`sf_assistant.knowledge.resolver.Resolver` over the knowledge files, the word list
        and the snapshot (stored values, person names), read-only and shared by every thread; None turns it off.
        Without a call (the default) ``self.resolver`` stays None and nothing changes.

        The catalog's BM25 indexes (the object index of :meth:`search_objects` and each object's
        :meth:`ObjectSpec.field_index`, built now or on first use) get the resolver's expander over the metadata kinds
        (``knowledge.search.resolver_expander``, docs/resolver_integration.md (2)): a misspelt search word is searched
        as it reads; a query whose words are all index words ranks exactly as without it."""
        self.resolver = resolver
        self._resolutions = {}
        expand = resolver_expander(resolver, kinds=METADATA_KINDS) if resolver is not None else None
        if self._object_index is not None:
            self._object_index.expand = expand
        for spec in self.objects.values():
            spec._field_expand = expand
            if spec._field_index is not None:
                spec._field_index.expand = expand

    def search_read_as(self, query: str) -> dict[str, str]:
        """``{typed word: the index words searched for it}`` of :meth:`search_objects` for ``query`` (the object
        index's ``KnowledgeIndex.search_read_as``); ``{}`` when the spelling help is off or the search read every word
        as typed. ``find_objects`` reports it as ``read_as``."""
        if self.resolver is None or self._object_index is None:
            return {}
        try:
            return dict(self._object_index.search_read_as(query, k=1)[1])
        except Exception:  # noqa: BLE001 - the spelling help is optional
            return {}

    def spelling(self, text: Any, kinds: Iterable[str], context_object: str | None = None,
                 within: str | None = None) -> Any:
        """The resolver's reading of one tool argument (``Resolver.resolve_term``, a tool lookup), cached per catalog
        (the resolver is read-only); None when the spelling help is off. Called only after every exact tier failed,
        so a correctly spelt argument never reaches it."""
        r = self.resolver
        if r is None:
            return None
        key = (str(text), tuple(kinds), context_object, within)
        res = self._resolutions.get(key)
        if res is None:
            res = r.resolve_term(text, kinds=list(kinds), context_object=context_object, within=within)
            if len(self._resolutions) >= _RESOLUTION_CACHE_MAX:
                self._resolutions = {}
            self._resolutions[key] = res
        return res

    def _close_object(self, raw: str) -> tuple[str | None, list[str]]:
        """Step (3) for an object name no exact tier knows: ``(API name to use, [])`` for one ``corrected`` match
        inside a tool call (recorded in :data:`READ_AS`), else ``(None, did-you-means)``: the matches of a
        ``corrected`` reading outside a tool call, of an ``ambiguous`` one or of an ``exact`` one (at most 3; [] for
        ``none``, whose error keeps today's text)."""
        res = self.spelling(raw, ("object",))
        if res is None:
            return None, []
        apis = [t for t in res.targets if t in self.objects and t not in NEVER_OBJECTS]
        if res.decision == "corrected" and len(apis) == 1 and in_tool_call():
            api = apis[0]
            record_read_as(raw, api, "object", api, "closest object name",
                           f"object '{raw}' read as '{api}' (closest object name)")
            return api, []
        return None, (apis[:3] if res.decision in _SUGGEST else [])

    def _object_names(self, apis: list[str]) -> str:
        """``Interview__c (Interview) or Internal_Interview__c (Internal Interview)`` for a did-you-mean."""
        return _or([a if norm_key(self.objects[a].label) == norm_key(a) else f"{a} ({self.objects[a].label})"
                    for a in apis])

    def _close_field(self, spec: ObjectSpec, raw: str) -> FieldSpec | None:
        """Step (3) for a field name no exact tier of :meth:`field` knows: the field of one ``corrected`` match
        inside a tool call (recorded in :data:`READ_AS`), else None. A documented field that is not synced raises E2b
        with the reading stated; the sensitivity checks stay with the callers (E2c)."""
        res = self.spelling(raw, ("field",), spec.api, f"{spec.api}.")
        if res is None or res.decision != "corrected" or not in_tool_call():
            return None
        prefix = spec.api + "."
        apis = list(dict.fromkeys(t[len(prefix):] for t in res.targets if t.startswith(prefix)))
        if len(apis) != 1 or (apis[0] not in spec.fields and apis[0] not in spec.not_synced):
            return None
        api = apis[0]
        note = f"field '{raw}' read as '{api}' (closest field name of {spec.api})"
        record_read_as(raw, api, "field", f"{spec.api}.{api}", "closest field name", note)
        if api in spec.fields:
            return spec.fields[api]
        err = self._not_synced(spec, api)
        raise RecordError(f"{note}: {err}", err.code)

    def _field_elsewhere(self, spec: ObjectSpec, raw: str) -> bool:
        """``raw`` is the API name (any case) of a field of another queryable object: the agent picked the wrong
        object, so the spelling help never binds a field of ``spec`` for it (:meth:`unknown_field_text` names the
        other object)."""
        low = raw.lower()
        return any(o.has_table and o.api != spec.api and any(a.lower() == low for a in o.fields)
                   for o in self.objects.values())

    def _close_field_names(self, spec: ObjectSpec, raw: str) -> list[str]:
        """The resolver's did-you-means for an unknown field of ``spec`` (queryable, not blocked; at most 3)."""
        res = self.spelling(raw, ("field",), spec.api, f"{spec.api}.")
        if res is None or res.decision not in _SUGGEST:
            return []
        prefix = spec.api + "."
        apis = dict.fromkeys(t[len(prefix):] for t in res.targets if t.startswith(prefix))
        return [a for a in apis if a in spec.fields and spec.fields[a].sensitivity != "blocked"][:3]

    # ================================================================== lookups
    def queryable(self) -> list[ObjectSpec]:
        return [s for s in self.objects.values() if s.has_table]

    def synced_at(self, api: str) -> str | None:
        """Last sync of ``api`` as 'YYYY-MM-DD HH:MM UTC' (case-insensitive object match)."""
        ts = self.sync.get(api)
        if ts is None:
            low = api.lower()
            ts = next((v for k, v in self.sync.items() if k.lower() == low), None)
        return ts.strftime("%Y-%m-%d %H:%M UTC") if ts is not None else None

    def sync_dt(self, api: str) -> Any:
        ts = self.sync.get(api)
        if ts is None:
            low = api.lower()
            ts = next((v for k, v in self.sync.items() if k.lower() == low), None)
        return ts

    def search_objects(self, query: str, k: int = 5) -> list[tuple[ObjectSpec, float, list[str]]]:
        """BM25 over one chunk per object plus a bonus of 2 for an exact label, plural or API name match."""
        qn = " " + " ".join(tokenize(query)) + " "
        qraw = norm_key(query)
        scored: dict[str, float] = {}
        matched: dict[str, list[str]] = {}
        if self._object_index is not None:
            for hit in self._object_index.search(query, k=len(self._object_chunks)):
                scored[hit["id"]] = float(hit["score"])
                matched.setdefault(hit["id"], []).append("knowledge")
        for spec in self.objects.values():
            for what, val in (("api_name", spec.api), ("label", spec.label), ("plural_label", spec.plural_label)):
                if not val:
                    continue
                toks = " ".join(tokenize(val))
                if (toks and f" {toks} " in qn) or norm_key(val) == qraw:
                    scored[spec.api] = scored.get(spec.api, 0.0) + 2.0
                    matched.setdefault(spec.api, []).append(what)
                    break
            for kc in spec.key_concepts:
                toks = " ".join(tokenize(kc))
                if toks and f" {toks} " in qn:
                    matched.setdefault(spec.api, []).append("key_concepts")
                    break
        ranked = sorted(scored.items(), key=lambda t: (-t[1], t[0]))[: max(1, k)]
        return [(self.objects[a], round(s, 3), list(dict.fromkeys(m for m in matched.get(a, []) if m != "knowledge"))
                 or ["description"]) for a, s in ranked if s > 0]

    def resolve_object(self, name: Any) -> ObjectSpec:
        """Object by API name, label, plural label or key concept. Raises E1/E1b/E1c."""
        raw = str(name or "").strip()
        if not raw:
            raise RecordError("object is missing. Call find_objects with the user's words.", "E1")
        api = self._lower.get(raw.lower())
        if api is None and raw.lower().endswith("__r"):
            api = self._lower.get(raw.lower()[:-3] + "__c")
        if api is None:
            low = raw.lower()
            for spec in self.objects.values():
                if low in (spec.label.lower(), spec.plural_label.lower()):
                    api = spec.api
                    break
        if api is None:
            low = raw.lower()
            for spec in self.objects.values():
                if any(low == c.lower() for c in spec.key_concepts):
                    api = spec.api
                    break
        if api is None:
            nk = norm_key(raw)
            for spec in self.objects.values():
                if nk and nk in (norm_key(spec.api), norm_key(spec.label), norm_key(spec.plural_label)):
                    api = spec.api
                    break
        if api is None:
            base = raw.split(".")[0]
            if base in NEVER_OBJECTS or base.endswith(("History", "Share", "Feed")) or raw.lower().startswith("raw."):
                raise RecordError(f"{raw} cannot be queried with the record tools (history, sharing, feed and setup "
                                  "tables are excluded; use get_field_history for field changes).", "E1c")
            dym: list[str] = []
            if self.resolver is not None:   # the spelling help (SFA_RESOLVER=on): one clear match, or did-you-means
                api, dym = self._close_object(raw)
            if api is None:
                close = [s.api for s, _, _ in self.search_objects(raw, k=3)]
                if len(close) < 3:
                    more = difflib.get_close_matches(raw, list(self.objects), n=3, cutoff=0.5)
                    close = list(dict.fromkeys(close + more))[:3]
                if dym:
                    raise with_did_you_mean(RecordError(
                        f"unknown object '{raw}'. Did you mean {self._object_names(dym)}? Closest: "
                        f"{', '.join(close) or 'none'}. Call find_objects with the user's words.", "E1"), raw, "object", dym)
                raise RecordError(f"unknown object '{raw}'. Closest: {', '.join(close) or 'none'}. Call find_objects with "
                                  "the user's words.", "E1")
            spec = self.objects[api]
            if not spec.has_table:
                raise RecordError(f"object '{raw}' read as '{api}' (closest object name): {api} is documented in the "
                                  "knowledge but has no table in the warehouse, so it has no records to query. Answer "
                                  "from the knowledge and say its records are not available.", "E1b")
            return spec
        spec = self.objects[api]
        if not spec.has_table:
            raise RecordError(f"{api} is documented in the knowledge but has no table in the warehouse, so it has no "
                              "records to query. Answer from the knowledge and say its records are not available.", "E1b")
        return spec

    def field(self, spec: ObjectSpec, name: Any) -> FieldSpec:
        """Field of ``spec`` by API name, label or loose match. Raises E2/E2b."""
        raw = str(name or "").strip()
        if not raw:
            raise RecordError(f"field is missing. Call describe_fields(object='{spec.api}', query='<words>') to see "
                              "the fields.", "E2")
        if raw in spec.fields:
            return spec.fields[raw]
        low = raw.lower()
        for api, f in spec.fields.items():
            if api.lower() == low:
                return f
        for api, f in spec.fields.items():
            if f.documented and f.label.lower() == low:
                return f
        for api, nf in spec.not_synced.items():
            if api.lower() == low or str(nf.get("label") or "").lower() == low:
                raise self._not_synced(spec, api)
        if spec.knowledge is not None:
            kf = spec.knowledge.field(raw)
            if kf is not None:
                api = str(kf.get("api_name"))
                if api in spec.fields:
                    return spec.fields[api]
                if api in spec.not_synced:
                    raise self._not_synced(spec, api)
        nk = norm_key(raw)
        if nk:
            for api, f in spec.fields.items():
                if norm_key(api) == nk or (f.documented and norm_key(f.label) == nk):
                    return f
            for alias, target in (("created", "CreatedDate"), ("createddate", "CreatedDate"), ("owner", "OwnerId"),
                                  ("recordtype", "RecordTypeId"), ("createdby", "CreatedById"),
                                  ("lastmodifiedby", "LastModifiedById"), ("modified", "LastModifiedDate"),
                                  ("lastmodified", "LastModifiedDate"), ("updated", "LastModifiedDate")):
                if nk == alias and target in spec.fields:
                    return spec.fields[target]
        if self.resolver is not None:   # the spelling help (SFA_RESOLVER=on): one clear match, or did-you-means
            # the exact API name of another object's field is the wrong object, not a typo: E2 says "check the object"
            close = None if self._field_elsewhere(spec, raw) else self._close_field(spec, raw)
            if close is not None:
                return close
            err = RecordError(self.unknown_field_text(spec, raw), "E2")
            dym = self._close_field_names(spec, raw)
            raise with_did_you_mean(err, raw, "field", dym) if dym else err
        raise RecordError(self.unknown_field_text(spec, raw), "E2")

    def _not_synced(self, spec: ObjectSpec, api: str) -> RecordError:
        nf = spec.not_synced[api]
        target = self._not_synced_target(nf)
        if target is not None:
            return RecordError(self._not_synced_lookup_text(spec, api, nf, target), "E2b")
        text = f"{spec.api}.{api} is documented in the knowledge but is not synced to the warehouse, so it cannot be queried."
        rb = nf.get("replaced_by")
        if rb and rb in spec.fields:
            text += f" Use {rb} instead."
        else:
            alts = [f.api for f in self.suggest_fields(spec, f"{nf.get('label')} {nf.get('meaning')}", 3)]
            if alts:
                text += f" Fields with similar meaning that can: {', '.join(alts)}."
        return RecordError(text, "E2b")

    def _not_synced_lookup_text(self, spec: ObjectSpec, api: str, nf: dict, target: str) -> str:
        """E2b for a documented lookup that is not synced, with the lookups to the same target that can be queried."""
        role = str(nf.get("role") or "").replace("_", " ").strip() or _cut(nf.get("concept") or nf.get("label"), 60)
        tlabel = self.label_of(target)
        text = (f"{spec.api}.{api} ({role}) is documented in the knowledge but is not synced to the warehouse, so it "
                "cannot be queried; say it is not recorded in the warehouse, never 0.")
        rb = nf.get("replaced_by")
        if rb and rb in spec.fields:
            text += f" Use {rb} instead."
        common = set(tokenize(" ".join(self._object_words(spec.api, target)))) | {"employee", "user", "record", "person"}
        want_role = set(tokenize(f"{role} {nf.get('label') or ''}")) - common
        want = (set(tokenize(str(nf.get("concept") or ""))) | want_role) - common
        cands: list[tuple[int, int, int, str, str]] = []

        def score(owner: ObjectSpec, g: FieldSpec) -> int:
            rw = set(tokenize(" ".join(self.role_words(owner, g)))) - common
            have = (set(tokenize(g.concept if g.ref_role != "undocumented" else "")) | rw) - common
            return 2 * len(want_role & rw) + len(want & have)

        def ok(g: FieldSpec) -> bool:
            return (g.kind == "reference" and g.sensitivity != "blocked" and g.filled > 0
                    and g.api not in SYSTEM_LOOKUPS and g.api != "RecordTypeId")

        for g in spec.fields.values():
            if ok(g) and self.target_of(g) == target:
                cands.append((score(spec, g), 0, g.filled, g.api, g.role or g.role_text()))
        for p in spec.fields.values():
            q = self.target_of(p) if ok(p) else None
            if q is None or q in (target, spec.api):
                continue
            qs = self.objects[q]
            for g in qs.fields.values():
                if ok(g) and self.target_of(g) == target:
                    cands.append((score(qs, g), 1, g.filled, f"{p.api}.{g.api}", g.role or g.role_text()))
        cands.sort(key=lambda c: (-c[0], c[1], -c[2], c[3]))
        if cands:
            text += (f" Lookups to {tlabel} that can be queried (other roles; call one a proxy if you use it): "
                     + ", ".join(f"{c[3]} ({c[4]})" for c in cands[:3]) + ".")
        else:
            text += f" No other lookup of {spec.api} to {tlabel} can be queried."
        return text

    def suggest_fields(self, spec: ObjectSpec, text: str, n: int = 3) -> list[FieldSpec]:
        """Closest fields by difflib over API names and labels, then BM25 over field meanings."""
        cands: dict[str, str] = {}
        for api, f in spec.fields.items():
            if f.sensitivity == "blocked":
                continue
            for key in (api.lower(), f.label.lower(), split_api_name(api)):
                if key:
                    cands.setdefault(key, api)
        out: list[str] = []
        # A field whose API name ends with the asked name ("Status__c" -> Background_Check_Status__c), or whose label
        # ends with its words, is the likeliest fix: difflib ranks such long names too low. Fields with data first.
        raw = re.sub(r"__(c|r)$", "", str(text).strip(), flags=re.IGNORECASE).lower()
        words = split_api_name(str(text)).lower() if raw else ""
        if len(raw) >= 3:
            ends = [f for api, f in spec.fields.items() if f.sensitivity != "blocked" and api.lower() != f"{raw}__c"
                    and (re.sub(r"__(c|r)$", "", api.lower()).endswith("_" + raw)
                         or (words and f.label.lower().endswith(" " + words)))]
            own = re.sub(r"__(c|r)$", "", spec.api.lower())   # the object's own <Object>_<raw> field first

            def own_first(f: FieldSpec) -> int:
                return 0 if re.sub(r"__(c|r)$", "", f.api.lower()) == f"{own}_{raw}" else 1

            ends.sort(key=lambda f: (0 if f.has_data else 1, own_first(f), 0 if f.documented else 1, -f.filled, f.api))
            out += [f.api for f in ends[:n]]
        for m in difflib.get_close_matches(str(text).lower(), list(cands), n=6, cutoff=0.55):
            if cands[m] not in out:
                out.append(cands[m])
        for hit in spec.field_index().search(str(text), k=8):
            api = hit["id"].split(":", 1)[1]
            if api not in out and spec.fields.get(api) and spec.fields[api].sensitivity != "blocked":
                out.append(api)
        return [spec.fields[a] for a in out[:n]]

    def unknown_field_text(self, spec: ObjectSpec, raw: str) -> str:
        close = self.suggest_fields(spec, raw, 3)
        if self.resolver is not None:   # the spelling help: its did-you-means first, then today's list, 3 in all
            first = [spec.fields[a] for a in self._close_field_names(spec, raw)]
            close = list(dict.fromkeys(first + close))[:3]
        parts = ", ".join(f"{f.api} ({f.type_text()}: {_cut(f.meaning, 60)})" for f in close) or "none"
        words = " ".join(tokenize(raw)) or raw
        text = (f"{spec.api} has no field '{raw}'. Closest: {parts}. Call describe_fields(object='{spec.api}', "
                f"query='{words}') to see more.")
        elsewhere = [o.api for o in self.objects.values() if o.has_table and o.api != spec.api and
                     any(a.lower() == raw.lower() for a in o.fields)][:3]
        if elsewhere:
            text += f" '{raw}' is a field of {', '.join(elsewhere)}; check the object."
        return text

    def lookup_fields(self, spec: ObjectSpec) -> list[FieldSpec]:
        return [f for f in spec.fields.values() if f.kind == "reference" and f.sensitivity != "blocked"]

    def resolve_path(self, spec: ObjectSpec, path: Any) -> PathRef:
        """A field or lookup path (``Recruiter__c.Name``, ``Program_Version__c.Program__c``). Raises E2/E3*."""
        raw = str(path or "").strip()
        if not raw:
            raise RecordError(f"field is missing. Call describe_fields(object='{spec.api}') to see the fields.", "E2")
        parts = [p.strip() for p in raw.split(".") if p.strip()]
        notes: list[str] = []
        if len(parts) > 1 and parts[0].lower() == spec.api.lower() and parts[1:] and \
                not any(a.lower() == parts[0].lower() for a in spec.fields):
            parts = parts[1:]
        if len(parts) - 1 > 3:
            raise RecordError(f"path '{raw}' has {len(parts) - 1} lookups; at most 3 are allowed.", "E3b")
        aliases = {"owner": "OwnerId", "recordtype": "RecordTypeId", "createdby": "CreatedById",
                   "lastmodifiedby": "LastModifiedById"}
        hops: list[Hop] = []
        cur = spec
        shown: list[str] = []
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            name = part
            if not last:
                if part.lower().endswith("__r"):
                    name = part[:-3] + "__c"
                    notes.append(f"path alias {part} read as {name}")
                elif part.lower() in aliases:
                    name = aliases[part.lower()]
                    notes.append(f"path alias {part} read as {name}")
            if not last:
                try:
                    f = self.field(cur, name)
                except RecordError as exc:
                    if exc.code != "E2":
                        raise  # E2b (documented, not synced) and E2c (sensitive) keep their own text
                    child = self.objects.get(name) or self.objects.get(self._lower.get(name.lower(), ""), None)
                    if child is not None and any(c.child == child.api for c in cur.children):
                        via = next(c.via for c in cur.children if c.child == child.api)
                        raise RecordError(
                            f"{child.api} is a child of {cur.api}, not a lookup on it. To count {child.api} per "
                            f"{cur.api}, use object '{child.api}' with group_by '{via}'; to keep {cur.api} records "
                            f"that have one, use related=[{{\"child_object\":\"{child.api}\",\"exists\":true}}].", "E3c")
                    raise RecordError(self._not_lookup_text(cur, part, raw), "E3")
                if f.kind != "reference":
                    raise RecordError(self._not_lookup_text(cur, part, raw), "E3")
                if f.sensitivity == "blocked":
                    raise RecordError(blocked_text(cur.api, f.api), "E2c")
                if f.api == "OwnerId":
                    rest = parts[i + 1:]
                    if len(rest) == 1 and rest[0].lower() == "name":
                        hops.append(Hop(cur.api, f, "User|Group"))
                        shown.append(f.api)
                        name_field = FieldSpec(api="Name", label="Name", kb_type=None, db_type="VARCHAR", kind="name",
                                               meaning="owner name (user or queue)", system=True, has_data=True)
                        return PathRef(".".join(shown + ["Name"]), hops, "User|Group", name_field, notes)
                    target = "User"
                else:
                    queryable = [t for t in f.ref_targets if t in self.objects and self.objects[t].has_table]
                    if len(queryable) != 1:
                        if len(queryable) > 1 or len(f.ref_targets) > 1:
                            raise RecordError(f"{cur.api}.{f.api} can point to several objects ({', '.join(f.ref_targets)}); "
                                              "use it with an Id value, or OwnerId.Name for owners.", "E3d")
                        raise RecordError(f"{cur.api}.{f.api} points to {', '.join(f.ref_targets) or 'an unknown object'}, "
                                          "which the record tools cannot read.", "E3")
                    target = queryable[0]
                hops.append(Hop(cur.api, f, target))
                shown.append(f.api)
                cur = self.objects[target]
            else:
                f = self.field(cur, name)
                shown.append(f.api)
                return PathRef(".".join(shown), hops, cur.api, f, notes)
        raise RecordError(f"path '{raw}' could not be read.", "E3")

    def _not_lookup_text(self, spec: ObjectSpec, hop: str, path: str) -> str:
        lookups = [f for f in self.lookup_fields(spec) if f.has_data][:12] or self.lookup_fields(spec)[:12]
        listed = ", ".join(f"{f.api}→{'/'.join(f.ref_targets) or '?'}" for f in lookups) or "none"
        example = f"{lookups[0].api}.Name" if lookups else "Recruiter__c.Name"
        return (f"'{hop}' in path '{path}' is not a lookup field of {spec.api}. Lookup fields of {spec.api}: {listed}. "
                f"Write paths as LookupField.Field, e.g. {example}.")

    # ================================================================== lookup roles (RECORDS_SPEC §21.3)
    def target_of(self, f: FieldSpec) -> str | None:
        """The single queryable target of a lookup ("User" for OwnerId); None when there are several or none."""
        if f.kind != "reference":
            return None
        if f.api == "OwnerId":
            u = self.objects.get("User")
            return "User" if u is not None and u.has_table else None
        if len(f.ref_targets) != 1:
            return None
        t = f.ref_targets[0]
        return t if t in self.objects and self.objects[t].has_table else None

    def same_target_lookups(self, spec: ObjectSpec, f: FieldSpec) -> list[FieldSpec]:
        """The other non-blocked lookups of ``spec`` to the same target (undocumented ones included; system lookups
        only when ``f`` is one), by filled descending, then API name."""
        t = self.target_of(f)
        if t is None:
            return []
        out = [g for g in spec.fields.values() if g is not f and g.kind == "reference" and g.sensitivity != "blocked"
               and g.api != "RecordTypeId" and (f.api in SYSTEM_LOOKUPS or g.api not in SYSTEM_LOOKUPS)
               and self.target_of(g) == t]
        out.sort(key=lambda g: (-g.filled, g.api))
        return out

    def lookup_groups(self, spec: ObjectSpec) -> list[tuple[str, list[FieldSpec | str]]]:
        """Targets with 2 or more lookups of ``spec`` (synced non-system lookups with data, and documented lookups
        that are not synced, given as str): synced first (filled descending), not synced last; at most 6 groups,
        the most filled first."""
        groups: dict[str, list[Any]] = {}
        for g in spec.fields.values():
            if g.kind != "reference" or g.sensitivity == "blocked" or g.api in SYSTEM_LOOKUPS or \
                    g.api == "RecordTypeId" or g.filled <= 0:
                continue
            t = self.target_of(g)
            if t:
                groups.setdefault(t, []).append(g)
        for api, nf in spec.not_synced.items():
            t = self._not_synced_target(nf)
            if t:
                groups.setdefault(t, []).append(api)
        out = []
        for t, items in groups.items():
            if len(items) < 2:
                continue
            synced = sorted((g for g in items if isinstance(g, FieldSpec)), key=lambda g: (-g.filled, g.api))
            out.append((t, synced + sorted(g for g in items if isinstance(g, str))))
        out.sort(key=lambda kv: (-sum(g.filled for g in kv[1] if isinstance(g, FieldSpec)), kv[0]))
        return out[:6]

    def _not_synced_target(self, nf: dict) -> str | None:
        targets = nf.get("targets") or []
        if len(targets) != 1:
            return None
        t = str(targets[0])
        return t if t in self.objects and self.objects[t].has_table else None

    def not_synced_lookups(self, spec: ObjectSpec, target: str) -> list[str]:
        """Documented lookups of ``spec`` to ``target`` that are not synced."""
        return sorted(api for api, nf in spec.not_synced.items() if self._not_synced_target(nf) == target)

    def person_lookups(self, spec: ObjectSpec, system: bool = False) -> dict[str, list[FieldSpec]]:
        """Person object -> lookups of ``spec`` to it (filled descending); system lookups only with ``system``."""
        out: dict[str, list[FieldSpec]] = {}
        for g in spec.fields.values():
            if g.kind != "reference" or g.sensitivity == "blocked" or g.api == "RecordTypeId":
                continue
            if g.api in SYSTEM_LOOKUPS and not system:
                continue
            t = self.target_of(g)
            if t in PERSON_OBJECTS:
                out.setdefault(t, []).append(g)
        for lst in out.values():
            lst.sort(key=lambda g: (-g.filled, g.api))
        return out

    @staticmethod
    def almost_empty(spec: ObjectSpec, f: FieldSpec) -> bool:
        """Filled on fewer than ALMOST_EMPTY of the object's rows, but not empty."""
        return 0 < f.filled < ALMOST_EMPTY * spec.rows

    def _object_words(self, *apis: str) -> set[str]:
        out: set[str] = set()
        for api in apis:
            o = self.objects.get(api)
            texts = [api, split_api_name(api)] + ([o.label, o.plural_label] if o is not None else [])
            for t in texts:
                for w in _words(split_api_name(t) if "_" in t else t):
                    out.add(w)
                    if w.endswith("s"):
                        out.add(w[:-1])
        return out

    def role_words(self, spec: ObjectSpec, f: FieldSpec) -> list[str]:
        """Words that say a lookup's role: the words of ``ref_role`` and the action words of ``from_target``
        (documented), the API-name words (undocumented, found from the Id prefix) or the system role. The label,
        API name and concept of a documented lookup are never used."""
        if f.api in SYSTEM_ROLES:
            return list(dict.fromkeys(_words(SYSTEM_ROLES[f.api][0])))
        target = self.target_of(f) or (f.ref_targets[0] if f.ref_targets else "")
        if f.ref_role == "undocumented" or not f.documented:
            drop = self._object_words(target) if target else set()
            return list(dict.fromkeys(w for w in _words(split_api_name(f.api)) if w not in drop))
        words = [w for part in f.ref_role.split("_") for w in _words(part)]
        drop = self._object_words(spec.api, *( [target] if target else []))
        for w in _words(f.from_target):
            if w in _ROLE_STOPWORDS or w in drop or (w.endswith("s") and w[:-1] in drop):
                continue
            words.append(w)
        return list(dict.fromkeys(words))

    def shared_role_words(self, spec: ObjectSpec, f: FieldSpec, other: ObjectSpec, g: FieldSpec) -> list[str]:
        """The words that the business roles (``ref_role``; the system role of a system lookup) of lookup ``f`` of
        ``spec`` and lookup ``g`` of ``other`` share and that are role words of :data:`ROLE_CUES` (a word that starts
        with one of its role stems), without the words of the objects involved (both objects and both targets):
        "owner" for OwnerId (record owner) and a Candidate Owner lookup, "support" for an interview support person and
        a mock support person, none for an interviewer and a session host, or a host and an attendee."""
        targets = [t for t in (self.target_of(f), self.target_of(g)) if t]
        drop = self._object_words(spec.api, other.api, *targets)
        stems = {st for cue in ROLE_CUES for col in (cue[1], cue[2]) for st in col}

        def words(x: FieldSpec) -> set[str]:
            if not x.ref_role or x.ref_role == "undocumented":
                return set()
            return {w for part in x.ref_role.split("_") for w in _words(part)
                    if len(w) >= 3 and w not in _ROLE_STOPWORDS and w not in drop and any(w.startswith(st) for st in stems)}

        a = words(f)
        return sorted(w for w in words(g) if w in a)

    @staticmethod
    def _question_tokens(question: str) -> list[str]:
        return [t for t in re.findall(r"[a-z0-9]+", normalize_value(question)) if len(t) >= 3]

    def value_words(self, question: str) -> frozenset[str]:
        """Question words that name a stored value, not a role: a one-word stored picklist value right before the
        label of the object storing it ("training sessions": 'Training' is a value of a session picklist, so
        "training" is a condition of the question, never the verb "train"). "did X host" keeps "host": no object
        label follows it."""
        return frozenset(self.value_conditions(question))

    def value_conditions(self, question: str) -> dict[str, frozenset[str]]:
        """:meth:`value_words` with the objects whose label (or plural label) follows each word."""
        q = str(question or "")
        hit = self._value_word_cache.get(q)
        if hit is not None:
            return hit
        toks = re.findall(r"[a-z0-9]+", normalize_value(q))
        out: dict[str, set[str]] = {}
        for i, tok in enumerate(toks[:-1]):
            rows = self.global_values.get(tok)
            if not rows or len(tok) < 3:
                continue
            for obj in dict.fromkeys(r[0] for r in rows):
                if self._label_follows(obj, toks, i + 1):
                    out.setdefault(tok, set()).add(obj)
        res = {k: frozenset(v) for k, v in out.items()}
        if len(self._value_word_cache) > 256:
            self._value_word_cache.clear()
        self._value_word_cache[q] = res
        return res

    def _label_follows(self, api: str, toks: list[str], j: int) -> bool:
        """True when the words from ``toks[j]`` on are the object's label or plural label (the last word may also
        drop or add a trailing "s")."""
        o = self.objects.get(api)
        if o is None:
            return False
        for label in (o.label, o.plural_label):
            words = re.findall(r"[a-z0-9]+", normalize_value(label))
            if not words or j + len(words) > len(toks):
                continue
            got = toks[j:j + len(words)]
            if got[:-1] != words[:-1]:
                continue
            a, b = got[-1], words[-1]
            if a == b or a.rstrip("s") == b.rstrip("s"):
                return True
        return False

    def _cue_tokens(self, question: str) -> list[str]:
        """The question tokens that can be role words: every token of 3 or more characters except stored values that
        modify an object word (:meth:`value_words`)."""
        skip = self.value_words(question)
        return [t for t in self._question_tokens(question) if t not in skip]

    def _fits_words(self, question: str, rw: list[str], kind: str) -> str | None:
        """The first question word whose ROLE_CUES cue has, for ``kind``, a role stem that one of the role words
        ``rw`` fits."""
        if kind not in ("employee", "candidate") or not question or not rw:
            return None
        col = 1 if kind == "employee" else 2
        for tok in self._cue_tokens(question):
            for cue in ROLE_CUES:
                if not any(tok.startswith(st) for st in cue[0]):
                    continue
                for r in cue[col]:
                    if any(w.startswith(r) and len(w) - len(r) <= 3 for w in rw):
                        return tok
        return None

    def role_fits(self, question: str, spec: ObjectSpec, f: FieldSpec, kind: str) -> str | None:
        """The first question word whose ROLE_CUES cue has, for ``kind``, a role stem that a role word of ``f``
        fits; None for an empty or almost empty lookup, for kind "other" and when nothing fits. Stored values that
        modify an object word ("training sessions") are no role words (:meth:`value_words`)."""
        if kind not in ("employee", "candidate") or not question or f.filled <= 0 or self.almost_empty(spec, f):
            return None
        return self._fits_words(question, self.role_words(spec, f), kind)

    def sparse_fits(self, question: str, spec: ObjectSpec, f: FieldSpec, kind: str) -> str | None:
        """The question word the role of ``f`` would fit (as :meth:`role_fits`) when ``f`` is empty or almost empty,
        else None. :meth:`role_fits` never marks such a lookup; this tells the answer checks that the lookup of the
        asked role exists but holds almost no data (a 0 through it is no evidence about the person)."""
        if kind not in ("employee", "candidate") or not question:
            return None
        if not (f.filled <= 0 or self.almost_empty(spec, f)):
            return None
        return self._fits_words(question, self.role_words(spec, f), kind)

    def question_cues(self, question: str, kind: str) -> list[str]:
        """Question words that match a cue with role stems for ``kind``, in question order, without repeats."""
        if kind not in ("employee", "candidate") or not question:
            return []
        col = 1 if kind == "employee" else 2
        out: list[str] = []
        for tok in self._cue_tokens(question):
            if tok in out:
                continue
            if any(cue[col] and any(tok.startswith(st) for st in cue[0]) for cue in ROLE_CUES):
                out.append(tok)
        return out

    def not_synced_fits(self, question: str, spec: ObjectSpec, kinds: dict[str, str] | None = None) -> list[dict]:
        """Documented lookups of ``spec`` to a person object that are NOT synced and whose role fits a question word
        for the person kind: ``[{"field", "role", "target", "target_label", "fits"}]``. ``kinds`` (person object ->
        kind of the person the question is about) limits the targets and gives their kind; without it every person
        object counts, Employee and User as employee, Account and Lead as candidate. Such a lookup is what the question
        asks about, but the warehouse does not record it (any count through another lookup is only a proxy)."""
        out: list[dict] = []
        if not question:
            return out
        for api, nf in sorted(spec.not_synced.items()):
            t = self._not_synced_target(nf)
            if t is None or t not in PERSON_OBJECTS or (kinds is not None and t not in kinds):
                continue
            kind = kinds[t] if kinds is not None else "employee" if t in ("Recruiter__c", "User") else "candidate"
            role = str(nf.get("role") or "")
            rw = list(dict.fromkeys(w for part in role.split("_") for w in _words(part)))
            fit = self._fits_words(question, rw, kind)
            # a synced lookup of the object to the same person object whose role fits the same word answers it
            synced = (g for g in spec.fields.values() if g.kind == "reference" and self.target_of(g) == t
                      and g.sensitivity != "blocked" and g.api not in SYSTEM_LOOKUPS)
            if fit and not any(self.role_fits(question, spec, g, kind) == fit for g in synced):
                out.append({"field": api, "role": role.replace("_", " ").strip() or "lookup", "target": t,
                            "target_label": self.label_of(t), "fits": fit})
        return out

    def is_person_account(self, detail: Any) -> bool:
        """True when ``detail`` names the Person Account record type."""
        d = normalize_value(detail)
        if not d:
            return False
        acc = self.objects.get("Account")
        names = {"person account", "personaccount"}
        if acc is not None:
            names |= {normalize_value(r.get("name")) for r in acc.record_types if r.get("developer_name") == "PersonAccount"}
        return d in names

    def person_kind(self, obj: str, detail: Any = None) -> str:
        """"employee" (Employee, User), "candidate" (Lead, or a Person Account), else "other"."""
        if obj in ("Recruiter__c", "User"):
            return "employee"
        if obj == "Lead":
            return "candidate"
        if obj == "Account" and self.is_person_account(detail):
            return "candidate"
        return "other"

    def profile_fields(self, spec: ObjectSpec) -> list[FieldSpec]:
        """The fields of a person's own record that ``about_people`` may show (RECORDS_SPEC §22.4), cached: every
        candidate in rank order (at most :data:`PROFILE_FIELDS_MAX`, a safety cap). The list is per object; the profile
        of each record keeps the first ones that are filled on that record (``tools.people.PROFILE_MAX_FILLED``).

        A field is kept when it holds data; is not sensitive, legacy, deprecated or a long text; is no system lookup,
        record type, Id, Name or identity-link column; does not look like a credential or contact field; has none of
        :data:`PROFILE_DROP_ROLES`; and either is documented with one of :data:`PROFILE_KEEP_ROLES` (a checkbox only
        with ``business_status``), or is a documented lookup to a person object (a candidate's assigned recruiter), or
        is one of :data:`STANDARD_PROFILE_FIELDS`; and is not one of :data:`PROFILE_SKIP_FIELDS`. Order: standard
        fields, lifecycle and status fields, lookups, categorical fields, dates; within each, the most filled
        first."""
        if spec.api in self._profile:
            return self._profile[spec.api]
        ident = {(lk.src, lk.col) for lk in self.links} | {(lk.dst, lk.dst_col) for lk in self.links if lk.dst_col != "Id"}
        std = STANDARD_PROFILE_FIELDS.get(spec.api, ())
        skip = PROFILE_SKIP_FIELDS.get(spec.api, frozenset())
        ranked: list[tuple[tuple, FieldSpec]] = []
        for f in spec.fields.values():
            if not f.has_data or f.sensitivity or f.legacy or f.deprecated or f.kind == "longtext":
                continue
            if f.api in SYSTEM_LOOKUPS or f.api in ("Id", "Name", "RecordTypeId") or (spec.api, f.api) in ident:
                continue
            if BLOCKED_COLUMN_RE.search(f.api) or CONTACT_COLUMN_RE.search(f.api) or f.api in skip:
                continue
            roles = set(f.roles)
            if roles & PROFILE_DROP_ROLES:
                continue
            if f.api in std:
                rank = (0, std.index(f.api))
            elif not f.documented:
                continue
            elif f.kind == "reference" and (self.target_of(f) in PERSON_OBJECTS or roles & PROFILE_KEEP_ROLES):
                rank = (2, 0)
            elif roles & PROFILE_KEEP_ROLES:
                if f.kind == "boolean" and "business_status" not in roles:
                    continue
                if roles & {"lifecycle_state", "business_status"}:
                    rank = (1, 0)
                elif f.is_date:
                    rank = (4, 0)
                else:
                    rank = (3, 0)
            else:
                continue
            ranked.append(((rank, -f.filled, f.api), f))
        ranked.sort(key=lambda x: x[0])
        out = [f for _k, f in ranked][:PROFILE_FIELDS_MAX]
        self._profile[spec.api] = out
        return out

    def contact_fields(self, spec: ObjectSpec) -> list[FieldSpec]:
        """The contact fields of a person's own record that ``about_people`` adds to the profile when the question asks
        for contact details (phase 1, :mod:`sf_assistant.engine.contact`), cached: the fields with ``sensitivity ==
        "contact"`` that hold data and are no legacy, deprecated or long-text field, no identity-link column and no
        system field; documented ones first, then the most filled. :meth:`profile_fields` never holds them."""
        key = "contact:" + spec.api
        if key in self._profile:
            return self._profile[key]
        ident = {(lk.src, lk.col) for lk in self.links} | {(lk.dst, lk.dst_col) for lk in self.links if lk.dst_col != "Id"}
        out = [f for f in spec.fields.values() if f.sensitivity == "contact" and f.has_data and not f.legacy
               and not f.deprecated and f.kind != "longtext" and not f.system and (spec.api, f.api) not in ident]
        out.sort(key=lambda f: (0 if f.documented else 1, -f.filled, f.api))
        self._profile[key] = out
        return out

    def identity_links(self, obj: str) -> list[IdentityLink]:
        return [lk for lk in self.links if lk.src == obj]

    def linked_from(self, obj: str) -> list[IdentityLink]:
        return [lk for lk in self.links if lk.dst == obj]

    def label_of(self, api: str) -> str:
        o = self.objects.get(api)
        return o.label if o is not None else api

    def target_text(self, api: str) -> str:
        """'Employee (Recruiter__c)', or just 'User' when the label is the API name."""
        if "|" in api:
            return " or ".join(self.label_of(a) for a in api.split("|"))
        lab = self.label_of(api)
        return api if lab == api else f"{lab} ({api})"

    def values_of(self, obj: str, field_api: str) -> ValueEntry | None:
        return self.values.get((obj, field_api))

    def other_fields_with(self, obj: str, norm: str, exclude: str) -> list[tuple[str, str, int]]:
        """(field, stored value, records) of other indexed fields of ``obj`` storing ``norm``."""
        return [t for t in self._obj_values.get(obj, {}).get(norm, []) if t[0] != exclude]

    def other_fields_containing(self, obj: str, norm: str, exclude: str, n: int = 3) -> list[tuple[str, str, int]]:
        """Like :meth:`other_fields_with`, else values whose normalised words contain ``norm``'s words."""
        exact = self.other_fields_with(obj, norm, exclude)
        if exact or not norm:
            return exact[:n]
        out = []
        pat = re.compile(r"(^| )" + re.escape(norm) + r"( |$)")
        for key, rows in self._obj_values.get(obj, {}).items():
            if pat.search(key):
                out.extend(t for t in rows if t[0] != exclude)
        out.sort(key=lambda t: -t[2])
        return out[:n]

    def value_matches(self, question: str, top_objects: list[str] | None = None, n: int = 5) -> list[tuple[str, str, str, int]]:
        """Picklist values whose words all appear in ``question`` (see RECORDS_SPEC §10.0)."""
        qtoks = set(_value_tokens(question, keep_short=True))
        top = top_objects or []
        found: list[tuple[int, int, int, tuple[str, str, str, int]]] = []
        for key, rows in self.global_values.items():
            toks = _value_tokens(key)
            if not toks or not set(toks) <= qtoks:
                continue
            for obj, fld, val, cnt in rows:
                spec = self.objects.get(obj)
                if spec is None:
                    continue
                f = spec.fields.get(fld)
                if f is None or f.sensitivity:
                    continue
                found.append((0 if obj in top else 1, -len(toks), -cnt, (obj, fld, val, cnt)))
        found.sort(key=lambda t: (t[0], t[1], t[2], t[3][0], t[3][1]))
        out, seen = [], set()
        for *_k, row in found:
            key = (row[0], row[1], row[2])
            if key not in seen:
                seen.add(key)
                out.append(row)
            if len(out) >= n:
                break
        return out


def _chunks(parts: list[str], max_chars: int) -> list[list[str]]:
    """Split SELECT items into batches whose joined text stays under ``max_chars``."""
    out: list[list[str]] = []
    batch: list[str] = []
    size = 0
    for part in parts:
        if batch and size + len(part) > max_chars:
            out.append(batch)
            batch, size = [], 0
        batch.append(part)
        size += len(part) + 2
    if batch:
        out.append(batch)
    return out


def normalize_value(s: Any) -> str:
    """Same normalisation as :func:`okrec.values.normalize` (kept here to avoid an import cycle)."""
    import unicodedata

    t = unicodedata.normalize("NFKD", str(s if s is not None else "")).casefold()
    t = re.sub(r"[-_/–—]", " ", t)
    t = re.sub(r"[.,'\"()]", "", t)
    return " ".join(t.split())


def entity_key(text: Any) -> str:
    """The entity index key of a stored value or of question words: the a-z0-9 words of :func:`normalize_value`
    after a possessive 's is removed, joined by one space ("Q&A" -> "q a", "Level 2-3 Drill" -> "level 2 3 drill",
    "PILOT'S" -> "pilot", "Désk" -> "desk"). Stored values and questions are keyed the same way.

    The words of ``re.findall(r"[a-z0-9]+", normalize_value(...))``, computed in one pass (it runs on every stored
    value at open): the dashes, underscores and slashes :func:`normalize_value` reads as spaces and the punctuation it
    removes are translated at once; a non-ASCII text is decomposed and its accents (combining marks) dropped, so an
    accented name and the same name typed without accents share a key."""
    t = str(text if text is not None else "")
    if "'" in t or "\u2019" in t:
        t = _POSSESSIVE_RE.sub("", t)
    if not t.isascii():
        t = "".join(ch for ch in unicodedata.normalize("NFKD", t) if not unicodedata.combining(ch))
    return " ".join(_KEY_WORD_RE.findall(t.casefold().translate(_KEY_TABLE)))


def _entity_value_key(s: str, source: str, digits: bool, dropped: dict[str, int]) -> str:
    """The entity key of one trimmed stored value of a field read as ``source`` (Catalog._entity_plan), or "" when
    the value names nothing a question would name (the reason counted in ``dropped``): empty, too long, Id-like, an
    e-mail address, link or path, only digits and punctuation or a date, without a letter, with an embedded secret
    (very high: never indexed), not of the auto-number shape (identifier text), or a date or amount shape (text). A
    digits-only value of a code field (``digits``) is a code, keyed by its digits without leading zeros."""
    voc = _vocabulary
    name_like = source in _ENTITY_LONG_SOURCES
    max_chars = int(ENTITY_NAME_MAX_CHARS if name_like else voc.MAX_VALUE_CHARS)
    max_words = int(ENTITY_NAME_MAX_WORDS if name_like else voc.MAX_VALUE_WORDS)
    why = ""
    if not s:
        why = "empty"
    elif len(s) > max_chars or len(s.split()) > max_words:
        why = "long"
    elif digits and _DIGITS_RE.match(s):
        return s.lstrip("0") or "0"
    elif source == "number":
        why = "not_code"          # a ...Number text field gives digits-only codes only
    elif _ID_RE.match(s) and any(ch.isdigit() for ch in s):
        why = "id_like"
    elif _SECRET_WORD_RE.search(s.lower()) and hide_embedded_secrets(s) != s:
        why = "secret"
    elif source in ("code", "code_text") and AUTO_NUMBER_RE.match(s):
        return entity_key(s)      # the usual code ("APP-0012"): letters and digits only, so every other rule holds
    elif voc._MAIL_URL_RE.search(s) or (("/" in s or "\\" in s) and any(_path_like(t) for t in s.split())):
        why = "mail_url_path"
    elif voc._NUMBERISH_RE.match(s) or _DATE_VALUE_RE.match(s):
        why = "number_date"
    elif not _LETTER_RE.search(s):
        why = "no_letter"
    elif source == "code_text":
        why = "not_code"
    else:
        key = entity_key(s)
        if not key:
            why = "no_letter"
        elif key.count(" ") + 1 > int(ENTITY_NAME_MAX_WORDS):
            why = "long"
        elif source == "text" and _date_or_amount(key):
            why = "date_amount"
        else:
            return key
    dropped[why] = dropped.get(why, 0) + 1
    return ""


def _path_like(token: str) -> bool:
    """A file path token: it starts with ``/``, ``~/`` or ``./`` and holds another ``/`` ("/home/a/b"), or holds a
    backslash. Names with a slash ("Billing/Payment", "B.Tech/B.E", "Sales / Marketing") are no paths."""
    return "\\" in token or (token.startswith(("/", "~/", "./")) and "/" in token.lstrip("~.")[1:])


def _date_or_amount(key: str) -> bool:
    """True for a key with a digit whose every word holds a digit, is a month or weekday name or abbreviation, or has
    at most 3 letters ("jan 2020", "q1 2020", "120k", "10 lpa"): a date or an amount written as text (a filter of a
    question, never an entity)."""
    words = key.split()
    return any(ch.isdigit() for ch in key) and all(
        any(ch.isdigit() for ch in w) or w in _CALENDAR_WORDS or len(w) <= 3 for w in words)


def _holds_people(kept: dict[str, tuple[str, int]], people: frozenset[str]) -> bool:
    """True when at least ENTITY_PERSON_SHARE of a field's values (the ENTITY_PERSON_SAMPLE most frequent ones) hold
    a stored person's name (a 2- or 3-word n-gram of their key is one of ``people``): a field of people's names, which
    the people path reads (a candidate's display name, an applicant's name, a referrer)."""
    limit = int(ENTITY_PERSON_SAMPLE)
    sample = list(kept.items()) if len(kept) <= limit else \
        heapq.nsmallest(limit, kept.items(), key=lambda kv: (-kv[1][1], kv[0]))
    if not sample:
        return False
    hits = 0
    for _value, (key, _n) in sample:
        ws = key.split()
        if any(" ".join(ws[i:i + n]) in people for n in (2, 3) for i in range(len(ws) - n + 1)):
            hits += 1
    return hits >= float(ENTITY_PERSON_SHARE) * len(sample)


def _add_reason(info: dict[str, Any], reason: str) -> None:
    """Add a cut reason of the entity index once (the start-up line names them)."""
    if reason not in info["cut_reasons"]:
        info["cut_reasons"].append(reason)


def _value_tokens(text: str, keep_short: bool = False) -> list[str]:
    out = []
    for tok in re.findall(r"[a-z0-9]+", normalize_value(text)):
        if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss"):
            tok = tok[:-1]
        if keep_short or len(tok) >= 3:
            out.append(tok)
    return out


def hide_embedded_secrets(text: str) -> str:
    """``text`` with the value of every :data:`SECRET_PARAM_RE` parameter and every :data:`LABELLED_SECRET_RE` value
    replaced by ``[hidden]`` (the rest stays readable)."""
    low = text.lower()
    if not any(w in low for w in _SECRET_WORDS):
        return text
    if "=" in text:
        text = SECRET_PARAM_RE.sub(r"\1[hidden]", text)
    return LABELLED_SECRET_RE.sub(lambda m: (m.group(1) or m.group(3)) + "[hidden]", text)


def blocked_text(obj: str, fld: str) -> str:
    scoped = FIELD_SENSITIVITY.get((obj, fld), ("", ""))
    what = scoped[1] if scoped[0] == "blocked" else "credential, passcode or identity document"
    return (f"{obj}.{fld} is a sensitive field ({what}); it cannot be read, "
            "filtered, grouped or shown. Tell the user the field exists but cannot be shown.")


def contact_text(obj: str, fld: str) -> str:
    """The refusal of a contact field when the question does not ask for contact details (phase 1): it never says
    that the question asked."""
    return (f"{obj}.{fld} holds contact details (e-mail, phone, address and the like). The question does not ask for "
            "contact details, so they are not shown, listed, grouped or used here; identify people by name. Contact "
            "details are shown only when the user's question asks for them.")
