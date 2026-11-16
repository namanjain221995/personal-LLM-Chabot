"""The merge of the knowledge answer and the record answer (moved verbatim from okrec/session.py).

:func:`combine` merges both answers in one model call and rejects a merge whose numbers no record fact holds
(:func:`audit_numbers`) or that changes the scope (:func:`merge_scope_issues`); :func:`clean_knowledge_answer`,
:func:`side_caveats`, :func:`drop_record_side_caveats`, :func:`with_name_note`, :func:`warehouse_availability`,
:func:`fix_any_role_scope` and :func:`merge_cut_off` are its checks and cleaners.

Evaluation fixes of 2026-10-22: a record answer or a merged text with nothing but its Source line is no answer
(:func:`~sf_assistant.agent.nudges.answer_text`, fix 2), and a merged count-0 clause that a clause of the record answer
holds with the same claim is no reason to reject the merge (:func:`_in_record_answer`, fix 3). Review of the same
day: fix 3 is the count-0 check only (the any_role clause is fixed by :func:`fix_any_role_scope`, which also splits a
bracket's items at "+" and "/"), and a clause that drops the record answer's negation is not the record answer's.

The lean-mode merge by code (latency plan step 2, the owner's go of 2026-10-29; new code, no notebook function behind
it): :func:`code_merge` keeps the record answer as it is and adds at most :data:`CODE_MERGE_MAX_BULLETS` bullets of
the lean facts block (the field meanings) before its Source line, with no model call. A bullet is kept only when it is
about a field the answer used (by object and field), or, naming no field, about the answer's object, and passes the
merge's own checks (the number audit, working notes and tool talk, the scope check) and those of its review (no name
of the question's people, no talk about the knowledge, the question or the prompt, no cut-off bullet); nothing is
rewritten, and after a list a label line comes first. ``pipeline.answer`` calls it in lean mode without a FIELD CHECK
line unless ``SFA_MERGE=model`` (:func:`sf_assistant.config.merge_settings`) or the record answer still plans aloud
(:func:`plans_aloud`); :func:`combine` is unchanged and still runs for the full knowledge agent, a FIELD CHECK line,
such a record answer and ``SFA_MERGE=model``.

Rows by code (latency plan step 3, ``SFA_TABLES=code``): :func:`combine` takes two trailing arguments, the record
answer as the merge call gets it (its code rows as table lines) and a note for its system prompt; every check still
reads the record answer with its rows. :func:`code_merge` audits with the rows' own text as one more source. Without
code rows both are as before.

The step-2 fixes of 2026-10-30 (the code-merge evaluation of 2026-10-29, docs/runbook.md): :func:`code_merge` reads the
record catalog (``objects``, name -> ObjectSpec) and drops a bullet that contradicts it or the answer's own results
(:func:`_contradiction`: a wrong lookup target, a field the object lacks, a picklist called a lookup, the row of an
object no lookup links to the answer's objects, a legacy or inactive claim the knowledge does not make or that undercuts
a count, an FLS claim the knowledge does not make, a wrong value meaning, a value count or "unused" claim the results
contradict, a caveat the lookup check resolves) or whose subject is another object
(:func:`_other_object_subject`); it reads the unfinished-end test on the facts block's last line only, puts the bullets
on the counting queries' date fields first at the cap, adds one code line that says what the one counting query
counted when the record answer leaves it out (:func:`_counted_line`, prompts/code_merge_counted.md) and adds nothing to
a Hinglish record answer (:func:`_hinglish`); :func:`plans_aloud` also sends thinking aloud, tool talk and Hinglish
first-person planning to the merge call. Every check only drops: code never rewrites a bullet.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from typing import Any, NamedTuple

from sf_assistant import prompts as _prompt_files   # the merge by code's label line (prompts/code_merge_label.md)
from sf_assistant.agent.checks import (
    _DATE_TIME_RE, _body_text, _is_source_line, _not_available, audit_numbers, drop_ref_codes, drop_tool_talk,
    drop_working_sentences, facts_json, numbers_in, quoted_spans, ref_codes_left, reference_codes, strip_reasoning,
    working_notes)
from sf_assistant.agent.checks import _PLANNING_RE, _TOOL_TALK_RE, _label_only   # the merge by code (2026-10-29)
from sf_assistant.agent.checks import _PROMPT_TALK_RE, _TOOL_WORD_RE   # plans_aloud's tool talk (step-2 fixes)
from sf_assistant.agent.nudges import (
    _CLAUSE_SPLIT_RE, _CUSTOM_FIELD_RE, _EXTRA_RE, _OVERLAP_RE, _REFERENCE_TOOLS, _SYSTEM_LOOKUPS, _body, _count,
    _derived_counts, _extra_numbers, _fields_used, _filter_names, _has_name, _lead, _names, _names_field,
    _no_data_but_not_found, _norm_text, _num, _object_names, _overlap_clause, _person_checks, _same_target,
    lookup_checks, with_source_line)
from sf_assistant.agent.nudges import answer_text   # evaluation fix 2 (2026-10-22)
from sf_assistant.agent.nudges import _COUNTING_TOOLS   # the merge by code: what a counting query is
from sf_assistant.agent.record_prompts import COMBINE_SYSTEM, combine_user_message
from sf_assistant.agent.record_prompts import KNOWLEDGE_LEAN_SYSTEM   # the merge by code: the lean prompt's own words
from sf_assistant.agent.results import MergeResult, RecordAnswer
from sf_assistant.config import LIMITS
from sf_assistant.engine.types import PERSON_TOOLS, QueryEvidence, RecordsStatus

#: RECORD FACTS size for the merge call: a find_references result alone can be about 10,000 characters.
MERGE_FACTS_CHARS = LIMITS.merge_facts_chars   # 16_000
#: Token limit of the merge call: a people answer with a dozen links and two profiles runs to about 3,000 characters
#: (900 tokens cut one off mid-sentence, RECORDS_SPEC §22.17).
MERGE_MAX_TOKENS = LIMITS.merge_max_tokens     # 1600


#: A sentence of the knowledge answer about fetching records instead of meaning: SOQL or SQL, "you would need to run a
#: query", "I do not have access to record data", "the object knowledge does not contain record data".
_KNOWLEDGE_NOISE_RE = re.compile(
    # SQL keywords in capitals only ("select a status from" is prose); "you can query by `X`: it holds ..." is metadata
    r"\bSOQL\b|(?-i:\bSELECT\s+[^\n]{0,200}?\bFROM\b)|"
    r"\byou\s+(?:would|will|can|could|may|might)\s+(?:need\s+to\s+|have\s+to\s+)?"
    r"(?:run|query(?!\s+by\b)|execute|write)\b|"
    r"\b(?:run|execute|write|use)\s+(?:a|an|the|this|the\s+following)\s+(?:\w+\s+)?query\b|"
    # "I cannot see / count ..." only about record data ("I cannot see a field that ..." is metadata)
    r"\bI\s+(?:do\s+not|don't|cannot|can't|can\s+not|am\s+unable\s+to|am\s+not\s+able\s+to)\s+(?:have\s+access\s+to|access|"
    r"provide|see|give|count|query|retrieve|look\s+up)\s+(?:(?!fields?\b|lookups?\b|picklists?\b|values?\b|objects?\b)"
    r"[\w-]+\s+){0,5}?(?:counts?|records?|record\s+data|live\s+data|actual\s+data|data|numbers?|totals?|how\s+many)\b|"
    r"\b(?:no|without)\s+access\s+to\s+(?:the\s+)?(?:record|actual)\s+data\b|"
    r"\bknowledge\s+(?:does\s+not|doesn't)\s+(?:contain|include|have|hold)\s+(?:any\s+)?(?:actual\s+)?"
    r"(?:record|actual)\s+(?:data|counts?)\b|"
    r"\brecord\s+data\s+(?:is|are)\s+(?:not\s+available|unavailable)\b", re.IGNORECASE)


def clean_knowledge_answer(text: str | None) -> str:
    """The knowledge answer without what it says about fetching records (:data:`_KNOWLEDGE_NOISE_RE`: SOQL or SQL, code
    blocks with a query, "you would need to run a query", "I do not have access to record data"): the merge must not
    copy it. Sentences are dropped one by one; a line left empty is removed. The meaning of objects, fields and values
    stays."""
    t = re.sub(r"```[^\n]*\n(.*?)```[ \t]*\n?", lambda m: "" if re.search(r"\bSELECT\b|\bFROM\b|SOQL|\bWHERE\b", m.group(1),
                                                                  re.IGNORECASE) else m.group(0),
               text or "", flags=re.DOTALL)
    lines: list[str] = []
    for ln in t.split("\n"):
        if not _KNOWLEDGE_NOISE_RE.search(ln):
            lines.append(ln)
            continue
        kept = [x for x in re.split(r"(?<=[.!?])\s+", ln) if x.strip() and not _KNOWLEDGE_NOISE_RE.search(x)]
        kept = [x for x in kept if re.search(r"[A-Za-z]{3}", x)]   # no leftover "(Note:" or "*"
        if kept:
            lines.append(" ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_SCOPE_API = r"[A-Za-z][A-Za-z0-9_]*__c(?:\.[A-Za-z][A-Za-z0-9_]*)*|RecordTypeId|OwnerId|CreatedById|LastModifiedById"
#: A condition stated on a field ("records where `X__c` is populated", "filtered by `X__c`", "with `X__c` = ...").
_FILTER_CLAIM_RE = re.compile(r"\b(?:where|whose|with|filtered\s+(?:on|by)|only\s+(?:records|those)\s+(?:where|with))\s+"
                              r"(?:the\s+)?`?(" + _SCOPE_API + r")`?\s*(?:=|!=|<>|\b(?:is|are|equals|in|has|have|not|"
                              r"was|were|populated|set|filled|blank|empty)\b)", re.IGNORECASE)


def _scope_fields(rec: RecordAnswer) -> set[str]:
    """API names a merged answer may state a condition on: every field the successful data queries used
    (``fields_used``, each path part), the names in their "applied" and notes, and the names in the record answer."""
    out = set(_CUSTOM_FIELD_RE.findall(rec.answer or ""))
    out.update(f for f in _SYSTEM_LOOKUPS + ("RecordTypeId",) if _names(rec.answer or "", f))
    for e in rec.evidence:
        if not (e.ok and e.kind == "data"):
            continue
        for _obj, fld in _fields_used(e):
            out.update(fld.split("."))
        res = e.model_result if isinstance(e.model_result, dict) else {}
        side = json.dumps([res.get("applied"), res.get("notes"), e.applied], ensure_ascii=False, default=str)
        out.update(_CUSTOM_FIELD_RE.findall(side))
        out.update(f for f in _SYSTEM_LOOKUPS + ("RecordTypeId",) if _names(side, f))
    return out


#: A claim that a person or value is absent ("not linked to", "no records", "none", "does not appear").
_NONE_CLAIM_RE = re.compile(r"\b(?:not\s+(?:linked|associated|referenced|related|recorded|listed|found)|no\s+(?:records?|"
                            r"links?|entries|matches|references)\b|none\b|ha(?:s|ve)\s+no\b|(?:does\s+not|doesn't|do\s+not|"
                            r"don't)\s+appear)", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n+")


def _uncounted_none_claims(text: str, rec: RecordAnswer) -> list[str]:
    """Objects that a sentence of ``text`` claims the person or value is absent from ("not linked to client interviews
    (`Interview__c`)"), although no successful counting query ran on that object and the record answer makes no such
    claim about it. Nothing when a find_references call ran or a lookup check counted 0 (its reference scan may
    back such a claim)."""
    ev = rec.evidence
    if any(e.ok and e.tool in PERSON_TOOLS for e in ev) or any(lc.get("records") == 0 for lc in lookup_checks(ev)):
        return []
    known = {str(e.object) for e in ev if e.object}
    counted = {str(e.object) for e in ev if e.ok and e.kind == "data" and e.tool in _REFERENCE_TOOLS}
    rec_claims = [s for s in _SENTENCE_SPLIT_RE.split(_body(rec.answer or "")) if _NONE_CLAIM_RE.search(s)]
    out: list[str] = []
    for sentence in _SENTENCE_SPLIT_RE.split(_body(text or "")):
        if not _NONE_CLAIM_RE.search(sentence):
            continue
        for obj in sorted(known - counted):
            if _names(sentence, obj) and not any(_names(s, obj) for s in rec_claims) and obj not in out:
                out.append(obj)
    return out


#: A claim that an object or field is not in the warehouse ("are not available", "is not synced", "does not exist").
_UNAVAILABLE_RE = re.compile(r"\b(?:not\s+(?:be\s+|been\s+)?(?:available|synced|present|stored\s+in\s+the\s+(?:warehouse|"
                             r"snapshot|data)|in\s+the\s+(?:local\s+)?(?:warehouse|snapshot|copy|synced\s+data)|part\s+of\s+the\s+"
                             r"(?:warehouse|snapshot))|unavailable|(?:does|do)\s+not\s+exist|missing\s+from\s+the\s+(?:warehouse|"
                             r"snapshot|data))", re.IGNORECASE)
#: Where a clause ends before an availability claim: what is named before it is what the claim is about.
_CLAIM_CLAUSE_RE = re.compile(r"[;:]|\bbut\b|\bso\b|\bwhile\b|\bhowever\b|\binstead\b|\bwhereas\b|\bproxy\b|,\s*(?:and|which)\b",
                              re.IGNORECASE)
_API_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*__c(?:\.[A-Za-z][A-Za-z0-9_]*)*\b")
#: After a field token: "(field) on / of / in / from (the) `Object__c`" - the claim is about that object's field.
_FIELD_OF_RE = re.compile(r"^[`*_]*(?:\s+(?:field|lookup|column))?\s*\(?\s*(?:on|of|in|from)\s+(?:the\s+)?[`*_]*"
                          r"(?P<obj>[A-Za-z][A-Za-z0-9_]*__c)\b(?!\.)", re.IGNORECASE)
#: After an object token: "('s | object's) (own) `Field__c`" - the claim is about that object's field.
_OWNS_FIELD_RE = re.compile(r"^[`*_]*(?:\s+object)?\s*['\u2019]s\s+(?:own\s+)?[`*_]*"
                            r"(?P<field>[A-Za-z][A-Za-z0-9_]*__c)\b(?!\.)", re.IGNORECASE)


def _clause_claims(clause: str) -> list[str]:
    """The API names a claim clause is about, in order: ``<Obj>.<Field>`` for "`Field` (field) on/of/in (the)
    `Obj`" and "`Obj`'s `Field`" (the object is only the field's container, never claimed itself), the written
    ``<Obj>.<Field>`` token, and every other name as it stands."""
    toks = list(_API_TOKEN_RE.finditer(clause))
    used: set[int] = set()   # start offsets of tokens already part of a pair
    out: list[tuple[int, str]] = []
    for m in toks:
        tok = m.group(0)
        if m.start() in used or "." in tok:
            continue
        rest = clause[m.end():]
        fo, owns = _FIELD_OF_RE.match(rest), _OWNS_FIELD_RE.match(rest)
        if fo:
            out.append((m.start(), f"{fo.group('obj')}.{tok}"))
            used.update({m.start(), m.end() + fo.start("obj")})
        elif owns:
            out.append((m.start(), f"{tok}.{owns.group('field')}"))
            used.update({m.start(), m.end() + owns.start("field")})
    out += [(m.start(), m.group(0)) for m in toks if m.start() not in used]
    return list(dict.fromkeys(tok for _i, tok in sorted(out)))


def _claimed_unavailable(text: str, pattern: re.Pattern | None = None) -> list[tuple[str, str]]:
    """``(API name, sentence)`` for each object or field that a sentence of ``text`` claims is not in the warehouse:
    the API names in the same clause before the claim words (:data:`_UNAVAILABLE_RE`). A field named with its object
    ("the `Evaluator__c` field on `Interview_Evaluation__c`", "`Interview_Evaluation__c.Evaluator__c`") gives the
    ``<Obj>.<Field>`` pair, not the object (:func:`_clause_claims`). ``pattern`` replaces the claim words
    (:data:`_NO_DATA_CLAIM_RE` for "has no data")."""
    out: list[tuple[str, str]] = []
    for sentence in _SENTENCE_SPLIT_RE.split(_body(text or "")):
        for m in (pattern or _UNAVAILABLE_RE).finditer(sentence):
            head = sentence[:m.start()]
            cuts = list(_CLAIM_CLAUSE_RE.finditer(head))
            clause = head[cuts[-1].end():] if cuts else head
            for tok in _clause_claims(clause):
                if all(t != tok for t, _s in out):
                    out.append((tok, sentence))
    return out


def _claimed_alike(tok: str, claims: set[str]) -> bool:
    """Whether ``claims`` (API names another text calls not available) make the claim about ``tok``: the same name,
    the field of an ``<Obj>.<Field>`` pair, or a pair whose field is ``tok``. A pair never covers its object."""
    if tok in claims:
        return True
    if "." in tok:
        return tok.split(".")[-1] in claims
    return any(c.endswith("." + tok) for c in claims)


def _unavailable_in_facts(rec: RecordAnswer, field_check: list[str] | None) -> set[str]:
    """API names the record facts give as not in the warehouse or without data: not_available errors, unsynced_fits,
    lookup_check not_in_warehouse and empty_lookups, no_data_fields, the notes that say so, and the FIELD CHECK lines
    (each "<Obj>.<Field>" also as "<Field>")."""
    out: set[str] = set()
    texts: list[str] = list(field_check or [])
    for e in rec.evidence:
        if _not_available(e):
            texts.append(str(e.error or ""))
        if not (e.ok and isinstance(e.model_result, dict)):
            continue
        res = e.model_result
        for x in res.get("unsynced_fits") or []:
            if isinstance(x, dict) and x.get("field"):
                out.update({str(x["field"]), f"{e.object}.{x['field']}"})
        for x in res.get("no_data_fields") or []:
            out.update({str(x), str(x).split(".")[-1]})
        for lc in res.get("lookup_check") or []:
            if isinstance(lc, dict):
                for x in list(lc.get("not_in_warehouse") or []) + list(lc.get("empty_lookups") or []):
                    out.update({str(x), f"{lc.get('object')}.{x}"})
        for note in res.get("notes") or []:
            for clause in re.split(r"[;]", str(note)):
                if re.search(r"not\s+in\s+the\s+warehouse|not\s+synced|blank\s+on\s+every|no\s+records\s+in\s+the\s+warehouse|"
                             r"does\s+not\s+record|has\s+no\s+table", clause, re.IGNORECASE):
                    texts.append(clause)
    for t in texts:
        for tok in _API_TOKEN_RE.findall(t):
            out.update({tok, tok.split(".")[-1]})
    return out


#: A claim that a field or lookup holds no data ("is empty", "has no data", "is blank on every record", "never filled").
_NO_DATA_CLAIM_RE = re.compile(r"\b(?:(?:is|are)\s+(?:empty|blank|unused|never\s+(?:filled|set|used))|ha(?:s|ve)\s+no\s+"
                               r"(?:data|values?)|blank\s+on\s+(?:every|all)|(?:never|not)\s+filled)\b", re.IGNORECASE)


def _side_unavailable(rec: RecordAnswer) -> set[str]:
    """API names the record facts list only as other lookups or fields that are not in the warehouse or are empty, which
    no query used: lookup_check ``not_in_warehouse`` and ``empty_lookups``, describe_fields ``not_queryable``
    ``documented_not_synced`` and lookup groups marked ``not_in_warehouse`` (each also as ``<Obj>.<Field>``)."""
    out: set[str] = set()
    for e in rec.evidence:
        if not (e.ok and isinstance(e.model_result, dict)):
            continue
        res = e.model_result
        for lc in res.get("lookup_check") or []:
            if isinstance(lc, dict):
                for x in list(lc.get("not_in_warehouse") or []) + list(lc.get("empty_lookups") or []):
                    out.update({str(x), f"{lc.get('object')}.{x}"})
        nq = res.get("not_queryable") if isinstance(res.get("not_queryable"), dict) else {}
        for x in nq.get("documented_not_synced") or []:
            out.update({str(x), f"{e.object}.{x}"})
        for g in res.get("lookup_groups") or []:
            for f in (g.get("fields") or []) if isinstance(g, dict) else []:
                if isinstance(f, dict) and f.get("not_in_warehouse") and f.get("field"):
                    out.update({str(f["field"]), f"{e.object}.{f['field']}"})
    return out


#: Words of an API name that say nothing about what it holds ("Interview_Support_Person__c" is about "support").
_API_FILLER_WORDS = frozenset({"by", "of", "the", "id", "c", "to", "for", "person", "persons", "user", "users", "record",
                               "records", "name"})


def _api_words(api: str) -> list[str]:
    """The lower-case words of an API name ("Final_Decision_By__c" -> final, decision, by; camel case split too)."""
    base = re.sub(r"__c$", "", str(api or "").split(".")[-1])
    return [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", base.replace("_", " "))]


def _same_stem(a: str, b: str) -> bool:
    """Two words of 4 letters or more that share their first 5 letters (or are equal): "supported" / "support",
    "trained" / "trainer"."""
    return len(a) >= 4 and len(b) >= 4 and (a == b or (len(a) >= 5 and len(b) >= 5 and a[:5] == b[:5]))


def _asked_unavailable(rec: RecordAnswer, field_check: list[str] | None, question: str = "") -> set[str]:
    """API names that are not there (not in the warehouse, no data) and that the question is about: the not_available
    errors, ``unsynced_fits`` (the question's words fit them), ``no_data_fields`` (a lookup the count filtered on),
    the FIELD CHECK lines, the record answer's own claims, and side names (:func:`_side_unavailable`) with a word that
    a question word shares its stem with ("supported" for ``Previous_Support_Person__c``, "decision" for
    ``Final_Decision_By__c``), filler words and the words of the queried objects' names aside ("interviews" says
    nothing about ``Interview_Support_Person__c`` on an interview object)."""
    out: set[str] = set()
    texts: list[str] = list(field_check or [])
    for e in rec.evidence:
        if _not_available(e):
            texts.append(str(e.error or ""))
        if not (e.ok and isinstance(e.model_result, dict)):
            continue
        for x in e.model_result.get("unsynced_fits") or []:
            if isinstance(x, dict) and x.get("field"):
                out.update({str(x["field"]), f"{e.object}.{x['field']}"})
        for x in e.model_result.get("no_data_fields") or []:
            out.update({str(x), str(x).split(".")[-1]})
    for t in texts:
        for tok in _API_TOKEN_RE.findall(t):
            out.update({tok, tok.split(".")[-1]})
    for pattern in (None, _NO_DATA_CLAIM_RE):
        for tok, _s in _claimed_unavailable(rec.answer or "", pattern):
            out.update({tok, tok.split(".")[-1]})
    q = set(re.findall(r"[a-z]+", (question or "").lower()))
    objects = {str(x) for e in rec.evidence for x in (e.object,) if x}
    for lc in lookup_checks(rec.evidence):
        objects.update(str(lc.get(k)) for k in ("object", "target") if lc.get(k))
    object_words = {w for o in objects for w in _api_words(o)}
    for tok in _side_unavailable(rec):
        words = [w for w in _api_words(tok) if w not in _API_FILLER_WORDS and w not in object_words]
        if any(_same_stem(w, x) for w in words for x in q):
            out.update({tok, tok.split(".")[-1]})
    return out


def side_caveats(text: str, rec: RecordAnswer, field_check: list[str] | None = None,
                 question: str = "") -> list[tuple[str, str]]:
    """``(API name, sentence)`` for each claim of ``text`` that a field or lookup is not in the warehouse or holds no
    data (:func:`_claimed_unavailable`) about a name the record facts list only on the side (:func:`_side_unavailable`:
    another lookup of the object that no query used) and that the question is not about (:func:`_asked_unavailable`):
    "`Final_Decision_By__c` is not in the warehouse" on every internal-interview answer."""
    side = _side_unavailable(rec)
    if not side:
        return []
    asked = _asked_unavailable(rec, field_check, question)
    claims = _claimed_unavailable(text) + _claimed_unavailable(text, _NO_DATA_CLAIM_RE)

    def is_asked(tok: str) -> bool:
        return tok in asked or tok.split(".")[-1] in asked

    with_asked = {_norm_sentence(sn) for t, sn in claims if is_asked(t)}   # a sentence about an asked name stays
    out: list[tuple[str, str]] = []
    for tok, sentence in claims:
        if is_asked(tok) or not (tok in side or tok.split(".")[-1] in side) or _norm_sentence(sentence) in with_asked:
            continue
        if all(t != tok for t, _s in out):
            out.append((tok, sentence))
    return out


def _norm_sentence(text: str) -> str:
    """A sentence without its list marker, spaces collapsed (to compare sentences cut from different line shapes)."""
    return " ".join(re.sub(r"^\s*(?:[-*+\u2022]|\d+[.)])\s+", "", text or "").split())


def drop_side_caveats(text: str, rec: RecordAnswer, field_check: list[str] | None = None, question: str = "") -> str:
    """``text`` without the sentences of :func:`side_caveats` that hold no number (a line left empty is removed); a
    sentence with a number stays (:func:`merge_scope_issues` then rejects the merge)."""
    bad = {_norm_sentence(sn) for _t, sn in side_caveats(text, rec, field_check, question) if not numbers_in(sn)}
    if not bad:
        return text
    out: list[str] = []
    for ln in (text or "").split("\n"):
        if _is_source_line(ln) or not ln.strip():
            out.append(ln)
            continue
        m = re.match(r"^(\s*(?:[-*+]|\d+[.)])\s+)", ln)
        prefix, rest = (m.group(1), ln[m.end():]) if m else ("", ln)
        kept = [x for x in _SENTENCE_SPLIT_RE.split(rest) if x.strip() and _norm_sentence(x) not in bad]
        if kept:
            out.append(prefix + " ".join(kept))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def drop_record_side_caveats(text: str, evidence: list[QueryEvidence], question: str = "") -> str:
    """The record answer without the number-free caveats of :func:`drop_side_caveats` (a lookup the question is not
    about is not in the warehouse or has no data: "`Final_Decision_By__c` is not in the warehouse." on a count of
    interviews conducted). The record answer's own claims do not count as asked here (they are what is checked); a
    caveat about what the question's words fit stays (:func:`_asked_unavailable`)."""
    probe = RecordAnswer(question=question, answer="", no_records=False, used_data=True, evidence=evidence)
    return drop_side_caveats(text, probe, None, question)


