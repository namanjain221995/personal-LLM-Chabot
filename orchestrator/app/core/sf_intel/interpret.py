"""Read the request the way a colleague would, BEFORE deciding to ask about it.

Two jobs, both deterministic and both upstream of the planner:

  1. SPELLING. Every grounding layer in this codebase matches words exactly —
     `brain._trigger_hits`, `org_brief.domain_rules_for`, `match_metrics`,
     `TABLE_ALIASES`. So "how many advance mock scheddule todau?" reaches the
     planner with no internal-interview rules, no metric definition and no
     detected period, and a planner given nothing asks a question about
     everything. The words are repaired against the org's OWN vocabulary —
     pack triggers, glossary terms, table names, metric aliases — so nobody
     maintains a list of misspellings and a new pack teaches new spellings for
     free.

  2. WHAT THE REQUEST ALREADY SAYS. "How many mocks today?" names its period.
     Asking "over what period?" about it is not care, it is an interrogation,
     and it is the single most common way a clarifying assistant becomes
     worse than a silent one. `satisfied_slots` is the deterministic floor
     under the planner's judgement: a slot the sentence already pins down is
     never a slot we ask about.

Nothing here rewrites what the user said. The repaired text is used for
MATCHING and shown to the planner as a reading; `original_user_text` — what
gets stored, resumed and displayed — is untouched. A normalizer that silently
edited the question would make every downstream answer describe a request the
user never made.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Sequence, Tuple

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")

#: Words never treated as misspellings, however close they sit to a domain
#: term. Ordinary English carries the sentence around the domain nouns, and a
#: repair that "fixes" a correct word is far more damaging than a missed typo:
#: it changes what the question means.
_SAFE_WORDS: FrozenSet[str] = frozenset(
    """
    about above across after again against all also and any are been before
    being below between both but can cannot come could did does doing done
    down during each else even ever every few for from further get give had
    has have having her here hers him his how into its itself just like made
    make many may me might more most much must my need needs new next no nor
    not now off often once one only onto other our ours out over own per
    please put same say see seen several she should show shown since some
    still such take tell than that the their theirs them then there these
    they thing things this those through too under until upon use used using
    very was way well were what when where which while who whom whose why
    will with within without would yes you your yours
    add added also amount amounts area areas average back best big break call
    called case cases change changed check compare compared count counted
    counts current data date dates day days detail details different display
    each end ends entry field fields file files filter filtered find first
    full give given group grouped high hour hours id ids include included
    info information item items keep kind kinds know large last late later
    least less level line lines list listed long look low main mean means
    month months name names near number numbers old open order part parts
    past percent period periods person place point points range ranges rate
    rates reason record records report reports result results right row rows
    run same search see set sets show side single size small sort sorted
    split start started state states status stats sum summary table tables
    team teams term terms text time times top total totals type types unit
    units up update updated user users value values view week weeks work
    working year years
    """.split()
)

#: Question words and time words the deterministic layers below depend on.
#: They are in the vocabulary as repair TARGETS, so "todau" becomes "today"
#: even in an org whose packs happen never to mention a date.
_CORE_VOCABULARY: FrozenSet[str] = frozenset(
    """
    today tomorrow yesterday tonight week weekly month monthly quarter
    quarterly year yearly annual daily morning afternoon evening now current
    currently recent recently upcoming
    january february march april may june july august september october
    november december
    monday tuesday wednesday thursday friday saturday sunday weekend
    schedule scheduled scheduling booked booking cancelled canceled completed
    complete pending upcoming attended absent passed failed cleared rejected
    selected shortlisted
    candidate candidates recruiter recruiters interviewer interviewers
    trainer trainers manager managers owner owners employee employees
    student students
    interview interviews mock mocks assessment assessments training trainings
    session sessions slot slots cohort batch module modules
    advanced basic beginner intermediate final initial internal external
    client
    count number total average percentage breakdown compare comparison trend
    """.split()
)

#: Slots `satisfied_slots` can rule out. Deliberately short: these are the
#: readings a SENTENCE can settle on its own. `metric` is not here — "how
#: many advanced mocks today" genuinely does not say whether it means
#: interviews or the candidates who sit them, and that is the one question in
#: this domain actually worth asking. Nor is `object`, whose whole difficulty
#: is that one English word ("interview") names two Salesforce objects.
_SATISFIABLE = ("date_range", "owner_scope", "region", "status", "result_format", "grouping")

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)

#: A period the sentence states. Broader than the resume classifier's version
#: on purpose — this one decides whether to ASK about the period, so a missed
#: expression costs the user a needless question. "Tomorrow" was missing from
#: the resume shapes entirely, which is why "how many mocks tomorrow?" could
#: still be met with "over what period?".
_DATE_RE = re.compile(
    r"\b("
    r"today|tomorrow|yesterday|tonight|now|currently|right now|"
    r"(?:this|last|next|past|previous|coming|current)\s+"
    r"(?:week|month|quarter|year|fortnight|day|weekend|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday)|"
    r"(?:last|past|next|previous|coming)\s+\d+\s+"
    r"(?:day|days|week|weeks|month|months|quarter|quarters|year|years)|"
    r"ytd|mtd|qtd|q[1-4]|fy\s?\d{2,4}|all\s?time|ever|to date|so far|"
    r"since\s+\S+|between\s+.+\s+and\s+\S+|"
    r"(?:" + _MONTHS + r")\s+\d{1,4}|\d{1,2}\s+(?:" + _MONTHS + r")|"
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|"
    r"(?:in|during|for)\s+(?:19|20)\d{2}"
    r")\b",
    re.I,
)

#: Whose records. Bare "me" is deliberately absent: "show me the top accounts"
#: is not a request scoped to the asker, and reading it as one suppressed the
#: one question that request actually needed.
_OWNER_RE = re.compile(
    r"\b(my|mine|i own|(?:for|to|by|assigned to|owned by)\s+me|my team|"
    r"our team|everyone|everybody|all users|all recruiters|the team|"
    r"whole team|unassigned|nobody|each recruiter|per recruiter|by recruiter|"
    r"by owner|each owner)\b",
    re.I,
)

#: "Divya's mocks", "mocks for Divya" — a named person IS the owner scope.
#: Which Divya is a `record_identity` question, and this deliberately does not
#: answer that one.
_PERSON_RE = re.compile(
    r"\b([A-Z][a-z]{2,})(?:'s|’s)\b|\b(?:for|of|by|assigned to|owned by|with)\s+"
    r"([A-Z][a-z]{2,})\b"
)

_REGION_RE = re.compile(
    r"\b(emea|apac|amer|americas|north america|latam|uk|europe|asia|india|"
    r"usa?|global|all regions|worldwide|every region|onshore|offshore)\b",
    re.I,
)

_STATUS_RE = re.compile(
    r"\b(open|closed|closed won|closed lost|won|lost|active|inactive|pending|"
    r"in progress|new|qualified|any status|all statuses|every status|"
    r"scheduled|unscheduled|completed|cancelled|canceled|rescheduled|"
    r"passed|failed|cleared|dropped|escalated)\b",
    re.I,
)

_FORMAT_RE = re.compile(
    r"\b(chart|graph|plot|table|list|count|how many|total|number of|summary|"
    r"breakdown|report|export|csv)\b",
    re.I,
)

_GROUPING_RE = re.compile(
    r"\b(by \w+|per \w+|grouped by|group by|split by|broken down by|"
    r"no grouping|don'?t group|each \w+)\b",
    re.I,
)

_SLOT_PATTERNS = {
    "date_range": _DATE_RE,
    "owner_scope": _OWNER_RE,
    "region": _REGION_RE,
    "status": _STATUS_RE,
    "result_format": _FORMAT_RE,
    "grouping": _GROUPING_RE,
}


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

_ORG_VOCAB_CACHE: Optional[Tuple[int, FrozenSet[str]]] = None


def _org_vocabulary() -> FrozenSet[str]:
    """The hand-authored half of the vocabulary: org_brief's own words.

    Read from the module's public tables rather than duplicated here, so a
    metric or alias added there is a spelling this understands immediately.
    """
    global _ORG_VOCAB_CACHE
    from .. import org_brief

    identity = len(org_brief.METRICS) + len(org_brief.TABLE_ALIASES)
    if _ORG_VOCAB_CACHE and _ORG_VOCAB_CACHE[0] == identity:
        return _ORG_VOCAB_CACHE[1]
    words: set = set()

    def add(raw: str) -> None:
        for word in _WORD_RE.findall(str(raw or "").lower()):
            if len(word) >= 3:
                words.add(word)

    for domain in org_brief.DOMAIN_RULES:
        for trigger in domain.get("triggers", ()):
            add(trigger)
    for metric in org_brief.METRICS:
        add(metric.get("name", ""))
        for alias in metric.get("aliases") or ():
            add(alias)
    for alias in org_brief.TABLE_ALIASES:
        add(alias)
    _ORG_VOCAB_CACHE = (identity, frozenset(words))
    return _ORG_VOCAB_CACHE[1]


def vocabulary() -> FrozenSet[str]:
    """Every word this org is known to use, for spelling repair."""
    from .. import brain

    return frozenset(_CORE_VOCABULARY | _org_vocabulary() | brain.vocabulary())


# ---------------------------------------------------------------------------
# Spelling
# ---------------------------------------------------------------------------

def _within(a: str, b: str, budget: int) -> Optional[int]:
    """Optimal string alignment distance, or None once it exceeds `budget`.

    Damerau rather than plain Levenshtein because ADJACENT TRANSPOSITION is one
    of the commonest ways a word gets mistyped, and Levenshtein charges two
    edits for it: "todya" is one swap from "today" and two substitutions, so a
    budget generous enough to catch it under Levenshtein is generous enough to
    turn unrelated five-letter words into each other.

    Bounded because the vocabulary is hundreds of words and this runs per token
    per request: an unbounded matrix over every candidate would be the most
    expensive thing in the pipeline, for a job that only ever cares about
    distances of one or two.
    """
    if abs(len(a) - len(b)) > budget:
        return None
    before_previous: List[int] = []
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            )
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cost = min(cost, before_previous[j - 2] + 1)
            current.append(cost)
        if min(current) > budget:
            return None
        before_previous, previous = previous, current
    return previous[-1] if previous[-1] <= budget else None


def _budget(word: str) -> int:
    """How wrong a word may be before a repair stops being a repair."""
    if len(word) <= 3:
        return 0
    if len(word) <= 6:
        return 1
    return 2


def _stem(word: str) -> str:
    """Crude singularisation; the same rule brain, org_brief and sf_dictionary use."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _repair(word: str, vocab: FrozenSet[str]) -> Optional[str]:
    """The word this is a misspelling of, or None to leave it alone.

    Four guards, each of which removes a whole class of false corrections:

      * the first letter must match — people mistype the middle of a word, and
        without this "leads" and "reads" are one edit apart;
      * the match must be UNIQUE at the best distance — "statis" sits one edit
        from both "status" and "static", and guessing between them is exactly
        the silent reinterpretation this module exists to avoid;
      * an INFLECTION is not a misspelling. "offers" sits one edit from the
        vocabulary's "offer" and "scores" from "score", so without this the
        repairs list filled up with plural-to-singular rewrites that changed
        nothing: every matcher downstream stems its input already. A repair the
        reader would not recognise as a correction is noise in the one place
        that has to stay legible — the reading shown to the planner;
      * a word already in the vocabulary, or in ordinary English, is never
        touched.
    """
    budget = _budget(word)
    if not budget:
        return None
    stem = _stem(word)
    best: Optional[str] = None
    best_distance = budget + 1
    ties = 0
    for candidate in vocab:
        if candidate[0] != word[0] or abs(len(candidate) - len(word)) > budget:
            continue
        if _stem(candidate) == stem:
            continue  # the same word, differently inflected
        distance = _within(word, candidate, budget)
        if distance is None or distance == 0:
            continue
        if distance < best_distance:
            best, best_distance, ties = candidate, distance, 1
        elif distance == best_distance and candidate != best:
            ties += 1
    return best if ties == 1 else None


