"""The record answer's scope claims, kept true to the queries that ran (business-term contracts of 2026-11-24).

The record agent writes what was counted in its own words (prompt rule 7), and only its numbers are audited. Two code
steps keep the candidate scope true, both read from the evidence (``QueryEvidence.applied``):

- :func:`drop_unapplied_scope_claims` cuts a claim of a Person Account or record-type scope ("(Person Accounts only)",
  "filtered to person accounts only", "where IsPersonAccount = true", "RecordTypeId = 'X'", "record type 'X' only")
  that no applied filter of a successful data call backs. Only the claim's clause goes (a parenthesis, a clause joined
  by "where", "with", "filtered / limited to" or set off by a comma at the end of its clause, a lead "...:"), never its
  sentence, the subject of a sentence or its number; a line that was nothing but the claim goes. When the cut would
  leave no text that states a number, the answer stays as it was (an emptied answer would read as no answer) and the
  claim is only reported. Review fixes of 2026-11-24: it runs only for a question about candidates, or when a call
  applied a record-type filter (code's or the model's); a claim after "not" is no claim; a record type value must be
  quoted or read as one (Person Account, a developer name, an Id); a Person Account claim is backed by an Account
  filter, or by a lookup filter when every successful call applied one; values compare by letters and digits.
- :func:`with_term_line` adds one code line, "Candidates counted: Person Accounts only (...)" (``prompts/
  term_contracts.md`` ``person_accounts_line``), before the Source line when code added the candidate filter to a
  successful call (``applied["term_filters"]``) and the answer does not state that scope already (a backed claim, or
  "(Person Accounts ...)").

``agent/record_session.py`` calls both after the number audit (:func:`~sf_assistant.agent.checks.drop_unsupported`) and
before the Source line. No duckdb; never raises.
"""

from __future__ import annotations

import re
from typing import Any

from sf_assistant.agent.checks import _DATE_TIME_RE, _body_text, _is_source_line, numbers_in
from sf_assistant.engine.term_contracts import detect as _detect
from sf_assistant.engine.term_contracts import text as _term_text

__all__ = ["drop_unapplied_scope_claims", "with_term_line", "without_term_line", "applied_scope", "term_filters"]

#: A Salesforce record type Id (15 or 18 characters).
_RT_ID = r"012[A-Za-z0-9]{12}(?:[A-Za-z0-9]{3})?"
#: A claimed record type value: quoted, Person Account, a developer name (an underscore in it) or an Id; never a bare
#: word ("RecordTypeId is not filtered", "RecordTypeId = grouped").
_VALUE = (r"""(?:'[^'\n]{1,60}'|"[^"\n]{1,60}"|`[^`\n]{1,60}`|Person\s*Accounts?\b|""" + _RT_ID
          + r"""\b|[A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]*)""")
#: A claim of a Person Account or record-type scope; the named groups say which kind.
_CLAIM = (r"(?:(?P<person>person[\s-]?accounts?\s+only|only\s+(?:the\s+)?person[\s-]?accounts?)"
          r"|(?P<flag>`?(?:[A-Za-z][A-Za-z0-9_]*__c\.)?IsPersonAccount`?\s*(?:=|==|is)\s*`?true`?)"
          r"|(?P<rtid>`?(?:[A-Za-z][A-Za-z0-9_]*__c\.)?RecordTypeId`?\s*(?:=|==|is)\s*(?P<rtv>" + _VALUE + r"))"
          r"|(?P<rtonly>record\s+type\s+(?P<rtw>'[^'\n]{1,60}'|\"[^\"\n]{1,60}\"|`[^`\n]{1,60}`)\s+only))")