#: A sentence that says a name is not stored ("The name 'X' is not stored in the system; the closest match is Y",
#: "No Employee record is named 'X'", "'X' does not exist in the records").
_NOT_STORED_RE = re.compile(r"\bnot\s+(?:stored|found|recorded|present|in\s+the\s+(?:system|records|data|warehouse))\b|"
                            r"\bno\s+(?:[\w/,]+\s+){0,6}?(?:records?|one|person|people|employees?|users?|candidates?)\s+"
                            r"(?:is\s+|are\s+)?(?:named|called)\b|\b(?:does|do)\s+not\s+(?:exist|appear)\b|"
                            r"\bclosest\s+(?:stored\s+)?(?:match|name|spelling)\b", re.IGNORECASE)
#: The sentence is about a person's name ("named", "the name", "spelling"), not a value or a field.
_NAME_WORD_RE = re.compile(r"\b(?:named|called|names?|spell(?:ing|ed|t)?|closest)\b", re.IGNORECASE)


def name_notes(rec_answer: str, question: str, names: Any = ()) -> list[tuple[str, str]]:
    """``(asked spelling, sentence)``, the first sentence of the record answer for each name the question gives that it
    says is not stored (:data:`_NOT_STORED_RE` with a name word, :data:`_NAME_WORD_RE`; the name, two words or more,
    quoted in the sentence and found in the question as written, case and spaces aside; no number beyond the
    question's): the record agent was asked to say so first when it used the closest stored name
    (``RECORDS_NOT_EXACT_NAME_MESSAGE``). A quoted value ('Role = "Support"' in a not-in-the-warehouse sentence) is
    none. ``names``: one-word names of the question that the record tools read as another, stored name (the spelling
    help, docs/resolver_integration.md (6); () by default): such a name counts too, though it has one word only."""
    q = " ".join(re.sub(r"[^\w\s'-]", " ", (question or "").lower()).split())
    one_word = {w for w in (" ".join(re.sub(r"[^\w\s'-]", " ", str(n).lower()).split()) for n in names or ()) if w}
    asked_nums = set(numbers_in(question))
    out: list[tuple[str, str]] = []
    for line in _body(rec_answer or "").split("\n"):
        rest = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line)
        for sent in re.split(r"(?<=[.!?])\s+", rest):
            if not (_NOT_STORED_RE.search(sent) and _NAME_WORD_RE.search(sent)) \
                    or any(n not in asked_nums for n in numbers_in(sent)):
                continue
            for span in quoted_spans(sent):
                asked = span.strip("`\"'“”")
                name = " ".join(re.sub(r"[^\w\s'-]", " ", asked.lower()).split())
                if (len(name.split()) >= 2 or name in one_word) \
                        and re.search(r"(?<![\w])" + re.escape(name) + r"(?![\w])", q) \
                        and all(a.lower() != asked.lower() for a, _s in out):
                    out.append((asked, sent.strip()))
                    break
    return out


def with_name_note(text: str, rec_answer: str, question: str, names: Any = ()) -> str:
    """The merged ``text`` led by the record answer's sentence that the asked spelling of a name is not stored
    (:func:`name_notes`) when the merged lead does not say so (the merge moved it into a bullet or dropped it): the
    sentence goes first, and merged sentences that repeat it (they quote the asked spelling and say it is not stored,
    with no count) are removed. Unchanged when the record answer has no such sentence. ``names``: the one-word names
    the record tools corrected (:func:`name_notes`; () by default)."""
    notes = name_notes(rec_answer, question, names) if names else name_notes(rec_answer, question)
    if not notes or not (text or "").strip():
        return text
    asked_nums = set(numbers_in(question))
    lead = _lead(text)
    if _NOT_STORED_RE.search(lead) and all(asked.lower() in lead.lower() for asked, _s in notes):
        return text
    out: list[str] = []
    for ln in text.split("\n"):
        if _is_source_line(ln) or not ln.strip():
            out.append(ln)
            continue
        m = re.match(r"^(\s*(?:[-*+]|\d+[.)])\s+)", ln)
        prefix, rest = (m.group(1), ln[m.end():]) if m else ("", ln)
        kept = [x for x in re.split(r"(?<=[.!?])\s+", rest)   # a repeat of the note; a sentence with a count stays
                if x.strip() and not (_NOT_STORED_RE.search(x) and any(a.lower() in x.lower() for a, _s in notes)
                                      and all(n in asked_nums for n in numbers_in(x)))]
        if kept:
            out.append(prefix + " ".join(kept))
    head = " ".join(dict.fromkeys(s for _a, s in notes))
    body = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return head + "\n\n" + body if body else head


def warehouse_availability(catalog: Any) -> Any:
    """A callable ``(api, context_objects) -> True | False | None`` over an open catalog (``RecordTools.catalog``):
    True when the warehouse holds ``api`` with data (an object with a table and records; a field synced and filled on
    every object of ``context_objects`` that has it, or on every object that has it when none of them does; an
    ``<Obj>.<Field>`` pair synced and filled), False when it is documented but not synced, or synced without data,
    None when unknown (also for a name that is both an object and a field of a context object)."""
    objects = getattr(catalog, "objects", None) or {}

    def field_state(spec: Any, f: str) -> bool | None:
        if f in (getattr(spec, "not_synced", None) or {}):
            return False
        fs = (getattr(spec, "fields", None) or {}).get(f)
        if fs is None:
            return None
        return bool(getattr(fs, "filled", 0) and getattr(fs, "filled", 0) > 0)

    def avail(api: str, context: Any = ()) -> bool | None:
        api = str(api or "")
        parts = api.split(".")
        if len(parts) >= 2 and parts[0] in objects and len(parts) == 2:
            spec = objects[parts[0]]
            if not getattr(spec, "has_table", False):
                return False
            return field_state(spec, parts[1])
        name = parts[-1]
        ctx = [objects[o] for o in dict.fromkeys(context or ()) if o in objects]
        holders = [sp for sp in ctx if field_state(sp, name) is not None]
        if name in objects and len(parts) == 1:
            spec = objects[name]
            obj_ok = bool(getattr(spec, "has_table", False) and getattr(spec, "rows", 0))
            if holders:   # also a field of an object in context: ambiguous unless both are there
                return True if obj_ok and all(field_state(sp, name) for sp in holders) else None
            return obj_ok
        if not holders:
            holders = [sp for sp in objects.values() if field_state(sp, name) is not None]
        if not holders:
            return None
        states = [field_state(sp, name) for sp in holders]
        if all(states):
            return True
        return False if not any(states) else None

    return avail


