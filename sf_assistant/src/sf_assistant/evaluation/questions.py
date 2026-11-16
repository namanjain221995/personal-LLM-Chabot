"""Question generation for the object-knowledge evaluation harness.

Four sources of questions are produced, all as :class:`Question` objects:

* ``gold``        -- :func:`gold_questions`, deterministic and derived purely from the
                     structure of the object-knowledge JSON (no LLM, no hardcoded object).
* ``adversarial`` -- :func:`adversarial_questions`, deterministic questions the knowledge
                     file *cannot* answer (record data, security, layouts, unknown fields).
* ``llm``         -- :func:`llm_questions`, free-form questions written by an LLM from
                     knowledge excerpts, validated and grounded against the JSON.
* ``paraphrase``  -- :func:`paraphrase`, business-language rewrites of existing questions
                     that keep the original expected facts and gold chunk ids.

Everything here is written against the ``ObjectKnowledge`` contract in docs/spec/SPEC.md, so it works
for any object file in the same schema. Generation is deterministic: phrasing templates are
rotated by index rather than sampled, so the same JSON always yields the same question set.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from collections import OrderedDict, defaultdict
from dataclasses import asdict
from typing import Any, Iterable, Sequence
from sf_assistant.agent.question import Question  # one class: the agents and the pipeline use it too
from sf_assistant.knowledge.store import child_chunk_id

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------------------------
# Public constants
# --------------------------------------------------------------------------------------------

CATEGORIES: list[str] = [
    "object_identity",
    "record_grain",
    "field_lookup",
    "field_meaning",
    "field_type",
    "picklist_values",
    "picklist_value_meaning",
    "reference_target",
    "relationship_semantics",
    "child_relationships",
    "temporal_audit",
    "validation_rules",
    "automation_writes",
    "automation_reads",
    "automation_triggers",
    "business_process",
    "legacy_vs_current",
    "typo_api_names",
    "semantic_role_search",
    "concept_to_field",
    "provenance_quality",
    "record_types",
    "cross_section",
    "unanswerable_record_data",
    "unanswerable_missing_knowledge",
]

#: Tool names exactly as defined in docs/spec/SPEC.md (tools.py).
TOOL_NAMES: list[str] = [
    "describe_object",
    "list_fields",
    "get_field",
    "search_fields",
    "get_picklist_values",
    "get_relationships",
    "get_child_relationships",
    "find_relationship",
    "get_temporal_semantics",
    "get_validation_rules",
    "get_automation",
    "get_business_processes",
    "search_knowledge",
    "get_provenance",
    "get_record_types",
]

#: Default tools that a well-behaved agent should call for each category. Used when a
#: generator (or the LLM) does not supply a more specific list.
DEFAULT_TOOLS: dict[str, list[str]] = {
    "object_identity": ["describe_object", "search_knowledge"],
    "record_grain": ["describe_object", "search_knowledge"],
    "field_lookup": ["search_fields", "get_field", "list_fields"],
    "field_meaning": ["get_field", "search_fields"],
    "field_type": ["get_field", "list_fields"],
    "picklist_values": ["get_picklist_values", "get_field"],
    "picklist_value_meaning": ["get_picklist_values", "get_field"],
    "reference_target": ["get_field", "get_relationships", "find_relationship"],
    "relationship_semantics": ["get_relationships", "find_relationship", "get_field"],
    "child_relationships": ["get_child_relationships", "find_relationship"],
    "temporal_audit": ["get_temporal_semantics", "get_field"],
    "validation_rules": ["get_validation_rules"],
    "automation_writes": ["get_automation"],
    "automation_reads": ["get_automation"],
    "automation_triggers": ["get_automation"],
    "business_process": ["get_business_processes", "get_automation"],
    "legacy_vs_current": ["get_field", "search_fields", "search_knowledge"],
    "typo_api_names": ["search_fields", "get_field", "list_fields"],
    "semantic_role_search": ["list_fields", "search_fields"],
    "concept_to_field": ["search_fields", "search_knowledge", "get_field"],
    "provenance_quality": ["get_provenance"],
    "record_types": ["get_record_types"],
    "cross_section": ["search_knowledge", "get_automation", "get_field"],
    "unanswerable_record_data": ["describe_object", "search_knowledge"],
    "unanswerable_missing_knowledge": ["search_knowledge", "get_field", "get_provenance"],
}

#: One-line description per category, passed to the question-generation prompt.
CATEGORY_DESCRIPTIONS: dict[str, str] = {
    "object_identity": "what the object is, its API name, label, plural label, business domains",
    "record_grain": "what one record represents and what hangs off a record",
    "field_lookup": "finding the API name of a field from its label or a business description",
    "field_meaning": "what a specific field stores or is used for",
    "field_type": "the data type of a field (picklist, date, reference, number, text, ...)",
    "picklist_values": "the allowed values of a picklist or multi-select picklist field",
    "picklist_value_meaning": "what a specific picklist value means",
    "reference_target": "which object a lookup/reference field points to",
    "relationship_semantics": "how the object relates to other objects, cardinality and roles",
    "child_relationships": "which child objects reference this object and through which field",
    "temporal_audit": "audit/temporal fields: created/modified date and user",
    "validation_rules": "validation rules, what they enforce and which fields they involve",
    "automation_writes": "which flows/Apex write which fields",
    "automation_reads": "which fields flows/Apex read",
    "automation_triggers": "what triggers an automation and what kind of automation it is",
    "business_process": "business processes the object takes part in and its role there",
    "legacy_vs_current": "legacy/older fields and what replaced them",
    "typo_api_names": "fields whose API names are misspelled in the schema",
    "semantic_role_search": "finding fields by semantic role (score, outcome, temporal, ...)",
    "concept_to_field": "mapping a business concept phrased in plain words to a field",
    "provenance_quality": "where the knowledge came from, review status, confidence",
    "record_types": "record types of the object",
    "cross_section": "questions that need two or more sections of the knowledge combined",
    "unanswerable_record_data": "questions about live record data, counts or values (not answerable)",
    "unanswerable_missing_knowledge": "questions about metadata the file does not contain",
}

#: Keyword queries used to pull knowledge excerpts for each category when asking an LLM to
#: write questions. Paired with chunk-id prefixes so excerpts are relevant even if BM25 is weak.
_CATEGORY_EXCERPTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "object_identity": ("object description label domain concept", ("object",)),
    "record_grain": ("record represents one grain", ("object.record_grain", "child:")),
    "field_lookup": ("field api name label", ("field:",)),
    "field_meaning": ("field meaning stores used for", ("field:",)),
    "field_type": ("field type date picklist number reference", ("field:",)),
    "picklist_values": ("picklist values options", ("picklist:",)),
    "picklist_value_meaning": ("picklist value meaning", ("picklist:",)),
    "reference_target": ("lookup reference target object", ("relationship:",)),
    "relationship_semantics": ("relationship cardinality role direction", ("relationship:",)),
    "child_relationships": ("child object related list", ("child:",)),
    "temporal_audit": ("created modified date timestamp user audit", ("temporal:",)),
    "validation_rules": ("validation rule error condition", ("validation:",)),
    "automation_writes": ("flow apex writes updates fields", ("flow:", "apex:")),
    "automation_reads": ("flow apex reads fields", ("flow:", "apex:")),
    "automation_triggers": ("trigger event autolaunched screen flow", ("flow:", "apex:")),
    "business_process": ("business process role", ("process:",)),
    "legacy_vs_current": ("legacy older newer replaced", ("field:",)),
    "typo_api_names": ("typo misspelled api name", ("field:",)),
    "semantic_role_search": ("semantic role concept", ("field:",)),
    "concept_to_field": ("concept business meaning", ("field:",)),
    "provenance_quality": ("provenance source review confidence", ("provenance",)),
    "record_types": ("record types", ("record_types", "provenance")),
    "cross_section": ("automation validation relationship process", ("flow:", "validation:", "process:")),
    "unanswerable_record_data": ("record count status value", ("object",)),
    "unanswerable_missing_knowledge": ("required security layout sharing", ("provenance", "object")),
}

_LEGACY_RX = re.compile(r"\b(legacy|older|deprecated|obsolete|superseded|no longer used)\b", re.I)
_TYPO_RX = re.compile(r"\b(typo|misspell(?:ed|ing)?|misspelt)\b", re.I)
_API_RX = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*__(?:c|r|mdt|e)\b")


# --------------------------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------------------------

# Question: sf_assistant.agent.question (imported above)


# --------------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------------

def _uniq(items: Iterable[str]) -> list[str]:
    """Order-preserving de-duplication that also drops empty strings."""
    return list(OrderedDict((s, None) for s in items if s))


def _human(token: str | None) -> str:
    """``long_text_area`` -> ``long text area``; ``many_to_one`` -> ``many to one``."""
    return re.sub(r"[_\s]+", " ", str(token or "")).strip()


def _fix_articles(text: str) -> str:
    """'a Internal Interview' -> 'an Internal Interview' (templates are label-agnostic)."""
    return re.sub(r"\b([Aa]) (?=[AEIOaeio])", lambda m: m.group(1) + "n ", text)


def _pick(templates: Sequence[str], i: int) -> str:
    """Deterministically rotate through phrasing templates."""
    return templates[i % len(templates)]


def _api_mentions(text: str, exclude: Iterable[str] = ()) -> list[str]:
    """Salesforce API names (``Foo__c``) mentioned in free text, minus ``exclude``."""
    ex = {e.lower() for e in exclude}
    return _uniq(m for m in _API_RX.findall(text or "") if m.lower() not in ex)


def _norm_text(s: str) -> str:
    """Normalisation used by :func:`dedupe`: lowercase, no punctuation, single spaces."""
    s = s.lower().replace("_", " ")
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _picklist_list(k: Any, api: str) -> list[str] | str | None:
    """Picklist values via the ObjectKnowledge API, falling back to the raw field dict."""
    try:
        vals = k.picklist_values(api)
    except Exception:  # pragma: no cover - defensive, knowledge.py is a separate module
        vals = None
    if vals is None:
        f = _field_by_api(k, api) or {}
        pl = f.get("picklist") or {}
        raw = pl.get("values")
        if isinstance(raw, list):
            vals = [str(v.get("value") or v.get("label")) if isinstance(v, dict) else str(v) for v in raw]
        elif isinstance(raw, str):
            vals = raw
    return vals


def _field_by_api(k: Any, api: str) -> dict | None:
    for f in k.fields():
        if str(f.get("api_name", "")).lower() == api.lower():
            return f
    return None


def _automations(raw: dict) -> list[tuple[str, dict]]:
    """All automation entries as ``(kind, entry)`` with kind ``"flow"`` or ``"apex"``."""
    auto = raw.get("automation") or {}
    out: list[tuple[str, dict]] = []
    for kind, key in (("flow", "flows"), ("apex", "apex")):
        for a in auto.get(key) or []:
            if isinstance(a, dict) and a.get("name"):
                out.append((kind, a))
    return out


def _snippet_facts(text: str) -> list[str]:
    """Atomic facts from a descriptive string such as ``'15-minute AM/PM time-slot values'``.

    Keeps tokens that carry information on their own (digits, slashes, all-caps); falls back
    to the first three words.
    """
    toks = [t.strip(",.;:()") for t in text.split()]
    keep = [t for t in toks if re.search(r"\d|/|^[A-Z]{2,}$", t)]
    return _uniq(keep) or [" ".join(toks[:3])]


class _Builder:
    """Accumulates questions for one source with per-category sequential qids.

    When built with the knowledge object, :meth:`add` drops expected facts that the question
    text already states (they would be earned by echoing the question back) and marks which
    facts must match literally. A question whose facts are *all* in its own text keeps them:
    the grader then checks them against the answer with the question echo removed. Such
    questions are listed in :attr:`tautological`.
    """

    def __init__(self, source: str, known_chunk_ids: set[str], k: Any = None):
        self.source = source
        self.known = known_chunk_ids
        self.k = k
        self.counters: dict[str, int] = defaultdict(int)
        self.items: list[Question] = []
        self.tautological: list[str] = []

    def _chunks(self, ids: Iterable[str]) -> list[str]:
        ids = _uniq(ids)
        if not self.known:
            return ids
        kept = [i for i in ids if i in self.known]
        # Never filter down to nothing: an id outside section_ids() still documents intent.
        return kept or ids

    def novel(self, facts: Iterable[str], question: str) -> list[str]:
        """The facts that ``question`` does not already state (all of them without knowledge)."""
        facts = [str(f) for f in facts if str(f).strip()]
        if self.k is None:
            return facts
        from . import grading

        return [f for f in facts if not grading.fact_in_question(f, question, self.k)]

    def add(
        self,
        category: str,
        question: str,
        facts: Iterable[str] = (),
        chunks: Iterable[str] = (),
        tools: Iterable[str] | None = None,
        answerable: bool = True,
        *,
        exact: Iterable[str] = (),
        forbidden: Iterable[str] = (),
    ) -> Question:
        """Append one question.

        Args:
            exact: facts that must match literally on top of the grader's category defaults.
            forbidden: confusable statements that fail the answer (see ``Question``).
        """
        assert category in CATEGORIES, category
        self.counters[category] += 1
        qid = f"{self.source}-{category}-{self.counters[category]:03d}"
        text = _fix_articles(re.sub(r"\s+", " ", question).strip())
        fact_list = _uniq(str(f) for f in facts) if answerable else []
        exact_set = {str(e) for e in exact}
        if self.k is not None and fact_list:
            from . import grading  # local import: grading is a sibling module

            exact_set |= {f for f in fact_list
                          if (category in grading.EXACT_API_CATEGORIES and grading.is_api_fact(f, self.k))
                          or category in grading.EXACT_VALUE_CATEGORIES}
            kept = [f for f in fact_list
                    if not grading.fact_in_question(f, text, self.k, exact=f in exact_set)]
            if kept:
                fact_list = kept
            else:
                self.tautological.append(qid)
        forbidden_list = [f for f in _uniq(str(x) for x in forbidden) if f not in fact_list]
        q = Question(
            qid=qid,
            category=category,
            question=text,
            expected_facts=fact_list,
            expected_tools=[t for t in _uniq(tools or DEFAULT_TOOLS[category]) if t in TOOL_NAMES],
            answerable=answerable,
            source=self.source,
            gold_chunk_ids=self._chunks(chunks),
            exact_facts=[f for f in fact_list if f in exact_set],
            forbidden_facts=forbidden_list if answerable else [],
        )
        self.items.append(q)
        return q


def _known_ids(k: Any) -> set[str]:
    try:
        return set(k.section_ids())
    except Exception:  # pragma: no cover - knowledge.py not available / incomplete
        return set()


def _fchunks(api: str, has_values: bool = False) -> list[str]:
    """Chunk ids for a field, plus its picklist chunk when it has concrete values."""
    return [f"field:{api}"] + ([f"picklist:{api}"] if has_values else [])


_SNAKE_RX = re.compile(r"\b[a-z]+(?:_[a-z]+)+\b")
_CAPS_RX = re.compile(r"\b[A-Z]{2,}(?:\s+[A-Z]{2,})*\b")
_PLACEHOLDER_ID_RX = re.compile(r"\b([A-Z]{2,})-x{3,}\b")
_QUOTED_RX = re.compile(r"'([^'\s][^']{1,38})'")
_TITLE_PHRASE_RX = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
_ENUM_RX = re.compile(r"\b(?:are|is|be)\s+([a-z]{3,}),\s+([a-z]{3,})\b[^,.]*,\s+or\s+([a-z]{3,})\b")
_CAP_RX = re.compile(r"\b(?:capped|limited)\s+at\s+(\w+)\b")
_NEWER_RX = re.compile(r"\bnewer\b[^.]*?\buses?\s+([a-z]+(?:\s+[a-z]+)?)", re.I)


def _salient_terms(text: str, exclude: Iterable[str] = (), limit: int = 3) -> list[str]:
    """Distinctive, checkable terms of a business-meaning string, most specific first.

    API names, snake_case tokens (``assign_owner``), id prefixes (``INT-`` from
    ``INT-xxxxx``), upper-case terms (``NOT NULL``, ``AWS``), quoted tokens, enumerated
    states ("ready, stale ..., or failed"), caps ("capped at two") and mid-sentence
    Title Case phrases ("Person Account"). Terms contained in a longer kept term are dropped.
    """
    text = text or ""
    ex = {e.lower() for e in exclude} | {"api"}  # "API name contains a typo" is not a fact
    out: list[str] = []
    out += _api_mentions(text)
    out += _SNAKE_RX.findall(text)
    out += [m + "-" for m in _PLACEHOLDER_ID_RX.findall(text)]
    for m in _ENUM_RX.finditer(text):
        out += list(m.groups())
    out += _CAP_RX.findall(text)
    out += _QUOTED_RX.findall(text)
    for m in _TITLE_PHRASE_RX.finditer(text):
        before = text[: m.start()].rstrip()
        if before and before[-1] not in ".!?:":  # skip sentence-initial capitals
            out.append(m.group(0))
    caps_src = _PLACEHOLDER_ID_RX.sub(" ", text)
    # Two-letter acronyms (AI, IR, CT) are too generic to show the meaning was understood.
    out += [m for m in _CAPS_RX.findall(caps_src) if not re.search(r"__", m) and len(m) > 2]
    terms = [t.strip() for t in _uniq(out) if t.strip() and t.strip().lower() not in ex]
    # Drop a term that is part of a longer kept term ("NULL" inside "NOT NULL").
    terms = [t for t in terms if not any(t != o and re.search(rf"(?i)\b{re.escape(t)}\b", o) for o in terms)]
    return terms[:limit]


def _respell(f: dict) -> str | None:
    """The 'corrected' spelling of a typo'd API name, from words in its label and meaning.

    ``Postion__c`` -> ``Position__c``, ``Final_Descion__c`` -> ``Final_Decision__c``,
    ``Selected_Time_Zone_Start_Tim__c`` -> ``Selected_Time_Zone_Start_Time__c``. None when
    nothing changes.
    """
    api = str(f.get("api_name") or "")
    m = re.match(r"^(.*?)(__c)?$", api)
    base, suffix = (m.group(1), m.group(2) or "") if m else (api, "")
    meaning = str(f.get("business_meaning") or "")
    quoted = {q.lower() for q in _QUOTED_RX.findall(meaning)}  # "the schema typo 'Descion'"
    meaning_words = {w.lower() for w in re.findall(r"[A-Za-z]{3,}", meaning)} - quoted
    vocab = meaning_words | {w.lower() for w in re.findall(r"[A-Za-z]{3,}", str(f.get("label") or ""))}
    parts = base.split("_")
    changed = False
    for i, part in enumerate(parts):
        low = part.lower()
        if len(low) < 3 or low in meaning_words:
            continue
        hit = difflib.get_close_matches(low, sorted(vocab - {low} - quoted), n=1, cutoff=0.75)
        if hit:
            parts[i] = hit[0][:1].upper() + hit[0][1:] if part[:1].isupper() else hit[0]
            changed = True
    return "_".join(parts) + suffix if changed else None


def _confusables(k: Any, api: str) -> list[str]:
    """Other fields easily mistaken for ``api``: same label, near-identical API name, or one's
    label equal to the other's split API name (``Date__c`` 'Scheduled Date' vs ``Scheduled_Date__c``)."""
    me = _field_by_api(k, api) or {}
    my_label = _norm_text(me.get("label") or "")
    my_split = _norm_text(re.sub(r"__c$", "", api))
    out = []
    for f in k.fields():
        other = str(f.get("api_name") or "")
        if not other or other.lower() == api.lower():
            continue
        o_label = _norm_text(f.get("label") or "")
        o_split = _norm_text(re.sub(r"__c$", "", other))
        if (my_label and o_label == my_label) or o_label == my_split or (my_label and my_label == o_split) \
                or difflib.SequenceMatcher(None, api.lower(), other.lower()).ratio() >= 0.9:
            out.append(other)
    return out


