"""No invented personal details (toggle-off isolation G8, 2026-11-25).

Owner order: the assistant must never invent personal details of real people.
Two production answers (route chat, Salesforce toggle off) carried a profile
with a phone number that appeared in no message, no saved fact and no source:
the model made it up.

* `PERSONAL_DETAILS_RULE` is the prompt rule every assistant-mode answering
  prompt carries (engines/chat.py ASSISTANT_SYSTEM; the agent's step,
  fallback and synthesis prompts), behind TOGGLE_OFF_PROMPT_RULE.
* `Guard` is the code mask behind it, on the chat route only: a phone number or
  an e-mail address in the answer (or its reasoning) that appears nowhere in
  the prompt the model was given is replaced. Streaming-safe: `feed` holds
  back only the tail that could still grow into a candidate, never more than
  the longest candidate (so a degenerate loop still reaches the loop guard
  behind it), and the streamed text equals `mask(full_text)` for every split.
* `third_party_contact` / `mask_third_party_contacts` keep other people's
  contact details out of saved facts (owner decision 2026-11-25): the
  extractor drops a fact that holds one, and a fact already saved has it
  masked before it enters any prompt. The person's own details stay.

Why only the chat route: vision, dataset, document, URL, search and research
answers quote phones and e-mails from files and pages that are not in the chat
prompt, and masking there would break real features.

Review fixes (2026-11-25; review_mask.md, review_regression.md R3/R9,
review_data.md F1/F4): bounded hold-back (F02/F03); Unicode digits (F01);
North American numbers, grouped and bracketed landlines, NBSP and dash
separators, `00` and `(+CC)` prefixes (F04/F16); no false hits on package
versions, git remotes, SSH and URL user parts, matmul, image density suffixes,
decimals, salary ranges, identifiers, byte counts and sums (F05/F06); whole
values in the allowed text, not substrings (F07/F13); wider role-mailbox and
test-domain lists (F12, data F4); a facts rule that asks who HOLDS the
detail (F10/F11, R9).
"""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable, List, Optional, Sequence, Set, Tuple

PERSONAL_DETAILS_RULE = (
    "Never state or guess a real person's phone number, e-mail address, home address, salary, "
    "date of birth or ID number unless it appears in this conversation, in the user's saved "
    "facts, in a file they shared or in a source given to you. Never write a profile of a real "
    "colleague, employee or candidate from memory; a clearly labelled fictional sample is fine. "
    "If asked, say you don't have it and, for the user's own company people, that the Salesforce "
    "toggle gives access. Turns marked \"not shown while the Salesforce toggle is off\" are "
    "hidden: never guess what they said, and never chart figures from them. With no data, never "
    "draw a chart or a labelled example of the user's own company figures: ask for the figures "
    "instead."
)

PHONE_MASK = "[phone not available]"
EMAIL_MASK = "[email not available]"

# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------

#: Group separators a phone number is written with: space, NBSP, thin and
#: narrow spaces, hyphen, Unicode hyphens, en dash (F04).
_SEP_CHARS = "    -‐‑–"
_S = "[" + re.escape(_SEP_CHARS) + "]"
_SD = "[" + re.escape(_SEP_CHARS) + ".]"
#: An Indian mobile: 10 digits from 6-9, optionally +91 / 0091 / 91 / (+91) /
#: 0 first, as 5-5, one run, 3-3-4 or 4-3-3.
_MOBILE = (
    rf"(?:(?:\(\+?91\)|(?:\+|00)?91){_S}?|0)?"
    rf"(?:[6-9]\d{{4}}{_S}?\d{{5}}|[6-9]\d{{2}}{_S}\d{{3}}{_S}\d{{4}}|[6-9]\d{{3}}{_S}\d{{3}}{_S}\d{{3}})"
)
#: An international number: +CC, 00CC or (+CC), then digit groups (the digit
#: count and the country code are checked in code).
_INTL = rf"(?:(?:\+|00)\d{{1,3}}|\(\+\d{{1,3}}\))(?:{_SD}?\(?\d{{1,5}}\)?){{2,6}}"
#: A North American number with its separators: (415) 555-0132, 415.555.0132,
#: 1-800-555-0199 (F04, F16). Separators are required: a bare run is not one.
_NANP = rf"(?:(?:\+|00)?1{_SD}?)?(?:\([2-9]\d{{2}}\){_SD}?|[2-9]\d{{2}}{_SD})\d{{3}}{_SD}\d{{4}}"
#: A landline with its STD code: 0 + 2-4 digits (or the code in brackets),
#: a separator, then 3-4 and 4 digits: 080-23456789, 080 2345 6789,
#: (080) 23456789. The separator after the code is required: a bare
#: zero-padded run in code is an identifier, not a phone (replay 2026-11-25).
_LANDLINE = rf"(?:\(0\d{{2,4}}\){_S}?|0\d{{2,4}}{_S})\d{{3,4}}{_S}?\d{{4}}"
#: Not inside a word, not after a sign or a decimal point, not a code joined
#: by a hyphen (SKU-9845012345), not the fraction of a decimal (F05).
_PHONE = rf"(?<![\w+.])(?<!\w-)(?<!\d,)(?:{_MOBILE}|{_INTL}|{_NANP}|{_LANDLINE})(?![\w])"
#: An e-mail: a bounded local part, 1-4 bounded labels and a LETTER top-level
#: label, not followed by ':' (scp, git and DSN syntax) or a further label (F06).
_EMAIL = (
    r"(?<![\w.%+-])[\w.%+-]{1,64}@"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.){1,4}[A-Za-z]{2,24}"
    r"(?![\w-])(?!\.[A-Za-z0-9])(?!:)"
)
CANDIDATE_RE = re.compile(rf"(?P<phone>{_PHONE})|(?P<email>{_EMAIL})")