def availability_issues(text: str, rec: RecordAnswer, field_check: list[str] | None = None,
                        availability: Any = None) -> list[str]:
    """Objects and fields that ``text`` calls not available (not in the warehouse, not synced, not existing) although
    the record facts do not say so (:func:`_unavailable_in_facts`) and the warehouse holds them with data
    (``availability``, :func:`warehouse_availability`). Without ``availability`` only an object a successful data
    query ran on (or a field one used) is known to be there. A claim the record answer makes too is left alone (the
    merge only repeats it). One line per name."""
    claims = _claimed_unavailable(text)
    if not claims:
        return []
    allowed = _unavailable_in_facts(rec, field_check)
    rec_claims = {t for t, _s in _claimed_unavailable(rec.answer or "")}
    queried = {str(e.object) for e in rec.evidence if e.ok and e.kind == "data" and e.object}
    used = {f for e in rec.evidence if e.ok and e.kind == "data" for _o, fld in _fields_used(e) for f in fld.split(".")}
    context = list(queried) + [str(e.object) for e in rec.evidence if e.object]
    out: list[str] = []
    for tok, sentence in claims:
        if tok in allowed or tok.split(".")[-1] in allowed or _claimed_alike(tok, rec_claims):
            continue
        here = [t for t in _API_TOKEN_RE.findall(sentence) if "." not in t]
        if availability is not None:
            try:
                present = availability(tok, here + context)
            except Exception:  # noqa: BLE001 - the check is best effort
                present = None
        else:
            present = True if (tok in queried or tok.split(".")[-1] in used) else None
        if present:
            out.append(f"that {tok} is not available, but the warehouse holds it")
    return out


#: A system lookup named by API name or role words (creator, last editor, owner): never part of ``any_role``.
_SYSTEM_ROLE_RE = re.compile(r"(?<![A-Za-z0-9_])(?:CreatedById|LastModifiedById|OwnerId|(?:record\s+)?creators?|"
                             r"created\s+by|last\s+(?:editors?|modifier|modified\s+by)|(?:record\s+)?owners?)"
                             r"(?![A-Za-z0-9_])", re.IGNORECASE)
#: Wording that leaves something out of a count ("creator, last editor and owner not included").
_LEFT_OUT_RE = re.compile(r"\bnot\s+(?:included|counted|part)\b|\bexclud\w*|\bexcept\b|\bother\s+than\b|"
                          r"\bapart\s+from\b|\bwithout\b|\bbesides\b|\bleft\s+out\b", re.IGNORECASE)


def _any_role_scope_clause(clause: str, evidence: list[QueryEvidence]) -> bool:
    """``clause`` gives a lookup check's ``any_role`` count (a number no other count of that check holds) as a count
    that includes a system lookup (creator, last editor, owner, CreatedById, LastModifiedById, OwnerId): any_role
    leaves those out. A clause that says they are left out, or that also gives a system lookup's own count, is fine."""
    clean = _DATE_TIME_RE.sub(" ", clause or "")
    if not _SYSTEM_ROLE_RE.search(clean) or _LEFT_OUT_RE.search(clean):
        return False
    nums = set(numbers_in(clean))
    for lc in lookup_checks(evidence):
        n = _num(lc.get("any_role"))
        if n is None or n == "0" or n not in nums:
            continue
        rest = [lc.get("records")] + [x.get("records") for key in ("others", "elsewhere", "by_record")
                                      for x in lc.get(key) or [] if isinstance(x, dict)]
        held = lc.get("all_records")
        rest += list(held.values()) if isinstance(held, dict) else []
        if n in {_num(v) for v in rest}:
            continue   # the number is also another lookup's count: the clause may give that one
        system = {_num(o.get("records")) for o in lc.get("others") or [] if isinstance(o, dict)
                  and str(o.get("field")) in _SYSTEM_LOOKUPS and _count(o.get("records")) > 0}
        if not (system & nums):
            return True
    return False


#: A bracket item that names a system lookup: a system role, or "editor" / "modifier" alone (the item after the "/" of
#: "User creator/editor").
_SYSTEM_ITEM_RE = re.compile(_SYSTEM_ROLE_RE.pattern + r"|^\s*(?:last\s+)?(?:editors?|modifiers?)\s*$", re.IGNORECASE)


def _drop_system_items(clause: str) -> str:
    """``clause`` without the system lookups (creator, last editor, owner) listed in its brackets: "(support,
    recruiter, creator, last editor): 474" -> "(support, recruiter): 474"; items are split at ",", ";", "and", "+"
    and "/" ("(Employee lookups + User creator/editor): 474" -> "(Employee lookups): 474", review of 2026-10-22). A
    bracket that lists nothing else is kept."""
    def fix(m: re.Match) -> str:
        items = [it for it in re.split(r"\s*[,;+/]\s*|\s+and\s+", m.group(1)) if it.strip()]
        keep = [it for it in items if not _SYSTEM_ITEM_RE.search(it)]
        if len(items) < 2 or not keep or len(keep) == len(items):
            return m.group(0)
        return "(" + ", ".join(keep) + ")"
    return re.sub(r"\(([^()\n]*)\)", fix, clause)


def fix_any_role_scope(text: str | None, evidence: list[QueryEvidence]) -> str:
    """``text`` with the system lookups taken out of the bracketed scope list of a clause that gives the any_role count
    (:func:`_any_role_scope_clause`: "In any of the Employee lookups (support, recruiter, creator, last editor): 474"
    -> "... (support, recruiter): 474"). Source lines and numbers are never touched; a clause it cannot fix stays (the
    merge then rejects it, :func:`merge_scope_issues`)."""
    body = text or ""
    if not lookup_checks(evidence) or not _SYSTEM_ROLE_RE.search(_body_text(body)):
        return body
    out: list[str] = []
    for ln in body.split("\n"):
        if _is_source_line(ln):
            out.append(ln)
            continue
        parts = re.split(r"((?<=[.!?;])\s+)", ln)
        out.append("".join(_drop_system_items(p) if _any_role_scope_clause(p, evidence) else p for p in parts))
    return "\n".join(out)


#: Wording that says a person is held by a lookup ("appears in", "exist", "is also listed as").
_PRESENT_RE = re.compile(r"\b(?:appears?|appearing|exists?|existing|holds?|held|referenced|associated|"
                         r"(?:is|are)\s+also|also\s+(?:appears?|listed|named|recorded|referenced|set|in|on|as))\b",
                         re.IGNORECASE)
#: Wording that says the person is not there ("no records", "never", "not listed").
_ABSENT_RE = re.compile(r"\b(?:no|none|never|neither|nor|zero|without|empty|blank)\b|n't\b|\bnot\s+(?:in|on|held|"
                        r"holding|listed|named|found|referenced|linked|associated|recorded|present|appear\w*|hold\w*|"
                        r"exist\w*|filled|set|used)\b", re.IGNORECASE)


def _held_lookups(evidence: list[QueryEvidence]) -> set[tuple[str, str]]:
    """(object, lookup) pairs a result shows holding the person (a count above 0): a lookup check's own count, its
    others, elsewhere and all_records entries, and find_references references."""
    out: set[tuple[str, str]] = set()
    for lc in lookup_checks(evidence):
        obj = str(lc.get("object"))
        if _count(lc.get("records")) > 0:
            out.add((obj, str(lc.get("field"))))
        out.update((obj, str(o.get("field"))) for o in lc.get("others") or []
                   if isinstance(o, dict) and _count(o.get("records")) > 0)
        out.update((str(x.get("object")), str(x.get("field"))) for x in lc.get("elsewhere") or []
                   if isinstance(x, dict) and _count(x.get("records")) > 0)
        held = lc.get("all_records")
        out.update((obj, str(f)) for f, v in (held.items() if isinstance(held, dict) else []) if _count(v) > 0)
    for e in evidence:
        if e.ok and e.kind == "data" and e.tool in PERSON_TOOLS and isinstance(e.model_result, dict):
            for key in ("references", "system_references"):
                out.update((str(r.get("object")), str(r.get("field"))) for r in e.model_result.get(key) or []
                           if isinstance(r, dict) and _count(r.get("records")) > 0)
    return out


#: A clause about the person without the name ("she also appears ...").
_PERSON_WORD_RE = re.compile(r"\b(?:she|he|they|her|him|them|this\s+person|the\s+person|this\s+employee|"
                             r"the\s+employee)\b", re.IGNORECASE)


def _zero_lookup_presence(clause: str, evidence: list[QueryEvidence]) -> str | None:
    """The lookup a clause says holds the person ("Other lookups for <name> ... (`Interview_Support_Person__c`, ...)
    exist") while its lookup check counts 0 for the person and no result shows the person in it
    (:func:`_held_lookups`), else None. The clause must be about the person (the check's name or a pronoun: a sentence
    about what the field holds is no claim about the person). A clause that says the person is absent, or gives a 0,
    is fine."""
    clean = _DATE_TIME_RE.sub(" ", clause or "")
    if not _PRESENT_RE.search(clean) or _ABSENT_RE.search(clean) or "0" in numbers_in(clean):
        return None
    held = _held_lookups(evidence)
    checks = _person_checks(evidence)
    objects = _object_names(checks)   # "linked to `Recruiter__c`" names the Employee object, not that lookup
    norm = _norm_text(clean)
    for lc in checks:
        obj = str(lc.get("object"))
        if not (_PERSON_WORD_RE.search(clean) or any(_has_name(norm, n) for n in _filter_names(lc))):
            continue
        for o in _same_target(lc):
            f = str(o.get("field") or "")
            if f and o.get("records") == 0 and (obj, f) not in held and _names_field(clean, f, o.get("role"), objects):
                return f
    return None


#: "none of these", "neither of them": a quantifier that places no count inside the counted records.
_NEG_QUANT_RE = re.compile(r"\b(?:none|neither|any|no\s+one|not\s+one)\s+$", re.IGNORECASE)


def _restated_overlap(clause: str, rec_answer: str, evidence: list[QueryEvidence]) -> bool:
    """The merged overlap clause (:func:`_overlap_clause`) only restates the record answer: each of its overlap words
    is a negated quantifier ("none of these records", which places no count inside the counted records), it presents
    no difference of two counts as extra records, and one clause of the record answer holds all of its numbers and
    names every lookup the clause names with a count of a lookup check or a reference (the same side count with the
    same lookup)."""
    clean = _DATE_TIME_RE.sub(" ", clause or "")
    nums = set(numbers_in(clean))
    if not nums or (_EXTRA_RE.search(clean) and _derived_counts(evidence) & _extra_numbers(clean)):
        return False
    if any(not _NEG_QUANT_RE.search(clean[:m.start()]) for m in _OVERLAP_RE.finditer(clean)):
        return False
    fields: set[str] = set()
    for lc in lookup_checks(evidence):
        for x in [lc] + [o for key in ("others", "elsewhere") for o in lc.get(key) or [] if isinstance(o, dict)]:
            if _num(x.get("records")) in nums and x.get("field") and _names(clean, str(x.get("field"))):
                fields.add(str(x.get("field")))
    for e in evidence:
        if e.ok and e.kind == "data" and e.tool in PERSON_TOOLS and isinstance(e.model_result, dict):
            for r in e.model_result.get("references") or []:
                if isinstance(r, dict) and _num(r.get("records")) in nums and r.get("field") \
                        and _names(clean, str(r.get("field"))):
                    fields.add(str(r.get("field")))
    if not fields:
        return False
    for rc in _CLAUSE_SPLIT_RE.split(_body(rec_answer or "")):
        rclean = _DATE_TIME_RE.sub(" ", rc)
        if nums <= set(numbers_in(rclean)) and all(_names(rclean, f) for f in fields):
            return True
    return False


def _clause_words(text: str) -> str:
    """``text`` as its lower-case words, numbers and API names, one space apart: markdown, list markers and
    punctuation left out, so the same clause in another line shape compares equal."""
    return " ".join(w for w in (t.strip("_") for t in re.findall(r"[a-z0-9_]+", (text or "").lower())) if w)


def _in_record_answer(clause: str, rec_answer: str, same_claim: Any = None) -> bool:
    """The merged clause is also in the record answer (evaluation fix 3, 2026-10-22): one clause of the record answer
    (:data:`_CLAUSE_SPLIT_RE` over its body) holds its words in the same order (:func:`_clause_words`) and, with
    ``same_claim`` (a test of one clause), makes the same claim, so a clause that drops the record answer's negation
    ("No record shows that he is also referenced in X" -> "He is also referenced in X") is not the record answer's
    (review of 2026-10-22). The record answer a rejected merge falls back on says the same, so such a clause is no
    reason to reject the merge (the idea of :func:`_restated_overlap`)."""
    key = _clause_words(clause)
    if not key:
        return False
    for rc in _CLAUSE_SPLIT_RE.split(_body(rec_answer or "")):
        if f" {key} " in f" {_clause_words(rc)} " and (same_claim is None or same_claim(rc)):
            return True
    return False


def merge_scope_issues(text: str, rec: RecordAnswer, question: str = "", field_check: list[str] | None = None,
                       availability: Any = None) -> list[str]:
    """What a merged answer states beyond the record facts' scope (empty when nothing):
    a condition on a field that no query used and the record answer does not name ("records where `X__c` is
    populated", taken from the knowledge answer), and numbers of the merged answer's first sentence that are not in
    the record answer, the question or the FIELD CHECK lines (the merge replaced the record answer's lead count with
    another count of the facts, for example a count of another object), a claim that the person is absent from
    an object no query counted (:func:`_uncounted_none_claims`), a claim that an object or field is not available
    that the record facts do not make while the warehouse holds it (:func:`availability_issues`), a clause that
    relates counts of different lookups to each other ("of these", "on these 79 sessions ... 84", "N additional";
    :func:`_overlap_clause`, the record drafts' overlap check) that the record answer does not have (neither as
    the same text nor with the same numbers and lookups in one clause, :func:`_restated_overlap`), the any_role count
    given with creator, last editor or owner (:func:`_any_role_scope_clause`, after :func:`fix_any_role_scope`), a
    clause that places the person in a lookup whose count is 0 (:func:`_zero_lookup_presence`), and a caveat
    that a field or lookup the question is not about is not in the warehouse or has no data (:func:`side_caveats`).
    The count-0 clause counts only when no clause of the record answer holds it with the same claim
    (:func:`_in_record_answer`, evaluation fix 3): the record answer a rejected merge falls back on says it too. The
    any_role clause always counts (review of 2026-10-22: fix 3 is the count-0 check only; :func:`fix_any_role_scope`
    takes the system lookups out of both texts first, so the record answer's own clause is fixed, not let through)."""
    issues: list[str] = []
    allowed = _scope_fields(rec)
    for m in _FILTER_CLAIM_RE.finditer(_body(text or "")):
        parts = [p for p in m.group(1).split(".") if p.endswith("__c") or p in _SYSTEM_LOOKUPS + ("RecordTypeId",)]
        bad = [p for p in parts if p not in allowed]
        if bad:
            issues.append(f"a condition on {', '.join(bad)} that no query applied")
    lead = _DATE_TIME_RE.sub(" ", _lead(text or ""))
    new = audit_numbers(lead, [rec.answer or "", question] + [str(x) for x in field_check or []])
    if new:
        issues.append(f"lead numbers {', '.join(new)} that the record answer does not give")
    for obj in _uncounted_none_claims(text, rec):
        issues.append(f"an absence on {obj}, which no query counted")
    issues += availability_issues(text, rec, field_check, availability)
    clause = _overlap_clause(text, rec.evidence)
    if clause and clause not in " ".join((rec.answer or "").split()) and \
            not _restated_overlap(clause, rec.answer or "", rec.evidence):
        issues.append(f'an overlap of counts that no result states ("{clause[:120]}")')
    for part in _CLAUSE_SPLIT_RE.split(_body(text or "")):
        if _any_role_scope_clause(part, rec.evidence):
            issues.append(f'the count in any role with creator, last editor or owner, which it leaves out '
                          f'("{" ".join(part.split())[:120]}")')
            break
    for part in _CLAUSE_SPLIT_RE.split(_body(text or "")):
        zero = _zero_lookup_presence(part, rec.evidence)
        if zero and not _in_record_answer(part, rec.answer, lambda rc, z=zero: _zero_lookup_presence(rc, rec.evidence)
                                          == z):   # the record answer's own clause, with the same claim (fix 3)
            issues.append(f'the person in {zero}, whose count is 0 ("{" ".join(part.split())[:120]}")')
            break
    for tok, _sentence in side_caveats(text, rec, field_check, question):
        issues.append(f"a caveat about {tok}, which the question does not ask about")
    return list(dict.fromkeys(issues))


#: The last words of a line cut off mid-sentence: a function word, an open date ("2024-0"), a hyphen or a comma.
_DANGLING_END_RE = re.compile(r"(?:\b(?:the|a|an|of|and|or|to|with|in|for|by|on|at|as|from|is|are|was|were|than|that|"
                              r"which|who|whose|its|their|his|her)|\b\d{4}-\d{0,2}|\d{4}-\d{2}-\d?|[-,(\u2014\u2013])$",
                              re.IGNORECASE)


def _finish_reason(resp: Any) -> str:
    """The chat result's finish reason ("stop", "length"), from the result dict or its raw response; "" when unknown."""
    if not isinstance(resp, dict):
        return ""
    reason = resp.get("finish_reason")
    if not reason:
        try:
            reason = resp["raw"].choices[0].finish_reason
        except Exception:  # noqa: BLE001 - optional
            reason = ""
    return str(reason or "")


