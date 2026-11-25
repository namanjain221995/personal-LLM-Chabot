"""Draft checks of the record agent (moved verbatim from okrec/session.py; RECORDS_SPEC §21.10, §22).

Lookup checks and nudges (:func:`lookup_checks`, :func:`lookup_nudges`, :func:`unfit_lead_zero_nudges`), made-up
overlaps (:func:`overlap_nudges`, :func:`drop_overlap_clauses`), breakdown, date, mirror, unsynced, people,
shared-name, no-data and noun nudges, the not-found answers (:func:`not_found_answer`, :func:`not_found_reply`), the
FIELD CHECK for the merge (:func:`field_checks`, :func:`field_check_lines`), the lookup footer
(:func:`with_lookup_footer`) and the Source line (:func:`with_source_line`). Code reads only structured keys of the
tool results, never note or error texts.

Group B (docs/people.md), new code with no notebook function behind it: the relation wording of the about_people
person lookups (:func:`relation_wording_nudges`, item 10a), the direction of a self-lookup the question asks about
(:func:`direction_nudges`, item 10b), and the status lines under a final answer whose question's verb can mean
finished records (:func:`status_lines`, :func:`with_status_line`, item 11).

Evaluation fixes of 2026-10-22: :func:`answer_text` (fix 2: an answer with nothing but its Source line and the lines
code writes around it is no answer), the knowledge wording of a lookup (label, ``from_source``, ``from_target``) as its
relation words in :func:`direction_nudges`, and :func:`relation_lookup_nudges` (fix 5: a word of the question that is
in the wording of one person lookup while the answer used another, with the direction the question asks for).

Step-3 fix T5 (counters for the next night A/B of the code rows): :func:`nudge_kinds`, the prompt ids of a draft-check
message, read from its text for the log; the checks above do not know about it.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal
from typing import Any

from sf_assistant.agent.checks import (
    _DATE_TIME_RE, _LIST_ITEM_RE, _NOT_RECORD_COUNT_RE, _body_text, _count_numbers, _error_code, _is_no_records,
    _is_source_line, _label_only, _norm_number, _not_available, numbers_in)
from sf_assistant.agent.record_prompts import (
    RECORDS_BOTH_OBJECTS_MESSAGE, RECORDS_BREAKDOWN_MESSAGE, RECORDS_DATE_COVERAGE_MESSAGE, RECORDS_ELSEWHERE_MESSAGE,
    RECORDS_LEAD_ZERO_NOTE, RECORDS_LINKED_EMPLOYEE_MESSAGE, RECORDS_LINKED_LOOKUP_MESSAGE, RECORDS_MIRROR_MESSAGE,
    RECORDS_NOT_EXACT_NAME_MESSAGE, RECORDS_NOT_SYNCED_MESSAGE, RECORDS_NO_DATA_MESSAGE,
    RECORDS_OTHER_LOOKUP_FIRM_MESSAGE, RECORDS_OTHER_LOOKUP_FITS_MESSAGE, RECORDS_OTHER_LOOKUP_MESSAGE,
    RECORDS_OVERLAP_MESSAGE, RECORDS_SAME_NAME_EMPLOYEE_MESSAGE, RECORDS_SEVERAL_PEOPLE_MESSAGE,
    RECORDS_SHARED_NAME_MESSAGE, RECORDS_SPARSE_ZERO_MESSAGE, RECORDS_UNFIT_ZERO_MESSAGE)
from sf_assistant.agent.record_prompts import (   # Group B (docs/people.md): relation, direction, status line
    RECORDS_DIRECTION_COUNTED_NOTE, RECORDS_DIRECTION_MESSAGE, RECORDS_DIRECTION_SOURCE_MESSAGE,
    RECORDS_DIRECTION_UNCLEAR_MESSAGE, RECORDS_RELATION_WORD_MESSAGE, RECORDS_STATUS_LINE)
from sf_assistant.agent.record_prompts import (   # Group B review fixes (docs/people.md): items 8 and 9
    RECORDS_CHILD_COPY_HELD_NOTE, RECORDS_CHILD_COPY_MESSAGE, RECORDS_PATHS_MESSAGE)
from sf_assistant.agent.record_prompts import (   # evaluation fixes of 2026-10-22: 2 (code lines) and 5 (relation)
    RECORDS_OTHER_PEOPLE_LINE, RECORDS_RELATION_LOOKUP_COUNTED_NOTE, RECORDS_RELATION_LOOKUP_MESSAGE,
    RECORDS_RELATION_LOOKUP_SOURCE_MESSAGE, RECORDS_RELATION_LOOKUP_UNCLEAR_MESSAGE)
from sf_assistant.agent.record_prompts import (   # texts of the lines code writes (prompts/<id>.md, one-pass fill)
    RECORDS_DATE_COVERAGE_MAIN, RECORDS_DATE_COVERAGE_MAIN_UNKNOWN, RECORDS_FIELD_CHECK_LOOKUP,
    RECORDS_FIELD_CHECK_UNSYNCED, RECORDS_FOOTER_LOOKUP_USED, RECORDS_FOOTER_NOT_SYNCED, RECORDS_FOOTER_SAME_PERSON,
    RECORDS_FOOTER_SAME_PERSON_FOR, RECORDS_FOOTER_WORD_FITS, RECORDS_NO_DATA_LINE, RECORDS_NO_DATA_LINE_SEVERAL,
    RECORDS_OTHER_LOOKUP_FIRM_SPARSE_NOTE, RECORDS_OTHER_LOOKUP_FITS_NOTE, RECORDS_SOURCE_LINE,
    RECORDS_UNFIT_ZERO_SPARSE_NOTE)
from sf_assistant.engine.types import PERSON_TOOLS, QueryEvidence, RecordsStatus
from sf_assistant.prompts import fill as _fill
from sf_assistant.prompts import ids as _prompt_ids
from sf_assistant.prompts import text as _prompt_text

# ------------------------------------------------------------------------------ lookup checks

#: Field API names a knowledge answer names (FIELD CHECK): custom fields plus the system user lookups.
_CUSTOM_FIELD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*__c\b")
_SYSTEM_LOOKUPS = ("CreatedById", "LastModifiedById", "OwnerId")
_MAX_NUDGE_OTHERS = 4
_MAX_ELSEWHERE = 5
_MAX_FIELD_CHECKS = 3
_MAX_FOOTER_OTHERS = 4
#: Query tools whose results count records: a later call of one of them "used" a lookup or queried an object
#: (listing a field's values does not). find_references counts references too (the elsewhere case).
_COUNTING_TOOLS = frozenset({"count_records", "aggregate_records", "group_records", "trend_records", "compare_records",
                             "percentage_of_records", "list_records"})
_REFERENCE_TOOLS = _COUNTING_TOOLS | PERSON_TOOLS
_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+|\n+")


def lookup_checks(evidence: list[QueryEvidence]) -> list[dict]:
    """Every ``lookup_check`` entry of the successful data results, in call order, with ``query_id`` and
    ``tool`` added (RECORDS_SPEC §21.2 C4.1)."""
    out: list[dict] = []
    for e in evidence:
        if not (e.ok and e.kind == "data" and isinstance(e.model_result, dict)):
            continue
        for lc in e.model_result.get("lookup_check") or []:
            if isinstance(lc, dict):
                out.append({**lc, "query_id": e.query_id, "tool": e.tool})
    return out


def _linked_others(lc: dict) -> list[dict]:
    """The ``linked`` others of a lookup check: lookups that hold the person through a record linked to the matched one
    (an employee's own Account, a login user)."""
    return [o for o in lc.get("others") or [] if isinstance(o, dict) and o.get("group") == "linked"
            and o.get("same_person", True)]


def _person_checks(evidence: list[QueryEvidence]) -> list[dict]:
    """The lookup checks of person filters: a lookup to a niche, a department or a company (``person_kind``
    "other") gets no nudge and no footer, unless the value matched a record linked to a person who has records on
    the object (``linked`` others above 0: an employee's own Account matched through a candidate lookup)."""
    return [lc for lc in lookup_checks(evidence) if lc.get("person_kind") != "other"
            or any(_count(o.get("records")) > 0 for o in _linked_others(lc))]


def _filter_names(lc: dict) -> tuple[str, ...]:
    """The quoted values of a lookup check's filter text ("Interviewer__c.Name = 'X'" -> ('X',)): the person or
    people it is about. Empty when the filter quotes none (an Id filter)."""
    return tuple(v.replace("''", "'") for v in re.findall(r"'((?:[^']|'')*)'", str(lc.get("filter") or "")))


def _person_key(lc: dict) -> Any:
    """Who a lookup check is about: its filter's quoted names, else the filter text."""
    return _filter_names(lc) or str(lc.get("filter") or "")


def _fields_used(e: QueryEvidence) -> set[tuple[str, str]]:
    """``QueryEvidence.fields_used`` as a set of (object, field) pairs (empty for older evidence)."""
    return {(str(p[0]), str(p[1])) for p in getattr(e, "fields_used", None) or []
            if isinstance(p, (list, tuple)) and len(p) == 2}


def _after(evidence: list[QueryEvidence], after_qid: str | None) -> list[QueryEvidence]:
    """The evidence entries logged after the call ``after_qid`` (all of them when it is not found)."""
    for i, e in enumerate(evidence):
        if e.query_id == after_qid:
            return evidence[i + 1:]
    return list(evidence)


def _used_later(evidence: list[QueryEvidence], after_qid: str | None, obj: str | None, field_api: str | None) -> bool:
    """True when a successful counting query after ``after_qid`` used ``(obj, field_api)`` (``fields_used``);
    get_field_values on that field does not count."""
    pair = (str(obj), str(field_api))
    return any(e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS and pair in _fields_used(e)
               for e in _after(evidence, after_qid))


def _queried_later(evidence: list[QueryEvidence], after_qid: str | None, objects: set[str]) -> bool:
    """True when a successful counting query (or find_references) after ``after_qid`` ran on one of ``objects``."""
    return any(e.ok and e.kind == "data" and e.tool in _REFERENCE_TOOLS and e.object in objects
               for e in _after(evidence, after_qid))


def _api_re(api: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(str(api)) + r"(?![A-Za-z0-9_])")


def _names(text: str, api: str | None) -> bool:
    """True when ``text`` names the API name ``api`` as a whole word (``Interview_Recruiter__c`` does not
    name ``Recruiter__c``)."""
    if not api:
        return False
    return _api_re(api).search(text or "") is not None


def _object_names(checks: list[dict]) -> set[str]:
    """The object API names of lookup checks (base objects and targets): a lookup field with one of these names
    (``Recruiter__c`` is also the Employee object) is named only in a clear lookup context."""
    out: set[str] = set()
    for lc in checks:
        for key in ("object", "target"):
            if lc.get(key):
                out.add(str(lc[key]))
        for o in lc.get("others") or []:
            if isinstance(o, dict) and o.get("target"):
                out.add(str(o["target"]))
    return out


def _names_field(text: str, field_api: str | None, role: str | None, objects: set[str]) -> bool:
    """True when ``text`` names the lookup ``field_api``. A lookup whose API name is also an object's name
    (``Recruiter__c``) counts only as ``<Obj>.<Field>``, as ``<Field>.<x>`` (a path through it) or right next to
    its role word ("as recruiter (`Recruiter__c`)", "`Recruiter__c` (recruiter)"): "Employee (`Recruiter__c`)"
    names the object, not the lookup."""
    if not field_api:
        return False
    if field_api not in objects:
        return _names(text, field_api)
    text = text or ""
    words = [w for w in re.findall(r"[a-z]+", str(role or "").lower()) if w != "undocumented"]
    words += [w for w in re.findall(r"[a-z]+", re.sub(r"__c$", "", str(field_api)).replace("_", " ").lower())
              if w not in words]   # "as a recruiter (`Recruiter__c`)" names the lookup even when its role is undocumented
    for m in _api_re(field_api).finditer(text):
        before, after = text[max(0, m.start() - 40):m.start()], text[m.end():m.end() + 40]
        if re.search(r"[A-Za-z0-9_]\.$", before) or re.match(r"\.[A-Za-z]", after):
            return True
        if re.search(r"\b(?:lookup|field)\s*:?\W{0,4}$", before, re.IGNORECASE) or \
                re.match(r"\W{0,4}(?:lookup|field)\b", after, re.IGNORECASE):
            return True   # "**Lookup:** `Recruiter__c`", "the `Recruiter__c` lookup"
        for w in words:
            if re.search(r"\b" + re.escape(w) + r"\W{0,4}$", before, re.IGNORECASE) or \
                    re.match(r"\W{0,4}" + re.escape(w) + r"\b", after, re.IGNORECASE):
                return True
    return False


def _body(text: str) -> str:
    """``text`` without its "Source: ..." lines."""
    return "\n".join(ln for ln in (text or "").split("\n")
                     if not ln.strip().lstrip("*_> ").lower().startswith("source:"))


def _quotes(text: str, value: Any, field_api: str | None, role: str | None) -> bool:
    """True when one clause of ``text`` (Source line, dates and times left out) holds the count ``value`` together
    with the lookup's API name or its role words: a number alone (a year, a day, another group's count) is no
    quote of that lookup's count."""
    n = _num(value)
    if n is None:
        return False
    role_l = str(role or "").lower()
    for clause in _CLAUSE_SPLIT_RE.split(_body(text)):
        clean = _DATE_TIME_RE.sub(" ", clause)
        if n not in numbers_in(clean):
            continue
        if _names(clean, field_api) or (role_l and role_l != "undocumented" and role_l in clean.lower()):
            return True
    return False


def _num(value: Any) -> str | None:
    """A count as :func:`numbers_in` writes it ('1,234' -> '1234'), or None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return None
    return _norm_number(str(value))


def _count(value: Any) -> int:
    """A count key of a lookup check (``records``), 0 when it is missing or not a number."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _says_role(text: str, role: Any) -> bool:
    """True when ``text`` has the role's words as a phrase ("as interviewer", "the candidate"); "undocumented" never."""
    words = [w for w in re.findall(r"[a-z]+", str(role or "").lower()) if w != "undocumented"]
    if not words:
        return False
    return re.search(r"\b" + r"\s+".join(re.escape(w) for w in words) + r"\b", (text or "").lower()) is not None


def _role(item: dict) -> str:
    return f" ({item['role']})" if item.get("role") else ""


def _role_fit(item: dict) -> str:
    """`` (<role>; fits '<word>')`` when the question's role word fits the lookup, else :func:`_role`. The elsewhere
    nudge lists references on several objects; the mark tells the model which one the verb points to."""
    if not item.get("fits"):
        return _role(item)
    return f" ({item['role']}; fits '{item['fits']}')" if item.get("role") else f" (fits '{item['fits']}')"


def _last_per_field(checks: list[dict]) -> list[dict]:
    """The last check per (object, field, person), in call order: one filter per person on the same lookup keeps one
    check per person (:func:`_person_key`)."""
    last: dict[tuple[Any, Any, Any], dict] = {}
    for lc in checks:
        last[(lc.get("object"), lc.get("field"), _person_key(lc))] = lc
    keep = {id(lc) for lc in last.values()}
    return [lc for lc in checks if id(lc) in keep]


def _same_target(lc: dict) -> list[dict]:
    return [o for o in lc.get("others") or [] if isinstance(o, dict) and o.get("group") == "same_target"]


def _role_says_other(lc: dict, o: dict) -> bool:
    """The question's role word fits ``o`` but not the lookup the count used (``fits``)."""
    return lc.get("fits") is None and o.get("fits") is not None


def _fits_text(others: list[dict]) -> str:
    """`` The question's word '<w>' fits <G> (<role>).`` for the others whose role fits the verb, else ''."""
    fit = [o for o in others if o.get("fits")]
    if not fit:
        return ""
    return _fill(RECORDS_OTHER_LOOKUP_FITS_NOTE, WORD=fit[0]['fits'],
                 LOOKUPS=", ".join(f"{o.get('field')}{_role(o)}" for o in fit[:2]))


#: A short label line with no number ("**Summary:**", "The query confirms:"): skipped when finding the answer's lead.
_LABEL_LINE_RE = re.compile(r"^[#>*_\s-]*[A-Za-z][A-Za-z ,/()'-]{0,40}[:*_\s]*:[*_\s]*$|^[#>*_\s-]*[A-Za-z][A-Za-z ]{0,30}[*_]*$")


def _lead(text: str) -> str:
    """The answer's first sentence: the first line of the body (Source lines and short label lines without a number
    left out), up to its first sentence end."""
    for ln in _body(text).split("\n"):
        s = ln.strip()
        if not s or (not re.search(r"\d", s) and _LABEL_LINE_RE.match(s)):
            continue
        m = re.search(r"(?<=[.!?])\s+(?=[A-Z*_`(\"'])", s)
        return s[:m.start()] if m else s
    return ""


def _lead_numbers(text: str) -> set[str]:
    """The numbers of the answer's first sentence (:func:`_lead`), dates and times left out."""
    return set(numbers_in(_DATE_TIME_RE.sub(" ", _lead(text))))


def _leads_zero_without(text: str, counts: list[Any]) -> bool:
    """The answer's first sentence gives a 0 and none of ``counts``: it leads with the 0."""
    lead = _lead_numbers(text)
    return "0" in lead and not any(_num(c) in lead for c in counts if _num(c) is not None)


def _role_via(o: dict) -> str:
    """`` (<role>; via <link>)`` for a ``linked`` other, else :func:`_role`."""
    if o.get("group") == "linked" and o.get("via"):
        return f" ({o['role']}; via {o['via']})" if o.get("role") else f" (via {o['via']})"
    return _role(o)


def _unfit_zero_nudge(lc: dict, checks: list[dict], reply: str) -> str | None:
    """``RECORDS_UNFIT_ZERO_MESSAGE`` for a person check that counted 0 through a lookup whose role does not fit the
    question's role word (``cues`` set, ``fits`` and ``fits_sparse`` null), when no check of the same object counted
    a lookup that fits (or would fit but is almost empty), no same-target other fits either (that case is the
    other-lookup nudge's), the person has records through other same-target lookups of the object, and the draft's
    first sentence gives a 0 and none of those counts (and names no other object). Else None."""
    cues = [str(c) for c in lc.get("cues") or [] if c]
    if (not cues or lc.get("fits") or lc.get("fits_sparse") or lc.get("records") != 0
            or lc.get("person_kind") not in ("employee", "candidate")):
        return None
    obj = lc.get("object")
    if any(c.get("fits") or c.get("fits_sparse") for c in checks if c.get("object") == obj):
        return None   # a lookup of the asked role was counted: its 0 may be the answer
    lead_objs = {str(c.get("object")) for c in checks if c.get("object") and _names(_lead(reply), str(c.get("object")))}
    if lead_objs and str(obj) not in lead_objs:
        return None   # the first sentence is about another object
    if any(o.get("fits") for o in _same_target(lc)):
        return None   # another lookup fits the role word: the other-lookup nudge decides
    holders = [o for o in _same_target(lc) if _count(o.get("records")) > 0]
    if not holders or not _leads_zero_without(reply, [o.get("records") for o in holders]):
        return None
    sparse = [o for o in _same_target(lc) if o.get("fits_sparse")]
    sparse_text = (_fill(RECORDS_UNFIT_ZERO_SPARSE_NOTE, FIELD=sparse[0].get('field'), ROLE=_role(sparse[0]),
                         OBJECT=obj) if sparse else "")
    listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in holders[:_MAX_NUDGE_OTHERS])
    return (RECORDS_UNFIT_ZERO_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
            .replace("<<ROLE>>", str(lc.get("role") or "role not documented")).replace("<<WORD>>", cues[0])
            .replace("<<SPARSE>>", sparse_text).replace("<<OBJECT>>", str(obj)).replace("<<OTHERS>>", listed))