def _newer_phrase(meaning: str) -> str | None:
    """'Newer interview creation uses attendee rows' -> 'attendee rows' (what superseded it)."""
    m = _NEWER_RX.search(meaning or "")
    return m.group(1).strip().lower() if m else None


# --------------------------------------------------------------------------------------------
# Gold questions
# --------------------------------------------------------------------------------------------

def gold_questions(k: Any) -> list[Question]:
    """Deterministic gold questions derived purely from the object-knowledge JSON.

    Covers every category in :data:`CATEGORIES` that the file supports. Phrasing rotates
    through several templates per category so the set is not one template per category.

    Args:
        k: an ``ObjectKnowledge`` instance (SPEC contract).

    Returns:
        A list of :class:`Question` with ``source="gold"``.
    """
    b = _Builder("gold", _known_ids(k), k)
    raw: dict = k.raw
    obj: dict = raw.get("object") or {}
    api_obj: str = obj.get("api_name") or k.api_name
    label: str = obj.get("label") or api_obj
    plural: str = obj.get("plural_label") or f"{label} records"
    fields: list[dict] = [f for f in k.fields() if f.get("api_name")]

    # Labels shared by more than one field are ambiguous; ask about them by API name instead.
    label_count: dict[str, int] = defaultdict(int)
    for f in fields:
        label_count[str(f.get("label", "")).lower()] += 1

    def name_of(f: dict) -> str:
        lab = f.get("label") or f["api_name"]
        return f["api_name"] if label_count[str(lab).lower()] > 1 else lab

    _gold_object(b, obj, api_obj, label, plural, raw)
    _gold_fields(b, k, fields, label, name_of, label_count)
    _gold_picklists(b, k, fields, label, name_of)
    _gold_relationships(b, k, fields, api_obj, label, name_of)
    _gold_children(b, k, label, api_obj)
    _gold_temporal(b, raw, label)
    _gold_validation(b, raw, label)
    _gold_automation(b, k, raw, label, name_of)
    _gold_processes(b, raw, label, api_obj, fields)
    _gold_legacy_typo(b, k, fields, label, name_of)
    _gold_semantic(b, fields, label, name_of, k)
    _gold_provenance(b, raw, label)
    _gold_record_types(b, raw, label)
    _gold_cross_section(b, k, raw, fields, label, name_of)
    _gold_documented_semantics(b, k, raw, fields, label, name_of)
    _gold_unanswerable(b, k, fields, label, plural)
    if b.tautological:
        log.warning("%d gold question(s) only expect facts their own text states; they are graded "
                    "against the answer with the question echo removed: %s",
                    len(b.tautological), ", ".join(b.tautological))
    return b.items