_CLAIM_RE = re.compile(_CLAIM, re.IGNORECASE)
#: A negation right before a claim: "(not Person Accounts only: every record type)" states no scope.
_NEG_RE = re.compile(r"(?:\bnot|n['’]t|\bnever|\bwithout|\bno)\W*$", re.IGNORECASE)
#: A parenthesis that holds a claim.
_PAREN_RE = re.compile(r"\s*\(([^()\n]*)\)")
#: A claim joined to its sentence by a connector ("where", "with", "filtered / limited / restricted / scoped to").
_JOINED_RE = re.compile(r"(?:\s*[,;:–—]\s*|\s+)(?:and\s+)?(?:(?:filtered|limited|restricted|scoped)\s+(?:to|by|on)|"
                        r"where|with)\s+(?:the\s+)?(?P<claim>" + _CLAIM + r")", re.IGNORECASE)
#: A claim set off by a comma or a dash at the end of its clause ("There are 5 candidates, Person Accounts only.").
_TAIL_RE = re.compile(r"(?:\s*[,;:–—]\s*|\s+-\s+)(?:the\s+)?(?P<claim>" + _CLAIM + r")(?=\s*(?:[.,;!?)]|$))",
                      re.IGNORECASE)
#: A claim that leads its line, set off by a colon, comma or dash ("**Person Accounts only**: 5").
_LEAD_RE = re.compile(r"^(\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*)?)(?P<claim>" + _CLAIM + r")(?:\*\*)?\s*[:,;–—-]\s*",
                      re.IGNORECASE)
#: A line that is nothing but a claim.
_ONLY_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)?(?:\*\*)?(?:the\s+)?(?P<claim>" + _CLAIM
                      + r")(?:\*\*)?\s*[.:;,]?\s*$", re.IGNORECASE)
#: The answer states the Person Account scope in a parenthesis ("2 candidates (Person Accounts) had interviews").
_PERSON_PAREN_RE = re.compile(r"\(\s*(?:only\s+)?person[\s-]?accounts?\b[^()\n]*\)", re.IGNORECASE)
#: An applied filter text that keeps Person Accounts (the record type, or the flag).
_PERSON_TEXT_RE = re.compile(r"RecordType(?:Id)?(?:\.\w+)?\s*(?:=|\bin\b).*'Person\s*Account'|IsPersonAccount\s*=\s*true",
                             re.IGNORECASE)
#: An applied filter on the counted object's own record type or flag (not through a lookup).
_OWN_TYPE_TEXT_RE = re.compile(r"^\s*(?:RecordType(?:Id)?|IsPersonAccount)\b", re.IGNORECASE)
_TYPE_TEXT_RE = re.compile(r"\b(?:RecordType(?:Id)?|IsPersonAccount)\b", re.IGNORECASE)


def _norm(value: Any) -> str:
    """Letters and digits only, lower case: a label and its developer name compare equal ("Vendor / Implementation
    Partner", 'Vendor_Implementation_Partner')."""
    return re.sub(r"[^0-9a-z]+", "", str(value or "").lower())


def _ok_data(evidence: Any) -> list[Any]:
    return [e for e in evidence or [] if getattr(e, "ok", False) and getattr(e, "kind", "") == "data"]


def _call_texts(e: Any) -> list[str]:
    """The applied filter texts of one call (filters, base, any_of, part and segment filters)."""
    out: list[str] = []
    applied = e.applied if isinstance(getattr(e, "applied", None), dict) else {}
    for key in ("filters", "base_filters", "any_of", "part_filters"):
        out += [str(x) for x in applied.get(key) or []]
    res = e.model_result if isinstance(getattr(e, "model_result", None), dict) else {}
    for seg in res.get("segments") or [] if isinstance(res.get("segments"), list) else []:
        if isinstance(seg, dict) and isinstance(seg.get("applied"), dict):
            out += [str(x) for x in seg["applied"].get("filters") or []]
    return out


def applied_scope(evidence: Any) -> list[str]:
    """Every applied filter text of the successful data calls (filters, base, any_of, part and segment filters)."""
    return [t for e in _ok_data(evidence) for t in _call_texts(e)]