#: The streaming bounds (F02/F03). A phone candidate is at most ~52
#: characters and an e-mail at most ~350, so a run longer than its cap is
#: already decided; a phone's decision reads `_AFTER` characters after it and
#: `_BEFORE` before it.
_PHONE_CAP = 64
_EMAIL_CAP = 400
_LOCAL_CAP = 64
_AFTER = 24
_BEFORE = 40

_EMAIL_CLASS_RE = re.compile(r"[\w.%+@-]")

_EXAMPLE_DOMAIN_RE = re.compile(
    r"(?:^|\.)(?:example\.(?:com|org|net)|[\w-]+\.example|[\w-]+\.test|[\w-]+\.invalid|localhost)$"
    r"|^(?:yourcompany|yourdomain|yourorg)\.[\w.-]+$"
    r"|^(?:company|domain|email|test|abc|xyz|sample|demo|dummy|fake)\.(?:com|in|co\.in|org|net|io)$",
    re.IGNORECASE,
)
#: Mailboxes of a role, not a person (C15, F12, data F4): compared on the
#: local part with any `+tag` dropped, and on its first `.`/`-`/`_` piece.
_ROLE_MAILBOXES = frozenset(
    {
        "hr", "careers", "career", "jobs", "job", "info", "support", "contact", "contactus", "admin",
        "noreply", "no-reply", "donotreply", "do-not-reply", "sales", "hello", "team", "recruitment",
        "recruiting", "recruiters", "talent", "hiring", "payroll", "accounts", "billing", "finance",
        "office", "enquiry", "enquiries", "inquiry", "inquiries", "feedback", "press", "media",
        "privacy", "security", "help", "helpdesk", "service", "services", "marketing", "legal",
        "compliance", "grievance", "webmaster", "postmaster", "abuse", "test", "user", "demo",
    }
)
#: Extensions that end an asset name like `logo@2x.png`, not a domain (F06).
_FILE_EXT = frozenset(
    "png jpg jpeg gif webp svg ico bmp tif tiff pdf js mjs cjs ts tsx jsx css scss less json yml yaml "
    "toml tar gz tgz zip py rb go rs java kt md txt csv htm html xml wasm map lock sh exe dll so dmg apk "
    "mp3 mp4 mov avi webm wav ogg woff woff2 ttf otf eot".split()
)
_DUMMY_NUMBERS = frozenset({"1234567890", "9876543210", "0123456789"})

#: Assigned country calling codes. A grouped `+CC ...` whose first group is
#: not one is a list of numbers, not a phone (F05: "+150 200 350 400").
_CC = frozenset(
    {"1", "7"}
    | {str(c) for c in (20, 27, 30, 31, 32, 33, 34, 36, 39, 40, 41, 43, 44, 45, 46, 47, 48, 49,
                         51, 52, 53, 54, 55, 56, 57, 58, 60, 61, 62, 63, 64, 65, 66, 81, 82, 84,
                         86, 90, 91, 92, 93, 94, 95, 98)}
    | {str(c) for c in (211, 212, 213, 216, 218, 290, 291, 297, 298, 299, 420, 421, 423, 800,
                         808, 850, 852, 853, 855, 856, 870, 878, 880, 881, 882, 883, 886, 888)}
    | {str(c) for r in ((220, 270), (350, 360), (370, 390), (500, 510), (590, 600), (670, 693),
                        (960, 969), (970, 980), (992, 999)) for c in range(*r)}
)