def normalize(text: str) -> Tuple[str, Tuple[Tuple[str, str], ...]]:
    """→ (text with domain words spelled as this org spells them, repairs).

    Capitalised words mid-sentence are skipped: they are overwhelmingly names,
    and "Divya" is not a misspelling of anything.
    """
    source = text or ""
    if not source.strip():
        return source, ()
    vocab = vocabulary()
    if not vocab:
        return source, ()

    repairs: List[Tuple[str, str]] = []
    seen: Dict[str, Optional[str]] = {}
    first = True

    def replace(match: "re.Match[str]") -> str:
        nonlocal first
        raw = match.group(0)
        was_first, first = first, False
        lowered = raw.lower()
        if lowered in _SAFE_WORDS or lowered in vocab or len(lowered) < 4:
            return raw
        if not was_first and raw[0].isupper():
            return raw  # a name, not a typo
        if lowered not in seen:
            seen[lowered] = _repair(lowered, vocab)
        fixed = seen[lowered]
        if not fixed:
            return raw
        repairs.append((raw, fixed))
        return fixed.upper() if raw.isupper() else fixed

    return _WORD_RE.sub(replace, source), tuple(repairs)


# ---------------------------------------------------------------------------
# What the request already settles
# ---------------------------------------------------------------------------

def satisfied_slots(text: str) -> FrozenSet[str]:
    """The slots this sentence already answers — never ask about these."""
    source = text or ""
    if not source.strip():
        return frozenset()
    found = {slot for slot in _SATISFIABLE if _SLOT_PATTERNS[slot].search(source)}
    if "owner_scope" not in found and _PERSON_RE.search(source):
        found.add("owner_scope")
    return frozenset(found)


