"""What the platform can do with files — said to every answering model, and
checked in what it answered.

THE INCIDENT (2026-09-15, paraphrased). After a long audit answer the person
asked for it "in docs … a classy format … a dox file". The chat model, whose
prompt never said this platform makes files, answered that as an AI it cannot
create a .docx and pasted python-docx code. Two defences:

  CAPABILITY_LINE  appended to the chat, agent-synthesis, document, vision and
                   dataset prompts: the platform makes DOCX/PDF/XLSX/CSV/PPTX
                   files and charts; never deny it, never substitute library
                   code or copy-paste instructions; never claim a file is being
                   prepared (the chat model cannot start a job — only the
                   system shows a file card).
  denial_in()      the post-answer backstop's detector (main.py): a denial
                   of FILE creation, in English, Hindi, Gujarati or Hinglish.
                   A file noun must sit near the denial, so "I can't open that
                   link" and "I can't see the attachment clearly" are not
                   denials of this kind.

The intent gate (artifacts/intent.py) is the primary fix; this line keeps a
turn the gate missed from saying something false, and the backstop is
watched by its counters.
"""
from __future__ import annotations

import re
from typing import Pattern

CAPABILITY_LINE = (
    "This platform can create downloadable Word (DOCX), PDF, Excel (XLSX), CSV and PowerPoint files and charts from this "
    "conversation or an uploaded file. Do not say you cannot create or attach files, and do not give python-docx, openpyxl, "
    "matplotlib or copy-paste instructions as a substitute. Mention files only when the person asks for one; then tell them "
    "to ask directly, for example \"make this a Word document\". Never claim a file is being prepared unless the system has "
    "shown a file card."
)

#: The prompt suffix, with its separator — one string concatenation per prompt.
CAPABILITY_SUFFIX = "\n\n" + CAPABILITY_LINE


def capability_suffix() -> str:
    """The suffix, or nothing when this deployment has documents switched off
    (ARTIFACTS_ENABLED=false): the line must not promise files the gate will
    never make (verifier 2026-09-15)."""
    try:
        from ..config import settings

        return CAPABILITY_SUFFIX if bool(getattr(settings, "artifacts_enabled", True)) else ""
    except Exception:  # noqa: BLE001 — the prompt is built either way
        return CAPABILITY_SUFFIX

_FILE_NOUN_EN = (
    r"(?:files?|\.?docx|word\s+(?:documents?|files?|docs?)|documents?|pdfs?|\.?pdf|excel(?:\s+(?:files?|sheets?|workbooks?))?|"
    r"\.?xlsx|spreadsheets?|workbooks?|\.?csv|csv\s+files?|\.?pptx|powerpoint(?:\s+(?:files?|presentations?))?|slide\s+decks?|"
    r"attachments?|downloads?|downloadable)"
)
_CREATE_VERB_EN = (
    r"(?:create|generate|produce|make|send|attach|provide|export|save|output|deliver|share|upload|build|convert|give\s+you|"
    r"hand\s+you|email|render|write\s+(?:out\s+)?(?:to\s+)?)"
)
_DENY_EN = (
    r"(?:can(?:not|'t|’t)|can\s+not|(?:am|'m|’m)\s+(?:unable|not\s+able)\s+to|unable\s+to|not\s+able\s+to|"
    r"(?:do|does)\s*(?:n't|n’t|\s+not)\s+have\s+(?:the\s+)?(?:ability|capability|means|option|access)\s+to|"
    r"(?:have|has)\s+no\s+(?:way|ability|capability)\s+to|no\s+way\s+(?:for\s+me\s+)?to|not\s+possible\s+for\s+me\s+to|"
    r"(?:is|are)\s+(?:not\s+)?(?:beyond|outside)\s+my\s+(?:ability|capabilities)\s+to|won't\s+be\s+able\s+to)"
)
#: "I cannot (directly) create and send a binary .docx file", "As an AI I
#: can't generate files", "I'm unable to attach a PDF".
_EN_RE: Pattern[str] = re.compile(
    rf"\b{_DENY_EN}\s+(?:\w+\s+){{0,3}}?{_CREATE_VERB_EN}\b[^.!?\n]{{0,80}}?{_FILE_NOUN_EN}\b"
    rf"|\b{_FILE_NOUN_EN}\b[^.!?\n]{{0,40}}?\b(?:can(?:not|'t|’t)\s+be\s+(?:created|generated|attached|sent|downloaded)|"
    rf"(?:is|are)\s+(?:not\s+)?(?:something\s+)?(?:i\s+)?(?:can(?:not|'t)|am\s+unable\s+to)\s+(?:create|generate|attach|send))",
    re.I,
)
#: "as a text-based AI, I have no file system" — only with a file noun near.
_EN_TEXT_ONLY_RE: Pattern[str] = re.compile(
    rf"\b(?:text[- ]based\s+(?:ai|assistant|interface|model)|only\s+(?:produce|output|provide|generate)\s+text)\b[^.!?\n]{{0,120}}?{_FILE_NOUN_EN}\b"
    rf"|{_FILE_NOUN_EN}\b[^.!?\n]{{0,120}}?\btext[- ]based\s+(?:ai|assistant|interface|model)\b",
    re.I,
)
_FILE_NOUN_HI = r"(?:फ़ाइल|फाइल|पीडीएफ|वर्ड|डॉक्यूमेंट|दस्तावेज़?|एक्सेल|शीट|प्रेजेंटेशन|file|pdf|docx|word|excel)"
_HI_RE: Pattern[str] = re.compile(
    rf"{_FILE_NOUN_HI}[^।.!?\n]{{0,40}}?(?:नहीं\s+(?:बना|भेज|दे|तैयार\s+कर|अटैच\s+कर|जनरेट\s+कर)\s*(?:सकता|सकती|सकते|पाऊँगा|पाऊंगा|पाती|पाता)|"
    rf"बनाने\s+में\s+असमर्थ|भेजने\s+में\s+असमर्थ|बनाना\s+संभव\s+नहीं)"
)
_FILE_NOUN_GU = r"(?:ફાઇલ|ફાઈલ|પીડીએફ|વર્ડ|ડોક્યુમેન્ટ|દસ્તાવેજ|એક્સેલ|શીટ|file|pdf|docx|word|excel)"
_GU_RE: Pattern[str] = re.compile(
    rf"{_FILE_NOUN_GU}[^.!?\n]{{0,40}}?(?:(?:બનાવી|મોકલી|આપી|તૈયાર\s+કરી|જોડી)\s+શક(?:તો|તી|તા|ું)\s+નથી|બનાવવું\s+શક્ય\s+નથી|બનાવવામાં\s+અસમર્થ)"
)
_HINGLISH_RE: Pattern[str] = re.compile(
    r"\b(?:file|pdf|docx|doc|word\s+file|excel|sheet|ppt|document)\b[^.!?\n]{0,40}?\b(?:nahi|nahin|nhi)\s+"
    r"(?:bana|bhej|de|create\s+kar|generate\s+kar|send\s+kar|attach\s+kar|banavi|mokli)\s*(?:sakta|sakti|sakte|paunga|paungi|shakto|shakti)\b",
    re.I,
)


