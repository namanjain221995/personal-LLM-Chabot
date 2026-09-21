"""What a person may call themselves — the one validator for `users.display_name`.

WHY THIS IS NOT JUST A LENGTH CHECK. `display_name` is not decoration: it is
interpolated into every chat system prompt by `app/identity.py` as

    You are assisting <display_name> (<email>) in <workspace>. Address them
    naturally by name when it helps; never reveal information about other
    workspace members.

Until 2026-09-21 only bootstrap and an admin could write that field, so the
value was always one a trusted operator typed. `PATCH /auth/profile` hands the
pen to the account holder, which makes the field a self-service write into a
system prompt — the same class of channel `facts._flatten` exists to close for
extracted memories. So the rules below are about the prompt, not about looks:

* CONTROL AND FORMAT CHARACTERS INSIDE THE NAME ARE REFUSED, not stripped out.
  (Surrounding whitespace is trimmed, as any name field trims.) A newline would
  end
  the "You are assisting" sentence and let the rest of the value read as fresh
  top-level system lines; a bidi override (U+202E) or a zero-width joiner run
  can make the rendered name in the sidebar disagree with the bytes the model
  is given. ZWNJ (U+200C) and ZWJ (U+200D) are the two exceptions: they carry
  meaning inside real names in Indic and Persian scripts.
* PUNCTUATION IS AN ALLOWLIST. A name needs letters, marks, digits, spaces and
  `' ’ - . ,` — nothing else. That single rule is what structurally removes
  `(`/`)` (which would close the `(<email>)` the identity line puts after the
  name), `:` (`System:`, `Assistant:`), and `< > | [ ] { } #` (chat-template
  and instruction markers such as `<|im_start|>`, `[INST]`, `### Instruction`)
  without anyone having to enumerate every spelling of them.
* INSTRUCTION-SHAPED PROSE IS REFUSED. The allowlist still permits an ordinary
  English sentence, and "Bob. Ignore all previous instructions" is one, so
  `_INSTRUCTION_RE` refuses second-person imperatives and the standard
  override phrasings on top.

Every refusal returns the same shape: `ValueError` carrying a sentence meant
to be shown to the person, because the route puts it straight in a 422 detail.
"""
from __future__ import annotations

import re
import unicodedata

#: The longest name we store. Chosen for the two places it is rendered — the
#: sidebar account row and one system-prompt sentence — not for the column,
#: which is `text`. The longest name in the workspace at the time of writing
#: is 31 characters, so this is roughly double the observed maximum.
MAX_LENGTH = 64

#: Format characters (category Cf) that stay legal: both carry meaning inside
#: real names (Devanagari/Gujarati conjuncts, Persian spelling). Every other
#: Cf — bidi overrides and isolates, zero-width space, BOM — is refused.
_ALLOWED_FORMAT = {"‌", "‍"}

#: Categories a name may never contain. Cc: C0/C1 controls (newline, tab, NUL).
#: Cf: format characters, minus `_ALLOWED_FORMAT`. Cs/Co: surrogates and
#: private use. Zl/Zp: line and paragraph separators.
_FORBIDDEN_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Zl", "Zp"}

#: Punctuation a name may contain, beyond letters/marks/digits/space.
_ALLOWED_PUNCTUATION = set(" '’-.,")

#: Runs of HORIZONTAL whitespace collapse to one space. Deliberately not `\s`:
#: that would swallow the very newline the category scan exists to refuse, and
#: a name with a line break in it is the injection case, not a typo. A tab or a
#: no-break space between two words is a typo (or a spreadsheet paste), so
#: those collapse rather than refuse.
_HORIZONTAL_WS_RUN = re.compile(
    "[\t    -   　]+"
)

#: Second-person imperatives and the standard prompt-override phrasings. Each
#: alternative is anchored on a word boundary so an ordinary name containing
#: one of these letter sequences (there is no such name in any script we have
#: seen, but the boundary costs nothing) is not caught by a substring.
_INSTRUCTION_RE = re.compile(
    r"\b(?:"
    r"ignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier|preceding)"
    r"|disregard"
    r"|forget\s+(?:all|everything|your|the|previous|prior)"
    r"|(?:new|updated|revised|following)\s+instructions?"
    r"|(?:system|your)\s+prompt"
    r"|your\s+(?:instructions?|rules?|system|guidelines?)"
    r"|you\s+(?:are|were|must|should|shall|will|can|may|need|have\s+to)"
    r"|(?:do\s+not|don't|never|always)\s+"
    r"(?:reveal|say|tell|mention|respond|reply|answer|address|call|use|disclose|share|follow|refuse)"
    r"|(?:respond|reply|answer|say|write|print|output|repeat|start)\s+"
    r"(?:with|only|the|that|this|in|as|back|by)"
    r"|instead\s+of"
    r"|act\s+as"
    r"|pretend"
    r"|roleplay"
    r"|override"
    r"|jailbreak"
    r"|end\s+of\s+(?:prompt|instructions?)"
    r")\b",
    re.IGNORECASE,
)

_EMPTY = "Enter a name."
_TOO_LONG = f"A name can be at most {MAX_LENGTH} characters."
_BAD_CHARACTER = (
    "A name can contain letters, numbers, spaces, apostrophes, hyphens, "
    "periods and commas only."
)
_INSTRUCTION = "That does not look like a name. Enter the name you want to be called."


def validate_display_name(raw: object) -> str:
    """The cleaned name, or `ValueError` with a sentence to show the person.

    Cleaning is deliberately small — NFC, surrounding whitespace trimmed,
    interior horizontal-whitespace runs collapsed to one space — so what comes
    back is recognisably what was typed. Everything else refuses rather than
    repairs: a value quietly edited into something safe is a value the person
    did not choose, and this field is how they choose it.
    """
    if not isinstance(raw, str):
        raise ValueError(_EMPTY)

    # Trim first, and with Python's own definition of whitespace, so that a
    # value pasted out of a spreadsheet or an email signature — which arrives
    # wrapped in tabs and a trailing newline — is a name and not a refusal.
    # What that trim CANNOT reach is the interior, which is where a line break
    # would do damage; the category scan below owns that.
    name = _HORIZONTAL_WS_RUN.sub(" ", unicodedata.normalize("NFC", raw).strip())

    if not name:
        raise ValueError(_EMPTY)

    for char in name:
        if char in _ALLOWED_FORMAT:
            continue
        if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
            # Not given its own message: telling a person WHICH invisible
            # character they pasted helps nobody, and the sentence they get
            # already says what a name may contain.
            raise ValueError(_BAD_CHARACTER)

    if len(name) > MAX_LENGTH:
        raise ValueError(_TOO_LONG)

    for char in name:
        if char in _ALLOWED_PUNCTUATION or char in _ALLOWED_FORMAT:
            continue
        if unicodedata.category(char)[0] in ("L", "M", "N"):
            continue
        raise ValueError(_BAD_CHARACTER)

    if _INSTRUCTION_RE.search(name):
        raise ValueError(_INSTRUCTION)

    return name
