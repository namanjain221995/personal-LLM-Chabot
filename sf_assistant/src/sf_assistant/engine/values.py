"""Value grounding: the model's value -> the stored spelling(s) to bind (pure Python over the catalog).

Order (RECORDS_SPEC §11.4): exact stored value, then stored values that normalise the same
(``'full-time'`` binds ``'Full Time'``), then a knowledge picklist value or label (bound with the
stored spelling when one exists, else the knowledge value with a zero-records note). Anything else is
error E4, which lists the stored values with counts, close values and the same value stored in
other fields.

With the spelling help on (``SFA_RESOLVER=on``, ``catalog.resolver``; docs/resolver_integration.md (4), decision B)
one more tier comes before E4: inside a record tool call, a value the term resolver reads as ``corrected`` (one clear
close spelling of this field's values, ``'Completd'``) is bound to its stored spelling, with the note
``'Completd' read as 'Completed' (closest stored value of Status__c)`` and an entry in the call's correction channel
(:data:`sf_assistant.engine.catalog.READ_AS`). Never bound: outside a tool call, an ``ambiguous`` reading, a
truncated value index, a person's name, a value with other numbers (``'01:55 PM'`` is no typo of ``'01:15 PM'``). E4
then lists the resolver's did-you-means instead of the ``Close:`` list (for a value typed with numbers only those with
the same numbers; none left: the ``Close:`` list).
With the switch off nothing fuzzy is ever bound automatically.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from .catalog import (AUTO_NUMBER_RE, Catalog, FieldSpec, RecordError, ValueEntry, in_tool_call, record_read_as,
                      with_did_you_mean)

__all__ = ["normalize", "Grounded", "ground_value", "parse_bool", "TRUE_WORDS", "FALSE_WORDS",
           "is_record_number", "escape_like", "looks_like_id", "id18", "value_error"]

TRUE_WORDS = frozenset({"true", "yes", "y", "checked", "ticked", "active", "enabled", "on", "1"})
FALSE_WORDS = frozenset({"false", "no", "n", "unchecked", "inactive", "disabled", "off", "0"})
_ID_RE = re.compile(r"^[A-Za-z0-9]{15}([A-Za-z0-9]{3})?$")


def normalize(s: Any) -> str:
    """NFKD, casefold, ``- _ /`` and dashes to spaces, drop ``.,'"()``, collapse whitespace."""
    t = unicodedata.normalize("NFKD", str(s if s is not None else "")).casefold()
    t = re.sub(r"[-_/–—]", " ", t)
    t = re.sub(r"[.,'\"()]", "", t)
    return " ".join(t.split())


