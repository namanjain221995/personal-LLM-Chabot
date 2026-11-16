"""Answers about named people (moved verbatim from okrec/session.py; RECORDS_SPEC §22).

:func:`people_tool_nudges` asks for ``about_people`` when no result covers the named people; :func:`people_answer`
writes the record part from ``about_people`` results when the agent gives no answer; :func:`agent_people_result` and
:func:`people_answer_from` read the last about_people result of the evidence (evaluation fix 2, 2026-10-22: the
people answer when the record agent used the people tool and the record side gave no answer). The same-name line names
the records that same-name people's identity links share (``shared_records``, evaluation fix 4) instead of saying
that nothing links them, and says the data does not tell whose they are (review of 2026-10-22: a shared record is
nobody's own); a person sentence gives up to :data:`PROFILE_ITEMS` profile items (the review: 5 left the joining date
out for about a third of the employees).

Step-2 fixes of 2026-10-30 (docs/people.md): :func:`people_answer` keeps its P/R reference codes, and both code paths
that show it (``RecordSession.ask`` and :func:`people_answer_from`) drop them (``checks.drop_ref_codes``, F1); a
closest stored name spelled unlike the asked name (:data:`CLOSE_NAME_PROFILE_RATIO`) that is not the asked name
written another way (:func:`_look_alike`) gets only the not-found lead, its name and its kind (F9).
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any

from sf_assistant.agent.checks import drop_ref_codes, reference_codes
from sf_assistant.agent.nudges import _norm_text
from sf_assistant.agent.record_prompts import RECORDS_PEOPLE_TOOL_MESSAGE
from sf_assistant.agent.triage import Triage, _name_key
from sf_assistant.engine.types import PERSON_TOOLS, QueryEvidence
from sf_assistant.tools.people import PROFILE_CORE_FIELDS
from sf_assistant.tools.names import _close_word, _same_name_words   # F9: the asked name written another way
from sf_assistant.tools.record_base import name_key

#: Profile items per person sentence of the code-written people answer: the profile core (review of 2026-10-22; it
#: was 5, which left an employee's joining date out when 5 filled values came before it).
PROFILE_ITEMS = PROFILE_CORE_FIELDS
#: A closest stored name whose spelling ratio to the asked name (difflib, lower case, spaces collapsed, a " (#k)"
#: suffix aside) is below this is a look-alike, not a typo: its sentence gives only its name and kind (step-2 fixes of
#: 2026-10-30, F9; a typo such as one wrong letter in a full name keeps the profile), unless it is the asked name
#: written another way (:func:`_look_alike`).
CLOSE_NAME_PROFILE_RATIO = 0.85


def _look_alike(p: dict) -> bool:
    """``p`` is a closest stored name spelled unlike the asked name (:data:`CLOSE_NAME_PROFILE_RATIO`) that is not the
    asked name written another way (review of 2026-10-30): the rule about_people picked it by (the words in another
    order, an initial, fewer or more words, a close spelling of a word: ``tools.names._same_name_words``), or, for a
    one-word ask, one of its words or a close spelling of one (``tools.names._close_word``: the spelling help reads a
    single name only for doubled letters or an identical sound)."""
    if p.get("match") != "closest stored name":
        return False
    asked = " ".join(str(p.get("asked") or "").lower().split())
    label = " ".join(re.sub(r"\s*\(#\d+\)$", "", str(p.get("label") or "")).lower().split())
    if SequenceMatcher(None, asked, label).ratio() >= CLOSE_NAME_PROFILE_RATIO:
        return False
    a, b = name_key(asked).split(), name_key(label).split()
    if len(a) == 1:
        return not any(a[0] == w or _close_word(a[0], w) for w in b)
    return not _same_name_words(a, b)


def _quoted(words: list[str]) -> str:
    items = [f"'{w}'" for w in words]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def people_tool_nudges(reply: str, evidence: list[QueryEvidence], triage: "Triage | None") -> list[str]:
    """``RECORDS_PEOPLE_TOOL_MESSAGE`` (once, on the first draft that is no NO_RECORDS reply) when the question names
    people (``triage``) and the evidence does not cover them: (a) two or more names and no successful person-tool call
    (:data:`PERSON_TOOLS`) took all of them; (b) a name that no successful person-tool call took, no successful data call
    matched (``names_matched`` holds a stored name of that name's people) and no failed call reported as not found.
    Code reads only the evidence, never the draft's words."""
    if triage is None:
        return []
    asked = triage.asked()
    if not asked:
        return []
    keys = {w: _name_key(w) for w in asked}
    stored: dict[str, set[str]] = {}
    for p in triage.people:
        stored[str(p.get("words"))] = {_norm_text(r[2]) for r in p.get("recs") or [] if len(r) > 2}
    person_calls = [e for e in evidence if e.ok and e.tool in PERSON_TOOLS]
    took = []
    for e in person_calls:   # a call covers the names it took and the asked names whose stored people it lists
        t = {_name_key(n) for n in (e.applied or {}).get("names") or []} | {_name_key((e.arguments or {}).get("name"))}
        res = e.model_result if isinstance(e.model_result, dict) else {}
        listed = {_norm_text(re.sub(r"\s*\(#\d+\)$", "", str(x.get("label") or "")))
                  for key in ("people", "matched_records") for x in res.get(key) or [] if isinstance(x, dict)}
        t |= {keys[w] for w in asked if stored.get(w, set()) & listed}
        took.append(t)
    matched = {_norm_text(n) for e in evidence if e.ok for x in getattr(e, "names_matched", None) or []
               if isinstance(x, dict) for n in x.get("names") or []}
    not_found = {_name_key(x.get("name")) for e in evidence if not e.ok for x in getattr(e, "not_found", None) or []
                 if isinstance(x, dict)}
    uncovered: list[str] = []
    if len(asked) >= 2 and not any(all(keys[w] in t for w in asked) for t in took):
        uncovered = [w for w in asked if not any(keys[w] in t for t in took)] or list(asked)
    else:
        for w in asked:
            if any(keys[w] in t for t in took) or keys[w] in not_found or (stored.get(w, set()) & matched):
                continue
            uncovered.append(w)
    if not uncovered:
        return []
    return [RECORDS_PEOPLE_TOOL_MESSAGE.replace("<<NAMES>>", _quoted(asked)).replace("<<MISSING>>", _quoted(uncovered))
            .replace("<<NAMES_JSON>>", json.dumps(asked, ensure_ascii=False))]


#: A profile key of the values of a copy's source on the record's child rows (Group B item 9):
#: "Marketing__c.Recruiter__c (via Candidate__c)".
_SOURCE_KEY_RE = re.compile(r"^([A-Za-z0-9_]+)\.([A-Za-z0-9_]+) \(via [A-Za-z0-9_]+\)$")


def _api_in_words(api: str) -> str:
    """An API name in words ("Marketing__c" -> "Marketing", "Assigned_Trainer__c" -> "Assigned Trainer")."""
    return " ".join(re.sub(r"__c$", "", str(api)).replace("_", " ").split())


def _profile_items(profile: Any, n: int) -> list[str]:
    """'label: value' of a profile dict ("Employment_Status__c (Employment Status)" reads "Employment Status"; the
    label ends at its first "; ", so a key's direction or copy wording, "Reporting to; senior recruiter this employee
    reports to", reads "Reporting to"). The record's own items come first; the values of a copy's source on its child
    rows ("Marketing__c.Recruiter__c (via Candidate__c)") follow as "Recruiter (on Marketing)" when there is room."""
    own, source = [], []
    for k, v in (profile or {}).items():
        val = str(v).lower() if isinstance(v, bool) else v
        sm = _SOURCE_KEY_RE.match(str(k))
        if sm:
            source.append(f"{_api_in_words(sm.group(2))} (on {_api_in_words(sm.group(1))}): {val}")
            continue
        m = re.match(r"^[A-Za-z0-9_]+ \((.+)\)$", str(k))
        own.append(f"{m.group(1).split('; ')[0] if m else k}: {val}")
    return (own + source)[:n]


def _article(word: str) -> str:
    return "an" if str(word or "x")[:1].lower() in "aeiou" else "a"


def people_answer(result: dict) -> str:
    """The record answer code writes from an ``about_people`` result when the record agent gave none (RECORDS_SPEC
    §22.8). Every number comes from the result; no arithmetic. One sentence per person (who they are, up to
    :data:`PROFILE_ITEMS` profile items, the records that reference them most), a line per asked name shared by several
    people, a line for records that people of one asked name share without sharing a name (``shared_records``), the
    links of each pair (at most 3) or that no stored record links them within what was checked, and the names that are
    not stored."""
    res = result if isinstance(result, dict) else {}
    people = [p for p in res.get("people") or [] if isinstance(p, dict)]
    refs = [e for e in res.get("references") or [] if isinstance(e, dict)]
    recs = [m for m in res.get("matched_records") or [] if isinstance(m, dict)]
    more = res.get("more_people") if isinstance(res.get("more_people"), dict) else {}
    by_ref = {p.get("ref"): p for p in people}

    def name(p: dict) -> str:
        return f"{p.get('label')} ({p.get('ref')})"

    def desc(p: dict) -> str:
        return f"{name(p)}, {p.get('kind')}" + (f", {p.get('detail')}" if p.get("detail") else "")

    def sentence(p: dict) -> str:
        kind = str(p.get("kind") or "person")
        lead = ""
        if p.get("match") == "closest stored name":   # never answer for a close name without saying so
            lead = f"No record is named '{p.get('asked')}'; the closest stored name is '{p.get('label')}'. "
        elif p.get("match") == "record name":
            own = next((m for m in recs if m.get("ref") == (p.get("records") or [None])[0]), {})
            what = str(own.get("object_label") or own.get("object") or "other")
            lead = f"'{p.get('asked')}' is not a person; {_article(what)} {what} record has this name. "
        if _look_alike(p):   # an unasked look-alike: its name and kind only (F9)
            return lead + f"{name(p)} is {_article(kind)} {kind}."
        s = lead + f"{name(p)} is {_article(kind)} {kind}" + (f" ({p.get('detail')})" if p.get("detail") else "")
        items = _profile_items(p.get("profile"), PROFILE_ITEMS)
        s += (": " + "; ".join(items) + ".") if items else "."
        main_rec = (p.get("records") or [None])[0]
        linked = [m for m in recs if m.get("person") == p.get("ref") and m.get("ref") != main_rec]
        if linked:
            parts = []
            for m in linked[:3]:
                extra = _profile_items(m.get("profile"), 3)
                inner = "; ".join(x for x in [str(m.get("detail") or "")] + extra if x)
                parts.append(f"{m.get('object_label') or m.get('object')} '{m.get('label')}'" + (f" ({inner})" if inner else ""))
            s += " Linked records: " + ", ".join(parts) + "."
        mine = [e for e in refs if e.get("person") == p.get("ref")][:3]
        if mine:
            s += " Referenced as " + "; ".join(f"{e.get('role')} on {e.get('records')} {e.get('object')} records "
                                               f"(`{e.get('object')}.{e.get('field')}`)" for e in mine)
            more_refs = len([e for e in refs if e.get("person") == p.get("ref")]) > 3 or p.get("references_shown")
            s += (", among others." if more_refs else ".")
        elif p.get("no_business_references"):
            s += " No business record references them."
        return s

    lines: list[str] = []
    for asked in res.get("asked") or []:
        ppl = [p for p in people if p.get("asked") == asked]
        extra = more.get(asked) if isinstance(more.get(asked), dict) else {}
        n_more = int(extra.get("count") or 0)
        if not ppl:
            continue
        if len(ppl) + n_more > 1:
            lines.append(f"'{asked}' matches {len(ppl) + n_more} stored people: " + "; ".join(desc(p) for p in ppl) + ".")
            lines += [sentence(p) for p in ppl[:3]]
            rest = len(ppl[3:]) + n_more
            if rest:
                kinds = dict(extra.get("by_kind") or {})
                for p in ppl[3:]:
                    kinds[p.get("kind")] = kinds.get(p.get("kind"), 0) + 1
                lines.append(f"And {rest} more (" + ", ".join(f"{v} {k}" for k, v in kinds.items()) + ").")
        else:
            lines.append(sentence(ppl[0]))
    if res.get("same_name"):
        lines.append(_same_name_line(res, by_ref, recs))
    lines += _shared_lines(res, by_ref, recs)
    links = [x for x in res.get("links") or [] if isinstance(x, dict)]
    checked = str(res.get("links_checked") or "").strip()
    if "links" in res:
        pairs: dict[tuple, list[str]] = {}
        for x in links:
            pairs.setdefault(tuple(x.get("between") or []), []).append(str(x.get("text") or ""))
        for (a, b), texts in pairs.items():
            pa, pb = by_ref.get(a, {}), by_ref.get(b, {})
            lines.append(f"{pa.get('label', a)} and {pb.get('label', b)}: " + "; ".join(texts[:3]) + ".")
        without = [x for x in res.get("pairs_without_links") or [] if isinstance(x, list) and len(x) == 2]
        scope = f" (checked: {checked})" if checked else ""
        stopped = res.get("partial") or [x for x in res.get("not_checked") or [] if "more_people" not in str(x)]
        if without and not links:
            lines.append(f"No link was found in what was checked; the check stopped early or hit a limit{scope}."
                         if stopped else f"No stored record links any of them{scope}.")
        elif without:
            listed = ", ".join(f"{by_ref.get(a, {}).get('label', a)} ({a}) and {by_ref.get(b, {}).get('label', b)} ({b})"
                               for a, b in without[:6])
            lines.append(f"No link was found between {listed} in what was checked; the check stopped early or hit a "
                         f"limit{scope}." if stopped else f"No stored record links {listed}{scope}.")
    for nf in res.get("not_found") or []:
        if not isinstance(nf, dict):
            continue
        text = f"No employee, user, candidate or lead record is named '{nf.get('asked')}'."
        close = [str(c) for c in nf.get("close") or [] if c]
        if close:
            text += " Closest stored names: " + ", ".join(close[:5]) + "."
        lines.append(text)
    if res.get("references_partial"):
        lines.append("The reference scan stopped early (time budget): some reference counts are missing.")
    if refs:
        lines.append("Reference counts cover all records of each object (no period, filter or status).")
    return "\n".join(lines).strip()


def _same_name_line(res: dict, by_ref: dict, recs: list[dict]) -> str:
    """The line on people listed with the same name: not linked by their records; or, when the identity links of
    their records reach the same records (``shared_records``, evaluation fix 4, 2026-10-22: two same-name employees
    with one person account and its portal user), linked only through those, which are nobody's own (the data does
    not say which of them uses them). Labels and P refs only (no record refs)."""
    plain = ("People listed with the same name are not linked by their records: the data does not record whether "
             "they are one human.")
    same = {str(x) for refs in (res.get("same_name") or {}).values() if isinstance(refs, list) for x in refs}
    shared = res.get("shared_records") if isinstance(res.get("shared_records"), dict) else {}
    rec_by_ref = {m.get("ref"): m for m in recs}
    groups: dict[tuple[str, ...], list[str]] = {}
    for rref, prefs in shared.items():
        prefs = [str(x) for x in prefs or []] if isinstance(prefs, list) else []
        if len([x for x in prefs if x in same]) < 2:
            continue
        m = rec_by_ref.get(rref) or {}
        groups.setdefault(tuple(prefs), []).append(f"{m.get('object_label') or m.get('object') or 'record'} "
                                                   f"'{m.get('label') or rref}'")
    if not groups:
        return plain

    def joined(items: list[str]) -> str:
        return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]

    def person(ref: str) -> str:
        p = by_ref.get(ref)
        return f"{p.get('label')} ({ref})" if isinstance(p, dict) else ref

    parts = [f"{joined([person(x) for x in prefs])} link to {joined(what)} (the data does not say which of them uses "
             f"{'them' if len(what) > 1 else 'it'})" for prefs, what in groups.items()]
    return ("People listed with the same name are not linked by their records, except through the records they share: "
            + "; ".join(parts) + "; the data does not record whether they are one human.")


