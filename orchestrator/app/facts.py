"""Explicit fact store (V10, 2026-08-21) — ChatGPT-style Memory.

When the user states something durable ("Sahil Patel is the CEO of TechSara",
"my name is Naman", "always answer in Hindi"), a background call to the small
router model extracts it as a short third-person fact and stores it in the
`user_facts` table. Every later assistant-mode request injects the user's
facts as a labelled system block, so the model "remembers" without any
fine-tuning — memory is retrieval, exactly as ChatGPT does it.

Extraction runs CONCURRENTLY with answer generation (it reads only the user's
message, not the answer), so it adds zero latency; its result rides out on
the final meta as `memory_updated` when it lands before the answer finishes.
Everything degrades to "no memory update" on failure — never a failed chat.

MEMORY INTEGRITY (2026-09-18). The extractor is a prompt, and until this
round whatever it returned was written to the table unchecked and then read
back to the model under "treat as true for this user". The platform sweep
found what that costs: a pasted CV overwrote an account's own name, email and
employer with a stranger's; "please forget that I'm vegetarian" was stored as
"The user is not vegetarian" and produced steakhouse recommendations; a
headcount moved from an old employer to a new one and was answered flatly;
and 39% of the live store was one-off task requests replayed as a to-do list.
Four rules now stand between the model and the table:

  1. Only the person's own words about themselves. A turn carrying an
     attachment writes nothing, and fenced/quoted material and pasted-length
     messages are not the person speaking (`own_words`).
  2. A "forget that" may only DELETE (`_FORGET_RE`, db.delete_user_fact); it
     can never add or rewrite, so an erasure request cannot leave a negated
     copy of the thing behind. And only the PERSON asking deletes anything
     (`_erasure_requests`, owner rule 2026-09-19): a question about
     forgetting, a reminder or a third party's "forget" deletes nothing,
     whatever the extractor proposes.
  3. Only what was stated: a fact may not contain a number or a name the
     message did not contain (`ungrounded_in`), and every row records where
     it came from (`source`, `source_excerpt` — db V40).
  4. A one-off task request is not a durable fact (`is_durable`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import List, Optional

from . import db, llm
from .config import settings

log = logging.getLogger(__name__)

FACTS_HEADER = (
    "Durable facts this user has told you in past conversations (their saved "
    "memory — treat as true for this user unless they correct you; don't "
    "mention this list unless asked):"
)

# Bounds keep the block and the extractor prompt from growing without limit.
_FACT_MAX_CHARS = 300
_BLOCK_MAX_CHARS = 6000
_MESSAGE_MAX_CHARS = 4000
_MESSAGE_MIN_CHARS = 8

_EXTRACT_SYSTEM = """You maintain a user's long-term memory for a chat assistant.