def _no_data_fields(evidence: list[QueryEvidence], qid: Any = None) -> set[str]:
    """The ``no_data_fields`` ("<Obj>.<Field>": a filtered lookup blank on every record) of the successful counting
    results (only the call ``qid`` when given)."""
    out: set[str] = set()
    for e in evidence:
        if (e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS and isinstance(e.model_result, dict)
                and (qid is None or e.query_id == qid)):
            out.update(str(x) for x in e.model_result.get("no_data_fields") or [] if x)
    return out


def _sparse_zero_nudge(lc: dict, evidence: list[QueryEvidence], reply: str, open_: Any) -> str | None:
    """``RECORDS_SPARSE_ZERO_MESSAGE`` for a person check that counted 0 through a lookup whose role fits the question's
    word but that is empty or almost empty (``fits_sparse``; a lookup with no data at all is the no-data nudge's), when
    ``same_target`` lookups whose role is not documented (role empty or "undocumented") hold the person, the draft does
    not report them (``open_`` with ``lead``) and its first sentence gives the 0 and none of their counts. Else None.
    A lookup with a documented role that does not fit the word (``Interviewer__c`` for "supported") never counts: its
    records are not the asked role, and leading with them would relabel them."""
    if (lc.get("records") != 0 or not lc.get("fits_sparse")
            or lc.get("person_kind") not in ("employee", "candidate")):
        return None
    if f"{lc.get('object')}.{lc.get('field')}" in _no_data_fields(evidence, lc.get("query_id")):
        return None
    holders = [o for o in _same_target(lc) if not o.get("fits") and str(o.get("role") or "").strip().lower()
               in ("", "undocumented") and open_(o, lead=True)]
    if not holders or not _leads_zero_without(reply, [o.get("records") for o in holders]):
        return None
    listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in holders[:_MAX_NUDGE_OTHERS])
    return (RECORDS_SPARSE_ZERO_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
            .replace("<<ROLE>>", str(lc.get("role") or "role not documented"))
            .replace("<<WORD>>", str(lc.get("fits_sparse"))).replace("<<OBJECT>>", str(lc.get("object")))
            .replace("<<OTHERS>>", listed))


def _other_lookup_firm(lc: dict, fit: list[dict]) -> str:
    """``RECORDS_OTHER_LOOKUP_FIRM_MESSAGE`` for a 0 through ``lc``'s lookup while the question's word fits the
    ``fit`` lookups (records above 0), not that one."""
    listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in fit[:_MAX_NUDGE_OTHERS])
    sparse = (_fill(RECORDS_OTHER_LOOKUP_FIRM_SPARSE_NOTE, FIELD=lc.get('field'), OBJECT=lc.get('object'))
              if lc.get("fits_sparse") else "")
    return (RECORDS_OTHER_LOOKUP_FIRM_MESSAGE.replace("<<SPARSE>>", sparse).replace("<<FIELD>>", str(lc.get("field")))
            .replace("<<ROLE>>", str(lc.get("role") or "role not documented"))
            .replace("<<WORD>>", str(fit[0].get("fits"))).replace("<<OBJECT>>", str(lc.get("object")))
            .replace("<<OTHERS>>", listed).replace("<<OTHER_FIELD>>", str(fit[0].get("field"))))


def unfit_lead_zero_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """The check of a draft written after a nudge (``RecordSession`` runs it on the second draft only): the firm
    other-lookup message (:func:`_other_lookup_firm`) when the draft still leads with the 0 of a person check whose
    lookup does not fit the question's word while a same-target lookup that fits holds the person (``fits`` set,
    records above 0), its first sentence gives none of the fitting counts (nor a later check's count through such a
    lookup) and names no fitting lookup, and it is not about another object. At most one message."""
    reply = draft or ""
    checks = _person_checks(evidence)
    objects = _object_names(checks)
    lead = _lead(reply)
    for lc in _last_per_field(checks):
        if lc.get("records") != 0 or lc.get("person_kind") not in ("employee", "candidate"):
            continue
        fit = [o for o in _same_target(lc) if _role_says_other(lc, o) and _count(o.get("records")) > 0]
        if not fit:
            continue
        fields = {str(o.get("field")) for o in fit}
        counts = [o.get("records") for o in fit] + [c.get("records") for c in checks if c.get("object") == lc.get(
            "object") and str(c.get("field")) in fields]
        if not _leads_zero_without(reply, counts):
            continue
        if any(_names_field(lead, o.get("field"), o.get("role"), objects) for o in fit):
            continue
        lead_objs = {str(c.get("object")) for c in checks if c.get("object") and _names(lead, str(c.get("object")))}
        if lead_objs and str(lc.get("object")) not in lead_objs:
            continue   # the first sentence is about another object
        return [_other_lookup_firm(lc, fit)]
    return []


def lookup_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """The other-lookup and elsewhere nudges for a draft answer (RECORDS_SPEC §21.10 check_draft steps 2-3).

    Only checks of person filters (:func:`_person_checks`), the last per (object, field, person). A lookup counts as
    handled when a later counting query used it, or the draft names it (:func:`_names_field`) or quotes its count in
    the same clause as its API name or role (:func:`_quotes`), except (for a 0) when the draft's first sentence gives
    the 0, neither that lookup's count nor its name: the draft leads with the 0 and mentions the other only aside.
    Linked nudge (``RECORDS_LINKED_LOOKUP_MESSAGE``), per check of kind "other" with ``records == 0``: ``linked``
    others with records (the value matched a record linked to a person, such as an employee's own Account through a
    candidate lookup). For employee and candidate checks, linked lookups never nudge.
    Other-lookup nudge, per check with ``records == 0``: the ``same_target`` others with records whose role says so
    (the used lookup's ``fits`` is null and the other's is set, or the question has no role word: ``cues`` empty).
    When the question's word fits one of them and not the used lookup, ``RECORDS_OTHER_LOOKUP_FIRM_MESSAGE``
    (:func:`_other_lookup_firm`: the 0 is no answer), unless the draft's first sentence names another counted object
    and not this one (that object's count leads; this object's counts are side notes, as in
    :func:`unfit_lead_zero_nudges`); else ``RECORDS_OTHER_LOOKUP_MESSAGE``, plus
    ``RECORDS_LEAD_ZERO_NOTE`` when the draft mentions them after leading with the 0. With ``records > 0`` (``RECORDS_OTHER_LOOKUP_FITS_MESSAGE``): only the others whose role strictly fits
    while the used lookup's does not. Same-name and system lookups never trigger them.
    Sparse-zero nudge (:func:`_sparse_zero_nudge`): a 0 through the lookup of the asked role that is almost empty
    (``fits_sparse``) leads the draft while other lookups of the object (roles that do not fit or are not documented)
    hold the person.
    Unfit-zero nudge (:func:`_unfit_zero_nudge`): a 0 through a lookup whose role does not fit the question's role word
    leads the draft while other lookups of the object hold the person and none of them fits either.
    Elsewhere nudge (``RECORDS_ELSEWHERE_MESSAGE``), per check with ``records == 0`` and ``elsewhere``, when no later
    query ran on one of those objects and the draft names none of them; not when the used lookup fits the verb and
    no reference elsewhere does (the 0 then answers the asked role).
    """
    reply = draft or ""
    checks = _person_checks(evidence)
    objects = _object_names(checks)
    out: list[str] = []
    for lc in _last_per_field(checks):
        qid, obj = lc.get("query_id"), lc.get("object")
        records = lc.get("records")

        def mentioned(o: dict) -> bool:   # the draft names the lookup or quotes its count next to its name or role
            return (_names_field(reply, o.get("field"), o.get("role"), objects)
                    or _quotes(reply, o.get("records"), o.get("field"), o.get("role")))

        def open_(o: dict, lead: bool = False) -> bool:
            """Records above 0, no later query used it, and the draft does not mention it (with ``lead``: or the
            draft leads with a 0 and gives this count only later)."""
            if _count(o.get("records")) <= 0 or _used_later(evidence, qid, obj, o.get("field")):
                return False
            if not mentioned(o):
                return True
            first = _lead(reply)
            return (lead and _leads_zero_without(reply, [o.get("records")])
                    and not _names_field(first, o.get("field"), o.get("role"), objects))

        if records == 0 and lc.get("person_kind") == "other":
            linked = [o for o in _linked_others(lc) if open_(o, lead=True)]
            if linked:
                listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in linked[:_MAX_NUDGE_OTHERS])
                via = "; ".join(dict.fromkeys(str(o.get("via")) for o in linked if o.get("via"))) or "a linked record"
                out.append(RECORDS_LINKED_LOOKUP_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
                           .replace("<<ROLE>>", str(lc.get("role") or "role not documented")).replace("<<VIA>>", via)
                           .replace("<<OBJECT>>", str(obj)).replace("<<OTHERS>>", listed))
                continue
        if lc.get("person_kind") == "other":
            continue   # kept only for its linked lookups

        if lc.get("person_kind") == "candidate":
            # The value matched an employee's own Account (the same person): the employee's lookups lead unless the
            # question asks about the candidate side; not when only the used lookup's role fits the question's word.
            def led(o: dict) -> bool:   # the draft's first sentence gives this count
                return _num(o.get("records")) in _lead_numbers(reply)

            emp_own = [o for o in _linked_others(lc) if o.get("target") == "Recruiter__c"
                       and (o.get("fits") or not lc.get("fits")) and o.get("field") != lc.get("field")
                       and _count(o.get("records")) > 0 and not _used_later(evidence, qid, obj, o.get("field"))
                       and not led(o)]
            if emp_own:
                listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in emp_own[:_MAX_NUDGE_OTHERS])
                via = "; ".join(dict.fromkeys(str(o.get("via")) for o in emp_own if o.get("via"))) or "a linked record"
                out.append(RECORDS_LINKED_EMPLOYEE_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
                           .replace("<<ROLE>>", str(lc.get("role") or "role not documented"))
                           .replace("<<COUNT>>", str(records)).replace("<<OBJECT>>", str(obj)).replace("<<VIA>>", via)
                           .replace("<<OTHERS>>", listed).replace("<<OTHER_FIELD>>", str(emp_own[0].get("field"))))
                continue
            # The count used a candidate record named like the person; an Employee with the same name (a different
            # record) has records through a lookup whose role fits the verb for an employee: the employee is meant
            # unless the question says candidate (prompt step 5).
            emp = [o for o in lc.get("others") or [] if isinstance(o, dict) and o.get("group") == "same_name"
                   and o.get("own_kind") == "employee" and o.get("own_fits") and o.get("field") != lc.get("field")
                   and open_(o)]
            if emp:
                listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in emp[:_MAX_NUDGE_OTHERS])
                out.append(RECORDS_SAME_NAME_EMPLOYEE_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
                           .replace("<<ROLE>>", str(lc.get("role") or "role not documented"))
                           .replace("<<COUNT>>", str(records)).replace("<<OBJECT>>", str(obj))
                           .replace("<<OTHERS>>", listed).replace("<<WORD>>", str(emp[0].get("own_fits")))
                           .replace("<<OTHER_FIELD>>", str(emp[0].get("field"))))
                continue

        if _count(records) > 0:
            others = [o for o in _same_target(lc) if _role_says_other(lc, o) and open_(o)]
            if others:
                listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in others[:_MAX_NUDGE_OTHERS])
                out.append(RECORDS_OTHER_LOOKUP_FITS_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
                           .replace("<<ROLE>>", str(lc.get("role") or "role not documented"))
                           .replace("<<COUNT>>", str(records)).replace("<<WORD>>", str(others[0].get("fits")))
                           .replace("<<OBJECT>>", str(obj)).replace("<<OTHERS>>", listed))
            continue
        if records != 0:
            continue
        no_role_word = not lc.get("cues")
        others = [o for o in _same_target(lc) if (_role_says_other(lc, o) or no_role_word) and open_(o, lead=True)]
        fit = [o for o in others if _role_says_other(lc, o)]
        lead = _lead(reply)
        lead_objs = {str(c.get("object")) for c in checks if c.get("object") and _names(lead, str(c.get("object")))}
        other_lead = bool(lead_objs) and str(obj) not in lead_objs
        if fit and not other_lead:
            # the question's word fits another lookup, not this one: its 0 is no answer (no "answer 0" choice)
            out.append(_other_lookup_firm(lc, fit))
            continue
        if others:   # with a first sentence about another counted object, this object's counts are side notes
            listed = ", ".join(f"{o.get('field')}{_role(o)} {o.get('records')}" for o in others[:_MAX_NUDGE_OTHERS])
            out.append(RECORDS_OTHER_LOOKUP_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
                       .replace("<<ROLE>>", str(lc.get("role") or "role not documented"))
                       .replace("<<OBJECT>>", str(obj)).replace("<<OTHERS>>", listed)
                       .replace("<<FITS>>", _fits_text(others[:_MAX_NUDGE_OTHERS]))
                       + (RECORDS_LEAD_ZERO_NOTE if not other_lead and any(mentioned(o) for o in others) else ""))
            continue
        sparse = _sparse_zero_nudge(lc, evidence, reply, open_)
        if sparse:
            out.append(sparse)
            continue
        unfit = _unfit_zero_nudge(lc, checks, reply)
        if unfit:
            out.append(unfit)
            continue
        elsewhere = [x for x in lc.get("elsewhere") or [] if isinstance(x, dict) and x.get("object")]
        if not elsewhere or lc.get("all_records"):
            continue   # all_records: the object holds the person; the other conditions explain the 0
        if lc.get("fits") and not any(x.get("fits") for x in elsewhere):
            continue   # the used lookup's role is the one asked, and no reference elsewhere fits better
        targets = {str(x["object"]) for x in elsewhere}
        if _queried_later(evidence, qid, targets) or any(_names(reply, o) for o in targets):
            continue
        listed = ", ".join(f"{x['object']}.{x.get('field')}{_role_fit(x)} {x.get('records')}"
                           for x in elsewhere[:_MAX_ELSEWHERE])
        out.append(RECORDS_ELSEWHERE_MESSAGE.replace("<<FIELD>>", str(lc.get("field")))
                   .replace("<<ROLE>>", str(lc.get("role") or "role not documented"))
                   .replace("<<OBJECT>>", str(obj)).replace("<<ELSEWHERE>>", listed))
    return out


#: Wording that relates one count to another ("220 of these", "on these records", "on the same 5 records", "the 15
#: include the 11", "4 additional", "11 + 15").
#: After "these" / "those": a word that makes it no set of records ("in these lookups", "for these roles").
_NOT_RECORDS = (r"(?!\s+(?:[\w-]+\s+)?(?:lookups?|roles?|fields?|people|persons?|columns?|objects?|capacit(?:y|ies)|"
                r"types?|categories|values?|names?)\b)")
#: A scope that places counts inside the counted records ("covering all `Interview__c` records created by this user",
#: "of the records she uploaded"): "records, not only those created by ..." is no such scope.
_COUNTED_SCOPE = (r"\b(?:covering|over|among|within|across|of|on|in|for)\s+(?:all\s+(?:of\s+)?)?(?:the\s+|these\s+|"
                  r"those\s+)?(?:\S+\s+){0,2}?records?\s+(?:(?:that\s+)?(?:she|he|they|this\s+\w+|the\s+\w+)\s+)?"
                  r"(?:\w+\s+)?(?:created|owned|uploaded|counted|conducted|hosted|held|edited|modified|assigned|"
                  r"supported|approved|taken|made)\b")
#: "the same <noun>" that is no set of records ("on the same object", "the same lookups").
_SAME_NOT_RECORDS = (r"(?!(?:conditions?|filters?|periods?|lookups?|roles?|dates?|way|time|person|people|name|objects?|"
                     r"tables?|fields?|query|queries)\b)")
#: "additional" that adds no records ("For additional context:").
_ADDITIONAL = r"\badditional\b(?!\s+(?:context|information|info|details?|notes?|lookups?|roles?|fields?|caveats?))"
_OVERLAP_RE = re.compile(r"\b(?:of|among|within|out of)\s+(?:(?:these|those)\b" + _NOT_RECORDS + r"|them\b)|"
                         r"\b(?:on|in|across|for)\s+(?:these|those)\b" + _NOT_RECORDS + r"|"
                         r"\b(?:of|on|in|among|across)\s+the\s+same\s+(?:\d[\d,]*\s+)?" + _SAME_NOT_RECORDS + r"[a-z]+\b|"
                         r"\binclud(?:e|es|ed|ing)\b|" + _ADDITIONAL + r"|\bplus\b|\bon top of\b|"
                         r"\boverlap(?:s|ping)?\s+with\b|"
                         # "distinct from the 18", "in addition to": the counts are said to be disjoint
                         r"\b(?:distinct|separate|apart|different)\s+from\s+(?:the\s+|these\s+|those\s+)?(?:\*\*)?\d|"
                         r"\bin\s+addition\s+to\b|" + _COUNTED_SCOPE + r"|"
                         r"\d\s*\+\s*\d", re.IGNORECASE)
#: Wording that puts a count inside the records of another count ("on these records", "220 of these", "on the same
#: records", "additional"): a lookup's own count named with it is always a made-up subset (lookup_check counts are
#: over all records that meet the other conditions).
_SUBSET_RE = re.compile(r"\b(?:of|out of)\s+(?:(?:these|those)\b" + _NOT_RECORDS + r"|them\b)|"
                        r"\b(?:among|within|on|in|across|for)\s+(?:these|those)\b" + _NOT_RECORDS + r"|"
                        r"\b(?:of|on|in|among|across)\s+the\s+same\s+(?:\d[\d,]*\s+)?" + _SAME_NOT_RECORDS + r"[a-z]+\b|"
                        + _ADDITIONAL + r"|\bon top of\b|\boverlap(?:s|ping)?\s+with\b|" + _COUNTED_SCOPE, re.IGNORECASE)