def _shared_lines(res: dict, by_ref: dict, recs: list[dict]) -> list[str]:
    """A line per group of people of one asked name whose records' identity links reach the same records
    (``shared_records``) when the same-name line does not cover them (they have different names) and no link names
    them (people of different asked names get a "shared record" link): "<people> link to <records> (the data does not
    say which of them uses them)." Labels and P refs only."""
    shared = res.get("shared_records") if isinstance(res.get("shared_records"), dict) else {}
    same = {str(x) for refs in (res.get("same_name") or {}).values() if isinstance(refs, list) for x in refs}
    rec_by_ref = {m.get("ref"): m for m in recs}
    groups: dict[tuple[str, ...], list[str]] = {}
    for rref, prefs in shared.items():
        prefs = [str(x) for x in prefs or []] if isinstance(prefs, list) else []
        if len(prefs) < 2 or len([x for x in prefs if x in same]) >= 2:
            continue   # the same-name line names them
        if len({(by_ref.get(x) or {}).get("asked") for x in prefs}) > 1:
            continue   # people of different asked names: the shared-record link names them
        m = rec_by_ref.get(rref) or {}
        groups.setdefault(tuple(prefs), []).append(f"{m.get('object_label') or m.get('object') or 'record'} "
                                                   f"'{m.get('label') or rref}'")
    out = []
    for prefs, what in groups.items():
        who = [f"{(by_ref.get(x) or {}).get('label', x)} ({x})" for x in prefs]
        out.append(f"{', '.join(who[:-1])} and {who[-1]} link to "
                   + (what[0] if len(what) == 1 else ", ".join(what[:-1]) + " and " + what[-1])
                   + f" (the data does not say which of them uses {'them' if len(what) > 1 else 'it'}).")
    return out