def term_filters(evidence: Any) -> list[str]:
    """The filter texts code added for a business term on the successful data calls (``applied["term_filters"]``)."""
    out: list[str] = []
    for e in _ok_data(evidence):
        applied = e.applied if isinstance(getattr(e, "applied", None), dict) else {}
        out += [str(x) for x in applied.get("term_filters") or []]
    return list(dict.fromkeys(out))


class _Scope:
    """What the successful data calls applied: every filter text, and whether a Person Account claim is backed (an
    Account-level Person Account filter on some call, or a lookup-level one when every call applied one)."""

    def __init__(self, evidence: Any) -> None:
        calls = [_call_texts(e) for e in _ok_data(evidence)]
        self.applied = [t for c in calls for t in c]
        own = any(_PERSON_TEXT_RE.search(t) and _OWN_TYPE_TEXT_RE.match(t) for t in self.applied)
        every = bool(calls) and all(any(_PERSON_TEXT_RE.search(t) for t in c) for c in calls)
        self.person = own or every


def _claims(s: str) -> list[re.Match]:
    """The claims in ``s`` (a claim right after a negation is none)."""
    return [m for m in _CLAIM_RE.finditer(s) if not _NEG_RE.search(s[max(0, m.start() - 24):m.start()])]


def _is_person_claim(m: re.Match) -> bool:
    return bool(m.group("person") or m.group("flag")) or \
        _norm(m.group("rtv") or m.group("rtw")) in ("personaccount", "personaccounts")


def _backed(m: re.Match, scope: _Scope) -> bool:
    """The claim of match ``m`` is a filter some successful call applied."""
    if _is_person_claim(m):
        return scope.person
    raw = str(m.group("rtv") or m.group("rtw") or "").strip("'\"`")
    if re.fullmatch(_RT_ID, raw):   # an Id: code cannot name its type here, so any record-type filter backs it
        return any("recordtypeid" in a.lower() for a in scope.applied)
    value = _norm(raw)
    return any("recordtypeid" in a.lower() and value and value in _norm(a) for a in scope.applied)