#: Wording that presents a number as extra records ("6 additional", "6 more", "other roles on 6"): checked against
#: differences of two lookup_check counts, which no result states.
_EXTRA_RE = re.compile(r"\b(?:additional|more|extra|other|another|besides|beyond|remaining|rest|further)\b", re.IGNORECASE)
_EXTRA_NEAR_WORDS = 3


def _extra_numbers(clause: str) -> set[str]:
    """The numbers of ``clause`` that it presents as extra records: an extra word (:data:`_EXTRA_RE`) within three
    words of the number ("6 additional records", "another 6", "other roles on 6"), the number counting no lookups,
    roles or fields (:data:`_NOT_RECORD_COUNT_RE`: "in 3 other roles" is no count of records)."""
    skip = {n for m in _NOT_RECORD_COUNT_RE.finditer(clause) for n in numbers_in(m.group(1))}
    words = clause.split()
    out: set[str] = set()
    for i, w in enumerate(words):
        for n in numbers_in(w):
            near = " ".join(words[max(0, i - _EXTRA_NEAR_WORDS):i] + words[i + 1:i + 1 + _EXTRA_NEAR_WORDS])
            if n not in skip and _EXTRA_RE.search(near):
                out.add(n)
    return out


#: Result keys whose numbers are no counts of the records the result counted (overlap check).
_SIDE_KEYS = frozenset({"lookup_check", "notes", "same_target_lookups", "unsynced_fits", "query_id", "source", "applied"})


def _reference_counts(e: QueryEvidence) -> list[Any]:
    """The reference counts of a find_references result (``references`` and ``system_references``, with their
    per-value ``by`` counts): records over the whole object, never a part of another result's count."""
    res = e.model_result if isinstance(e.model_result, dict) else {}
    vals: list[Any] = []
    for key in ("references", "system_references", "links"):
        for r in res.get(key) or []:
            if isinstance(r, dict):
                vals.append(r.get("records"))
                by = r.get("by")
                vals += list(by.values()) if isinstance(by, dict) else []
    vals += [p.get("business_reference_total") for p in res.get("people") or [] if isinstance(p, dict)]
    return vals


def _side_counts(evidence: list[QueryEvidence]) -> set[str]:
    """Counts of the lookup checks that cover other records than the result's own count: other lookups (``others``),
    other objects (``elsewhere``) and all records (``all_records``). ``any_role`` and ``by_record`` are left out: the
    result's own records are part of them."""
    out: set[str] = set()
    for lc in lookup_checks(evidence):
        vals = [o.get("records") for o in lc.get("others") or [] if isinstance(o, dict)]
        vals += [x.get("records") for x in lc.get("elsewhere") or [] if isinstance(x, dict)]
        held = lc.get("all_records")
        vals += list(held.values()) if isinstance(held, dict) else []
        out.update(n for n in (_num(v) for v in vals if _count(v) > 0) if n)
    return out


def _reference_side_counts(evidence: list[QueryEvidence]) -> set[str]:
    """The reference counts of the find_references results (:func:`_reference_counts`), above 0."""
    out: set[str] = set()
    for e in evidence:
        if e.ok and e.kind == "data" and e.tool in PERSON_TOOLS:
            out.update(n for n in (_num(v) for v in _reference_counts(e) if _count(v) > 0) if n)
    return out


def _check_numbers(lc: dict) -> list[int]:
    """Every count of a lookup check: its own, any_role, others, elsewhere, all_records and by_record."""
    vals: list[Any] = [lc.get("records"), lc.get("any_role")]
    for key in ("others", "elsewhere", "by_record"):
        vals += [x.get("records") for x in lc.get(key) or [] if isinstance(x, dict)]
    held = lc.get("all_records")
    vals += list(held.values()) if isinstance(held, dict) else []
    return [v for v in vals if isinstance(v, int) and not isinstance(v, bool)]


def _derived_counts(evidence: list[QueryEvidence]) -> set[str]:
    """Differences of two counts of one lookup check (any_role - records = "6 additional records") that are no count
    of any lookup check: no result states them, so a draft that gives one as extra records did arithmetic."""
    out: set[str] = set()
    stated: set[str] = set()
    for lc in lookup_checks(evidence):
        nums = sorted({v for v in _check_numbers(lc) if v > 0})
        stated.update(str(v) for v in _check_numbers(lc))
        out.update(str(a - b) for a in nums for b in nums if a > b)
    return out - stated


def _named_difference(clause: str, lc: dict, nums: set[str]) -> bool:
    """True when ``clause`` names a lookup of the check ``lc`` (its own or an ``others`` lookup of the same person) next
    to a number that is not that lookup's count but the difference of two of the check's counts ("6 additional records
    (`Interview_Readiness_Person__c`)" when that lookup holds 12 and any_role - records = 6). ``nums`` should hold no
    number that a note states (a note's "filled on only 1 of 400" is no difference)."""
    counts = {str(lc.get("field")): lc.get("records")}
    counts.update({str(o.get("field")): o.get("records") for o in lc.get("others") or []
                   if isinstance(o, dict) and o.get("field") and o.get("same_person", True)})
    named = [f for f in counts if f and _names(clause, f)]
    if not named:
        return False
    vals = _check_numbers(lc)
    pos = sorted({v for v in vals if v > 0})
    diffs = {str(a - b) for a in pos for b in pos if a > b} - {str(v) for v in vals}
    return any(n in diffs and all(_num(counts[f]) != n for f in named) for n in nums)


def _named_side(clause: str, lc: dict, nums: set[str]) -> bool:
    """True when ``clause`` names another lookup of the check ``lc`` (an ``others`` entry with records) next to its own
    count: the clause speaks of that lookup's count, whatever other result happens to hold the same number."""
    return any(_count(o.get("records")) > 0 and _num(o.get("records")) in nums and _names(clause, o.get("field"))
               for o in lc.get("others") or [] if isinstance(o, dict) and o.get("field"))


def _result_numbers(evidence: list[QueryEvidence], references: bool = True) -> set[str]:
    """The numbers of the successful data results without their side keys (:data:`_SIDE_KEYS`); without
    ``references`` the find_references results are left out."""
    out: set[str] = set()
    for e in evidence:
        if (e.ok and e.kind == "data" and (references or e.tool not in PERSON_TOOLS)
                and isinstance(e.model_result, dict)):
            core = {k: v for k, v in e.model_result.items() if k not in _SIDE_KEYS}
            out.update(numbers_in(json.dumps(core, ensure_ascii=False, default=str)))
    return out