def parse_bool(v: Any) -> bool | None:
    """True/False for checkbox words (true, yes, checked, active, 1 ...), None when not a boolean word."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    s = str(v if v is not None else "").strip().lower()
    if s in TRUE_WORDS:
        return True
    if s in FALSE_WORDS:
        return False
    return None


def is_record_number(s: Any) -> bool:
    """True for auto-number style names such as ``REC-00024`` or ``II0042``."""
    return bool(AUTO_NUMBER_RE.match(str(s or "").strip()))


def escape_like(s: str) -> str:
    """Escape ``\\``, ``%`` and ``_`` for ``ILIKE ... ESCAPE '\\'``."""
    return str(s).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def looks_like_id(s: Any) -> bool:
    """A 15- or 18-character Salesforce Id."""
    return bool(_ID_RE.match(str(s or "").strip()))


_ID_SUFFIX = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"


def id18(s: Any) -> str:
    """The 18-character form of a 15-character Salesforce Id (the warehouse stores 18): the case-checksum suffix,
    one character per block of 5 whose bit k is set when character k is an upper-case letter. Any other value is
    returned stripped and unchanged."""
    v = str(s if s is not None else "").strip()
    if len(v) != 15 or not _ID_RE.match(v):
        return v
    return v + "".join(_ID_SUFFIX[sum(1 << j for j, ch in enumerate(v[i:i + 5]) if "A" <= ch <= "Z")]
                       for i in (0, 5, 10))


@dataclass
class Grounded:
    """Stored spellings to bind for one value."""

    values: list[str]
    notes: list[str] = field(default_factory=list)
    zero: bool = False
    read_as: tuple[str, str] | None = None   # (typed, bound) when the spelling help read the value as its closest one

    @property
    def shown(self) -> str:
        return self.values[0] if len(self.values) == 1 else " / ".join(self.values)


def _fmt_count(n: int) -> str:
    return str(n)


# ------------------------------------------------------------------ spelling help (docs/resolver_integration.md (4))
def _spelling(catalog: Catalog, obj: str, f: FieldSpec, raw: str) -> Any:
    """The resolver's reading of ``raw`` among the values of ``obj.f`` (stored and knowledge picklist values), or
    None when the spelling help is off."""
    return catalog.spelling(raw, ("value",), obj, f"{obj}.{f.api}=")


def _values_of(res: Any, obj: str, f: FieldSpec) -> list[str]:
    """The values the reading names (``Obj.Field=value`` targets), best first."""
    prefix = f"{obj}.{f.api}="
    return list(dict.fromkeys(t[len(prefix):] for t in res.targets if t.startswith(prefix)))


def _numbers(s: Any) -> list[int]:
    """The numbers of a value as integers (``'09:30 AM'`` -> ``[9, 30]``, as ``'9:30 AM'``)."""
    return [int(x) for x in re.findall(r"\d+", unicodedata.normalize("NFKD", str(s if s is not None else "")))]


def _number_key(s: Any) -> str:
    """:func:`normalize` with every number written as its integer: ``'09:30 AM'`` and ``'9:30 AM'`` -> ``'9:30 am'``."""
    return re.sub(r"\d+", lambda m: str(int(m.group())), normalize(s))


def _is_person_name(catalog: Catalog, text: str) -> bool:
    """A stored person name (exactly, as the resolver reads names): never bound as a close value (a filter never binds
    a person it was not given)."""
    res = catalog.spelling(text, ("person",))
    return res is not None and res.decision == "exact"


def _close_value(catalog: Catalog, obj: str, f: FieldSpec, s: str, entry: ValueEntry) -> tuple[list[str], bool] | None:
    """``(stored spellings to bind, zero)`` for one ``corrected`` reading of ``s`` inside a tool call (zero: a
    knowledge picklist value no stored record has), else None. Never with a truncated value index (``s`` may be stored
    beyond it), nor for a person's name."""
    if catalog.resolver is None or entry.truncated or not in_tool_call():
        return None
    res = _spelling(catalog, obj, f, s)
    if res is None or res.decision != "corrected":
        return None
    vals = _values_of(res, obj, f)
    norms = {normalize(v) for v in vals}
    if len(norms) != 1 or _is_person_name(catalog, s) or any(_is_person_name(catalog, v) for v in vals):
        return None
    if any(_numbers(v) != _numbers(s) for v in vals):   # another number is another value (a time, a visa class)
        return None
    stored = [v for v, _ in entry.values if normalize(v) in norms]
    if stored:
        return stored, False
    kv = next((v for v, _lab, _m in f.picklist if normalize(v) in norms), None)
    return ([kv], True) if kv is not None else None


def _value_suggestions(catalog: Catalog, obj: str, f: FieldSpec, raw: str, cands: list[str]) -> list[str]:
    """The resolver's did-you-means for ``raw`` (``corrected`` outside a tool call, ``ambiguous``, ``exact``), as the
    stored spellings (else the knowledge values) of ``cands``; at most 3. [] for ``none`` and with the switch off.
    A value typed with numbers keeps them: suggestions with other numbers are left out (``'9:30 AM'`` is never
    pointed to ``'01:30 AM'``), and the values with the same numbers written another way come first (``'09:30 AM'``);
    with none left the caller keeps today's ``Close:`` list."""
    res = _spelling(catalog, obj, f, str(raw).strip())
    if res is None or res.decision not in ("corrected", "ambiguous", "exact"):
        return []
    by_norm: dict[str, str] = {}
    for c in cands:
        by_norm.setdefault(normalize(c), c)
    out = [by_norm[normalize(v)] for v in _values_of(res, obj, f) if normalize(v) in by_norm]
    typed = _numbers(raw)
    if typed:
        key = _number_key(raw)
        out = [c for c in cands if _number_key(c) == key] + [c for c in out if _numbers(c) == typed]
    return list(dict.fromkeys(out))[:3]