def _cut_line(line: str, scope: _Scope, removed: list[str]) -> str | None:
    """``line`` without its unbacked claims (None: the line was nothing but a claim)."""
    def unbacked(s: str) -> bool:
        return any(not _backed(m, scope) for m in _claims(s))

    if not unbacked(line):
        return line
    only = _ONLY_RE.match(line)
    if only and not _NEG_RE.search(line[:only.start("claim")]) and not _backed(only, scope):
        removed.append(" ".join(only.group("claim").split()))
        return None

    def paren(m: re.Match) -> str:
        inner = m.group(1)
        bad = [c for c in _claims(inner) if not _backed(c, scope)]
        if not bad:
            return m.group(0)
        removed.extend(" ".join(c.group(0).split()) for c in bad)
        rest = inner
        for c in reversed(bad):
            rest = rest[:c.start()] + rest[c.end():]
        rest = re.sub(r"^\W*(?:and\s+)?|\s*(?:,|;|\band\b)\s*$", "", rest.strip()).strip(" ,;")
        rest = re.sub(r"\s*(?:,|;)\s*(?:,|;)\s*", ", ", rest)
        return f" ({rest})" if re.search(r"[A-Za-z0-9]", rest) else ""

    out = _PAREN_RE.sub(paren, line)

    def joined(m: re.Match) -> str:
        at = m.start("claim")
        if _NEG_RE.search(m.string[max(0, at - 24):at]) or _backed(m, scope):
            return m.group(0)
        removed.append(" ".join(m.group("claim").split()))
        return ""

    lead = _LEAD_RE.match(out)
    if lead and not _backed(lead, scope):
        removed.append(" ".join(lead.group("claim").split()))
        out = lead.group(1) + out[lead.end():]
    out = _JOINED_RE.sub(joined, out)
    out = _TAIL_RE.sub(joined, out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\s+([.,;:!?])", r"\1", out)
    out = re.sub(r"([,;:])\s*([.!?])", r"\2", out)
    if not re.search(r"[A-Za-z0-9]", re.sub(r"[*_`#>\-]+", "", out)):
        return None
    return out.rstrip()


def _checked(evidence: Any, question: str) -> bool:
    """The claims are checked for a question about candidates, or when code added a term filter or a call applied a
    record-type filter (review fixes of 2026-11-24: never on other answers, whose words may only describe)."""
    if term_filters(evidence) or any(_TYPE_TEXT_RE.search(t) for t in applied_scope(evidence)):
        return True
    try:
        return "candidates" in _detect(question).keys
    except Exception:  # noqa: BLE001 - never raises
        return False


def drop_unapplied_scope_claims(answer: str | None, evidence: Any, question: str = "") -> tuple[str, list[str]]:
    """``answer`` without the Person Account / record-type scope claims no applied filter backs, and the claims cut
    (see the module text). Source lines are never touched; unchanged when nothing would be left that states a
    number, and for an answer the check does not cover (``question`` names no candidates and no call applied a
    record-type filter)."""
    body = answer or ""
    if not _claims(body) or not _checked(evidence, question):
        return body, []
    scope = _Scope(evidence)
    removed: list[str] = []
    out: list[str] = []
    for ln in body.split("\n"):
        if not ln.strip() or _is_source_line(ln):
            out.append(ln)
            continue
        got = _cut_line(ln, scope, removed)
        if got is None and numbers_in(ln):
            got = ln
        if got is not None:
            out.append(got)
    if not removed:
        return body, []
    result = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    had = numbers_in(_DATE_TIME_RE.sub(" ", _body_text(body)))
    left = _body_text(result).strip()
    if not left or (had and not numbers_in(_DATE_TIME_RE.sub(" ", left))):
        return body, list(dict.fromkeys(removed))
    return result, list(dict.fromkeys(removed))


def _says_scope(body: str, scope: _Scope) -> bool:
    """The answer states the Person Account scope already, and truly: a backed Person Account claim, or
    "(Person Accounts ...)" with the scope backed. Words that only mention person accounts are no such statement
    (review fixes of 2026-11-24: "Person accounts also include employees' own accounts")."""
    if not scope.person:
        return False
    return bool(_PERSON_PAREN_RE.search(body)) or any(_is_person_claim(m) for m in _claims(body))


def with_term_line(answer: str | None, evidence: Any) -> str:
    """``answer`` with the code line on the candidate filter code added (``person_accounts_line``) before its Source
    line (at the end when it has none), when a successful data call holds ``term_filters`` and the answer does not
    state that scope already (:func:`_says_scope`); unchanged otherwise (and empty text stays empty)."""
    text = answer or ""
    added = term_filters(evidence)
    if not text.strip() or not added or _says_scope(_body_text(text), _Scope(evidence)):
        return text
    line = _term_text("person_accounts_line", FILTERS="; ".join(f"`{t}`" for t in added))
    lines = text.rstrip().split("\n")
    at = next((i for i, ln in enumerate(lines) if _is_source_line(ln)), len(lines))
    head, tail = lines[:at], lines[at:]
    while head and not head[-1].strip():
        head.pop()
    return "\n".join(head + ["", line] + tail)


#: The code line of :func:`with_term_line` as it stands in an answer (its fixed words, any filters).
_TERM_LINE_RE = re.compile("^" + re.escape(_term_text("person_accounts_line", FILTERS="\x00")).replace("\x00", ".*")
                           + "$")


def without_term_line(text: str | None) -> str:
    """``text`` without the code line of :func:`with_term_line` (the merge's checks read the record answer's own
    words: the line names the candidate lookup only to state the filter code added)."""
    return "\n".join(ln for ln in (text or "").split("\n") if not _TERM_LINE_RE.match(ln.strip()))