# ---------------------------------------------------------------------------
# The reading
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Reading:
    """One deterministic pass over a request, shared by everything downstream."""

    original: str
    #: The same request with domain words spelled the org's way. Used for
    #: MATCHING (brain packs, metrics, slot detection) — never for storage.
    text: str
    repairs: Tuple[Tuple[str, str], ...] = ()
    satisfied: FrozenSet[str] = frozenset()

    @property
    def repaired(self) -> bool:
        return bool(self.repairs)

    def note(self) -> str:
        """A line for the planner prompt when the spelling needed work.

        Shown as a READING rather than a correction: the planner is told the
        user's own words are authoritative, so a wrong repair costs nothing but
        a line of prompt.
        """
        if not self.repairs:
            return ""
        pairs = ", ".join(f"{was!r} → {now!r}" for was, now in self.repairs[:8])
        return (
            f"Reading of the request after obvious spelling repairs: {self.text}\n"
            f"(repaired: {pairs}). Treat this as the intended meaning; the "
            "user's own wording above is authoritative if the two disagree."
        )


def read(text: str) -> Reading:
    """Normalize a request and record what it already settles."""
    normalized, repairs = normalize(text)
    return Reading(
        original=text or "",
        text=normalized,
        repairs=repairs,
        satisfied=satisfied_slots(normalized),
    )