def merge_cut_off(resp: Any, text: str | None, max_tokens: int = MERGE_MAX_TOKENS) -> bool:
    """The merge reply was cut off: the call stopped at its token limit (finish reason "length", or as many completion
    tokens as the limit), or the reply has no Source line (the prompt ends every answer with one) and its last line
    ends mid-sentence (:data:`_DANGLING_END_RE`, an unclosed bracket, bold or code span)."""
    if _finish_reason(resp) == "length":
        return True
    usage = resp.get("usage") if isinstance(resp, dict) else None
    try:
        if isinstance(usage, dict) and max_tokens and int(usage.get("completion_tokens") or 0) >= max_tokens:
            return True
    except (TypeError, ValueError):
        pass
    lines = [ln for ln in (text or "").split("\n") if ln.strip()]
    if not lines or any(_is_source_line(ln) for ln in lines):
        return False
    last = lines[-1].rstrip()
    core = last.rstrip(" *_")
    return bool(_DANGLING_END_RE.search(core) or last.count("(") > last.count(")") or last.count("**") % 2 == 1
                or last.count("`") % 2 == 1)


def combine(llm: Any, question: str, knowledge_answer: str, rec: RecordAnswer, status: RecordsStatus,
            field_check: list[str] | None = None, availability: Any = None,
            knowledge_numbers: bool = True, name_words: Any = (), record_text: str | None = None,
            table_note: str = "") -> MergeResult:
    """Merge the knowledge answer and the record answer in one model call, then audit the numbers and the scope.

    Never raises. ``rec.no_records`` gives an empty text (the caller prints the knowledge answer), and so does a record
    answer with nothing but its Source line (:func:`~sf_assistant.agent.nudges.answer_text`, evaluation fix 2: error
    "the record answer is empty", no model call); an
    empty knowledge answer gives the record answer without a model call; a failed call, an empty
    reply (or one with nothing but its Source line), a number that is not in the sources, or a scope issue
    (:func:`merge_scope_issues`: a condition
    no query applied, a lead count the record answer does not give) gives the record answer with
    ``merged=False`` (``error`` says which scope issue).
    ``field_check`` (:func:`field_check_lines`) goes into the user message as FIELD CHECK; its numbers
    come from the record facts, so the audit sources do not change. The knowledge answer is passed without what it
    says about fetching records (:func:`clean_knowledge_answer`). The merged text loses working sentences
    (:func:`drop_working_sentences`), tool talk (:func:`drop_tool_talk`), system lookups in an any_role scope
    (:func:`fix_any_role_scope`), the about_people reference codes (:func:`drop_ref_codes`) and number-free caveats
    about fields the question is not about (:func:`drop_side_caveats`); a merged text that still reads as working notes
    (:func:`working_notes`, which includes the merge's own input names such as "RECORD FACTS"), still names a reference
    code (:func:`ref_codes_left`) or was cut off (:func:`merge_cut_off`, the call has :data:`MERGE_MAX_TOKENS`) is
    rejected. A merged text whose lead does not say that the asked spelling of a name is
    not stored gets the record answer's sentence first (:func:`with_name_note`). When the record answer says that the
    name, field or object is not in the records (:func:`_no_data_but_not_found`), a merged text with any number beyond
    the question's (0 included; dates and the Source line aside) is rejected. ``availability``
    (:func:`warehouse_availability`) lets the scope check catch "not available" claims
    about objects and fields the warehouse holds. An accepted text without a Source line gets one
    (:func:`with_source_line`). ``knowledge_numbers=False`` (the lean facts block, RECORDS_SPEC §21.19, which carries no
    record numbers): the number audit takes from the knowledge answer only the numbers of its quoted values and API
    names (:func:`quoted_spans`), not those of its prose, so a snapshot count the knowledge quotes cannot pass as a
    record count. ``name_words`` (the spelling help, ``SFA_RESOLVER=on``; () by default): the one-word names of the
    question the record tools read as another, stored name; the name note covers them too (:func:`with_name_note`).
    ``record_text`` and ``table_note`` (the code-built rows, ``SFA_TABLES=code``; latency plan step 3): the RECORD
    ANSWER the merge call gets instead of ``rec.answer`` (the record answer with its code rows as table lines,
    ``agent.tables.marked``) and a text appended to its system prompt (``agent.tables.COMBINE_TABLES_NOTE``); every
    check still reads ``rec.answer`` with its rows, and the caller puts the rows back (``agent.tables.restore``).
    Without them the request is byte for byte the one before.
    """
    t0 = time.perf_counter()
    if rec.no_records:
        return MergeResult(text="", merged=False)
    if not answer_text(rec.answer):   # nothing but a Source line: no record answer to merge or to fall back on (fix 2)
        return MergeResult(text="", merged=False, error="the record answer is empty",
                           seconds=round(time.perf_counter() - t0, 3))
    if not (knowledge_answer or "").strip():
        return MergeResult(text=rec.answer, merged=False, error="the knowledge agent gave no answer",
                           seconds=round(time.perf_counter() - t0, 3))
    knowledge = clean_knowledge_answer(knowledge_answer)
    if not knowledge:
        return MergeResult(text=rec.answer, merged=False, error="the knowledge answer only said how to fetch records",
                           seconds=round(time.perf_counter() - t0, 3))
    try:
        user = combine_user_message(question, knowledge, rec.answer if record_text is None else record_text,
                                    facts_json(rec.evidence, MERGE_FACTS_CHARS), field_check)
        resp = llm.chat([{"role": "system", "content": COMBINE_SYSTEM + table_note}, {"role": "user", "content": user}],
                        max_tokens=MERGE_MAX_TOKENS)
        raw = strip_reasoning((resp or {}).get("content"))
        cut = bool(raw) and merge_cut_off(resp, raw)
        codes = reference_codes(rec.evidence)
        text = drop_ref_codes(drop_tool_talk(drop_working_sentences(raw)), codes)
        text = fix_any_role_scope(drop_side_caveats(text, rec, field_check, question), rec.evidence)
        text = (with_name_note(text, rec.answer, question, name_words) if name_words
                else with_name_note(text, rec.answer, question))
    except Exception as exc:  # noqa: BLE001 - the record answer is still printed
        return MergeResult(text=rec.answer, merged=False, error=f"{type(exc).__name__}: {exc}"[:300],
                           seconds=round(time.perf_counter() - t0, 3))
    if not answer_text(text):   # empty, or nothing but a Source line (evaluation fix 2)
        return MergeResult(text=rec.answer, merged=False, error="empty merged answer",
                           seconds=round(time.perf_counter() - t0, 3))
    if cut:
        return MergeResult(text=rec.answer, merged=False, error="the merged answer was cut off (token limit or "
                           "mid-sentence end)", seconds=round(time.perf_counter() - t0, 3))
    left = ref_codes_left(text, codes, question)
    if left:
        return MergeResult(text=rec.answer, merged=False, error="the merged answer names internal reference codes ("
                           + ", ".join(left[:5]) + ")", seconds=round(time.perf_counter() - t0, 3))
    known = knowledge if knowledge_numbers else " ".join(quoted_spans(knowledge))
    bad = audit_numbers(text, [facts_json(rec.evidence, 10**9), known, question, status.line()])
    if bad:
        return MergeResult(text=rec.answer, merged=False, rejected_numbers=bad, seconds=round(time.perf_counter() - t0, 3))
    tri = getattr(rec, "triage", None) or {}
    if _no_data_but_not_found(rec.evidence, tri.get("people_data", not (tri.get("missing") and not tri.get("people")))):
        # the record answer says the name or field is not there: no count
        asked = set(numbers_in(question))
        counts = [n for n in numbers_in(_DATE_TIME_RE.sub(" ", _body_text(text))) if n not in asked]
        if counts:
            return MergeResult(text=rec.answer, merged=False,
                               error=f"the merged answer gives a count ({', '.join(dict.fromkeys(counts))}) although the "
                                     "record answer says the name or field is not in the records",
                               seconds=round(time.perf_counter() - t0, 3))
    text = with_source_line(text, rec.evidence, status)
    if working_notes(text):
        return MergeResult(text=rec.answer, merged=False, error="the merged answer reads as working notes",
                           seconds=round(time.perf_counter() - t0, 3))
    scope = merge_scope_issues(text, rec, question, field_check, availability)
    if scope:
        return MergeResult(text=rec.answer, merged=False, error="the merged answer states " + "; ".join(scope)[:280],
                           seconds=round(time.perf_counter() - t0, 3))
    return MergeResult(text=text, merged=True, seconds=round(time.perf_counter() - t0, 3))


# --------------------------------------------------------------------------- the lean-mode merge by code (2026-10-29)
# Latency plan step 2 (.private/latency_reduction_plan_2026-10-29.md O2; the owner's go of 2026-10-29: "at most 3
# relevant meaning bullets"). In lean mode the merge call only retyped the record answer (a median 0.984 similar on the
# page log) and added some field meanings of the facts block, so code adds those meanings and nothing else. Its review
# (same day) made relevance a matter of (object, field) pairs and added the checks the merge call's rewrite gave for
# free: no bullet about the question's people, the knowledge, the question or the tools, no cut-off bullet, the
# not-found count rule, a label line after a list, and the merge call for a record answer that still plans aloud.

#: The most field-meaning bullets :func:`code_merge` adds (the owner's choice: at most 3 relevant ones); read at call
#: time.
CODE_MERGE_MAX_BULLETS = 3
#: The longest bullet :func:`code_merge` adds, in characters; read at call time (review of 2026-10-29: a lean reply cut
#: at its token limit was one bullet of 2,277 characters naming about 50 objects; no bullet of the two saved standard
#: evaluation runs is longer than 400).
CODE_MERGE_MAX_CHARS = 400
#: The code line before the added bullets when the record answer's body ends with a list (review of 2026-10-29: the
#: page's renderer, like Markdown, reads bullets after a blank line as more items of that list; a line that is no list
#: item ends it).
CODE_MERGE_LABEL = _prompt_files.text("code_merge_label")
#: A bullet of the lean facts block as its prompt asks for it: "- " at the start of a line (an indented sub-bullet
#: would lose the line it belongs to, so it is none).
_FACT_BULLET_RE = re.compile(r"^-[ \t]+\S")
#: A span in backticks, and the name tokens inside one.
_TICKED_RE = re.compile(r"`([^`\n]+)`")
_NAME_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*(?![A-Za-z0-9_])")
#: An API name that is a field by its form: a custom name (``__c``, or its relationship name ``__r``), a system lookup
#: or RecordTypeId.
_FIELD_FORM_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*__[cr]|CreatedById|LastModifiedById|OwnerId|RecordTypeId")
#: An API path ("Interviewer__c.Name" in an applied filter, "Recruiter__c.Manager__c" in a link's path): its first two
#: names.
_API_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z][A-Za-z0-9_]*)\.([A-Za-z][A-Za-z0-9_]*)")
#: The first API name of each path of an "applied" block: a field of the call's object ("Interviewer__c.Name = ...").
_APPLIED_FIELD_RE = re.compile(r"(?<![A-Za-z0-9_.])([A-Za-z][A-Za-z0-9_]*__c|CreatedById|LastModifiedById|OwnerId|"
                               r"RecordTypeId)(?![A-Za-z0-9_])")
#: A bullet that puts a field on an object: "`Field` on `Object`", "`Field` is the lookup field on `Object`" (between
#: the two spans nothing, or words with a field word: :data:`_FIELD_WORD_RE`); "`Object`'s `Field`" below.
_ON_OBJECT_RE = re.compile(r"`(?P<field>[A-Za-z][A-Za-z0-9_]*)`(?P<mid>(?:[^`\n;.]|\.(?=\S)){0,60}?)\b(?:on|of|in)\s+"
                           r"(?:the\s+)?`(?P<obj>[A-Za-z][A-Za-z0-9_]*)`", re.IGNORECASE)
_OWNS_TICKED_RE = re.compile(r"`(?P<obj>[A-Za-z][A-Za-z0-9_]*)`(?:\s+object)?['\u2019]s\s+(?:own\s+)?"
                             r"`(?P<field>[A-Za-z][A-Za-z0-9_]*)`")
_FIELD_WORD_RE = re.compile(r"\b(?:field|lookup|picklist|column|checkbox|boolean|reference|counter|formula)\b",
                            re.IGNORECASE)
#: The words before a span that make it the target of a lookup ("a lookup to `Recruiter__c` on `Session__c`"): the
#: object a field points to, never the one it is on.
_TARGET_BEFORE_RE = re.compile(r"\b(?:to|references?|referencing)\s+(?:the\s+)?$", re.IGNORECASE)
#: The words next to an object's name in backticks that make it the lookup field named after that object ("superseded by
#: the lookup `Recruiter__c`", "`Recruiter__c` is the lookup field that ...", "`Recruiter__c` = lookup to ...").
_FIELD_BEFORE_RE = re.compile(r"\b(?:lookup|field|reference)(?:\s+field)?\s+$", re.IGNORECASE)
_FIELD_AFTER_RE = re.compile(r"\s*(?:(?:is|=)\s+)?(?:(?:a|an|the)\s+)?(?:[\w-]+\s+){0,2}?(?:lookup|field|reference|"
                             r"picklist)\b", re.IGNORECASE)
#: What a lean bullet says that is no field meaning (review of 2026-10-29, the page log and the saved evaluation runs):
#: talk about the knowledge, the documentation or the schema and what they do not hold ("The knowledge base contains
#: no specific records ...", "not documented", "the provided snippets"), about the question ("The question names a
#: person but ...", never "the question bank") or the prompt ("from_target"); a claim that no field links or names
#: someone ("none of which link
#: these two individuals", "has no documented lookup field", "cannot be determined"); advice ("one must first locate
#: ...", "should be used") and guesses ("would be found", "if such", "probably"). Step-2 fixes of 2026-10-30 (F10e):
#: also the working-notes tails "fitting the 'supported' verb" and "(implied by 'guest-scoped')".
_NOT_MEANING_RE = re.compile(
    r"\bfitting\s+the\b|\bimplied\s+by\b|"
    r"\bknowledge\b|\bdocumentation\b|\bschema\b|\bsnippets?\b|\bchunks?\b|\bprovided\s+(?:tools?|results?|data|"
    r"information|definitions?|context)\b|\bobject\s+definitions?\b|\b(?:not|never)\s+(?:explicitly\s+)?documented\b|"
    r"\bundocumented\b|\bno\b[^.;\n]{0,60}\bdocumented\b|\bdocumented\s+(?:lookup|field|link|relationship)s?\b|"
    r"\bthe\s+question\s+(?:asks|asked\s+about|names|refers|specifies|mentions|is\s+about|does|wants|says|uses|"
    r"provides|gives|points|requests|concerns)\b|\bin\s+the\s+question\b|\bquestion's\b|"
    r"\bthe\s+user\s+(?:asks|asked|wants|means|is\s+asking|says|refers)\b|"
    r"\bfrom_target\b|\bnone\s+of\s+(?:which|them|these|those)\b|\bnot\s+detailed\b|"
    r"\bwould\s+be\s+(?:found|stored|linked|identified)\b|\bif\s+such\b|\bif\s+(?:they|he|she)\b|"
    r"\bcannot\s+be\s+(?:determined|identified|confirmed|established)\b|"
    r"\b(?:one|you|we)\s+(?:must|should|need\s+to|would\s+need\s+to|have\s+to)\b|\bshould\s+be\s+used\b|"
    r"\bwould\s+need\s+to\b|\b(?:presumably|probably|likely|potentially)\b|\bno\s+(?:specific|direct)\b|"
    r"\b(?:has|have|there\s+is|there\s+are)\s+no\s+(?:[\w-]+\s+){0,3}(?:fields?|lookups?|links?|relationships?)\b|"
    r"\bno\s+(?:[\w-]+\s+){0,2}(?:field|lookup|link|relationship)s?\s+(?:that\s+)?(?:links?|linking|references?|"
    r"referencing|connects?|connecting|ties|identif\w+|to\s+link)\b|\bno\s+(?:\w+\s+)?(?:field|lookup|object)s?\s+"
    r"(?:on|of|in)\s+\S+\s+(?:stores?|holds?|records?|tracks?|gives?|links?)\b|"
    r"\bmust\s+be\s+(?:derived|computed|calculated|inferred)\b|"
    r"\b(?:is|are)\s+not\s+(?:found|present|named|identified|mentioned|associated|mapped)\b|"
    r"\bdoes\s+not\s+(?:identify|map|name|specify|document|confirm|mention|directly)\b", re.IGNORECASE)
#: Words a bullet must repeat in a row from the lean step's prompt to echo it (:func:`_prompt_echo`).
_ECHO_WORDS = 6


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_']+", (text or "").lower())