def _gold_object(b: _Builder, obj: dict, api_obj: str, label: str, plural: str, raw: dict) -> None:
    sem = obj.get("business_semantics") or {}
    primary = sem.get("primary_concept") or label.lower()
    domains = obj.get("business_domain") or []
    concepts = sem.get("concepts") or []
    ids = ["object", "object.semantics", "object.discovery_summary"]
    description = str(obj.get("description") or "")
    # Concepts the description itself names besides the object's own name ("mock interview").
    described = [c for c in concepts if c.lower() != primary.lower() and c.lower() in description.lower()]

    b.add("object_identity", f"What is the API name of the {label} object?", [api_obj], ["object"],
          exact=[api_obj])
    b.add("object_identity", f"In plain terms, what does {api_obj} represent?", [primary] + described[:2], ids)
    b.add("object_identity", f"What is the plural label used for {api_obj} records?", [plural], ["object"])
    if domains:
        b.add("object_identity", f"Which business areas or domains does the {label} object belong to?",
              domains, ["object"])
    if "custom" in obj:
        is_custom = bool(obj.get("custom"))
        b.add("object_identity", f"Is {api_obj} a standard object or a custom object?",
              ["custom" if is_custom else "standard"], ["object"],
              forbidden=["standard object" if is_custom else "custom object"])
    if concepts:
        b.add("object_identity", f"Which business concepts is {label} meant to cover?",
              concepts[:6], ["object.semantics"])

    grain = (obj.get("record_grain") or {}).get("description")
    if grain:
        # What the grain says beyond the object's own name: the other concepts it covers and
        # the first kind of row that hangs off a record ("one or more attendee records").
        grain_facts = [c for c in concepts if c.lower() != primary.lower() and c.lower() in grain.lower()][:2]
        grain_facts += re.findall(r"\bone or more ([a-z]+)", grain)[:1]
        grain_facts = grain_facts or [primary]
        b.add("record_grain", f"What does a single {label} record stand for?", grain_facts,
              ["object.record_grain"])
        b.add("record_grain", f"If I count rows in {api_obj}, what exactly am I counting?", grain_facts,
              ["object.record_grain"])
    kid_rows = [c for c in raw.get("child_relationships") or [] if c.get("child_object")]
    children = list(dict.fromkeys(c["child_object"] for c in kid_rows))
    if children and grain:
        b.add("record_grain",
              f"Can one {label} have several related child records, and of which kinds?",
              children, ["object.record_grain"] + [child_chunk_id(c, kid_rows) for c in kid_rows],
              tools=["describe_object", "get_child_relationships"])


def _gold_fields(b: _Builder, k: Any, fields: list[dict], label: str, name_of, label_count) -> None:
    lookup_t = [
        "What is the API name of the '{lab}' field on {obj}?",
        "I see '{lab}' on the {obj} page layout. Which field is that in the API?",
        "Which {obj} field stores the {lab_l}? Give me its API name.",
        "For a SOQL query on {obj}, what API name should I use for '{lab}'?",
    ]
    meaning_t = [
        "What does the {name} field on {obj} store?",
        "Explain what the {name} field on {obj} is used for.",
        "What is the business meaning of the {name} field on {obj}?",
        "Can you describe the purpose of {name} on {obj}?",
    ]
    type_t = [
        "What data type is the {name} field?",
        "What kind of field is {name}, and what is its exact type?",
        "What field type does {api} have?",
    ]
    by_desc_t = [
        "Which {obj} field holds the {desc}? I need the API name.",
        "I'm looking for the field that stores the {desc}. What is it called in the API?",
        "What's the API name of the field for the {desc}?",
    ]
    n: dict[str, int] = defaultdict(int)  # per-category template counter, so phrasing rotates

    def desc_of(f: dict) -> str | None:
        """First clause of business_meaning, usable as a noun phrase ('the <desc>')."""
        first = re.split(r"(?<=[.;])\s", (f.get("business_meaning") or "").strip())[0].rstrip(".;")
        words = first.split()
        if not (3 <= len(words) <= 16) or f["api_name"].lower() in first.lower():
            return None
        if words[0].lower() in {"true", "set", "indicates", "explains", "describes", "tracks",
                                "determines", "records", "for", "stores", "defines", "the"}:
            return None  # verb-led sentences do not read as a noun phrase
        # Lower-case the first word only when it is an ordinary word ("Reason ..." -> "reason ..."),
        # not an acronym or a proper term ("JSON ...", "Interview Readiness ...").
        if words[0][1:].islower() and (len(words) < 2 or not words[1][:1].isupper()):
            first = first[0].lower() + first[1:]
        return first

    for i, f in enumerate(fields):
        api, lab = f["api_name"], f.get("label") or f["api_name"]
        name = name_of(f)
        chunks = [f"field:{api}"]
        slot = i % 3
        label_is_trivial = lab.lower() == _human(re.sub(r"__c$", "", api)).lower()
        desc = desc_of(f)
        # A lookup answer naming a confusable sibling (same label, near-identical API name,
        # or the "corrected" spelling of a typo'd name) is wrong even if it also hedges.
        confusable = _confusables(k, api) + ([_respell(f)] if _TYPO_RX.search(f.get("business_meaning") or "") else [])
        confusable = [c for c in confusable if c]
        if slot == 0 and label_count[lab.lower()] == 1 and not label_is_trivial:
            tmpl = _pick(lookup_t, n["lookup"])
            if _is_legacy(f) and "{lab_l}" in tmpl:
                # "Which field stores the scheduled date?" has a better (current) answer than the
                # legacy field: ask for the exact label instead.
                tmpl = lookup_t[0]
            b.add("field_lookup", tmpl.format(lab=lab, lab_l=lab.lower(), obj=label), [api], chunks,
                  forbidden=confusable)
            n["lookup"] += 1
        elif slot == 0 and desc:
            b.add("field_lookup", _pick(by_desc_t, n["desc"]).format(desc=desc, obj=label), [api], chunks,
                  forbidden=confusable)
            n["desc"] += 1
        elif slot in (0, 1):
            # The meaning itself must be checked, not just the field's own name (which the
            # question states): distinctive terms of business_meaning plus lookup targets.
            refs = (f.get("reference") or {}).get("target_objects") or []
            terms = _salient_terms(f.get("business_meaning", ""), exclude=[api, label, k.api_name], limit=6)
            # A quoted typo token ('Descion') is part of the API name, not of the meaning.
            terms = [t for t in terms if t.lower() not in api.lower()]
            qtext = _pick(meaning_t, n["meaning"]).format(name=name, obj=label)
            facts = b.novel(terms + list(refs), qtext)[:3]
            b.add("field_meaning", qtext, facts or [api], chunks,
                  exact=[t for t in refs if t in facts])
            n["meaning"] += 1
        elif f.get("type"):
            b.add("field_type", _pick(type_t, n["type"]).format(name=name, api=api), [_human(f["type"])], chunks)
            n["type"] += 1

    _gold_field_properties(b, fields, name_of)

    # Label collisions are a classic lookup trap: ask for both API names.
    by_label: dict[str, list[dict]] = defaultdict(list)
    for f in fields:
        by_label[str(f.get("label", "")).lower()].append(f)
    for group in by_label.values():
        if len(group) > 1:
            apis = [g["api_name"] for g in group]
            b.add("field_lookup",
                  f"There seem to be several fields labelled '{group[0].get('label')}' on {label}. "
                  "What are their API names and how do they differ?",
                  apis + [_human(g.get("type")) for g in group], [f"field:{a}" for a in apis])


def _gold_field_properties(b: _Builder, fields: list[dict], name_of) -> None:
    """Numeric precision/scale, text length, and calculated / roll-up summary fields."""
    for f in fields:
        api, name = f["api_name"], name_of(f)
        if f.get("precision") is not None and f.get("scale") is not None:
            b.add("field_type", f"What are the precision and scale of the {name} field?",
                  [str(f["precision"]), str(f["scale"])], [f"field:{api}"], tools=["get_field"],
                  exact=[str(f["precision"]), str(f["scale"])])
    by_len: dict[Any, list[dict]] = defaultdict(list)
    for f in fields:
        if f.get("length"):
            by_len[f["length"]].append(f)
    for length, group in sorted(by_len.items(), key=lambda kv: str(kv[0])):
        f = group[0]  # one question per distinct length keeps the set balanced
        b.add("field_type", f"What is the maximum length of the {name_of(f)} field?",
              [str(length)], [f"field:{f['api_name']}"], tools=["get_field"], exact=[str(length)])
    calculated = [f["api_name"] for f in fields if f.get("calculated") is True]
    if calculated:
        b.add("field_type", "Which fields are calculated by Salesforce rather than entered or set by automation?",
              calculated, [f"field:{a}" for a in calculated], tools=["list_fields", "get_field"])
    for f in fields:
        if str(f.get("type")) == "rollup_summary":
            terms = _salient_terms(f.get("business_meaning", ""), exclude=[f["api_name"]])
            b.add("field_type", "Is there a roll-up summary field, and what does it count?",
                  [f["api_name"]] + terms[:1], [f"field:{f['api_name']}"], tools=["list_fields", "get_field"])


def _gold_picklists(b: _Builder, k: Any, fields: list[dict], label: str, name_of) -> None:
    list_t = [
        "What values can {name} take?",
        "List the allowed picklist options for {name} on {obj}.",
        "Which choices are available in the {name} dropdown?",
        "What are the possible {name} values?",
    ]
    multi_t = [
        "{name} is multi-select. Which options can be selected?",
        "Which items can be ticked in the multi-select {name} field?",
    ]
    meaning_t = [
        "What does the value '{val}' mean on {name}?",
        "If {name} is set to '{val}', what does that indicate?",
        "How should I interpret {name} = '{val}'?",
    ]
    n_meaning = 0
    for i, f in enumerate(fields):
        if not f.get("picklist"):
            continue
        api, name = f["api_name"], name_of(f)
        vals = _picklist_list(k, api)
        if isinstance(vals, list) and vals:
            tmpl = multi_t if "multi" in str(f.get("type", "")) else list_t
            b.add("picklist_values", _pick(tmpl, i).format(name=name, obj=label), vals, _fchunks(api, True))
            for v in (f.get("picklist") or {}).get("values") or []:
                if isinstance(v, dict) and v.get("business_meaning"):
                    val = v.get("value") or v.get("label")
                    # Expect the meaning itself (matched on its content words, with polarity),
                    # not the value and field the question already names.
                    b.add("picklist_value_meaning", _pick(meaning_t, n_meaning).format(name=name, val=val),
                          [str(v["business_meaning"]).rstrip(".")], _fchunks(api, True))
                    n_meaning += 1
        elif isinstance(vals, str) and vals.strip():
            b.add("picklist_values", f"What kind of values does the {name} picklist hold?",
                  _snippet_facts(vals), [f"field:{api}"])

    # Documented lifecycle orders ("The normal lifecycle is A to B to C, with D, E and F as
    # alternate states").
    for f in fields:
        m = re.search(r"lifecycle is ([^,.]+?)(?:,\s*with ([^.]+?) as alternate states)?\.",
                      f.get("business_meaning") or "")
        if not m:
            continue
        main = [x.strip() for x in re.split(r"\s+to\s+", m.group(1)) if x.strip()]
        alt = [x.strip() for x in re.split(r",\s*|\s+and\s+", m.group(2) or "") if x.strip()]
        if len(main) >= 2:
            b.add("picklist_values",
                  f"What is the normal lifecycle of {name_of(f)} on {label}, and which states are alternates?",
                  main + alt, _fchunks(f["api_name"], isinstance(_picklist_list(k, f["api_name"]), list)),
                  tools=["get_field", "get_picklist_values"])

    # Outcome picklists with different value sets ("No Hire" vs "NoHire", Retake only on one).
    by_role: dict[str, list[dict]] = defaultdict(list)
    for f in fields:
        if isinstance(_picklist_list(k, f["api_name"]), list):
            for r in (f.get("semantic") or {}).get("roles") or []:
                if r == "outcome":
                    by_role[r].append(f)
    for role, group in by_role.items():
        if len(group) < 2:
            continue
        sets = [set(_picklist_list(k, g["api_name"]) or []) for g in group]  # type: ignore[arg-type]
        common = set.intersection(*sets)
        diff = _uniq(v for vals in sets for v in sorted(vals - common))
        if diff:
            names = ", ".join(name_of(g) for g in group)
            b.add("cross_section", f"How do the value sets of {names} differ?",
                  diff[:6], [f"picklist:{g['api_name']}" for g in group],
                  tools=["get_picklist_values", "get_field"], exact=diff[:6])

    # Picklists that share an identical value set are worth knowing about.
    groups: dict[tuple, list[str]] = defaultdict(list)
    for f in fields:
        vals = _picklist_list(k, f["api_name"]) if f.get("picklist") else None
        if isinstance(vals, list) and len(vals) >= 3:
            groups[tuple(sorted(vals))].append(f["api_name"])
    for apis in groups.values():
        if len(apis) > 1:
            b.add("cross_section",
                  f"Which {label} picklist fields share exactly the same set of values as {apis[0]}?",
                  apis, [f"picklist:{a}" for a in apis], tools=["get_picklist_values", "list_fields"])


