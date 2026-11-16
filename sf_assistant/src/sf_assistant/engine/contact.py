"""The phase-1 contact rule (owner, 2026-10-26; docs/data_and_privacy.md): pure text functions, no duckdb, no catalog.

The first users are admins. Contact details (e-mail, phone, mobile, WhatsApp, fax, address, postal code, geolocation,
LinkedIn, IP address, browser or device: the catalog's ``sensitivity == "contact"`` fields) are shown when the user's
question asks for contact information, and never otherwise.

* :func:`asks_for_contact` reads the question: e-mail / mail id, phone / mobile / WhatsApp / a number to call, contact
  details, address / postal code / where someone lives / a person's location, LinkedIn, IP address, browser or device,
  in English and in the romanized Hindi and Gujarati the team mixes in ("uska number batao", "X ka contact kya hai",
  "X kidhar rehta hai", "X nu number aapo", "sampark"), plus the Devanagari and Gujarati script words. A question word
  one edit away from a cue word ("emial", "phon", "contect", "detials", "moblie") is read as that word, unless it is a
  real word ("contract", "prone"); the edit distance is the term resolver's (:func:`sf_assistant.knowledge.resolver.
  osa_distance`). A question that holds an e-mail address asks too. A question that only names a count, an object or
  the presence of a contact field does not ask: "how many contacts", "number of interviews", "point of contact", "how
  many leads have an email", "candidates without a phone number", "the address field", "device count", "the location
  of the session", e-mail messages and phone calls ("which flows send emails", "phone interviews").
* :func:`drop_contact_details` is the last check of an answer whose question did not ask: it replaces every e-mail
  address and every phone number in the text (10 to 15 digits shaped like a phone number; never a date, a range, an
  amount, a count, a record number, a number written after "id", "meeting" or "webinar", or a number the question's
  tool results hold as a whole value of a field that is no contact field, such as a Zoom meeting id: ``keep``, from
  :func:`result_numbers`).

``RecordTools.begin_question`` keeps :func:`asks_for_contact` of the question for the record tools (list, values,
group, filter, history, profile); ``RecordSession.ask`` and ``pipeline.answer`` run :func:`drop_contact_details` on
the record answer and the final answer (records on) when the question does not ask.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from sf_assistant.knowledge.resolver import osa_distance

__all__ = ["asks_for_contact", "drop_contact_details", "result_numbers", "CONTACT_CUE_RE", "EMAIL_RE",
           "PHONE_CANDIDATE_RE", "EMAIL_SHOWN", "PHONE_SHOWN"]

#: What a possessive points at ("X ka number", "X's contact", "uska pata", "her location"): the things that are contact
#: details of a person. "number" never before "of" ("X's number of interviews").
_THINGS = (r"(?:e ?mails?(?: ?ids?)?|g ?mail|mail(?: ?ids?)?|phones?(?: (?:no|nos|number|numbers|num))?|ph(?: no| number)?"
           r"|mobiles?(?: (?:no|number|numbers))?|cell(?: no| number)?|(?:numbers?|nmbr|num|nambar)(?! of)"
           r"|contacts?(?: (?:no|number|numbers|details?|info))?|address(?:es)?|pata(?! (?:nahi|nhi|nai|chal|kar|lag))"
           r"|whats ?app(?: (?:no|number))?|linked ?in|locations?)")
#: Possessives: Hindi ka / ki / ke, Gujarati nu / no / na / ni, English 's (read as " s"; never "let's", "what's" ...)
#: and the pronouns.
_POSS = (r"(?:ka|ki|ke|nu|no|na|ni|(?<!let )(?<!what )(?<!that )(?<!it )(?<!here )(?<!there )(?<!who )(?<!he )"
         r"(?<!where )(?<!how )s|his|her|their|uska|uski|uske|unka|unki|unke|iska|iski|inka|inki|eno|eni|enu|teno|tenu)")
#: Objects a "location of ..." can be that are no person ("the location of the session"): no contact question.
_NOT_PERSON = (r"(?:the |this |that |a |an |each |every |all |our )?(?:sessions?|interviews?|meetings?|events?|trainings?"
               r"|class(?:es)?|offices?|company|companies|clients?|jobs?|batch(?:es)?|venues?|webinars?|drives?"
               r"|branch(?:es)?|courses?|exams?|tests?|programs?|records?|files?|documents?|flows?)(?= |$)")
#: Cue phrases, matched as whole words on the normalised question (:func:`_norm`: lower case, every run of characters
#: that are no letter, mark or digit one space, so "E-Mail", "e_mail" and "e mail" read alike).
_CUES = (
    # e-mail
    r"e ?mails?(?: ?ids?)?", r"gmail", r"mail ?ids?", r"mail address(?:es)?", r"mailing address(?:es)?",
    r"emial|emaill|eamil|emal|emaul|imail|imel", r"e ?mials?", r"mial ?ids?", r"mail (?:of|for)",
    # phone, mobile, WhatsApp, fax, a number to call
    r"phones?(?: (?:no|nos|nr|number|numbers|num))?", r"phoneno", r"telephones?", r"(?:tel|cell) (?:no|number|of|for)",
    r"mobiles?(?: (?:no|number|numbers))?", r"mobileno", r"mob no", r"cell ?(?:phone|no|number)s?",
    r"ph (?:no|nos|number|num)", r"wa (?:no|number|nmbr)",
    r"whats ?app|watsapp|whatsap|watsap|wtsp|whtsapp", r"landlines?", r"fax(?: (?:no|number))?",
    r"phn|phne|fone|mobil|moble|mobail|mobille",
    r"contact (?:no|nos|number|numbers|num)", r"numbers? to (?:call|reach|dial|contact)",
    r"(?:number|nmbr|nambar) (?:do|dedo|de do|bhejo|batao|bata do|bataiye|chahiye|share karo|share kar do|aapo|moklo"
    r"|please|pls|plz)",
    # possessives: "X ka number", "X's contact", "uska pata", "her contact", "X nu email"
    _POSS + " " + _THINGS,
    # contact details, how to reach someone
    r"contacts? (?:details?|info|infos|information|data|address(?:es)?|list|card)", r"contactdetails?",
    r"contct|conatct|cotact|contatc", r"contact (?:of|for)",
    r"(?:how|where) (?:do |can |could |should |would |to |may )?(?:i |we |you |one |someone )?"
    r"(?:contact|reach|call|phone|email|mail|message|text|whatsapp)",
    r"(?:details?|info|information|ways?) to (?:contact|reach|call)",
    r"get in touch", r"reach (?:him|her|them|out to)", r"contact (?:him|her|them|kar(?:na|e|ein|o)|kaise|kese)",
    r"sampark", r"personal (?:details?|info|information|data)",
    # address, where someone lives, a person's location
    r"address(?:es)?|adress|addres|adres|addrss|addresss|adderss", r"postal(?: codes?)?", r"post ?codes?",
    r"pin ?codes?", r"zip ?codes?", r"street(?: address)?", r"residen(?:ce|tial)", r"home ?town",
    r"where (?:does|do|did) (?:\w+ ){0,5}(?:live|stay|reside)s?", r"(?:lives|he lives|she lives|they live) (?:in|at)",
    r"(?:which|what) (?:city|town|place|area|state|country) (?:does|do|did) (?:\w+ ){0,5}(?:live|stay|reside)s?",
    r"(?:kaha{1,2}n?|kidhar|kiddhar) (?:re?h|ra?h)(?:ta|ti|te)", r"(?:re?h|ra?h)(?:ta|ti|te) (?:kaha{1,2}n?|kidhar)",
    r"kis (?:city|shehar|sheher|jagah|area|state) (?:me|mein|main|mai|ma) (?:re?h|ra?h)(?:ta|ti|te)",
    r"ghar (?:ka pata|kaha{1,2}n?|kidhar)", r"saranam[uo]?", r"(?:kya|kyan) (?:rahe|rehe) (?:che|chhe)",
    r"geo ?locations?", r"(?:current|home|residential|live) locations?", r"latitudes?", r"longitudes?", r"coordinates",
    r"locations? of (?!" + _NOT_PERSON + ")",
    r"(?:candidates?|persons?|people|employees?|recruiters?|trainers?|interviewers?|leads?|users?|students?) locations?",
    # LinkedIn, IP address, browser or device
    r"linked ?in|linkdin|linkedln|linkden|linkdn|linkedn", r"ips?(?: address(?:es)?)?",
    r"browsers?", r"user ?agents?", r"devices?",
    # Devanagari and Gujarati script
    r"ईमेल|ई मेल|फोन|फ़ोन|मोबाइल|संपर्क|ઈમેલ|ઇમેલ|ફોન|મોબાઇલ|સંપર્ક|સરનામું",
    r"(?:का|की|के|उसका|उसकी|इसका|નો|નું|ના|એનો|તેનો) (?:नंबर|नम्बर|पता|નંબર)",
)
CONTACT_CUE_RE = re.compile("(?:^| )(?:" + "|".join(f"(?:{c})" for c in _CUES) + ")(?= |$)")
#: The contact words a presence, field or count frame names ("have an email", "the phone field", "device count").
_CORE = (r"(?:e ?mails?(?: ?(?:ids?|address(?:es)?))?|mail ?ids?|phones?(?: (?:no|nos|numbers?))?"
         r"|mobiles?(?: (?:no|nos|numbers?))?|whats ?app(?: (?:no|numbers?))?|linked ?in(?: (?:profiles?|urls?|ids?))?"
         r"|address(?:es)?|contact (?:no|nos|numbers?|details?|info)|ips?(?: address(?:es)?)?|devices?|browsers?"
         r"|user ?agents?|locations?|pin ?codes?|zip ?codes?|postal codes?)")
#: Phrases that are no ask for contact details, taken out before the cues are read: e-mail messages and phone calls
#: ("which flows send emails", "emails sent this week", "phone calls made", "phone interviews"), a mobile app, the
#: point of contact of a record, and a contact word that only says whether a field is filled ("candidates without a
#: phone number", "leads with no email") or names the field ("the address field", "device count").
_NOT_CUES_RE = re.compile(
    r"(?:^| )(?:(?:send|sends|sent|sending|triggers?) (?:an |the )?e ?mails?"
    r"|e ?mails? (?:(?:were|was|are|is|have been|has been|got|get|gets|being) )?(?:sent|notifications?"
    r"|templates?|alerts?|messages?|flows?|campaigns?|bounced|delivered|delivery|deliveries)"
    r"|phone (?:calls?|screens?|screenings?|interviews?|rounds?)|calls? by phone|telephonic (?:interviews?|rounds?)"
    r"|mobile (?:apps?|applications?|users?|versions?|views?|platforms?|friendly|responsive)"
    r"|(?:points? of|primary|main|key|billing|technical|business|account|client|company) contacts?"
    r"|(?:without|missing|lacking|lacks|lack|(?:with|have|has|having|had|got) no|no valid|bina) (?:an? |any |the "
    r"|valid |their |his |her |koi )?" + _CORE + r"|" + _CORE + r" (?:nahi|nhi|nai) (?:hai|he|h|hain|ho)" +
    r"|(?:the |a |an )?" + _CORE + r" (?:fields?|columns?|counts?|fill rate|coverage))(?= |$)")
#: In a question that counts ("how many", "count of", "kitne"): a contact word that says what the counted records have
#: ("how many leads have an email", "accounts with a phone number", "email is blank for how many", "kitne leads ke
#: paas email hai").
_COUNT_RE = re.compile(r"(?:^| )(?:how many|count|counts|number of|total|kitne|kitni|kitna|ketla|ketli|ketlu|no of"
                       r"|percentage|percent|share|ratio)(?= |$)")
_HAVE_RE = re.compile(r"(?:^| )(?:(?:with|have|has|having|had|holding|ke paa?s) (?:an? |any |the |valid |a valid |koi )?"
                      + _CORE +
                      r"|" + _CORE + r" (?:is|are|was|were) (?:not )?(?:blank|empty|filled|missing|null|set|populated"
                      r"|present|available))(?= |$)")
#: Words one edit from which a question word is read as that word (:func:`_fuzzy`), and the real words one edit from
#: one of them that are never read so (the English word lists, same first letter; "emil" is a first name).
_FUZZY = ("email", "emails", "phone", "phones", "mobile", "mobiles", "contact", "contacts", "address", "details",
          "detail", "number", "numbers", "whatsapp", "linkedin", "telephone")
_REAL_WORDS = frozenset({"emil", "pone", "prone", "phony", "phoney", "phoned", "phonies", "phoneys", "motile",
                         "motiles", "contract", "contracts", "derail", "derails", "detain", "detains", "numbed",
                         "telephoned", "telephony"})
#: An e-mail address.
EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
#: What may be a phone number: an optional + or 00, then digits with spaces, dots, dashes or brackets between them,
#: not inside a word, a record number ("REC-00024") or a longer number; :func:`_is_phone` decides.
PHONE_CANDIDATE_RE = re.compile(r"(?<![\w+.,/-])(?:\+|00)?\(?\d[\d().\- \t]{7,22}\d\)?(?![\w.,/-]?\d)")
_DATE_RE = re.compile(r"\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}")
#: The words before a number that make it an id, never a phone number ("Meeting ID 812 3456 7890", "Zoom_Meeting_Id__c:
#: 81234567890", "webinar 812...").
_ID_BEFORE_RE = re.compile(r"(?:(?<![a-z])ids?(?:__c)?|meeting|webinar)\W{0,3}$", re.IGNORECASE)
_SPLIT_RE = re.compile(r"(\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}| - )")
EMAIL_SHOWN = "[e-mail not shown]"
PHONE_SHOWN = "[phone number not shown]"


def _norm(text: Any) -> str:
    t = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return " ".join("".join(ch if unicodedata.category(ch)[0] in "LMN" else " " for ch in t).split())


def _fuzzy(word: str) -> str:
    """``word``, or the :data:`_FUZZY` word one edit away with the same first letter (``emial`` -> ``email``,
    ``contect`` -> ``contact``); a word of fewer than 4 letters, a cue word itself and a real word stay."""
    if len(word) < 4 or not word.isalpha() or word in _FUZZY or word in _REAL_WORDS:
        return word
    for cue in _FUZZY:
        if cue[0] == word[0] and osa_distance(word, cue, 1) <= 1:
            return cue
    return word


def asks_for_contact(question: Any) -> bool:
    """True when the question asks for contact information (see the module text); False for "" and None."""
    if EMAIL_RE.search(str(question or "")):
        return True
    q = " ".join(_fuzzy(w) for w in _norm(question).split())
    q = _NOT_CUES_RE.sub(" ", q)
    if _COUNT_RE.search(q):
        q = _HAVE_RE.sub(" ", q)
    return bool(CONTACT_CUE_RE.search(" ".join(q.split())))


def _digits(raw: str) -> str:
    s = raw.strip()
    return re.sub(r"\D", "", s[2:] if s.startswith("00") else s.lstrip("+"))


def _is_phone(raw: str) -> bool:
    """A phone number: 10 to 15 digits (an international prefix + or 00 not counted); at most 5 digit groups, at
    least one of 3 digits or more, no 1-digit group after the first; no decimal number ("12345678.90"). Dates and
    ranges are split off before (:func:`drop_contact_details`)."""
    s = raw.strip()
    if re.fullmatch(r"\d+\.\d{1,2}", s):
        return False
    body = s[2:] if s.startswith("00") else s.lstrip("+")
    digits = re.sub(r"\D", "", body)
    if not 10 <= len(digits) <= 15:
        return False
    groups = re.findall(r"\d+", body)
    if len(groups) > 5 or max(len(g) for g in groups) < 3:
        return False
    return not any(len(g) == 1 for g in groups[1:])


def result_numbers(results: Iterable[Any]) -> frozenset[str]:
    """The digits of every phone-shaped number that ``results`` (tool results: dicts, lists and values) hold as a
    whole value, such as a Zoom meeting id: :func:`drop_contact_details` keeps them. A contact field's value is in
    no result of a question that does not ask for contact details, and a number inside a longer text is no whole
    value, so neither is kept."""
    out: set[str] = set()
    stack = list(results)
    while stack:
        v = stack.pop()
        if isinstance(v, dict):
            stack.extend(v.values())
        elif isinstance(v, (list, tuple)):
            stack.extend(v)
        elif isinstance(v, (str, int)) and not isinstance(v, bool):
            s = str(v).strip()
            if len(s) <= 24 and PHONE_CANDIDATE_RE.fullmatch(s) and _is_phone(s):
                out.add(_digits(s))
    return frozenset(out)


def drop_contact_details(text: Any, keep: Iterable[str] = ()) -> tuple[str, int]:
    """``(text without e-mail addresses and phone numbers, how many were replaced)``: each one becomes
    :data:`EMAIL_SHOWN` or :data:`PHONE_SHOWN`. For an answer whose question did not ask for contact details.
    ``keep``: the digits of numbers the tool results hold as a whole value (:func:`result_numbers`), never replaced."""
    n = 0
    kept = frozenset(keep)

    def mail(_m: re.Match) -> str:
        nonlocal n
        n += 1
        return EMAIL_SHOWN

    def phone(m: re.Match) -> str:
        nonlocal n
        raw = m.group(0)
        if _DATE_RE.search(raw) or " - " in raw:   # a date or a range: the parts around it are read on their own
            return "".join(p if i % 2 else PHONE_CANDIDATE_RE.sub(phone, p) for i, p in enumerate(_SPLIT_RE.split(raw)))
        if not _is_phone(raw) or _ID_BEFORE_RE.search(m.string[max(0, m.start() - 30):m.start()]) or \
                _digits(raw) in kept:
            return raw
        n += 1
        return PHONE_SHOWN

    s = EMAIL_RE.sub(mail, str(text or ""))
    return PHONE_CANDIDATE_RE.sub(phone, s), n