#: Read right before a number: an identifier or money/range word makes it
#: not a phone; a contact word makes it one (F05).
_NOT_PHONE_CUE_RE = re.compile(
    r"(?:₹|\$|€|£|\b(?:rs|inr|usd|eur|ctc|lpa|salary|salaries|stipend|range|between|budget|price|"
    r"cost|rent|pay|package|band|ports?|id|ids|sku|order(?:\s+(?:no|number|id))?|ref(?:erence)?"
    r"(?:\s+(?:no|number))?|ticket|invoice(?:\s+(?:no|number))?|account(?:\s+(?:no|number))?|acct|a/c|"
    r"txn|transaction(?:\s+id)?|serial(?:\s+(?:no|number))?|isbn|otp|imei|aadhaar|pan|uan|gstin|cin|"
    r"tracking(?:\s+(?:no|number|id))?|awb|pnr|timestamp|epoch|seed|hash|checksum|row)\b)[\W_]{0,4}$",
    re.IGNORECASE,
)
_CONTACT_CUE_RE = re.compile(
    r"\b(?:phone|ph|mobile|mob|cell|tel|telephone|whatsapp|call|contact|reach|landline|fax|sms|"
    r"dial|helpline|number)\b",
    re.IGNORECASE,
)
_OPERATOR_BEFORE_RE = re.compile(r"[=+*/×^<>%!]\s*$|\d\s+$")
_UNIT_AFTER_RE = re.compile(
    r"^\s*(?:bytes?|kb|mb|gb|tb|kib|mib|gib|tib|bits?|people|persons|users|rows|records|items|times|"
    r"ms|seconds|secs|years|km|kms|rupees|dollars|views|downloads|followers|votes|tokens|words|steps|"
    r"cells|units|copies|lakh|crore|million|billion)\b",
    re.IGNORECASE,
)


def _ascii_digits(text: str) -> str:
    """Every Unicode decimal digit as its ASCII digit (F01)."""
    if (text or "").isascii():
        return text or ""
    return "".join(
        str(unicodedata.digit(ch, 0)) if ch.isdecimal() and not ("0" <= ch <= "9") else ch for ch in text
    )


def _digits(text: str) -> str:
    return re.sub(r"\D", "", _ascii_digits(text or ""))


def _phone_key(text: str) -> str:
    return _digits(text)[-10:]


_CC_HEAD_RE = re.compile(r"^\(?(?:\+|00)(\d{1,3})\)?(?=[" + re.escape(_SEP_CHARS) + r".(])")


def _intl_parts(span: str) -> Optional[Tuple[str, str]]:
    """(country code, national digits) of a `+CC`, `00CC` or `(+CC)` span, or
    None for a number written without one."""
    s = _ascii_digits(span.strip())
    m = _CC_HEAD_RE.match(s)
    if m:
        return m.group(1), _digits(s[m.end():])
    if s.startswith(("+", "00", "(+")):
        digits = _digits(s)
        if s.startswith("00"):
            digits = digits[2:]
        if len(digits) > 10:
            return digits[: len(digits) - 10], digits[-10:]
        return "", digits
    return None


def _is_phone(span: str) -> bool:
    parts = _intl_parts(span)
    if parts is not None:
        cc, national = parts
        if cc and cc not in _CC:
            return False
        if cc == "91":
            return len(national) == 10
        # International: 8-15 digits after the country code.
        return 8 <= len(national) <= 15
    return len(_digits(span)) >= 10


def _dummy_phone(span: str) -> bool:
    key = _phone_key(span)
    return len(set(key)) <= 1 or key in _DUMMY_NUMBERS or key.endswith("000000")