def _gold_relationships(b: _Builder, k: Any, fields: list[dict], api_obj: str, label: str, name_of) -> None:
    target_t = [
        "Which object does {name} point to?",
        "{name} is a lookup. What object is on the other end of it?",
        "What is the target object of the {api} reference?",
        "When I follow {name} from a {obj}, which object do I land on?",
    ]
    rels = {r.get("field"): r for r in k.relationships() if r.get("field")}
    refs = [f for f in fields if (f.get("reference") or {}).get("target_objects")
            or str(f.get("type")) == "reference"]
    for i, f in enumerate(refs):
        api = f["api_name"]
        targets = (f.get("reference") or {}).get("target_objects") or (rels.get(api) or {}).get("target_objects") or []
        chunks = [f"field:{api}"] + ([f"relationship:{api}"] if api in rels else [])
        # Targets are object names: exact, so a same-named field's label cannot stand in.
        b.add("reference_target", _pick(target_t, i).format(name=name_of(f), api=api, obj=label),
              list(targets), chunks, exact=[t for t in targets if _API_RX.search(t)])

    sem_t = [
        "What role does the {flabel} lookup ({field}) play for a {obj}, what does it point to, and what is its cardinality?",
        "How is {obj} connected to other records through {field}, and what is the cardinality?",
        "Describe the {flabel} relationship on {obj}: target object, cardinality and meaning.",
    ]
    labels = {f["api_name"]: f.get("label") or f["api_name"] for f in fields}
    by_target: dict[str, list[str]] = defaultdict(list)
    for i, (fld, r) in enumerate(rels.items()):
        targets = r.get("target_objects") or []
        for t in targets:
            by_target[t].append(fld)
        facts = [fld] + list(targets)
        if r.get("cardinality"):
            facts.append(_human(r["cardinality"]))
        b.add("relationship_semantics",
              _pick(sem_t, i).format(flabel=labels.get(fld, fld), field=fld, obj=label),
              facts, [f"relationship:{fld}", f"field:{fld}"], exact=[t for t in targets if _API_RX.search(t)])
    rel_targets = set(by_target)
    for f in refs:  # reference fields without a relationships entry still count as lookups
        for t in (f.get("reference") or {}).get("target_objects") or []:
            if f["api_name"] not in by_target[t]:
                by_target[t].append(f["api_name"])

    def _multi_lookup(t: str, flds: list[str]) -> None:
        b.add("relationship_semantics",
              f"{label} has more than one lookup to {t}. Which fields are they and what does each represent?",
              flds, [f"relationship:{x}" if x in rels else f"field:{x}" for x in flds])

    for t, flds in by_target.items():
        if t in rel_targets and len(flds) > 1:
            _multi_lookup(t, flds)
    self_refs = by_target.get(api_obj) or []
    for fld in self_refs:
        b.add("relationship_semantics", f"Does {api_obj} link to itself? Through which field and why?",
              [fld], [f"relationship:{fld}" if fld in rels else f"field:{fld}"])
    for t, flds in by_target.items():  # groups only visible through field references: appended last
        if t not in rel_targets and len(flds) > 1:
            _multi_lookup(t, flds)


def _gold_children(b: _Builder, k: Any, label: str, api_obj: str) -> None:
    kids = [c for c in k.child_relationships() if c.get("child_object")]
    if not kids:
        return
    b.add("child_relationships", f"Which child objects reference {api_obj}?",
          list(dict.fromkeys(c["child_object"] for c in kids)), [k.child_chunk_id(c) for c in kids])
    tmpl = [
        "Which field on {child} links it back to its parent {obj}?",
        "Where would I find the {meaning_l} for a {obj}? Which object holds them?",
        "How is {child} related to {obj}?",
    ]
    for i, c in enumerate(kids):
        child = c["child_object"]
        meaning = (c.get("business_meaning") or _human(c.get("business_role")) or child).rstrip(".")
        t = _pick(tmpl, i)
        facts = [child] if "{meaning_l}" in t else [c.get("field") or "", child]
        b.add("child_relationships",
              t.format(child=child, obj=label, meaning_l=meaning[0].lower() + meaning[1:]),
              facts, [k.child_chunk_id(c)], exact=[x for x in facts if _API_RX.search(x)])


_TEMPORAL_PHRASES = {
    "created_at": "when a {obj} record was created",
    "updated_at": "when a {obj} record was last modified",
    "created_by": "who created a {obj} record",
    "last_updated_by": "who last modified a {obj} record",
    "updated_by": "who last modified a {obj} record",
}


def _gold_temporal(b: _Builder, raw: dict, label: str) -> None:
    temporal = raw.get("temporal_semantics") or {}
    tmpl = [
        "Which field tells me {phrase}?",
        "For auditing, what field captures {phrase}?",
    ]
    all_fields: list[str] = []
    for i, (key, spec) in enumerate(temporal.items()):
        fld = (spec or {}).get("field") if isinstance(spec, dict) else None
        if not fld:
            continue
        all_fields.append(fld)
        phrase = _TEMPORAL_PHRASES.get(key, f"the '{_human(key)}' information of a {{obj}} record").format(obj=label)
        b.add("temporal_audit", _pick(tmpl, i).format(phrase=phrase), [fld], [f"temporal:{key}"])
    if len(all_fields) > 1:
        b.add("temporal_audit", f"Which fields make up the audit trail (created/modified date and user) on {label}?",
              all_fields, [f"temporal:{k_}" for k_ in temporal])


def _gold_validation(b: _Builder, raw: dict, label: str) -> None:
    rules = [r for r in raw.get("validation_rules") or [] if r.get("name")]
    if not rules:
        return
    b.add("validation_rules", f"What validation rules are defined on {label}?",
          [r["name"] for r in rules], [f"validation:{r['name']}" for r in rules])
    on_field: dict[str, list[str]] = defaultdict(list)
    for r in rules:
        flds = r.get("fields_involved") or []
        for f in flds:
            on_field[f].append(r["name"])
        b.add("validation_rules", f"What does the {r['name']} validation rule enforce, and which fields does it check?",
              [r["name"]] + list(flds), [f"validation:{r['name']}"])
        if "active" in r and r["active"] is not None:
            b.add("validation_rules", f"Is the validation rule {r['name']} currently active?",
                  ["active" if r["active"] else "inactive"], [f"validation:{r['name']}"],
                  forbidden=["inactive"] if r["active"] else ["active"])
        formula = str(r.get("formula") or "")
        if "ISNEW" in formula.upper():
            # NOT(ISNEW()) limits the rule to edits of existing records; PRIORVALUE compares
            # against the value before the edit.
            facts = ["existing"] if re.search(r"NOT\s*\(\s*ISNEW", formula, re.I) else ["new"]
            facts += re.findall(r'PRIORVALUE\([^)]*\)\s*,\s*"([^"]+)"', formula.replace("\n", " "))[:1]
            b.add("validation_rules",
                  f"Does the {r['name']} rule apply when a record is first created, or only on later edits? "
                  "What exactly does its formula compare?",
                  facts, [f"validation:{r['name']}"])
    by_rule = {r["name"]: r for r in rules}
    for f, names in on_field.items():
        effects = []
        for n in names:
            em = str(by_rule[n].get("error_meaning") or "")
            m = (re.search(r"\b(?:treated as|considered|becomes?)\s+(?:an?\s+|the\s+)?([a-z]{4,})", em)
                 or re.search(r"\bis\s+(?:an?\s+|the\s+)?([a-z]{4,})", em))
            if m:
                effects.append(m.group(1))
        b.add("validation_rules", f"Are there any validation rules that look at {f}? What happens when they fire?",
              names + effects[:1], [f"validation:{n}" for n in names] + [f"field:{f}"],
              tools=["get_validation_rules", "get_field"])