def overlap_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_OVERLAP_MESSAGE`` for the first clause of the draft that relates counts to each other ("of these",
    "on these records", "include", "additional", "plus", "11 + 15") and holds a lookup_check count of other records
    (:func:`_side_counts`) that is no number of the results themselves, or that puts a find_references count
    (:func:`_reference_side_counts`; all records of an object) inside the counted records ("220 of these", "on these
    records"; :data:`_SUBSET_RE`, so a role breakdown "roles include Trainer: 10" passes), or that presents as
    extra records ("additional", "more", "other") a difference of two lookup_check counts that no result holds
    (:func:`_derived_counts`), or such a difference next to a lookup whose count it is not
    (:func:`_named_difference`), or that puts another lookup's count, named with its lookup, inside the counted records
    ("on these records: `X` (12)", :func:`_named_side`; also when another result holds the same number). No result
    states such an overlap or difference, so the claim is made up. :func:`merge_scope_issues` runs the same check
    (:func:`_overlap_clause`) on a merged answer."""
    clause = _overlap_clause(draft, evidence)
    return [RECORDS_OVERLAP_MESSAGE.replace("<<CLAUSE>>", clause[:240])] if clause else []


def _overlap_units(text: str) -> list[tuple[str, list[int], str]]:
    """``(clause, line indices, scope text)`` of ``text`` for the overlap check: every clause of every line
    (:data:`_CLAUSE_SPLIT_RE`; Source lines left out; its scope text is the clause), and before them a label line that
    ends with ":" (no number of its own) together with the list items under it, as one clause whose scope text is the
    label alone: the label's scope words ("**Lookup counts** (covering all `X__c` records created by this user):",
    "**On these records:**") apply to the items' counts, while an item's own words ("including ...") stay with the
    item."""
    lines = (text or "").split("\n")
    units: list[tuple[str, list[int], str]] = []
    for i, ln in enumerate(lines):
        if _is_source_line(ln) or not _label_only(ln):
            continue
        items: list[int] = []
        j = i + 1
        while j < len(lines) and lines[j].strip() and _LIST_ITEM_RE.match(lines[j]) and not _is_source_line(lines[j]) \
                and not _label_only(lines[j]):
            items.append(j)
            j += 1
        if items:
            units.append((" ".join([lines[i].strip()] + [lines[k].strip() for k in items]), [i] + items,
                          lines[i].strip()))
    for i, ln in enumerate(lines):
        if _is_source_line(ln) or not ln.strip():
            continue
        units += [(c, [i], c) for c in _CLAUSE_SPLIT_RE.split(ln) if c.strip()]
    return units


def _overlap_clause(draft: str, evidence: list[QueryEvidence]) -> str | None:
    """The first clause of ``draft`` that :func:`overlap_nudges` finds (one line, spaces collapsed), else None."""
    found = _overlap_found(draft, evidence)
    return found[0][0] if found else None


def _overlap_found(draft: str, evidence: list[QueryEvidence]) -> list[tuple[str, list[int]]]:
    """Every unit of ``draft`` (:func:`_overlap_units`) that :func:`overlap_nudges` finds, in order, as (clause with
    spaces collapsed, line indices)."""
    core = _result_numbers(evidence)
    side = _side_counts(evidence) - core
    refs = _reference_side_counts(evidence) - _result_numbers(evidence, references=False)
    derived = _derived_counts(evidence) - core - _side_counts(evidence) - refs
    checks = lookup_checks(evidence)
    if not side and not refs and not derived and not checks:
        return []
    noted = {n for e in evidence if e.ok and isinstance(e.model_result, dict)
             for n in numbers_in(" ".join(str(x) for x in e.model_result.get("notes") or []))}
    out: list[tuple[str, list[int]]] = []
    for clause, where, scope in _overlap_units(draft or ""):
        clean, words = _DATE_TIME_RE.sub(" ", clause), _DATE_TIME_RE.sub(" ", scope)
        nums = set(numbers_in(clean))
        block = len(where) > 1
        overlap = _OVERLAP_RE.search(words) is not None
        extra = not block and _EXTRA_RE.search(clean) is not None
        subset = _SUBSET_RE.search(words) is not None
        record_nums = nums - {n for m in _NOT_RECORD_COUNT_RE.finditer(clean) for n in numbers_in(m.group(1))}
        if ((overlap and side & nums) or (subset and refs & nums) or (extra and derived & _extra_numbers(clean))
                or ((overlap or extra) and any(_named_difference(clean, lc, record_nums - noted) for lc in checks))
                or (subset and any(_named_side(clean, lc, nums) for lc in checks))):
            out.append((" ".join(clause.split()), where))
    return out


def _side_item(line: str, evidence: list[QueryEvidence]) -> bool:
    """A list item that gives a count of other records than the result's own: a lookup-check count of another lookup,
    another object or all records, a find_references count, or a difference of two lookup-check counts (above 0 and
    no number of the results themselves), or another lookup named with its own count (:func:`_named_side`)."""
    clean = _DATE_TIME_RE.sub(" ", line or "")
    nums = set(numbers_in(clean))
    core = _result_numbers(evidence)
    other = ((_side_counts(evidence) - core) | (_reference_side_counts(evidence) - _result_numbers(evidence, False))
             | (_derived_counts(evidence) - core))
    return bool(nums & other) or any(_named_side(clean, lc, nums) for lc in lookup_checks(evidence))


def drop_overlap_clauses(text: str | None, evidence: list[QueryEvidence]) -> str:
    """``text`` without the made-up overlap clauses that :func:`overlap_nudges` finds, as the last backstop for the final
    record answer (the draft after a nudge, or the last one when the tool budget ran out, is not checked by a model
    call). Under a label line found with its list items ("**Lookup counts** (covering all `X__c` records created by
    this user):" and the items) the items that give other records' counts go (:func:`_side_item`; all items when none
    does), and the label with them when no item is left (else the label loses a bracket that holds its scope words); a
    clause in a list item takes the item; a clause in prose takes its sentence. The lead sentence is never removed; at
    most 3 removals. Unchanged when nothing would be left that states a number."""
    body = text or ""
    for _ in range(3):
        lines = body.split("\n")
        lead = " ".join(_lead(body).split())
        found = next(((c, w) for c, w in _overlap_found(body, evidence)
                      if not (len(w) == 1 and lead and c in lead)), None)
        if found is None:
            break
        clause, where = found
        if len(where) > 1:   # a label with its items: the items with other records' counts; the label if none is left
            gone = [i for i in where[1:] if _side_item(lines[i], evidence)] or where[1:]
            if len(gone) == len(where) - 1:
                gone = where
            else:   # the label stays for the items left: without a bracket that holds its scope words
                lines = list(lines)
                lines[where[0]] = re.sub(r"\s*\(([^()\n]*)\)",
                                         lambda m: "" if (_OVERLAP_RE.search(m.group(1)) or _SUBSET_RE.search(m.group(1)))
                                         else m.group(0), lines[where[0]])
            out = [ln for i, ln in enumerate(lines) if i not in gone]
        elif _LIST_ITEM_RE.match(lines[where[0]]):
            out = [ln for i, ln in enumerate(lines) if i not in where]
        else:
            ln = lines[where[0]]
            sents = re.split(r"(?<=[.!?])\s+", ln)
            kept = [x for x in sents if clause not in " ".join(x.split())]
            if len(kept) == len(sents):
                break
            out = lines[:where[0]] + ([" ".join(kept)] if any(x.strip() for x in kept) else []) + lines[where[0] + 1:]
        new = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
        if new == body or not numbers_in(_DATE_TIME_RE.sub(" ", _body_text(new))):
            break
        body = new
    return body


#: A breakdown line of a draft: "- **Completed**: 3", "Unscheduled: 7 interviews" (label, then a whole number that is no
#: percentage and no date).
_PAIR_LINE_RE = re.compile(r"^[\s>]*(?:[-*•+]|\d+[.)])?\s*[*_`\"']*(?P<label>[A-Za-z(][^:\n]{0,58}?)[*_`\"']*\s*:\s*[*_]*"
                           r"\s*(?P<n>\d[\d,]*)(?![\d,]*(?:\.\d|\s*%|-\d|:\d))", re.MULTILINE)
#: Labels that are no stored value (totals, blanks): never checked.
_NO_VALUE_LABEL_RE = re.compile(r"\b(?:total|all|blank|none|null|unresolved|other|others|count|sum|any|overall)\b|\d",
                                re.IGNORECASE)
_LABEL_KEYS = ("group", "label", "bucket", "value", "segment", "name", "field", "record")
_COUNT_KEYS = ("value", "records", "count", "n", "total", "part", "base", "total_count", "total_across_groups")


def _norm_label(text: Any) -> str:
    return " ".join(re.sub(r"[*_`\"']+", " ", str(text or "")).lower().split())


def _pairs(obj: Any, out: set[tuple[str, str]]) -> None:
    """(label, count) pairs of a result: entries with a label key and a count key, and {label: count} mappings."""
    if isinstance(obj, dict):
        labels = [obj[k] for k in _LABEL_KEYS if isinstance(obj.get(k), str)]
        nums = [_num(obj[k]) for k in _COUNT_KEYS if k in obj and _num(obj[k]) is not None]
        for lab in labels:
            for n in nums:
                out.add((_norm_label(lab), n))
        for k, v in obj.items():
            n = _num(v)
            if n is not None and isinstance(k, str) and k not in _COUNT_KEYS:
                out.add((_norm_label(k), n))
            _pairs(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _pairs(x, out)


def _result_pairs(evidence: list[QueryEvidence]) -> set[tuple[str, str]]:
    """Every (label, count) pair of the successful data results, plus (filter value, count) for results whose filters
    name a value ("Status__c = 'Completed'" with count 3)."""
    out: set[tuple[str, str]] = set()
    for e in evidence:
        res = e.model_result if (e.ok and e.kind == "data" and isinstance(e.model_result, dict)) else None
        if res is None:
            continue
        _pairs(res, out)
        values = re.findall(r"'((?:[^']|'')+)'", json.dumps(res.get("applied") or {}, ensure_ascii=False))
        for k in ("count", "total", "part", "total_count", "total_across_groups"):
            n = _num(res.get(k))
            if n is not None:
                out.update((_norm_label(v), n) for v in values)
    return out


def _value_domains(evidence: list[QueryEvidence]) -> dict[str, set[str]]:
    """Field API name -> its stored values, from describe_fields and get_field_values results."""
    out: dict[str, set[str]] = {}
    for e in evidence:
        res = e.model_result if (e.ok and isinstance(e.model_result, dict)) else None
        if res is None:
            continue
        rows = [res] if e.tool == "get_field_values" else [f for f in res.get("fields") or [] if isinstance(f, dict)]
        for f in rows:
            vals = [v.get("value") for v in f.get("values") or [] if isinstance(v, dict) and isinstance(v.get("value"), str)]
            if f.get("field") and vals:
                out.setdefault(str(f["field"]).split(".")[-1], set()).update(_norm_label(v) for v in vals)
    return out


def _counted_objects(evidence: list[QueryEvidence]) -> list[str]:
    """The objects whose records the data results count: each data call's object and, for a person tool, the objects
    of its references."""
    out: list[str] = []
    for e in evidence:
        if not (e.ok and e.kind == "data"):
            continue
        if e.object:
            out.append(str(e.object))
        if e.tool in PERSON_TOOLS and isinstance(e.model_result, dict):
            out += [str(r.get("object")) for r in e.model_result.get("references") or []
                    if isinstance(r, dict) and r.get("object")]
    return list(dict.fromkeys(out))


def breakdown_nudges(draft: str, evidence: list[QueryEvidence], status_values: set[str] | None = None) -> list[str]:
    """``RECORDS_BREAKDOWN_MESSAGE`` for breakdown lines of the draft ("- **Completed**: 3") whose count no result gives
    for that label: the label is a group or split value of a grouped result with another count, or a stored value of
    the grouped field (describe_fields, get_field_values) that the grouped result does not have (it has no records
    under the query's conditions). The audit's 0-12 allowance lets such small invented counts through otherwise."""
    grouped = [e.model_result for e in evidence if e.ok and e.kind == "data" and e.tool in ("group_records",)
               and isinstance(e.model_result, dict)]
    counted = [e.model_result for e in evidence if e.ok and e.kind == "data" and isinstance(e.model_result, dict)
               and isinstance(e.model_result.get("breakdown"), dict)]   # a person count's status / purpose split
    if not grouped and not counted:
        # no result splits anything: a line whose label is a status value of a counted object (``status_values``,
        # from the catalog) and whose count no result gives is invented (the audit lets 0-12 through)
        if not status_values:
            return []
        pairs = _result_pairs(evidence)
        bad = []
        for m in _PAIR_LINE_RE.finditer(_body(draft or "")):
            label, n = _norm_label(m.group("label")), _norm_number(m.group("n"))
            if label in status_values and n not in (None, "0") and (label, n) not in pairs:
                bad.append(f"{m.group('label').strip(' *_`')}: {n}")
        return [RECORDS_BREAKDOWN_MESSAGE.replace("<<ITEMS>>", "; ".join(dict.fromkeys(bad[:6])))] if bad else []
    pairs = _result_pairs(evidence)
    domains = _value_domains(evidence)
    keys: set[str] = set()
    missing: set[str] = set()
    for res in counted:
        for fld, by in res["breakdown"].items():
            if isinstance(by, dict):
                have = {_norm_label(k) for k in by}
                keys |= have
                if not res.get("trimmed") and "(other)" not in by:
                    missing |= domains.get(str(fld).split(".")[-1], set()) - have
    for res in grouped:
        gkeys = {_norm_label(g.get("group")) for g in res.get("groups") or [] if isinstance(g, dict)}
        skeys = {_norm_label(k) for g in res.get("groups") or [] if isinstance(g, dict)
                 for k in (g.get("by") or {}) if isinstance(g.get("by"), dict)}
        skeys |= {_norm_label(k) for k in (res.get("split_totals") or {})}
        keys |= gkeys | skeys
        for fld, have in ((res.get("grouped_by"), gkeys), (res.get("split_by"), skeys)):
            api = str(fld or "").split(" ")[0].split(".")[-1]
            if api and api in domains and not res.get("trimmed") and res.get("shown", 0) >= res.get("group_count", 0):
                missing |= domains[api] - have
    bad: list[str] = []
    for m in _PAIR_LINE_RE.finditer(_body(draft or "")):
        label, n = _norm_label(m.group("label")), _norm_number(m.group("n"))
        if not label or n is None or _NO_VALUE_LABEL_RE.search(label) or (label, n) in pairs:
            continue
        if label in keys or (label in missing and n != "0"):
            bad.append(f"{m.group('label').strip(' *_`')}: {n}")
    if not bad:
        return []
    return [RECORDS_BREAKDOWN_MESSAGE.replace("<<ITEMS>>", "; ".join(dict.fromkeys(bad[:6])))]


#: A trend whose date field is blank on at least this share of the records meeting its other conditions is checked.
_MIN_UNDATED_SHARE = 0.10
_MIN_UNDATED = 5


def _first_date_field(evidence: list[QueryEvidence], obj: str | None) -> str | None:
    """The first entry of ``date_fields`` in the last describe_fields result of ``obj`` (its main business date)."""
    for e in reversed(evidence):
        if e.ok and e.tool == "describe_fields" and e.object == obj and isinstance(e.model_result, dict):
            for d in e.model_result.get("date_fields") or []:
                if isinstance(d, dict) and d.get("field"):
                    return str(d["field"])
    return None


def date_nudges(evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_DATE_COVERAGE_MESSAGE`` for the last count trend per object whose ``records_without_date`` is at
    least 5 and at least 10% of the records meeting its other conditions (``total`` + ``records_without_date``), and
    whose date field is not the object's first business date (describe_fields ``date_fields``)."""
    last: dict[Any, QueryEvidence] = {}
    for e in evidence:
        if e.ok and e.kind == "data" and e.tool == "trend_records" and isinstance(e.model_result, dict):
            last[e.object] = e
    out: list[str] = []
    for obj, e in last.items():
        res = e.model_result or {}
        nd, total, df = res.get("records_without_date"), res.get("total"), res.get("date_field")
        if res.get("measure") not in (None, "count") or not df or isinstance(nd, bool) or not isinstance(nd, int):
            continue
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            continue
        if nd < _MIN_UNDATED or nd < _MIN_UNDATED_SHARE * (total + nd):
            continue
        main = _first_date_field(evidence, obj)
        if main == df:
            continue
        out.append(RECORDS_DATE_COVERAGE_MESSAGE.replace("<<FIELD>>", str(df)).replace("<<MISSING>>", str(nd))
                   .replace("<<MAIN>>", _fill(RECORDS_DATE_COVERAGE_MAIN, MAIN=main, OBJECT=obj) if main
                            else _fill(RECORDS_DATE_COVERAGE_MAIN_UNKNOWN, OBJECT=obj)))
    return out


def mirror_nudges(evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_MIRROR_MESSAGE`` for the last successful counting query per object whose result has
    ``copied_lookups`` (a lookup that copies a path of the same record was used without the path), unless a later
    counting query on that object used the path's first lookup."""
    last: dict[Any, QueryEvidence] = {}
    for e in evidence:
        if e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS:
            last[e.object] = e
    out: list[str] = []
    for obj, e in last.items():
        res = e.model_result if isinstance(e.model_result, dict) else {}
        for c in res.get("copied_lookups") or []:
            if not isinstance(c, dict) or not c.get("field") or not c.get("source"):
                continue
            if (str(obj), str(c["source"]).split(".")[0]) in _fields_used(e):
                continue
            out.append(RECORDS_MIRROR_MESSAGE.replace("<<OBJECT>>", str(obj)).replace("<<FIELD>>", str(c["field"]))
                       .replace("<<SOURCE>>", str(c["source"])))
    return out


def unsynced_checks(evidence: list[QueryEvidence]) -> list[dict]:
    """Every ``unsynced_fits`` entry of the successful data results (the lookup the question's words fit is
    documented but not synced), once per (object, field), with ``object``, ``query_id`` and ``tool`` added."""
    out: list[dict] = []
    seen: set[tuple[Any, Any]] = set()
    for e in evidence:
        if not (e.ok and e.kind == "data" and isinstance(e.model_result, dict)):
            continue
        for x in e.model_result.get("unsynced_fits") or []:
            if not isinstance(x, dict) or not x.get("field"):
                continue
            key = (e.object, x.get("field"))
            if key in seen:
                continue
            seen.add(key)
            out.append({**x, "object": e.object, "query_id": e.query_id, "tool": e.tool})
    return out


def _proxy_text(x: dict) -> str:
    proxy = [str(p) for p in x.get("proxy") or [] if p]
    return ", ".join(proxy) if proxy else "another lookup or path"


def _unsynced_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_NOT_SYNCED_MESSAGE`` for each unsynced lookup (:func:`unsynced_checks`) that the draft does not name."""
    out: list[str] = []
    for x in unsynced_checks(evidence):
        if _names(draft, x.get("field")):
            continue
        out.append(RECORDS_NOT_SYNCED_MESSAGE.replace("<<WORD>>", str(x.get("fits") or ""))
                   .replace("<<OBJECT>>", str(x.get("object"))).replace("<<FIELD>>", str(x.get("field")))
                   .replace("<<ROLE>>", str(x.get("role") or "lookup")).replace("<<PROXY>>", _proxy_text(x)))
    return out


def _norm_text(text: str) -> str:
    """Lower case, non-breaking and repeated spaces read as one space, markdown emphasis removed (name matching)."""
    t = re.sub(r"[*_`]+", "", str(text or "").replace("\xa0", " ")).lower()
    return " ".join(t.split())


def _has_name(norm_text: str, name: str) -> bool:
    n = _norm_text(name)
    return bool(n) and re.search(r"(?<![a-z0-9])" + re.escape(n) + r"(?![a-z0-9])", norm_text) is not None


def covers_all_named(text: str | None, words: str) -> bool:
    """``text`` (Source lines aside) makes a statement about every stored person with the name ``words``: a quantifier
    before the name ("no other Kilo", "any Kilo", "every Kilo", "all Kilos", "neither Sierra", "none of the people
    named Kilo", "No other Kilo or Sierra"), with "other", "stored" and a person noun ("person named", "employee
    called") allowed in between; a full name after the quantifier ("no Kilo Zephyr record") is one person."""
    w = " ".join((words or "").split())
    if not w:
        return False
    name = r"\s+".join(re.escape(x) for x in w.split())
    noun = r"(?:people|persons?|employees?|candidates?|users?|records?|leads?|accounts?)"
    pat = (r"\b(?:no|any|every|each|all|neither|none\s+of\s+the)\s+(?:other\s+)?(?:stored\s+)?"
           r"(?:(?:" + noun + r"\s+)?(?:named|called)\s+)?(?:[\"'\u201c\u2018]?[A-Za-z][\w.'-]*[\"'\u201d\u2019]?\s+(?:or|and|nor)\s+)?"
           r"[\"'\u201c\u2018]?" + name + r"(?:s|es|'s)?\b(?!(?-i:\s+[A-Z][a-z]))")
    return re.search(pat, _body_text(text or ""), re.IGNORECASE) is not None


def people_nudges(draft: str, people: list[dict] | None) -> list[str]:
    """Answer checks on the people the question names (``RecordTools.question_people``).

    ``RECORDS_NOT_EXACT_NAME_MESSAGE``: the question's full name is not stored (``asked``), and the draft uses a stored
    name but never the asked spelling (it corrected the name silently). ``RECORDS_SEVERAL_PEOPLE_MESSAGE``: the question
    gives only a first name that several people have (``first_only`` with 2 or more ``names``), and the draft names
    exactly one of them (a silent pick; naming none, for example asking which one, passes). With the spelling help on,
    a person it read as a stored spelling (``read_as``, a stored first name for one misspelt word) counts as used when
    the draft names that spelling, so a draft that only writes the stored first name is nudged too; and a first name it
    read that way which several people have (``first_only`` and ``read_as``) gets the several-people check on the full
    names (``<<WORD>>`` is the stored first name).
    """
    out: list[str] = []
    text = _norm_text(draft)
    for p in people or []:
        if not isinstance(p, dict):
            continue
        names = [str(n) for n in p.get("names") or [] if n]
        used = [n for n in names if _has_name(text, n)]
        asked = p.get("asked")
        if asked and not used and p.get("read_as") and _has_name(text, str(p["read_as"])):
            used = [str(p["read_as"])]   # the spelling help read one word as a stored first name the draft names
        if asked:
            if used and not _has_name(text, str(asked)):
                out.append(RECORDS_NOT_EXACT_NAME_MESSAGE.replace("<<ASKED>>", str(asked))
                           .replace("<<USED>>", ", ".join(f"'{n}'" for n in used[:2])))
            full = [n for n in names if _has_name(text, n)]   # full names only: the stored first name alone is no pick
            if p.get("first_only") and p.get("read_as") and len({_norm_text(n) for n in names}) >= 2 and len(full) == 1:
                out.append(RECORDS_SEVERAL_PEOPLE_MESSAGE.replace("<<WORD>>", str(p["read_as"]))
                           .replace("<<NAMES>>", ", ".join(f"'{n}'" for n in names[:6]))
                           .replace("<<USED>>", f"'{full[0]}'"))
            continue
        if p.get("first_only") and len({_norm_text(n) for n in names}) >= 2 and len(used) == 1:
            out.append(RECORDS_SEVERAL_PEOPLE_MESSAGE.replace("<<WORD>>", str(p.get("words") or ""))
                       .replace("<<NAMES>>", ", ".join(f"'{n}'" for n in names[:6]))
                       .replace("<<USED>>", f"'{used[0]}'"))
    return out


#: The draft says that several records share the person's name ("two Employee records with the same name", "#2").
#: Only explicit wording counts ("same name", "(#2)", "Delta Person #2", "two employees named ..."): "N records with a
#: blank status" or a fill rate ("300 of 400 records with a value") is no word about a shared name.
_SHARED_SAID_RE = re.compile(r"same\s+name|share[sd]?\s+(?:the|this|a|that|their|one)\s+(?:same\s+)?name|\(#\d+\)|#\d+\b|"
                             r"\b(?:two|three|four|five|several|multiple|[2-9])\s+"
                             r"(?:different\s+|distinct\s+|separate\s+)?(?:[a-z]+\s+){0,2}"
                             r"(?:records|employees|people|persons|users|accounts|candidates|leads)\s+"
                             r"(?:named|called)\b|"
                             r"named\s+alike|duplicate", re.IGNORECASE)


def shared_name_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_SHARED_NAME_MESSAGE`` for the last person check per (object, field, person) whose filter matched two
    or more records of the asked person kind that share the name (``by_record`` entries whose ``kind`` is the check's
    ``person_kind``; entries without ``kind`` count as that kind), one of them with records, when the draft does not
    say that several records share the name (:data:`_SHARED_SAID_RE`). A record of another kind with the same name (an
    employee's own Account next to a candidate) is no shared name of the asked person: other nudges cover it."""
    out: list[str] = []
    if _SHARED_SAID_RE.search(_body(draft or "")):
        return out
    for lc in _last_per_field(_person_checks(evidence)):
        kind = lc.get("person_kind")
        same = [b for b in lc.get("by_record") or [] if isinstance(b, dict) and b.get("kind", kind) == kind]
        if len(same) < 2 or not any(_count(b.get("records")) > 0 for b in same):
            continue
        names = _filter_names(lc)
        listed = ", ".join(f"{b.get('record')}" + (f" ({b.get('detail')})" if b.get("detail") else "")
                           + f" {b.get('records')}" for b in same[:5])
        out.append(RECORDS_SHARED_NAME_MESSAGE.replace("<<NAME>>", names[0] if names else str(lc.get("filter")))
                   .replace("<<N>>", str(len(same))).replace("<<LABEL>>", str(lc.get("target_label") or lc.get("target")))
                   .replace("<<FIELD>>", f"{lc.get('object')}.{lc.get('field')}").replace("<<RECORDS>>", listed))
        break
    return out


#: The draft's first sentence already says the data does not hold the value ("does not record", "blank on").
_NO_DATA_SAID_RE = re.compile(r"does\s+not\s+record|doesn't\s+record|not\s+recorded|no\s+data|blank\s+on|never\s+filled|"
                              r"not\s+filled|(?:is|are)\s+(?:empty|blank)|has\s+no\s+values?", re.IGNORECASE)


def no_data_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_NO_DATA_MESSAGE`` when a counting result filtered a person through a lookup with no data at all
    (``no_data_fields``) and the draft's first sentence gives 0 without saying that the data does not record it."""
    fields = sorted(_no_data_fields(evidence))
    if not fields:
        return []
    lead = _lead(draft or "")
    if "0" not in _lead_numbers(draft or "") or _NO_DATA_SAID_RE.search(lead):
        return []
    return [RECORDS_NO_DATA_MESSAGE.replace("<<FIELDS>>", ", ".join(fields[:3]))]


def with_no_data_line(text: str, evidence: list[QueryEvidence]) -> str:
    """``text`` with a first line from code when the final answer still leads with 0 through a lookup that has no data
    (the case of :func:`no_data_nudges`, whose retry is sent once only and not again after another message came
    first): "The data does not record this: `<Obj>.<Field>` is blank on every record in the warehouse, so a 0
    through it is no answer." The lead sentence itself is removed when 0 is its only number ("Delta Person has
    approved **0** resumes." would contradict the line above it); a lead with other numbers stays. Unchanged when the
    lead says so already or names a custom API name other than those objects and fields (its 0 is about something
    else). Its text has no numbers."""
    fields = sorted(_no_data_fields(evidence))
    if not fields or not (text or "").strip():
        return text
    lead = _lead(text)
    if "0" not in _lead_numbers(text) or _NO_DATA_SAID_RE.search(lead):
        return text
    parts = {p for f in fields for p in f.split(".")}
    if any(tok not in parts for tok in _CUSTOM_FIELD_RE.findall(lead)):
        return text
    shown = fields[:3]
    names = ", ".join(f"`{f}`" for f in shown)
    line = _fill(RECORDS_NO_DATA_LINE if len(shown) == 1 else RECORDS_NO_DATA_LINE_SEVERAL, FIELDS=names)
    rest = text.lstrip("\n")
    if _lead_numbers(text) == {"0"}:
        rest = _without_lead(rest, lead)
    return line + ("\n\n" + rest if rest.strip() else "")


def _without_lead(text: str, lead: str) -> str:
    """``text`` without its lead sentence ``lead`` (:func:`_lead`): the first occurrence is cut from its line, and a
    line left empty (or only a list marker and emphasis) is removed."""
    if not lead:
        return text
    lines = text.split("\n")
    for i, ln in enumerate(lines):
        k = ln.find(lead)
        if k < 0:
            continue
        rest = (ln[:k] + ln[k + len(lead):]).strip()
        if re.fullmatch(r"(?:[-*+•]|\d+[.)])?[\s*_]*", rest):
            del lines[i]
        else:
            lines[i] = (ln[:k] + ln[k + len(lead):].lstrip()).rstrip()
        break
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip("\n")


def _person_counts_on(obj: str, entry: dict, evidence: list[QueryEvidence]) -> set[str]:
    """The person's counts on ``obj`` that the hints or the results hold: the reference counts of the
    ``question_objects`` entry, lookup_check ``elsewhere`` entries and find_references references on that object
    (above 0)."""
    vals: list[Any] = []
    for o in entry.get("objects") or []:
        if isinstance(o, dict) and str(o.get("object")) == obj:
            vals += [r.get("records") for r in o.get("refs") or [] if isinstance(r, dict)]
    for lc in lookup_checks(evidence):
        vals += [x.get("records") for x in lc.get("elsewhere") or [] if isinstance(x, dict) and str(x.get("object")) == obj]
    for e in evidence:
        if e.ok and e.kind == "data" and e.tool in PERSON_TOOLS and isinstance(e.model_result, dict):
            for r in e.model_result.get("references") or []:
                if isinstance(r, dict) and str(r.get("object")) == obj:
                    vals.append(r.get("records"))
                    by = r.get("by")
                    vals += list(by.values()) if isinstance(by, dict) else []
    return {n for n in (_num(v) for v in vals if _count(v) > 0) if n}


def _left_out_backed(body: str, obj: str, entry: dict, evidence: list[QueryEvidence]) -> bool:
    """The draft names ``obj``, an object it did not count, and the results back leaving it out: a call on it failed
    because the object has no table (E1b) or because the person's name was not found through it (E4h, E15a with the
    entry's name in ``not_found``; a field that is not synced, E2b, says nothing about the person's records there), a
    counting result found a lookup of it with no data (``no_data_fields``), or a clause that names it gives one of the
    person's counts on it (:func:`_person_counts_on`). A reason the draft only writes ("its type field is almost
    empty") is none."""
    if not _names(body, obj):
        return False
    person = _norm_text(entry.get("name"))
    for e in evidence:
        if e.ok or str(e.object) != obj:
            continue
        code = _error_code(e)
        if code == "E1b" or (code in NAME_NOT_FOUND_CODES and person and any(
                isinstance(x, dict) and _norm_text(x.get("name")) == person for x in getattr(e, "not_found", None) or [])):
            return True
    if any(f.split(".")[0] == obj for f in _no_data_fields(evidence)):
        return True
    counts = _person_counts_on(obj, entry, evidence)
    return any(_names(clause, obj) and set(numbers_in(_DATE_TIME_RE.sub(" ", clause))) & counts
               for clause in _CLAUSE_SPLIT_RE.split(body))


def noun_nudges(draft: str, evidence: list[QueryEvidence], objects: list[dict] | None) -> list[str]:
    """``RECORDS_BOTH_OBJECTS_MESSAGE`` per entry of ``RecordTools.question_objects()`` (the question's noun has no
    qualifier and no role word and fits the label of several objects that reference the named person) when the
    successful counting queries ran on some of those objects but not all of them, and the draft does not leave out
    every missing object with a reason the results back (:func:`_left_out_backed`: naming the object with a reason of
    its own, such as "its type field is almost empty", is not enough)."""
    counted = {str(e.object) for e in evidence if e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS and e.object}
    body = _body(draft or "")
    out: list[str] = []
    for x in objects or []:
        if not isinstance(x, dict):
            continue
        objs = [o for o in x.get("objects") or [] if isinstance(o, dict) and o.get("object")]
        done = [str(o["object"]) for o in objs if str(o["object"]) in counted]
        missing = [str(o["object"]) for o in objs if str(o["object"]) not in counted]
        if not done or not missing or all(_left_out_backed(body, m, x, evidence) for m in missing):
            continue

        def refs(o: dict) -> str:
            looks = ", ".join(f"{r.get('field')} ({r.get('role')})" for r in o.get("refs") or [] if isinstance(r, dict))
            return f"{o['object']} ({o.get('label') or o['object']}" + (f"; {looks})" if looks else ")")

        out.append(RECORDS_BOTH_OBJECTS_MESSAGE.replace("<<WORD>>", str(x.get("word") or ""))
                   .replace("<<NAME>>", str(x.get("name") or "")).replace("<<OBJECTS>>", "; ".join(refs(o) for o in objs))
                   .replace("<<DONE>>", ", ".join(done)).replace("<<MISSING>>", ", ".join(missing)))
    return out[:1]


#: Error codes of a failed call whose name (a person or value filter, find_references) is not stored.
NAME_NOT_FOUND_CODES = frozenset({"E4h", "E15a"})
#: Data calls that count none of the asked records: value lists, the sync status, name searches.
_NO_COUNT_TOOLS = frozenset({"search_records", "get_field_values", "get_sync_status"})


def _name_missing(evidence: list[QueryEvidence]) -> bool:
    """A data call failed because a name it filtered on is not stored (:data:`NAME_NOT_FOUND_CODES`)."""
    return any(e.kind == "data" and not e.ok and _error_code(e) in NAME_NOT_FOUND_CODES for e in evidence)


def _close_names(evidence: list[QueryEvidence]) -> set[str]:
    """The stored names the not-found errors (E4h, E15a) list instead of the asked one (``not_found`` "close"),
    normalised (:func:`_norm_text`)."""
    return {_norm_text(c) for e in evidence if not e.ok and _error_code(e) in NAME_NOT_FOUND_CODES
            for x in getattr(e, "not_found", None) or [] if isinstance(x, dict)
            for c in x.get("close") or [] if str(c or "").strip()}


def _data_found(e: QueryEvidence, name_missing: bool = False, close: set[str] | None = None,
                people_data: bool = True) -> bool:
    """A successful data call that a count can rest on. search_records is none: after a not-found error it either
    confirms that the name is not there or lists other records that only share a word of it. After a name was not
    found (``name_missing``) the sync status is none either; a value list counts only when it holds a name the error
    listed instead (``close``, :func:`_close_names`: the reply may answer for the corrected name); and a list_records
    call counts only when one of its name filters matched stored records (``names_matched``: an exact name, an Id or
    a contains filter that matched one name; or a ``lookup_check``). An unfiltered list, a list filtered on other
    fields only and a contains filter that matched several names (listing the employees to look for the name, or the
    records without the person) count nothing about the asked name. Without ``people_data`` (the question names only
    people no stored person has, RECORDS_SPEC §22.8) a person-tool result (other people who share a word of the name)
    counts nothing either."""
    if not (e.ok and e.kind == "data") or e.tool == "search_records":
        return False
    if not people_data and e.tool in PERSON_TOOLS:
        return False
    if name_missing:
        res = e.model_result if isinstance(e.model_result, dict) else {}
        if e.tool == "get_field_values":
            vals = res.get("values") or []
            return bool(close) and any(isinstance(v, dict) and _norm_text(v.get("value")) in close for v in vals)
        if e.tool in _NO_COUNT_TOOLS:
            return False
        if e.tool == "list_records":
            matched = [x for x in getattr(e, "names_matched", None) or [] if isinstance(x, dict)]
            return bool(res.get("lookup_check")) or any(x.get("match") != "contains" or len(x.get("names") or []) == 1
                                                        for x in matched)
    return True


def _no_data_but_not_found(evidence: list[QueryEvidence], people_data: bool = True) -> bool:
    """No data call found anything (:func:`_data_found`), and one failed because a name, field or object is not
    there (NOT_FOUND_CODES), or the question names only names that are not stored (``people_data`` False: the
    triage found no record under them, RECORDS_SPEC §22.15), so the same rules apply as after a name was not found."""
    missing = _name_missing(evidence) or not people_data
    close = _close_names(evidence) if missing else set()
    return (not any(_data_found(e, missing, close, people_data) for e in evidence)
            and (not people_data or any(e.kind == "data" and _not_available(e) for e in evidence)))


def not_found_answer(evidence: list[QueryEvidence]) -> str:
    """The answer code builds when no data call found anything and a name the calls filtered on is not stored: one
    sentence per name (``QueryEvidence.not_found`` of the failed E4h / E15a calls), "No Employee (Recruiter__c) record
    is named '<name>' in the records (checked through `<Obj>.<Field>`)." plus " Closest stored names: a, b, c." when
    the error listed some, and " There is no count for this name." only when the failed call was a counting tool.
    Empty when no failed call logged a name. It holds no number of its own."""
    seen: set[str] = set()
    out: list[str] = []
    for e in evidence:
        if e.ok or _error_code(e) not in NAME_NOT_FOUND_CODES:
            continue
        for x in getattr(e, "not_found", None) or []:
            if not isinstance(x, dict) or not str(x.get("name") or "").strip():
                continue
            name = " ".join(str(x["name"]).split())
            if name.lower() in seen:
                continue
            seen.add(name.lower())
            what = "name contains" if x.get("match") == "contains" else "is named"
            where = (f" (checked through `{x['object']}.{x['field']}`)" if x.get("object") and x.get("field") else "")
            text = f"No {x.get('target') or 'person'} record {what} '{name}' in the records{where}."
            close = [" ".join(str(c).split()) for c in x.get("close") or [] if str(c or "").strip()]
            if close:
                text += " Closest stored names: " + ", ".join(list(dict.fromkeys(close))[:5]) + "."
            if e.tool in _COUNTING_TOOLS:
                text += " There is no count for this name."
            out.append(text)
    return " ".join(out)


def not_found_reply(reply: str, evidence: list[QueryEvidence], question: str, status: RecordsStatus) -> bool:
    """True when ``reply`` may stand as a "no such person / not recorded" answer although no data call succeeded.

    At least one failed data call must carry an ``error_code`` in :data:`NOT_FOUND_CODES`, and every number
    of the reply outside its Source lines, dates and times (:func:`_count_numbers`) must be a number of the
    question (so the "Source: ..., synced <time>" line passes, but a count of 0 or more does not: stricter than
    :func:`audit_numbers`). The status line gives no numbers: its copy time, age and object count are no counts.
    """
    if not reply or _is_no_records(reply):
        return False
    if not any(e.kind == "data" and _not_available(e) for e in evidence):
        return False
    allowed = set(numbers_in(question))
    return all(n in allowed for n in _count_numbers(re.sub(r"\(\s*\d+(?:\.\d+)?\s*h\s+ago\s*\)", " ", reply)))


def _named_fields(text: str) -> set[str]:
    names = set(_CUSTOM_FIELD_RE.findall(text or ""))
    names.update(f for f in _SYSTEM_LOOKUPS if _names(text, f))
    return names


def field_checks(knowledge_answer: str, evidence: list[QueryEvidence]) -> list[dict]:
    """FIELD CHECK findings (RECORDS_SPEC §21.10), at most 3, each a dict with ``line`` (the text for the
    merge) and ``object``, ``field``, ``role``, ``records``, ``other_field``, ``other_role``,
    ``other_records`` and ``query_id``.

    Per lookup check of a person filter (the last per (object, field)), one finding for each ``same_target`` other
    that the knowledge answer names, whose role fits the question's verb while the used lookup's does not, and that
    no later counting query used. Whether the knowledge answer also names the used lookup does not matter.
    """
    out: list[dict] = []
    for x in unsynced_checks(evidence)[:_MAX_FIELD_CHECKS]:
        line = _fill(RECORDS_FIELD_CHECK_UNSYNCED, WORD=x.get('fits'), OBJECT=x.get('object'), FIELD=x.get('field'),
                     ROLE=_role(x), PROXY=_proxy_text(x), QUERY_ID=x.get('query_id'))
        out.append({"line": line, "kind": "unsynced", "object": x.get("object"), "field": _proxy_text(x),
                    "role": None, "records": None, "other_field": x.get("field"), "other_role": x.get("role"),
                    "other_records": None, "query_id": x.get("query_id")})
    named = _named_fields(knowledge_answer)
    if not named:
        return out
    for lc in _last_per_field(_person_checks(evidence)):
        qid, obj = lc.get("query_id"), lc.get("object")
        for o in _same_target(lc):
            if len(out) >= _MAX_FIELD_CHECKS:
                return out
            g = o.get("field")
            if g not in named or not _role_says_other(lc, o) or _used_later(evidence, qid, obj, g):
                continue
            line = _fill(RECORDS_FIELD_CHECK_LOOKUP, OBJECT=obj, OTHER_FIELD=g, OTHER_ROLE=_role(o),
                         FIELD=lc.get('field'), ROLE=_role(lc), RECORDS=lc.get('records'),
                         OTHER_RECORDS=o.get('records'), QUERY_ID=qid)
            out.append({"line": line, "kind": "lookup", "object": obj, "field": lc.get("field"), "role": lc.get("role"),
                        "records": lc.get("records"), "other_field": g, "other_role": o.get("role"),
                        "other_records": o.get("records"), "query_id": qid})
    return out


def field_check_lines(knowledge_answer: str, evidence: list[QueryEvidence]) -> list[str]:
    """The FIELD CHECK lines passed to the merge (:func:`field_checks`), at most 3."""
    return [c["line"] for c in field_checks(knowledge_answer, evidence)]


def _reported_through_other(text: str, lc: dict, objects: set[str], text_numbers: set[str]) -> bool:
    """True when ``text`` reports the count of another lookup of the check ``lc`` (an ``others`` entry of the same
    person with records, named or quoted next to its name or role) and not the check's own count (above 0: its number
    is not in the text; 0: the answer's lead gives that other lookup's count and no clause gives 0 with the check's
    lookup or role): the answer's number came from the lookup check, so the check's own lookup (often an abandoned
    first query) is not the lookup the answer used. A 0 check whose lead gives no other lookup's count (a leading 0,
    the other count only as context) is the check's own report, so the footer keeps it."""
    alt = [o for o in lc.get("others") or [] if isinstance(o, dict) and _count(o.get("records")) > 0
           and o.get("same_person", True) and (_names_field(text, o.get("field"), o.get("role"), objects)
                                               or _quotes(text, o.get("records"), o.get("field"), o.get("role")))]
    if not alt:
        return False
    if _count(lc.get("records")) > 0:
        return _num(lc.get("records")) not in text_numbers
    lead = _lead_numbers(text)
    if not any(_num(o.get("records")) in lead for o in alt):
        return False
    return not _quotes(text, lc.get("records"), lc.get("field"), lc.get("role"))


def with_lookup_footer(text: str, evidence: list[QueryEvidence]) -> str:
    """``text`` with a code footer naming the lookup its person count used, when the text does not.

    The checks used (person filters only), per base object: the checks whose lookup the text names; when it names
    none of them, the checks above 0 whose role the text names, else the last check of that object, but only when
    the text reports that object (it names the object, quotes a count above 0 of one of its checks, or the checks
    cover one object only) and not through one of that check's other lookups (:func:`_reported_through_other`: the
    text names the other lookup whose count it gives): an abandoned first query, or a query on an object the answer
    does not report, is not listed, and a number that happens to equal another check's count never selects it. "Lookup used: ..." lists the
    used checks the text does not name. "Same person through other lookups of <Obj> ..." is added for a used check
    that counted 0 and has ``same_target`` or ``linked`` others with records that the text does not name (at most 4),
    unless another used check of the same object and person counted above 0; with several people on that object it
    says for whom ("... of <Obj> for '<name>' ..."). "... fits ..." for a used check above 0 whose role does not fit
    the question's verb while an unnamed ``same_target`` other with records does. The footer goes before a last line
    that starts with "Source:" (else at the end). Its numbers come from the record facts.
    """
    checks = _person_checks(evidence)
    unsynced = [x for x in unsynced_checks(evidence) if not _names(text, x.get("field"))]
    if (not checks and not unsynced) or not (text or "").strip():
        return text
    objects = _object_names(checks)
    text_numbers = set(numbers_in(_DATE_TIME_RE.sub(" ", _body(text))))
    by_obj: dict[Any, list[dict]] = {}
    for lc in checks:
        by_obj.setdefault(lc.get("object"), []).append(lc)
    used: list[dict] = []
    for obj, lst in by_obj.items():
        named = [lc for lc in lst if _names_field(text, lc.get("field"), lc.get("role"), objects)]
        if not named:   # no lookup named: the checks above 0 whose role the text names ("as interviewer"), else the last
            named = [lc for lc in _last_per_field(lst) if _count(lc.get("records")) > 0 and _says_role(text, lc.get("role"))]
        if named:
            used += _last_per_field(named)
        elif (len(by_obj) == 1 or _names(text, str(obj))
              or any(_count(lc.get("records")) > 0 and _num(lc.get("records")) in text_numbers for lc in lst)):
            if not _reported_through_other(text, lst[-1], objects, text_numbers):
                used.append(lst[-1])

    def unnamed(o: dict) -> bool:
        return not _names_field(text, o.get("field"), o.get("role"), objects)

    parts: list[str] = []
    missing = [lc for lc in used if unnamed(lc)]
    if missing:
        parts.append(_fill(RECORDS_FOOTER_LOOKUP_USED, LOOKUPS="; ".join(
            f"`{lc.get('object')}.{lc.get('field')}`{_role(lc)}" for lc in missing)))
    people: dict[Any, set[Any]] = {}
    for lc in used:
        people.setdefault(lc.get("object"), set()).add(_person_key(lc))
    counted = {(lc.get("object"), _person_key(lc)) for lc in used if _count(lc.get("records")) > 0}
    for lc in used:
        if lc.get("records") == 0:
            if (lc.get("object"), _person_key(lc)) in counted:
                continue   # the text also reports a count above 0 for this person here: the 0 is one role among others
            others = [o for o in _same_target(lc) + _linked_others(lc) if _count(o.get("records")) > 0 and unnamed(o)]
            if others:
                names = _filter_names(lc)
                listed = ", ".join(f"`{o.get('field')}`{_role_via(o)} {o.get('records')}"
                                   for o in others[:_MAX_FOOTER_OTHERS])
                if names and len(people.get(lc.get("object"), ())) > 1:   # several people on the object: for whom
                    parts.append(_fill(RECORDS_FOOTER_SAME_PERSON_FOR, OBJECT=lc.get('object'),
                                       NAMES=", ".join(f"'{n}'" for n in names), OTHERS=listed))
                else:
                    parts.append(_fill(RECORDS_FOOTER_SAME_PERSON, OBJECT=lc.get('object'), OTHERS=listed))
        elif _count(lc.get("records")) > 0:
            better = [o for o in _same_target(lc) if _count(o.get("records")) > 0 and _role_says_other(lc, o)
                      and unnamed(o)]
            if better:
                parts.append(_fill(RECORDS_FOOTER_WORD_FITS, WORD=better[0].get('fits'), LOOKUPS=", ".join(
                    f"`{lc.get('object')}.{o.get('field')}`{_role(o)}: {o.get('records')}"
                    for o in better[:_MAX_FOOTER_OTHERS])))
    for x in unsynced[:2]:
        parts.append(_fill(RECORDS_FOOTER_NOT_SYNCED, OBJECT=x.get('object'), FIELD=x.get('field'), ROLE=_role(x),
                           WORD=x.get('fits'), PROXY=_proxy_text(x)))
    if not parts:
        return text
    footer = " ".join(parts)
    lines = text.rstrip().split("\n")
    last_line = lines[-1].strip().lstrip("*_> ").lower()
    if last_line.startswith("source:"):
        return "\n".join(lines[:-1] + [footer, lines[-1]])
    return "\n".join(lines + [footer])


def with_source_line(text: str, evidence: list[QueryEvidence], status: RecordsStatus | None) -> str:
    """``text`` with a last line "Source: <origin>, synced <time>." when no line of it is a Source line (in any bullet
    or bold form). The time is the sync time of the object of the last successful data query (as the results' source
    gives it), else the newest sync of the status. Empty text is returned unchanged."""
    if not (text or "").strip() or any(_is_source_line(ln) for ln in text.split("\n")):
        return text
    origin = (getattr(status, "origin", None) or "copy of unknown origin").lower()
    when = None
    for e in reversed(evidence or []):
        synced = getattr(e, "synced_at", None) or {}
        if e.ok and e.kind == "data" and e.object and synced.get(e.object):
            when = synced[e.object]
            break
    when = when or getattr(status, "newest_sync_utc", None) or "unknown"
    return text.rstrip() + "\n" + _fill(RECORDS_SOURCE_LINE, ORIGIN=origin, WHEN=when)


# -------------------------------------------------------- evaluation fix 2 (2026-10-22): an answer that says nothing
#: The text a placeholder that ends a sentence of a code line holds (a bare "." follows it, as in "Lookup used:
#: <<LOOKUPS>>."): anything within one sentence (a dot only inside a word, as in an API name
#: "Interview__c.Recruiter__c"; review of 2026-10-22: ".*?" let "Lookup used: <a sentence>. <another>." match).
_PLACEHOLDER = r"(?:[^.]|\.(?=\S))*?"


def _template_pattern(template: str) -> str:
    """A regex of the lines a prompt text gives when it is filled: its fixed text, each ``<<NAME>>`` any text within
    one sentence (:data:`_PLACEHOLDER`) when a bare "." follows it, else any text (``.*?``: the fixed text after it
    ends it, so a stored name with an initial, "E. Two", still fills the other-people line; review of 2026-10-23)."""
    parts = re.split(r"<<[A-Z][A-Z0-9_]*>>", template)
    return re.escape(parts[0]) + "".join((_PLACEHOLDER if part.strip() == "." else ".*?") + re.escape(part)
                                         for part in parts[1:])


#: One line code writes around a record or merged answer: the lookup footer (its parts joined by spaces on one line,
#: :func:`with_lookup_footer`), a status line (:func:`status_lines`) or the other-people line (RecordSession). With the
#: Source lines these lines are no answer of their own (:func:`answer_text`).
_CODE_PART = "(?:" + "|".join(_template_pattern(t) for t in (
    RECORDS_FOOTER_LOOKUP_USED, RECORDS_FOOTER_SAME_PERSON, RECORDS_FOOTER_SAME_PERSON_FOR, RECORDS_FOOTER_WORD_FITS,
    RECORDS_FOOTER_NOT_SYNCED, RECORDS_STATUS_LINE, RECORDS_OTHER_PEOPLE_LINE)) + ")"
_CODE_LINE_RE = re.compile(_CODE_PART + r"(?: " + _CODE_PART + r")*", re.DOTALL)


#: Words a label or heading line needs to say something when no other line of the answer is left (review of
#: 2026-10-22: "### No interviews are recorded for Delta Person" is an answer, "### Totals" or "**Summary:**" is not).
_LABEL_ANSWER_WORDS = 3


def answer_text(text: str | None) -> str:
    """The lines of an answer that say something (evaluation fix 2, 2026-10-22): ``text`` without its Source lines,
    the lines code writes around an answer (:data:`_CODE_LINE_RE`: the lookup footer, the status lines, the
    other-people line), label or heading lines without a number (:func:`_label_only`: "**Summary:**", "### Totals")
    and lines with no letter or digit; when nothing else is left, the label or heading lines of at least
    :data:`_LABEL_ANSWER_WORDS` words (a one-line answer written as a heading or a bold line ending in a colon, review
    of 2026-10-22). Empty: the answer says nothing and counts as no answer, so the record session, the merge and the
    pipeline's choice of the answer never give it."""
    lines = [ln for ln in (text or "").split("\n")
             if ln.strip() and not _is_source_line(ln) and re.search(r"[A-Za-z0-9]", ln)
             and not _CODE_LINE_RE.fullmatch(ln.strip())]
    kept = [ln for ln in lines if not _label_only(ln)]
    if not kept:
        kept = [ln for ln in lines if len(re.findall(r"[A-Za-z][A-Za-z0-9'’-]*", ln)) >= _LABEL_ANSWER_WORDS]
    return "\n".join(kept).strip()


# ------------------------------------------------------------------- Group B (docs/people.md): items 10 and 11
# Relation wording and direction of the person lookups an about_people result shows (item 10), and the status line
# under a final answer whose question's verb can mean finished records (item 11). Code reads only structured keys of
# the results (profiles, matched_records, links, references, breakdown, groups, rows, lookup_check) and the catalog
# through a callable the record session builds (``directions``, ``status_of``); the words come from the lookups'
# labels, their knowledge texts (direction_semantics) and the existing cue stems, never from a list of phrases.

#: The record session's lookup texts: ``directions(object, field)`` -> ``{"label", "target", "person",
#: "from_source", "from_target"}`` or None (RecordSession.lookup_directions).
_MAX_RELATION_NUDGES = 1
_MAX_STATUS_LINES = 2
_MAX_ZERO_VALUES = 8
_CLAUSE_TEXT = 200
#: Clauses of a draft for the relation checks: sentence ends and ";" (lines first); a short bullet label before ": "
#: ("**Same Manager:** Both report to ..." -> the label and the sentence after it) is a clause of its own.
_RELATION_SPLIT_RE = re.compile(r"(?<=[.!?;])\s+")
_LABEL_HEAD_RE = re.compile(r"^([^:]{1,40}):\s+(\S.*)$")
_MARKDOWN_RE = re.compile(r"[*`#>]+|(?<![A-Za-z0-9_])_+|_+(?![A-Za-z0-9_])")
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:['’]s)?")


def _stem(word: Any) -> str:
    """A word's stem for the relation and cue checks: lower case, a possessive and one plural or third-person "s" cut,
    then one of -ing, -ed, -er when 4 or more letters stay, then a final "e" ("reporting", "reports" -> "report";
    "manager", "managed", "manages", "manage" -> "manag"; "interviewer", "interviewed" -> "interview")."""
    w = str(word or "").lower().replace("’", "'")
    if w.endswith("'s"):
        w = w[:-2]
    if w.endswith("s") and not w.endswith("ss") and len(w) > 4:
        w = w[:-1]
    for suf in ("ing", "ed", "er"):
        if w.endswith(suf) and len(w) - len(suf) >= 4:
            w = w[:-len(suf)]
            break
    while w.endswith("e") and len(w) > 4:
        w = w[:-1]
    return w


def _label_stems(text: Any) -> set[str]:
    """The stems (4 or more letters) of a lookup label's words that are no function words: "Reporting to" ->
    {"report"}, "Team Lead" -> {"team", "lead"}."""
    from sf_assistant.engine.catalog import _ROLE_STOPWORDS   # the catalog's function words (role words)

    return {_stem(w) for w in re.findall(r"[a-z]+", str(text or "").lower())
            if w not in _ROLE_STOPWORDS and len(_stem(w)) >= 4}


def _plain_name(label: Any) -> str:
    """A people item's label without its " (#k)" number (the stored name the text writes)."""
    return re.sub(r"\s*\(#\d+\)\s*$", "", " ".join(str(label or "").split()))


def _people_results(evidence: list[QueryEvidence]) -> list[dict]:
    """The successful about_people / find_references results, in call order."""
    return [e.model_result for e in evidence if e.ok and e.kind == "data" and e.tool in PERSON_TOOLS
            and isinstance(e.model_result, dict)]


def _main_objects(res: dict) -> dict[str, str]:
    """``{person ref: object of the person's main record}``: the first matched record of each person (about_people
    lists the main record first; its profile is the people item's)."""
    out: dict[str, str] = {}
    for m in res.get("matched_records") or []:
        if isinstance(m, dict) and m.get("person") and m.get("object") and m["person"] not in out:
            out[str(m["person"])] = str(m["object"])
    return out


def _call_directions(directions: Any, obj: Any, field: Any) -> dict | None:
    if directions is None or not obj or not field:
        return None
    try:
        info = directions(str(obj), str(field))
    except Exception:  # noqa: BLE001 - optional
        return None
    return info if isinstance(info, dict) else None


def _relation_facts(evidence: list[QueryEvidence], directions: Any) -> list[dict]:
    """Who holds whom through which person lookup, from the about_people results: ``{"holder", "object", "field",
    "label", "target", "value", "words"}`` per filled person lookup of a profile (the people item's, of its main record,
    and the matched records' own), per "shared parent" link through the same person lookup on both own records, and
    per "own record" link. ``holder`` is the person whose record holds the lookup, ``value`` the person it names."""
    out: list[dict] = []
    seen: set[tuple] = set()

    def add(holder: Any, obj: Any, field: Any, value: Any) -> None:
        info = _call_directions(directions, obj, field)
        if not info or not info.get("person") or not isinstance(value, str) or not value.strip():
            return
        key = (_norm_text(holder), str(obj), str(field), _norm_text(value))
        if key in seen:
            return
        seen.add(key)
        out.append({"holder": _plain_name(holder), "object": str(obj), "field": str(field),
                    "label": str(info.get("label") or field), "target": info.get("target"),
                    "value": " ".join(value.split()), "words": _label_stems(info.get("label"))})

    for res in _people_results(evidence):
        label_of = {str(p.get("ref")): p.get("label") for p in res.get("people") or [] if isinstance(p, dict)}
        main = _main_objects(res)
        for p in res.get("people") or []:
            if isinstance(p, dict) and isinstance(p.get("profile"), dict):
                for k, v in p["profile"].items():
                    add(p.get("label"), main.get(str(p.get("ref"))), str(k).split(" ", 1)[0], v)
        for m in res.get("matched_records") or []:
            if isinstance(m, dict) and isinstance(m.get("profile"), dict):
                for k, v in m["profile"].items():
                    add(label_of.get(str(m.get("person"))), m.get("object"), str(k).split(" ", 1)[0], v)
        for lk in res.get("links") or []:
            if not isinstance(lk, dict):
                continue
            path = str(lk.get("path") or "")
            between = [label_of.get(str(r)) for r in lk.get("between") or []]
            m = re.match(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+) \(the same ", path)
            if lk.get("how") == "shared parent" and m and lk.get("examples"):
                for who in between:
                    add(who, m.group(1), m.group(2), str(lk["examples"][0]))
            m = re.match(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+) \(own record\)", path)
            pair = [str(r) for r in lk.get("between") or []]
            if lk.get("how") == "own record" and m and str(lk.get("holder") or "") in pair and len(pair) == 2:
                other = next(r for r in pair if r != str(lk["holder"]))   # the structured key (about_people C9)
                add(label_of.get(str(lk["holder"])), m.group(1), m.group(2), _plain_name(label_of.get(other)))
                continue
            t = re.match(r"^(P\d+) \(.*?\)'s .+? record has (P\d+) \(", str(lk.get("text") or ""))
            if lk.get("how") == "own record" and m and t:
                add(label_of.get(t.group(1)), m.group(1), m.group(2), _plain_name(label_of.get(t.group(2))))
    return out


def _clauses(text: str) -> list[str]:
    """The clauses of ``text`` for the relation checks (Source lines left out; :data:`_RELATION_SPLIT_RE`)."""
    out = []
    for ln in _body_text(text or "").split("\n"):
        for c in _RELATION_SPLIT_RE.split(ln):
            c = " ".join(_MARKDOWN_RE.sub(" ", c or "").split()).strip(" -")   # API names keep their "_"
            c = re.sub(r"\s+([).,;:!?])", r"\1", re.sub(r"\(\s+", "(", c))
            m = _LABEL_HEAD_RE.match(c)
            out += [x.strip() for x in (m.groups() if m else (c,)) if x.strip()]
    return out


def _holds_text(name: str, facts: list[dict]) -> str:
    """"'<name>' is `<F>` (<label>) of <holder>, <holder>; `<G>` (<label>) of <holder>" for the facts that hold
    ``name``."""
    by: dict[tuple[str, str], list[str]] = {}
    for f in facts:
        by.setdefault((f["field"], f["label"]), [])
        if f["holder"] and f["holder"] not in by[(f["field"], f["label"])]:
            by[(f["field"], f["label"])].append(f["holder"])
    parts = [f"`{fld}` ({lab})" + (" of " + " and ".join(hs[:3]) if hs else "") for (fld, lab), hs in by.items()]
    return f"'{name}' is " + "; ".join(parts[:3])


def _word_fields_text(lookups: list[tuple[str, str]], facts: list[dict]) -> str:
    """"`<F>` (<label>: '<value>' for <holder>, ...)" for these lookups, from the facts."""
    parts = []
    for obj, fld in lookups:
        fs = [f for f in facts if f["object"] == obj and f["field"] == fld]
        if not fs:
            continue
        vals = ", ".join(f"'{f['value']}' for {f['holder']}" if f["holder"] else f"'{f['value']}'" for f in fs[:3])
        parts.append(f"`{fld}` ({fs[0]['label']}: {vals})")
    return "; ".join(parts[:2])


def relation_wording_nudges(draft: str, evidence: list[QueryEvidence], directions: Any = None) -> list[str]:
    """``RECORDS_RELATION_WORD_MESSAGE`` for the first clause of the draft that ties a relation word to a stored person
    whom no lookup with that word holds (Group B item 10a, docs/people.md).

    The facts are the person lookups of the about_people results (:func:`_relation_facts`: profiles and links; which
    profile keys are person lookups and their labels come from ``directions``, the record session's catalog view).
    The relation words of a lookup are its label's words (:func:`_label_stems`). Per clause (:func:`_clauses`) and per
    person ``Y`` the clause names who is a value of those lookups: the lookups that hold ``Y`` (for a person the clause
    also names, only theirs when there are such), and all the label words of another lookup of the same object and
    target (a sibling: `Manager__c` next to the reporting line; "team lead", never "team" alone) while the clause has
    no word of the lookups that hold ``Y`` and no other person of the clause is held through that sibling's relation:
    the clause calls the relation by the wrong lookup ("both report to <the manager>"). API names in the clause are
    no relation words. Without ``directions`` or about_people facts: []. At most one message."""
    facts = _relation_facts(evidence, directions)
    if not facts or not (draft or "").strip():
        return []
    people = {_norm_text(f["holder"]) for f in facts if f["holder"]}
    values = {_norm_text(f["value"]): f["value"] for f in facts}
    sibling_words: dict[tuple[str, str], set[str]] = {}
    for f in facts:
        sibling_words.setdefault((f["object"], f["field"]), set()).update(f["words"])
    group = {(f["object"], f["field"]): (f["object"], f["target"]) for f in facts}
    out: list[str] = []
    for clause in _clauses(draft):
        norm = _norm_text(clause)
        named = [v for k, v in values.items() if _has_name(norm, v)]
        if not named:
            continue
        persons = {p for p in people if p and _has_name(norm, p)}
        # API names ("`Manager__c`") name a field, not a relation: only the clause's own words count
        toks = [t for t in _TOKEN_RE.findall(_CUSTOM_FIELD_RE.sub(" ", clause).lower())]
        name_toks = {t for n in named + [*persons] for t in _TOKEN_RE.findall(n.lower())}
        words = {_stem(t): t for t in toks if t not in name_toks}
        holding_of: dict[str, list[dict]] = {}
        for y in named:
            ny = _norm_text(y)
            held = [f for f in facts if _norm_text(f["value"]) == ny]
            mine = [f for f in held if _norm_text(f["holder"]) in persons - {ny}]
            holding_of[y] = mine or held
        held_words = {y: {w for f in fs for w in f["words"]} for y, fs in holding_of.items()}
        for y, fs in holding_of.items():
            if not fs or held_words[y] & set(words):
                continue
            groups = {group[(f["object"], f["field"])] for f in fs}
            own = {(f["object"], f["field"]) for f in fs}
            bad: dict[frozenset, list[tuple[str, str]]] = {}
            for lk, ws in sibling_words.items():
                # a sibling's relation is named only by all of its label words ("team lead", not "team" alone)
                if lk in own or group.get(lk) not in groups or not ws or not ws <= set(words):
                    continue
                if any(ws <= held_words[o] for o in held_words if o != y):
                    continue   # another person of the clause is held through that relation
                bad.setdefault(frozenset(ws), []).append(lk)
            if not bad:
                continue
            ws, lks = next(iter(bad.items()))
            said = " ".join(t for t in dict.fromkeys(words[w] for w in sorted(ws, key=lambda x: toks.index(words[x]))))
            near = [f for f in fs if group[(f["object"], f["field"])] in {group[lk] for lk in lks}] or fs
            out.append(_fill(RECORDS_RELATION_WORD_MESSAGE, CLAUSE=clause[:_CLAUSE_TEXT], HOLDS=_holds_text(y, near),
                             WORD=said, WORD_FIELDS=_word_fields_text(lks, facts) or ", ".join(f"`{f}`" for _o, f in lks)))
            break
        if len(out) >= _MAX_RELATION_NUDGES:
            break
    return out


def _slot_order(tokens: list[str], start: int, end: int, stems: set[str]) -> str | None:
    """Where a person stands next to a relation word: "after" (the relation word comes first, a possessive "X's
    <word>" = "<word> of X", or a passive the person comes before: "X is managed by Y", "who is X managed by", "by
    whom is X managed" = "manager of X") or "before" (the person comes first, or a passive "... by X": the doer).
    ``tokens[start:end]`` is the person (a name, or "this <noun>" in a direction text). None when no relation word is
    outside it."""
    rel = next((i for i, t in enumerate(tokens) if (i < start or i >= end) and _stem(t) in stems), None)
    if rel is None:
        return None
    last = tokens[end - 1] if end - 1 < len(tokens) else ""
    if last.endswith(("'s", "’s")) and rel >= end:
        return "after"
    if start > 0 and tokens[start - 1] == "by":
        return "before"
    if rel >= end and ((rel + 1 < len(tokens) and tokens[rel + 1] == "by")
                       or any(t == "by" and tokens[i + 1:i + 2] in (["whom"], ["who"]) for i, t in enumerate(tokens))):
        return "after"   # a passive "X is managed by (whom)", "by whom is X managed": X is the one managed
    return "after" if rel < start else "before"


def _text_order(text: str, stems: set[str]) -> str | None:
    """:func:`_slot_order` of a direction text (``from_source``, ``from_target``): the slot is its first "this <noun>"
    (else "the <noun>"), the record the text speaks from ("senior recruiter this employee reports to": "before";
    "employees reporting to this senior recruiter": "after"; "employees managed by this manager": "before")."""
    toks = _TOKEN_RE.findall(str(text or "").lower())
    slot = next((i for i, t in enumerate(toks) if t == "this"), None)
    if slot is None:
        slot = next((i for i, t in enumerate(toks) if t == "the"), None)
    if slot is None or slot + 1 >= len(toks):
        return None
    return _slot_order(toks, slot, slot + 2, stems)


def _direction_side(question: str, name: str, stems: set[str], from_source: str, from_target: str) -> str | None:
    """Which direction of a self-lookup the question asks about the person ``name``: "source" (the person's own
    value: "who does X report to", "who is X's manager"), "target" (the records that point to the person: "who reports
    to X", "who does X manage"), "unclear", or None when no relation word of the lookup (``stems``, its label) is in
    the question. The person's place next to the relation word (:func:`_slot_order`) is compared with the slot's place
    in ``from_source`` and in ``from_target`` (:func:`_text_order`); a word only one of the texts has ("team members")
    that the question uses against that reading makes it unclear."""
    toks = _TOKEN_RE.findall(str(question or "").lower())
    plain = [t[:-2] if t.endswith(("'s", "’s")) else t for t in toks]
    nt = _TOKEN_RE.findall(_plain_name(name).lower())
    if not nt or not stems:
        return None
    start = next((i for i in range(len(plain) - len(nt) + 1) if plain[i:i + len(nt)] == nt), None)
    if start is None:
        return None
    q = _slot_order(toks, start, start + len(nt), stems)
    if q is None:
        return None
    src, tgt = _text_order(from_source, stems), _text_order(from_target, stems)
    if src is None or tgt is None or src == tgt:
        return "unclear"
    side = "source" if q == src else "target"

    def words(text: str) -> set[str]:
        from sf_assistant.engine.catalog import _ROLE_STOPWORDS

        return {_stem(w) for w in re.findall(r"[a-z]+", str(text or "").lower())
                if w not in _ROLE_STOPWORDS and len(_stem(w)) >= 4} - stems
    qw = words(question) - {_stem(t) for t in nt}
    only_src, only_tgt = words(from_source) - words(from_target), words(from_target) - words(from_source)
    if (side == "source" and qw & only_tgt) or (side == "target" and qw & only_src):
        return "unclear"
    rel = next((i for i, t in enumerate(toks) if (i < start or i >= start + len(nt)) and _stem(t) in stems), None)
    nxt = words(plain[rel + 1]) if rel is not None and rel + 1 < len(plain) else set()
    if nxt - {_stem(t) for t in nt} - words(from_source) - words(from_target):
        return "unclear"   # the relation word starts a noun neither text has ("reporting line"): no reading
    return side


def _list_ran(evidence: list[QueryEvidence], obj: str, field: str) -> bool:
    """A successful list_records on ``obj`` used the lookup ``field`` (a filter on it)."""
    return any(e.ok and e.kind == "data" and e.tool == "list_records" and str(e.object) == obj
               and (obj, field) in _fields_used(e) for e in evidence)


def _lookup_views(lookups: Any, obj: Any) -> dict[str, dict]:
    """``{field: view}`` of the record session's person lookups of ``obj`` (``lookups(obj)``:
    :meth:`~sf_assistant.agent.record_session.RecordSession.person_lookups`); {} without the view or on any problem."""
    if lookups is None or not obj:
        return {}
    try:
        got = lookups(str(obj))
    except Exception:  # noqa: BLE001 - optional
        return {}
    return {str(v["field"]): v for v in got or [] if isinstance(v, dict) and v.get("field")}


def _wording(view: dict | None) -> set[str]:
    """The relation words of a person lookup (evaluation fix 5, 2026-10-22): the stems of its label and of its
    knowledge texts ``from_source`` and ``from_target`` (:func:`_label_stems`), without the words of the names of its
    object and its target (the view's ``object_words``: "employee" and "recruiter" name the records, not how they
    relate). "Reporting to" with "senior recruiter this employee reports to" -> {"report", "senior"}."""
    if not view:
        return set()
    stems: set[str] = set()
    for key in ("label", "from_source", "from_target"):
        stems |= _label_stems(view.get(key))
    return stems - {_stem(w) for w in view.get("object_words") or ()}


def _relation_stems(info: dict, view: dict | None, question: str, name: str) -> set[str]:
    """The relation words :func:`_direction_side` reads for a lookup: its label's stems (Group B item 10); when the
    label has no word of the question and the record session's view of the lookup is there, the words of its
    knowledge wording that the question has (:func:`_wording`, evaluation fix 5). The person's name is no relation
    word."""
    label = _label_stems(info.get("label"))
    asked = _label_stems(question) - _label_stems(name)
    if view is None or label & asked:
        return label
    return _wording(view) & asked


def direction_nudges(draft: str, evidence: list[QueryEvidence], question: str, directions: Any = None,
                     lookups: Any = None) -> list[str]:
    """A message about the direction of a self-lookup of the person the question names (Group B item 10b,
    docs/people.md), else [].

    It runs on an about_people result for one asked name whose person has a profile value of a lookup from the
    person's object to the same object, or references through such a lookup (a self-lookup; ``directions`` gives its
    label, target and knowledge texts ``from_source`` / ``from_target``) and whose label word is in the question
    (with no own value the source side needs no message: the value is blank, and the other messages say "blank"). :func:`_direction_side` reads the
    direction from the word order: the records that point to the person ("target") with no list_records on that
    lookup -> ``RECORDS_DIRECTION_MESSAGE`` (both texts, the person's own value, and the list to run); the person's own
    value ("source") that the draft does not name -> ``RECORDS_DIRECTION_SOURCE_MESSAGE``; unclear, and the draft
    names no such lookup -> ``RECORDS_DIRECTION_UNCLEAR_MESSAGE``. Several fitting lookups whose sides differ read as
    unclear. At most one message.

    The relation words of a lookup are its label's words; with ``lookups`` (the record session's view
    :meth:`~sf_assistant.agent.record_session.RecordSession.person_lookups`, evaluation fix 5) a lookup whose label has
    no word of the question is read through the words of its knowledge wording that the question has
    (:func:`_relation_stems`: from_source and from_target too, never the names of the objects)."""
    if directions is None or not (draft or "").strip():
        return []
    text = _norm_text(draft)
    for res in reversed(_people_results(evidence)):
        asked = [str(a) for a in res.get("asked") or [] if str(a or "").strip()]
        if len({_norm_text(a) for a in asked}) != 1:
            continue
        main = _main_objects(res)
        for p in res.get("people") or []:
            if not isinstance(p, dict):
                continue
            obj = main.get(str(p.get("ref")))
            views = _lookup_views(lookups, obj)
            fits: list[tuple[str, dict, str, str]] = []
            prof = p.get("profile") if isinstance(p.get("profile"), dict) else {}
            cands = [(str(k).split(" ", 1)[0], v) for k, v in prof.items() if isinstance(v, str) and v.strip()]
            own = {api for api, _v in cands}
            # a self-lookup that holds the person on other records only (no own value: "who does <a team lead> lead")
            cands += [(str(r.get("field")), "") for r in res.get("references") or [] if isinstance(r, dict)
                      and r.get("person") == p.get("ref") and str(r.get("object")) == obj and _count(r.get("records")) > 0
                      and str(r.get("field")) not in own]
            for api, v in dict.fromkeys(cands):
                info = _call_directions(directions, obj, api)
                if not info or info.get("target") != obj or not info.get("from_source") or not info.get("from_target"):
                    continue
                stems = _relation_stems(info, views.get(api) if views else None, question, asked[0])
                side = _direction_side(question, asked[0], stems, str(info["from_source"]), str(info["from_target"]))
                if side is not None:
                    fits.append((api, info, " ".join(v.split()), side))
            if not fits:
                continue
            if len({f[3] for f in fits}) > 1:
                fits = [(a, i, v, "unclear") for a, i, v, _s in fits]
            name = _plain_name(p.get("label"))
            for api, info, value, side in fits:
                n = sum(_count(r.get("records")) for r in res.get("references") or [] if isinstance(r, dict)
                        and r.get("person") == p.get("ref") and str(r.get("object")) == obj and r.get("field") == api)
                counted = _fill(RECORDS_DIRECTION_COUNTED_NOTE, RECORDS=n, OBJECT=obj, NAME=name, FIELD=api) if n else ""
                vals = dict(FIELD=api, LABEL=info.get("label") or api, NAME=name, OBJECT=obj, VALUE=value or "blank",
                            FROM_SOURCE=info["from_source"], FROM_TARGET=info["from_target"])
                if side == "target" and not _list_ran(evidence, str(obj), api):
                    return [_fill(RECORDS_DIRECTION_MESSAGE, COUNTED=counted, **vals)]
                if side == "source" and value and not _has_name(text, value):
                    return [_fill(RECORDS_DIRECTION_SOURCE_MESSAGE, **vals)]
                if side == "unclear" and not any(_names(draft, a) for a, _i, _v, _s in fits):
                    return [_fill(RECORDS_DIRECTION_UNCLEAR_MESSAGE, COUNTED=counted, **vals)]
            return []
    return []


# ------------------------------------------- evaluation fix 5 (2026-10-22): the question's relation word, no role cue
def _slot_person(names: list[str], people: list[dict] | None, target: str) -> tuple[str, str] | None:
    """``(name, the question's words for it)`` of the one person a lookup slot is about: the person checks' filter
    names, else the triage's people (``people``: :meth:`RecordSession.triage` spots, ``{"words", "recs"}``) with a
    record of ``target``; None for no person or several."""
    spots: list[tuple[set[str], str, str]] = []
    for p in people or []:
        if not isinstance(p, dict):
            continue
        stored = [" ".join(str(r[2]).split()) for r in p.get("recs") or []
                  if isinstance(r, (list, tuple)) and len(r) > 2 and r[0] == target and str(r[2] or "").strip()]
        if stored:
            spots.append(({_norm_text(x) for x in stored}, stored[0], " ".join(str(p.get("words") or "").split())))
    if names:
        if len({_norm_text(n) for n in names}) != 1:
            return None
        key = _norm_text(names[0])
        words = next((w for keys, _n, w in spots if key in keys or _norm_text(w) == key), "")
        return names[0], words or names[0]
    if len(spots) != 1 or len(spots[0][0]) != 1:
        return None
    return spots[0][1], spots[0][2] or spots[0][1]


def _shown_lookups(evidence: list[QueryEvidence], obj: str, name: str) -> set[str]:
    """The lookups of ``obj`` that an about_people result shows for the person ``name`` (the profile keys of their main
    record, their references on ``obj``): :func:`direction_nudges` has read those for the question already."""
    key = _norm_text(name)
    out: set[str] = set()
    for res in _people_results(evidence):
        main = _main_objects(res)
        for p in res.get("people") or []:
            if not isinstance(p, dict) or _norm_text(_plain_name(p.get("label"))) != key:
                continue
            if main.get(str(p.get("ref"))) == obj and isinstance(p.get("profile"), dict):
                out.update(str(k).split(" ", 1)[0] for k in p["profile"])
            out.update(str(r.get("field")) for r in res.get("references") or [] if isinstance(r, dict)
                       and r.get("person") == p.get("ref") and str(r.get("object")) == obj)
    return out


def relation_lookup_nudges(draft: str, evidence: list[QueryEvidence], question: str, lookups: Any = None,
                           people: list[dict] | None = None) -> list[str]:
    """``RECORDS_RELATION_LOOKUP_MESSAGE`` (or its source or unclear form) when a word of the question is in the
    knowledge wording of one person lookup while the answer used another lookup of the same object to the same person
    object (evaluation fix 5, 2026-10-22: "who reports to X" counted through `Manager__c`, while "reports to" is the
    wording of the reporting line `Assigned_Sr_Recruiter__c`; Group B item 10 with no about_people result), else [].

    The lookups the answer used: those of the person checks (a count filtered on a person, with the other lookups of
    the object to the same person object and their counts under the same conditions) and the person lookups the
    successful counting, grouping and listing calls used (``fields_used``: a count grouped by a lookup, with no person
    filter). Per object and person object, about one person (the checks' filter names, else the one person of the
    triage with a record of that object, ``people``): the relation words are the words of the question, the person's
    name left out, that the knowledge wording of a lookup of that slot has (:func:`_wording`: its label,
    ``from_source`` and ``from_target``, never the names of the objects) and not every lookup of the slot has (a word
    all of them have, "mock" of "mock interviews", says what the records are).

    It fires when a used lookup has none of them, another lookup of the slot (with both knowledge texts) has one that
    no used lookup has, no call used that lookup, the draft does not name it, and no about_people result shows it for
    the person (:func:`_shown_lookups`: :func:`direction_nudges` read it and said what was needed). The direction comes
    from the word order (:func:`_direction_side`, on the words of the lookup's label that the question has, else the
    hit words): the records that point to the person, the person's own value (only when every word of the lookup's
    label is in the question: "team" alone is half of "Team Lead"), or either; a lookup to another object can only be
    read the first way (another reading gives no message). Under the same conditions, no message for the records
    that point to the person when the person has 0 through that lookup and through the used ones (the answer's 0
    stands), and none for those or for either reading when the person has 0 through that lookup while a used lookup
    has records (the answer's count stands; review of 2026-10-22: "who is in X's team" answered with 28 reports was
    steered to a team-lead lookup at 0). The best lookup has the most of the words, then a label with one of them, then the largest count;
    the message gives that count when a check made it (RECORDS_RELATION_LOOKUP_COUNTED_NOTE). Nothing when the role
    of a lookup of the slot fits the question's word (lookup check ``fits``: the role checks of :func:`lookup_nudges`
    decide), without ``lookups`` (the record session's view
    :meth:`~sf_assistant.agent.record_session.RecordSession.person_lookups`) or without both knowledge texts. At most
    one message. Every word comes from the knowledge; there is no list of phrases."""
    if lookups is None or not (draft or "").strip() or not str(question or "").strip():
        return []
    cache: dict[str, dict[str, dict]] = {}

    def views_of(obj: str) -> dict[str, dict]:
        if obj not in cache:
            cache[obj] = _lookup_views(lookups, obj)
        return cache[obj]

    slots: dict[tuple[str, str], dict] = {}

    def slot(obj: str, target: str) -> dict:
        return slots.setdefault((obj, target), {"used": [], "records": {}, "names": [], "fits": False})

    checks = _person_checks(evidence)
    for lc in _last_per_field(checks):
        obj, target, fld = (str(lc.get(k) or "") for k in ("object", "target", "field"))
        if not (obj and target and fld):
            continue
        s = slot(obj, target)
        if fld not in s["used"]:
            s["used"].append(fld)
        names = list(_filter_names(lc))
        s["names"] += [n for n in names if _norm_text(n) not in {_norm_text(x) for x in s["names"]}]
        others = _same_target(lc)
        if len(names) == 1:   # one person's counts (a filter on several people counts none of them alone)
            s["records"][fld] = lc.get("records")
            for o in others:
                s["records"].setdefault(str(o.get("field")), o.get("records"))
        s["fits"] = s["fits"] or bool(lc.get("fits") or lc.get("fits_sparse")
                                      or any(o.get("fits") or o.get("fits_sparse") for o in others))
    for e in evidence:
        if not (e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS):
            continue
        for obj, api in _fields_used(e):
            v = views_of(obj).get(api)
            if v and v.get("person") and v.get("target"):
                s = slot(obj, str(v["target"]))
                if api not in s["used"]:
                    s["used"].append(api)
    objects = _object_names(checks)
    for (obj, target), s in slots.items():
        if s["fits"] or not s["used"]:
            continue
        person = _slot_person(s["names"], people, target)
        if person is None:
            continue
        name, words = person
        views = {f: v for f, v in views_of(obj).items() if v.get("target") == target and v.get("person")}
        drop = _label_stems(name) | _label_stems(words)
        wording = {f: _wording(v) - drop for f, v in views.items()}
        asked = _label_stems(question) - drop
        # words every lookup of the slot has say what the records are ("mock interviews"), not how the person relates
        common = set.intersection(*wording.values()) if len(wording) > 1 else set()
        rel = (asked & set().union(*wording.values()) - common) if wording else set()
        if not rel:
            continue   # no word of the question is in the wording of these lookups
        used_words = set().union(*(wording.get(f, set()) for f in s["used"]))
        unnamed = [f for f in s["used"] if not wording.get(f, set()) & rel]
        if not unnamed:
            continue   # a used lookup carries the question's word: the answer took the relation the question names
        shown = _shown_lookups(evidence, obj, name)   # the direction check read these through about_people
        best = None
        for f, v in views.items():
            if f in s["used"] or f in shown or not (v.get("from_source") and v.get("from_target")):
                continue
            hit = (wording[f] & rel) - used_words
            if not hit or _names_field(draft, f, v.get("role"), objects | {obj, target}):
                continue
            # the direction reads the words of the lookup's label the question has (as direction_nudges does), else
            # the hit words: its guard "a word only one of the texts has" then still sees the question's other words
            # ("team members"); the person's own value only when every word of the label is in the question ("team"
            # alone is half of "Team Lead": either reading). Review of 2026-10-22.
            lab = _label_stems(v.get("label")) - drop - {_stem(w) for w in v.get("object_words") or ()}
            side = _direction_side(question, words, (lab & hit) or hit, str(v["from_source"]),
                                   str(v["from_target"])) or "unclear"
            if side == "source" and not (lab and lab <= asked):
                side = "unclear"
            n = s["records"].get(f)
            if obj != target and side != "target":
                continue   # a lookup to another object holds the person only as the value: no other reading
            if side == "target" and n == 0 and all(s["records"].get(u) == 0 for u in unnamed):
                continue   # 0 through it as through the used lookups, under the same conditions: the 0 stands
            if side in ("target", "unclear") and n == 0 and any(_count(s["records"].get(u)) > 0 for u in unnamed):
                continue   # 0 through it while a used lookup has records for the person: the answer's count stands
            key = (-len(hit), 0 if hit & _label_stems(v.get("label")) else 1, -_count(n), f)
            if best is None or key < best[0]:
                best = (key, f, v, hit, side, n)
        if best is None:
            continue
        _key, f, v, hit, side, n = best
        counted = (_fill(RECORDS_RELATION_LOOKUP_COUNTED_NOTE, RECORDS=n, OBJECT=obj, NAME=name, FIELD=f)
                   if isinstance(n, int) and not isinstance(n, bool) else "")
        said = " ".join(dict.fromkeys(t for t in re.findall(r"[a-z]+", str(question).lower()) if _stem(t) in hit))
        used = ", ".join(f"`{u}` ({views[u].get('label') or u})" if u in views else f"`{u}`" for u in unnamed[:3])
        vals = dict(WORD=said, OBJECT=obj, FIELD=f, LABEL=v.get("label") or f, FROM_SOURCE=v["from_source"],
                    FROM_TARGET=v["from_target"], USED=used, NAME=name)
        if side == "target":
            return [_fill(RECORDS_RELATION_LOOKUP_MESSAGE, COUNTED=counted, **vals)]
        if side == "source":
            return [_fill(RECORDS_RELATION_LOOKUP_SOURCE_MESSAGE, **vals)]
        return [_fill(RECORDS_RELATION_LOOKUP_UNCLEAR_MESSAGE, COUNTED=counted, **vals)]
    return []


# ------------------------------------------------------------- items 8 and 9: parent paths and a child copy (review)
_PATH_API_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*__c|[A-Z][A-Za-z]*Id\b")


def paths_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_PATHS_MESSAGE`` (Group B item 8, docs/people.md) for the last person check per object, lookup and
    person whose ``any_path`` (the filter or any parent path or same-role child row, C5) is larger than its own count,
    when the draft's first sentence gives that own count, the draft gives no ``any_path`` count, and it names no
    lookup or object of the paths (beyond the counted object and the filter's lookup). Not for same-name records
    (``by_record``: the per-record note says it). At most one message."""
    if not (draft or "").strip():
        return []
    lead = _lead_numbers(draft)
    nums = set(numbers_in(_DATE_TIME_RE.sub(" ", _body(draft))))
    last: dict[tuple, dict] = {}
    for lc in _person_checks(evidence):
        if lc.get("paths") is not None and "any_path" in lc:
            last[(lc.get("object"), lc.get("field"), _person_key(lc))] = lc
    for lc in last.values():
        n0, ap = lc.get("records"), lc.get("any_path")
        if _count(ap) <= _count(n0) or lc.get("by_record") or _num(n0) not in lead or _num(ap) in nums:
            continue
        rel = [x for x in lc.get("related") or [] if isinstance(x, dict) and x.get("same_role")]
        texts = [str(x.get("path") or "") for x in lc.get("paths") or [] if isinstance(x, dict)]
        texts += [f"{x.get('object')}.{x.get('field')}" for x in rel]
        own = {str(lc.get("object")), str(lc.get("field"))}
        apis = {t for x in texts for t in _PATH_API_RE.findall(x)} - own
        if any(_names(draft, a) for a in apis):
            continue   # the draft speaks of a path
        parts = [f"`{x.get('path')}` {x.get('records')}" for x in (lc.get("paths") or [])[:3] if isinstance(x, dict)]
        parts += [f"`{x.get('object')}.{x.get('field')}` rows ({x.get('distinct')} {lc.get('object')} records)"
                  for x in rel[:2]]
        names = _filter_names(lc)
        return [_fill(RECORDS_PATHS_MESSAGE, RECORDS=n0, OBJECT=lc.get("object"), FIELD=lc.get("field"),
                      NAME=" / ".join(f"'{n}'" for n in names) if names else str(lc.get("filter") or "this person"),
                      PATHS="; ".join(parts), ANY=ap)]
    return []


def child_copy_nudges(draft: str, evidence: list[QueryEvidence]) -> list[str]:
    """``RECORDS_CHILD_COPY_MESSAGE`` (Group B item 9, docs/people.md) for the last counting query per object whose
    result has ``child_copy`` (C6: a lookup that copies a child object's lookup, used without the source), unless the
    draft names the source lookup or a later counting query used it. The lookup check's count through the source
    (``copy_source``) is added (``RECORDS_CHILD_COPY_HELD_NOTE``). At most one message."""
    if not (draft or "").strip():
        return []
    last: dict[Any, QueryEvidence] = {}
    for e in evidence:
        if e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS | {"list_records"}:
            last[e.object] = e
    for obj, e in last.items():
        res = e.model_result if isinstance(e.model_result, dict) else {}
        for c in res.get("child_copy") or []:
            if not isinstance(c, dict) or "." not in str(c.get("source") or ""):
                continue
            src_obj, src_field = str(c["source"]).split(".", 1)
            if _names(draft, str(c["source"])) or (_names(draft, src_obj) and _names(draft, src_field)):
                continue
            if _used_later(evidence, e.query_id, src_obj, src_field):
                continue
            held = ""
            for lc in res.get("lookup_check") or []:
                cs = lc.get("copy_source") if isinstance(lc, dict) else None
                if isinstance(cs, dict) and cs.get("field") == c.get("field") and _count(cs.get("records")) > 0:
                    names = _filter_names(lc)
                    held = _fill(RECORDS_CHILD_COPY_HELD_NOTE, SOURCE=c["source"], COUNT=cs["records"], OBJECT=obj,
                                 NAME=" / ".join(f"'{n}'" for n in names) if names else "this person")
                    break
            return [_fill(RECORDS_CHILD_COPY_MESSAGE, OBJECT=obj, FIELD=c.get("field"), SOURCE=c["source"],
                          FILLED=c.get("filled"), SOURCE_FILLED=c.get("source_filled"), HELD=held,
                          VIA=str(c.get("via") or "").split(".")[-1])]
    return []


# ------------------------------------------------------------------------------- item 11: the status line
def _completion_cues(question: str) -> list[str]:
    """The question's words that can mean every assigned record or only the finished ones: a word that starts with a
    completion stem of the hints (``tools.names._COMPLETION_CUES``) or of the "taken / done" role cue
    (``engine.catalog.ROLE_CUES[0]``), or whose stem starts that cue's role stems (the activity of the role:
    "interview" of "interviewer"). The stems are the existing ones; no new word list."""
    from sf_assistant.engine.catalog import ROLE_CUES
    from sf_assistant.tools.names import _COMPLETION_CUES

    cues = tuple(dict.fromkeys(_COMPLETION_CUES + tuple(ROLE_CUES[0][0])))
    out: list[str] = []
    for tok in re.findall(r"[a-z]+", str(question or "").lower()):
        st = _stem(tok)
        if tok.startswith(cues) or (len(st) >= 5 and any(r.startswith(st) for r in ROLE_CUES[0][1])):
            out.append(tok)
    return list(dict.fromkeys(out))


def _cue_fits_role(cues: list[str], role: Any) -> bool:
    """A completion cue of the question fits a lookup role: the cue's stem and a role word's stem start one another
    ("interview" / "interviewer", "train" / "trainer"), or a ``ROLE_CUES`` entry whose cue stems the cue starts with
    has an employee role stem that a role word starts with ("taken" -> interviewer). The candidate side of a link
    (the one who is interviewed or trained) never fits alone: a training cue would fit every link with a candidate."""
    from sf_assistant.engine.catalog import ROLE_CUES

    words = [w for w in re.findall(r"[a-z]+", str(role or "").lower()) if len(w) >= 4]
    for c in cues:
        sc = _stem(c)
        if any(len(_stem(w)) >= 4 and (_stem(w).startswith(sc) or sc.startswith(_stem(w))) for w in words):
            return True
        for cue in ROLE_CUES:
            if c.startswith(tuple(cue[0])) and any(w.startswith(tuple(cue[1])) for w in words):
                return True
    return False


def _call_status_of(status_of: Any, obj: Any) -> dict | None:
    if status_of is None or not obj:
        return None
    try:
        info = status_of(str(obj))
    except Exception:  # noqa: BLE001 - optional
        return None
    return info if isinstance(info, dict) else None


def _value_key(value: Any) -> str:
    v = str(value if value is not None else "").strip()
    return "(blank)" if v in ("", "(blank)", "None", "null") else v


def _link_roles(lk: dict, directions: Any = None) -> list[str]:
    """The roles of the two lookups of a "same record" link: from its ``path`` ("<Obj>.<a> + <Obj>.<b>") through
    ``directions`` (the record session's catalog view, ``role``), else from the link's text ("`X` (role)")."""
    m = re.match(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+) \+ ([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)$", str(lk.get("path") or ""))
    if m and directions is not None:
        roles = [str((_call_directions(directions, m.group(o), m.group(f)) or {}).get("role") or "")
                 for o, f in ((1, 2), (3, 4))]
        if all(roles):
            return roles
    return re.findall(r"`[A-Za-z0-9_]+` \(([^)]*)\)", str(lk.get("text") or ""))


def _filters_field(e: QueryEvidence, fld: str, label: str = "") -> bool:
    """The call filtered on the base object's field ``fld`` itself (its API name or label as a filter or ``any_of``
    field of the arguments, or the field an ``applied`` filter starts with: "Status__c = 'Completed'"): its split is
    the filter's, not the person's whole set of records. The arguments are those that ran: code's enforced ones for a
    business term (``QueryEvidence.enforced_arguments``, 2026-11-24), else the model's."""
    ran = getattr(e, "enforced_arguments", None) or e.arguments
    args = ran if isinstance(ran, dict) else {}
    keys = {str(fld).lower()} | ({str(label).lower()} if label else set())
    for key in ("filters", "any_of"):
        for x in args.get(key) or []:
            if isinstance(x, dict) and str(x.get("field") or "").strip().lower() in keys:
                return True
    applied = e.applied if isinstance(e.applied, dict) else {}
    for key in ("filters", "any_of"):
        for x in applied.get(key) or []:
            m = re.match(r"\s*([A-Za-z][A-Za-z0-9_]*)\s*(?:=|!=|<>|<|>|\bIN\b|\bNOT\b|\bLIKE\b|\bIS\b)", str(x),
                         re.IGNORECASE)
            if m and m.group(1).lower() == str(fld).lower():
                return True
    return False


def _status_splits(evidence: list[QueryEvidence], cues: list[str], status_of: Any,
                   directions: Any = None) -> list[dict]:
    """The status splits of the person's records that the question's completion cue is about, the last per (object,
    status field, person): ``{"object", "field", "label", "values", "by", "who"}``. From a person count's
    ``breakdown``, a group_records result grouped (or split) by the status field, a complete list_records result with
    the status column (each with a person lookup_check whose ``fits`` is a cue, and not filtered on the status field
    itself: its split only shows the filter), and an about_people "same record" link whose roles the cue fits (its
    ``by_status``, else its examples' "(<status>)" when they cover every record). ``who`` is the filter's names (the
    link's people). ``status_of(object)`` gives the object's status field, label and picklist values, ``directions``
    a lookup's role (the record session's catalog view)."""
    found: dict[tuple[str, str, Any], dict] = {}
    for e in evidence:
        res = e.model_result if (e.ok and e.kind == "data" and isinstance(e.model_result, dict)) else None
        if res is None:
            continue
        items: list[tuple[str, dict, Any]] = []   # (object, {value: n}, who)
        if e.tool in PERSON_TOOLS:
            label_of = {str(p.get("ref")): _plain_name(p.get("label")) for p in res.get("people") or []
                        if isinstance(p, dict)}
            for lk in res.get("links") or []:
                if not isinstance(lk, dict) or lk.get("how") != "same record":
                    continue
                obj = str(lk.get("path") or "").split(".", 1)[0]
                roles = _link_roles(lk, directions)
                info = _call_status_of(status_of, obj)
                if not obj or not info or not info.get("field") or not any(_cue_fits_role(cues, r) for r in roles):
                    continue
                bs = lk.get("by_status") if isinstance(lk.get("by_status"), dict) else {}
                by = bs.get(info["field"]) if isinstance(bs.get(info["field"]), dict) else None
                if by is None:
                    exs, n = [str(x) for x in lk.get("examples") or []], lk.get("records")
                    known = {_norm_label(x) for pair in info.get("values") or [] for x in pair}
                    if not isinstance(n, int) or n <= 0 or len(exs) != n:
                        continue
                    by = {}
                    for x in exs:   # "<Name> (<status>)"; no bracket: a blank status; another bracket: unknown
                        m = re.search(r"\(([^()]*)\)\s*$", x)
                        if m and _norm_label(m.group(1)) not in known:
                            by = None
                            break
                        v = _value_key(m.group(1) if m else None)
                        by[v] = by.get(v, 0) + 1
                    if by is None:
                        continue
                who = tuple(sorted(label_of.get(str(r), str(r)).lower() for r in lk.get("between") or []))
                items.append((obj, by, who))
            _add_splits(found, items, status_of)
            continue
        checks = [lc for lc in res.get("lookup_check") or [] if isinstance(lc, dict)]
        fitting = [lc for lc in checks if str(lc.get("fits") or "").lower() in cues]
        if not fitting or not e.object:
            continue
        info = _call_status_of(status_of, e.object)
        fld = (info or {}).get("field")
        if not fld or _filters_field(e, fld, (info or {}).get("field_label") or ""):
            continue
        who = tuple(n.lower() for n in _filter_names(fitting[0])) or str(fitting[0].get("filter") or "")
        by = None
        if e.tool == "count_records" and isinstance(res.get("breakdown"), dict):
            by = res["breakdown"].get(fld) if isinstance(res["breakdown"].get(fld), dict) else None
        elif e.tool == "group_records" and res.get("measure", "count") == "count" and not res.get("trimmed") \
                and res.get("shown", 0) >= res.get("group_count", 0):
            g_api = str(res.get("grouped_by") or "").split(" ")[0]
            s_api = str(res.get("split_by") or "").split(" ")[0]
            if g_api == fld and not res.get("split_by"):
                by = {str(g.get("group")): g.get("value") for g in res.get("groups") or [] if isinstance(g, dict)}
            elif s_api == fld and isinstance(res.get("split_totals"), dict):
                by = dict(res["split_totals"])
        elif e.tool == "list_records" and fld in (res.get("columns") or []) and not res.get("truncated") \
                and res.get("returned") == res.get("total_count"):
            by = {}
            for r in res.get("rows") or []:
                if isinstance(r, dict):
                    v = _value_key(r.get(fld))
                    by[v] = by.get(v, 0) + 1
        if by:
            items.append((str(e.object), by, who))
        _add_splits(found, items, status_of)
    return list(found.values())


def _add_splits(found: dict, items: list[tuple[str, dict, Any]], status_of: Any) -> None:
    """Put the ``(object, {value: n}, who)`` items of one result into ``found`` (a later result replaces an earlier
    one of the same object, field and person)."""
    for obj, by, who in items:
        info = _call_status_of(status_of, obj) or {}
        clean: dict[str, int] = {}
        for k, v in by.items():
            if _count(v) > 0:
                clean[_value_key(k)] = clean.get(_value_key(k), 0) + _count(v)
        if clean and info.get("field"):
            found[(obj, info["field"], who)] = {"object": obj, "field": info["field"], "label": info.get("label") or obj,
                                                "field_label": info.get("field_label") or "",
                                                "values": info.get("values") or [], "by": clean, "who": who}


def _or_list(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def _names_value(text: str, value: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", text, re.IGNORECASE) is not None


def status_lines(text: str, evidence: list[QueryEvidence], question: str, status_of: Any = None,
                 directions: Any = None) -> list[str]:
    """The code lines of :func:`with_status_line` for ``text`` (a final record or merged answer), at most 2, else [].

    One line per status split the question's verb is about (Group B item 11, docs/people.md): "Status of these
    <Object> records (`<field>`, <n>): <value> <k>, ...; none is <A>, <B> or <C>." (``RECORDS_STATUS_LINE``). It fires
    when the question has a completion cue (:func:`_completion_cues`: "taken", "interviewed", "trained"), a
    person-filtered count, group, list or link of the evidence has a status split of the cue's records
    (:func:`_status_splits`), the question does not name the status field itself (its label: "by status" asks for
    the split, so the answer's split is the answer), the status field has picklist values with no record in it, and
    the text names none of those values in a line that names no other object of the evidence (a "(Scheduled)" said of
    a session is no word about the interviews). It never says which value means done, and it says nothing for an
    object and status field that splits of two or more people share (one line cannot say whose records it counts)
    or for a call filtered on the status field (:func:`_status_splits`). [] without ``status_of`` (the record
    session's view of the status fields), without a split, or for an empty text. ``directions``: the roles of a
    link's lookups (else read from the link's text)."""
    if not (text or "").strip() or status_of is None:
        return []
    cues = _completion_cues(question)
    if not cues:
        return []
    splits = _status_splits(evidence, cues, status_of, directions)
    per: dict[tuple[str, str], int] = {}
    for x in splits:
        per[(x["object"], x["field"])] = per.get((x["object"], x["field"]), 0) + 1
    splits = [x for x in splits if per[(x["object"], x["field"])] == 1]   # several people: whose? no line
    if not splits:
        return []
    objects = {str(e.object) for e in evidence if e.ok and e.kind == "data" and e.object}
    for e in evidence:
        if e.ok and e.tool in PERSON_TOOLS and isinstance(e.model_result, dict):
            objects.update(str(lk.get("path") or "").split(".", 1)[0] for lk in e.model_result.get("links") or []
                           if isinstance(lk, dict))
    labels = {o: str((_call_status_of(status_of, o) or {}).get("label") or "") for o in objects if o}
    lines_out: list[str] = []
    body = [ln for ln in _body_text(text).split("\n")]
    asked = {_stem(t) for t in _TOKEN_RE.findall(str(question or "").lower())}
    for s in splits:
        fl = _label_stems(s.get("field_label"))
        if fl and fl <= asked:
            continue   # the question asks for the split by name ("by status"): the answer's split is the answer
        zero = []
        for pair in s["values"]:
            value, label = (list(pair) + [None, None])[:2]
            names = {str(x) for x in (value, label) if x}
            if not names or any(n in s["by"] for n in names):
                continue
            zero.append(str(label or value))
        zero = list(dict.fromkeys(zero))
        if not zero or "(other)" in s["by"]:
            continue

        def about_other(ln: str) -> bool:   # the line names another object of the evidence (and not this one's)
            own = re.sub(re.escape(s["label"]), " ", ln, flags=re.IGNORECASE) if s["label"] else ln
            own = re.sub(r"(?<![A-Za-z0-9_])" + re.escape(s["object"]) + r"(?![A-Za-z0-9_])", " ", own)
            return any(o != s["object"] and (_names(own, o) or (lab and _names_value(own, lab)))
                       for o, lab in labels.items())
        if any(_names_value(ln, z) for ln in body if not about_other(ln) for z in zero):
            continue
        split = ", ".join(f"{'blank' if k == '(blank)' else k} {v}" for k, v in
                          sorted(s["by"].items(), key=lambda kv: (-kv[1], kv[0])))
        lines_out.append(_fill(RECORDS_STATUS_LINE, OBJECT=s["label"], FIELD=s["field"], COUNT=sum(s["by"].values()),
                               SPLIT=split, ZERO=_or_list(zero[:_MAX_ZERO_VALUES])))
        if len(lines_out) >= _MAX_STATUS_LINES:
            break
    return lines_out


def with_lines(text: str, lines_out: list[str]) -> str:
    """``text`` with ``lines_out`` before a last Source line (else at the end); unchanged without lines."""
    if not lines_out or not (text or "").strip():
        return text
    lines = text.rstrip().split("\n")
    if _is_source_line(lines[-1]):
        return "\n".join(lines[:-1] + lines_out + [lines[-1]])
    return "\n".join(lines + lines_out)


def without_lines(text: str, lines_out: list[str]) -> str:
    """``text`` without the whole lines ``lines_out`` (what :func:`with_lines` added)."""
    if not lines_out:
        return text
    drop = set(lines_out)
    return "\n".join(ln for ln in (text or "").split("\n") if ln not in drop)


def with_status_line(text: str, evidence: list[QueryEvidence], question: str, status_of: Any = None,
                     directions: Any = None) -> str:
    """``text`` with the status lines of :func:`status_lines` before a last Source line (Group B item 11)."""
    return with_lines(text, status_lines(text, evidence, question, status_of, directions))


# ------------------------------------------------------------------------- step-3 fix T5: the kinds of a draft message
#: Each ``records_*_message`` prompt's id and the fixed parts of its text around its placeholders (bound at import, as
#: every prompt text: a page keeps one view of the prompts folder).
_NUDGE_PARTS = tuple((pid, re.split(r"<<[A-Z][A-Z0-9_]*>>", _prompt_text(pid).strip())) for pid in _prompt_ids()
                     if pid.startswith("records_") and pid.endswith("_message"))


def _fills(parts: list[str], text: str) -> bool:
    """``text`` opens with these fixed parts, in order, with any text in the placeholders between them (an appended note
    may follow). A plain scan: no regular expression may stall a nudge over a log counter."""
    if not text.startswith(parts[0]):
        return False
    pos = len(parts[0])
    for part in parts[1:]:
        pos = text.find(part, pos)
        if pos < 0:
            return False
        pos += len(part)
    return True


def nudge_kinds(msg: str | None) -> list[str]:
    """The prompt ids of the messages joined in ``msg`` (one blank line between them, as ``check_draft`` joins them), in
    order, read from the whole text; "other" for a part that fills none of them. Prompt ids only, never text. Never
    raises (a raise would silently cancel the nudge: ``answer_question`` swallows what its check raises)."""
    try:
        parts = (part.strip() for part in str(msg or "").split("\n\n"))
        return [next((i for i, fixed in _NUDGE_PARTS if _fills(fixed, part)), "other") for part in parts if part]
    except Exception:  # noqa: BLE001
        return []
