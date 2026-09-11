"""Is this turn asking for a FILE? — the artifact-intent gate.

    "Create a professional PDF about this."       create   (explicit pdf)
    "Give this to me as a document."              create   (kind: document)
    "Make slide 4 shorter."                       edit     (the deck in this conversation)
    "Convert the previous document to PDF."       convert  (explicit pdf)
    "Export the previous answer as PDF."          export   (the last assistant turn → a file)
    "What is a PDF?" / "Can a PDF contain video?" none     (a question ABOUT the format)
    "Show me Python code that reads a DOCX."      none     (code, not a file)

DETERMINISTIC FIRST. Every example in the product brief is decided by the
rules here, and the rules are tested one by one — a request that plainly
asks for a file must never depend on a model's mood. The rules are
conservative in the other direction too: a format word in ordinary
conversation ("should I use Excel or a database?") is not a request.

A CLASSIFIER ONLY FOR THE GENUINELY AMBIGUOUS. `decide()` returns
`ambiguous=True` for the narrow band where a creation verb and a document
noun are both present but so is a question shape it cannot read — the chat
engine may then ask a small strict-JSON classifier (`classify_hook`) and,
absent one, treats the turn as NOT an artifact request: a text answer to a
request for a file is a smaller failure than a file nobody asked for.

Explicit intent always wins: a named format is used, a named target
("version 1", "the deck") is used, and "it" resolves to the most recent
artifact in the conversation unless the words name another.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence

from . import formats as F

Action = str  # "create" | "edit" | "convert" | "export" | "none"

# ------------------------------------------------------------ vocabulary --

_CREATE_VERBS = (
    r"(?:make|create|generate|build|write|draft|prepare|produce|compile|assemble|"
    r"put together|design|develop|export|save|download|print|render|give me|send me|"
    r"i need|we need|i want|we want|i'd like|we'd like|i would like|we would like|looking for|"
    r"turn(?:\s+\w+){0,4}?\s+into|convert(?:\s+\w+){0,4}?\s+(?:to|into))"
)
_ARTIFACT_NOUNS = (
    r"(?:pdf|docx|word(?:\s+(?:document|file|doc))?|powerpoint|pptx|presentation|slides?|"
    r"slide ?deck|deck|pitch ?deck|excel|xlsx|spreadsheet|workbook|tracker|document|doc|"
    r"report|sop|standard operating procedure|memo|brief|one[- ]pagers?|one[- ]page|proposal|"
    r"policy|letter|handout|write[- ]?up|whitepaper|white paper|summary document|"
    r"deliverables?|files?|dashboard)"
)
_FORMAT_WORD = r"(?:pdf|docx|word|powerpoint|pptx|excel|xlsx|spreadsheet|slides?|deck|presentation|document|report)"

#: A creation verb, then an artifact noun within six words. The noun alone
#: is never enough: "the report from finance said…" is conversation.
_CREATE_RE = re.compile(
    rf"\b{_CREATE_VERBS}\b(?:\W+\w+){{0,6}}?\W+{_ARTIFACT_NOUNS}\b",
    re.I,
)
#: "as a PDF" / "in Word" / "to Excel" — the deliverable named as a form.
_AS_FORMAT_RE = re.compile(rf"\b(?:as|in|into|to)\s+(?:an?\s+|the\s+)?(?:{_FORMAT_WORD})\b", re.I)
#: "the best format" / "best deliverable" / "all (the) (required|final) files|deliverables"
_BEST_OR_ALL_RE = re.compile(
    r"\b(?:best\s+(?:format|deliverable|output)|all\s+(?:the\s+)?(?:required|final|necessary)?\s*(?:files|deliverables|documents|outputs))\b",
    re.I,
)

#: A question ABOUT a format, not a request for one.
_ABOUT_FORMAT_RE = re.compile(
    rf"^\s*(?:what(?:'s| is| are| does)|why|how (?:do|does|did|is|are|can|would)|explain|describe|"
    rf"tell me (?:about|how)|define|is (?:a|an|the)|are|can (?:a|an|the)|could (?:a|an)|does (?:a|an|the)|"
    rf"should (?:i|we)|which (?:is|one)|difference between|compare)\b.*?\b{_FORMAT_WORD}s?\b",
    re.I,
)
#: "show me code that…", "write a script that reads…" — the artifact is code.
_CODE_RE = re.compile(r"\b(?:code|script|snippet|function|program|regex|query|sql|python|javascript|typescript|bash)\b", re.I)
#: An imperative edit at the start of a short message — "Add our logo.",
#: "Use a more formal tone." — is about the latest artifact when there is one.
_IMPERATIVE_EDIT_RE = re.compile(
    r"^\s*(?:please\s+)?(?:add|insert|include|remove|delete|drop|change|update|rename|retitle|shorten|expand|"
    r"rewrite|reword|revise|tighten|trim|fix|tweak|adjust|use (?:a )?(?:more|less)|make\b.{1,40}?\b(?:shorter|longer|simpler|clearer|concise|formal|professional))\b",
    re.I,
)
#: Polite imperatives are requests: "can you make…", "could you create…".
_POLITE_RE = re.compile(r"^\s*(?:can|could|would|will|please|pls|kindly)\b\s*(?:you|u)?\s*(?:please\s+)?", re.I)

# --- follow-ups --------------------------------------------------------------

_REFERENCE_RE = re.compile(
    r"\b(?:it|this|that|the (?:previous|last|same|current|existing|earlier) (?:one|file|document|doc|deck|presentation|report|spreadsheet|workbook|brief|proposal|version)|"
    r"the (?:file|document|doc|deck|presentation|report|spreadsheet|workbook|brief|proposal|sop|memo|pdf|docx|pptx|xlsx)|"
    r"(?:slide|page|sheet|section|chapter|tab)\s+\d+|the (?:title|intro|introduction|conclusion|summary|chart|table|cover|tone|font|logo))\b",
    re.I,
)
_EDIT_VERBS_RE = re.compile(
    r"\b(?:make\b.{1,40}?\b(?:shorter|longer|briefer|simpler|clearer|more \w+|less \w+|formal|professional|concise|punchier)|"
    r"shorten|lengthen|expand|trim|cut|tighten|rewrite|reword|rephrase|revise|edit|update|change|modify|tweak|adjust|fix|"
    r"rename|retitle|add|insert|include|remove|delete|drop|replace|swap|move|reorder|restructure|"
    r"use (?:a )?(?:more|less)|go back to|revert to|restore|redo)\b",
    re.I,
)
#: A conversion names the SAME content in another form: "convert it to",
#: "export as", "also as", "a Word version". "Turn this into an Excel
#: tracker" and "make it a deck" are creation verbs and stay with create —
#: the content is the conversation's, and the engine makes a new artifact
#: of the requested kind rather than refusing to convert the latest one.
_CONVERT_RE = re.compile(
    rf"\b(?:convert|export|save|also|too|as well|another|a copy)\b.*?\b(?:as|to|into|in)\s+(?:an?\s+|the\s+)?{_FORMAT_WORD}\b"
    rf"|\b(?:also|too)\s+(?:as|in)\s+(?:an?\s+)?{_FORMAT_WORD}\b"
    rf"|\b(?:{_FORMAT_WORD})\s+(?:version|copy|too|as well)\b",
    re.I,
)
_PREVIOUS_ANSWER_RE = re.compile(
    r"\b(?:(?:the|your|that) (?:previous|last|above|earlier|prior) (?:answer|reply|response|message|summary|explanation)|"
    r"(?:what|everything) you (?:just )?(?:said|wrote|explained)|your answer|that answer|this answer|the answer above|the above)\b",
    re.I,
)
_VERSION_RE = re.compile(r"\b(?:version|v)\s*(\d{1,3})\b", re.I)


@dataclass
class ArtifactIntent:
    action: Action
    #: Formats the person named, in order. Empty means "policy decides".
    formats: List[str] = field(default_factory=list)
    #: For edit/convert: what the person pointed at.
    reference: str = "none"          # none | latest | previous_answer | named
    reference_hint: str = ""         # "deck", "slide 4", "version 1", a title fragment
    version: Optional[int] = None    # "go back to version 1"
    rule: str = "none"
    ambiguous: bool = False
    #: The instruction with the reference words left in — the composer
    #: needs "make slide 4 shorter" verbatim.
    instruction: str = ""

    @property
    def wants_file(self) -> bool:
        return self.action != "none"


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def decide(
    text: str,
    *,
    has_artifacts: bool = False,
    artifact_hints: Sequence[str] = (),
    has_assistant_answer: bool = False,
) -> ArtifactIntent:
    """Decide from the words alone; nothing here calls a model.

    `has_artifacts`: the conversation already holds at least one artifact
    (so "make it shorter" can mean the file). `artifact_hints`: short labels
    of those artifacts (kind words / titles) so "the deck" can be matched to
    one. `has_assistant_answer`: there is a previous assistant turn to
    export.
    """
    raw = _clean(text)
    if not raw:
        return ArtifactIntent("none", rule="empty")
    low = raw.lower()
    explicit = F.explicit_formats(low)

    # 1. Questions ABOUT a format, code requests: not a file. Checked first,
    #    because "explain how to create a PDF in Python" has a creation verb.
    if _CODE_RE.search(low) and not _AS_FORMAT_RE.search(low):
        return ArtifactIntent("none", formats=explicit, rule="code")
    if _ABOUT_FORMAT_RE.search(low) and not _POLITE_RE.match(low):
        return ArtifactIntent("none", formats=explicit, rule="about-format")

    # 2. Follow-ups on an existing artifact.
    if has_artifacts:
        version = _VERSION_RE.search(low)
        if version and re.search(r"\b(?:go back|revert|restore|use|return|switch)\b", low):
            return ArtifactIntent("edit", formats=explicit, reference="named", reference_hint=f"version {version.group(1)}",
                                  version=int(version.group(1)), rule="restore-version", instruction=raw)
        if _CONVERT_RE.search(low) and explicit:
            return ArtifactIntent("convert", formats=explicit, reference=_which(low, artifact_hints),
                                  reference_hint=_hint(low, artifact_hints, exclude=_target_words(explicit)),
                                  rule="convert", instruction=raw)
        if _EDIT_VERBS_RE.search(low) and (_REFERENCE_RE.search(low) or _mentions_hint(low, artifact_hints)):
            return ArtifactIntent("edit", formats=explicit, reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints),
                                  rule="edit", instruction=raw)
        if _IMPERATIVE_EDIT_RE.match(low) and len(low.split()) <= 12:
            return ArtifactIntent("edit", formats=explicit, reference=_which(low, artifact_hints), reference_hint=_hint(low, artifact_hints),
                                  rule="edit-imperative", instruction=raw)
        # "Also as PDF" with nothing else said.
        if explicit and re.match(r"^\s*(?:also|and|plus|too)?\s*(?:as|in)\s+(?:an?\s+)?\w+\s*(?:too|as well|please)?\s*[.!]?\s*$", low):
            return ArtifactIntent("convert", formats=explicit, reference="latest", rule="convert-short", instruction=raw)

    # 3. Exporting the previous answer as a file.
    if has_assistant_answer and _PREVIOUS_ANSWER_RE.search(low) and (explicit or _AS_FORMAT_RE.search(low) or _CREATE_RE.search(low)):
        return ArtifactIntent("export", formats=explicit, reference="previous_answer", rule="export-answer", instruction=raw)

    # 4. Creation.
    if _CREATE_RE.search(low) or _AS_FORMAT_RE.search(low) or _BEST_OR_ALL_RE.search(low):
        if "?" in raw and not _POLITE_RE.match(low) and not explicit:
            # "Would a report help here?" — a creation verb, a document noun,
            # a question, no format: the one shape the rules cannot read.
            return ArtifactIntent("none", formats=explicit, rule="ambiguous", ambiguous=True, instruction=raw)
        return ArtifactIntent("create", formats=explicit, rule="create", instruction=raw)

    return ArtifactIntent("none", formats=explicit, rule="no-request")


def _mentions_hint(low: str, hints: Sequence[str]) -> bool:
    return any(h and h.lower() in low for h in hints)


def _which(low: str, hints: Sequence[str]) -> str:
    return "named" if _mentions_hint(low, hints) or re.search(r"\b(?:the (?:deck|presentation|document|report|spreadsheet|workbook|brief|proposal|sop|memo))\b", low) else "latest"


def _hint(low: str, hints: Sequence[str], *, exclude: Sequence[str] = ()) -> str:
    """What the person pointed at: a known artifact label, else the artifact
    noun they used, else the part ("slide 4", "the title"). `exclude` holds
    the words that name the TARGET of a conversion, which are not a hint."""
    for h in hints:
        if h and h.lower() in low:
            return h
    skip = {w.lower() for w in exclude}
    for m in re.finditer(r"\b(?:the )?(deck|presentation|document|report|spreadsheet|workbook|brief|proposal|sop|memo|pdf|docx|pptx|xlsx)\b", low):
        if m.group(1) not in skip:
            return m.group(1)
    m = re.search(r"\b((?:slide|page|sheet|section)\s+\d+)\b", low)
    if m:
        return m.group(1)
    m = re.search(r"\bthe (title|intro|introduction|conclusion|summary|chart|table|cover|tone|font|logo)\b", low)
    return m.group(1) if m else ""


_FORMAT_WORDS_FOR = {"pdf": ("pdf",), "docx": ("docx", "word"), "pptx": ("pptx", "powerpoint"), "xlsx": ("xlsx", "excel")}


def _target_words(explicit: Sequence[str]) -> List[str]:
    out: List[str] = []
    for f in explicit:
        out.extend(_FORMAT_WORDS_FOR.get(f, ()))
    return out


# ------------------------------------------------------------- classifier --

ClassifyHook = Callable[[str], Awaitable[Optional[ArtifactIntent]]]


async def decide_with_hook(
    text: str,
    hook: Optional[ClassifyHook],
    **kw,
) -> ArtifactIntent:
    """The rules, then — only for the ambiguous band — the hook (a strict
    JSON classifier the engine provides). No hook, or a hook that fails,
    means no artifact: the text answer is the safe default."""
    intent = decide(text, **kw)
    if not intent.ambiguous or hook is None:
        return intent
    try:
        verdict = await hook(text)
    except Exception:  # noqa: BLE001 — a classifier outage is a text answer
        return intent
    if verdict is None:
        return intent
    verdict.rule = f"classifier:{verdict.rule or 'model'}"
    return verdict