def _gold_automation(b: _Builder, k: Any, raw: dict, label: str, name_of) -> None:
    autos = _automations(raw)
    writes_t = [
        "Which fields does {name} update on {obj}?",
        "What does the {name} {kind} write to?",
        "List the {obj} fields set by {name}.",
    ]
    reads_t = [
        "Which fields does {name} read?",
        "What inputs does the {name} {kind} look at?",
        "Which fields does {name} depend on?",
    ]
    written_by: dict[str, list[str]] = defaultdict(list)
    for i, (kind, a) in enumerate(autos):
        cid = f"{kind}:{a['name']}"
        kind_word = "flow" if kind == "flow" else "Apex class"
        if a.get("fields_written"):
            b.add("automation_writes", _pick(writes_t, i).format(name=a["name"], obj=label, kind=kind_word),
                  a["fields_written"], [cid])
            for f in a["fields_written"]:
                written_by[f].append(a["name"])
        if a.get("fields_read"):
            b.add("automation_reads", _pick(reads_t, i).format(name=a["name"], kind=kind_word),
                  a["fields_read"], [cid])
        trig = a.get("trigger")
        if isinstance(trig, dict) and trig.get("object"):
            b.add("automation_triggers", f"What triggers {a['name']} to run?",
                  [trig["object"]] + list(trig.get("events") or []), [cid])
        if a.get("type"):
            b.add("automation_triggers",
                  _pick(["What kind of automation is {name}, and how is it started?",
                         "Is {name} user-launched or does it run automatically? What type is it?"], i)
                  .format(name=a["name"]), [_human(a["type"])], [cid])

    rev_t = [
        "Which automation sets the {name} field?",
        "What flow or Apex code writes to the {name} field on {obj}?",
        "If the {name} field changes without anyone editing it, which automation could be responsible?",
    ]
    for i, (fld, names) in enumerate(sorted(written_by.items())):
        if "." in fld:
            continue  # cross-object write: not a field of this object
        f = _field_by_api(k, fld)
        disp = name_of(f) if f else fld
        kinds = {a["name"]: kd for kd, a in autos}
        b.add("automation_writes", _pick(rev_t, i).format(name=disp, obj=label), names,
              [f"{kinds[n]}:{n}" for n in names] + [f"field:{fld}"])

    # Automations fired by other objects' events.
    by_obj: dict[str, list[str]] = defaultdict(list)
    for kind, a in autos:
        trig = a.get("trigger")
        if isinstance(trig, dict) and trig.get("object"):
            by_obj[trig["object"]].append(a["name"])
    for o, names in by_obj.items():
        b.add("automation_triggers", f"Which automations fire when a {o} record is saved?",
              names, [f"flow:{n}" for n in names])

    # Apex entry points (REST resources, triggers) are often asked about.
    for kind, a in autos:
        if kind == "apex" and "rest" in str(a.get("type", "")):
            b.add("automation_triggers", f"Is there an external API or REST endpoint that updates {label}?",
                  [a["name"]] + list(a.get("fields_written") or []), [f"apex:{a['name']}"])

    # Purpose of every automation: distinctive terms of its purpose, the other objects it
    # works with, or (when neither exists) the business process it implements.
    api_obj = k.api_name
    proc_names = {p.get("process_id"): p.get("process_name") for p in raw.get("business_process_usage") or []}
    purpose_t = [
        "What is the {name} {kind} for, and which other objects does it work with?",
        "What does {name} do?",
    ]
    for i, (kind, a) in enumerate(autos):
        purpose = str(a.get("purpose") or "")
        if not purpose:
            continue
        kind_word = "flow" if kind == "flow" else "Apex class"
        others = [o for o in a.get("related_objects") or [] if o != api_obj]
        terms = _salient_terms(purpose, exclude=[a["name"], label, api_obj])
        facts = (terms + others)[:3] or [proc_names.get(a.get("business_process")) or _human(a.get("business_process"))]
        b.add("automation_triggers", _pick(purpose_t, i).format(name=a["name"], kind=kind_word),
              [f for f in facts if f], [f"{kind}:{a['name']}"], tools=["get_automation"])

    # Objects that several automations work with ("Which automations touch Resume__c?").
    touching: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for kind, a in autos:
        for o in a.get("related_objects") or []:
            if o != api_obj:
                touching[o].append((kind, a["name"]))
    for o, items in sorted(touching.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if len(items) >= 2:
            b.add("cross_section", f"Which {label} automations also work with {o} records?",
                  [n for _, n in items], [f"{kd}:{n}" for kd, n in items], tools=["get_automation"])

    # Source-of-truth automations: edit the source object, not the mirror.
    for kind, a in autos:
        purpose = str(a.get("purpose") or "")
        trig = a.get("trigger") if isinstance(a.get("trigger"), dict) else {}
        src = (trig or {}).get("object")
        written = [w for w in a.get("fields_written") or [] if "." not in w]
        if src and written and re.search(r"source of truth", purpose, re.I):
            f = _field_by_api(k, written[0])
            disp = name_of(f) if f else written[0]
            b.add("cross_section",
                  f"If an {label}'s {disp} is wrong, should I fix it on the {label} itself or somewhere else? "
                  "Which record is the source of truth?",
                  [src], [f"{kind}:{a['name']}", f"field:{written[0]}"],
                  tools=["get_automation", "get_field"])


def _phrase_to_field(phrase: str, fields: list[dict]) -> str | None:
    """Field for a short business phrase ("result deadlines" -> ``Deadline__c``).

    A field qualifies when at least two word stems of the phrase occur in its label or
    semantic concept; the phrase maps only when exactly one field qualifies (no guessing).
    """
    def stems(text: str) -> set[str]:
        return {re.sub(r"(?:ings?|ed|s)$", "", w) for w in re.findall(r"[a-z]{3,}", text.lower())} - {"the", "and"}
    want = stems(phrase)
    hits = [f["api_name"] for f in fields
            if len(want & stems(f"{f.get('label', '')} {_human((f.get('semantic') or {}).get('concept'))}")) >= 2]
    return hits[0] if len(hits) == 1 else None


def _process_fields(meaning: str, fields: list[dict]) -> list[str]:
    """Fields a process meaning points at: API names it mentions plus what it says it tracks."""
    out = _api_mentions(meaning)
    m = re.search(r"\btracks?\s+([^.]+)", meaning or "")
    if m:
        for phrase in re.split(r",\s*|\s+and\s+", m.group(1)):
            api = _phrase_to_field(phrase, fields) if phrase.strip() else None
            if api:
                out.append(api)
    return _uniq(out)


def _gold_processes(b: _Builder, raw: dict, label: str, api_obj: str, fields: list[dict] | None = None) -> None:
    procs = [p for p in raw.get("business_process_usage") or [] if p.get("process_id")]
    if not procs:
        return
    b.add("business_process", f"Which business processes is {label} part of?",
          [p.get("process_name") or p["process_id"] for p in procs], [f"process:{p['process_id']}" for p in procs],
          tools=["get_business_processes"])
    by_proc: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for kind, a in _automations(raw):
        if a.get("business_process"):
            by_proc[a["business_process"]].append((kind, a["name"]))
    tmpl = [
        "What role does {obj} play in the {pname} process?",
        "How is {api} used in '{pname}'?",
        "In {pname}, is {obj} the primary record or something else?",
    ]
    for i, p in enumerate(procs):
        pid, pname = p["process_id"], p.get("process_name") or _human(p["process_id"])
        role = _human(p.get("object_role"))
        b.add("business_process", _pick(tmpl, i).format(obj=label, api=api_obj, pname=pname),
              [role] if role else [pname], [f"process:{pid}"], tools=["get_business_processes"])
        autos = by_proc.get(pid) or []
        if autos:
            b.add("business_process", f"Which flows or Apex classes implement the {pname} process?",
                  [n for _, n in autos], [f"process:{pid}"] + [f"{kd}:{n}" for kd, n in autos])
        pf = _process_fields(str(p.get("meaning") or ""), fields or [])
        own_fields = {f["api_name"] for f in fields or []}
        pf = [x for x in pf if x in own_fields]  # only this object's fields (not related objects)
        if pf:
            b.add("business_process", f"Which {label} fields carry the {pname} information?",
                  pf, [f"process:{pid}"] + [f"field:{x}" for x in pf],
                  tools=["get_business_processes", "search_fields", "get_field"])


def _is_legacy(f: dict) -> bool:
    """True when a field is marked legacy by its semantic roles/concept or its business meaning."""
    sem = f.get("semantic") or {}
    roles = [str(r) for r in sem.get("roles") or []]
    concept = str(sem.get("concept") or "")
    return (any("legacy" in r for r in roles) or concept.startswith("legacy")
            or bool(_LEGACY_RX.search(f.get("business_meaning", "") or "")))


def _gold_legacy_typo(b: _Builder, k: Any, fields: list[dict], label: str, name_of) -> None:
    concepts = {(f.get("semantic") or {}).get("concept"): f["api_name"] for f in fields
                if (f.get("semantic") or {}).get("concept")}
    # Legacy = marked legacy, or described as superseded by a newer model ("Newer interview
    # creation uses attendee rows") even without the word.
    legacy: list[dict] = [f for f in fields if _is_legacy(f) or _newer_phrase(f.get("business_meaning", ""))]
    with_successor_t = [
        "Is {name} still the field to use, or is it legacy? What replaced it?",
        "Which field is legacy for the {topic}, and what should I use instead?",
        "Should new reports use {name}? Is there a newer equivalent?",
    ]
    for i, f in enumerate(legacy):
        api = f["api_name"]
        meaning = f.get("business_meaning", "") or ""
        concept = str((f.get("semantic") or {}).get("concept") or "")
        # A replacement is asked for only when the file names one: an API name in the meaning,
        # a concept pair legacy_X -> X, or the "newer ... uses <phrase>" wording.
        replacements = _api_mentions(meaning, exclude=[api])
        stripped = re.sub(r"^legacy_", "", concept)
        if stripped != concept and stripped in concepts and concepts[stripped] != api:
            replacements.insert(0, concepts[stripped])
        newer = _newer_phrase(meaning)
        marked = _is_legacy(f)
        topic = _human(stripped or concept) or (f.get("label") or api).lower()
        chunks = [f"field:{api}"] + [f"field:{r}" for r in replacements if _field_by_api(k, r)]
        if replacements or newer:
            facts = [api] + (["legacy"] if marked else []) + replacements + ([newer] if newer and not replacements else [])
            b.add("legacy_vs_current", _pick(with_successor_t, i).format(name=name_of(f), topic=topic),
                  facts, chunks)
        else:
            # The file marks it legacy but names no successor: do not invite a guess. A named
            # replacement is graded as a hallucination by the judge, and a confusable sibling
            # stated as the replacement fails deterministically.
            b.add("legacy_vs_current",
                  f"Is {name_of(f)} a legacy field? Does the knowledge file say what replaced it?",
                  [api, "legacy"], chunks, forbidden=_confusables(k, api))
    if len(legacy) > 1:
        b.add("legacy_vs_current", f"Which {label} fields are legacy or older and should be avoided?",
              [f["api_name"] for f in legacy], [f"field:{f['api_name']}" for f in legacy])

    typos = [f for f in fields if _TYPO_RX.search(f.get("business_meaning", ""))]
    tmpl_t = [
        "What is the exact API name of the {lab} field? I suspect it is spelled oddly.",
        "I tried {guess} in a query and it failed. What is the real API name for {lab}?",
        "How is the {lab} field spelled in the Salesforce schema?",
    ]
    for i, f in enumerate(typos):
        api, lab = f["api_name"], f.get("label") or f["api_name"]
        guess_base = lab
        if sum(1 for g in fields if str(g.get("label", "")).lower() == lab.lower()) > 1:
            lab = f"{lab} ({_human(f.get('type'))})"  # disambiguate colliding labels by type
        corrected = _respell(f)
        guess = corrected or re.sub(r"[^A-Za-z0-9]+", "_", guess_base).strip("_") + "__c"
        if guess.lower() == api.lower():
            guess = api.replace("__c", "") + "_Field__c"
        # The "corrected" spelling and any near-identical sibling are wrong answers.
        forbidden = _uniq([x for x in [corrected, *_confusables(k, api)] if x])
        b.add("typo_api_names", _pick(tmpl_t, i).format(lab=lab, guess=guess), [api], [f"field:{api}"],
              forbidden=forbidden)
    if len(typos) > 1:
        b.add("typo_api_names", f"Which {label} API names contain spelling mistakes?",
              [f["api_name"] for f in typos], [f"field:{f['api_name']}" for f in typos])
    # Near-miss names: the user types the correctly spelled name of a typo'd picklist field.
    # The answer must point to the real field (and may list its values), not refuse or invent.
    existing = {f["api_name"].lower() for f in fields}
    n_near = 0
    for f in typos:
        corrected = _respell(f)
        vals = _picklist_list(k, f["api_name"])
        if not corrected or corrected.lower() in existing or not isinstance(vals, list) or n_near >= 2:
            continue
        b.add("typo_api_names", f"What values does {corrected} take on {label}?",
              [f["api_name"]] + vals[:6], _fchunks(f["api_name"], True),
              tools=["get_field", "search_fields", "get_picklist_values"], exact=vals[:6])
        n_near += 1


def _gold_semantic(b: _Builder, fields: list[dict], label: str, name_of, k: Any = None) -> None:
    # semantic_role_search: group by role; useful groups have 2..8 members.
    by_role: dict[str, list[str]] = defaultdict(list)
    for f in fields:
        for r in (f.get("semantic") or {}).get("roles") or []:
            by_role[str(r)].append(f["api_name"])
    role_t = [
        "Which {obj} fields act as {role} fields?",
        "Show me every field with the '{role}' role.",
        "What fields on {obj} hold {role} information?",
    ]
    i = 0
    for role, apis in sorted(by_role.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if 2 <= len(apis) <= 8 and i < 14:
            b.add("semantic_role_search", _pick(role_t, i).format(obj=label, role=_human(role)),
                  apis, [f"field:{a}" for a in apis])
            i += 1

    # concept_to_field: phrase with the concept words, expect the api name. Skip concepts that
    # just restate the label (too easy), and spread the selection over the whole field list.
    cands = []
    for f in fields:
        concept = (f.get("semantic") or {}).get("concept")
        if not concept:
            continue
        words = _human(concept)
        if _norm_text(words) == _norm_text(f.get("label") or "") or words.startswith("legacy"):
            continue
        cands.append((f, words))
    step = max(1, len(cands) // 20)
    tmpl = [
        "Where do we record the {w} on {obj}?",
        "Which field captures the {w}?",
        "I need the {w}. Which field is it?",
        "Is there a field for {w} on {obj}?",
    ]
    for j, (f, words) in enumerate(cands[::step][:20]):
        b.add("concept_to_field", _pick(tmpl, j).format(w=words, obj=label), [f["api_name"]],
              [f"field:{f['api_name']}"], forbidden=_confusables(k, f["api_name"]))


def _gold_provenance(b: _Builder, raw: dict, label: str) -> None:
    prov = raw.get("provenance") or {}
    if not prov:
        return
    sources = [s for s in prov.get("sources") or [] if isinstance(s, dict)]
    if sources:
        b.add("provenance_quality", f"Where does the knowledge about {label} come from?",
              [s.get("source") for s in sources if s.get("source")], ["provenance"])
        kinds = _uniq(s.get("knowledge_type") for s in sources if s.get("knowledge_type"))
        if kinds:
            b.add("provenance_quality", "Which kinds of knowledge (technical vs curated) went into this file?",
                  kinds, ["provenance"])
        confident = [s for s in sources if isinstance(s.get("confidence"), (int, float))]
        if confident:
            top = max(confident, key=lambda s: s["confidence"])
            b.add("provenance_quality", "Which knowledge source has the highest stated confidence, and how much?",
                  [top.get("source"), str(top["confidence"])], ["provenance"])
    if prov.get("review_status"):
        b.add("provenance_quality", f"Has the {label} knowledge been reviewed? What is its review status?",
              [_human(prov["review_status"])], ["provenance"])
    if prov.get("source_environment"):
        b.add("provenance_quality", "Which Salesforce environment was this knowledge extracted from?",
              [prov["source_environment"]], ["provenance"])
    if prov.get("notes"):
        b.add("provenance_quality", "What does the provenance say should be verified from the runtime schema instead?",
              _uniq(w for w in ("queryable", "required", "record types") if w in prov["notes"].lower())
              or [prov["notes"].split()[0]], ["provenance"])


def _gold_record_types(b: _Builder, raw: dict, label: str) -> None:
    rts = raw.get("record_types") or []
    names = [str(r.get("name") or r.get("developer_name") or r.get("label")) if isinstance(r, dict) else str(r)
             for r in rts]
    names = [n for n in names if n and n != "None"]
    if names:
        b.add("record_types", f"What record types does {label} have?", names, ["record_types"])
        b.add("record_types", f"How many record types are defined for {label}, and what are they called?",
              names, ["record_types"])
    else:
        # The file lists none: the right answer is that the knowledge does not document any.
        b.add("record_types", f"What record types does {label} have?", [], ["record_types", "provenance"],
              answerable=False, tools=["get_record_types", "get_provenance"])
        b.add("record_types", f"Which record type should I pick when creating a new {label}?", [],
              ["record_types", "provenance"], answerable=False, tools=["get_record_types"])


def _gold_cross_section(b: _Builder, k: Any, raw: dict, fields: list[dict], label: str, name_of) -> None:
    autos = _automations(raw)
    kind_of = {a["name"]: kd for kd, a in autos}

    # Validation field x automation writes.
    for r in raw.get("validation_rules") or []:
        for fld in r.get("fields_involved") or []:
            writers = [a["name"] for _, a in autos if fld in (a.get("fields_written") or [])]
            if writers:
                b.add("cross_section",
                      f"The {r.get('name')} rule checks {fld}. Which automations write that field and could trip it?",
                      writers + [fld], [f"validation:{r.get('name')}"] + [f"{kind_of[w]}:{w}" for w in writers])

    # Lookups stamped by automation, and their targets.
    for kind, a in autos:
        written_refs = []
        for fld in a.get("fields_written") or []:
            f = _field_by_api(k, fld)
            tg = ((f or {}).get("reference") or {}).get("target_objects") or []
            if tg:
                written_refs.append((fld, tg))
        if len(written_refs) >= 2:
            facts = [x for fld, tg in written_refs for x in [fld, *tg]]
            b.add("cross_section", f"Which lookups does {a['name']} fill in, and what objects do they point to?",
                  facts, [f"{kind}:{a['name']}"] + [f"relationship:{fld}" for fld, _ in written_refs])

    # Fields written per business process (union across that process's automations).
    proc_names = {p.get("process_id"): p.get("process_name") for p in raw.get("business_process_usage") or []}
    by_proc: dict[str, list[str]] = defaultdict(list)
    for _, a in autos:
        if a.get("business_process"):
            by_proc[a["business_process"]].extend(f for f in a.get("fields_written") or [] if "." not in f)
    for pid, flds in by_proc.items():
        flds = _uniq(flds)
        if len(flds) >= 2:
            b.add("cross_section",
                  f"During {proc_names.get(pid) or _human(pid)}, which {label} fields get written by automation?",
                  flds, [f"process:{pid}"] + [f"{kind_of[a['name']]}:{a['name']}" for _, a in autos
                                               if a.get("business_process") == pid],
                  tools=["get_automation", "get_business_processes"])

    # Mirrored fields: automation that copies another object's field onto this one.
    for kind, a in autos:
        reads = [r for r in a.get("fields_read") or [] if "." in r]
        if reads:
            src = reads[0].split(".")[0]
            mirrored = [w for w in a.get("fields_written") or [] if any(r.split(".")[-1] == w for r in reads)]
            if mirrored:
                b.add("cross_section",
                      f"Which {label} fields are copied from {src}, and which automation keeps them in sync?",
                      mirrored + [a["name"]], [f"{kind}:{a['name']}"] + [f"field:{m}" for m in mirrored])


def _gold_documented_semantics(b: _Builder, k: Any, raw: dict, fields: list[dict], label: str, name_of) -> None:
    """Business semantics stated in field meanings that structural questions never assert:
    discriminator fields, the attendee model, mode-dependent fields, and the places where the
    file contradicts itself (asked so that the gap shows up in the report)."""
    for f in fields:
        api, meaning = f["api_name"], f.get("business_meaning") or ""
        # Discriminators: "NOT NULL indicates a training mock while NULL indicates a real ..."
        m = re.search(r"NOT NULL indicates (?:an? )?([^.,]+?) while NULL indicates (?:an? )?([^.,]+)", meaning)
        if m:
            b.add("concept_to_field", f"How can I tell whether an {label} is a {m.group(1)} or a {m.group(2)}?",
                  [api, "NOT NULL"], [f"field:{api}"], tools=["search_fields", "search_knowledge", "get_field"])
        # Attendee model: "The actual interviewer is represented by an Interview_Attendee__c row with role X"
        m = re.search(r"actual ([\w\s-]+?) is represented by an? (\w+__c) row with role\s*=?\s*(\w+)", meaning, re.I)
        if m:
            b.add("cross_section", f"Where is the actual {m.group(1)} of an {label} stored? Is {name_of(f)} enough?",
                  [m.group(2), m.group(3)], [f"field:{api}"] + ([f"child:{m.group(2)}"] if f"child:{m.group(2)}" in b.known else []),
                  tools=["get_field", "get_child_relationships", "search_knowledge"])
        # Mode-dependent fields: "Interview template used when Launch Mode is Template."
        m = re.search(r"used when ([A-Z][\w ]*?) is (\w+)", meaning)
        if m:
            ctrl = k.field(m.group(1))
            if ctrl and ctrl.get("api_name") != api:
                b.add("cross_section", f"When is {name_of(f)} used on an {label}?",
                      [ctrl["api_name"], m.group(2)], [f"field:{api}", f"field:{ctrl['api_name']}"],
                      tools=["get_field", "get_picklist_values"])

    # Contradictions inside the file (see ObjectKnowledge.consistency_warnings): mirrored
    # fields with no syncing automation are unanswerable "which automation" questions.
    try:
        warnings = k.consistency_warnings()
    except Exception:  # pragma: no cover - older knowledge.py
        warnings = []
    for w in warnings:
        m = re.search(r"consistency: (\w+) is described as mirrored(?: from (\w+))?", w)
        if m:
            src = m.group(2) or "its source"
            b.add("cross_section", f"Which automation keeps {m.group(1)} in sync with {src}?", [],
                  [f"field:{m.group(1)}"], answerable=False, tools=["get_automation", "get_field"])
            continue
        m = re.search(r"consistency: (\w+) mentions the back-reference (\w+)\.(\w+)", w)
        if m:
            b.add("cross_section", f"Can one {label} be linked to several {m.group(2)} records, and through which field?",
                  [f"{m.group(2)}.{m.group(3)}", "mutual"], [f"field:{m.group(1)}", f"relationship:{m.group(1)}"],
                  tools=["get_field", "get_relationships", "get_child_relationships"])

    # Field dependencies: an empty list means none are documented.
    if not raw.get("field_dependencies"):
        b.add("cross_section", f"Are any picklist fields on {label} controlled by another field (field dependencies)?",
              [], ["field_dependencies"], answerable=False, tools=["describe_object", "search_knowledge"])
    else:
        deps = [d for d in raw.get("field_dependencies") or [] if isinstance(d, dict)]
        ctrl = _uniq(str(d.get("controlling_field") or "") for d in deps)
        if ctrl:
            b.add("cross_section", f"Which {label} picklists are controlled by another field?", ctrl,
                  ["field_dependencies"], tools=["describe_object", "search_knowledge"])


def _gold_unanswerable(b: _Builder, k: Any, fields: list[dict], label: str, plural: str) -> None:
    """A few JSON-derived unanswerable questions so both negative categories exist in gold."""
    status = next((f for f in fields if "status" in str((f.get("semantic") or {}).get("roles", "")).lower()
                   and isinstance(_picklist_list(k, f["api_name"]), list)), None)
    if status:
        val = _picklist_list(k, status["api_name"])[0]  # type: ignore[index]
        b.add("unanswerable_record_data", f"How many {plural} currently have {status.get('label')} = '{val}'?",
              answerable=False, chunks=[f"field:{status['api_name']}"])
    b.add("unanswerable_record_data", f"Who created the most recent {label} record?", answerable=False,
          chunks=["object"])
    unknown_req = [f for f in fields if f.get("required") is None]
    if unknown_req:
        f = unknown_req[0]
        b.add("unanswerable_missing_knowledge", f"Is {f['api_name']} a required field?", answerable=False,
              chunks=[f"field:{f['api_name']}", "provenance"], tools=["get_field", "get_provenance"])
    b.add("unanswerable_missing_knowledge", f"Which profiles have edit access to {label} records?",
          answerable=False, chunks=["provenance"])


# --------------------------------------------------------------------------------------------
# Adversarial questions
# --------------------------------------------------------------------------------------------

def _fake_field_name(k: Any, existing: set[str]) -> str:
    """Plausible-looking API name guaranteed not to exist on the object."""
    for cand in ("Priority_Level__c", "Customer_Satisfaction__c", "Escalation_Reason__c",
                 "Region_Code__c", "Approval_Stage__c", "Budget_Amount__c"):
        if cand.lower() not in existing:
            return cand
    return "Nonexistent_Field_Zz__c"


def adversarial_questions(k: Any) -> list[Question]:
    """Deterministic negatives the knowledge file cannot answer (``answerable=False``).

    Covers record counts, specific record values, aggregates, owner/sharing, page layouts,
    field-level security, non-existent fields and metadata the file leaves null/unknown.
    """
    b = _Builder("adversarial", _known_ids(k), k)
    obj = k.raw.get("object") or {}
    api_obj = obj.get("api_name") or k.api_name
    label = obj.get("label") or api_obj
    plural = obj.get("plural_label") or f"{label} records"
    fields = [f for f in k.fields() if f.get("api_name")]
    existing = {f["api_name"].lower() for f in fields}
    rec = "unanswerable_record_data"
    miss = "unanswerable_missing_knowledge"

    def first(pred) -> dict | None:
        return next((f for f in fields if pred(f)), None)

    picklist = first(lambda f: isinstance(_picklist_list(k, f["api_name"]), list) and f.get("type") == "picklist")
    numeric = first(lambda f: f.get("type") in ("number", "currency", "percent", "double", "int"))
    # Prefer a current (non-legacy) date field: asking by a legacy label invites the model to
    # answer with the newer field, which is then graded as a guess.
    date_f = (first(lambda f: f.get("type") in ("date", "datetime") and not _is_legacy(f))
              or first(lambda f: f.get("type") in ("date", "datetime")))
    ref = first(lambda f: bool((f.get("reference") or {}).get("target_objects")))
    ident = first(lambda f: "identifier" in str((f.get("semantic") or {}).get("roles"))) or first(lambda f: f["api_name"] == "Name")

    # --- live record data -------------------------------------------------------------------
    b.add(rec, f"How many {plural} are there in total right now?", answerable=False, chunks=["object"])
    b.add(rec, f"How many {plural} were created last week?", answerable=False, chunks=["object"])
    if ident:
        b.add(rec, f"What is the {ident.get('label')} of the newest {label}?", answerable=False,
              chunks=[f"field:{ident['api_name']}"])
    if picklist:
        vals = _picklist_list(k, picklist["api_name"])
        b.add(rec, f"Which {picklist.get('label')} value is most common across all {plural}?",
              answerable=False, chunks=[f"picklist:{picklist['api_name']}"])
        b.add(rec, f"Give me the list of {plural} where {picklist['api_name']} is '{vals[0]}'.",  # type: ignore[index]
              answerable=False, chunks=[f"picklist:{picklist['api_name']}"])
    if numeric:
        b.add(rec, f"What is the average {numeric.get('label')} across all {plural}?", answerable=False,
              chunks=[f"field:{numeric['api_name']}"])
    if date_f:
        b.add(rec, f"How many {plural} have a {date_f.get('label')} in the next 7 days?", answerable=False,
              chunks=[f"field:{date_f['api_name']}"])
    if ref:
        tgt = ref["reference"]["target_objects"][0]
        b.add(rec, f"Which {tgt} has the most {plural} linked through {ref['api_name']}?", answerable=False,
              chunks=[f"field:{ref['api_name']}"])
    b.add(rec, f"Who last modified the {label} record with Id a0X000000000001?", answerable=False,
          chunks=["object"])

    # --- metadata the file does not contain -------------------------------------------------
    fake = _fake_field_name(k, existing)
    b.add(miss, f"What values can {fake} take on {api_obj}?", answerable=False, chunks=[],
          tools=["get_field", "search_fields", "get_picklist_values"])
    b.add(miss, f"What does the {_human(fake.replace('__c', ''))} field on {label} mean?", answerable=False,
          chunks=[], tools=["search_fields", "get_field"])
    # Not "who owns the records": a lookup with a business-owner role (and the standard
    # OwnerId) makes that partly answerable. Sharing model + queue ownership is truly absent.
    b.add(miss, f"What is the org-wide sharing model for {api_obj}, and which queues own {plural}?",
          answerable=False, chunks=["object", "provenance"])
    b.add(miss, f"Which page layouts are assigned to {label}?", answerable=False, chunks=["object"])
    target_f = numeric or (fields[0] if fields else None)
    if target_f:
        b.add(miss, f"Which profiles or permission sets can edit {target_f['api_name']}?", answerable=False,
              chunks=[f"field:{target_f['api_name']}"], tools=["get_field", "get_provenance"])
    null_req = [f for f in fields if f.get("required") is None]
    for f in null_req[1:3]:  # gold already asks about null_req[0]
        b.add(miss, f"Must {f.get('label')} be filled in before a {label} can be saved?", answerable=False,
              chunks=[f"field:{f['api_name']}", "provenance"], tools=["get_field", "get_provenance"])
    null_unique = first(lambda f: f.get("unique") is None and "unique" not in f) or first(lambda f: f.get("unique") is None)
    if null_unique:
        b.add(miss, f"Is {null_unique['api_name']} enforced as unique?", answerable=False,
              chunks=[f"field:{null_unique['api_name']}"], tools=["get_field"])
    no_default = first(lambda f: "default_value" not in f and "default" not in f and not f.get("calculated")
                       and f.get("type") not in ("auto_number", "formula", "rollup_summary"))
    if no_default:
        b.add(miss, f"What is the default value of {no_default['api_name']} on new records?", answerable=False,
              chunks=[f"field:{no_default['api_name']}"], tools=["get_field"])
    # Values the file holds as explicit nulls, with provenance saying to verify them from the
    # runtime schema: the right answer says the file does not state them.
    rel_null = next((r for r in k.relationships() if r.get("field") and "relationship_name" in r
                     and r.get("relationship_name") is None), None)
    if rel_null:
        b.add(miss, f"What is the child relationship name of the {rel_null['field']} lookup?", answerable=False,
              chunks=[f"relationship:{rel_null['field']}", "provenance"],
              tools=["get_relationships", "find_relationship", "get_provenance"])
    null_caps = (first(lambda f: f.get("type") == "picklist" and "filterable" in f and f.get("filterable") is None)
                 or first(lambda f: "filterable" in f and f.get("filterable") is None))
    if null_caps:
        b.add(miss, f"Is {null_caps['api_name']} filterable and groupable in SOQL?", answerable=False,
              chunks=[f"field:{null_caps['api_name']}", "provenance"], tools=["get_field", "get_provenance"])
    if "queryable" not in obj and "queryable" in str((k.raw.get("provenance") or {}).get("notes", "")).lower():
        b.add(miss, f"Is {api_obj} queryable through the API?", answerable=False,
              chunks=["object", "provenance"], tools=["describe_object", "get_provenance"])
    b.add(miss, f"Which reports and dashboards use {api_obj}?", answerable=False, chunks=["object"])
    b.add(miss, f"What list views exist for {plural}?", answerable=False, chunks=["object"])
    b.add(miss, f"What is the help text shown for fields on the {label} edit page?", answerable=False,
          chunks=["object"])
    b.add(miss, f"How much data storage do {plural} use?", answerable=False, chunks=["object"])
    return b.items


# --------------------------------------------------------------------------------------------
# LLM-generated questions
# --------------------------------------------------------------------------------------------

def _chunk_text(c: dict, limit: int = 900) -> str:
    """Compact rendering of one chunk for the prompt."""
    body = c.get("text") or json.dumps(c.get("data") or {}, ensure_ascii=False)
    if len(body) > limit:
        body = body[:limit] + " ..."
    return f"[{c.get('id')}] {c.get('title') or ''}\n{body}"


def _excerpts_for(k: Any, index: Any, category: str, max_chunks: int = 8) -> list[dict]:
    """Pick knowledge chunks for a category: id-prefix matches first, then BM25 hits."""
    query, prefixes = _CATEGORY_EXCERPTS[category]
    chunks = list(k.chunks())
    picked: "OrderedDict[str, dict]" = OrderedDict()
    pref = [c for c in chunks if any(str(c.get("id", "")).startswith(p) for p in prefixes)]
    # Spread over the matching chunks rather than always taking the first few.
    step = max(1, len(pref) // max(1, max_chunks // 2))
    for c in pref[::step][: max_chunks // 2 or 1]:
        picked[c["id"]] = c
    try:
        for c in index.search(query, k=max_chunks):
            if len(picked) >= max_chunks:
                break
            picked.setdefault(c["id"], c)
    except Exception as e:  # pragma: no cover - search.py is a separate module
        log.warning("index.search failed for %s: %s", category, e)
    return list(picked.values())


def _coerce_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v] if v.strip() else []
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if isinstance(x, (str, int, float)) and str(x).strip()]
    return []


def _coerce_bool(v: Any, default: bool = True) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "no", "0", "n")
    return default


def _ground_facts(facts: list[str], haystack: str) -> list[str]:
    """Keep only facts that literally occur in the knowledge (case/punctuation-insensitive)."""
    hay = _norm_text(haystack)
    return [f for f in facts if _norm_text(f) and _norm_text(f) in hay]


def _parse_llm_questions(payload: Any, category: str, excerpts: list[dict], haystack: str,
                         start: int) -> list[Question]:
    """Validate an LLM JSON payload into Questions, dropping malformed or ungrounded items."""
    items: Any = payload.get("questions") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return []
    out: list[Question] = []
    for item in items:
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict):
            continue
        text = item.get("question")
        if not isinstance(text, str) or len(text.strip()) < 8:
            continue
        negative_cat = category.startswith("unanswerable")
        answerable = _coerce_bool(item.get("answerable"), not negative_cat)
        if negative_cat and answerable:
            continue  # an answerable question in a negative category is mislabelled: drop it
        facts = _ground_facts(_coerce_list(item.get("expected_facts")), haystack) if answerable else []
        if answerable and not facts:
            continue  # nothing verifiable to grade against
        tools = [t for t in _coerce_list(item.get("expected_tools")) if t in TOOL_NAMES] or DEFAULT_TOOLS[category]
        gold = [c["id"] for c in excerpts
                if any(_norm_text(f) in _norm_text(c.get("text", "") + " " + json.dumps(c.get("data") or {}))
                       for f in facts)]
        out.append(Question(
            qid=f"llm-{category}-{start + len(out) + 1:03d}",
            category=category,
            question=text.strip(),
            expected_facts=_uniq(facts),
            expected_tools=_uniq(tools),
            answerable=answerable,
            source="llm",
            gold_chunk_ids=gold or ([c["id"] for c in excerpts] if answerable else []),
        ))
    return out


def _template(name: str) -> str | None:
    """Current text of a prompt template, honouring runtime edits to ``prompts.TEMPLATES``."""
    from sf_assistant.agent import knowledge_prompts as prompts

    templates = getattr(prompts, "TEMPLATES", None)
    if isinstance(templates, dict) and templates.get(name):
        return templates[name]
    return getattr(prompts, name, None)


def _check_flag(payload: Any, key: str) -> bool | None:
    """Boolean ``key`` from a checker's JSON reply; None when absent or unparseable."""
    if not isinstance(payload, dict) or key not in payload:
        return None
    v = payload[key]
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1", "y")
    return bool(v)


def _premise_ok(llm: Any, q: Question, excerpts: list[dict], object_name: str) -> bool:
    """LLM check that the question's presupposition holds in the excerpts.

    Returns False only on an explicit ``premise_true: false``; a failed or malformed check
    keeps the question (and is logged).
    """
    from sf_assistant.agent import knowledge_prompts as prompts

    try:
        user = prompts.render("PREMISE_CHECK_USER", object_name=object_name, question=q.question,
                              excerpts="\n\n".join(_chunk_text(c) for c in excerpts))
        payload = llm.chat_json([{"role": "user", "content": user}],
                                schema_hint='{"premise_true": bool, "reason": str}')
    except Exception as e:  # network/parse problems must not drop questions silently
        log.warning("premise check failed for %s: %s: %s", q.qid, type(e).__name__, e)
        return True
    ok = _check_flag(payload, "premise_true")
    if ok is False:
        log.info("dropping %s (false premise): %s", q.qid, (payload or {}).get("reason", ""))
    return ok is not False


def llm_questions(k: Any, llm: Any, index: Any, per_category: int = 3,
                  categories: Sequence[str] | None = None, *, check_premise: bool = True) -> list[Question]:
    """Ask the LLM to write natural questions per category from knowledge excerpts.

    For each category, relevant chunks are pulled (by chunk-id prefix and BM25), rendered into
    ``prompts.QUESTION_GEN_USER`` and sent with ``QUESTION_GEN_SYSTEM`` through
    ``llm.chat_json``. Output is validated: malformed items are dropped, expected facts must
    literally occur in the knowledge JSON, and tool names must be real tools.

    Args:
        k: ``ObjectKnowledge``.
        llm: ``okeval.llm.LLM`` (or anything with a compatible ``chat_json``).
        index: ``KnowledgeIndex`` over ``k.chunks()``.
        per_category: number of questions requested per category.
        categories: optional subset of :data:`CATEGORIES` (default: all).

        check_premise: ask the LLM (``PREMISE_CHECK_USER``) whether each question's
            presupposition holds in the excerpts, and drop the ones it rejects.

    Returns:
        Validated questions with ``source="llm"``. Categories whose call fails are skipped.
    """
    from sf_assistant.agent import knowledge_prompts as prompts  # local import: prompts.py is a sibling module built in parallel

    obj = k.raw.get("object") or {}
    haystack = json.dumps(k.raw, ensure_ascii=False)
    out: list[Question] = []
    for category in categories or CATEGORIES:
        excerpts = _excerpts_for(k, index, category)
        ctx = {
            "n": per_category,
            "category": category,
            "category_description": CATEGORY_DESCRIPTIONS[category],
            "excerpts": "\n\n".join(_chunk_text(c) for c in excerpts),
            "object_api_name": obj.get("api_name") or k.api_name,
            "object_label": obj.get("label") or k.api_name,
            "tool_names": ", ".join(TOOL_NAMES),
        }
        try:
            user = prompts.render("QUESTION_GEN_USER", **ctx)
            messages = [{"role": "system", "content": _template("QUESTION_GEN_SYSTEM") or ""},
                        {"role": "user", "content": user}]
            payload = llm.chat_json(messages, schema_hint='{"questions":[{"question","category",'
                                    '"expected_facts":[...],"expected_tools":[...],"answerable":bool}]}')
        except (KeyError, ValueError) as e:
            log.warning("question generation failed for %s: %s", category, e)
            continue
        except Exception as e:  # network or client errors must not kill the whole run
            log.warning("question generation errored for %s: %s: %s", category, type(e).__name__, e)
            continue
        parsed = _parse_llm_questions(payload, category, excerpts, haystack, 0)
        if check_premise:
            parsed = [q for q in parsed if _premise_ok(llm, q, excerpts, ctx["object_api_name"])]
        out.extend(parsed[:per_category])
    return out


# --------------------------------------------------------------------------------------------
# Paraphrases
# --------------------------------------------------------------------------------------------

def _extract_strings(payload: Any) -> list[str]:
    """Pull paraphrase strings out of whatever JSON shape the model returned."""
    if isinstance(payload, str):
        return [payload]
    if isinstance(payload, list):
        out: list[str] = []
        for x in payload:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                v = x.get("question") or x.get("paraphrase") or x.get("text")
                if isinstance(v, str):
                    out.append(v)
        return out
    if isinstance(payload, dict):
        for key in ("paraphrases", "questions", "rewrites", "variants", "items"):
            if key in payload:
                return _extract_strings(payload[key])
        for v in payload.values():
            got = _extract_strings(v) if isinstance(v, list) else []
            if got:
                return got
    return []


_TITLE_ANY_RX = re.compile(r"\b[A-Z][A-Za-z']+(?:\s+(?:of\s+)?[A-Z][A-Za-z']+)+\b")


def _subject_fields(k: Any, text: str) -> list[dict]:
    """Fields the question is about: API name or (multi-character) label mentioned in it."""
    low = f" {_norm_text(text)} "
    out = []
    for f in k.fields():
        api, lab = str(f.get("api_name") or ""), str(f.get("label") or "")
        if api and api.lower() in text.lower():
            out.append(f)
        elif lab and len(lab) >= 4 and f" {_norm_text(lab)} " in low:
            out.append(f)
    return out


def paraphrase_problem(original: Question, text: str, k: Any) -> str | None:
    """Cheap, deterministic reasons to reject a paraphrase (None = looks fine).

    * it introduces a Title Case name that is neither in the original nor anywhere in the
      knowledge JSON (an invented label such as 'Resume Attached');
    * the original names a field (API name or label) and the paraphrase keeps neither that
      name nor most of the label's words (it drifted to another subject) - except for
      concept_to_field, whose point is to avoid the name.
    """
    known = _norm_text(json.dumps(k.raw, ensure_ascii=False))
    orig = _norm_text(original.question)
    for m in _TITLE_ANY_RX.finditer(text):
        phrase = _norm_text(m.group(0))
        words = phrase.split()
        if len(words) >= 2 and phrase not in orig and phrase not in known:
            # Ignore sentence-initial question words ("Which Fields") by retrying without them.
            if len(words) > 2 and " ".join(words[1:]) in known:
                continue
            if m.start() == 0 or text[: m.start()].rstrip()[-1:] in ".?!":
                continue
            return f"invented name '{m.group(0)}'"
    if original.category != "concept_to_field":
        subjects = _subject_fields(k, original.question)
        if subjects:
            para = f" {_norm_text(text)} "
            para_words = set(para.split())

            def kept(f: dict) -> bool:
                api, lab = str(f.get("api_name") or ""), _norm_text(f.get("label") or "")
                if api.lower() in text.lower() or (lab and f" {lab} " in para):
                    return True
                lw = [w for w in lab.split() if len(w) > 2]
                return bool(lw) and sum(1 for w in lw if w in para_words or w.rstrip("s") in para_words) * 2 >= len(lw)

            if not any(kept(f) for f in subjects):
                return "dropped the subject field"
    return None


def _llm_paraphrase_ok(llm: Any, original: Question, text: str, object_name: str) -> bool:
    """LLM equivalence check (``PARAPHRASE_CHECK_USER``); False only on an explicit 'no'."""
    from sf_assistant.agent import knowledge_prompts as prompts

    try:
        user = prompts.render("PARAPHRASE_CHECK_USER", object_name=object_name, original=original.question,
                              rewrite=text, expected_facts=json.dumps(original.expected_facts, ensure_ascii=False))
        payload = llm.chat_json([{"role": "user", "content": user}],
                                schema_hint='{"same_information": bool, "facts_answer_rewrite": bool, "reason": str}')
    except Exception as e:
        log.warning("paraphrase check failed for %s: %s: %s", original.qid, type(e).__name__, e)
        return True
    same = _check_flag(payload, "same_information")
    answers = _check_flag(payload, "facts_answer_rewrite")
    if same is False or answers is False:
        log.info("dropping paraphrase of %s: %r (%s)", original.qid, text, (payload or {}).get("reason", ""))
        return False
    return True


def paraphrase(questions: Sequence[Question], llm: Any, n: int = 2, *, knowledge: Any = None,
               validate: bool = True) -> list[Question]:
    """Rewrite each question ``n`` ways in business language via ``PARAPHRASE_USER``.

    Paraphrases keep ``category``, ``expected_facts``, ``exact_facts``, ``forbidden_facts``,
    ``expected_tools``, ``answerable`` and ``gold_chunk_ids`` of the original; ``source``
    becomes ``"paraphrase"`` and the qid is ``<original qid>~p<j>``. Failed calls are skipped;
    only new phrasings are returned.

    Because the expected facts are copied, a paraphrase that changes the question would be
    graded against the wrong answer. Each candidate is therefore validated: with
    ``knowledge`` the deterministic :func:`paraphrase_problem` checks run first, and with
    ``validate`` the LLM confirms (``PARAPHRASE_CHECK_USER``) that both questions ask for the
    same information and that the expected facts answer the rewrite. Rejected candidates are
    dropped (logged at INFO).
    """
    from sf_assistant.agent import knowledge_prompts as prompts

    object_name = getattr(knowledge, "api_name", "") or "the object"
    out: list[Question] = []
    for q in questions:
        try:
            user = prompts.render("PARAPHRASE_USER", question=q.question, n=n, k=n, category=q.category)
            messages = [{"role": "user", "content": user}]
            system = _template("PARAPHRASE_SYSTEM")
            if system:
                messages.insert(0, {"role": "system", "content": system})
            payload = llm.chat_json(messages,
                                    schema_hint='{"paraphrases": ["...", "..."]}')
        except Exception as e:
            log.warning("paraphrase failed for %s: %s: %s", q.qid, type(e).__name__, e)
            continue
        seen = {_norm_text(q.question)}
        j = 0
        for text in _extract_strings(payload):
            text = re.sub(r"\s+", " ", str(text)).strip()
            if len(text) < 8 or _norm_text(text) in seen:
                continue
            seen.add(_norm_text(text))
            if knowledge is not None:
                problem = paraphrase_problem(q, text, knowledge)
                if problem:
                    log.info("dropping paraphrase of %s: %r (%s)", q.qid, text, problem)
                    continue
            if validate and not _llm_paraphrase_ok(llm, q, text, object_name):
                continue
            j += 1
            out.append(Question(
                qid=f"{q.qid}~p{j}", category=q.category, question=text,
                expected_facts=list(q.expected_facts), expected_tools=list(q.expected_tools),
                answerable=q.answerable, source="paraphrase", gold_chunk_ids=list(q.gold_chunk_ids),
                exact_facts=list(q.exact_facts), forbidden_facts=list(q.forbidden_facts),
            ))
            if j >= n:
                break
    return out


# --------------------------------------------------------------------------------------------
# Dedupe + utilities
# --------------------------------------------------------------------------------------------

def dedupe(questions: Iterable[Question]) -> list[Question]:
    """Drop questions whose normalised text was already seen (first wins); make qids unique."""
    seen_text: set[str] = set()
    seen_qid: dict[str, int] = defaultdict(int)
    out: list[Question] = []
    for q in questions:
        key = _norm_text(q.question)
        if not key or key in seen_text:
            continue
        seen_text.add(key)
        seen_qid[q.qid] += 1
        if seen_qid[q.qid] > 1:
            q = Question(**{**asdict(q), "qid": f"{q.qid}#{seen_qid[q.qid]}"})
        out.append(q)
    return out


def category_counts(questions: Iterable[Question]) -> dict[str, int]:
    """Number of questions per category, in :data:`CATEGORIES` order (zeros included)."""
    counts = {c: 0 for c in CATEGORIES}
    for q in questions:
        counts[q.category] = counts.get(q.category, 0) + 1
    return counts


def save_questions(questions: Iterable[Question], path: str) -> str:
    """Write questions as JSONL; returns the path."""
    with open(path, "w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q.to_dict(), ensure_ascii=False) + "\n")
    return path


def load_questions(path: str) -> list[Question]:
    """Read questions written by :func:`save_questions`."""
    with open(path, encoding="utf-8") as fh:
        return [Question.from_dict(json.loads(line)) for line in fh if line.strip()]


def closest_category(name: str) -> str | None:
    """Map a loosely-spelled category (e.g. from an LLM) onto :data:`CATEGORIES`."""
    hit = difflib.get_close_matches(name.strip().lower(), CATEGORIES, n=1, cutoff=0.6)
    return hit[0] if hit else None


__all__ = [
    "CATEGORIES", "TOOL_NAMES", "DEFAULT_TOOLS", "CATEGORY_DESCRIPTIONS", "Question",
    "gold_questions", "adversarial_questions", "llm_questions", "paraphrase", "paraphrase_problem", "dedupe",
    "category_counts", "save_questions", "load_questions", "closest_category",
]
