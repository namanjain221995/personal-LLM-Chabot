"""Small talk answered by code (prerequisite P6 of 2026-11-25): a greeting, thanks, an acknowledgement, a goodbye, a
presence check ("are you there"), an empty ask ("can you tell me one thing") or a help ask gets one fixed line, with no
tool and no model call (``prompts/small_talk.md``). Before it, such a turn ran the route, names and knowledge calls,
and one answer echoed a nudge of the knowledge agent (the prompt wording leaked).

:func:`small_talk` is a full match: every word of the turn must belong to a phrase of one of the tables below, so any
other word (an object, a field, a business term, a period, a name, a record word) keeps the whole pipeline: "hi, how
many candidates joined this month" is a question. In order:

1. more than :data:`MAX_CHARS` characters: no small talk. The text is cased down, ``’`` read as ``'``, emoji,
   punctuation and symbols dropped, 3 or more equal letters read as 2 ("hiiii" -> "hii"), spaces joined;
2. any digit, ``@`` or ``_``: no small talk (an Id, a record number, an e-mail address, an API name);
3. a few emoji are words (:data:`_EMOJI`: thumbs-up and OK hand "ok", folded hands "thanks", waving hand "hi");
   please, pls, plz and kindly are dropped wherever they stand (no phrase holds them: "can you please tell me one
   thing"); address words (:data:`_ADDRESS`: sir, ji, bro, team, there, all, again ...) may stand between phrases,
   unless a phrase holds them ("are you there", "all right"); a turn of address words only is no small talk;
4. more than :data:`MAX_WORDS` words left: no small talk;
5. the words are read left to right as whole phrases of the tables (longest first); a word that fits no phrase: no small
   talk. A word of 6 or more letters one edit from a long table word (:data:`_FUZZY`) reads as that word ("thnaks");
6. several kinds: one line, by :data:`PRIORITY`;
7. after a greeting, a self-introduction ("hi, I am <name>", "hello, this is <name> here", "mera naam <name> hai") of at
   most 2 name words is part of the greeting; the reply never repeats the name. A name word is typed with a capital
   letter, or a tail (here, hai, hoon) follows the name, and it is none of :data:`_NOT_A_NAME` (about, for, wrong,
   confused, urgent, interviews ...: "hi, this is about interviews", "hey this is wrong" and "hi I am confused" are
   no small talk; review of 2026-11-25). Without a greeting it is no small talk ("I am <name>" may ask about a person).

Never small talk: yes, no and their Hinglish forms (outside a phrase such as "no thanks"). They answer a question (also
the clarifying question of P2, ``agent/clarify.py``), and the caller's bridge sends them elsewhere anyway. Nor a turn
that points at the last answer: "how does this work" and "how do I use this" ask about what was just shown.

Standard library, :mod:`sf_assistant.prompts` and :func:`sf_assistant.knowledge.resolver.osa_distance` only: a
records-off run imports this module and still loads no engine module.
"""

from __future__ import annotations

import re
import unicodedata
from itertools import product

from sf_assistant import prompts as _prompt_files
from sf_assistant.knowledge.resolver import osa_distance

__all__ = ["SMALL_TALK_TEXT", "LINES", "KINDS", "PRIORITY", "MAX_CHARS", "MAX_WORDS", "small_talk", "line"]

SMALL_TALK_TEXT = _prompt_files.text("small_talk")
#: kind -> the fixed line code sends (prompts/small_talk.md)
LINES: dict[str, str] = dict(ln.split(": ", 1) for ln in SMALL_TALK_TEXT.split("\n") if ": " in ln)
#: When a turn holds several kinds, the line of the first of them in this order is sent.
PRIORITY = ("empty_ask", "presence", "help", "greeting", "thanks", "ack", "bye")
KINDS = frozenset(PRIORITY)
MAX_CHARS = 80
MAX_WORDS = 10
_MAX_NAME_WORDS = 2

#: Words that may stand between the phrases of a small-talk turn and say nothing of its kind.
_ADDRESS = frozenset("""sir madam maam mam ji bro buddy dear team there everyone all again mate guys assistant bot sf
salesforce""".split())
#: Words dropped wherever they stand, inside a phrase too ("can you please tell me one thing"): no phrase holds them.
_SOFT = frozenset("please pls plz kindly".split())
#: Never small talk outside a phrase: they answer a question.
_NEVER = frozenset("yes no yep nope yeah haan han ha nahi nahin na".split())
#: Emoji read as a word: thumbs-up and OK hand, folded hands, waving hand (any other emoji is dropped).
_EMOJI = {"\U0001F44D": " ok ", "\U0001F44C": " ok ", "\U0001F64F": " thanks ", "\U0001F44B": " hi "}
#: Never a name in a self-introduction: what a turn about the last answer or about a subject says after "this is" /
#: "I am" (review of 2026-11-25), and the record words of this org.
_NOT_A_NAME = frozenset("""about for regarding re from of on with to in at by into this that it these those here there
the a an my your our their his her its not no wrong right correct incorrect confused confusing urgent stuck broken
missing empty sure fine ok okay good bad new back done lost busy sorry ready asking looking trying unable getting facing
having interested curious calling writing following checking very so really just also still again only same
interview interviews candidate candidates placement placements offer offers application applications lead leads
account accounts contact contacts session sessions recruiter recruiters trainer trainers report reports data record
records field fields object objects flow flows salesforce question questions query issue problem error bug""".split())