def _phone_is_personal(text: str, start: int, end: int) -> bool:
    """Is the phone-shaped text[start:end] a phone, read with the words around
    it? A sum, a decimal, a money range, an identifier and a count with a
    unit are not (F05). Reads at most `_BEFORE` characters before it and
    `_AFTER` after it."""
    span = text[start:end]
    if not _is_phone(span) or _dummy_phone(span):
        return False
    before = text[max(0, start - _BEFORE) : start]
    if _NOT_PHONE_CUE_RE.search(before):
        return False
    if _CONTACT_CUE_RE.search(before[-30:]):
        return True
    if _OPERATOR_BEFORE_RE.search(before):
        return False
    if _UNIT_AFTER_RE.match(text[end : end + _AFTER]):
        return False
    halves = re.fullmatch(r"(\d{5})" + _S + r"(\d{5})", _ascii_digits(span))
    if halves and halves.group(1).endswith("000") and halves.group(2).endswith("000"):
        return False  # "70000-85000 a month": a range of round figures
    if _intl_parts(span) is not None and re.search(r"[(\d]\s*$", before):
        return False  # "100 +150 200": a sum
    return True


def _always_allowed_email(email: str) -> bool:
    local, _, domain = email.lower().partition("@")
    if _EXAMPLE_DOMAIN_RE.search(domain):
        return True
    local = local.split("+", 1)[0]
    return local in _ROLE_MAILBOXES or re.split(r"[._-]", local, maxsplit=1)[0] in _ROLE_MAILBOXES


def _email_is_personal(text: str, start: int, end: int) -> bool:
    email = text[start:end]
    labels = email.rpartition("@")[2].split(".")
    if len(labels) == 2 and labels[-1].lower() in _FILE_EXT:
        return False  # logo@2x.png, icon@3x.webp
    if re.search(r"://[^\s/@]*$", text[max(0, start - _BEFORE) : start]):
        return False  # scheme://user:password@host
    return not _always_allowed_email(email)


def _decide(text: str, m: "re.Match[str]") -> Optional[str]:
    """The mask for this candidate when it is a personal detail, else None
    (before the allowed-text check)."""
    if m.group("phone") is not None:
        return PHONE_MASK if _phone_is_personal(text, m.start(), m.end()) else None
    return EMAIL_MASK if _email_is_personal(text, m.start(), m.end()) else None


# ---------------------------------------------------------------------------
# Allowed text: whole values the prompt held (F07, F13)
# ---------------------------------------------------------------------------

_LOOSE_EMAIL_RE = re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+")
#: Digit groups joined by at most three separator characters form a chain.
_JOIN = re.escape(_SEP_CHARS + ".()/+")
_CHAIN_RE = re.compile(rf"(?:\+|00)?\d+(?:[{_JOIN}]{{1,3}}\d+)*")
_GROUP_RE = re.compile(r"(\+)?(\d+)")


class _Allowed:
    """The phone numbers and e-mail addresses a text holds, as whole values:
    a number is the digits of whole groups (any spacing), with the country
    code written in front of it, if any; an address is the whole address."""

    def __init__(self, text: str) -> None:
        text = _ascii_digits(text or "")
        self.emails: Set[str] = {m.group(0).lower() for m in _LOOSE_EMAIL_RE.finditer(text)}
        #: last 10 digits -> the country codes seen in front ("" = none)
        self.keys10: dict = {}
        #: whole numbers of 7-9 digits (a short local form, F13)
        self.short: Set[str] = set()
        for chain in _CHAIN_RE.finditer(text):
            self._add_chain(chain.group(0))

    def _add(self, key: str, cc: str) -> None:
        self.keys10.setdefault(key, set()).add(cc)

    def _add_chain(self, chain: str) -> None:
        groups: List[Tuple[str, bool]] = []
        for k, g in enumerate(_GROUP_RE.finditer(chain)):
            digits = g.group(2)
            plus = bool(g.group(1))
            if k == 0 and not plus and chain.startswith("00") and len(digits) > 2:
                digits, plus = digits[2:], True
            groups.append((digits, plus and len(digits) <= 3))
        for i, (head, is_cc) in enumerate(groups):
            ctx = groups[i - 1][0] if i > 0 and groups[i - 1][1] else ""
            digits = ""
            for j in range(i, len(groups)):
                digits += groups[j][0]
                if len(digits) > 18:
                    break
                if is_cc:
                    national = digits[len(head):]
                    if len(national) >= 10:
                        self._add(national[-10:], head)
                    elif 7 <= len(national) <= 9:
                        self.short.add(national)
                    if len(digits) >= 10:
                        self._add(digits[-10:], head)
                elif len(digits) >= 10:
                    self._add(digits[-10:], ctx)
                elif 7 <= len(digits) <= 9:
                    self.short.add(digits)

    def phone(self, span: str) -> bool:
        parts = _intl_parts(span)
        ccs = self.keys10.get(_phone_key(span))
        if ccs is not None:
            if parts is None or not parts[0] or parts[0] in ccs or "" in ccs:
                return True
        if parts is not None and parts[0]:
            national = parts[1]
            if 7 <= len(national) <= 9 and national in self.short:
                return True
        return False

    def email(self, email: str) -> bool:
        return email.lower() in self.emails