#: Every run of :data:`_ECHO_WORDS` words of the lean step's prompt, without its examples (in backticks or brackets: a
#: bullet may say "`Interview__c` = client interviews" as the prompt's example does).
_LEAN_PROMPT_RUNS = frozenset(
    " ".join(w[i:i + _ECHO_WORDS]) for w in [_words(re.sub(r"`[^`]*`|\([^()]*\)", " ", KNOWLEDGE_LEAN_SYSTEM))]
    for i in range(len(w) - _ECHO_WORDS + 1))


def _prompt_echo(bullet: str) -> bool:
    """``bullet`` repeats :data:`_ECHO_WORDS` words in a row of the lean step's prompt (its API names left out): the
    model copied an instruction back ("the object that holds the records the question asks about, and what it is")."""
    w = _words(re.sub(r"`[^`]*`", " ", bullet))
    return any(" ".join(w[i:i + _ECHO_WORDS]) in _LEAN_PROMPT_RUNS for i in range(len(w) - _ECHO_WORDS + 1))


def _norm_api(name: str) -> str:
    """An API name with a relationship name read as its field (``Interviewer__r`` -> ``Interviewer__c``)."""
    return name[:-3] + "__c" if name.endswith("__r") else name


def _placed(text: str) -> set[tuple[str, str]]:
    """The (object, field) pairs ``text`` puts in backticks: ``<Obj>.<Field>`` (its first two names),
    :data:`_ON_OBJECT_RE` (not after "a lookup to") and :data:`_OWNS_TICKED_RE`; relationship names read as fields."""
    out: set[tuple[str, str]] = set()
    for span in _TICKED_RE.findall(text or ""):
        parts = span.strip().split(".")
        if len(parts) >= 2 and all(_NAME_TOKEN_RE.fullmatch(p) for p in parts):
            out.add((_norm_api(parts[0]), _norm_api(parts[1])))
    for m in _ON_OBJECT_RE.finditer(text or ""):
        mid = m.group("mid")
        if (mid.strip() and not _FIELD_WORD_RE.search(mid)) or _TARGET_BEFORE_RE.search(text[:m.start()]):
            continue
        out.add((_norm_api(m.group("obj")), _norm_api(m.group("field"))))
    out.update((_norm_api(m.group("obj")), _norm_api(m.group("field"))) for m in _OWNS_TICKED_RE.finditer(text or ""))
    return out


def _used_api(rec: RecordAnswer, objects: Any = ()) -> tuple[set[tuple[str, str]], set[str], set[str], set[str],
                                                             set[str]]:
    """``(pairs, said, scope, known, home)``: what the answer used, for :func:`_relevance` (review of 2026-10-29: by
    (object, field) pair, no longer any part of any path, table or object name).

    ``pairs``: the (object, field) pairs the record queries used: of each successful data query that is no person
    tool its ``fields_used`` (each hop of a path with its own object) and the first API name of each path of its
    "applied" block with its object; the lookup of each lookup check; the pairs the record answer writes
    (:func:`_placed`); and, only when no counting query ran (a people answer), the lookups of the person tool's
    references with records and of its links' paths (the person's system work is none). ``said``: the other names the
    record answer gives (in backticks, custom ``__c`` names, system lookups) that are no object. ``scope``: the objects
    a bullet that names no field may be about: the object of each successful counting query, and the objects the
    record answer names when no person tool ran (never a table a query joined, nor a person tool's read list).
    ``known``: every object the queries read or name and ``objects`` (the record catalog's), to tell an object from a
    field. ``home``: the objects a field the record answer names may be on (``scope``, the objects of ``pairs`` and
    those the record answer names)."""
    pairs: set[tuple[str, str]] = set()
    people_pairs: set[tuple[str, str]] = set()
    scope: set[str] = set()
    known: set[str] = {str(o) for o in objects or ()}
    people = counted = False
    for e in rec.evidence:
        if not (e.ok and e.kind == "data"):
            continue
        used = _fields_used(e)
        known.update(str(x) for x in [e.object, *(e.tables or [])] if x)
        known.update(o for o, _f in used)
        if e.tool in PERSON_TOOLS:
            people = True
            res = e.model_result if isinstance(e.model_result, dict) else {}
            people_pairs.update((str(r["object"]), str(r["field"])) for r in res.get("references") or []
                                if isinstance(r, dict) and r.get("object") and r.get("field")
                                and _count(r.get("records")) > 0)   # where the people are: the lookups with records
            for x in res.get("links") or []:   # how two of them are linked: the lookups of the link's path
                if isinstance(x, dict) and _count(x.get("records")) > 0:
                    people_pairs.update(_API_PATH_RE.findall(str(x.get("path") or "")))
            continue
        pairs.update(used)
        if e.object:
            side = json.dumps(e.applied or {}, ensure_ascii=False, default=str)
            pairs.update((str(e.object), f) for f in _APPLIED_FIELD_RE.findall(side))
            if e.tool in _COUNTING_TOOLS:
                counted = True
                scope.add(str(e.object))
    if not counted:   # what a people answer says comes from the person tool's references and links
        pairs |= people_pairs
    for lc in lookup_checks(rec.evidence):   # the lookup the person count used (never its "others")
        if lc.get("object") and lc.get("field"):
            pairs.add((str(lc["object"]), str(lc["field"])))
        known.update(str(lc[k]) for k in ("object", "target") if lc.get(k))
    known.update(o for o, _f in pairs | people_pairs)
    text = _body_text(rec.answer or "")
    placed = _placed(text)   # "The `Evaluator__c` field on `Interview_Evaluation__c` ...", "`Obj.Field`"
    pairs |= placed
    known.update(o for o, _f in placed)
    names = {t for span in _TICKED_RE.findall(text) for t in _NAME_TOKEN_RE.findall(span)}
    names.update(_CUSTOM_FIELD_RE.findall(text))
    names.update(f for f in _SYSTEM_LOOKUPS + ("RecordTypeId",) if _names(text, f))
    names = {_norm_api(n) for n in names}
    answer_objects = names & known
    if not people:   # a people answer names the object of every reference it gives
        scope |= answer_objects
    home = scope | answer_objects | {o for o, _f in pairs}
    return pairs, names - known, scope, known, home


def _bullet_api(bullet: str, known: set[str], fields: set[str]) -> tuple[set[tuple[str, str]], set[str], set[str]]:
    """``(placed, loose, objects)``: the API names ``bullet`` gives in backticks. ``placed``: the fields it puts on an
    object (:func:`_placed`); ``loose``: its other fields: a name that is a field by its form (:data:`_FIELD_FORM_RE`)
    or one of ``fields``, and no object of ``known`` unless the words next to it make it a field (:data:`
    _FIELD_BEFORE_RE`, :data:`_FIELD_AFTER_RE`: "superseded by the lookup `Recruiter__c`"); ``objects``: the objects of
    ``known`` it names otherwise. A lookup named after its object ("`Department__c` is the employee's department") is
    that field when ``fields`` has the name and the bullet names no other field. A value or a word in backticks is
    none of them."""
    placed = _placed(bullet)
    named = {x for p in placed for x in p}
    loose: set[str] = set()
    objects: set[str] = set()
    both: set[str] = set()   # an object's name that is also the name of a field the answer used
    for m in _TICKED_RE.finditer(bullet):
        span = m.group(1)
        if len(span.split(".")) >= 2 and all(_NAME_TOKEN_RE.fullmatch(p) for p in span.strip().split(".")):
            continue   # an <Obj>.<Field> name: placed
        for tok in map(_norm_api, _NAME_TOKEN_RE.findall(span)):
            if tok in named:
                continue
            if tok in known and not (_FIELD_BEFORE_RE.search(bullet[:m.start()])
                                     or _FIELD_AFTER_RE.match(bullet[m.end():])):
                (both if tok in fields else objects).add(tok)
            elif _FIELD_FORM_RE.fullmatch(tok) or tok in fields:
                loose.add(tok)
    if placed or loose:   # the bullet is about other fields: a name of both kinds is the object there
        return placed, loose, objects | both
    return placed, both, objects


def _relevance(bullet: str, used: tuple) -> int:
    """How ``bullet`` bears on the answer (``used``: :func:`_used_api`): 2 when it names a field the answer used (a
    placed field whose pair the answer used, or that the record answer names, on an object of the answer; a loose
    field that the answer used on some object, or that the record answer names), 1 when it names no field but an
    object of ``scope``, else 0. A bullet about a field the count did not use is 0 even when it names the counted
    object (review of 2026-10-29)."""
    pairs, said, scope, known, home = used
    fields = {f for _o, f in pairs} | said
    placed, loose, objects = _bullet_api(bullet, known, fields)
    if placed or loose:
        hit = any(p in pairs or (p[1] in said and p[0] in home) for p in placed) or bool(loose & fields)
        return 2 if hit else 0
    return 1 if objects & scope else 0


def _people_words(rec: RecordAnswer) -> set[str]:
    """The lower-case words (3 letters or more) of the names the question gives for people: the record answer's
    ``named_people`` and stored spellings (``named_spellings``), and the triage's names, people, missing names and
    corrected spellings."""
    tri = getattr(rec, "triage", None) or {}
    names = [str(x) for x in getattr(rec, "named_people", None) or []]
    for key in ("names", "people", "missing"):
        names += [str(x) for x in tri.get(key) or [] if x]
    names += [str(x.get(k) or "") for x in tri.get("spelling") or [] if isinstance(x, dict)
              for k in ("typed", "suggestion")]
    for typed, stored in (getattr(rec, "named_spellings", None) or {}).items():
        names += [str(typed), *(str(s) for s in stored or [])]
    return {w.lower() for n in names for w in re.findall(r"[A-Za-z]+", n) if len(w) >= 3}


def _names_people(bullet: str, words: set[str]) -> bool:
    """``bullet`` names a person of the question: one of ``words`` (:func:`_people_words`) as a word of its own, case
    aside (review of 2026-10-29: the lean step reads no record, so what it says about the people is a guess: "<name>
    is a person, so the records are linked via participant objects", "<name> is identified as a recruiter")."""
    if not words:
        return False
    return re.search(r"(?<![A-Za-z0-9_])(?:" + "|".join(map(re.escape, sorted(words))) + r")(?![A-Za-z0-9_])", bullet,
                     re.IGNORECASE) is not None


def _cut_off_bullet(bullet: str, last: bool = True) -> bool:
    """``bullet`` was cut off or is too long to add (review of 2026-10-29): longer than :data:`CODE_MERGE_MAX_CHARS`,
    an odd number of backticks or bold marks, an unclosed bracket, or, when it is the facts block's ``last`` line, an
    end that leaves it unfinished (:data:`_DANGLING_END_RE`; the line checks of :func:`merge_cut_off`). Step-2 fixes of
    2026-10-30 (F6): a token-limit cut can only end the lean reply, so a bullet with lines after it that ends with a
    particle ("... moved on", "... prepares for") is complete."""
    core = bullet.rstrip().rstrip(" *_")
    return (len(bullet) > CODE_MERGE_MAX_CHARS or bullet.count("`") % 2 == 1 or bullet.count("**") % 2 == 1
            or bullet.count("(") > bullet.count(")") or (last and bool(_DANGLING_END_RE.search(core))))


def _working_bullet(bullet: str, body: str) -> bool:
    """``bullet`` is working notes, tool talk or no field meaning: :func:`working_notes` says so, or a cleaner would
    change it (:func:`drop_tool_talk`; :func:`clean_knowledge_answer`, which drops what it says about fetching records;
    :func:`drop_working_sentences` with the bullet after the record answer's ``body``, where it goes). Review of
    2026-10-29: also a bullet with tool talk of its own ("tool results", :data:`~sf_assistant.agent.checks.
    _TOOL_TALK_RE`), one that is no field meaning (:data:`_NOT_MEANING_RE`) and one that echoes the lean step's prompt
    (:func:`_prompt_echo`)."""
    if working_notes(bullet) or drop_tool_talk(bullet) != bullet or clean_knowledge_answer(bullet) != bullet.strip():
        return True
    if _TOOL_TALK_RE.search(bullet) or _NOT_MEANING_RE.search(bullet) or _prompt_echo(bullet):
        return True
    cleaned = drop_working_sentences(body.rstrip() + "\n" + bullet)
    return " ".join(bullet.split()) not in {" ".join(ln.split()) for ln in cleaned.split("\n")}


#: The last line of a body that ends with a list: a list item ("- ", "* ", "+ ", "1. ") or an indented line (the
#: continuation of one).
_LIST_END_RE = re.compile(r"^\s*(?:[-*+]|\d{1,3}[.)])\s+\S|^\s{2,}\S")


def _with_bullets(answer: str, bullets: list[str], lead: str = "") -> str:
    """``answer`` with ``bullets`` after its body: a blank line before them, and its last Source lines (with the blank
    line before them, if any) after them, so the Source line stays last. Without a Source line the bullets end it.
    When the body ends with a list (:data:`_LIST_END_RE`) the label line :data:`CODE_MERGE_LABEL` comes first, so the
    bullets start a list of their own (review of 2026-10-29). ``lead`` (the counted line, :func:`_counted_line`; step-2
    fixes of 2026-10-30, F8) goes first after a blank line, the bullets (if any) after another blank line."""
    lines = (answer or "").rstrip().split("\n")
    end = len(lines)
    while end and (_is_source_line(lines[end - 1]) or not lines[end - 1].strip()):
        end -= 1
    label = [CODE_MERGE_LABEL] if end and _LIST_END_RE.match(lines[end - 1]) else []
    added = label + list(bullets) if bullets else []
    if lead:
        added = [lead] + ([""] + added if added else [])
    return "\n".join(lines[:end] + [""] + added + lines[end:])


def code_merge_lines(text: str, record_answer: str) -> list[str]:
    """The lines a merge by code added to ``record_answer`` in ``text`` (its counted line, label line and bullets; blank
    and Source lines aside), so that ``pipeline.answer`` reads what the answer already says without them (the lookup
    footer, the status lines, the person lead; review of 2026-10-29)."""
    have = set((record_answer or "").split("\n"))
    return [ln for ln in (text or "").split("\n") if ln.strip() and ln not in have and not _is_source_line(ln)]


# ------------------------------------------------------- the step-2 fixes of 2026-10-30 (the code merge's own checks)
# The code-merge evaluation of 2026-10-29 (A/B night run, same-input replay, hand reviews; docs/runbook.md): bullets
# that contradict the catalog (F2), legacy or inactive claims the knowledge does not make or that undercut a correct
# count (F3, F10c), off-topic subjects (F4), value meanings, value counts and usage claims the knowledge or the answer's
# own results contradict, caveats the lookup check resolves and FLS claims the knowledge does not make (F10), the cap
# order (F7), the counted line (F8) and Hinglish record answers (F11, F5). Every check only drops a bullet. The catalog
# checks (F2, F3, F10a, F10c, F10f) give no verdict without a catalog entry; F4, F10b and F10d read the answer's own
# evidence.

#: Lookup-target position (S2): a span right after these words is the object a lookup points to ("a lookup to `X`",
#: "the reference field at `X`", "points to `X`", "references `X`"); "linked to `X`", "copied to `X`" and "referenced
#: by `X`" are not (:data:`_TARGET_BEFORE_RE`, which :func:`_placed` reads, is unchanged).
_TARGET_SPAN_RE = re.compile(r"\b(?:(?:lookup|reference)(?:\s+field)?\s+(?:to|at)|points?\s+to|references?|"
                             r"referencing)\s+(?:(?:the|a|an)\s+)?$", re.IGNORECASE)
#: A tight placement after a field's span: "`F` [field word] on|of [the] `O`" (never "in `O`" or words between).
_TIGHT_ON_RE = re.compile(r"\s+(?P<word>(?:field|lookup|picklist|checkbox|column|formula(?:\s+field)?)\s+)?(?:on|of)\s+"
                          r"(?:the\s+)?`(?P<obj>[A-Za-z][A-Za-z0-9_]*)`", re.IGNORECASE)


class _Span(NamedTuple):
    """One span in backticks of a bullet (S2): ``kind`` "field", "path" (a dotted span whose first name is no known
    object: a lookup path, whose claims are about its last field, so the claim checks give it no verdict), "object" or
    "other" (a value, a flow, words); ``name`` the field (a relationship name read as its ``__c`` field; a path's first
    name) or the object; ``obj`` the object a placement puts the field on ("" when none); ``target`` the span is in
    lookup-target position."""
    start: int
    end: int
    kind: str
    name: str
    obj: str
    target: bool