def _phrases(text: str) -> list[tuple[str, ...]]:
    return [tuple(p.split()) for p in text.split(",") if p.strip()]


def _empty_asks() -> list[tuple[str, ...]]:
    """"(can / could / may / will) (you / i) (tell / ask / help / answer) (me / you) (one / a / some / something)
    (thing / question / query / doubt)" and the fixed empty asks."""
    out: list[tuple[str, ...]] = []
    for modal, who, verb, obj, many, noun in product(
            ("can", "could", "may", "will", "shall"), ("you", "u", "i"), ("tell", "ask", "help", "answer"),
            ("me", "you", "u", ""), ("one", "a", "some", "something", "ek", ""),
            ("thing", "question", "questions", "query", "doubt", "")):
        if many == "something" and noun or many in ("one", "a", "ek") and not noun:
            continue
        out.append(tuple(w for w in (modal, who, verb, obj, many, noun) if w))
    for modal, who, tail in product(("can", "could", "will"), ("you", "u"),
                                    ("with something", "with a question", "with one thing", "out")):
        out += _phrases(f"{modal} {who} help me {tail}")
    out += _phrases("""i have a question, i have a query, i have a doubt, i have one question, i have some questions,
        i have a question for you, i have a small question, i have a quick question, i have a small doubt,
        i want to ask, i want to ask you, i want to ask something, i want to ask a question,
        i want to ask you something, i want to ask you a question, i want to know something, i want to know one thing,
        i need help, i need some help, i need your help, need help, need some help, need your help, help me,
        help me out, quick question, small question, one question, one more question, a question, question,
        ek baat batao, ek baat bataiye, ek baat bataoge, ek baat puchu, ek baat poochu, ek baat puchun,
        mujhe kuch puchna hai, mujhe kuch poochna hai, mujhe kuch janna hai, ek sawal hai, ek sawaal hai,
        ek question hai""")
    return out


def _thanks() -> list[tuple[str, ...]]:
    """"thanks / thank you (so much / a lot ...) (for the help / for the info ...)", praise and the fixed thanks."""
    out: list[tuple[str, ...]] = []
    for word, much, what in product(
            ("thanks", "thank you", "thank u", "thankyou", "thanku", "thx", "ty"),
            ("", "so much", "a lot", "a ton", "very much"),
            ("", "for the help", "for your help", "for help", "for the info", "for the information",
             "for the answer", "for the reply", "for your time", "for that")):
        out += _phrases(" ".join(w for w in (word, much, what) if w))
    return out + _phrases("""thnx, thnks, thanx, tnx, tq, tysm, many thanks, much appreciated, appreciate it,
        dhanyavad, dhanyavaad, dhanyawad, shukriya, shukria, good job, great job, well done, nice work, great work""")


_TABLE: dict[str, list[tuple[str, ...]]] = {
    "greeting": _phrases("""hi, hii, hey, heyy, heya, hiya, hello, helloo, hellow, helo, hlo, hola, yo, howdy, namaste,
        namaskar, salaam, salam, good morning, good afternoon, good evening, good day, gm, morning, how are you,
        how r u, how are u, how r you, how are you doing, hows it going, how is it going, whats up, wassup, sup,
        kaise ho, kaise hain aap, kaise ho aap, kya haal hai"""),
    "thanks": _thanks(),
    "ack": _phrases("""ok, okay, okk, k, kk, okie, okey, oki, okies, hmm, hm, cool, great, nice, perfect, awesome,
        got it, understood, noted, fine, alright, all right, sure, good, theek hai, thik hai, accha, achha, acha,
        no thanks, no thank you, thats all, thats it, nothing else, nothing more"""),
    "bye": _phrases("""bye, goodbye, bye bye, good bye, good night, gn, see you, see ya, see you later, cya,
        take care, tc, alvida, chalo bye"""),
    "presence": _phrases("""are you there, you there, u there, are u there, r u there, anyone there, still there,
        are you still there, are you online, you online, are you available, suno, test, testing, just testing,
        ping, anyone, anybody, anybody there, is anyone there, is anybody there"""),
    "empty_ask": _empty_asks(),
    # never "how does this work" or "how do I use this": after an answer, "this" is what was shown (review of 2026-11-25)
    "help": _phrases("""help, what can you do, what all can you do, who are you, what are you, what do you do,
        how do i use you, how do you work, how does this assistant work, how does this bot work,
        how do i use this assistant, how do i use this bot, what can i ask, what can i ask you, what all can i ask,
        what questions can i ask"""),
}
#: phrase -> kind, longest phrases first when read
_BY_PHRASE: dict[tuple[str, ...], str] = {}
for _kind, _list in _TABLE.items():
    for _p in _list:
        _BY_PHRASE.setdefault(_p, _kind)