class Guard:
    """Masks phones and e-mails the model was not given.

    `allowed_text` is the prompt the model was sent (system prompt, identity
    line, facts, cross-chat recall, grounding, history, question). A phone is
    allowed when the same number is written there (any spacing, with or
    without its country code or trunk 0); an e-mail when the same whole
    address is (any case). Example, placeholder and role-mailbox addresses
    and dummy numbers are always allowed.
    """

    def __init__(self, allowed_text: str = "") -> None:
        self._allowed = _Allowed(allowed_text or "")
        self.count = 0
        # Streaming state: `_text` holds the unreleased tail plus some
        # released characters before it, for the lookbehinds and the cues.
        self._text = ""
        self._done = 0  # index in _text where the unreleased part starts

    # -- decisions ---------------------------------------------------------

    def _replacement(self, text: str, match: "re.Match[str]") -> Optional[str]:
        repl = _decide(text, match)
        if repl is None:
            return None
        span = match.group(0)
        if repl == PHONE_MASK:
            return None if self._allowed.phone(span) else PHONE_MASK
        return None if self._allowed.email(span) else EMAIL_MASK

    def _masked(self, text: str, start: int, end: int, *, counted: bool = True) -> str:
        """text[start:end] with every candidate inside it replaced. The scan
        starts at `start` with the text before it visible to lookbehinds."""
        out: List[str] = []
        at = start
        for m in CANDIDATE_RE.finditer(text, start, len(text)):
            if m.start() >= end:
                break
            repl = self._replacement(text, m)
            if repl is None:
                continue
            out.append(text[at : m.start()])
            out.append(repl)
            at = m.end()
            if counted:
                self.count += 1
        out.append(text[at:end])
        return "".join(out)

    def mask(self, text: str, *, counted: bool = True) -> str:
        """The whole text, masked."""
        if not text:
            return text
        return self._masked(text, 0, len(text), counted=counted)

    # -- streaming ---------------------------------------------------------

    def _release_point(self) -> int:
        """Where the text that can no longer change ends.

        Held: the trailing run of phone characters from its first digit, '+'
        or '(' (no phone starts with anything else), the trailing run of
        e-mail characters (any word may still turn out to be the local part
        of an address), and a phone whose decision still waits for the words
        after it. A run is held for at most the longest candidate of its
        kind, so a run beyond that (a loop of "0 0 0", a base64 blob) keeps
        streaming and the loop guard behind the mask still sees it. A
        candidate can only straddle the point if it ends before the first
        character of the held tail that its own class excludes, so it is
        already complete and the loop below moves the point to its start.
        The scan starts at the released position with the released text
        still visible, so a lookbehind reads exactly what it reads in
        `mask(full_text)`."""
        text = self._text
        end = len(text)
        floor = self._done
        lo = max(floor, end - _PHONE_CAP)
        p = end
        while p > lo and _phone_char(text[p - 1]):
            p -= 1
        while p < end and not _phone_start(text[p]):
            p += 1
        lo = max(floor, end - _EMAIL_CAP)
        q = end
        while q > lo and _EMAIL_CLASS_RE.match(text[q - 1]):
            q -= 1
        # A local part has at most 64 characters and no '@': with no '@' in
        # the run yet, only its last 64 characters can still start an
        # address; with one, nothing more than 64 before the first.
        at = text.find("@", q, end)
        q = max(q, (end if at < 0 else at) - _LOCAL_CAP)
        r = min(p, q)
        for m in CANDIDATE_RE.finditer(text, floor, end):
            if m.start() >= r:
                break
            if m.end() > r or (m.group("phone") is not None and m.end() + _AFTER > end):
                r = m.start()
                break
        return max(r, floor)

    def feed(self, delta: str) -> str:
        """Add a delta; return the masked text that can be released now."""
        if not delta:
            return ""
        self._text += delta
        r = self._release_point()
        if r <= self._done:
            return ""
        released = self._masked(self._text, self._done, r)
        self._done = r
        self._trim()
        return released

    def finish(self) -> str:
        """Release everything still held."""
        if self._done >= len(self._text):
            return ""
        released = self._masked(self._text, self._done, len(self._text))
        self._done = len(self._text)
        self._trim()
        return released

    def held(self) -> str:
        """The text fed but not released yet."""
        return self._text[self._done :]

    def _trim(self) -> None:
        keep = _BEFORE + 8  # context for the lookbehinds and the cues
        if self._done > keep:
            cut = self._done - keep
            self._text = self._text[cut:]
            self._done -= cut