def _spans(bullet: str, used: tuple) -> list[_Span]:
    """The spans of ``bullet`` in their order (S2; ``used``: :func:`_used_api`). "`O.F`" with O a known object puts F
    on O, any other dotted span is a lookup path (kind "path", named by its first name: a claim after it is about its
    last field, which the checks do not resolve); "`F` [field word] on|of [the] `O`" and
    "`O`'s `F`" put F on O and make O an object; a span after the lookup-target words is an object; a known name with
    no field word beside it is an object; any other custom name or used field is a field on no stated object. A
    placement needs a field-like F (a custom name, a system lookup, a used field, or a field word before "on|of"), so
    a value in backticks ("`Done` of `Visit__c`") is placed nowhere."""
    pairs, said, _scope, known, _home = used
    fields = {f for _o, f in pairs} | said
    ticks = list(_TICKED_RE.finditer(bullet))
    at = {m.start(): i for i, m in enumerate(ticks)}
    target = [bool(_TARGET_SPAN_RE.search(bullet[:m.start()])) for m in ticks]
    placed: dict[int, str] = {}
    objects: set[int] = set()

    def fieldlike(name: str) -> bool:
        return bool(_FIELD_FORM_RE.fullmatch(name)) or _norm_api(name) in fields

    for i, m in enumerate(ticks):
        if target[i] or i in objects or not _NAME_TOKEN_RE.fullmatch(m.group(1).strip()):
            continue
        tight = _TIGHT_ON_RE.match(bullet, m.end())
        j = at.get(tight.start("obj") - 1) if tight else None
        if j is not None and (tight.group("word") or fieldlike(m.group(1).strip())):
            placed[i] = tight.group("obj")
            objects.add(j)
            continue
        owns = _OWNS_TICKED_RE.match(bullet, m.start())
        j = at.get(owns.start("field") - 1) if owns else None
        if j is not None and fieldlike(owns.group("field")):
            objects.add(i)
            placed[j] = owns.group("obj")
    out: list[_Span] = []
    for i, m in enumerate(ticks):
        inner = m.group(1).strip()
        parts = inner.split(".")
        if len(parts) >= 2 and all(_NAME_TOKEN_RE.fullmatch(p) for p in parts):
            on = parts[0] if parts[0] in known else ""
            out.append(_Span(m.start(), m.end(), "field" if on else "path", _norm_api(parts[1] if on else parts[0]), on,
                             False))
        elif not _NAME_TOKEN_RE.fullmatch(inner):
            out.append(_Span(m.start(), m.end(), "other", inner, "", target[i]))
        elif target[i] or i in objects:
            out.append(_Span(m.start(), m.end(), "object", inner, "", target[i]))
        elif i in placed:
            out.append(_Span(m.start(), m.end(), "field", _norm_api(inner), placed[i], False))
        elif inner in known and not (_FIELD_BEFORE_RE.search(bullet[:m.start()])
                                     or _FIELD_AFTER_RE.match(bullet[m.end():])):
            out.append(_Span(m.start(), m.end(), "object", inner, "", False))
        elif _FIELD_FORM_RE.fullmatch(inner) or _norm_api(inner) in fields:
            out.append(_Span(m.start(), m.end(), "field", _norm_api(inner), "", False))
        else:
            out.append(_Span(m.start(), m.end(), "other", inner, "", False))
    return out


def _subject_field(bullet: str, used: tuple) -> str:
    """The bullet's subject field (S2): its first span that is a field (a lookup path: its first name), "" when none."""
    return next((s.name for s in _spans(bullet, used) if s.kind in ("field", "path")), "")


def _clauses(bullet: str) -> list[tuple[int, int]]:
    """The clauses of ``bullet`` as (start, end) offsets (S4): split at ";" and at ". ", "! ", "? " outside backticks."""
    out, start, tick = [], 0, False
    for i, ch in enumerate(bullet):
        if ch == "`":
            tick = not tick
        elif not tick and (ch == ";" or (ch in ".!?" and bullet[i + 1:i + 2].isspace())):
            out.append((start, i + 1))
            start = i + 1
    out.append((start, len(bullet)))
    return out


def _clause_at(bounds: list[tuple[int, int]], pos: int) -> tuple[int, int]:
    return next(((a, b) for a, b in bounds if a <= pos < b), bounds[-1])


def _field_before(spans: list[_Span], bounds: list[tuple[int, int]], pos: int) -> _Span | None:
    """The field span nearest before ``pos`` in its clause (S4: the field a claim belongs to), else None; None too when
    a lookup path is nearer (its claim is about its last field: no verdict)."""
    a, _b = _clause_at(bounds, pos)
    got = [s for s in spans if s.kind in ("field", "path") and s.start >= a and s.end <= pos]
    return got[-1] if got and got[-1].kind == "field" else None


def _has(spec: Any, name: str) -> str:
    """The key of ``spec.fields`` or ``spec.not_synced`` that the field name ``name`` stands for (S3): itself, its
    relationship name read as ``__c``, or ``name`` + "Id" (a standard relationship name: RecordType, Owner, Account),
    case aside; "" when none."""
    if spec is None:
        return ""
    keys = {str(k).lower(): str(k) for k in [*(getattr(spec, "fields", None) or {}),
                                             *(getattr(spec, "not_synced", None) or {})]}
    for cand in (name, _norm_api(name), name + "Id"):
        if cand.lower() in keys:
            return keys[cand.lower()]
    return ""


def _with_fields(catalog: Mapping, obj: str) -> Any:
    """The catalog's spec of ``obj`` when it has fields, else None (no verdict)."""
    spec = catalog.get(obj) if obj else None
    return spec if spec is not None and getattr(spec, "fields", None) else None


def _child_of(spec: Any, parent: str) -> bool:
    """``spec`` (a catalog object's) has a lookup to ``parent``: a reference field, or a field documented as not
    synced, whose targets hold it, case aside. "Each `Child__c` of the `Parent__c`" then names a child object, no field
    the parent lacks (F2 b; review of 2026-10-30)."""
    if spec is None:
        return False
    p = parent.lower()
    targets = [getattr(fs, "ref_targets", None) or [] for fs in (getattr(spec, "fields", None) or {}).values()
               if getattr(fs, "kind", "") == "reference"]
    targets += [v.get("targets") or [] for v in (getattr(spec, "not_synced", None) or {}).values()
                if isinstance(v, dict)]
    return any(p == str(t).lower() for ts in targets for t in ts)


def _resolve(span: _Span, spans: list[_Span], home: set[str], catalog: Mapping) -> list[tuple[str, Any, str]]:
    """``(object, spec, field key)`` of each catalog object a field span stands for (S3): its placed object; else the
    objects the bullet names (not in lookup-target position) that have the field; else the answer's home objects that
    have it. [] without a verdict (no catalog entry, a spec without fields, a placed object without the field)."""
    if not catalog:
        return []
    if span.obj:
        spec = _with_fields(catalog, span.obj)
        key = _has(spec, span.name)
        return [(span.obj, spec, key)] if key else []
    for group in ([s.name for s in spans if s.kind == "object" and not s.target], sorted(home)):
        got = [(o, spec, key) for o in dict.fromkeys(group)
               for spec in (_with_fields(catalog, o),) for key in (_has(spec, span.name),) if key]
        if got:
            return got
    return []


def _knowledge_entry(spec: Any, key: str) -> dict | None:
    """The raw knowledge entry of the field ``key`` of ``spec`` (case aside); None when the object or the field has
    none."""
    kn = getattr(spec, "knowledge", None)
    return next((f for f in (kn.fields() if kn is not None else [])
                 if str(f.get("api_name") or "").lower() == key.lower()), None)


def _legacy_mark(spec: Any, key: str) -> bool | None:
    """Whether the knowledge marks the field ``key`` of ``spec`` legacy (S5, its raw entry: legacy or deprecated true,
    replaced_by set, the role legacy_attribute or deprecated, a concept or meaning that starts with "legacy" or
    "deprecated"); None when the object or the field has no knowledge entry. Never FieldSpec.legacy, which is also
    true when the meaning only mentions legacy rows or values."""
    entry = _knowledge_entry(spec, key)
    if entry is None:
        return None
    sem = entry.get("semantic") if isinstance(entry.get("semantic"), dict) else {}
    roles = {str(r).lower() for r in sem.get("roles") or []}
    meaning = str(entry.get("business_meaning") or "").strip().lower()
    return bool(entry.get("legacy") or entry.get("deprecated") or entry.get("replaced_by")
                or roles & {"legacy_attribute", "deprecated"} or str(sem.get("concept") or "").lower().startswith("legacy")
                or meaning.startswith(("legacy", "deprecated")))


#: A legacy claim's word (F3), and the tokens of the word windows around it (a span in backticks is one token).
_LEGACY_WORD_RE = re.compile(r"\b(?:legacy|deprecated|inactive|obsolete|retired)\b", re.IGNORECASE)
_TOKEN_RE = re.compile(r"`[^`\n]*`|[A-Za-z][A-Za-z'-]*")
_CLAIM_VALUE_WORDS = frozenset({"value", "values", "rows", "records", "data", "entries", "options", "statuses"})
_CLAIM_PREDICATE_WORDS = frozenset({"is", "are", "was", "were", "marked", "flagged", "considered", "now", "being"})
_CLAIM_FIELD_NOUNS = frozenset({"field", "picklist", "lookup", "checkbox", "column", "formula", "roll-up", "rollup",
                                "attribute"})
_CLAIM_BETWEEN_WORDS = frozenset({"value", "values", "these", "those", "they", "statuses"})
#: The words that may stand between a predicate word and the claim word ("is now marked as legacy", "is a legacy
#: field"); any other word ("is active or inactive", "is set to Inactive", "is false for inactive employees") makes the
#: word no claim about the field (review of 2026-10-30).
_CLAIM_GLUE_WORDS = _CLAIM_PREDICATE_WORDS | frozenset({"as", "also", "still", "treated", "deemed", "officially",
                                                         "effectively", "currently", "kept", "a", "an", "the"})
#: Form (i): the claim word right before a span, up to two field words between ("the legacy `F` picklist").
_BEFORE_SPAN_RE = re.compile(r"\s+(?:(?:field|picklist|lookup|checkbox|column|formula|roll-up|rollup|attribute)\s+)"
                             r"{0,2}(?=`)", re.IGNORECASE)


def _predicate_claim(bullet: str, spans: list[_Span], pos: int) -> bool:
    """Form (ii) of :func:`_legacy_claims`: the claim word at ``pos`` ends a predicate about a field. Right before it
    stand only predicate and glue words (:data:`_CLAIM_GLUE_WORDS`), at least one of them a predicate word ("is marked
    as legacy"), and right before those (a parenthetical aside skipped: "`F` (text) is legacy") the field's span, a
    field noun, "which" or "that" ("the field, which is now inactive"). Review of 2026-10-30: "is active or inactive",
    "is set to Inactive", "a person is inactive" and "this rule is inactive" make no claim about a field."""
    toks = list(_TOKEN_RE.finditer(bullet, 0, pos))
    k = len(toks)
    while k and len(toks) - k < 5 and toks[k - 1].group(0).lower() in _CLAIM_GLUE_WORDS:
        k -= 1
    if not any(t.group(0).lower() in _CLAIM_PREDICATE_WORDS for t in toks[k:]):
        return False
    head = bullet[:toks[k].start()].rstrip()
    if head.endswith(")"):   # a parenthetical aside between the subject and the predicate
        depth = 0
        for i in range(len(head) - 1, -1, -1):
            depth += {")": 1, "(": -1}.get(head[i], 0)
            if depth == 0:
                head = head[:i].rstrip()
                break
    prev = list(_TOKEN_RE.finditer(head))
    if not prev:
        return False
    t = prev[-1]
    if t.group(0).startswith("`"):
        return any(s.start == t.start() and s.kind == "field" for s in spans)
    return t.group(0).lower() in _CLAIM_FIELD_NOUNS | {"which", "that"}


def _legacy_claims(bullet: str, spans: list[_Span], bounds: list[tuple[int, int]]) -> list[_Span]:
    """The field spans ``bullet`` calls legacy, deprecated, inactive, obsolete or retired (F3): the word right before a
    field's span (not "the inactive `F` value"; a span that is no field, such as a flow, makes no claim); the word in a
    field noun phrase ("obsolete lookup field") about a field's span among the 3 words after it ("the legacy free-text
    `F` field"), else about the nearest field before it in its clause; or the word ending a predicate about a field
    (:func:`_predicate_claim`: "is marked as legacy", "which is now inactive") about the nearest field before it in its
    clause (or the bullet's first field when the clause reads "the field is"). No claim when the word is followed within
    2 words by values, rows, records, data, entries, options or statuses ("keeps inactive legacy values"), or when
    values, these, those, they or statuses stand between the field and the word."""
    out: list[_Span] = []
    for m in _LEGACY_WORD_RE.finditer(bullet):
        if any(s.start <= m.start() < s.end for s in spans):
            continue   # inside a span
        after_t = list(_TOKEN_RE.finditer(bullet, m.end()))
        after = [t.group(0) for t in after_t]
        if any(t.lower() in _CLAIM_VALUE_WORDS for t in after[:2]):
            continue
        nxt = _BEFORE_SPAN_RE.match(bullet, m.end())
        if nxt:
            s = next((x for x in spans if x.start == nxt.end()), None)
            if s is not None:
                if s.kind == "field" and not re.match(r"\s+values?\b", bullet[s.end:], re.IGNORECASE):
                    out.append(s)
                continue
        noun = any(t.lower() in _CLAIM_FIELD_NOUNS for t in after[:3])
        if noun:   # a field's (or a path's) span among the 3 words after the word, in its clause, is the one meant
            _a, b = _clause_at(bounds, m.start())
            s = next((x for t in after_t[:3] if t.start() < b for x in spans
                      if x.start == t.start() and x.kind in ("field", "path")), None)
            if s is not None:
                if s.kind == "field" and not re.match(r"\s+values?\b", bullet[s.end:], re.IGNORECASE):
                    out.append(s)
                continue
        if not (noun or _predicate_claim(bullet, spans, m.start())):
            continue
        f = _claimed_field(bullet, spans, bounds, m.start())
        if f is None or any(t.lower() in _CLAIM_BETWEEN_WORDS for t in _TOKEN_RE.findall(bullet[f.end:m.start()])):
            continue
        out.append(f)
    return out


def _claimed_field(bullet: str, spans: list[_Span], bounds: list[tuple[int, int]], pos: int) -> _Span | None:
    """The field a claim at ``pos`` is about (F3, F10f): the nearest field before it in its clause
    (:func:`_field_before`), else the bullet's first field when the clause reads "the field is"; None when neither."""
    f = _field_before(spans, bounds, pos)
    if f is None:
        a, b = _clause_at(bounds, pos)
        if re.search(r"\bthe\s+field\s+is\b", bullet[a:b], re.IGNORECASE):
            f = next((x for x in spans if x.kind == "field"), None)
    return f


#: F2 (a): "lookup|reference [field] to|at [the|a|an] `X`", "points to [the|a|an] `X`".
_LOOKUP_CLAIM_RE = re.compile(r"\b(?:(?:lookup|reference)(?:\s+field)?\s+(?:to|at)|points?\s+to)\s+(?:(?:the|a|an)\s+)?"
                              r"`(?P<x>[A-Za-z][A-Za-z0-9_]*)`", re.IGNORECASE)
#: F2 (c): "`F` [field|picklist] [on [the] `O` [object]] is|= [a|an|the] [up to 2 words] lookup".
_CALLED_LOOKUP_RE = re.compile(r"`(?P<f>[A-Za-z][A-Za-z0-9_]*)`(?:\s+(?:field|picklist))?(?:\s+on\s+(?:the\s+)?"
                               r"`[A-Za-z][A-Za-z0-9_]*`(?:\s+object)?)?\s+(?:is|=)\s+(?P<mid>(?:(?:a|an|the)\s+)?"
                               r"(?:[\w-]+\s+){0,2}?)lookup\b", re.IGNORECASE)
#: F2 (d): "stored on|in [the|a|an|each] `X` [up to 2 words] row(s)|record(s)" ("stored on the `X` attendee row").
_STORED_ON_RE = re.compile(r"\bstored\s+(?:on|in)\s+(?:(?:the|a|an|each)\s+)?`(?P<x>[A-Za-z][A-Za-z0-9_]*)`"
                           r"(?:\s+[\w-]+){0,2}?\s+(?:rows?|records?)\b", re.IGNORECASE)
#: F10f: a field called hidden by field-level security, unless a negation stands at most 2 words before the claim
#: ("is not FLS-hidden", "not subject to field-level security").
_FLS_CLAIM_RE = re.compile(r"\bFLS[- ]hidden\b|\bhidden\s+by\s+FLS\b|\bfield[- ]level\s+security\b", re.IGNORECASE)
_NEGATED_RE = re.compile(r"(?:\b(?:not|no|never)|n't)\s+(?:[\w-]+\s+){0,2}$", re.IGNORECASE)
#: F10a: "`V` [value] means|indicates|... <M>", M to the next ".", ";", "(" or the line end.
_MEANS_RE = re.compile(r"`(?P<v>[^`\n]+)`(?:\s+value)?\s+(?:means|indicates|signals|signifies|represents|denotes)\s+"
                       r"(?P<m>[^.;(\n]*)", re.IGNORECASE)