# ---------------------------------------------------------------------------
# Grounding key
# ---------------------------------------------------------------------------

#: How much earlier context a follow-up may borrow when matching packs. Enough
#: for the subject to survive, small enough that a long transcript cannot drag
#: in every pack in the directory.
_CONTEXT_CHARS = 600


def grounding_text(
    reading: Reading,
    *,
    original_request: str = "",
    carried_slots: Optional[Dict[str, str]] = None,
    conversation_summary: str = "",
    recent_turns: Sequence[dict] = (),
) -> str:
    """The text the knowledge layers should match on, not just this turn's words.

    "Tomorrow?" contains no trigger for any pack, names no metric and hints at
    no table — so a follow-up arrives at the planner with none of the grounding
    its own first turn had, and the planner asks again what the conversation
    already knows. Matching against the SUBJECT plus the new words fixes that
    without changing what anybody is asked.

    Only USER turns are borrowed. An assistant turn is mostly the previous
    answer, and letting a paragraph of prose pull packs in would ground the
    follow-up in whatever the last answer happened to mention.
    """
    parts: List[str] = [reading.text]
    if original_request and original_request.strip() != reading.original.strip():
        parts.append(original_request)
    for slot, value in (carried_slots or {}).items():
        if value:
            parts.append(f"{slot.replace('_', ' ')} {value}")
    if conversation_summary:
        parts.append(conversation_summary)
    borrowed = 0
    for turn in reversed(list(recent_turns)):
        if turn.get("role") != "user":
            continue
        content = str(turn.get("content") or "").strip()
        if not content:
            continue
        parts.append(content[:_CONTEXT_CHARS])
        borrowed += len(content)
        if borrowed >= _CONTEXT_CHARS:
            break
    return "\n".join(p for p in parts if p)[: _CONTEXT_CHARS * 4]


# ---------------------------------------------------------------------------
# What the planner needs to know about this business
# ---------------------------------------------------------------------------

#: The planner's share of the org brief. Bounded independently of the SQL
#: engine's budget: this prompt already carries a schema summary and the
#: conversation state, and prefill latency on the 35B is roughly linear in
#: prompt size.
_KNOWLEDGE_CAP = 6000


def domain_knowledge(text: str) -> str:
    """What this org's own words mean, for the component that decides to ask.

    Deliberately NOT the same block the SQL engine gets. That one is about
    writing a correct query — canonical SQL, join traps, column spellings. This
    one is about understanding a sentence: the subject-area rules and the
    definitions of the measures the business names. The brain packs' rules and
    glossary reach the planner through the schema summary already
    (`tools.get_salesforce_schema`), so they are not repeated here.
    """
    from .. import org_brief

    blocks: List[str] = []
    lowered = " " + (text or "").lower() + " "
    for domain in org_brief.DOMAIN_RULES:
        if any(
            re.search(r"\b" + re.escape(word) + r"\b", lowered)
            for word in domain.get("triggers", ())
        ):
            blocks.append(str(domain.get("rules") or ""))

    definitions = []
    for metric in org_brief.match_metrics(text or ""):
        line = f'- "{metric["name"]}": {metric["definition"]}'
        if metric.get("caveat"):
            line += f" (must state: {metric['caveat']})"
        definitions.append(line)
    if definitions:
        blocks.append(
            "Canonical measures this request names — these fix the population "
            "and the denominator, so do not ask what they mean:\n"
            + "\n".join(definitions)
        )

    out: List[str] = []
    budget = _KNOWLEDGE_CAP
    for block in blocks:
        block = block.strip()
        if not block or len(block) > budget:
            continue
        budget -= len(block)
        out.append(block)
    return "\n\n".join(out)
