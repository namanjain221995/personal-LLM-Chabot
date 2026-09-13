"""How current does an answer have to be?

Three levels, decided BEFORE any expensive work:

    STATIC    the answer does not change ("what is photosynthesis?")
    RECENT    it changes on the scale of months ("who is the VP of India?")
    REALTIME  it changes hourly or faster ("NVIDIA stock price right now")

WHY THIS EXISTS. The 35B's weights are frozen at its training cut-off, so on
2026-08-31 it answered "who's vice president of india" with Jagdeep Dhankhar —
confidently, and wrong, while 19 pages already stored on this machine said
C. P. Radhakrishnan. Knowing a question is time-sensitive is what lets the
platform reach for that evidence instead of trusting the weights.

CHEAP FIRST. The deterministic pass below settles the large majority of real
questions with regex work measured in microseconds. The router model (8B) is
consulted ONLY when the lexical signals are genuinely ambiguous, and never the
main model — spending a 35B call to decide whether to spend a 35B call is how
a "fast" mode stops being fast.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from . import metrics

log = logging.getLogger(__name__)


class Freshness(str, Enum):
    STATIC = "static"
    RECENT = "recent"
    REALTIME = "realtime"


@dataclass(frozen=True)
class Verdict:
    requirement: Freshness
    #: How old stored evidence may be before it stops counting as an answer.
    max_age_seconds: int
    #: Which rule fired — surfaced in metrics and the debug view, never to the
    #: user. Makes a misclassification diagnosable instead of mysterious.
    reason: str

    #: The answer can change within days ("latest release", "current
    #: price"): a stored page inside the RECENT window can still be stale
    #: and an auto-decided live search must not be skipped for it
    #: (ADR-0001 D6, design critique 2026-09-03).
    volatile: bool = False

    @property
    def needs_evidence(self) -> bool:
        """True when pretrained weights alone are not a trustworthy source."""
        return self.requirement is not Freshness.STATIC


#: Seconds. A REALTIME answer from a two-hour-old page is a wrong answer; a
#: RECENT one from a two-week-old page is usually still right, which is why
#: the two levels exist at all rather than one "is it fresh" boolean.
_MAX_AGE = {
    Freshness.REALTIME: 3 * 3600,
    Freshness.RECENT: 14 * 24 * 3600,
    Freshness.STATIC: 365 * 24 * 3600,
}

# --------------------------------------------------------------------------
# Deterministic signals.
#
# Word-boundary anchored on purpose: a bare "now" substring matches "known",
# "nowhere" and "knowledge", which would drag half of all questions into
# REALTIME and defeat the point of having levels.
# --------------------------------------------------------------------------

_REALTIME = re.compile(
    r"\b("
    r"right now|at the moment|as of now|currently trading|"
    r"stock price|share price|exchange rate|conversion rate|"
    r"today'?s|tonight|this morning|this afternoon|live score|"
    r"score (?:of|in) the|weather|temperature outside|"
    r"trending|breaking news|happening now"
    r")\b",
    re.I,
)

#: Roles whose holder changes — the class of fact that produced the original
#: failure. Asking "who is the X" about any of these is inherently RECENT even
#: with no other time word in the sentence.
_OFFICE = re.compile(
    r"\b("
    r"president|vice[- ]president|vp|prime minister|pm|chancellor|premier|"
    r"chief minister|governor|mayor|senator|ambassador|"
    r"ceo|cto|cfo|coo|chairman|chairperson|chairwoman|"
    r"managing director|executive director|head of|leader of|"
    r"captain|coach|manager of"
    r")\b",
    re.I,
)

_RECENT = re.compile(
    r"\b("
    r"current|currently|latest|newest|most recent|up[- ]to[- ]date|"
    r"today|now|nowadays|these days|this (?:year|month|week|quarter)|"
    r"recent|recently|so far|to date|"
    r"who leads|who runs|who won|who currently|"
    r"new (?:version|release|model|ceo|president)|"
    r"released|launch(?:ed|es)?|announce(?:d|ment)?|"
    r"version|release notes|changelog|roadmap|"
    r"price|cost|salary|valuation|market cap|"
    r"election|appointed|resigned|replaced|succeeded"
    r")\b",
    re.I,
)

#: Explicitly timeless shapes. These OVERRIDE a weak RECENT hit — "what is the
#: current through a resistor" is physics, not news, and "how does X work"
#: never needs a web fetch.
_STATIC = re.compile(
    r"\b("
    # Bare "what is X" too, not only "what is a X" — the narrower form let
    # "What is photosynthesis?" fall through to the ambiguous branch and cost
    # a router call for the most obviously timeless question there is.
    # Volatile phrasings are unaffected: _REALTIME and _OFFICE are tested
    # first, and "what is the latest version" also trips _RECENT, which makes
    # it ambiguous rather than static.
    r"what is|what'?s a|what are|what does .* mean|definition of|define|"
    r"how does .* work|how do .* work|how to|why does|why do|why is|"
    r"explain|describe the|difference between|compare|"
    r"formula for|theorem|proof of|derive|"
    r"history of|who invented|who discovered|who wrote|who created|"
    r"author of|writer of|creator of|composer of|director of the film|"
    r"born in|died in|founded in"
    r")\b",
    re.I,
)

#: A four-digit year in the question is a strong recency signal when it is
#: near the present, and a strong STATIC signal when it is clearly historical.
#: Markers that settle the question on their own, whatever else matches.
_STRONG_RECENT = re.compile(
    r"\b(latest|newest|most recent|current|currently|up[- ]to[- ]date|"
    r"as of today|right now)\b",
    re.I,
)

_YEAR = re.compile(r"\b(19|20)\d{2}\b")


def _deterministic(question: str, now_year: int) -> Optional[Verdict]:
    """A confident answer, or None when the model should decide."""
    q = (question or "").strip()
    if not q:
        return Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], "empty")

    if _REALTIME.search(q):
        return Verdict(Freshness.REALTIME, _MAX_AGE[Freshness.REALTIME], "lexical:realtime")

    # An office-holder question is RECENT even phrased as "who is the ...",
    # which the STATIC pattern would otherwise claim.
    if _OFFICE.search(q):
        return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "lexical:office")

    years = [int(m.group(0)) for m in _YEAR.finditer(q)]
    if years:
        newest = max(years)
        if newest >= now_year - 1:
            return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], f"year:{newest}")
        if newest <= now_year - 3:
            # "the 2019 election" is history; it is not going to change.
            return Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], f"year:{newest}")

    # "latest/newest/current" is decisive: it outranks the generic timeless
    # shapes ("what is ...") that would otherwise tie with it and push an
    # obviously time-sensitive question to the router.
    if _STRONG_RECENT.search(q):
        return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "lexical:recent")

    static_hit = _STATIC.search(q)
    recent_hit = _RECENT.search(q)
    if static_hit and not recent_hit:
        return Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], "lexical:static")
    if recent_hit and not static_hit:
        return Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "lexical:recent")

    # Both fired, or neither did — genuinely ambiguous, so ask the router.
    return None


_ROUTER_SYSTEM = (
    "Classify how time-sensitive a question is. Answer with EXACTLY one word:\n"
    "STATIC — the true answer does not change (science, definitions, history, "
    "how things work).\n"
    "RECENT — the answer changes over months (who currently holds an office or "
    "job, latest software version, current prices of slow-moving things).\n"
    "REALTIME — the answer changes hourly (live scores, market prices, weather, "
    "breaking news).\n"
    "Answer with the single word only."
)

_WORD = {
    "static": Freshness.STATIC,
    "recent": Freshness.RECENT,
    "realtime": Freshness.REALTIME,
}


async def _ask_router(question: str) -> Optional[Verdict]:
    """One tiny non-thinking call on the 8B. None on any failure."""
    try:
        from . import llm

        reply = await llm.router_chat_completion(
            [
                {"role": "system", "content": _ROUTER_SYSTEM},
                {"role": "user", "content": question[:400]},
            ],
            temperature=0.0,
            max_tokens=4,
        )
    except Exception:  # noqa: BLE001 — classification must never cost the answer
        log.debug("freshness router unavailable", exc_info=True)
        return None
    word = (reply or "").strip().lower().strip(".").split()
    if not word:
        return None
    level = _WORD.get(word[0])
    if level is None:
        return None
    return Verdict(level, _MAX_AGE[level], "router")


def classify_offline(question: str, *, now_year: int) -> Verdict:
    """The deterministic verdict, with the ambiguous case settled as RECENT.

    For callers that must never wait on a model to decide how to treat time
    (Deep Research, which already plans with the main model and only needs
    the level to weight recency): the same regex pass as `classify`, minus
    the router round trip, minus the possibility of blocking on it.
    """
    verdict = _deterministic(question, now_year)
    if verdict is not None:
        return _with_volatility(verdict, question)
    return _with_volatility(
        Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "default"), question
    )


#: Shapes whose answer moves within days. A page inside the RECENT window
#: (14 d) that answers "latest vLLM release" can be three releases old.
_VOLATILE = re.compile(
    r"\b(release[sd]?|version|changelog|price[sd]?|pricing|stock|score[sd]?|"
    r"rate[sd]?|schedule|status)\b",
    re.I,
)
#: How old a stored page may be for a volatile question before it is worth a
#: lookup: a day, not two weeks.
VOLATILE_MAX_AGE_S = 24 * 3600
#: How long the freshness router may take before the deterministic default
#: stands. It shares the 8B model with fact extraction, titling and query
#: rewriting; under load its queue must not become Fast's time to first token.
ROUTER_DEADLINE_S = 0.6


def _with_volatility(verdict: Verdict, question: str) -> Verdict:
    q = question or ""
    volatile = verdict.requirement is Freshness.REALTIME or bool(
        _STRONG_RECENT.search(q) or _VOLATILE.search(q)
    )
    if not volatile or verdict.requirement is Freshness.STATIC:
        return verdict
    return Verdict(
        verdict.requirement,
        min(verdict.max_age_seconds, VOLATILE_MAX_AGE_S),
        verdict.reason,
        volatile=True,
    )


#: The reason a Fast turn is settled STATIC without the router: the question
#: is a timeless TASK (small talk, creative writing, a transformation of text
#: the person supplied, a coding or maths exercise) with no live-value signal
#: anywhere in it (see `clearly_timeless`). Its own rule, so a
#: misclassification it causes is countable in
#: techsara_freshness_classified_total instead of hiding under "router".
TIMELESS_TASK_REASON = "timeless_task"

# --------------------------------------------------------------------------
# Fast effort's router skip (performance plan item 2, revised 2026-09-13).
#
# The first version skipped the router for any undecided question without a
# _RECENT word. The prover measured what that cost: "euro to dollar", "is AWS
# down", "Tesla share value" and "score of india vs australia" went from a
# RECENT router verdict plus the Fast live lookup (4/4 on HEAD) to STATIC
# answers from 2024 weights with no staleness note (0/4). No recency word is
# not the same as timeless: live values are asked about with nouns ("rate",
# "score", "down", "out"), currency pairs and product names, not with "now".
#
# So the skip is inverted into an ALLOWLIST. A question skips the router only
# when it positively reads as a timeless task AND no live-value signal fires.
# Everything else — including every question this code has no opinion on —
# goes to the router exactly as before, bounded by ROUTER_DEADLINE_S with the
# RECENT default on timeout. A wrong skip answers a live question from
# weights; a wrong route costs one router call (mean 0.216 s, p95 0.483 s,
# measured 2026-09-13), so every doubt resolves to the router.
#
# The skip itself is OPT-IN since 2026-09-14 (FRESHNESS_FAST_SKIP_ROUTER,
# default false): the vetoes below are still word lists, and the re-prover's
# 50 new live-value questions in timeless-task shapes skipped the router on 13
# ("write a poem for our cji", "write an email to airtel about their unlimited
# plan", "tell me a joke about elon musk"). With the default every Fast
# question the regex pass leaves undecided asks the router, as on HEAD.
# --------------------------------------------------------------------------

#: A message that is ONLY small talk. Anchored at both ends: "hi, is AWS down"
#: must not ride through on its greeting.
_SMALL_TALK = re.compile(
    r"^\W*(?:(?:hi|hello|hey|hiya|yo|greetings|good (?:morning|afternoon|evening|night)|"
    r"thanks|thank you|thx|ty|ok(?:ay)?|cool|great|nice|awesome|bye|goodbye|see you|"
    r"how are you(?: doing)?|how(?:'s| is) it going|what'?s up|sup|nice to meet you|"
    r"there|again|so much|very much|a lot)[\s,!.?]*)+$",
    re.I,
)

#: Programming and human languages, which are capitalised mid-sentence in the
#: most ordinary timeless task ("how do I reverse a list in Python", "translate
#: this into French") and are not live entities.
_LANGUAGE_NAMES = (
    r"python|javascript|typescript|java|kotlin|swift|rust|go|golang|ruby|php|perl|"
    r"scala|haskell|elixir|erlang|clojure|lua|dart|julia|matlab|fortran|cobol|"
    r"c|c\+\+|c#|bash|shell|powershell|sql|postgres|postgresql|mysql|sqlite|html|css|"
    r"json|yaml|xml|csv|regex|excel|latex|markdown|linux|unix|"
    r"english|french|spanish|german|italian|portuguese|dutch|russian|chinese|"
    r"mandarin|cantonese|japanese|korean|arabic|hindi|gujarati|marathi|bengali|"
    r"tamil|telugu|kannada|malayalam|punjabi|urdu|turkish|greek|latin|hebrew|"
    r"persian|polish|swedish|norwegian|danish|finnish|thai|vietnamese|indonesian|swahili"
)

#: Timeless tasks, anchored at the start after an optional polite lead-in.
_TIMELESS_TASK = re.compile(
    r"^\W*(?:(?:please|pls|kindly|ok(?:ay)?|so|hey|hi|hello)[\s,!.]+)*"
    r"(?:(?:can|could|would|will) you\s+(?:please\s+)?|i (?:want|need) you to\s+|help me\s+)?"
    r"(?:"
    # Creative writing.
    r"(?:write|compose|draft|create|generate|make(?: up)?|give me|come up with)\s+"
    r"(?:me\s+|us\s+)?(?:a|an|one|some|two|three|\d+|another|the)?\s*"
    r"(?:(?:short|long|little|funny|cute|sad|happy|romantic|simple|rhyming|silly|"
    r"heartfelt|formal|polite|professional|casual|creative|original|bedtime|"
    r"birthday|cover|thank[- ]you|love|wedding|farewell|condolence|apology|"
    r"resignation|welcome|anniversary|motivational|inspirational)\s+)*"
    r"(?:poem|poems|haiku|haikus|limerick|limericks|sonnet|verse|rhyme|story|stories|"
    r"tale|fable|song|lyrics|rap|joke|jokes|pun|puns|riddle|riddles|essay|letter|"
    r"email|note|toast|speech|wish|wishes|greeting|message|caption|slogan|tagline|"
    r"dialogue|script|scene|character|plot|metaphor|acrostic)\b"
    r"|tell me (?:a|another|one more|some)\s+(?:(?:short|funny|good|bad|dad|silly|"
    r"scary|bedtime)\s+)*(?:joke|jokes|story|riddle|pun|poem)\b"
    # Brainstorming that needs no facts.
    r"|(?:suggest|brainstorm|give me|list)\s+(?:me\s+)?(?:some|a few|\d+)?\s*"
    r"(?:(?:cute|funny|creative|good|catchy|unique)\s+)*"
    r"(?:names|nicknames|titles|slogans|taglines|captions|pickup lines|puns|rhymes|"
    r"synonyms|antonyms|words)\b"
    # Transforming text the person supplied.
    r"|(?:translate|rephrase|paraphrase|reword|rewrite|re-write|proofread|"
    r"spell ?check|polish|shorten|expand|simplify|summari[sz]e|tl;?dr|"
    r"correct|fix)\s+(?:this|these|that|my|the following|the text|the sentence|"
    r"the paragraph|the grammar|the spelling|it|below|following|"
    r"the plot of|the story of)\b"
    r"|translate\s+['\"]"
    r"|make (?:this|it|my \w+) (?:sound )?(?:more |less )?"
    r"(?:formal|informal|polite|professional|casual|friendly|concise|shorter|longer|"
    r"clearer|simpler|better)\b"
    # Coding exercises.
    r"|(?:write|create|generate|give me|show me)\s+(?:me\s+)?(?:a|an|the|some)?\s*"
    rf"(?:(?:{_LANGUAGE_NAMES}|simple|recursive|basic|small)\s+)*"
    r"(?:function|method|class|script|program|snippet|regex|regular expression|"
    r"query|loop|unit tests?|test case|algorithm|code)\b"
    r"|(?:fix|debug|refactor|optimi[sz]e|review|comment|document|explain)\s+"
    r"(?:this|my|the following|the)\s+(?:code|function|script|query|regex|snippet|"
    r"program|class|method|loop|error|bug|stack ?trace)\b"
    rf"|how (?:do|can|would|should|to) (?:i|you|we|one)?\s*.+\s(?:in|using|with) (?:{_LANGUAGE_NAMES})\W*$"
    # Maths exercises.
    r"|(?:solve|simplify|factori[sz]e|factor|integrate|differentiate|derive|prove|"
    r"evaluate)\b"
    r")",
    re.I,
)

#: Anything that could make the answer a LIVE value. Deliberately wide: a hit
#: here only costs a router call. Grouped by the kind of question it protects.
#: Words that are overwhelmingly code vocabulary inside a timeless task ("the
#: largest number", "update rows", "convert celsius") were left out on
#: purpose; their live senses are caught by a neighbour in the same group
#: (a currency name, a product, a state question).
_LIVE_SIGNAL = re.compile(
    r"\b(?:"
    # Money and markets — prices, rates, currency pairs ("euro to dollar").
    r"price[sd]?|pricing|costs?|fees?|rates?|valuation|worth|net worth|"
    r"market|markets|stocks?|shares?|ticker|trading|crypto|bitcoin|btc|ethereum|"
    r"dogecoin|solana|forex|currenc(?:y|ies)|exchange|inflation|interest|gdp|ipo|"
    r"earnings|revenue|profits?|dividends?|salary|salaries|wages?|tax|taxes|loan|"
    r"emi|mortgage|budget|cheap|cheapest|expensive|afford(?:able)?|deals|best deal|"
    r"discount|sale|buy|sell|gold|silver|oil|petrol|diesel|"
    r"dollars?|usd|euros?|eur|pounds?|gbp|sterling|yen|jpy|yuan|cny|renminbi|"
    r"rupees?|inr|aud|cad|chf|francs?|pesos?|won(?!'t)|rubles?|roubles?|dirhams?|"
    r"aed|riyals?|lira|baht|ringgit|sgd|hkd|nzd|"
    # Sport, contests, rankings, benchmarks ("score of india vs australia").
    r"scores?|scored|results?|(?<!to )(?<!that )(?<!which )(?<!will )match(?:es)?|"
    r"fixtures?|standings|points table|league|tournament|world cup|finals|the final|"
    r"semi-?finals?|playoffs?|vs|versus|beat|beats|won|wins?|winners?|lose|lost|"
    r"leading|lineup|squad|roster|transfers?|ranking|rankings|ranked|leaderboard|"
    r"benchmarks?|elo|ratings?|top(?: \d+| ten| five| three|-rated| rated| selling)|"
    r"best|worst|richest|most popular|most valuable|world record|all-time high|"
    r"ipl|nba|nfl|fifa|uefa|f1|formula 1|cricket|football|soccer|tennis|"
    # Status, outages, schedules ("is AWS down" is also a state question below).
    r"outages?|downtime|offline|status|incident|in stock|sold out|delay(?:ed)?|"
    r"traffic|flights?|trains?|schedule|timetable|opening hours|business hours|"
    r"weather|forecast|aqi|pollution|"
    # Releases, versions, products ("upcoming iPhone release date").
    r"release[sd]?|releasing|launch(?:ed|es|ing)?|upcoming|coming out|coming soon|"
    r"out yet|announce[sd]?|unveil(?:ed)?|leaks?|rumou?rs?|eta|beta|preview|specs|"
    r"specifications|model|models|install|upgrade|"
    r"iphone|ipad|pixel|galaxy|android|ios|"
    r"gpt|chatgpt|openai|anthropic|claude|gemini|llama|mistral|deepseek|qwen|grok|"
    r"vllm|nvidia|gpu|gpus|tesla|google|microsoft|amazon|aws|azure|spacex|starlink|"
    r"twitter|x\.com|"
    # News, people, public statements, events, rules ("what did Sam Altman say").
    r"news|headlines?|said|says|tweet(?:ed|s)?|posted|statement|interview|"
    r"reacted|responded|controversy|scandal|lawsuit|sued|arrested|died|dead|alive|"
    r"death|married|divorced?|pregnant|elections?|polls?|votes?|voting|war|attack|"
    r"ceasefire|sanctions|protests?|strike|policy|policies|law|laws|bill|"
    r"regulations?|ban|banned|visa|deadline|population|subscribers|followers|"
    r"tomorrow|yesterday|tonight|soon|anymore|ago|"
    r"new(?! (?:line|lines|list|array|file|files|object|instance|string|dict|"
    r"dictionary|row|rows|column|columns|branch|folder|directory|variable|tab|"
    r"paragraph|sentence|word|words|node|element|item|key|value|table|user|class))|"
    r"(?:next|last|this|coming) (?:week|weekend|month|year|season|match|game|"
    r"election|event|night|quarter|release|version|update)"
    r")\b"
    # Lookups of a person, organisation or event by name.
    r"|\b(?:who(?:'s| is| are| was| were)|whos|tell me about|what happened|"
    r"what'?s happening|when (?:is|does|will|was|are|did)|when'?s|where (?:is|are)|"
    r"how much|how old|still (?:alive|open|available|working|running|in))\b"
    # A yes/no about a state: "is the vLLM 0.12 out", "is AWS down",
    # "are the banks open".
    # Bounded to one clause of 120 characters: an unbounded `.*` here took
    # 81.5 s on a 140,000-character paste (measured 2026-09-13).
    r"|\b(?:is|are|was|were|has|have|did|does)\b[^.?!\n]{0,120}?\b(?:out|up|live|back|down|open|"
    r"closed|working|broken|available|released|over|cancel(?:l)?ed|delayed)\b",
    re.I,
)

#: A version or model token: o5, gpt-5.2, iphone17, h100, rtx5090, v2, 0.12.
_VERSIONISH = re.compile(r"\b[a-z]+-?\d+(?:\.\d+)*[a-z]*\b|\b\d+\.\d+\b", re.I)

#: A capitalised word that does not start a sentence: a name ("Sam Altman",
#: "Tesla") when the person typed one in its case. Languages are exempt.
_MID_SENTENCE_CAPITAL = re.compile(r"(?<![.!?:;]\s)(?<!^)(?<![\"'(\[])\b[A-Z][\w'-]*")
_LANGUAGE_WORD = re.compile(rf"^(?:{_LANGUAGE_NAMES}|i|i'm|i've|i'll|i'd|ok|okay)$", re.I)


def _names_something(question: str) -> bool:
    for m in _MID_SENTENCE_CAPITAL.finditer(question.strip()):
        if not _LANGUAGE_WORD.match(m.group(0)):
            return True
    return False


def router_would_be_asked(question: str, *, now_year: int) -> bool:
    """True when `classify` would consult the router for this question: the
    deterministic pass could not decide. Lets a caller start work that does
    not depend on the answer BEFORE the round trip, instead of after it."""
    return _deterministic(question, now_year) is None


# --------------------------------------------------------------------------
# The OBJECT of a timeless task (second prover pass, 2026-09-13).
#
# The noun veto above is a list, and a list leaks. The prover wrapped 20
# live values in timeless shapes with no word from it — "write a short poem
# congratulating the chief justice of india", "draft a letter to the pope",
# "write an email to my accountant about the gst on laptops", "write a toast
# for the reigning miss universe", "give me some slogans for the ruling party
# in bihar" — and 15 of 20 skipped the router and the Fast live lookup
# (HEAD: 20/20 attempted). What they share is structural: the task is ABOUT
# a definite thing in the world ("the pope", "india's chess world champion")
# or a role, record, rate or incumbency whose holder changes. So:
#   - a definite object ("the <noun>") vetoes, unless the noun is text the
#     person supplied ("the following", "the paragraph"), code ("the
#     function"), a timeless setting ("the ocean", "the night") or a maths
#     object ("the sum", "the largest number");
#   - a possessive of anything but a pronoun or a relative vetoes
#     ("india's", "apple's"; not "my mom's");
#   - a role, office, title, record, tax or incumbency word vetoes wherever
#     it stands ("a poem for chief justice gavai").
# Each costs one router call on a timeless task (mean 0.216 s); a leak costs
# an answer from 2024 weights.
# --------------------------------------------------------------------------

_DEFINITE_OBJECT = re.compile(
    r"\bthe\s+(?!(?:"
    # Text the person supplied, and the parts of it a rewrite talks about.
    r"following|above|below|text|sentence|sentences|paragraph|paragraphs|passage|grammar|spelling|"
    r"punctuation|tone|wording|same|letter|email|message|note|poem|story|essay|draft|"
    r"plot of|story of|"
    # Code.
    r"code|function|script|query|regex|snippet|program|class|method|loop|error|bug|stack ?trace|output|"
    r"input|file|list|array|string|word|words|"
    # Timeless settings of a creative task.
    r"moon|sun|sky|stars|sea|ocean|rain|wind|night|morning|evening|forest|mountains?|river|beach|snow|"
    r"seasons?|autumn|fall|winter|spring|summer|future|past|end|beginning|"
    # Maths objects.
    r"(?:largest|smallest|biggest|longest|shortest|highest|lowest)\s+(?:number|element|value|integer|word)|"
    r"first|second|third|nth|kth|number|numbers|sum|average|mean|median|product|difference|square|cube|"
    r"factorial|area|volume|perimeter|derivative|integral|roots?|equation"
    r")\b)\w",
    re.I,
)

#: A possessive of a name or a thing in the world. Pronouns, contractions and
#: the people a personal note is usually for are not.
_POSSESSIVE = re.compile(
    r"\b(?!(?:it|that|what|let|here|there|he|she|who|where|how|when|why|one|mom|mum|dad|mother|father|"
    r"wife|husband|son|daughter|brother|sister|friend|boss|cat|dog|baby|kid|child|teacher|grandma|"
    r"grandpa|grandmother|grandfather|partner|girlfriend|boyfriend|fiance|fiancee|aunt|uncle|cousin|"
    r"neighbou?r|colleague|coworker|team|company|everyone|someone|nobody|anyone)['’]s\b)[a-z0-9]+['’]s\b",
    re.I,
)

#: Roles, offices, titles, records, taxes and incumbency: a holder or a value
#: that changes. Superlatives that are maths vocabulary ("the largest number
#: in a list") are left to the definite-object rule's exemption.
_ROLE_RECORD_OR_RATE = re.compile(
    r"\b(?:"
    r"president|presidents|vice[- ]president|prime minister|ministers?|premier|chancellor|governor|mayor|"
    r"senators?|congress(?:man|woman)|mps?|mlas?|meps?|ceo|cfo|coo|cto|chairman|chairwoman|chairperson|"
    r"chief|justice|judges?|pope|speaker|secretary|ambassador|envoy|commissioner|attorney general|"
    r"monarch|dalai lama|head coach|"
    r"champions?|championship|titleholder|title holder|record holder|miss universe|miss world|miss india|"
    r"mvp|ballon d'or|oscars?|grammys?|emmys?|nobel|laureate|"
    r"tallest|richest|highest[- ]grossing|grossing|highest[- ]paid|best[- ]selling|bestsellers?|"
    r"most[- ](?:followed|watched|subscribed|streamed|downloaded|searched|visited)|"
    r"gst|vat|tariffs?|customs|duty|duties|levy|levies|cess|surcharges?|tolls?|fares?|premiums?|"
    r"subsid(?:y|ies)|lottery|quota|repo rate|"
    r"(?:ruling|opposition|political|governing) part(?:y|ies)|government|cabinet|parliament|congress|senate|"
    r"lok sabha|rajya sabha|"
    r"reigning|ruling|defending|incumbent|sitting|current|currently|serving|outgoing|newly|elected|"
    r"appointed|nominees?"
    r")\b",
    re.I,
)


#: A transformation whose text follows a colon, a quote or a line break
#: ("translate this paragraph: the cat sat on the mat"). The object rules
#: read only the instruction before it: "the cat" is the person's text, not a
#: thing in the world. The noun veto above still reads everything.
_SUPPLIED_TEXT_LEAD = re.compile(
    r"^\W*(?:(?:please|pls|kindly)\s+)?(?:(?:can|could|would) you\s+(?:please\s+)?)?"
    r"(?:translate|rephrase|paraphrase|reword|rewrite|re-write|proofread|spell ?check|polish|shorten|expand|"
    r"simplify|summari[sz]e|tl;?dr|correct|fix|improve)\b[^:\"'“‘\n]{0,60}[:\"'“‘\n]",
    re.I,
)


def _live_signal(question: str) -> bool:
    """Does anything in the question suggest its answer is a live value?"""
    q = question or ""
    if (
        _RECENT.search(q)
        or _YEAR.search(q)
        or _LIVE_SIGNAL.search(q)
        or _VERSIONISH.search(q)
        or _names_something(q)
    ):
        return True
    lead = _SUPPLIED_TEXT_LEAD.match(q)
    instruction = q[: lead.end()] if lead else q
    return bool(
        _DEFINITE_OBJECT.search(instruction)
        or _POSSESSIVE.search(instruction)
        or _ROLE_RECORD_OR_RATE.search(instruction)
    )


#: Longer messages are not skip candidates. A long paste is exactly where a
#: live value hides past the opening words, the router reads only the first
#: 400 characters anyway, and the `.*` alternatives above must never scan an
#: unbounded paste on the event loop (the composer has no size limit).
_SKIP_MAX_CHARS = 500


def clearly_timeless(question: str, *, now_year: int) -> bool:
    """Undecided by the regex pass, positively a timeless task, and not one
    live-value signal in it. The ONLY questions Fast may settle without the
    router, and only while FRESHNESS_FAST_SKIP_ROUTER is on (default off);
    see the block comment above for why the default is to ask."""
    q = (question or "").strip()
    if not q or len(q) > _SKIP_MAX_CHARS or not router_would_be_asked(q, now_year=now_year):
        return False
    if _live_signal(q):
        return False
    return bool(_SMALL_TALK.match(q) or _TIMELESS_TASK.match(q))


def static_timeless_task() -> Verdict:
    """The verdict a Fast turn gets when `clearly_timeless` holds."""
    return Verdict(Freshness.STATIC, _MAX_AGE[Freshness.STATIC], TIMELESS_TASK_REASON)


async def classify(question: str, *, now_year: int, allow_router: bool = True) -> Verdict:
    """How fresh must the evidence behind this answer be?

    `now_year` is passed in rather than read here so callers share ONE notion
    of "now" for a request — the same value that goes into the prompt.
    """
    verdict = _deterministic(question, now_year)
    if verdict is not None:
        return _with_volatility(verdict, question)
    if allow_router:
        started = time.perf_counter()
        asked: Optional[Verdict] = None
        try:
            async with asyncio.timeout(ROUTER_DEADLINE_S):
                asked = await _ask_router(question)
            metrics.observe("freshness_router_seconds", time.perf_counter() - started, outcome="ok")
        except TimeoutError:
            metrics.observe("freshness_router_seconds", time.perf_counter() - started, outcome="timeout")
        except Exception:  # noqa: BLE001 — the default below is the fallback
            metrics.observe("freshness_router_seconds", time.perf_counter() - started, outcome="error")
        if asked is not None:
            return _with_volatility(asked, question)
    # Unclassifiable and no router: treat as RECENT. The failure this module
    # exists to prevent is answering a live question from stale weights, so an
    # unnecessary cache lookup is the cheaper mistake than a wrong fact.
    return _with_volatility(
        Verdict(Freshness.RECENT, _MAX_AGE[Freshness.RECENT], "default"), question
    )