def _phone_char(ch: str) -> bool:
    return ch.isdecimal() or ch in _SEP_CHARS or ch in "+()."


def _phone_start(ch: str) -> bool:
    return ch.isdecimal() or ch in "+("


def prompt_text(messages: Sequence[dict]) -> str:
    """The text of a message list, as `Guard` reads it."""
    parts: List[str] = []
    for m in messages or ():
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Saved facts: other people's contact details (owner decision 2026-11-25)
# ---------------------------------------------------------------------------

CONTACT_MASK = "[contact detail not saved]"

#: Extra phone shapes only the facts side reads (it can afford a false hit,
#: the answer mask cannot): dotted 5.5 and paired 2-2-2-2-2 (data F1).
_FACT_PHONE_EXTRA_RE = re.compile(
    rf"(?<![\w+.])(?:[6-9]\d{{4}}\.\d{{5}}|[6-9]\d(?:{_S}\d{{2}}){{4}})(?![\w])"
)
#: Where an address statement ends: a clause end, or a full stop before the
#: next sentence of the fact (F11: never the rest of the fact).
_STATEMENT_END = (
    r"(?=;|\n|\.\s+(?:the\s+user|user|i|my|he|she|they|his|her|their|it|this)\b|\.\s*$|$)"
)
_ADDRESS_RE = re.compile(
    r"\baddress(?:\s+is|\s*:|\s+-)\s*(?P<a>.+?)" + _STATEMENT_END
    + r"|\b(?:home|postal|residential|mailing|street|office|permanent|current)\s+address\s+(?P<b>.+?)"
    + _STATEMENT_END
    + r"|\b(?:lives|resides|stays|living|residing|staying)\s+at\s+(?P<c>.+?)" + _STATEMENT_END
    + r"|\bcontact(?:\s+(?:info(?:rmation)?|details))?\s*[:\-–]\s*(?P<d>.+?)" + _STATEMENT_END,
    re.IGNORECASE,
)

#: A clause ends at ';', a newline, or a sentence stop that is not an
#: abbreviation or an initial ("Mr. Sharma", "e.g. ", "A. Kumar") (F10).
_CLAUSE_END_RE = re.compile(r"[;\n]|[.!?](?=\s)")
_ABBREV_RE = re.compile(
    r"(?:\b(?:mr|mrs|ms|dr|prof|st|sr|jr|shri|smt|no|vs|etc|e\.g|i\.e|approx|dept|govt)|\b[A-Za-z])$",
    re.IGNORECASE,
)