def agent_people_result(evidence: list[QueryEvidence] | None) -> dict | None:
    """The result of the last successful about_people call of ``evidence`` (a dict), else None (evaluation fix 2: the
    people answer of an empty record answer reads it)."""
    for e in reversed(evidence or []):
        if e.ok and e.kind == "data" and e.tool == "about_people" and isinstance(e.model_result, dict):
            return e.model_result
    return None


def named_spellings(people: list[dict] | None) -> dict[str, list[str]]:
    """The stored spellings of the triage's confirmed people whose words are not stored as typed (``asked`` set: a
    misspelt, reordered or shortened name; not a ``partial`` one), by their words: their ``close`` names and, with
    the spelling help, ``read_as`` (``RecordAnswer.named_spellings``; review of 2026-10-23: the agent's about_people
    call may take the stored spelling the hints gave it). People of a stored name: none."""
    out: dict[str, list[str]] = {}
    for p in people or []:
        if not isinstance(p, dict) or not p.get("words") or not p.get("asked") or p.get("partial"):
            continue
        names = [str(x) for x in list(p.get("close") or []) + [p.get("read_as")] if str(x or "").strip()]
        if names:
            got = out.setdefault(str(p["words"]), [])
            got += [x for x in dict.fromkeys(names) if x not in got]
    return out


