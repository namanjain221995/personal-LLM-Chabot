"""Does THIS prompt read as multi-step reasoning? A deterministic classifier.

NO RUNTIME CALLER since 2026-09-17. This module used to decide, per Fast turn,
whether the model should think anyway (PR #71, "adaptive thinking"); that
behaviour is gone. The owner's rule is that a turn asked for at Fast never
thinks, on any path, and it is enforced in `llm` for the whole turn
(`llm.mark_fast_turn`) rather than judged per prompt. The judgement was also
wrong often enough to matter: in production all four grants it opened were
false positives — a pasted job description scored as a "measurement" problem —
costing 11-47 s each.

`classify(text)` is kept because it is pure, cheap and labelled, and a future
round may want it for something honest (shaping a Fast prompt, or offering
"switch to Think"). It must not be wired back to `enable_thinking`.

`classify(text)` — a deterministic scorer over a small lexicon and a few
regular expressions, in English, Hindi (Latin and Devanagari), Gujarati
(script and Latin) and Hinglish. It fires on multi-step reasoning: puzzles,
maths and word problems, logic, measurement and ratio problems, code
debugging and tracing, "prove / derive / why exactly". It stays quiet on
greetings, chit-chat, factual lookups (numbers included: "what is 5G", "top
10 movies 2024", "iPhone 15 price"), summaries, translations, writing tasks
and live-value questions. Measured well under a millisecond per prompt; the
labelled set and its precision / false-positive gates live in
tests/test_effort_policy.py. Pure: no network, no model call, no heavy import.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

#: Only the head and the tail of a long message are scored. The question sits
#: at one end ("here is my code ... why does it hang?"); a pasted document is
#: not a puzzle because a number appears on page nine; and the scan stays
#: bounded (~1 ms per 1,000 characters on the worst input measured).
_HEAD_CHARS = 2_000
_TAIL_CHARS = 600

#: Score at which the prompt needs thinking. Strong signals weigh 3 on their
#: own; medium ones need company.
THRESHOLD = 3

#: Every reason `classify` can return — a closed set, so the metric label
#: built from it cannot grow (app/metrics.py).
REASONS = frozenset({
    "puzzle", "proof", "why_exactly", "combinatorics", "measurement",
    "equation", "sequence", "logic", "code_debug", "ratio",
    "word_problem", "arithmetic", "show_work",
    # decision=direct
    "no_signal", "veto_translate", "veto_summary", "veto_writing", "empty",
})


@dataclass(frozen=True)
class ThinkingDecision:
    """What the policy decided, and why (names only — never prompt text)."""

    think: bool
    reason: str
    score: int = 0
    signals: Tuple[str, ...] = field(default_factory=tuple)
    elapsed_ms: float = 0.0

    def as_trace(self) -> dict:
        return {
            "think": self.think,
            "reason": self.reason,
            "score": self.score,
            "signals": list(self.signals),
            "classify_ms": round(self.elapsed_ms, 3),
        }


def _rx(pattern: str) -> "re.Pattern[str]":
    return re.compile(pattern, re.IGNORECASE | re.UNICODE)


# ---------------------------------------------------------------------------
# Strong signals (weight 3): each one alone is a reasoning task.
# ---------------------------------------------------------------------------

_PUZZLE = _rx(
    r"\b(?:puzzles?|riddles?|brain\s{0,4}-?\s{0,4}teasers?|teaser|logic\s+problem|trick\s+question"
    r"|paheli|paheliyan|pahe?li|kodyo|koydo|koyado)\b"
    r"|पहेली|पहेलियाँ|કોયડો|કોયડા|ઉખાણું"
)

_PROOF = _rx(
    # "proof of work", "proof of address" are names, not proofs.
    r"\b(?:prove|proof(?![-\s]+of\b(?!\s+(?:the|that|this)\b))|derive|derivation|show\s+that|demonstrate\s+that|disprove"
    r"|saabit\s+karo|sabit\s+karo|siddh\s+karo|sabit\s+kar)\b"
    r"|सिद्ध\s{0,4}(?:करें|करो|कीजिए|कीजिये)|साबित\s{0,4}(?:करें|करो|कीजिए)|व्युत्पन्न"
    r"|સાબિત\s{0,4}કરો|સિદ્ધ\s{0,4}કરો"
)

_WHY_EXACTLY = _rx(
    r"\bwhy\s+exactly\b|\bexactly\s+why\b|\bexplain\s+(?:precisely|rigorously)\s+why\b"
)

_COMBINATORICS = _rx(
    r"\bhow\s+many\s+(?:different\s+|distinct\s+|possible\s+|unique\s+)?(?:ways|combinations|arrangements|permutations|handshakes|paths|outcomes)\b"
    r"|\b(?:probability|odds)\s+(?:of|that)\b|\bwhat\s+(?:is|are)\s+the\s+(?:chances?|odds|probability)\b"
    r"|\bexpected\s+(?:value|number)\b|\bpermutations?\s+of\b|\bcombinations?\s+of\s+\d"
    r"|\bkitne\s+(?:tarike|tareeke|tarah)\b|\bketli\s+rite\b|\bsambhavna\b"
    r"|कितने\s+(?:तरीके|तरीकों)|प्रायिकता|संभावना\s+(?:क्या|ज्ञात)"
    r"|કેટલી\s+રીતે|સંભાવના"
)

_CONTAINER = _rx(
    r"\b(?:jugs?|bottles?|buckets?|containers?|jars?|vessels?|glass(?:es)?|cans?|tanks?"
    r"|balti|bartan|lota|botal|dabba)\b"
    r"|बाल्टी|बर्तन|बोतल|जग|ડોલ|વાસણ|બોટલ|જગ"
)
_MEASURE_ACT = _rx(
    r"\b(?:measure|measured|exactly|pour|pouring|fill|filled|empty|mix|mixing|mixture)\b"
    r"|\b(?:naap|naapna|napna|bharo|bharna|bhar|daalo|dalo|mila|milao|milana)\b"
    r"|नाप|मापना|भरो|भरना|डालो|मिलाओ|મપા|માપ|ભરો|રેડો|મિશ્રણ|મિક્સ"
)

_EQUATION = _rx(
    r"\bsolve\s+(?:for\s+[a-z]\b|the\s+(?:equation|system|inequality)|this\s+(?:equation|system)|[^.?!\n]{0,20}=)"
    r"|\b\d{0,15}\s{0,4}[a-z]\s{0,4}(?:\^|\*\*)\s{0,4}\d"                       # x^2, 3x**2
    r"|(?<![A-Za-z\d])\d{1,15}\s{0,4}[a-z](?![A-Za-z])\s{0,4}[-+=]"         # 2x + / 3y =
    r"|(?<![A-Za-z])[a-z]\s{0,4}[-+]\s{0,4}\d{1,15}\s{0,4}="                   # x + 3 =
    r"|\b(?:quadratic|simultaneous\s+equations|linear\s+equations?|integrate|integral\s+of|differentiate|derivative\s+of|limit\s+of)\b"
    r"|\bsamikaran\b|समीकरण|समाकलन|अवकलन|સમીકરણ"
    # number theory and calendar arithmetic over a concrete number
    r"|\bis\s+\d{3,}\s+(?:a\s+)?(?:prime|divisible|perfect\s+(?:square|cube))"
    r"|\d{1,15}\s{0,4}(?:!|factorial)|\bfactorial\s+of\s+\d|\b(?:gcd|lcm|hcf)\s+of\s+\d|\bdivisible\s+by\s+\d"
    r"|\bwhat\s+day\s+(?:of\s+the\s+week\s+)?(?:will|was|would)\s+it\s+be\b"
    r"|\bconsecutive\s+(?:even\s+|odd\s+|whole\s+|natural\s+|positive\s+)?(?:numbers|integers)\b"
)

#: The mislabelled-boxes riddle: wrong labels AND the containers they sit on.
_WRONG_LABELS = _rx(r"\b(?:labels?\s+(?:are\s+|is\s+)?(?:all\s+)?(?:wrong|incorrect)|mislabell?ed|every\s+label\s+is\s+(?:wrong|incorrect))\b")
_LABELLED_THINGS = _rx(r"\b(?:box|boxes|jars?|bags?|drawers?|sacks?|crates?|chests?)\b|डिब्बे|डिब्बा|ડબ્બા|ડબ્બો")

#: Clock-hand problems: the hands AND a geometric question about them.
_CLOCK_HANDS = _rx(
    r"\b(?:hour|minute)\s+and\s+(?:the\s+)?(?:minute|hour)\s+hands?\b|\bghante\s+aur\s+minute\s+ki\s+sui\b"
    r"|घंटे\s+(?:और|व)\s+मिनट\s+की\s+सुई|કલાક\s+અને\s+મિનિટ(?:ના|ની)\s+કાંટા"
)
_CLOCK_Q = _rx(
    r"\b(?:angle|degrees?|overlap|overlaps|coincide|coincides|opposite|right\s+angle|straight\s+line|together|kon|kona|kitne\s+baar)\b"
    r"|कोण|डिग्री|ખૂણો|ડિગ્રી"
)

#: A value someone LOOKS UP rather than works out ("SBI FD rate for 5 years",
#: "आज सोने का भाव 10 ग्राम"). Vetoes only the weakest firing — a quantity
#: question over fewer than three operands with nothing else behind it.
_LOOKUP_VALUE = _rx(
    r"\b(?:rates?|prices?|bhav|bhaav|keemat|kimat|fares?|today|todays|aaj|abhi|current|currently|latest|live|right\s+now)\b"
    r"|भाव|कीमत|दाम|आज|ભાવ|કિંમત|આજે"
)
_COMPUTE_MARKER = _rx(r"%|\b(?:percent|discount|profit|loss|munafa|nuksan|if|agar|jo)\b|अगर|प्रतिशत|જો|ટકા")

#: Divisibility phrased without "by <n>" ("12, 18 aur 30 se poori tarah
#: divisible"). A reasoning signal only with two or more operands beside it.
_DIVISIBILITY = _rx(
    r"\bdivisible\b|\b(?:lcm|hcf|gcd|lasa|masa)\b|\bpoori\s+tarah\s+(?:se\s+)?(?:bhag|vibhajit)|\bvibhajya\b"
    r"|विभाज्य|पूरी\s+तरह\s+(?:से\s+)?विभाजित|ભાગી\s+શકાય|લ\.?\s{0,2}સા\.?\s{0,2}અ"
)

#: Transitive comparisons ("A is taller than B, B is taller than C").
_COMPARATIVE = _rx(
    r"\b(?:taller|shorter|older|younger|heavier|lighter|faster|slower|richer|poorer)\s+than\b"
    r"|\bse\s+(?:lamba|lambi|chhota|chhoti|bada|badi|tez|bhaari|bhari|mota|moti|amir|ameer)\b"
    r"|से\s+(?:लंबा|लंबी|छोटा|छोटी|बड़ा|बड़ी|तेज|भारी|अमीर)"
    r"|થી\s+(?:ઊંચો|ઊંચી|ઉંચો|ઉંચી|મોટો|મોટી|નાનો|નાની|ઝડપી|ભારે)"
)
_SUPERLATIVE_Q = _rx(
    r"\b(?:who|which|kaun|kon)\b[^\n]{0,40}\b(?:tallest|shortest|oldest|youngest|heaviest|lightest|fastest|slowest|richest|poorest|sabse)\b"
    r"|कौन\s[^\n]{0,40}सबसे|કોણ\s[^\n]{0,40}સૌથી"
)

_SEQUENCE = _rx(
    r"\b(?:next|missing)\s+(?:number|term|letter)s?\b"
    r"|\bwhat\s+comes\s+next\b|\bcomplete\s+the\s+(?:series|sequence|pattern)\b"
    r"|\bagla\s+(?:number|ank)\b|अगली\s+संख्या|अगला\s+पद|શ્રેણી(?:માં)?\s+આગળ|આગળની\s+સંખ્યા"
)

_LOGIC = _rx(
    r"\b(?:knights?\s+and\s+knaves|truth[-\s]?tellers?|always\s+lies|always\s+tells\s+the\s+truth"
    r"|who\s+is\s+(?:lying|telling\s+the\s+truth)|syllogism|logically\s+follows?"
    r"|what\s+can\s+(?:we|you|be)\s+(?:deduce|conclude|deduced|concluded)|seating\s+arrangement|blood\s+relation)\b"
    r"|\bif\s+all\s+\w+\s+are\s+\w+"
    r"|\bkaun\s+jhooth\b|\bkaun\s+sach\b|कौन\s+झूठ|कौन\s+सच"
    r"|કોણ\s+ખોટું|કોણ\s+સાચું"
)

_RATIO = _rx(
    r"(?<![\d:])\d{1,15}\s{0,4}:\s{0,4}\d{1,15}(?![\d:])(?!\s{0,4}(?:am|pm|a\.m|p\.m|baje|vage))"
    r"|\bin\s+the\s+ratio\b|\bratio\s+of\s+\d|\bproportion\s+of\s+\d"
    r"|\banupat\b|अनुपात|ગુણોત્તર|પ્રમાણ(?:માં)"
)
_PAIR = re.compile(r"\d{1,15}\s{0,4}:\s{0,4}\d{1,15}")
_RATIO_WORDS = _rx(
    r"\b(?:ratio|anupat|mix|mixed|mixture|proportion|share|shared|divide|divided|split|parts?|bhaag)\b"
    r"|अनुपात|भाग|मिश्रण|ગુણોત્તર|ભાગ|મિશ્રણ"
)
#: "2, 6, 12, 20, ?" — a run of numbers ending in a blank.
_SERIES_BLANK = re.compile(
    r"(?<!\d)\d{1,15}\s{0,4},\s{0,4}\d{1,15}\s{0,4},\s{0,4}\d{1,15}\s{0,4},\s{0,4}(?:\d{1,15}\s{0,4},\s{0,4}){0,12}(?:\?|_{1,8}|\.\.\.|…)"
)

_CODE_BLOCK = re.compile(
    r"```|^\s{0,4}(?:def |class |for |while |if |elif |return |import |from \S+ import |function |const |let |var "
    r"|public |private |#include|int main|console\.log|print\(|printf\(|System\.out|SELECT |UPDATE |[}{];?\s{0,4}$)",
    re.MULTILINE,
)
_CODE_INLINE = re.compile(
    r"\b[A-Za-z_]\w{0,40}\([^()\n]{0,60}\)|\b[A-Za-z_]\w{0,40}\[[^\]\n]{0,20}\]\s{0,4}=|==|!=|\+=|->|=>|&&|\|\|"
)
_DEBUG_INTENT = _rx(
    r"\b(?:bug|buggy|debug|error|exception|traceback|stack\s{0,4}trace|crash(?:es|ing)?|fails?|failing"
    r"|wrong|incorrect|broken|doesn'?t\s+work|does\s+not\s+work|not\s+working|infinite\s+loop|off[-\s]by[-\s]one|never\s+(?:ends|stops|terminates|finishes)|hangs|stuck"
    r"|why\s+does|why\s+is|why\s+do(?:es)?n'?t|what\s+(?:does|will|would)\s+(?:this|it|the)\s+(?:(?:code|program|snippet|function|following|loop|script)\s+)?(?:print|output|return)"
    r"|what\s+is\s+the\s+output|output\s+of|returns?\s+(?:none|null|undefined|nan|nil|nothing|empty|the\s+wrong)|returning\s+(?:none|null|undefined|nan)|duplicate\s+rows|duplicates|trace\s+(?:through|the|this)|step\s+through|dry\s+run|time\s+complexity|space\s+complexity"
    r"|kyu\s+nahi|kyon\s+nahi|kyun\s+nahi|nahi\s+chal|galat|kem\s+nathi|chaltu\s+nathi)\b|\bwhy\s{0,2}\?"
    r"|क्यों\s+नहीं|काम\s+नहीं|त्रुटि|ગલત|ભૂલ|કેમ\s+નથી"
)
_TRACEBACK = _rx(r"traceback \(most recent call last\)|\b\w+(?:Error|Exception):\s|segmentation fault|panicked at")


# ---------------------------------------------------------------------------
# Medium signals (weight 2): a quantity question with numbers to work on.
# ---------------------------------------------------------------------------

_QUANTITY_Q = _rx(
    r"\bhow\s+(?:many|much|long|far|fast|old|often|tall|deep|heavy|high|wide)\b"
    r"|\bwhat\s+(?:is|was|will\s+be|are)\s+(?:the\s+|his\s+|her\s+|their\s+|my\s+)?(?:\w+\s+)?(?:total|sum|difference|product|average|mean|remainder|speed|distance|time\s+taken|area|volume|perimeter|profit|loss|interest|percentage|share|capacity|angle|length|marked\s+price|cost\s+price|selling\s+price|amount|value\s+of\s+[a-z]\b)"
    r"|\bwhen\s+(?:do|does|will|would)\s+(?:they|the\s+two|both|it|he|she|the\s+trains?)\b|\bon\s+which\s+day\b|\bhow\s+much\s+(?:is|will\s+be)\s+left\b"
    r"|\b(?:find|calculate|compute|work\s+out|determine|evaluate|simplify|figure\s+out)\b"
    r"|\bwhen\s+will\s+(?:they|the\s+two|both)\b|\b(?:at\s+)?what\s+time\s+will\b"
    r"|\b(?:cost\s+price|selling\s+price|marked\s+price|munafa|munafaa|profit|loss|nuksan|nuksaan|byaj|interest|raftar|umar|umr|angle|kona)\s+(?:kya|kitn[aie]|ketl[aiou])\b"
    r"|\b(?:kitne|kitna|kitni|kitnaa|ketla|ketlu|ketli|ketlo|kab\s+tak|kitne\s+din|nikalo|nikaalo|gino|ganana|hisab\s+lagao|batao\s+kitne|shodho|gano)\b"
    r"|कितने|कितना|कितनी|ज्ञात\s{0,4}(?:करें|करो|कीजिए|कीजिये)|गणना|निकालो|हल\s{0,4}(?:करें|करो|कीजिए)"
    r"|કેટલા|કેટલું|કેટલી|કેટલો|શોધો|ગણતરી|ગણો|ઉકેલો"
)

_WORD_PROBLEM_VOCAB = _rx(
    r"\b(?:train|trains|speed|km/?h|kmph|mph|per\s+hour|upstream|downstream|leaves?\s+at|meets?"
    r"|older|younger|twice|thrice|times\s+as|half\s+as|years?\s+ago|years?\s+hence"
    r"|together|alone|each|per|remains?|remaining|left|shared?\s+equally|divided\s+equally|in\s+total|more\s+than|less\s+than"
    r"|sum|difference|minutes|hours|days|take|takes|full|capacity|angle|tax|gst|amount|slips?|climbs?"
    r"|profit|loss|discount|interest|compound|simple\s+interest|principal|average|mixture|litres?|liters?|ml"
    r"|umar|saal\s+pehle|ghante|ghanta|raftar|gati|rupaye|rupees|bache|bachenge|baki|baaki|milkar|har\s+ek|ek\s+saath"
    r"|dar\s+ek|vadhare|vadhu|ochha|bachse|sarkhi|sarkha|kalak|divas|munafa|nuksan|cost\s+price|selling\s+price|din|aadmi|kaam"
    r"|km|kms|cm|mm|kg|metres?|meters?|north|south|east|west|every|area|perimeter|width|length|breadth|radius)\b"
    r"|\d\s{0,4}(?:m|g)\b|\d\s{0,4}%"
    r"|लंबाई|चौड़ाई|क्षेत्रफल|परिमाप|सेमी|मीटर|किलो|दिन|काम|લંબાઈ|પહોળાઈ|ક્ષેત્રફળ|પરિમિતિ|વળતર|ટકા|દિવસ|કામ"
    r"|उम्र|वर्ष\s+पहले|घंटे|गति|चाल|रुपये|बचे|बचेगा|मिलकर|प्रत्येक|लीटर|ब्याज|लाभ|हानि"
    r"|ઉંમર|વર્ષ\s+પહેલાં|કલાક|ઝડપ|રૂપિયા|બાકી|સાથે\s+મળીને|દરેક|લિટર|વ્યાજ|નફો|ખોટ"
)

#: The person asks for the working itself.
_SHOW_WORK = _rx(
    r"\bshow\s+(?:the|your|all|me\s+the)\s+(?:reasoning|working|work|steps|calculation)\b"
    r"|\bstep[-\s]by[-\s]step\b|\bexplain\s+(?:your|the)\s+reasoning\b|\bthink\s+(?:carefully|it\s+through)\b"
    r"|\bstep\s+wise\b|\bsteps\s+ke\s+saath\b|चरण\s{0,4}दर\s{0,4}चरण|સ્ટેપ\s{0,4}બાય\s{0,4}સ્ટેપ"
)
#: A code-referential question: the snippet may be one line of SQL.
_ABOUT_CODE = _rx(r"\b(?:this|my|the\s+following)\s+(?:code|sql|query|function|script|program|snippet|regex|loop|class|method)\b")

#: An operator between two numbers. "5 x 3" and "5 × 3" both count.
_ARITH_OP = re.compile(r"(?<![\d,.])(\d[\d,.]{0,20})\s{0,4}([+\-*/×÷^x%])\s{0,4}(?=\d)")
_PERCENT_OF = _rx(
    r"(?<![\d.])\d{1,15}(?:\.\d{1,15})?\s{0,4}(?:%|percent|pratishat|प्रतिशत|ટકા)\s+(?:\w+\s+){0,2}?(?:of|on|ka|ki|ke|par|pe|का|की|के|पर|ના|નું|ની|પર)\s+(?:rs\.?\s{0,4}|₹\s{0,4}|\$\s{0,4})?\d"
)

#: A number that is an OPERAND, not a name: digits not glued to letters
#: ("5G", "iPhone15", "GPT-4o", "COVID-19" are names).
_NUMBER = re.compile(r"(?<![\w.\-/])\d{1,15}(?:[.,]\d{1,15})*(?![\w])|(?<![\w])\d{1,15}(?:\.\d{1,15})?%")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")
#: A number right after a product/ranking word names a thing rather than a
#: quantity: "iPhone 15", "top 10", "Windows 11", "PS 5", "Class 12".
_NAME_NUMBER = _rx(
    r"\b(?:iphone|galaxy|pixel|windows|android|ios|macos|ps|playstation|xbox|top|best|class|std|standard|grade"
    r"|version|v|chapter|episode|season|part|level|round|gen|generation|series|model|note|redmi|oneplus|ipad|watch"
    r"|article|section|sec|rule|clause|room|flat|no|number|page|line|step|day|week|sector|phase|galaxy\s+s)\s+\d{1,15}\b"
)
_WORD_NUMBERS = _rx(
    r"\b(?:two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty|thirty|forty|fifty|hundred|half|double|triple|twice|thrice|quarter)\b"
)


# ---------------------------------------------------------------------------
# Vetoes: the task is to transform or produce text, not to reason.
# ---------------------------------------------------------------------------

_TRANSLATE = _rx(
    r"^\W*(?:please\s+|pls\s+|can\s+you\s+|could\s+you\s+)?(?:translate|translation\s+of)\b"
    r"|\btranslate\s+(?:this|it|the\s+following)\b|\b(?:in|into|to)\s+(?:english|hindi|gujarati|french|spanish|german|marathi|tamil)\s{0,4}(?:translate|anuvad)"
    r"|\b(?:anuvad|anuvaad|translate)\s+(?:karo|kar\s+do|kijiye)\b|अनुवाद|ભાષાંતર|અનુવાદ"
)
_SUMMARY = _rx(
    r"^\W*(?:please\s+|pls\s+|can\s+you\s+|could\s+you\s+)?(?:summari[sz]e|summary\s+of|tl;?dr|give\s+(?:me\s+)?a\s+summary|sum\s+up)\b"
    r"|\bsummari[sz]e\s+(?:this|the\s+following|it)\b|\b(?:saar|saransh)\s+(?:batao|do|likho)\b|सारांश|સારાંશ"
)
_WRITING = _rx(
    r"^\W*(?:please\s+|pls\s+|can\s+you\s+|could\s+you\s+|help\s+me\s+)?(?:write|draft|compose|rewrite|paraphrase|proofread|edit|polish|create|generate|make)\s+"
    r"(?:me\s+)?(?:an?\s+|the\s+|my\s+|some\s+|\d{1,15}\s+)?(?:\w+\s+){0,3}?"
    r"(?:email|e-mail|mail|letter|essay|poem|story|caption|tweet|post|blog|article|speech|message|reply|cover\s+letter|resume|cv|bio|description|slogan|tagline|script|lyrics|song|note|invitation|report|summary|outline|paragraph|headline|joke|wish|quote|quotes|content|copy)\b"
    r"|\b(?:likh\s+do|likho|likhiye)\b|लिखो|लिखिए|लिखें|લખો|લખી\s+આપો"
)
_GREETING = _rx(
    r"^\W*(?:hi|hii+|hello|hey|namaste|namaskar|kem\s+cho|good\s+(?:morning|afternoon|evening|night)|thanks?|thank\s+you|ok(?:ay)?|bye)\b[\W\w]{0,20}$"
)
#: Words that open a code-writing task; writing code is not debugging it.
_CODE_WRITE = _rx(
    r"^\W*(?:please\s+|can\s+you\s+|could\s+you\s+)?(?:write|create|generate|build|implement|make)\s+(?:me\s+)?(?:an?\s+|the\s+)?(?:\w+\s+){0,3}?(?:function|script|program|class|code|query|regex|api|app|component)\b"
)


#: Word numbers in Hindi and Gujarati (script and Latin). Whitespace-bounded:
#: `\b` is unreliable inside Indic scripts, whose vowel signs are not word
#: characters. "दो" / "do" are left out: they also mean "give".
_INDIC_WORD_NUMBERS = re.compile(
    r"(?<!\S)(?:तीन|चार|पांच|पाँच|छह|सात|आठ|नौ|दस|आधा|आधी|दुगुन[ाीे]|दुगन[ाीे]|दोगुन[ाीे]|तिगुन[ाीे]|गुन[ाीे]"
    r"|ત્રણ|ચાર|પાંચ|અડધ[ોુી]|બમણ[ોુી]|ગણ[ોુી]"
    r"|dugna|dugni|dugne|doguna|tigna|tigni|tiguna|guna|guni|aadha|aadhi|bamno|bamni|bamnu)(?!\S)",
    re.IGNORECASE,
)


def _operand_numbers(text: str) -> int:
    """How many numbers in `text` are operands rather than names or years."""
    named = set()
    for m in _NAME_NUMBER.finditer(text):
        named.add(m.end())
    count = 0
    years = 0
    for m in _NUMBER.finditer(text):
        if m.end() in named:
            continue
        token = m.group(0)
        if _YEAR.match(token):
            years += 1
            continue
        count += 1
    count += len(_WORD_NUMBERS.findall(text))
    count += len(_INDIC_WORD_NUMBERS.findall(text))
    # Years are dates, not operands, unless there are already other numbers
    # to work on ("born in 1990, how old in 2030" is a problem).
    if count >= 1 and years:
        count += years
    return count


def _arithmetic(text: str) -> bool:
    """A calculation worth doing carefully: two or more operators, a
    percentage of a number, or a single × ÷ ^ over multi-digit operands.
    "2+2" and "7 x 8" are not; "25 * 48" and "15% of 2400" are."""
    if _PERCENT_OF.search(text):
        return True
    real = []
    for m in _ARITH_OP.finditer(text):
        op = m.group(2)
        before = text[m.end(1):m.start(2)]
        after = text[m.end(2):m.end()]
        # A hyphen or slash glued to digits on both sides is a phone number,
        # an id, a date or a range ("98765-43210", "12/05/2024", "10-15").
        if op in "-/" and not before and not after:
            continue
        real.append(m)
        if len(real) >= 2:
            break
    if len(real) >= 2:
        return True
    if len(real) == 1:
        m = real[0]
        op = m.group(2)
        left = re.sub(r"\D", "", m.group(1))
        right_m = re.match(r"\d[\d,.]{0,20}", text[m.end():])
        right = re.sub(r"\D", "", right_m.group(0)) if right_m else ""
        if op in "*/×÷^x" and len(left) + len(right) >= 4:
            return True
        if op in "+-" and (len(left) >= 4 or len(right) >= 4) and len(left) + len(right) >= 7:
            return True
    return False


#: An inline SQL statement ("... why? SELECT a FROM t JOIN u ON ...").
_INLINE_SQL = re.compile(r"\bSELECT\s[^\n]{1,300}?\sFROM\s|\bUPDATE\s+\w+\s+SET\s|\bINSERT\s+INTO\s|\bDELETE\s+FROM\s")


def _looks_like_code(text: str) -> bool:
    if "```" in text or _TRACEBACK.search(text) or _INLINE_SQL.search(text):
        return True
    block_lines = len(_CODE_BLOCK.findall(text))
    inline = len(_CODE_INLINE.findall(text))
    return block_lines >= 2 or (block_lines >= 1 and inline >= 1) or inline >= 3


def classify(text: Optional[str]) -> ThinkingDecision:
    """Decide whether a Fast prompt needs bounded thinking. Pure, < 5 ms."""
    started = time.perf_counter()

    def _done(think: bool, reason: str, score: int = 0, signals: Tuple[str, ...] = ()) -> ThinkingDecision:
        return ThinkingDecision(
            think=think,
            reason=reason,
            score=score,
            signals=signals,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    raw = (text or "").strip()
    if not raw:
        return _done(False, "empty")
    if len(raw) > _HEAD_CHARS + _TAIL_CHARS:
        body = raw[:_HEAD_CHARS] + "\n" + raw[-_TAIL_CHARS:]
    else:
        body = raw
    # The instruction is at the start; a veto reads only the first lines so a
    # "translate" inside pasted code is not a translation request.
    head = body[:400]

    if _TRANSLATE.search(head):
        return _done(False, "veto_translate", signals=("translate",))
    if _SUMMARY.search(head):
        return _done(False, "veto_summary", signals=("summary",))
    if _GREETING.match(body):
        return _done(False, "no_signal", signals=("greeting",))

    signals: List[Tuple[str, int]] = []
    if _PUZZLE.search(body):
        signals.append(("puzzle", 3))
    if _PROOF.search(body):
        signals.append(("proof", 3))
    if _WHY_EXACTLY.search(body):
        signals.append(("why_exactly", 3))
    if _COMBINATORICS.search(body):
        signals.append(("combinatorics", 3))
    if _CONTAINER.search(body) and _MEASURE_ACT.search(body) and (
        _operand_numbers(body) >= 1 or _RATIO.search(body)
    ):
        signals.append(("measurement", 3))
    if _EQUATION.search(body):
        signals.append(("equation", 3))
    if _SEQUENCE.search(body) or _SERIES_BLANK.search(body):
        signals.append(("sequence", 3))
    if _LOGIC.search(body):
        signals.append(("logic", 3))
    # The SAME comparison chained ("A se lamba, B se lamba ... kya C se
    # lamba?"), not a product sheet ("faster than X and lighter than Y").
    chained: dict = {}
    for m in _COMPARATIVE.finditer(body):
        key = " ".join(m.group(0).lower().split())
        chained[key] = chained.get(key, 0) + 1
    most = max(chained.values(), default=0)
    if most >= 3 or (most >= 2 and _SUPERLATIVE_Q.search(body)):
        signals.append(("logic", 3))
    if _WRONG_LABELS.search(body) and _LABELLED_THINGS.search(body):
        signals.append(("logic", 3))
    if _CLOCK_HANDS.search(body) and _CLOCK_Q.search(body) and not _looks_like_code(body):
        signals.append(("equation", 3))
    ratio = _RATIO.search(body)
    if ratio is not None:
        bare_pair = _PAIR.fullmatch(ratio.group(0).strip()) is not None
        # A bare "a:b" is a ratio only with ratio vocabulary beside it;
        # otherwise it is a time, a score or a verse ("5:30", "2:1 win").
        if not bare_pair or _RATIO_WORDS.search(body):
            signals.append(("ratio", 3))
    code = _looks_like_code(body) or (
        _ABOUT_CODE.search(body) is not None and len(_CODE_BLOCK.findall(body)) >= 1
    )
    if code and (_DEBUG_INTENT.search(body) or _TRACEBACK.search(body)):
        signals.append(("code_debug", 3))

    operands = _operand_numbers(body)
    if "equation" not in (n for n, _ in signals) and operands >= 2 and _DIVISIBILITY.search(body):
        signals.append(("equation", 3))
    quantity = _QUANTITY_Q.search(body) is not None
    if quantity and operands >= 2:
        # Three operands and a quantity question is a problem statement;
        # with two, the problem vocabulary has to agree.
        signals.append(("word_problem", 3 if operands >= 3 else 2))
        if operands < 3 and _WORD_PROBLEM_VOCAB.search(body):
            signals.append(("word_problem_vocab", 1))
    if _SHOW_WORK.search(body) and operands >= 1:
        signals.append(("show_work", 2))
    if _arithmetic(body):
        signals.append(("arithmetic", 3 if operands >= 2 else 2))

    score = sum(weight for _, weight in signals)
    names = tuple(name for name, _ in signals)

    if (
        names
        and set(names) <= {"word_problem", "word_problem_vocab"}
        and operands < 3
        and _LOOKUP_VALUE.search(body)
        and not _COMPUTE_MARKER.search(body)
    ):
        return _done(False, "no_signal", score, names)

    if _WRITING.search(head) and not any(n in names for n in ("proof", "equation", "code_debug")):
        return _done(False, "veto_writing", score, names)
    if _CODE_WRITE.search(head) and "code_debug" not in names and not any(
        n in names for n in ("puzzle", "proof", "equation", "combinatorics")
    ):
        return _done(False, "veto_writing", score, names)

    if score >= THRESHOLD:
        strongest = max(signals, key=lambda item: item[1])[0]
        reason = "word_problem" if strongest == "word_problem_vocab" else strongest
        return _done(True, reason, score, names)
    return _done(False, "no_signal", score, names)