_MEANING_STOP_WORDS = frozenset(
    "the a an of to in on for and or is are was were be been by with that this it its as at from when which who not no "
    "has have had did does do value values record records field fields status picklist set sets if then than into after "
    "before automatically".split())
#: F10b: "N [possible|...] values", "the only [documented|possible] value" (never "N active values": the records keep
#: values made inactive, which a result counts too); negative usage claims.
_NUMBER_WORDS = {w: i + 1 for i, w in enumerate("one two three four five six seven eight nine ten".split())}
_N_VALUES_RE = re.compile(r"\b(?P<n>\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:(?:possible|documented|"
                          r"picklist|distinct|different)\s+)?values\b|\bthe\s+only\s+(?:(?:documented|possible)\s+)?"
                          r"value\b", re.IGNORECASE)
_UNUSED_RE = re.compile(r"\b(?:largely|mostly)\s+(?:unused|empty|blank)\b|\b(?:rarely|seldom|barely|hardly)\s+(?:used|"
                        r"filled|populated|set)\b|\bunused\b|\balmost\s+never\s+(?:used|filled|populated|set)\b|"
                        r"\bnot\s+used\s+in\s+practice\b", re.IGNORECASE)
#: A hedged usage claim ("largely unused", "rarely filled", "almost never set"): about how often the field is filled,
#: which a large blank group confirms; a bare one ("unused", "not used in practice") says it holds no value.
_HEDGED_RE = re.compile(r"(?:largely|mostly|rarely|seldom|barely|hardly|almost)\b", re.IGNORECASE)
#: F10d: an undercount caveat about a lookup ("only set for ...", "is blank for ...", "not in `X`", "instead of").
_CAVEAT_RE = re.compile(r"\bonly\s+(?:set|populated|filled|used)\b|\b(?:is|remains|stays)\s+blank\b|\bblank\s+(?:for|on|"
                        r"in)\b|\bnot\s+in\s+`|\binstead\s+of\b|\brather\s+than\b", re.IGNORECASE)


def _content_stems(text: str) -> set[str]:
    """The content words of ``text`` (3 letters or more, stop words aside) by their first 5 letters."""
    return {w[:5] for w in re.findall(r"[a-z]+", (text or "").lower()) if len(w) >= 3 and w not in _MEANING_STOP_WORDS}


def _result_values(rec: RecordAnswer) -> dict[str, int]:
    """field (lower case; a path also by its last name) -> the most non-blank values a result of the answer shows for
    it: a group_records grouped by it (its groups other than "(blank)" and "(unresolved id)", at least group_count),
    a split by it (its split_totals keys other than those)."""
    out: dict[str, int] = {}

    def add(text: Any, n: int) -> None:
        m = re.match(r"[A-Za-z][\w.]*", str(text or ""))
        for k in ({m.group(0).lower(), m.group(0).split(".")[-1].lower()} if m else ()):
            out[k] = max(out.get(k, 0), n)

    special = ("(blank)", "(unresolved id)")
    for e in rec.evidence:
        res = e.model_result if e.ok and e.kind == "data" and isinstance(e.model_result, dict) else None
        if res is None:
            continue
        if e.tool == "group_records" and res.get("grouped_by"):
            groups = [g for g in res.get("groups") or [] if isinstance(g, dict) and g.get("group") not in special]
            add(res["grouped_by"], max(len(groups), _count(res.get("group_count"))))
        if res.get("split_by") and isinstance(res.get("split_totals"), dict):
            add(res["split_by"], sum(1 for k in res["split_totals"] if str(k) not in special))
    return out


def _result_fill(rec: RecordAnswer) -> dict[str, list[tuple[int, int]]]:
    """field (lower case; a path also by its last name) -> (filled, blank) record counts of each count result of the
    answer that groups or splits by it: a group_records (its total_across_groups, or records_matched, less its blank
    group, and that blank group) and a split by it (its split_totals other than "(blank)", and "(blank)"). A result
    whose own filters name the field is left out: it shows only the records the filter kept (F10b; review of
    2026-10-30)."""
    out: dict[str, list[tuple[int, int]]] = {}

    def key(text: Any) -> set[str]:
        m = re.match(r"[A-Za-z][\w.]*", str(text or ""))
        return {m.group(0), m.group(0).split(".")[-1]} if m else set()

    for e in rec.evidence:
        res = e.model_result if e.ok and e.kind == "data" and isinstance(e.model_result, dict) else None
        if res is None or e.tool != "group_records" or res.get("measure") != "count":
            continue
        applied = e.applied if isinstance(e.applied, dict) else {}
        conds = json.dumps([applied.get("filters"), applied.get("any_of")], ensure_ascii=False, default=str)
        got: list[tuple[Any, int, int]] = []
        whole = _count(res.get("total_across_groups")) or _count(res.get("records_matched"))
        if res.get("grouped_by") and whole > 0:
            blank = _count(res.get("blank_group_value"))
            got.append((res["grouped_by"], max(0, whole - blank), blank))
        if res.get("split_by") and isinstance(res.get("split_totals"), dict):
            totals = res["split_totals"]
            blank = sum(_count(v) for k, v in totals.items() if str(k) == "(blank)")
            filled = sum(_count(v) for k, v in totals.items() if str(k) != "(blank)")
            if filled + blank > 0:
                got.append((res["split_by"], filled, blank))
        for text, filled, blank in got:
            names = key(text)
            if names and not any(_names(conds, x) for x in names):
                for k in names:
                    out.setdefault(k.lower(), []).append((filled, blank))
    return out


def _contradiction(bullet: str, used: tuple, rec: RecordAnswer, catalog: Mapping) -> str:
    """Why ``bullet`` contradicts the record catalog or the answer's own results ("" when nothing does; step-2 fixes of
    2026-10-30). F2: a placed field its object does not have (no column, not documented as not synced; a child object
    placed on its parent is no field: :func:`_child_of`), a lookup target the field's reference targets do not hold, a
    picklist called a lookup, something "stored on the `X` ... row" of a catalog object outside the answer's objects
    that no lookup links to any of them, in either direction (F2 d). F3 / F10c: a legacy or inactive claim
    (:func:`_legacy_claims`) about a field the knowledge marks legacy on none of its objects (:func:`_legacy_mark`), or
    about a field the answer's queries used that the snapshot fills on at least half of the rows. F10f: a field called
    FLS-hidden (:data:`_FLS_CLAIM_RE`, the field as for F3: :func:`_claimed_field`) whose raw knowledge entry flags
    ``fls_hidden`` on none of its objects (no entry: no verdict). F10a: a value meaning that shares no content word with
    the value's knowledge meaning. F10b: "N values" or "the only value" while a result groups or splits by the field
    with more values; "unused" while it shows 2 or more values; "largely unused", "rarely filled" while the field is
    filled on at least half of the records its count results group or split (:func:`_result_fill`; a sum or a result
    filtered on the field gives no verdict). F10d: an undercount caveat about a lookup check's field or its others
    when every other lookup holds the same records."""
    pairs, _said, _scope, _known, home = used
    spans = _spans(bullet, used)
    bounds = _clauses(bullet)
    names = {k.lower(): k for k in catalog}
    for s in spans:   # F2 (b): a field placed on an object that has no such field
        if s.kind == "field" and s.obj and s.name.lower() != s.obj.lower():
            spec = _with_fields(catalog, s.obj)
            if spec is not None and not _has(spec, s.name) and not _child_of(catalog.get(names.get(s.name.lower(), "")),
                                                                              s.obj):
                return f"{s.obj} has no field {s.name}"
    for m in _LOOKUP_CLAIM_RE.finditer(bullet):   # F2 (a): a lookup target its reference targets do not hold
        x = m.group("x")
        f = _field_before(spans, bounds, m.start())
        if (x.lower() not in names and x.lower() not in ("user", "group")) or f is None:
            continue
        got = _resolve(f, spans, home, catalog)
        targets: list[str] = []
        for _o, spec, key in got:
            fs = (getattr(spec, "fields", None) or {}).get(key)
            t = (list(getattr(fs, "ref_targets", None) or []) if fs is not None and getattr(fs, "kind", "") == "reference"
                 else [] if fs is not None else list(((getattr(spec, "not_synced", None) or {}).get(key) or {})
                                                    .get("targets") or []))
            if not t:
                targets = []
                break
            targets += t
        if targets and x.lower() not in {str(t).lower() for t in targets}:
            return f"{f.name} is no lookup to {x}"
    for m in _CALLED_LOOKUP_RE.finditer(bullet):   # F2 (c): a picklist called a lookup
        f = next((s for s in spans if s.start == m.start() and s.kind == "field"), None)
        if f is None or re.search(r"\b(?:not|no|never)\b", m.group("mid"), re.IGNORECASE):
            continue
        got = _resolve(f, spans, home, catalog)
        kinds = [getattr((getattr(spec, "fields", None) or {}).get(key), "kind", None) for _o, spec, key in got]
        if kinds and all(k in ("picklist", "multipicklist") for k in kinds):
            return f"{f.name} is a picklist, no lookup"
    for m in _STORED_ON_RE.finditer(bullet):   # F2 (d): the rows of an object no lookup links to the answer's objects
        x = names.get(m.group("x").lower(), "")
        specs = {h: _with_fields(catalog, h) for h in sorted(home)}
        if not x or x in home or _with_fields(catalog, x) is None or not specs \
                or any(s is None for s in specs.values()):
            continue   # no verdict without catalog entries
        if not any(_child_of(catalog[x], h) or _child_of(spec, x) for h, spec in specs.items()):
            return f"no lookup links {x} to {', '.join(specs)}"
    used_ci = {(str(o).lower(), str(f).lower()) for o, f in pairs}
    for f in _legacy_claims(bullet, spans, bounds):   # F3 and F10c
        got = _resolve(f, spans, home, catalog)
        marks = [_legacy_mark(spec, key) for _o, spec, key in got]
        if marks and all(mk is False for mk in marks):
            return f"the knowledge does not mark {f.name} legacy"
        for o, spec, key in got:
            fs = (getattr(spec, "fields", None) or {}).get(key)
            rows = getattr(spec, "rows", 0) or 0
            if (o.lower(), key.lower()) in used_ci and fs is not None and rows > 0 and \
                    (getattr(fs, "filled", 0) or 0) * 2 >= rows:
                return f"a legacy note on {f.name}, which the count used and the snapshot fills on most rows"
    for m in _FLS_CLAIM_RE.finditer(bullet):   # F10f: an FLS claim the knowledge does not make
        if any(s.start <= m.start() < s.end for s in spans) or _NEGATED_RE.search(bullet[:m.start()]):
            continue
        f = _claimed_field(bullet, spans, bounds, m.start())
        marks = [None if entry is None else bool(entry.get("fls_hidden"))
                 for _o, spec, key in (_resolve(f, spans, home, catalog) if f is not None else [])
                 for entry in (_knowledge_entry(spec, key),)]
        if marks and all(mk is False for mk in marks):
            return f"{f.name} called FLS-hidden, which the knowledge flags on none of its objects"
    for m in _MEANS_RE.finditer(bullet):   # F10a: a value meaning the knowledge does not give
        v, said = m.group("v").strip().lower(), _content_stems(m.group("m"))
        meanings = [meaning for s in spans if s.kind == "field" for _o, spec, key in _resolve(s, spans, home, catalog)
                    for fs in ((getattr(spec, "fields", None) or {}).get(key),) if fs is not None
                    for value, label, meaning in getattr(fs, "picklist", None) or []
                    if v in (str(value).lower(), str(label).lower()) and str(meaning or "").strip()]
        if meanings and said and not any(_content_stems(x) & said for x in meanings):
            return f"a meaning of the value {m.group('v').strip()} that the knowledge does not give"
    values = _result_values(rec)
    fills = _result_fill(rec)
    for m in list(_N_VALUES_RE.finditer(bullet)) + list(_UNUSED_RE.finditer(bullet)):   # F10b: the answer's results
        f = _field_before(spans, bounds, m.start())
        n = values.get(f.name.lower()) if f is not None else None
        if n is None:
            continue
        if m.re is _UNUSED_RE and not _HEDGED_RE.match(m.group(0)):   # "unused": the field holds no value
            if n >= 2:
                return f"{f.name} called unused while a result shows {n} values"
            continue
        if m.re is _UNUSED_RE:   # "largely unused", "rarely filled": how often; a large blank group confirms it
            shares = fills.get(f.name.lower())   # none for a sum or a result filtered on the field: no verdict
            if shares and all(filled >= blank for filled, blank in shares):
                return f"{f.name} called unused while it is filled on at least half of the records"
            continue
        word = (m.group("n") or "").lower()
        claimed = 1 if not word else int(word) if word.isdigit() else _NUMBER_WORDS[word]
        if n > claimed:
            return f"{f.name} with {claimed} values while a result shows {n}"
    for lc in lookup_checks(rec.evidence):   # F10d: a caveat the lookup check resolves
        others = [o for o in lc.get("others") or [] if isinstance(o, dict)]
        if _count(lc.get("records")) <= 0 or not others or any(o.get("records") != lc.get("records") for o in others):
            continue
        fields = [str(x) for x in [lc.get("field")] + [o.get("field") for o in others] if x]
        if any(_names(bullet, x) for x in fields) and _CAVEAT_RE.search(bullet):
            return "a caveat that the lookup check resolves (every other lookup holds the same records)"
    return ""


def _other_object_subject(bullet: str, used: tuple, catalog: Mapping) -> bool:
    """The bullet is about another object (F4): its subject (S2, its first span) is a field placed on an object
    outside the answer's home objects, or an object outside them that is not in lookup-target position and is no
    field of a home object (a used pair, or with a catalog a field of its spec: an object named like a lookup, such as
    `Department__c`); and the bullet places no used pair on a home object and names no home object outside
    lookup-target position. A used field it names in passing then makes it no more relevant."""
    pairs, _said, _scope, _known, home = used
    spans = _spans(bullet, used)
    if not spans:
        return False
    s = spans[0]
    if s.kind == "field" and s.obj:
        off = s.obj not in home
    elif s.kind == "object" and not s.target:
        off = s.name not in home and not any(f == s.name and o in home for o, f in pairs) \
            and not (catalog and any(_has(catalog.get(o), s.name) for o in home))
    else:
        off = False
    if not off:
        return False
    placed = {(x.obj, x.name) for x in spans if x.kind == "field" and x.obj} | _placed(bullet)
    if any(p in pairs and p[0] in home for p in placed):
        return False
    return not any(x.kind == "object" and not x.target and x.name in home for x in spans)


def _date_fields(rec: RecordAnswer) -> set[str]:
    """The date fields (lower case; a path also by its last name) of the answer's successful counting queries: every
    string under a "date_field" key of their "applied" blocks and results (a period, compare segments, a trend)."""
    out: set[str] = set()

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                if k == "date_field" and isinstance(v, str) and v.strip():
                    out.update({v.strip().lower(), v.strip().split(".")[-1].lower()})
                else:
                    walk(v)
        elif isinstance(x, (list, tuple)):
            for v in x:
                walk(v)

    for e in rec.evidence:
        if e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS:
            walk(e.applied)
            walk(e.model_result)
    return out


#: The words of the counted line (F8): one "key: text" line per part, read into :data:`_COUNTED`.
CODE_MERGE_COUNTED = _prompt_files.text("code_merge_counted")
_COUNTED = dict(ln.split(": ", 1) for ln in CODE_MERGE_COUNTED.split("\n") if ": " in ln)
#: The group tool's note when its ranking leaves the blank group out (tools/records.py).
_BLANK_LEFT_OUT = "the (blank) group is left out of the ranking"