#: The substitute: library code that builds the file the person could have
#: been given ("Here is the Python code to generate a classy Word document").
_SUBSTITUTE_RE: Pattern[str] = re.compile(
    r"(?:from\s+docx\s+import|import\s+docx\b|python-docx|import\s+openpyxl|from\s+openpyxl|from\s+reportlab|"
    r"from\s+pptx\s+import|python-pptx|xlsxwriter|fpdf\b|docx\.Document\(|Workbook\(\))",
    re.I,
)
_SUBSTITUTE_PROSE_RE: Pattern[str] = re.compile(
    rf"\b(?:code|script)\b[^.!?\n]{{0,80}}?\b(?:generate|create|produce|build|make)s?\b[^.!?\n]{{0,60}}?{_FILE_NOUN_EN}\b"
    rf"|\b(?:copy|paste)\b[^.!?\n]{{0,60}}?\b(?:into|in)\s+(?:microsoft\s+)?(?:word|excel|google\s+docs|a\s+text\s+editor)\b",
    re.I,
)


def denial_in(text: str) -> bool:
    """Does this answer deny that the assistant can create/attach a FILE — or
    hand over library code / copy-paste steps in place of the file?"""
    t = (text or "")[:20000]
    if not t:
        return False
    if _EN_RE.search(t) or _EN_TEXT_ONLY_RE.search(t) or _HI_RE.search(t) or _GU_RE.search(t) or _HINGLISH_RE.search(t):
        return True
    if not _SUBSTITUTE_RE.search(t):
        return False
    # Library code for the file, introduced by prose that names the file
    # ("Here is your report in a classy DOCX format." then ```python from docx).
    fence = t.find("```")
    lead = t[: fence if 0 <= fence <= 1500 else 400]
    return bool(_SUBSTITUTE_PROSE_RE.search(t[:1500]) or re.search(rf"\b{_FILE_NOUN_EN}\b|\bformat\b", lead, re.I))


_OFFERS = {
    "en": "I can make this a file for you — just ask directly, for example \"make this a Word document\".",
    "hinglish": "Main isse file bana sakta hoon — seedha boliye, jaise \"isko Word document bana do\".",
    "hi": "मैं इसे फ़ाइल बना सकता हूँ — सीधे कहिए, जैसे \"इसे वर्ड डॉक्यूमेंट बना दो\"।",
    "gu": "હું આને ફાઇલ બનાવી શકું છું — સીધું કહો, જેમ કે \"આને વર્ડ ડોક્યુમેન્ટ બનાવો\".",
    "gujlish": "Hu aane file banavi shaku chu — sidhu kaho, jem ke \"aane Word document banavo\".",
}


def offer_line(language: str = "en") -> str:
    """One sentence that tells the person how to get the file."""
    return _OFFERS.get(language or "en", _OFFERS["en"])


__all__ = ["CAPABILITY_LINE", "CAPABILITY_SUFFIX", "capability_suffix", "denial_in", "offer_line"]
