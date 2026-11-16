"""Names in a question (moved verbatim from okrec/session.py; RECORDS_SPEC §22).

:func:`parse_names` reads the names call's reply; :class:`Triage` holds what the route call and the names call
decided. The route and names calls themselves are :meth:`RecordSession.triage`.

Value spots (2026-10-28: every stored record value a question names): :class:`Triage` also holds the stored record
values that code spotted in the question (``values``, the listed spots of ``RecordTools.spot_values``) and why the
triage overruled the route call (``overruled_by``, ``overrule_reason``); the helpers below say a spot the same way in
the log, the trace and the record agent's pushback, and never repeat an e-mail address or phone number typed in the
question (:func:`value_log`, :func:`value_text`, :func:`value_reason`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

from sf_assistant.agent.checks import strip_thinking
from sf_assistant.agent.record_prompts import NO_RECORDS, RECORDS
from sf_assistant.engine.values import normalize
from sf_assistant.tools import record_base as _base   # MAX_PEOPLE (names per question): read (and patched) there

# ------------------------------------------------------------------------------------- people (RECORDS_SPEC §22)

# Names per question that the triage keeps: ``tools.record_base.MAX_PEOPLE`` (also the about_people limit), read at
# call time; the notebook's okrec/session.py had its own copy, _MAX_PEOPLE (review 2026-10-20: one source).
_POSSESSIVE_RE = re.compile(r"(\w)['’]s\b")
#: Separators between names glued together ("a,b", "[a]"): read as spaces (``okrec.tools._NAME_SEP_RE``).
_NAME_SEP_RE = re.compile(r"[,;:!?()\[\]{}<>\"&+|]")


def _name_tokens(text: Any) -> list[str]:
    """Words of a name or question as the record tools' name index writes them (``okrec.tools._q_tokens``):
    possessives removed, curly apostrophes read as straight ones, separators read as spaces,
    :func:`okrec.values.normalize`."""
    t = _POSSESSIVE_RE.sub(r"\1", str(text if text is not None else "")).replace("’", "'")
    return [w for w in normalize(_NAME_SEP_RE.sub(" ", t)).split() if w]


def _name_key(text: Any) -> str:
    return " ".join(_name_tokens(text))


def parse_names(reply: str | None, question: str) -> list[str]:
    """The names of the names call's reply (:data:`NAMES_SYSTEM`): split on ``|`` (and lines), quotes, backticks and
    punctuation stripped; NONE (or RECORDS / NO_RECORDS) gives []. Only names of at most 6 words whose words all occur
    in the question are kept (a made-up name, or any other reply, is never a name of the question); at most 4."""
    text = strip_thinking(reply).strip()
    if not text or text.strip(" .`'\"*").upper() in ("NONE", RECORDS, NO_RECORDS):
        return []
    qtoks = set(_name_tokens(question))
    out: list[str] = []
    for item in re.split(r"\||\n", text):
        item = item.strip().strip("'\"`*.,;:!?()[]{}<>-").strip()
        toks = _name_tokens(item)
        if not toks or len(toks) > 6 or item.upper() in ("NONE", RECORDS, NO_RECORDS):
            continue
        if not all(w in qtoks for w in toks):
            continue
        if all(_name_key(x) != _name_key(item) for x in out):
            out.append(item)
    return out[:_base.MAX_PEOPLE]


# ----------------------------------------------------------------------------- stored record values (2026-10-28)

#: Value spots that :meth:`Triage.as_dict` lists for the JSONL sidecar (the hints get every listed spot).
VALUES_LOG_MAX = 8
#: Value spots that the record agent's NO_RECORDS pushback names (``RECORDS_VALUE_NO_RECORDS_MESSAGE``).
VALUES_MESSAGE_MAX = 6
#: Kinds of value spots that are design vocabulary: a picklist value or a record type name names no record, so such a
#: spot never overrules the route call and never forbids a NO_RECORDS reply.
_DESIGN_KINDS = frozenset({"picklist", "record_type"})
#: Kinds of value spots whose words name a record: a names-call name inside such a span is no missing person.
_RECORD_KINDS = frozenset({"name", "code", "text"})
#: The keys of a value spot that the log keeps: never the stored value, its spellings or a record Id.
_VALUE_LOG_KEYS = ("span", "object", "field", "kind", "strength", "why", "records")
#: ``Triage.overrule_reason`` of the people overrules (log and trace only; the people path decides as before).
PERSON_OVERRULE_REASON = "a named person"
MISSING_OVERRULE_REASON = "a name no stored person has"


def _is_email(d: dict) -> bool:
    return "@" in str(d.get("span") or "")


def value_place(d: dict) -> str:
    """``<object>.<field>`` of a value spot."""
    return f"{d.get('object')}.{d.get('field')}"


def value_text(d: dict) -> str:
    """A value spot as the record agent's pushback names it: "'<span>' (<object>.<field>)"; for an e-mail address or
    phone number typed in the question (kind ``contact``) "an e-mail address in the question (<object>.<field>)", so
    the typed value is never repeated (the nudges reach the JSONL)."""
    if d.get("kind") == "contact":
        return f"{'an e-mail address' if _is_email(d) else 'a phone number'} in the question ({value_place(d)})"
    return f"'{d.get('span')}' ({value_place(d)})"


def value_reason(d: dict) -> str:
    """Why a value spot overruled the route call (``Triage.overrule_reason``, the trace's route line): "the stored
    value '<span>' (<object>.<field>, <why>)", with "read as '<spelling>'" after the span when the spelling help read
    it; a contact spot as :func:`value_text` says it."""
    if d.get("kind") == "contact":
        return value_text(d)
    read = f" read as '{d.get('read_as')}'" if d.get("read_as") else ""
    return f"the stored value '{d.get('span')}'{read} ({value_place(d)}, {d.get('why')})"


def value_log(d: dict) -> dict:
    """A value spot as the JSONL triage keeps it: ``{"span", "object", "field", "kind", "strength", "why",
    "records"}`` plus ``read_as`` when the spelling help read it; the span of a contact spot is "<e-mail>" or
    "<phone>" (the typed value is never logged)."""
    out = {k: d.get(k) for k in _VALUE_LOG_KEYS}
    if d.get("kind") == "contact":
        out["span"] = "<e-mail>" if _is_email(d) else "<phone>"
    elif d.get("read_as"):
        out["read_as"] = d.get("read_as")
    return out


def strong_value(d: dict) -> bool:
    """A listed value spot strong enough to overrule a NO_RECORDS route and to push back on a NO_RECORDS reply:
    strength "strong", and no picklist value or record type name (design vocabulary never overrules)."""
    return bool(d.get("listed")) and d.get("strength") == "strong" and d.get("kind") not in _DESIGN_KINDS


def value_named(name: Any, values: list[dict], design_word: Callable[[str], bool]) -> bool:
    """Every word of the names-call name ``name`` that is no design word (``design_word``) lies inside the span of one
    listed value spot of kind name, code or text: the name is a stored record ("Team Zentrova" for a stored
    department), not a missing person. The words of separate spots never pool (review F1: a made-up full name whose
    first and last word are two different stored values stays a missing person). False for a name of design words
    only (no such name is missing anyway)."""
    words = {w for w in _name_tokens(name) if not design_word(w)}
    return bool(words) and any(words <= set(_name_tokens(d.get("span"))) for d in values
                               if d.get("kind") in _RECORD_KINDS)


@dataclass
class Triage:
    """What code and one small model call found about the people a question names (RECORDS_SPEC §22.3).

    ``routed``: True when a named person overrules the route (:meth:`RecordSession.triage`), else the route call's
    answer; it is what :meth:`RecordSession.route` returns and what ``ask.py`` uses. ``route``: the route call's own
    answer. ``people``: confirmed :meth:`okrec.tools.RecordTools.spot_people` entries (strong spots, spots and names
    the names call confirms; ``partial``: only a word of the asked name is a stored first or last name, ``asked`` is
    the question's name, which is not stored). ``hinted``: the people for the hints (``people`` plus strong spots of a
    design question that the names call did not name). ``missing``: names of the names call that no stored person
    has, nor a word of them (with a word that is no design word). ``names``: the names call's names (None: the call
    failed or did not run).

    With the spelling help on (``SFA_RESOLVER=on``, docs/resolver_integration.md step (5)) a confirmed person whose
    misspelt name the term resolver read as a stored one carries ``asked`` (the question's words), ``close`` (the
    stored names; for one word, the stored first or last name) and ``read_as`` (the stored spelling): the structured
    data of the correction for the answer's name note. A name the resolver only has did-you-means for is never
    confirmed: it is in ``missing``.

    Value spots (2026-10-28): ``values`` are the listed spots of ``RecordTools.spot_values`` (stored record values
    the question names: object, field, kind, strength, why; no record Id), spotted after the people were decided.
    ``overruled_by`` says what sent the question to the record agent against the route call: "value" (the route call
    said NO_RECORDS and a strong listed spot names a stored record), "person" or "missing name" (the people overrules,
    set when the route call did not say RECORDS; log only), else None; ``overrule_reason`` says it in words
    (:func:`value_reason`, :data:`PERSON_OVERRULE_REASON`, :data:`MISSING_OVERRULE_REASON`)."""

    routed: bool | None
    route: bool | None
    people: list[dict] = field(default_factory=list)
    hinted: list[dict] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    names: list[str] | None = None
    seconds: float = 0.0
    values: list[dict] = field(default_factory=list)
    overruled_by: str | None = None
    overrule_reason: str = ""

    def asked(self) -> list[str]:
        """The question's words for its people (confirmed, then missing), at most 4."""
        return list(dict.fromkeys([str(p.get("words")) for p in self.people if p.get("words")]
                                  + list(self.missing)))[:_base.MAX_PEOPLE]

    def people_data(self) -> bool:
        """A person-tool result is data about the question's people: a confirmed person is stored under the asked name
        (or a close spelling of it), or every name of the question is stored. False when the question names only names
        that are not stored (``missing``, ``partial`` people): results about other people who share a word of the name
        count nothing (RECORDS_SPEC §22.8)."""
        return any(not p.get("partial") for p in self.people) or not (self.missing or self.people)

    def strong_values(self) -> list[dict]:
        """The listed value spots that name a stored record strongly (:func:`strong_value`), in question order."""
        return [d for d in self.values if strong_value(d)]

    def as_dict(self) -> dict:
        """JSON-safe summary for the JSONL sidecar (words only, no records). With the spelling help on, ``spelling``
        lists the confirmed people whose name it read as a stored one (``read_as``): ``{"typed", "decision":
        "corrected", "suggestion", "kind": "person"}``; the key is left out when there is none (always with the
        switch off). ``first_name_fit`` lists ``{"words", "word", "lookup", "records"}`` for the people confirmed only
        because the data kept a first name in play that the route call and the names call missed (Group B item 13);
        left out when there is none. Value spots (2026-10-28): ``values`` lists at most :data:`VALUES_LOG_MAX` spots
        as :func:`value_log` writes them, and ``overruled_by`` / ``overrule_reason`` say why the route call was
        overruled; each key is left out when there is none, so a question without them logs as before."""
        out = {"routed": self.routed, "route": self.route, "names": self.names,
               "people": [p.get("words") for p in self.people], "missing": list(self.missing),
               "people_data": self.people_data(), "seconds": round(self.seconds, 3)}
        spelling = [{"typed": str(p.get("asked") or p.get("words") or ""), "decision": "corrected",
                     "suggestion": str(p["read_as"]), "kind": "person"} for p in self.people if p.get("read_as")]
        if spelling:
            out["spelling"] = spelling
        fits = [{"words": p.get("words"), **p["first_name_fit"]} for p in self.people
                if isinstance(p.get("first_name_fit"), dict)]
        if fits:   # Group B item 13: a first name both calls missed that the data kept in play
            out["first_name_fit"] = fits
        if self.values:   # value spots: never the stored value, its spellings or an Id; no typed contact value
            out["values"] = [value_log(d) for d in self.values[:VALUES_LOG_MAX]]
        if self.overruled_by:
            out["overruled_by"] = self.overruled_by
            out["overrule_reason"] = self.overrule_reason
        return out