def _counted_line(rec: RecordAnswer, body: str, bullets: list[str] | tuple[str, ...] = ()) -> str:
    """One code line that says what the answer's one counting query counted, when the record answer (``body``, Source
    lines aside) leaves it out (F8), else "". Only for exactly one successful counting query, a group_records with the
    measure count or sum or a list_records, without any_of or related conditions; the record answer leaves out the
    grouped field (its whole path at the start of grouped_by, or its last name), the blank group the ranking left out
    (its size), a not-blank filter on the grouped field (the word "blank"), or a list's total. A kept bullet
    (``bullets``) that names the grouped field says it too when the record answer or the bullets also name the field of
    every filter (review of 2026-10-30: the line would only repeat them). The line states the object, the grouped field
    or the total, every filter, the period (its first and last day: the period's own words, "since ...", "upcoming", do
    not read after "in") and the blank group left out; each value comes from the result and its "applied" block (the
    number audit passes). Its words: prompts/code_merge_counted.md."""
    counting = [e for e in rec.evidence if e.ok and e.kind == "data" and e.tool in _COUNTING_TOOLS]
    if len(counting) != 1 or counting[0].tool not in ("group_records", "list_records"):
        return ""
    e = counting[0]
    res = e.model_result if isinstance(e.model_result, dict) else None
    applied = e.applied if isinstance(e.applied, dict) else {}
    if res is None or not e.object or applied.get("any_of") or applied.get("related"):
        return ""
    filters = [str(f) for f in applied.get("filters") or []]
    period = applied.get("period") if isinstance(applied.get("period"), dict) else {}
    if period and not (period.get("date_field") and period.get("time_zone")
                       and (period.get("from") or period.get("to"))):
        return ""   # the line must state every condition
    said = set(numbers_in(body))
    fill = _prompt_files.fill
    blank = None
    if e.tool == "group_records":
        measure = res.get("measure")
        g = re.match(r"[A-Za-z][\w.]*", str(res.get("grouped_by") or ""))
        if measure not in ("count", "sum") or g is None or (measure == "sum" and not res.get("measure_field")):
            return ""
        group, last = g.group(0), g.group(0).split(".")[-1]
        if res.get("blank_group_value") is not None and _BLANK_LEFT_OUT in (e.notes or []):
            blank = res["blank_group_value"]
        not_blank = {f"{group} is not blank".lower(), f"{last} is not blank".lower()}
        named = _names(body, group) or _names(body, last)
        if not named and bullets:   # a kept bullet names it: only when every filter's field is named too
            told = "\n".join([body, *bullets])
            heads = {h.group(0).split(".")[0] for h in (re.match(r"[A-Za-z][\w.]*", f) for f in filters) if h}
            named = any(_names(b, x) for b in bullets for x in (group, last)) and all(
                _names(told, h) or _names(told, _norm_api(h)) for h in heads)
        if named and (blank is None or _num(blank) in said) and not (
                any(f.strip().lower() in not_blank for f in filters) and "blank" not in body.lower()):
            return ""
        parts = fill(_COUNTED["records"], OBJECT=e.object) + " " + fill(_COUNTED["by"], GROUP_FIELD=group)
    else:
        total = res.get("total_count")
        if _num(total) is None or _num(total) in said:
            return ""
        parts = fill(_COUNTED["total"], TOTAL=str(total), OBJECT=e.object)
    if filters:
        parts += ", " + fill(_COUNTED["where"], FILTERS=" and ".join(filters))
    if period:   # its first and last day (an open end: "on or after", "up to")
        frm, to = period.get("from"), period.get("to")
        parts += ", " + fill(_COUNTED["period" if frm and to else "period_from" if frm else "period_to"],
                             DATE_FIELD=str(period["date_field"]), FROM=str(frm or ""), TO=str(to or ""),
                             ZONE=str(period["time_zone"]))
    if blank is not None:
        parts += "; " + fill(_COUNTED["blank"], VALUE=str(blank))
    if e.tool == "group_records" and res.get("measure") == "sum":
        return fill(_COUNTED["summed"], MEASURE_FIELD=str(res["measure_field"]), PARTS=parts)
    return fill(_COUNTED["counted"], PARTS=parts)


#: Short Hindi function words for :func:`_hinglish` (F11) next to the spoken-words list (knowledge/spoken_words.txt,
#: which the value spots and the resolver read too, so an edit there also moves F11 and F5): 30 words, 11 of them also
#: in that list (the fallback when it cannot be read); "main" is Hinglish for "in" as well as "I" (review of
#: 2026-10-30).
_HINGLISH_WORDS = frozenset("ke ki ka ko se ne hai hain tha thi hue hua hui aur yeh ye ek kiye kiya raha rahe beech "
                            "liye naam wala wale wali gaye gayi main".split())
#: A list item ("- ", "* ", "+ ", "1. "), and a table row or table line ("| ...", "{{table q1}}"): no prose (F5, F11).
_LIST_ITEM_RE = re.compile(r"\s*(?:[-*+]|\d+\.)\s")
_TABLE_LINE_RE = re.compile(r"\s*\||\s*[*_`]{0,3}\{\{")


def _prose_lines(text: str | None, blocks: Any = None, lists: bool = False) -> list[str]:
    """The prose lines of ``text``: each of ``blocks`` (the code rows' texts) removed whole, and no Source line, table
    row or table line, nor a list item unless ``lists``."""
    t = text or ""
    for block in (blocks.values() if isinstance(blocks, Mapping) else blocks or ()):
        if block:
            t = t.replace(str(block), "")
    return [ln for ln in t.split("\n") if ln.strip() and not _is_source_line(ln) and not _TABLE_LINE_RE.match(ln)
            and (lists or not _LIST_ITEM_RE.match(ln))]


def _prose_words(text: str | None, blocks: Any = None, lists: bool = False) -> list[str]:
    """The lower-case letter words of the prose lines of ``text`` (:func:`_prose_lines`), spans in backticks aside."""
    return re.findall(r"[a-z]+", re.sub(r"`[^`\n]*`", " ", "\n".join(_prose_lines(text, blocks, lists))).lower())


def _hinglish(text: str | None, blocks: Any = None) -> bool:
    """The record answer is Hinglish (F11): of the letter words of its prose (:func:`_prose_words`), or of its prose
    with its list items (review of 2026-10-30: a short Hinglish lead over Hinglish list lines), at least 3, and at least
    10%, are romanized Hindi words (``value_spots._spoken_words``, read once, and :data:`_HINGLISH_WORDS`). The lean
    facts block is always English and code cannot translate it."""
    from sf_assistant.tools.value_spots import _spoken_words   # knowledge/spoken_words.txt, read lazily

    known = _spoken_words() | _HINGLISH_WORDS

    def hindi(words: list[str]) -> bool:
        n = sum(1 for w in words if w in known)
        return n >= 3 and n * 10 >= len(words)

    return hindi(_prose_words(text, blocks)) or hindi(_prose_words(text, blocks, lists=True))


#: Thinking aloud at a line start or after ". ", "! ", "? " or ":" (F5): "I have the answer.", "Now I know:",
#: "It seems ...".
_THINKS_ALOUD_RE = re.compile(r"(?:^|[.!?]\s+|:\s*)[*_]{0,3}\s*(?:I\s+have\s+(?:the|an|all\s+the)\s+answers?\b|"
                              r"I\s+(?:now\s+)?know\s*:|Now\s+I\s+know\b|It\s+(?:seems|appears|looks\s+like)\b)",
                              re.IGNORECASE | re.MULTILINE)
#: A first-person word of Hinglish working notes (F5): "main", "mujhe", "maine".
_HINGLISH_I_RE = re.compile(r"\b(?:main|mujhe|maine)\b")


def plans_aloud(answer: str | None) -> bool:
    """The record answer still holds a first-person planning phrase (``checks._PLANNING_RE``: "Let me compile the
    comprehensive answer.", which the record session's cleaners keep): the merge call rewrote it away, code would show
    it, so ``pipeline.answer`` makes the merge call for such an answer (review of 2026-10-29). Step-2 fixes of
    2026-10-30 (F5): also thinking aloud in a prose line (:data:`_THINKS_ALOUD_RE`: "I have the answer.", "Now I
    know:", "It seems ..."; never in a list item or a table row, whose values may read so: "Note: It seems fine"), tool
    talk (a tool name or result key, ``checks._TOOL_WORD_RE``; talk about the prompt, ``checks._PROMPT_TALK_RE``; never
    the merge input words of ``working_notes``, which read "in draft", a business value, as merge talk) and Hinglish
    (:func:`_hinglish`) with a first-person word in its prose ("main", "mujhe", "maine"). Code strips nothing."""
    body = _body_text(answer or "")
    if (_PLANNING_RE.search(body) is not None or _THINKS_ALOUD_RE.search("\n".join(_prose_lines(answer))) is not None
            or _TOOL_WORD_RE.search(body) is not None or _PROMPT_TALK_RE.search(body) is not None):
        return True
    return _hinglish(answer) and _HINGLISH_I_RE.search(" ".join(_prose_words(answer))) is not None


def code_merge(question: str, knowledge_answer: str, rec: RecordAnswer, status: RecordsStatus,
               availability: Any = None, objects: Any = ()) -> MergeResult:
    """The lean-mode merge by code (latency plan step 2; ``SFA_MERGE=code``, the default): the record answer as it is,
    with at most :data:`CODE_MERGE_MAX_BULLETS` bullets of the lean facts block (the field meanings) before its Source
    line, and no model call.

    Never raises: an internal error gives the record answer with ``merged=False`` and ``error``. The early returns are
    :func:`combine`'s: ``rec.no_records`` gives an empty text (the caller prints the knowledge answer); a record answer
    with nothing but its Source line (:func:`~sf_assistant.agent.nudges.answer_text`) an empty text with error "the
    record answer is empty"; an empty knowledge answer the record answer with error "the knowledge agent gave no
    answer"; one that only said how to fetch records (:func:`clean_knowledge_answer`) the record answer with that error.
    A Hinglish record answer (:func:`_hinglish`, its code rows aside; step-2 fixes of 2026-10-30, F11) gets nothing:
    the record answer with ``merged=False`` and no error.

    The candidates are the "- " bullet lines of the facts block, in their order. A bullet is kept only when it is about
    a field the answer used, or, naming no field, about an object of the answer (:func:`_relevance`, by (object, field)
    pair: a bullet about a field the count did not use is left out, also when it names the counted object; a person
    tool's read list of objects makes no bullet relevant; ``objects``, the record catalog's objects, tells an object
    the evidence does not name from a field), its subject is no other object (:func:`_other_object_subject`, F4), and
    it holds no number the number audit would flag (:func:`audit_numbers` with :func:`combine`'s sources for
    ``knowledge_numbers=False``: the record facts, the quoted values and API names of the facts block, the question and
    the status line). A bullet is left out when it is cut off or longer than :data:`CODE_MERGE_MAX_CHARS`
    (:func:`_cut_off_bullet`; the unfinished-end test on the facts block's last line only, F6), working notes, tool talk
    or no field meaning (:func:`_working_bullet`: talk about the knowledge, the question or the prompt, a claim that no
    field links someone, advice, a guess, an echo of the lean prompt, a working-notes tail), names a person of the
    question (:func:`_names_people`), is only a label (:func:`~sf_assistant.agent.checks._label_only`), is already said
    by the record answer (the same words in the same order, :func:`_clause_words`), holds a number beyond the
    question's (dates aside) while the record answer says the name or field is not in the records (:func:`combine`'s
    not-found rule), contradicts the catalog or the answer's results (:func:`_contradiction`, F2, F3, F10; ``objects``
    as a dict name -> ObjectSpec: names only give no verdict), or adds a scope issue the merge call's answer would be
    rejected for (:func:`merge_scope_issues`: a condition no query applied, a field or object called not available that
    the warehouse holds, ``availability``; a caveat about a field the question is not about). Nothing is rewritten: a
    bullet is kept as the lean step wrote it, or left out. Of the bullets left, the first one that names only an object
    goes first, then those about a used field (first those whose subject field is a date field of the counting queries,
    :func:`_date_fields`, F7), then the other object ones, up to the cap; they are shown in their order.

    The counted line (:func:`_counted_line`, F8; never when the record answer says the name or field is not in the
    records, nor when code wrote the record answer from about_people) goes after the body, before the bullets. No
    bullet and no counted line: the record answer with
    ``merged=False`` and no error (the answer kind is then "records", as for an empty knowledge answer). Otherwise the
    record answer with a blank line, the counted line and the kept bullets before its Source line
    (:func:`_with_bullets`; after a list, the label line :data:`CODE_MERGE_LABEL` before the bullets; a record answer
    without a Source line gets one, :func:`with_source_line`) and ``merged=True`` when a bullet was kept (a counted
    line alone: ``merged=False``), unless the whole text fails the same number audit (the record answer with
    ``rejected_numbers``). The record answer's lead, numbers, names and dates are never touched; the name note of
    :func:`combine` (:func:`with_name_note`) is not applied, so the lead is the record answer's own.
    """
    t0 = time.perf_counter()
    if rec.no_records:
        return MergeResult(text="", merged=False)
    if not answer_text(rec.answer):   # nothing but a Source line: no record answer to add to or to fall back on
        return MergeResult(text="", merged=False, error="the record answer is empty",
                           seconds=round(time.perf_counter() - t0, 3))
    if not (knowledge_answer or "").strip():
        return MergeResult(text=rec.answer, merged=False, error="the knowledge agent gave no answer",
                           seconds=round(time.perf_counter() - t0, 3))
    try:
        knowledge = clean_knowledge_answer(knowledge_answer)
        if not knowledge:
            return MergeResult(text=rec.answer, merged=False,
                               error="the knowledge answer only said how to fetch records",
                               seconds=round(time.perf_counter() - t0, 3))
        if _hinglish(rec.answer, getattr(rec, "table_blocks", None)):   # F11: English bullets would switch language
            return MergeResult(text=rec.answer, merged=False, seconds=round(time.perf_counter() - t0, 3))
        catalog = objects if isinstance(objects, Mapping) else {}   # name -> ObjectSpec (names only: no verdict)
        # the sources of combine's audit for a lean facts block (knowledge_numbers=False): no number from its prose;
        # SFA_TABLES=code: the code rows' own text too (a stored line break reads as a space in a cell, as "n15" in the
        # facts); otherwise the list is as before
        sources = ([facts_json(rec.evidence, 10**9), " ".join(quoted_spans(knowledge)), question,
                    status.line() if status is not None else ""]
                   + list((getattr(rec, "table_blocks", None) or {}).values()))
        body = _body_text(rec.answer)
        used = _used_api(rec, objects)
        people = _people_words(rec)
        tri = getattr(rec, "triage", None) or {}
        not_found = _no_data_but_not_found(rec.evidence, tri.get("people_data",
                                                                 not (tri.get("missing") and not tri.get("people"))))
        asked = set(numbers_in(question))
        base = set(merge_scope_issues(rec.answer, rec, question, None, availability))   # the record answer's own
        lines = knowledge_answer.split("\n")
        last = max((j for j, ln in enumerate(lines) if ln.strip()), default=-1)   # a cut can only end the reply (F6)
        found: list[tuple[int, str, int]] = []   # (order, bullet, relevance) of the bullets that pass the checks
        for j, line in enumerate(lines):
            bullet = line.rstrip()
            if not _FACT_BULLET_RE.match(bullet) or any(b == bullet for _i, b, _k in found) or _label_only(bullet):
                continue
            kind = 0 if _cut_off_bullet(bullet, last=j == last) else _relevance(bullet, used)
            if kind and _other_object_subject(bullet, used, catalog):   # F4: about another object
                kind = 0
            if not kind:   # cut off, or about no field the answer used and (naming no field) none of its objects
                continue
            if audit_numbers(bullet, sources) or (not_found and any(
                    n not in asked for n in numbers_in(_DATE_TIME_RE.sub(" ", bullet)))):   # combine's not-found rule
                continue
            if _working_bullet(bullet, body) or _names_people(bullet, people):
                continue
            key = _clause_words(re.sub(r"^-[ \t]+", "", bullet))
            if key and f" {key} " in f" {_clause_words(body)} ":   # the record answer says it already
                continue
            if _contradiction(bullet, used, rec, catalog):   # F2, F3, F10: the catalog or the results say otherwise
                continue
            found.append((len(found), bullet, kind))
        object_bullets = [c for c in found if c[2] == 1]
        field_bullets = [c for c in found if c[2] == 2]
        dates = _date_fields(rec)   # F7: a bullet on a counting query's date field first
        dated = [c for c in field_bullets if dates and _subject_field(c[1], used).lower() in dates]
        kept: list[tuple[int, str, int]] = []
        for c in object_bullets[:1] + dated + [c for c in field_bullets if c not in dated] + object_bullets[1:]:
            if len(kept) >= CODE_MERGE_MAX_BULLETS:
                break
            trial = [b for _i, b, _k in sorted(kept + [c])]
            if set(merge_scope_issues(_with_bullets(rec.answer, trial), rec, question, None, availability)) - base:
                continue
            kept.append(c)
        # F8: what the one counting query counted; never after a not-found answer, nor for a people answer code wrote
        # from about_people (it states no count of that query)
        lead = ("" if not_found or getattr(rec, "code_answer", False)
                else _counted_line(rec, body, [b for _i, b, _k in sorted(kept)]))
        if not kept and not lead:
            return MergeResult(text=rec.answer, merged=False, seconds=round(time.perf_counter() - t0, 3))
        text = _with_bullets(rec.answer, [b for _i, b, _k in sorted(kept)], lead)
        bad = audit_numbers(text, sources)
        if bad:
            return MergeResult(text=rec.answer, merged=False, rejected_numbers=bad,
                               seconds=round(time.perf_counter() - t0, 3))
        text = with_source_line(text, rec.evidence, status)
    except Exception as exc:  # noqa: BLE001 - the record answer is still printed
        return MergeResult(text=rec.answer, merged=False, error=f"{type(exc).__name__}: {exc}"[:300],
                           seconds=round(time.perf_counter() - t0, 3))
    return MergeResult(text=text, merged=bool(kept), seconds=round(time.perf_counter() - t0, 3))