_LONGEST = max(len(p) for p in _BY_PHRASE)
_ONE_WORD = frozenset(p[0] for p in _BY_PHRASE if len(p) == 1)
#: Long table words a typo of one edit is read as (6 or more letters; shorter typos are listed in the tables).
_FUZZY = ("thanks", "thankyou", "goodbye", "morning", "evening", "afternoon", "namaste", "shukriya", "dhanyavad")
#: A self-introduction after a greeting: what stands before the name, and what may follow it.
_INTRO_LEADS = _phrases("i am, im, i m, this is, my name is, myself, mera naam")
_INTRO_TAILS = frozenset("here hai hoon hu".split())


def _normal(text: str, fold: bool = True) -> list[str]:
    """The words of ``text``: cased down (``fold``), ``’`` as ``'``, apostrophes joined ("what's" -> "whats"), the
    emoji of :data:`_EMOJI` as their word, any other punctuation, symbol or emoji a space, 3 or more equal letters as
    2. With ``fold`` False the case stays (the same words, one for one)."""
    out = []
    for ch in (text.casefold() if fold else text).replace("’", "'"):
        if ch == "'":
            continue
        if ch in _EMOJI:
            out.append(_EMOJI[ch])
            continue
        cat = unicodedata.category(ch)
        out.append(ch if cat[0] == "L" or cat == "Nd" or ch.isspace() else " ")
    return re.sub(r"(.)\1{2,}", r"\1\1", "".join(out)).split()


def _fuzzy(word: str) -> str:
    if len(word) < 6 or word in _ONE_WORD or word in _ADDRESS:
        return word
    for target in _FUZZY:
        if osa_distance(word, target, 1) <= 1:
            return target
    return word


def _read(words: list[str], at: int, greeted: bool, kinds: list[str], cased: list[str] | None = None) -> bool:
    """Read ``words[at:]`` as phrases, address words and (after a greeting) a self-introduction; ``kinds`` gets the
    kind of each phrase. ``cased``: the same words as typed (a name word is capitalised, or a tail follows the name).
    A plain backtracking scan over at most :data:`MAX_WORDS` words."""
    if at == len(words):
        return True
    for n in range(min(_LONGEST, len(words) - at), 0, -1):
        kind = _BY_PHRASE.get(tuple(words[at:at + n]))
        if kind is not None:
            kinds.append(kind)
            if _read(words, at + n, greeted or kind == "greeting", kinds, cased):
                return True
            kinds.pop()
    if words[at] in _ADDRESS:
        return _read(words, at + 1, greeted, kinds, cased)
    if greeted:
        typed = cased if cased is not None and len(cased) == len(words) else words
        for lead in _INTRO_LEADS:
            if tuple(words[at:at + len(lead)]) != lead:
                continue
            start = at + len(lead)
            for size in range(1, _MAX_NAME_WORDS + 1):
                name = words[start:start + size]
                if len(name) < size or any(w in _NEVER or w in _NOT_A_NAME for w in name):
                    break
                end = start + size
                while end < len(words) and words[end] in _INTRO_TAILS:
                    end += 1
                if end == start + size and not all(w[:1].isupper() for w in typed[start:start + size]):
                    continue   # a lower-case word with no tail is no name ("hi i am recruiter")
                if _read(words, end, greeted, kinds, cased):
                    return True
    return False


def small_talk(text: str) -> str | None:
    """The kind of small talk ``text`` is (a key of :data:`LINES`), or None for anything else (see the module text).
    Never raises."""
    try:
        raw = str(text or "").strip()
        if not raw or len(raw) > MAX_CHARS or "@" in raw or "_" in raw or any(ch.isdigit() for ch in raw):
            return None
        folded, typed = _normal(raw), _normal(raw, fold=False)
        if len(typed) != len(folded):   # casefold split a letter differently: read the name rule on the folded words
            typed = folded
        pairs = [(w, c) for w, c in zip(folded, typed) if w not in _SOFT]
        words, cased = [_fuzzy(w) for w, _c in pairs], [c for _w, c in pairs]
        if not words:
            return None
        if len([w for w in words if w not in _ADDRESS]) > MAX_WORDS or len(words) > MAX_WORDS + 6:
            return None
        kinds: list[str] = []
        # a word of _NEVER outside a phrase fits nothing, so the turn is no small talk ("ok yes"; "no thanks" is one)
        if not _read(words, 0, False, kinds, cased) or not kinds:
            return None
        return next(k for k in PRIORITY if k in kinds)
    except Exception:  # noqa: BLE001 - a question never fails on this
        return None


def line(kind: str) -> str:
    """The fixed line of a small-talk kind (``prompts/small_talk.md``)."""
    return LINES[kind]