def people_result_covers(result: dict | None, names: list[str] | None,
                         spellings: dict[str, list[str]] | None = None) -> bool:
    """The about_people ``result`` covers every name of ``names`` (the question's named people, ``rec.named_people``):
    each name's words, or the words of one of its stored spellings (``spellings``, :func:`named_spellings`: a
    misspelt name read as a stored one), are all in one of the result's asked names or listed people's labels (review
    of 2026-10-22: a result for one of two named people is no answer about both). No names: True."""
    if not isinstance(result, dict):
        return False
    have = [set(_name_key(x).split()) for x in list(result.get("asked") or [])
            + [re.sub(r"\s*\(#\d+\)$", "", str(p.get("label") or "")) for p in result.get("people") or []
               if isinstance(p, dict)] if _name_key(x)]
    alt = {_name_key(k): list(v or []) for k, v in (spellings or {}).items()}
    return all(any(set(_name_key(w).split()) <= h for w in [n] + alt.get(_name_key(n), []) if _name_key(w)
                   for h in have) for n in names or [] if _name_key(n))


def people_answer_from(evidence: list[QueryEvidence] | None, names: list[str] | None = None,
                       spellings: dict[str, list[str]] | None = None) -> str:
    """:func:`people_answer` of :func:`agent_people_result` ("" without an about_people result, or when it does not
    cover ``names``, :func:`people_result_covers`, with their stored ``spellings``): the code-written people answer
    when the record agent used the people tool and the record side gave no answer (evaluation fix 2,
    ``pipeline.answer``), without the P/R reference codes the evidence maps (``checks.drop_ref_codes``; step-2 fixes
    of 2026-10-30, F1)."""
    res = agent_people_result(evidence)
    return (drop_ref_codes(people_answer(res), reference_codes(evidence))
            if res is not None and people_result_covers(res, names, spellings) else "")