#: Words that name another person the detail may belong to (F10).
_RELATION = (
    r"manager|boss|supervisor|mentor|mentee|lead|team\s?lead|tl|cto|ceo|cfo|coo|cmo|founder|"
    r"co-?founder|director|head|hr|recruiter|interviewer|interviewee|referee|reference|guardian|"
    r"wife|husband|spouse|partner|girlfriend|boyfriend|fianc[eé]e?|son|daughter|mother|father|mom|mum|"
    r"dad|parent|brother|sister|sibling|nephew|niece|grand(?:mother|father|ma|pa|son|daughter)|uncle|"
    r"aunt|cousin|friend|roommate|flatmate|housemate|colleague|co-?worker|teammate|team\s+member|team|"
    r"staff|reportee|direct\s+report|subordinate|junior|client|customer|candidate|applicant|employee|"
    r"intern|contractor|freelancer|landlord|tenant|doctor|lawyer|accountant|assistant|secretary|"
    r"neighbou?r|child|children|kid|vendor|supplier|employer|emergency\s+contact|contact\s+person|"
    r"point\s+of\s+contact|in-?laws?"
)
_RELATION_RE = re.compile(rf"\b(?:{_RELATION})(?:e?s)?\b", re.IGNORECASE)
#: A titled or possessive name: "Mr. Sharma", "Ravi's" (case matters).
_NAME_REF_RE = re.compile(
    r"\b(?:[Mm]r|[Mm]rs|[Mm]s|[Mm]iss|[Dd]r|[Pp]rof|[Ss]hri|[Ss]mt)\.?\s+[A-Z]"
    r"|\b(?!(?:User|It|That|What|Here|There|Let|He|She|Who|This)['’]s\b)[A-Z][\w-]*['’]s\b"
)
#: Another person as the object of a contact verb, or a recipient ("send the
#: offer letter to <e-mail>").
_OBJECT_RE = re.compile(
    r"\b(?:reach|contact|call|e-?mail|mail|text|message|ping|phone|ring|whatsapp)\s+(?:him|her|them)\b"
    r"|\b(?:send|sent|sending|forward|forwarded|write|wrote|mail|e-?mail|cc|bcc|deliver|share|submit)\b"
    r"(?:(?!\b(?:the\s+user|me|us)\b)[^.;\n]){0,60}?\b(?:to|at|on)\s*$",
    re.IGNORECASE,
)
_CONTACT_NOUN = (
    r"(?:phone|mobile|cell|number|whatsapp|contact(?:\s+(?:details|info(?:rmation)?|number))?|"
    r"e-?mail(?:\s+(?:address|id))?|mail\s+id|address|landline|telephone|tel)"
)
#: The user named as the HOLDER of the detail.
_HOLDER_RE = re.compile(
    rf"\b(?:the\s+user['’]s|user['’]s|my|our)\s+(?:[\w-]+\s+){{0,3}}?{_CONTACT_NOUN}\b"
    r"|\b(?:the\s+user|user|i|we)\b(?!['’]s)[^.;\n]{0,60}?\b(?:can\s+be\s+(?:reached|contacted)|"
    r"is\s+reachable|am\s+reachable|reachable|uses|use|has|have|gave|shared|listed)\b"
    r"|\b(?:reach|contact|call|e-?mail|text|message|ping|write\s+to)\s+(?:the\s+user|me|us)\b",
    re.IGNORECASE,
)
#: The user's own job, which names the user and nobody else (R9): "the user
#: is an HR manager", "works as a recruiter at Acme", "the user, a senior
#: recruiter,". Blanked before the fact is searched for other people.
_JOB_WORDS = (
    r"executive|officer|specialist|consultant|associate|analyst|engineer|developer|generalist|"
    r"coordinator|administrator|admin|partner|manager|lead|head|recruiter|member"
)
_PREDICATE_RE = re.compile(
    r"\b(?:the\s+user|user|i)\s*,?\s*(?:is|am|was|works|worked|working|serves|joined)\s+"
    r"(?:(?:as|at|for|in)\s+)?(?:an?\s+|the\s+)?(?:[\w-]+\s+){0,4}?(?:" + _RELATION + r")(?:e?s)?\b"
    r"(?:\s+(?:" + _JOB_WORDS + r"))*"
    r"(?:\s+(?:of|at|in|for|with)\s+(?:the\s+)?[\w&.-]+(?:\s+[\w&.-]+){0,3})?"
    r"|\bthe\s+user\s*,\s*(?:an?|the)\s+[^,;\n]{1,60},",
    re.IGNORECASE,
)
_USER_SUBJECT_RE = re.compile(r"^\W*(?:(?:the\s+)?user(?:['’]s)?|i|i'm|i am|my|me)\b", re.IGNORECASE)
#: The fact is about the user somewhere before the detail ("Asha is the
#: user's name. Her phone is ...").
_USER_MENTION_RE = re.compile(r"\b(?i:the\s+user|user|my|me)\b|\bI\b")
#: A clause that is only a label in front of the detail: "phone", "her email is".
_BARE_LABEL_RE = re.compile(
    rf"^\W*(?:and\s+)?(?:(?:his|her|their)\s+)?(?:(?:personal|work|official|primary|alternate|"
    rf"alternative|other|second|office|home)\s+)*{_CONTACT_NOUN}(?:\s+(?:number|no\.?|id))?"
    r"\s*(?:is|:|-|=|–)?\s*$",
    re.IGNORECASE,
)