def value_error(catalog: Catalog, obj: str, f: FieldSpec, raw: str, shown_field: str | None = None) -> RecordError:
    """Error E4 for ``raw`` on ``obj.f``."""
    entry = catalog.values_of(obj, f.api)
    stored = entry.values if entry else []
    name = shown_field or f"{obj}.{f.api}"
    listed = ", ".join(f"{v} {_fmt_count(n)}" for v, n in stored[:12])
    if len(stored) > 12:
        listed += f", ... and {len(stored) - 12} more"
    if entry and entry.blank:
        listed += (", " if listed else "") + f"(blank) {entry.blank}"
    text = f"'{raw}' is not a value of {name}. Stored values (records): {listed or 'none'}."
    stored_norm = {normalize(v) for v, _ in stored}
    unused = [v for v, _lab, _m in f.picklist if normalize(v) not in stored_norm]
    if unused:
        text += f" Knowledge values with no records: {', '.join(unused[:12])}."
    ns = normalize(raw)
    counts = {v: n for v, n in stored}
    cands = [v for v, _ in stored] + [v for v, _l, _m in f.picklist if v not in counts]
    by_norm = {normalize(c): c for c in cands}
    close: list[str] = []
    for c in cands:
        nc = normalize(c)
        if ns and (re.search(r"(^| )" + re.escape(ns) + r"( |$)", nc) or (len(nc) >= 3 and nc in ns)):
            close.append(c)
    for m in difflib.get_close_matches(ns, list(by_norm), n=3, cutoff=0.6):
        close.append(by_norm[m])
    close = list(dict.fromkeys(close))[:3]
    dym = _value_suggestions(catalog, obj, f, raw, cands) if catalog.resolver is not None else []
    if dym:   # the spelling help: its did-you-means replace the Close: list
        text += " Did you mean: " + ", ".join(f"'{c}' ({counts.get(c, 0)})" for c in dym) + "?"
    elif close:
        text += " Close: " + ", ".join(f"'{c}' ({counts.get(c, 0)})" for c in close) + "."
    for fld, val, n in catalog.other_fields_containing(obj, ns, f.api, 3):
        text += f" '{val}' is stored in {obj}.{fld} ({n} records)."
    text += " Use one of them exactly, or op contains."
    if dym:
        return with_did_you_mean(RecordError(text, "E4"), str(raw), "value", dym)
    return RecordError(text, "E4")


def ground_value(catalog: Catalog, obj: str, f: FieldSpec, raw: Any, shown_field: str | None = None) -> Grounded | None:
    """Ground ``raw`` against the stored values of ``obj.f``.

    Returns None when the field has no value index (the caller compares ``lower(c) = lower(?)``).
    Raises :class:`RecordError` E4 when nothing matches exactly or after normalising (with the spelling help on:
    nor as one ``corrected`` close value inside a tool call, which is bound with a note and ``read_as``).
    """
    entry = catalog.values_of(obj, f.api)
    if entry is None:
        return None
    s = str(raw).strip()
    name = shown_field or f.api
    stored = entry.values
    notes: list[str] = []
    bound: list[str]
    zero = False
    read_as: tuple[str, str] | None = None
    if any(v == s for v, _ in stored):
        bound = [s]
    else:
        ns = normalize(s)
        same = [v for v, _ in stored if normalize(v) == ns]
        if same:
            bound = same
        else:
            kv = next(((v, lab) for v, lab, _m in f.picklist if normalize(v) == ns or normalize(lab) == ns), None)
            if kv is not None:
                nv = normalize(kv[0])
                st = [v for v, _ in stored if normalize(v) == nv]
                if st:
                    bound = st
                else:
                    bound, zero = [kv[0]], True
                    notes.append(f"'{kv[0]}' is a valid picklist value of {name}; no stored records have it")
            elif (close := _close_value(catalog, obj, f, s, entry)) is not None:   # spelling help on, in a tool call
                bound, zero = close
                shown = bound[0] if len(bound) == 1 else " / ".join(bound)
                how = "closest picklist value" if zero else "closest stored value"
                note = f"'{s}' read as '{shown}' ({how} of {name})"
                notes.append(note)
                if zero:
                    notes.append(f"'{bound[0]}' is a valid picklist value of {name}; no stored records have it")
                read_as = (s, shown)
                record_read_as(s, shown, "value", f"{obj}.{f.api}={bound[0]}", how, note)
            elif entry.truncated:
                return None
            else:
                raise value_error(catalog, obj, f, s, shown_field)
    if f.picklist:
        known = {normalize(v) for v, _l, _m in f.picklist}
        spelled = {normalize(v): v for v, _l, _m in f.picklist}
        for b in bound:
            nb = normalize(b)
            if nb not in known:
                notes.append(f"'{b}' is a legacy value of {name} (not in the current picklist)")
            elif spelled[nb] != b:
                notes.append(f"knowledge spells it '{spelled[nb]}'; the stored spelling '{b}' is used")
    current_here = bool(f.picklist) and normalize(bound[0]) in {normalize(v) for v, _l, _m in f.picklist}
    for fld, val, n in catalog.other_fields_with(obj, normalize(bound[0]), f.api):
        other = catalog.objects[obj].fields.get(fld)
        legacy = ""
        current_there = False
        if other is not None and other.picklist:
            if normalize(val) in {normalize(v) for v, _l, _m in other.picklist}:
                current_there = True
            else:
                legacy = ", legacy value"
        if current_here and current_there:
            continue  # two live picklists sharing a word (Pending, Active...): different meanings, not a mix-up
        notes.append(f"'{val}' is also stored in {fld} ({n} records{legacy})")
    return Grounded(bound, notes, zero, read_as)