Given the user's newest message and their currently saved facts, decide what
to remember. A fact is a short, durable, third-person statement the user
STATED about themselves or about the world ("Sahil Patel is the CEO of
TechSara", "The user's name is Naman", "The user prefers answers in Hindi").

Store ONLY what the message says in so many words. Never infer, never
combine a saved fact with a new one, and never carry a detail (a number, a
name) from one subject to another: if the user changes employer, the old
employer's headcount is NOT the new employer's.

The message may quote or contain a document, a CV, an email or another
person's words. That material is not the user. Only first-person statements
the user makes about themselves become facts about them.

Do NOT store: questions, requests ("give me 200 practice questions"),
greetings, opinions about the current task, anything transient ("today",
"this file"), or anything already saved.

If a new statement contradicts or updates a saved fact, replace that fact.
When the user asks you to FORGET something, put that saved fact's id in
"remove" — never store a negated version of it.

Reply with ONLY a JSON object, no other text:
{"add": ["<new fact>", ...], "replace": [{"id": <saved fact id>, "fact": "<rewritten fact>"}, ...], "remove": [<saved fact id>, ...]}
Use {"add": [], "replace": [], "remove": []} when there is nothing to do."""

#: A message that asks the assistant to forget something. Such a message
#: never CREATES memory — the sweep found "please forget that I'm vegetarian"
#: stored as "The user is not vegetarian", so the erasure request itself
#: became a permanent record of the thing (and the assistant then recommended
#: steakhouses). Every add/replace is dropped for these messages; only a
#: delete may come out of one.
_FORGET_RE = re.compile(
    r"\b(?:forget|un-?remember|erase)\b"
    r"|\b(?:stop|don'?t|do not|no longer)\s+(?:remember|remembering|storing|saving)\b"
    r"|\b(?:delete|remove|drop|clear)\s+(?:that|this|the|my)?\s*(?:saved\s+)?"
    r"(?:memory|memories|fact|facts)\b",
    re.I,
)

#: …but "forget" is also ordinary English, and the id-less fallback below
#: DELETES the one saved fact whose content words a forget request matches.
#: QA reproduced two silent, irreversible deletions on 2026-09-18: "Don't
#: forget to add the unit tests" removed "The user always wants unit tests with
#: code", and "I always forget my password, any tips?" removed the password
#: manager fact. A reminder ("forget to …") and a confession ("I always forget
#: …") are not erasure requests.
_NOT_A_FORGET_RE = re.compile(
    r"\bforget\s+to\b"
    r"|\b(?:i|we)\s+(?:always|often|sometimes|usually|keep|kept|never)?\s*forget\b"
    # "Don't forget my name is Naman" asks to REMEMBER; at 4810da0 it deleted
    # the name fact through the content word "name" (2026-09-18).
    r"|\b(?:don['’]?t|do not|never|won['’]?t)\s+forget\b",
    re.I,
)

#: A one-off task request wearing a fact's clothes. 51 of the 132 rows in the
#: production store matched this shape ("The user is asking about all movie
#: names in the Spider-Man franchise", "The user wants 200 LeetCode
#: questions"), and they came back to the person as a to-do list when they
#: asked what the assistant remembered about them. The extractor prompt
#: already forbids them; the model ignores it, so the filter is code.
_TRANSIENT_FACT_RE = re.compile(
    r"^the user(?:'s)?\s+(?:is\s+|was\s+|has\s+|have\s+|had\s+)?"
    r"(?:currently\s+|now\s+|also\s+)?"
    r"(?:asking|asked|asks|request|requests|requested|requesting|want|wants|"
    r"wanted|need|needs|needed|looking(?!\s+after)|discussing|trying)\b",
    re.I,
)

#: What may follow "to be called" in a name preference: a capitalised word
#: that is not a day or a deadline ("called Monday morning" is a task), or
#: "by their (first|nick…) name".
_A_NAME = (
    r"(?:(?!(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
    r"Today|Tonight|Tomorrow|ASAP)\b)(?-i:[A-Z])"
    r"|by\s+(?:my|their|his|her)\s+(?:first\s+|last\s+|middle\s+|full\s+)?(?:nick)?name\b)"
)

#: …unless the same sentence states a STANDING preference, which is durable
#: however it is phrased. Two shapes count: an explicit standing word
#: ("always", "prefers"), and a wish about HOW the assistant should answer
#: ("The user wants responses in layman terms") — the production store holds
#: real ones of both kinds, and losing them would trade one regression for
#: another. A wish about WHAT to produce ("200 LeetCode questions", "an ATM
#: UI in Python") matches neither and stays out.
_DURABLE_PREFERENCE_RE = re.compile(
    r"\b(?:always|never|prefers?|preference|by default|from now on)\b"
    r"|\b(?:answers?|responses?|replies|explanations?|output|tone|style|"
    r"wording|format|formatting|language|units)\b"
    r"\s+(?:in|to be|as|with|without|free of|avoiding|using|written|formatted)\b"
    # How the person wants to be addressed ("The user wants to be called
    # Sam"). "Wants" made it a task request, so the name was never saved.
    # The shape must END in a name: listing what may not follow ("back",
    # "tomorrow") let "wants to be called when the build finishes" and "is
    # asking what the band goes by" in as durable (QA, 2026-09-18). And the
    # one named must be the USER: unanchored, "The user wants the new repo
    # to be called Atlas" and "…if Priya goes by Pri" were durable too, and
    # came back as the person's to-do list (security review, 2026-09-18).
    r"|^the user\s+(?:(?:also|still|now|really)\s+)?"
    r"(?:wants|prefers|likes|would\s+like|asks|asked|has\s+asked)\s+to\s+be\s+"
    r"(?:called|addressed(?:\s+as)?|referred\s+to\s+as)\s+" + _A_NAME +
    r"|^the user\s+(?:(?:also|still|now|usually)\s+)?goes\s+by\s+" + _A_NAME +
    r"|\b(?:address|call|refer to)\s+(?:me|them|him|her|the user)\s+as\s+" + _A_NAME,
    re.I,
)

#: Fenced blocks and quoted lines are material the person put in front of the
#: assistant, not the person speaking. They are cut out before the extractor
#: sees the message, so nothing inside them can become a fact and nothing
#: inside them can GROUND one either.
_FENCE_RE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_QUOTED_LINE_RE = re.compile(r"^\s*>.*$", re.M)

#: A pasted document announces itself in its layout long before it reaches the
#: length ceiling: an ALL-CAPS name banner, or a line carrying an email address.
#: QA stored a 214-character CV paste as the account's name, email and employer
#: on 2026-09-18 — the same defect as a 4,000-character one, under the ceiling.
_DOCUMENT_BANNER_RE = re.compile(
    r"^[A-Z][A-Z][A-Z .'\u2019-]{3,}$"
    r"|^[^\n]*[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}[^\n]*$",
    re.M,
)

#: Grounding: a stored fact may only contain numbers and names the message
#: itself contains. `_NAME_RE` is deliberately crude — a capitalized word is
#: a name often enough, and the cost of a false positive is one fact not
#: saved, while the cost of a false negative is an invented one treated as
#: true forever.
_NUMBER_RE = re.compile(r"\d+")
_NAME_RE = re.compile(r"\b[A-Z][A-Za-z][A-Za-z'’-]+\b")
_NAME_STOPWORDS = frozenset(
    """The This That There These Those They Their Them User Users And But
    For Not With From Into Also When What Who Where Why How Has Have Had
    Does Did Will Would Should Could Every Always Never Prefers Prefer
    Wants Want Likes Like Uses Use Works Work Lives Live Needs Need""".split()
)

#: A legal-form word the extractor appends to a company the person named
#: ("I work at TechSara" -> "works at TechSara Solutions"). Ungrounded, it
#: dropped the whole fact (QA-mem-techsara-solutions, 2026-09-18); it is cut
#: instead — but only where the fact names an organisation ("at", "for",
#: "joined" …) and only after a name the message contains, so "leads the
#: Platform Group" after "I lead the platform team" still fails closed.
_CORPORATE_SUFFIX = r"(?:Solutions|Inc|Ltd|LLC|Technologies|Labs|Group|Corp|Pvt|Limited)"
_CORPORATE_SUFFIX_CHAIN_RE = re.compile(
    r"(?P<lead>\b(?i:at|for|with|by|of|from|joined|founded|runs|owns)\s+)"
    r"(?P<name>[A-Z][\w&'’-]*(?:\s+[A-Z][\w&'’-]*)*?)"
    r"(?P<chain>(?:,?\s+" + _CORPORATE_SUFFIX + r"\b\.?)+)"
)
_CORPORATE_SUFFIX_RE = re.compile(r",?\s+(?P<word>" + _CORPORATE_SUFFIX + r")\b\.?")

#: Words too common to identify WHICH saved fact a "forget that" points at.
_MATCH_STOPWORDS = frozenset(
    """please forget remember memory memories fact facts that this these
    those about from with your you mine thing things stuff anymore longer
    what when where which have here there stop don't dont delete remove
    drop clear stored saved again also just only more been very said told
    tell said know known sure okay date outdated wrong""".split()
)


#: WHO MAY DELETE A FACT (owner rule, 2026-09-19): only the person, by asking
#: for it. Round 2 took any clause holding "forget" as an erasure and let a
#: profile noun in it pick the row, and both reviewers then hard-deleted a
#: profile fact with "Did you forget where I live?", "Don't you dare forget my
#: employer!" and "How do I make Chrome forget my address?" (none of which
#: deleted anything at 4810da0). A request is now recognised by its SHAPE: a
#: sentence that OPENS with the erasure verb, after nothing but "please",
#: "ok", "can you" and the like, aimed at the person's own memory and ending
#: there. Questions, reminders, complaints, third parties, quoted or reported
#: speech and "forget X, tell me Y" all fail that shape. Measured on the 818
#: messages of tests/test_memory_erasure_requests.py, written before this
#: code: at 2f702e1 the fallback deleted a fact for 431 of them and a
#: worst-case extractor's remove ids got through for 579; both are 0 now.
_ERASE_LEAD = (
    r"[\s,]*"
    r"(?:(?:ok(?:ay)?|alright|all\s+right|so|and|also|now|hey|actually|then|oh|btw|"
    r"by\s+the\s+way|one\s+more\s+thing|last\s+thing|finally|lastly|anyways?)\b[\s,]*)*"
    r"(?:(?:please|pls|plz|kindly)\b[\s,]*)?"
    # "can you forget my employer?" asks; "can you ACTUALLY forget …?" wonders
    r"(?:(?P<ask>(?:can|could|would)\s+you)\s+"
    r"|i\s+(?:want|need)\s+you\s+to\s+|i(?:['’]d|\s+would)\s+like\s+you\s+to\s+)?"
    r"(?:(?:please|kindly|just|go\s+ahead\s+and)\s+)*"
)
_ERASE_SENTENCE_RE = re.compile(
    _ERASE_LEAD
    + r"(?:(?P<forget>forget|erase|un-?remember)\s+(?:about\s+)?"
    r"|(?:stop|quit)\s+(?P<keep>remembering|storing|saving|keeping)\s+(?:about\s+)?"
    r"|(?P<delete>delete|remove|drop|clear|wipe|purge)\s+)"
    r"(?P<object>.*\S)",
    re.I,
)

#: What may follow the thing to be forgotten: a courtesy, a reason ("I
#: moved"), or a reminder ("…, and don't forget to answer in English").
#: Anything else — "for now", "in the resume", "tell me a joke" — makes it
#: a topic set aside or an edit, not an erasure.
_ERASE_TAIL = (
    # Possessive (*+, ?+): with backtracking, a clause read lazily up to a
    # long run of ", please" cost 160-175 ms of event loop on a
    # 1,150-character message (measured 2026-09-19).
    r"(?:[\s,]++(?:please|pls|plz|now|too|as\s+well|for\s+good|permanently|completely|"
    r"entirely|forever|for\s+me|thanks|thank\s+you|thx|ty)\b)*+"
    r"(?:,?\s+(?:(?:because\s+|since\s+|as\s+)?i(?:\s+have|['’]ve|\s+just)?\s+"
    r"(?:left|quit|moved(?:\s+out|\s+house)?|resigned|relocated|changed\s+jobs|switched\s+jobs)"
    r"|(?:that|it|this)(?:['’]s|\s+is)\s+(?:now\s+)?(?:out\s+of\s+date|outdated|wrong|"
    r"incorrect|old\s+news|not\s+true|no\s+longer\s+true|not\s+right|changed|not\s+the\s+case)"
    r"|not\s+any\s*more|no\s+longer)\b)?+"
    r"(?:,?\s+(?:and|but)\s+(?:please\s+)?(?:(?:don['’]?t|do\s+not|never)\s+forget|remember)\b.*)?+"
    r"(?:[\s,]++(?:please|pls|plz|thanks|thank\s+you|thx|ty)\b)*+"
    r"[\W_]*+"
)

#: One profile item the person owns: "my employer", "where I live". Not "my
#: company's logo" (the possessive) and not "my work" (as often a topic as
#: a workplace: "forget my work, tell me a joke").
_PROFILE_ITEM = (
    r"(?:my\s+(?:current\s+|old\s+|previous\s+|former\s+)?(?:home\s+)?"
    r"(?:employer|company|job|workplace|address|home|city|name)(?![\w'’])"
    r"|where\s+i\s+(?:work|live)|who\s+i\s+work\s+for)"
)
_PROFILE_LIST = _PROFILE_ITEM + r"(?:(?:\s*,\s*|\s+)(?:(?:and|or)\s+)?" + _PROFILE_ITEM + r")*"
_PROFILE_NOUN_RE = re.compile(
    r"\b(?:(?P<noun>employer|company|job|workplace|address|home|city|name)"
    r"|where\s+i\s+(?P<verb>work|live)|who\s+i\s+(?P<who>work)\s+for)\b",
    re.I,
)

#: The objects an erasure verb may take, each tried against the whole rest
#: of the sentence. `clause` is what names the fact; a form with no clause,
#: or the bare "my <noun phrase>", lets the extractor's id through but gives
#: the id-less fallback nothing to match.
_OBJ_PROFILE_RE = re.compile(
    r"(?P<clause>" + _PROFILE_LIST + r")"
    # "forget my employer, Cognitiv": the appositive must also be in the fact
    r"(?:\s*,\s*(?P<named>(?!(?:please|thanks|thank|thx|i)\b)(?-i:[A-Z])[\w&'’-]*+"
    r"(?:\s+(?-i:[A-Z])[\w&'’-]*+)*+))?" + _ERASE_TAIL,
    re.I,
)
_OBJ_MEMORY_RE = re.compile(
    r"(?:my|the|that|this|your|these|those|every|all(?:\s+of)?(?:\s+(?:my|your|the))?)\s+"
    r"(?:saved\s+|stored\s+)?(?:memory|memories|facts?)"
    r"(?:\s+(?:about|on|regarding|of|that)\s+(?P<clause>.*?\w))?" + _ERASE_TAIL,
    re.I,
)
_OBJ_FROM_MEMORY_RE = re.compile(
    r"(?P<clause>.+?)\s+from\s+(?:your\s+|the\s+|my\s+)?"
    r"(?:memory|memories|saved\s+(?:facts|memories))" + _ERASE_TAIL,
    re.I,
)
_OBJ_KNOWN_RE = re.compile(
    r"(?:what|everything|anything|whatever|all)\s+(?:that\s+)?you\s+(?:have\s+|['’]ve\s+)?"
    r"(?:saved|stored|remember|know|noted|kept|learned|learnt|have\s+on\s+file)\s+"
    r"(?:about|regarding|on)\s+(?P<clause>.*?\w)" + _ERASE_TAIL,
    re.I,
)
_OBJ_TOLD_RE = re.compile(
    r"(?:(?:what|everything|anything|all)\s+(?:that\s+)?)?i\s+(?:ever\s+|just\s+)?"
    r"(?:told\s+you|said|mentioned|shared|wrote)\s+(?:(?:that|about|regarding|on)\s+)?"
    r"(?P<clause>.*?\w)" + _ERASE_TAIL,
    re.I,
)
_OBJ_THAT_RE = re.compile(
    r"that\s+(?P<clause>(?:i|my|i['’](?:m|ve|d|ll))\b.*?\w)" + _ERASE_TAIL,
    re.I,
)
_OBJ_MY_RE = re.compile(
    r"(?:about\s+)?my\s+(?P<np>[\w'’-]+(?:\s+[\w'’-]+){0,2})" + _ERASE_TAIL,
    re.I,
)
#: Which object each verb may take. "Delete my address" is as often an edit
#: to a document as a request to the memory, so delete/remove/clear only
#: count when the object names the memory itself.
_FORGET_OBJECTS = (
    _OBJ_PROFILE_RE, _OBJ_MEMORY_RE, _OBJ_FROM_MEMORY_RE, _OBJ_KNOWN_RE,
    _OBJ_TOLD_RE, _OBJ_THAT_RE, _OBJ_MY_RE,
)
_DELETE_OBJECTS = (_OBJ_MEMORY_RE, _OBJ_FROM_MEMORY_RE, _OBJ_KNOWN_RE)

#: Words that turn "my <noun phrase>" into something else: "my name ON the
#: certificate", "my employer IN the resume template".
_NOT_A_NOUN_PHRASE = frozenset(
    """in on at for from to of with and or but when while if so the a an this
    that these those it its as by about into than then until after before
    since because unless once later""".split()
)

#: A clause that goes on to ask for something else ("…vegetarian and tell me
#: the best steakhouse"), that is only for a while ("for tonight", "when we
#: eat out"), or that means "never mind" ("that I asked", "I said that").
_CLAUSE_CONTINUES_RE = re.compile(
    r"[,;:]"
    r"|\b(?:and|but|so|then|or)\s+(?:then\s+|just\s+|please\s+|also\s+)?"
    r"(?:tell|give|show|recommend|suggest|find|write|help|list|explain|answer|make|"
    r"create|plan|book|search|look|let['’]?s|let\s+me|what|how|which|where|who|why|when|"
    r"can|could|would|will|use|keep|start|do|is|are)\b",
    re.I,
)
_CLAUSE_TEMPORARY_RE = re.compile(
    r"\b(?:for\s+(?:now|today|tonight|the\s+moment|a\s+(?:second|sec|moment|minute|bit|while)|"
    r"one\s+(?:second|sec|moment|minute)|this\s+\w+)|just\s+this\s+once|this\s+(?:time|once)|"
    r"temporarily|when|whenever|if|after|until|once|unless|later)\b",
    re.I,
)
#: …or that points at a document rather than at the memory: "delete what
#: you know about my employer from the report".
_CLAUSE_DOCUMENT_RE = re.compile(
    r"\b(?:in|on|from|into|to)\s+(?:the|this|that|your|my|our|a)\s+(?:[\w-]+\s+){0,2}"
    r"(?:report|docs?|document|letter|e-?mail|mail|file|draft|resume|résumé|cv|pdf|page|"
    r"form|slides?|deck|essay|story|answer|reply|response|text|message|notes?|summary|"
    r"bio|profile|post|invoice|contract|template|code|script|spreadsheet|sheet|table|list|"
    r"prompt)\b",
    re.I,
)
_NEVER_MIND_RE = re.compile(
    r"(?:i\s+)?(?:just\s+)?(?:said|asked|mentioned|wrote|typed|sent|told\s+you)?\s*"
    r"(?:it|that|this|anything|something|everything|so|all\s+(?:that|this|of\s+(?:it|that|this)))?"
    r"\s*(?:earlier|before|above|previously|just\s+now)?[\W_]*",
    re.I,
)

#: A message that takes the request back, or is not in earnest, anywhere in
#: it: "Please forget my employer. Just kidding!", "forget my employer lol".
_RETRACTION_RE = re.compile(
    r"\b(?:just\s+kidding|j/?k|kidding|joking|never\s*mind|nvm|scratch\s+that|"
    r"ignore\s+(?:that|this|what\s+i\s+(?:just\s+)?said)|"
    r"(?:actually|wait|no)[\s,]+(?:no[\s,]+)?(?:don['’]?t|do\s+not|keep\s+it|leave\s+it)|"
    r"keep\s+it|leave\s+it|not\s+really|lol|lmao|rofl|haha+|hehe+)\b|[😂🤣]",
    re.I,
)

#: What else a message that deletes may say. "Forget my address. I'll send
#: it later." and "Forget my employer. Write a generic cover letter." put a
#: topic aside for a task, sentence by sentence; only a courtesy, a reason
#: ("I moved.", "That's out of date."), another erasure, or a reminder about
#: the person ("Don't forget I like spicy food though.") may stand beside a
#: request that deletes. Any other sentence and nothing is deleted.
_ERASE_COMPANION_RE = re.compile(
    r"[\s,]*(?:"
    r"(?:ok(?:ay)?|thanks?(?:\s+(?:a\s+lot|so\s+much|again|in\s+advance))?|"
    r"thank\s+you(?:\s+(?:so\s+much|again))?|thx|ty|please|pls|cheers|appreciate\s+it)"
    r"|(?:because\s+|since\s+)?i(?:['’]ve|\s+have|\s+just)?\s+(?:left|quit|moved(?:\s+out|\s+house|\s+away)?|"
    r"resigned|relocated|retired|changed\s+jobs|switched\s+jobs)"
    # RV3: the reason ends at the verb or names where the person went; "I moved THE PARTY
    # to the office" / "I left IT in the form" is a task, not a reason.
    r"(?:\s+(?:(?:to|from|for)\s+)?(?-i:[A-Z])[\w&'’-]*+(?:\s+(?-i:[A-Z])[\w&'’-]*+)*+"
    r"|\s+(?:the|that|my)\s+(?:company|job|firm|city|country|flat|apartment|house|place)"
    r"|\s+(?:recently|already|last\s+(?:week|month|year)|(?:a\s+)?(?:few|couple\s+of)\s+(?:days|weeks|months|years)\s+ago))*+"
    r"|(?:that|it|this)(?:['’]s|\s+is)\s+(?:now\s+)?(?:out\s+of\s+date|outdated|wrong|"
    r"incorrect|old\s+news|not\s+true(?:\s+any\s*more)?|no\s+longer\s+true|not\s+right|"
    r"private|personal|changed|not\s+the\s+case)"
    r"|(?:that|it)\s+changed"
    r"|i\s+(?:don['’]?t|do\s+not)\s+want\s+(?:you\s+to\s+(?:remember|store|save|keep|know)\s+"
    r"(?:that|it|this)|(?:that|it|this)\s+(?:saved|stored|remembered|kept))(?:\s+any\s*more)?"
    r"|(?:and\s+|but\s+)?(?:please\s+)?(?:(?:don['’]?t|do\s+not|never)\s+forget|remember)\s+"
    r"(?:that\s+)?(?:i\b|i['’]|my\b).*"
    r")[\W_]*",
    re.I,
)

_ERASE_SENTENCE_SPLIT_RE = re.compile(r"[^.!?\n]+")
_END_PUNCT_RE = re.compile(r"[.!?]*")

#: …and the fact each profile item is stated in. A bag of the words such a
#: fact is written with ("named", "works", "based") matched "The user's dog
#: is named Rex", "The user's wife works at Google" and "The user prefers
#: answers based on primary sources", and exactly-one-match does not help
#: when the one match is the wrong fact (QA, 2026-09-18). The subject must be
#: the user and the verb the attribute.
_PROFILE_FACT_SHAPES = (
    (
        frozenset({"employer", "company", "job", "work", "workplace"}),
        re.compile(
            r"^the user\s+(?:(?:currently|now|still)\s+)?"
            r"(?:(?:works|worked|is\s+working)\s+(?:at|for)|is\s+employed\s+(?:at|by|with))\b"
            r"|^the user['’]s\s+(?:(?:current|new)\s+)?(?:employer|company|workplace|job)\s+is\b",
            re.I,
        ),
    ),
    (
        frozenset({"home", "address", "city", "live"}),
        re.compile(
            r"^the user\s+(?:(?:currently|now|still)\s+)?"
            r"(?:lives|lived|resides|is\s+(?:based|living))\s+in\b"
            r"|^the user['’]s\s+(?:(?:current|home)\s+)?(?:home|address|city|hometown)\s+is\b",
            re.I,
        ),
    ),
    (
        frozenset({"name"}),
        re.compile(
            r"^the user['’]s\s+(?:(?:first|full|last)\s+)?name\s+is\b"
            r"|^the user\s+(?:is\s+(?:called|named)|goes\s+by|"
            r"(?:wants|prefers|likes)\s+to\s+be\s+(?:called|addressed))\b",
            re.I,
        ),
    ),
)

#: Words that cannot tell one saved fact from another. Every fact starts
#: "The user…", and a request is full of "told", "about", "saved".
_ERASE_STOPWORDS = _MATCH_STOPWORDS | frozenset(
    """the and for are was were you your yours i'm i've i'd i'll not but has had
    his her hers its our ours their theirs them they she him who how why any all
    can will would could should into onto over under some every each user users
    mine myself one out off too now yet did does doing done got get let may
    might must shall than then via it's mentioned shared wrote noted kept
    learned learnt file""".split()
)

#: A clause: the unit a "forget" and its "don't forget" exception are judged
#: in, for the one thing that is still judged loosely: whether a message
#: MENTIONS forgetting, which stops it writing memory. Judged over the whole
#: message, a reminder in the second sentence ("…Don't forget I like spicy
#: food though") cancelled the erasure in the first, and the extractor's
#: negated rewrite was written instead (QA, 2026-09-18).
_CLAUSE_RE = re.compile(r"[^.!?;,\n]+")


def facts_block(facts: List[dict]) -> Optional[str]:
    """Render saved facts as the system block, or None when there are none."""
    if not facts:
        return None
    lines = [FACTS_HEADER]
    used = len(FACTS_HEADER)
    for f in facts:
        line = f"- {f['fact']}"
        if used + len(line) > _BLOCK_MAX_CHARS:
            break
        lines.append(line)
        used += len(line)
    return "\n".join(lines) if len(lines) > 1 else None


def parse_extraction(raw: str) -> dict:
    """The extractor's JSON, tolerantly parsed. {} when unusable."""
    if not raw:
        return {}
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    raw_add = data.get("add")
    add = [
        _flatten(f)
        for f in (raw_add if isinstance(raw_add, list) else [])
        if isinstance(f, str) and f.strip()
    ]
    raw_replace = data.get("replace")
    replace = []
    for item in raw_replace if isinstance(raw_replace, list) else []:
        if not isinstance(item, dict):
            continue
        fact = item.get("fact")
        try:
            fact_id = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        if isinstance(fact, str) and fact.strip():
            replace.append({"id": fact_id, "fact": _flatten(fact)})
    raw_remove = data.get("remove")
    remove = []
    for item in raw_remove if isinstance(raw_remove, list) else []:
        try:
            remove.append(int(item))
        except (TypeError, ValueError):
            continue
    add = [f for f in add if is_durable(f)]
    replace = [item for item in replace if is_durable(item["fact"])]
    return {"add": add, "replace": replace, "remove": remove}


def is_durable(fact: str) -> bool:
    """False for a one-off task request dressed as a fact.

    "The user is asking about all movie names in the Spider-Man franchise" is
    what the person wanted once, not who they are; saved, it comes back as a
    to-do list the next time they ask what the assistant remembers. A standing
    preference stays, however it is phrased.
    """
    text = " ".join((fact or "").split())
    if not text:
        return False
    if _DURABLE_PREFERENCE_RE.search(text):
        return True
    return _TRANSIENT_FACT_RE.match(text) is None


def own_words(text: str) -> Optional[str]:
    """The part of a message that is the PERSON speaking, or None.

    Fenced blocks and quoted lines are material, not speech, and come out.
    What is left is the person's own words — unless it is longer than a
    self-disclosure can plausibly be, in which case the message is a pasted
    document (a CV, a contract, an email thread) and none of it is a fact
    about the person. The composer folds a paste inline with no marker
    (frontend/lib/pasted.ts), so length and layout are the only signals there
    are: a multi-line message with an ALL-CAPS name banner or an email line is
    a pasted document whatever its length.
    """
    body = _FENCE_RE.sub(" ", text or "")
    body = _QUOTED_LINE_RE.sub(" ", body)
    body = body.strip()
    if not body:
        return None
    if len(body) > settings.memory_self_disclosure_max_chars:
        return None
    if "\n" in body and _DOCUMENT_BANNER_RE.search(body):
        return None
    return body


def ungrounded_in(fact: str, *sources: Optional[str]) -> Optional[str]:
    """The first number or name in `fact` that no source contains, or None.

    The extractor is a prompt, and prompts invent. A person who says "I left
    Northwind Freight, I'm at Halcyon Rail now" has not said how big Halcyon
    Rail is — but the extractor rewrote the old employer's headcount onto the
    new one and the next chat answered "your engineering team at Halcyon Rail
    has 29 engineers". A fact may only carry numbers and names that were
    actually said.
    """
    return _ungrounded_number(fact, *sources) or _ungrounded_name(fact, *sources)


def _ungrounded_number(fact: str, *sources: Optional[str]) -> Optional[str]:
    hay = " ".join(s or "" for s in sources).lower()
    for number in _NUMBER_RE.findall(fact or ""):
        if number not in hay:
            return number
    return None


def _ungrounded_name(fact: str, *sources: Optional[str]) -> Optional[str]:
    hay = " ".join(s or "" for s in sources).lower()
    for match in _NAME_RE.finditer(fact or ""):
        token = match.group(0)
        if match.start() == 0 or token in _NAME_STOPWORDS:
            continue
        if token.lower() not in hay:
            return token
    return None


def strip_ungrounded_suffixes(fact: str, *sources: Optional[str]) -> str:
    """`fact` without the corporate suffixes no source contains, when they
    follow an organisation name every word of which a source contains.
    Anything else is left for `ungrounded_in` to judge."""
    hay = " ".join(s or "" for s in sources).lower()

    def cut(match: "re.Match[str]") -> str:
        name = match.group("name")
        if any(word.lower() not in hay for word in name.split()):
            return match.group(0)
        chain = _CORPORATE_SUFFIX_RE.sub(
            lambda w: w.group(0) if w.group("word").lower() in hay else "",
            match.group("chain"),
        )
        return match.group("lead") + name + chain

    return _CORPORATE_SUFFIX_CHAIN_RE.sub(cut, fact or "")


def _flatten(text: str) -> str:
    """One whitespace-normalized line, capped. A fact with embedded newlines
    would escape its bullet in facts_block and read as fresh top-level system
    lines — a durable prompt-injection channel for text the extractor was fed.
    Same normalization memory_api applies to manual adds."""
    return " ".join((text or "").split())[:_FACT_MAX_CHARS]


def _normalized(text: str) -> str:
    return " ".join((text or "").lower().split()).rstrip(".")


def _content_words(text: str) -> set:
    return {
        w
        for w in re.findall(r"[a-z][a-z'’-]{3,}", (text or "").lower())
        if w not in _MATCH_STOPWORDS
    }


def _erasure_spans(text: str) -> List[tuple]:
    """(start, end) of each clause of `text` that mentions forgetting: a
    forget verb, and not a reminder or a confession ("don't forget …", "I
    always forget …") in the same clause. Such a message writes no memory;
    whether it may DELETE any is `_erasure_requests`."""
    return [
        m.span()
        for m in _CLAUSE_RE.finditer(text or "")
        if _FORGET_RE.search(m.group(0)) and not _NOT_A_FORGET_RE.search(m.group(0))
    ]


def _erasure_requests(text: str) -> List[tuple]:
    """Each thing `text` asks the assistant to forget, as (clause, named,
    matchable): the words that name the fact, an appositive name the fact
    must contain ("my employer, Cognitiv"), and whether the id-less
    fallback may match on the clause at all. [] when the message asks for
    no deletion — and then nothing may be deleted, whatever the extractor
    proposes."""
    # Quoted and reported speech needs no rule of its own: a sentence that
    # opens with a quote mark or "She said" is not a request, and one that
    # a quote or a "My landlord wrote:" line splits off stands beside a
    # sentence that is neither a request nor a companion.
    body = text or ""
    if _RETRACTION_RE.search(body):
        return []
    asks: List[tuple] = []
    for sentence in _ERASE_SENTENCE_SPLIT_RE.finditer(body):
        text_ = sentence.group(0)
        if not re.search(r"\w", text_):
            continue
        ask = _erasure_request(text_, _END_PUNCT_RE.match(body, sentence.end()).group(0))
        if ask is not None:
            asks.append(ask)
        elif not _ERASE_COMPANION_RE.fullmatch(text_) or (
            # RV3: a reminder that sets the thing aside for a task or a later moment
            _CLAUSE_TEMPORARY_RE.search(text_) or _CLAUSE_DOCUMENT_RE.search(text_)
            or re.search(r"\bremember\s+(?:that\s+)?i\s+(?:told|said|asked|mentioned|wrote)\b", text_, re.I)
        ):
            return []
    return asks


def _erasure_request(sentence: str, terminator: str) -> Optional[tuple]:
    match = _ERASE_SENTENCE_RE.fullmatch(sentence.strip())
    if match is None:
        return None
    # "Forget my address? Never." — only "can you …?" may end in a question mark
    if "?" in terminator and not match.group("ask"):
        return None
    # "Would you forget my name?" is as often "would you ever…?"; with a
    # "please" it is a request
    if (match.group("ask") or "").lower().startswith("would") and not re.search(
        r"\b(?:please|pls|plz|kindly)\b", sentence, re.I
    ):
        return None
    obj = match.group("object")
    for form in _DELETE_OBJECTS if match.group("delete") else _FORGET_OBJECTS:
        found = form.fullmatch(obj)
        if found is None:
            continue
        if form is _OBJ_PROFILE_RE:
            return (found.group("clause"), found.group("named"), True)
        if form is _OBJ_MY_RE:
            words = found.group("np").lower().split()
            if any(w in _NOT_A_NOUN_PHRASE for w in words):
                return None
            return (found.group("np"), None, False)
        clause = (found.group("clause") or "").strip()
        if clause and (
            _CLAUSE_CONTINUES_RE.search(clause)
            or _CLAUSE_TEMPORARY_RE.search(clause)
            or _CLAUSE_DOCUMENT_RE.search(clause)
            or _NEVER_MIND_RE.fullmatch(clause)
        ):
            return None
        return (clause, None, True)
    return None


def _asked_for(asks: List[tuple], fact: str) -> bool:
    """Whether a request lets the extractor delete `fact`. A request that
    names something ("forget my address, Detective") only reaches a fact
    that contains the name: role-play addresses someone, and the appositive
    reading must not hand the extractor every row."""
    return any(
        (named is None or named.lower() in (fact or "").lower())
        # RV3: a bare "forget my <noun phrase>" reaches only a fact holding every word it
        # names; "Forget my previous question." / "Forget my job tonight." reach nothing
        and (matchable or _erase_words(clause) <= _erase_words(fact))
        for clause, named, matchable in asks
    )


def _stem(word: str) -> str:
    word = re.sub(r"['’]s$", "", word)
    for suffix in ("ing", "ed", "es", "s", "e"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def _erase_words(text: str) -> set:
    return {
        _stem(w)
        for w in re.findall(r"[a-z0-9][a-z0-9'’-]*", (text or "").lower())
        if len(w) >= 3 and w not in _ERASE_STOPWORDS
    }


def _facts_named(asks: List[tuple], existing: List[dict]) -> List[int]:
    """The ids of the saved facts the requests name, when the extractor
    named none.

    The model is asked for the id and usually gives one; when it does not,
    "forget that I'm vegetarian" still points at the row that says
    vegetarian, and "forget my employer" at the row that states where the
    person works. Each named thing must pin down exactly ONE fact, or
    nothing is deleted for it — deleting the wrong memory is worse than
    deleting none. Every word the request names must be in the fact:
    "forget that my sister moved to Delhi" does not delete "The user's
    sister lives in Mumbai".
    """
    ids: List[int] = []
    for clause, named, matchable in asks:
        if not matchable or not clause:
            continue
        groups: List[List[int]] = []
        if _OBJ_PROFILE_RE.fullmatch(clause):
            for noun in _PROFILE_NOUN_RE.finditer(clause):
                said = (noun.group("noun") or noun.group("verb") or noun.group("who")).lower()
                for nouns, shape in _PROFILE_FACT_SHAPES:
                    if said in nouns:
                        groups.append([
                            f["id"]
                            for f in existing
                            if shape.match(" ".join((f["fact"] or "").split()))
                            and (not named or named.lower() in (f["fact"] or "").lower())
                        ])
        else:
            words = _erase_words(clause)
            if words:
                groups.append(
                    [f["id"] for f in existing if words <= _erase_words(f["fact"])]
                )
        for hits in groups:
            if len(hits) == 1 and hits[0] not in ids:
                ids.append(hits[0])
    return ids


async def remember_after_route(
    gate: "asyncio.Future[bool]",
    user_id: int,
    user_text: str,
    conversation_id: Optional[str],
    *,
    attachments: bool = False,
    complete=None,
) -> List[dict]:
    """`remember_from_message`, held until the turn knows its route.

    The chat turn starts extraction the moment it has the message, before
    it knows whether the message is a request for a FILE. A request for a
    file is not a fact about the person ("make me a PDF of the audit" is a
    task), yet the extractor is a prompt and prompts are not guarantees —
    so an artifact turn must never reach the model with it, let alone the
    `user_facts` table (CONTRACT-2 §8). Cancelling the task is not enough:
    the extractor's first awaits are thread hops and it often FINISHES
    before the artifact intent is decided. The turn therefore resolves
    `gate` once the route is known — True to extract as before (still
    concurrent with the answer, which starts after the same decision),
    False to do nothing — and every path out of the turn resolves it, so
    the task never waits forever."""
    if not await gate:
        return []
    return await remember_from_message(
        user_id,
        user_text,
        conversation_id,
        attachments=attachments,
        complete=complete,
    )


async def remember_from_message(
    user_id: int,
    user_text: str,
    conversation_id: Optional[str],
    *,
    attachments: bool = False,
    complete=None,
) -> List[dict]:
    """Extract and store durable facts from one user message.

    Returns the facts that were added, rewritten or deleted (empty when
    none); a deleted one carries `"deleted": True`. `complete` defaults to
    llm.router_chat_completion — injectable for tests.

    WHAT MAY BECOME A FACT (2026-09-18). Only the person's own words about
    themselves. A turn that carries an ATTACHMENT is a turn about a document,
    so it writes no memory at all, and a message long enough to be a pasted
    document is treated the same way (`own_words`). What the extractor then
    proposes is checked rather than trusted: nothing transient (`is_durable`),
    no number or name the message did not contain (`ungrounded_in`), and a
    message that asks to FORGET something may only delete.
    """
    if not settings.fact_extraction_enabled:
        return []
    # A document the person uploaded is third-party content: it is material
    # for the turn, never a statement the person made about themselves.
    if attachments:
        return []
    text = own_words(user_text)
    if not text or len(text) < _MESSAGE_MIN_CHARS:
        return []
    # Two questions, answered separately. Does the message MENTION
    # forgetting? Then it writes nothing (rule 2). Does the person ASK for
    # a deletion? Only then may anything be deleted, by the extractor's id
    # or by the fallback.
    asks = _erasure_requests(text)
    forget_request = bool(asks) or bool(_erasure_spans(text))
    try:
        existing = await db.run_in_thread(
            db.list_user_facts, user_id, settings.memory_max_facts
        )
        saved_lines = "\n".join(
            f"[{f['id']}] {f['fact']}" for f in existing
        ) or "(none yet)"
        if complete is None:
            complete = llm.router_chat_completion
        raw = await complete(
            [
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Saved facts:\n{saved_lines}\n\n"
                        f"User's newest message:\n{text[:_MESSAGE_MAX_CHARS]}"
                    ),
                },
            ],
            max_tokens=400,
        )
        ops = parse_extraction(raw)
        if not ops:
            return []
        by_id = {f["id"]: f for f in existing}
        known = {_normalized(f["fact"]): f["id"] for f in existing}
        stored: List[dict] = []
        added = 0
        # A delete happens because the PERSON asked for one. The extractor
        # may point at the row, but a "remove" on any other message — an
        # ordinary one, or "Did you forget my employer?" — would let a prompt
        # quietly drop somebody's memory.
        removals = [
            i
            for i in ops.get("remove", [])
            if i in by_id and _asked_for(asks, by_id[i]["fact"])
        ]
        if forget_request:
            # An erasure request never writes memory. Left to itself the
            # extractor answers "please forget that I'm vegetarian" with a
            # REPLACE — "The user is not vegetarian" — which is the erased
            # thing, kept forever and stated as the person's own words.
            ops["add"] = []
            ops["replace"] = []
            if asks and not removals:
                removals = _facts_named(asks, existing)
        for fact_id in removals:
            deleted = await db.run_in_thread(db.delete_user_fact, user_id, fact_id)
            if deleted:
                row = dict(by_id[fact_id])
                row["deleted"] = True
                stored.append(row)
                known.pop(_normalized(row["fact"]), None)
        for item in ops.get("replace", []):
            row = by_id.get(item["id"])
            if row is None:  # an id this user does not own, or already gone
                continue
            # A rewrite may re-use the names already in the fact it rewrites,
            # but every NUMBER must come from this message — carrying the old
            # employer's headcount onto the new employer is exactly the
            # invention this guards.
            item["fact"] = strip_ungrounded_suffixes(item["fact"], text, row["fact"])
            missing = _ungrounded_number(item["fact"], text) or _ungrounded_name(
                item["fact"], text, row["fact"]
            )
            if missing is not None:
                log.info("fact rewrite dropped: %r not in the message", missing)
                continue
            updated = await db.run_in_thread(
                db.update_user_fact,
                user_id,
                item["id"],
                item["fact"],
                source="stated",
                source_excerpt=text,
            )
            if updated:
                known[_normalized(item["fact"])] = updated["id"]
                stored.append(updated)
        for fact in ops.get("add", []):
            fact = strip_ungrounded_suffixes(fact, text)
            if _normalized(fact) in known:  # extractor re-suggested a saved fact
                continue
            missing = ungrounded_in(fact, text)
            if missing is not None:
                log.info("fact dropped: %r not in the message", missing)
                continue
            # Replaces rewrite existing rows; only genuine adds consume slots.
            if len(existing) + added >= settings.memory_max_facts:
                break
            created = await db.run_in_thread(
                db.add_user_fact,
                user_id,
                fact,
                conversation_id,
                source="stated",
                source_excerpt=text,
            )
            known[_normalized(fact)] = created["id"]
            stored.append(created)
            added += 1
        return stored
    except Exception:
        log.warning("fact extraction failed", exc_info=True)
        return []