def _contact_spans(fact: str) -> List[tuple]:
    """(start, end) of every phone, e-mail and address statement in `fact`."""
    spans: List[tuple] = []
    for m in CANDIDATE_RE.finditer(fact):
        if _decide(fact, m) is not None:
            spans.append((m.start(), m.end()))
    for m in _FACT_PHONE_EXTRA_RE.finditer(fact):
        if not _dummy_phone(m.group(0)):
            spans.append((m.start(), m.end()))
    for m in _ADDRESS_RE.finditer(fact):
        group = next(g for g in "abcd" if m.group(g) is not None)
        if m.group(group).strip():
            spans.append((m.start(group), m.end(group)))
    # A contact statement holds the phone inside it: merge overlaps.
    merged: List[list] = []
    for s, e in sorted(spans):
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [tuple(x) for x in merged]


def _clause_start(prefix: str) -> int:
    start = 0
    for m in _CLAUSE_END_RE.finditer(prefix):
        if m.group(0) == "." and _ABBREV_RE.search(prefix[: m.start()]):
            continue
        start = m.end()
    return start


def _blank(text: str, spans: Iterable[tuple]) -> str:
    """`text` with every span blanked to spaces (same length, same offsets)."""
    chars = list(text)
    for s, e in spans:
        for i in range(max(0, s), min(len(chars), e)):
            chars[i] = " "
    return "".join(chars)


def _others(text: str) -> List["re.Match[str]"]:
    return [*_RELATION_RE.finditer(text), *_NAME_REF_RE.finditer(text), *_OBJECT_RE.finditer(text)]


def _about_the_user(fact: str, start: int) -> bool:
    """Is the detail at `start` the user's own?

    Read in its clause, the nearest reference wins: "the user's phone is"
    and "the user can be reached at" name the user as its holder; a relation
    word ("the user's mentor", "her manager"), a titled or possessive name
    ("Mr. Sharma", "Ravi's"), "reach him at" or "send it to" name someone
    else, and so does a relation word inside the holder ("the user's
    emergency contact number"). The user's own job is not someone else ("the
    user is a recruiter"). A detail with no holder in its clause ("phone
    ...", "her email is ...") is the user's only when the fact is about the
    user and names nobody else before it. Anything else is not the user's:
    dropping one of the user's own details is the cheaper error (F10)."""
    prefix = fact[:start]
    neutral = _blank(prefix, (m.span() for m in _PREDICATE_RE.finditer(prefix)))
    cs = _clause_start(prefix)
    clause = neutral[cs:]
    holders = [m.span() for m in _HOLDER_RE.finditer(clause)]
    holder_end = max((e for _s, e in holders), default=-1)
    for m in _others(clause):
        inside = any(s <= m.start() and m.end() <= e for s, e in holders)
        if inside and _RELATION_RE.fullmatch(m.group(0)):
            return False
        if not inside and m.end() > holder_end:
            return False
    if holder_end >= 0:
        return True
    about_user = bool(
        _USER_SUBJECT_RE.match(fact) or _USER_SUBJECT_RE.match(fact[cs:]) or _USER_MENTION_RE.search(neutral)
    )
    if not about_user or _others(neutral):
        return False
    raw = prefix[cs:]
    return not raw.strip() or bool(_BARE_LABEL_RE.match(raw) or _USER_SUBJECT_RE.match(raw))


def third_party_contact(fact: str) -> bool:
    """Does this fact hold a contact detail of someone other than the user?"""
    text = fact or ""
    return any(not _about_the_user(text, s) for s, _e in _contact_spans(text))


def mask_third_party_contacts(fact: str) -> str:
    """The fact with every third-party contact detail masked."""
    text = fact or ""
    spans = [(s, e) for s, e in _contact_spans(text) if not _about_the_user(text, s)]
    if not spans:
        return text
    out: List[str] = []
    at = 0
    for s, e in spans:
        if s < at:
            continue
        out.append(text[at:s])
        out.append(CONTACT_MASK)
        at = e
    out.append(text[at:])
    return "".join(out)


def mask_facts(rows: Iterable[dict]) -> List[dict]:
    """Saved-fact rows with third-party contact details masked (copies)."""
    out: List[dict] = []
    for row in rows or ():
        fact = row.get("fact") if isinstance(row, dict) else None
        if isinstance(fact, str):
            masked = mask_third_party_contacts(fact)
            if masked != fact:
                row = {**row, "fact": masked}
        out.append(row)
    return out
