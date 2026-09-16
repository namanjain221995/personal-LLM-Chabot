"""The words people type when they want a file — and the words that only
look like it. The multilingual, typo-tolerant vocabulary of the intent gate.

    "just give it in docs, provide a dox file"   -> docx, a hand-over, "it"
    "isko docx me dedo"                          -> docx, _in_, _give_, _this_
    "इसे पीडीएफ में बदल दो"                        -> pdf, _in_, _convert_, _this_
    "summarize this pdf" (a PDF is attached)     -> negative: read_source
    "pdf kaise banate hai"                       -> negative: how_to
    "write python that makes a docx"             -> negative: code_request

WHY A NORMALISER. The rules in intent.py are English regexes that were tested
one example at a time. Rather than write a second rule set per language, the
text is first NORMALISED: case folded, zero-width characters removed, typos
mapped to the word they mean ("genrate" -> "generate", "dox" -> "docx"), and
Hindi, Gujarati, Hinglish and Gujlish words mapped to English tokens or to a
few reserved tokens the SOV rules read:

    _give_     a create / hand-over verb at the END of a clause (bana do, बनाओ,
               આપો), and SHOW after a chart word ("pie chart me dikhao") —
               SHOW anywhere else is a read of the thing, not a request for it
    _convert_  a conversion verb after a destination (badal do, बदल दो, ફેરવો)
    _in_       a postposition naming a destination (me, में, માં)
    _this_     a bare reference to what came before (isko, इसे, આને)

The ORIGINAL text is never replaced: the composer gets the person's words.

NEGATIVE SHAPES. A format word in a message very often names the SOURCE, not
the output: "summarize this pdf" with a PDF attached, "how do I convert word
to pdf", "the pdf looks good", "write python that makes a docx". The
negative pass classifies each clause; the message is negative only when a
clause is negative-shaped and no other clause is a plain request, so
"thanks! now make it a pdf" is still a request.

COST. Every function is pure (no I/O), every pattern is compiled at import,
and no pattern nests a quantifier over a word gap: fast_lane.py calls the
rules synchronously on the event loop for every small-talk candidate.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import List, Literal, Optional, Pattern, Sequence, Tuple

#: A letter of any script this module reads, for boundaries: Python's `\b`
#: puts a boundary inside a Devanagari word at every vowel sign or virama
#: (they are not `\w`), so "डॉक" would match inside "डॉक्यूमेंट".
#: The danda and double danda (U+0964/5) end a sentence; they are not letters.
_L = r"0-9A-Za-z_\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF"
_B = rf"(?<![{_L}])"
_E = rf"(?![{_L}])"
#: Gujarati writes postpositions attached to the noun ("પીડીએફમાં").
_GU_SUFFIX = r"(?P<suf>માં|મા|ને|નો|ની|નું|ના)?"

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")

NegativeShape = Literal["read_source", "how_to", "trivia", "feedback", "code_request"]
Language = Literal["en", "hinglish", "hi", "gu", "gujlish"]


@dataclass(frozen=True)
class StylePhrase:
    start: int
    end: int
    text: str


def _w(pattern: str) -> str:
    """A whole-word alternative for any script."""
    return rf"{_B}(?:{pattern}){_E}"


# ----------------------------------------------------------- normalising --

def _script_word(pattern: str, repl: str) -> Tuple[Pattern[str], object]:
    """An Indic word, optionally with an attached Gujarati postposition. The
    postposition "માં" becomes " _in_"; a case suffix is dropped."""
    rx = re.compile(rf"{_B}(?:{pattern}){_GU_SUFFIX}{_E}")

    def sub(m: "re.Match[str]") -> str:
        suf = m.group("suf") or ""
        return f" {repl}{' _in_' if suf in ('માં', 'મા') else ''} "

    return rx, sub


def _word(pattern: str, repl: str) -> Tuple[Pattern[str], str]:
    return re.compile(_w(pattern)), f" {repl} "


#: SHOW, in all four languages. `_SHOW_LONG` is the "show and hand over"
#: form; it is kept out of the read fallback so the plain `aapo`/`આપો`
#: hand-over rule below still reads "batavi aapo" the way it always has.
_SHOW = (r"दिखाओ|दिखा\s*(?:दो|दें|दीजिए|देना)|दिखाइए|दिखाएँ|दिखाएं|બતાવો|બતાવજો|"
         r"dikhao|dikha\s*(?:do|de|dijiye|dena)|dikhado|dikhaiye|dekhao|batavo|batavjo")
_SHOW_LONG = r"બતાવી\s+(?:આપો|દો)|batavi\s+(?:aapo|apo|do)"

#: Applied in order. Indic phrases first (they are longest), then Latin
#: script Hinglish/Gujlish, then English typos. Every replacement is padded
#: with spaces; whitespace is collapsed at the end.
_NORMALISE: List[Tuple[Pattern[str], object]] = [
    # --- Hindi / Gujarati formats and nouns ---------------------------------
    _script_word(r"पीडीएफ़?|पी\s?डी\s?एफ|પીડીએફ|પી\s?ડી\s?એફ", "pdf"),
    _script_word(r"वर्ड|વર્ડ", "word"),
    _script_word(r"डॉक्यूमेंट|डाक्यूमेंट|दस्तावेज़?|ડોક્યુમેન્ટ|ડૉક્યુમેન્ટ|દસ્તાવેજ", "document"),
    _script_word(r"डॉक|ડૉક|ડોક", "doc"),
    _script_word(r"एक्सेल|એક્સેલ", "excel"),
    _script_word(r"शीट|શીટ", "sheet"),
    _script_word(r"सीएसवी|સીએસવી", "csv"),
    _script_word(r"पावरपॉइंट|પાવરપોઈન્ટ|પાવરપોઇન્ટ", "powerpoint"),
    _script_word(r"पीपीटी|પીપીટી", "ppt"),
    _script_word(r"प्रेजेंटेशन|प्रेज़ेंटेशन|प्रस्तुति|પ્રેઝન્ટેશન|પ્રેઝેન્ટેશન", "presentation"),
    _script_word(r"स्लाइड्स?|સ્લાઇડ્સ?|સ્લાઈડ્સ?", "slides"),
    _script_word(r"बार\s+चार्ट|બાર\s+ચાર્ટ", "bar chart"),
    _script_word(r"पाई\s+चार्ट|પાઇ\s+ચાર્ટ", "pie chart"),
    _script_word(r"चार्ट|ग्राफ़?|ચાર્ટ|ગ્રાફ", "chart"),
    _script_word(r"फ़ाइल|फाइल|फ़ाईल|फाईल|ફાઇલ|ફાઈલ", "file"),
    _script_word(r"रिपोर्ट|રિપોર્ટ", "report"),
    _script_word(r"जवाब|उत्तर|જવાબ", "answer"),
    _script_word(r"शीर्षक|શીર્ષક|टाइटल|ટાઇટલ", "title"),
    _script_word(r"हेडिंग्स|हेडिंग|શીર્ષકો|હેડિંગ્સ|હેડિંગ", "headings"),
    _script_word(r"कॉलम|कालम|કૉલમ|કોલમ", "column"),
    _script_word(r"पंक्ति|પંક્તિ|रो|રો", "row"),
    _script_word(r"सेक्शन|खंड|વિભાગ|સેક્શન", "section"),
    _script_word(r"पेज|पृष्ठ|પેજ|પાનું", "page"),
    _script_word(r"फ़ॉन्ट|फॉन्ट|ફોન્ટ", "font"),
    _script_word(r"तालिका|टेबल|ટેબલ|કોષ્ટક", "table"),
    _script_word(r"लैंडस्केप|લેન્ડસ્કેપ|લૅન્ડસ્કેપ", "landscape"),
    _script_word(r"पोर्ट्रेट|પોર્ટ્રેટ", "portrait"),
    _script_word(r"गहरा\s+नीला|गहरे\s+नीले|ઘાટો\s+વાદળી|ઘેરો\s+વાદળી|ઘાટા\s+વાદળી", "dark blue"),
    _script_word(r"नीला|नीले|નીલો|વાદળી", "blue"),
    _script_word(r"लाल|લાલ", "red"),
    _script_word(r"हरा|हरे|લીલો|લીલા|લીલું", "green"),
    _script_word(r"पीला|पीले|પીળો|પીળા|પીળું", "yellow"),
    _script_word(r"काला|काले|કાળો|કાળા", "black"),
    _script_word(r"सफ़ेद|सफेद|સફેદ", "white"),
    _script_word(r"नारंगी|નારંગી", "orange"),
    _script_word(r"बोल्ड|બોલ્ડ", "bold"),
    _script_word(r"रंग|રંગ", "color"),
    _script_word(r"मोटा|मोटे|बड़ा|बड़े|મોટો|મોટા|મોટું", "bigger"),
    _script_word(r"ऊपर\s+वाले|ऊपर\s+वाला|ऊपर\s+का|ऊपर\s+की|ઉપરનો|ઉપરના|ઉપરની", "above"),
    _script_word(r"डेटा|डाटा|ડેટા|ડાટા", "data"),
    _script_word(r"टेक्स्ट|ટેક્સ્ટ", "text"),
    _word(r"के\s+रूप\s+में|તરીકે|रूप\s+में", "as"),
    # "सेव कर दो", "ડાઉનલોડ કરો": an English verb typed in the script, then "do".
    _word(r"(?:सेव|સેવ)(?:\s+(?:कर\s*(?:दो|दें|दीजिए)|करो|करें|કરો|કરી\s+(?:દો|આપો)))?", "save"),
    _word(r"(?:डाउनलोड|ડાઉનલોડ)(?:\s+(?:कर\s*(?:दो|दें|दीजिए)|करो|करें|કરો|કરી\s+(?:દો|આપો)))?", "download"),
    _word(r"(?:कन्वर्ट|કન્વર્ટ)(?:\s+(?:कर\s*(?:दो|दें|दीजिए)|करो|करें|કરો|કરી\s+(?:દો|આપો)))?", "_convert_"),
    _word(r"(?:एक्सपोर्ट|એક્સપોર્ટ)(?:\s+(?:कर\s*(?:दो|दें|दीजिए)|करो|करें|કરો|કરી\s+(?:દો|આપો)))?", "export"),
    _word(r"(?:एडिट|એડિટ|अपडेट|અપડેટ)(?:\s+(?:कर\s*(?:दो|दें|दीजिए)|करो|करें|કરો|કરી\s+(?:દો|આપો)))?", "update"),
    _word(r"फेरबदल\s+(?:करो|कर\s+दो)|बदलाव\s+(?:करो|कर\s+दो|करें)|ફેરફાર\s+(?:કરો|કરી\s+(?:દો|આપો))", "change"),
    _word(r"पूरा\s+(?:करो|कर\s+दो)|પૂરું\s+કરો|પૂરો\s+કરો", "complete"),
    # --- Hindi / Gujarati verbs ---------------------------------------------
    # A destination then a change verb is a conversion ("पीडीएफ में बदल दो").
    (re.compile(rf"(pdf|word|doc|excel|csv|powerpoint|presentation|file|document|sheet|_in_|में|માં)\s+(?:बदल\s*(?:दो|दें|दीजिए|देना)|बदलें|बदलो|ફેરવો|ફેરવી\s+આપો|બદલો|બદલી\s+આપો|કન્વર્ટ\s+કરો){_E}"), r" \1 _convert_ "),
    _word(r"बनाकर\s+(?:दो|दें|दीजिए|दे\s+दो)|बना\s*(?:दो|दें|दीजिए|देना)|बनाओ|बनाइए|बनाइये|बनाएं|बनाएँ|बनाये|बनाकर|"
          r"तैयार\s+(?:करें|करो|कीजिए|कर\s+दो|करके\s+दो)|दे\s+(?:दो|दीजिए|दें)|दीजिए|भेज\s*(?:दो|दीजिए)|भेजो|चाहिए|"
          r"બનાવી\s+(?:આપો|આપજો|દો)|બનાવો|બનાવજો|તૈયાર\s+કરો|તૈયાર\s+કરી\s+આપો|આપો|આપજો|જોઈએ|મોકલો|મોકલી\s+આપો", "_give_"),
    # SHOW. Measured 2026-09-16 (measure2 harness): "show this as a pie
    # chart" returned action=none in all 4 languages, because SHOW had no
    # entry at all. It is a hand-over ONLY after a chart word — "pie chart me
    # dikhao", "પાઇ ચાર્ટમાં બતાવો" — where there is something to hand over.
    # On a source it is a read, like "बताओ"/"batao" below: "mujhe ye pdf
    # dikhao" with a PDF attached must stay a question about that PDF, not an
    # export of the previous answer. The chart words are already Latin here
    # (चार्ट / ચાર્ટ / ગ્રાફ are mapped above), so one rule reads all four
    # languages.
    (re.compile(rf"{_B}(?P<lead>(?:chart|graph)s?(?:\s+\S+){{0,2}}?\s+)(?:{_SHOW}|{_SHOW_LONG}){_E}"), r" \g<lead> _give_ "),
    _word(_SHOW, "_read_"),
    # Verifier 2026-09-15: "undo the last change" said as a removal of it.
    _word(r"पिछला\s+बदलाव\s+(?:हटा\s*(?:दो|दें|दीजिए)|हटाओ|वापस\s+(?:लो|ले\s+लो))|पिछले\s+बदलाव\s+(?:हटा\s*(?:दो|दें)|हटाओ)|છેલ્લો\s+ફેરફાર\s+(?:કાઢી\s+નાખો|પાછો\s+લો)", "undo"),
    _word(r"जोड़ो|जोड़ें|जोड़\s+दो|जोडो|जोड़िए|ઉમેરો|ઉમેરી\s+દો|ઉમેરી\s+આપો", "add"),
    _word(r"हटाओ|हटा\s+दो|हटाएं|કાઢી\s+નાખો|કાઢો|દૂર\s+કરો", "remove"),
    _word(r"बदलकर|बदल\s*(?:दो|दें|दीजिए)|बदलें|बदलो|બદલીને|બદલો|બદલી\s+નાખો", "change"),
    _word(r"वापस|पहले\s+जैसा|પહેલા\s+જેવું|પાછું", "undo"),
    _word(r"कैसे|કેવી\s+રીતે|કઈ\s+રીતે", "_howto_"),
    _word(r"समझाओ|समझाइए|बताओ|बताइए|सारांश|સમજાવો|સારાંશ|કહો", "_read_"),
    _word(r"इसे|इसको|इसका|इसकी|इसके|इसमें|इस|यह|ये|આને|આનો|આની|આનું|આના|આમાં|આ", "_this_"),
    _word(r"भी|પણ", "also"),
    _word(r"में|मे", "_in_"),
    _word(r"एक|એક", "a"),
    # --- Hinglish / Gujlish (Latin script) ----------------------------------
    (re.compile(rf"(pdf|word|doc|docs|docx|excel|exel|csv|ppt|pptx|powerpoint|presentation|file|document|sheet)\s+(?:me|mein|mai|mei|ma|maa|m)\s+(?:convert\s+)?(?:kar\s*do|karo|kardo|kari\s+do|kari\s+aapo|badal\s+do|badlo|badli\s+do){_E}"), r" \1 _in_ _convert_ "),
    _word(r"convert\s+(?:kar\s*do|karo|kardo|kari\s+aapo|kari\s+do|kar\s*ke\s+(?:de|do|dena|dijiye)|karke\s+(?:de|do|dena|dijiye)|kr\s*(?:do|de)|krwa\s+(?:de|do)|karwa\s+(?:de|do))", "_convert_"),
    _word(r"(save|download|export|share|send|bhej)\s+(?:kar\s*do|karo|kardo|kar\s*de|kr\s*(?:do|de)|krwa\s+(?:de|do)|karwa\s+(?:de|do)|kar\s*ke\s+(?:de|do)|karke\s+(?:de|do)|kari\s+(?:aapo|do)|karvi\s+aapo)", r"\1 _give_"),
    _word(r"lagao|laga\s+do|lagaa\s+do|laga\s+dijiye|lagavo|lagavi\s+aapo|लगाओ|लगा\s+दो|લગાવો", "add"),
    _word(r"bana\s*de|bna\s*de", "_give_"),
    _word(r"bana\s*(?:do|de|dijiye|dena|dijie)|banao|bnao|bna\s*do|banaiye|banaye|banake\s+(?:do|de\s*do|dijiye)|bana\s+ke\s+(?:do|dijiye)|"
          r"banana\s+hai|banani\s+hai|generate\s+kar\s*do|download\s+karna\s+hai|download\s+kar\s*do|bhej\s*do|nikal\s+do|"
          r"de\s*do|dedo|dijiye|dijie|chahiye|chaiye|chahie|chahiya|"
          r"banavi\s+(?:aapo|apo|aapjo|do)|banavo|banavjo|mokli\s+aapo|kari\s+aapo|aapo|apo|aapjo|joie|joiye|joiae", "_give_"),
    # Verifier 2026-09-15: "isko excel sheet me daal do" puts the thing IN a format: a hand-over, not an add.
    (re.compile(rf"(pdf|word|doc|docs|docx|excel|exel|csv|ppt|pptx|powerpoint|presentation|file|document|sheet)\s+(?:me|mein|mai|mei|ma|maa)\s+(?:daal|dal|daalo|daldo|rakh)\s*(?:do|de|dijiye|dena)?{_E}"), r" \1 _in_ _give_ "),
    _word(r"pichla\s+change\s+(?:hata\s*do|hatao|wapas\s+lo|remove\s+kar\s*do)|last\s+change\s+(?:hata\s*do|hatao|wapas\s+lo)|"
          r"chh?ell[oa]\s+ferfar\s+(?:kadhi\s+nakho|kadho|kadhi\s+do|pachho\s+lo|dur\s+karo)|chh?ell[oa]\s+badlav\s+kadhi\s+nakho", "undo"),
    _word(r"add\s+(?:kar\s*do|karo|kardo)|daal\s+do|dal\s+do|daalo|daldo|jod\s+do|jodo|umero|umeri\s+do", "add"),
    _word(r"hata\s+do|hatao|nikal\s+do|kadhi\s+nakho", "remove"),
    _word(r"badal\s+do|badlo|badli\s+do|badli\s+nakho|change\s+kar\s*do|change\s+karo", "change"),
    _word(r"wapas|vapas|pehle\s+jaisa|pahle\s+jaisa|pehla\s+jevu|pachu", "undo"),
    _word(r"kaise|kese|kaisey|kaise\s+kare|kevi\s+rite|kem\s+kari", "_howto_"),
    _word(r"samjhao|samjha\s+do|samjhaiye|batao|bata\s+do|bataiye|samjavo|samjhavo|kaho", "_read_"),
    _word(r"isko|iska|iski|iske|ise|isse|isme|isey|aane|aano|aani|aanu|aana|ama", "_this_"),
    (re.compile(rf"{_B}(?:is|iss|ye|yeh|aa|es|e){_E}(?=\s+(?:pdf|word|doc|docs|excel|csv|file|document|report|answer|data|sheet|table|audit|jawab|chart|list|text|content|reply|response|output|info|information|summary|explanation|ppt|presentation)\b)"), " _this_ "),
    # An object marker after a noun of the conversation: "report ne pdf ma
    # aapjo", "ye reply ko word file me" — the existing thing.
    (re.compile(rf"{_B}(report|answer|content|data|text|reply|response|jawab|summary|output|audit)\s+(?:ne|ko|nu|ka|ki)(?=\s+(?:pdf|word|doc|docx|excel|csv|ppt|pptx|file|document|sheet)\b)"), r" the \1 "),
    # An English verb and a Hinglish/Gujlish light verb: "create karo", "banavi do ne".
    (re.compile(rf"{_B}(?:create|generate|make|prepare|build|export|convert|save|download|send|share|tayyar|taiyar|taiyyar|tayar)\s+(?:karo|kar\s*do|kari\s+(?:do|aapo|dejo)|karjo|kar\s*ke\s+do|karke\s+do|kardo|kar\s*dijiye|karvanu)(?:\s+ne)?{_E}"), " _give_ "),
    _word(r"navi|navu|nayi|naya|नई|नया|નવી|નવું", "new"),
    _word(r"kripya|kripaya|कृपया|કૃપા\s+કરીને", "please"),
    _word(r"upar\s+(?:wala|wale|wali|ka|ki|ke|diya|diye|lakhelo|no)|above\s+wala|uparno", "above"),
    _word(r"bhi|pan\s+(?=pdf|excel|word|csv|ppt)", "also"),
    (re.compile(rf"(pdf|word|doc|docs|docx|excel|exel|csv|ppt|pptx|powerpoint|presentation|file|document|sheet|format|report|version)\s+(?:me|mein|mai|mei|ma|maa|mā){_E}"), r" \1 _in_ "),
    _word(r"neela|nila|neele", "blue"),
    _word(r"gehra\s+neela|gehre\s+neele|dark\s+neela", "dark blue"),
    _word(r"lal|laal", "red"),
    _word(r"hara|hare", "green"),
    _word(r"peela|peele|pila|pile", "yellow"),
    _word(r"rang|rangon|rango|colours|colors|colour", "color"),
    # --- English typos -------------------------------------------------------
    _word(r"creat|craete|crate\s+a|cretae", "create"),
    _word(r"genrate|genarate|generete|gnerate|genrat|generat", "generate"),
    _word(r"mak|mke|amke", "make"),
    _word(r"provde|provid|porvide|privide", "provide"),
    _word(r"giv|gve|gimme", "give"),
    _word(r"presentaion|presntation|prsentation|presentasion|presantation|presentatin", "presentation"),
    _word(r"spreadhseet|spreadsheat|sprdsheet|spredsheet|spreedsheet|spread\s+sheet|sprsht", "spreadsheet"),
    _word(r"exel|excell|exl|exceel|xcel", "excel"),
    _word(r"xlxs|xslx|xsls|xlsxs", "xlsx"),
    _word(r"dox|doxc|docxs|doxs|docz|dcox", "docx"),
    _word(r"pfd|pdff|pdfs", "pdf"),
    _word(r"powerpint|powerpont|power\s+point|pwerpoint|powepoint", "powerpoint"),
    _word(r"formatt|formate|fromat|foramt|frmat", "format"),
    _word(r"profesional|proffesional|professinal|proffessional|profesionnal|professionl", "professional"),
    _word(r"chnage|chng|chang|chage|cahnge|chnge", "change"),
    _word(r"tittle|titel|tilte|titile", "title"),
    _word(r"colum|coloumn|collumn|colmn|coulmn|colunm", "column"),
    (re.compile(_w(r"ad") + r"(?=\s+(?:a|an|the|new|one|column|row|section|slide|chart|table|page|footer|header|total)\b)"), " add "),
    _word(r"hedings|headngs|headins|heddings", "headings"),
    _word(r"heding|headng|headin|hedding", "heading"),
    _word(r"blu", "blue"),
    _word(r"fnt|fotn", "font"),
    _word(r"wrod|wrd", "word"),
    _word(r"documnet|docment|documant|documnt|dcument", "document"),
    _word(r"repot|reprt|reoprt", "report"),
    _word(r"landscap|lanscape|landscpae|landscaep", "landscape"),
    _word(r"pls|plz|plss|plx|plzz|pleas|kindly", "please"),
    _word(r"u", "you"),
    _word(r"ur", "your"),
    (re.compile(rf"(?<=[a-z])\s+n\s+(?=[a-z])"), " and "),
    # "docs" / "doc" name Word only in a file context: "in docs", "doc file",
    # "docs me" — never "the docs say" or "google docs".
    (re.compile(rf"(?<!google )(?<!google  ){_B}docs?{_E}(?=\s*(?:file|format|version|copy|_in_)\b)"), " docx "),
    (re.compile(rf"{_B}(in|as|into|to)\s+docs?{_E}"), r" \1 docx "),
    # "excel me do", "पीडीएफ में दो": a bare "give" after a destination.
    (re.compile(rf"_in_\s+(?:do|दो|दें|de|dijiye){_E}"), " _in_ _give_ "),
    _word(r"ek", "a"),
    # "pdf mat banao", "file nahi chahiye", "પીડીએફ ના બનાવો": a negated
    # hand-over. intent.py blanks the clause that carries it.
    (re.compile(rf"{_B}(?:mat|nahi|nahin|nai|na|nako|मत|नहीं|ना|ના|નહીં|નહિ)\s+_give_"), " _neg_ "),
    (re.compile(rf"_give_\s+(?:mat|nahi|nahin|मत|नहीं|ના|નહીં){_E}"), " _neg_ "),
    (re.compile(rf"{_B}docs?{_E}\s*$"), " docx "),
]

_SPACES = re.compile(r"[ \t\r\f\v]+")


def normalize(text: str) -> str:
    """Case folded, zero-width stripped, typos and script variants mapped to
    canonical tokens. Newlines are kept (clauses are split on them)."""
    out = unicodedata.normalize("NFKC", text or "")
    out = _ZERO_WIDTH.sub("", out).casefold()
    out = out.replace("’", "'").replace("‘", "'")
    for rx, repl in _NORMALISE:
        out = rx.sub(repl, out)  # type: ignore[arg-type]
    return _SPACES.sub(" ", out).strip()


# ------------------------------------------------------------- signals --

#: Formats named in normalised text. `sheet` is not here: "cheat sheet".
FORMAT_ALIASES = {
    "docx": r"docx|word\s+(?:document|file|doc|docs|version|copy|format|report)|ms\s*word|microsoft\s+word|(?:in|as|to|into)\s+word|"
            # The normaliser's SOV shape — see formats._ALIAS for the measurement.
            r"word\s+_in_(?:\s+\S+){0,2}?\s+(?:_convert_|_give_)|word\s+(?:_convert_|_give_)",
    "xlsx": r"xlsx|xls|excel|spreadsheets?|workbook|(?<!cheat )(?<!fact )(?<!balance )(?<!time )sheet(?!\s*\d)",
    "pdf": r"pdf",
    "pptx": r"pptx|ppt|powerpoint|presentation|slide\s*deck|deck|slides",
    "csv": r"csv|comma[- ]separated",
    "image": r"png|svg|jpe?g|(?:as|an?)\s+image",
}
_FORMAT_RE = re.compile(_w("|".join(f"(?:{p})" for p in FORMAT_ALIASES.values())))
_FILE_NOUN_RE = re.compile(_w(r"files?|documents?|docs?|reports?|attachments?|downloadable|download"))
_CHART_RE = re.compile(_w(
    r"(?:bar|line|pie|donut|doughnut|area|scatter|bubble|column|stacked(?:\s+bar)?|combo|radar|funnel|waterfall|gantt(?:-style)?|box|"
    r"histogram|heat\s*map)\s+(?:chart|graph|plot)s?|charts?|graphs?|plots?|histograms?|heat\s*maps?|scatter\s*plots?|"
    r"box\s*plots?|gantt(?:-style)?\s+timeline|plot|"
    # The tier-2 names of 2026-09-16, as nouns: without them the intent
    # gate answered "visualise this table as a treemap" and "show this as a
    # sunburst" with chart_request=False, and formats.decide — which takes
    # the gate's verdict over its own regex — sent them to Word and PDF.
    # Only the unambiguous nouns are here; "pareto chart", "violin plot"
    # and "bullet chart" already match through charts?|graphs?|plots?,
    # while their bare forms are ordinary words.
    r"tree\s*maps?|sunbursts?|candlesticks?|ohlc|"
    # AS3 integration (live 2026-09-15): a chart type named as a noun, "scatter of Salary vs Experience".
    r"(?:scatter|bubble|waterfall|funnel|radar|pie|donut|doughnut|gantt)\s+(?:of|showing|comparing)\s+\S+(?:\s+\S+){0,6}?\s+(?:vs\.?|versus|by|per|against|over)"
))
#: A chart TYPE said in words — chart_spec.CHART_TYPES plus the words people
#: type for them. One home, read by intent.py (which routes "make it a bar
#: chart instead" to an edit) and by edits.py (which turns it into a
#: set_chart op); chart_spec itself is not imported by either on the chat
#: event loop, and both would otherwise drift apart.
CHART_TYPE_WORDS = (
    r"horizontal\s+bar|percent\s+stacked(?:\s+bar)?|stacked(?:\s+bar)?|bar|column|line|area|pie|donut|doughnut|"
    r"scatter|bubble|histogram|combo|dual[\s-]axis|box(?:\s*plot)?|heat\s*map|waterfall|funnel|gantt|radar|spider|"
    # Every reader of this vocabulary requires a "chart|graph|plot" after
    # it, so the ordinary-word names are safe here: "make it a pareto chart
    # instead" has to reach the set_chart edit the same way "bar chart" does.
    r"tree\s*map|sunburst|candlestick|ohlc|pareto|violin|bullet"
)

_REQUEST_VERB_RE = re.compile(_w(
    r"make|create|generate|build|prepare|produce|export|convert|turn|put|save|download|give|send|share|provide|deliver|"
    r"format|wrap|compile|draft|write|design|plot|draw|need|want|get|add|change|update|insert|include|remove|delete|"
    r"rename|set|use|also|_give_|_convert_|_in_|also"
))


def formats_in(norm: str) -> List[str]:
    """Canonical formats named in NORMALISED text, in order of first mention
    ('image' for png/svg/jpg). Sources are not told apart here — intent.py
    and formats.py do that."""
    out: List[str] = []
    for m in _FORMAT_RE.finditer(norm or ""):
        word = m.group(0)
        for fmt, pat in FORMAT_ALIASES.items():
            if re.fullmatch(pat, word):
                if fmt not in out:
                    out.append(fmt)
                break
    return out


def file_signal(text: str) -> bool:
    """Does the message name a file format, a file, or a chart? Reads raw or
    normalised text (normalises when it looks raw)."""
    norm = _ensure_norm(text)
    return bool(_FORMAT_RE.search(norm) or _CHART_RE.search(norm) or _FILE_NOUN_RE.search(norm))


def chart_signal(text: str) -> bool:
    return bool(_CHART_RE.search(_ensure_norm(text)))


def _ensure_norm(text: str) -> str:
    t = text or ""
    # Normalised text is already folded and has no zero-width characters;
    # anything else is normalised first. Cheap either way.
    return normalize(t)


# --------------------------------------------------------------- style --

_COLOR = (
    r"dark\s+blue|navy(?:\s+blue)?|light\s+blue|sky\s+blue|royal\s+blue|blue|dark\s+green|light\s+green|green|dark\s+red|light\s+red|red|"
    r"orange|amber|yellow|light\s+yellow|purple|violet|pink|grey|gray|light\s+grey|light\s+gray|black|white|brown|teal|gold|golden|maroon|"
    r"#[0-9a-f]{6}|#[0-9a-f]{3}"
)
_FONT_NAMES = (
    r"arial|calibri|cambria|georgia|times(?:\s+new\s+roman)?|helvetica|verdana|garamond|segoe(?:\s+ui)?|roboto|"
    r"open\s+sans|lato|tahoma|courier(?:\s+new)?|carlito|caladea|noto(?:\s+sans)?"
)
_STYLE_TERM_RE = re.compile(_w(
    rf"{_COLOR}|colou?r(?:ed|s|ful)?|fonts?|font\s+size|{_FONT_NAMES}|\d{{1,2}}\s*(?:pt|point|px)|size\s+\d{{1,2}}|"
    r"bold|italic(?:s|ize|ise|ized)?|underline[ds]?|highlight(?:ed|s)?|shad(?:e|ed|ing)|background|fill|banded|zebra|borders?|"
    r"landscape|portrait|margins?|page\s+numbers?|bigger|smaller|larger|classy|elegant|stylish|"
    r"(?:look|looks|looking)\s+(?:more\s+)?(?:classy|professional|elegant|modern|clean|better|nicer|plain)|"
    r"percentages?|currency\s+format|number\s+format|data\s+labels?|legend|a4|letter\s+size|dark\s+mode"
))
#: A clause boundary for style clauses: comma, semicolon, " and ", " with ".
_CLAUSE_SPLIT_RE = re.compile(r"[,;.!?\n]|\s(?:and|with|aur|ane|और|અને)\s")


def style_phrases(text: str) -> List[StylePhrase]:
    """The clauses of `text` that ask how something LOOKS — "make the headings
    dark blue", "use Georgia", "landscape". Offsets index the text as given
    (normalise first when you need the Indic forms)."""
    t = text or ""
    out: List[StylePhrase] = []
    start = 0
    bounds = [m for m in _CLAUSE_SPLIT_RE.finditer(t)] + [None]
    for b in bounds:
        end = b.start() if b is not None else len(t)
        seg = t[start:end]
        if seg.strip() and _STYLE_TERM_RE.search(seg.casefold()):
            lead = len(seg) - len(seg.lstrip())
            out.append(StylePhrase(start + lead, end, seg.strip()))
        start = b.end() if b is not None else len(t)
    return out


def strip_style_clauses(text: str) -> str:
    """`text` without its style clauses — what the section-request reader
    must see ("with white bold text" is not a section)."""
    t = text or ""
    phrases = style_phrases(t)
    if not phrases:
        return t
    pieces: List[str] = []
    last = 0
    for p in phrases:
        pieces.append(t[last:p.start])
        last = p.end
    pieces.append(t[last:])
    return re.sub(r"\s{2,}", " ", "".join(pieces)).strip(" ,;")


_UNDO_RE = re.compile(_w(r"undo|revert(?:\s+(?:it|that|this|back))?|go\s+back(?:\s+to)?|roll\s*back|restore|previous\s+version|last\s+version|put\s+it\s+back"))


def undo_signal(text: str) -> bool:
    return bool(_UNDO_RE.search(_ensure_norm(text)))


# ----------------------------------------------------- negative shapes --

_SOURCE_DET = r"(?:this|the|that|these|those|my|attached|uploaded|_this_|above)"
_READ_VERB = (
    r"summari[sz]e|summary\s+of|explain|what\s+does|what'?s\s+in|what\s+is\s+in|what\s+are\s+the|translate|analy[sz]e|compare|"
    r"review|check|read|go\s+through|tell\s+me|list\s+the|extract|does|is\s+there|how\s+many|how\s+much|who|which|where|when|"
    r"(?:need|want)\s+to\s+(?:know|understand|see|check|find|learn|confirm)|page\s+count|word\s+count|is\s+it\s+safe|"
    r"key\s+points|main\s+points|_read_|padh(?:\s+ke|kar)?|padho|shu\s+che|shu\s+lakhyu|ketl[aou]|kitn[aie]|kya\s+likh|kya\s+galat|"
    r"kahan|kaha|kidhar|kaun|kon|kyun|kyu|kaisi|kaisa|dekh(?:\s+ke|kar|o)?|kya\s+hai|kya\s+he|"
    r"क्या\s+लिखा|कितनी|कितना|कितने|શું|કેટલું|કેટલા|કેટલી|क्या"
)
_READ_VERB_RE = re.compile(_w(_READ_VERB))
_READ_ON_SOURCE_RE = re.compile(
    rf"{_B}(?:{_READ_VERB}){_E}(?:\s+\S+){{0,5}}?\s+(?:{_SOURCE_DET}\s+)?(?:\S+\s+)?"
    rf"(?:pdf|docx|doc|word\s+(?:file|document|doc)|word|excel|xlsx|spreadsheet|sheet|csv|document|file|report|deck|slides|presentation|table)"
    rf"|(?:pdf|docx|word|excel|xlsx|sheet|csv|document|file|report)(?:\s+\S+){{0,6}}?\s+"
    rf"(?:_read_|padh|shu|ketl|kitn|kya|samj|bata|summary|saransh|કેટલ|શું|कितन|क्या|कौन|कहाँ|कब)"
)
_UPLOADED_RE = re.compile(_w(r"i\s+(?:just\s+)?(?:uploaded|attached|shared|sent)|uploaded|attached"))
#: What may follow a postposition that names a DESTINATION: the end of the
#: clause, a hand-over or conversion verb (possibly after one word), or a
#: style word. Anything else makes it a place ("csv file me data kahan hai").
_DEST_AFTER = (
    r"(?=\s*$|\s*[.,!?;]|\s+(?:_give_|_convert_|also|please|save|download|export|convert|chahiye|de|do|dedo|add|put|classy|professional|standard|"
    r"proper|format|landscape|portrait)\b|\s+\S+\s+(?:_give_|_convert_)\b)"
)
DEST_AFTER = _DEST_AFTER
#: A target: "as a pdf", "into excel", "pdf _in_", "a docx version".
_TARGET_RE = re.compile(
    rf"{_B}(?:as|into|to)\s+(?:an?\s+|the\s+)?(?:\S+\s+){{0,2}}?(?:pdf|docx|word|excel|xlsx|spreadsheet|csv|pptx|ppt|powerpoint|presentation|slides|png|image)"
    rf"|(?:pdf|docx|word|excel|xlsx|csv|pptx|ppt|powerpoint|file|document|sheet)(?:\s+(?:file|format|version|copy))?\s+_in_{_DEST_AFTER}"
    rf"|_convert_|_give_|{_B}(?:convert|export|turn|make|create|generate|build|save)\s"
)
_HOWTO_RE = re.compile(_w(
    r"how\s+(?:do|can|to|would|should|does|did|could)(?:\s+(?:i|we|you|one))?|how\s+to|steps\s+to|what'?s\s+the\s+(?:best|easiest)\s+way\s+to|"
    r"show\s+me\s+how|teach\s+me|_howto_"
))
_TRIVIA_RE = re.compile(_w(
    r"what\s+is|what'?s\s+a|what\s+are|what\s+does\s+the\s+\w+\s+(?:in|stand)|difference\s+between|better\s+than|better\s+for|"
    r"which\s+is\s+better|is\s+(?:a\s+|an\s+)?\S+\s+(?:better|the\s+same|worse)|why\s+(?:is|does|do|can'?t|won'?t)|can\s+a|does\s+a|"
    r"kya\s+hot[aie]|kya\s+fark|kya\s+antar|me\s+kya\s+fark|better\s+hai|etle\s+shu|shu\s+fark|shu\s+farak|"
    r"क्या\s+होत[ाीे]|क्या\s+अंतर|में\s+क्या\s+अंतर|_in_\s+क्या\s+अंतर|किस\s+काम|એટલે\s+શું|શું\s+ફરક|શું\s+તફાવત"
))
_FORMAT_OR_KIND_RE = re.compile(_w(
    r"pdf|docx|doc|word|excel|xlsx|xls|spreadsheets?|sheets?|csv|pptx|ppt|powerpoint|presentation|slides?|deck|"
    r"documents?|reports?|memo|files?|charts?|graphs?|google\s+sheets?"
))
_FEEDBACK_RE = re.compile(_w(
    r"looks?\s+(?:good|great|nice|fine|perfect|amazing|awesome)|came\s+out|is\s+(?:perfect|great|fine|good|nice|exactly|ready|amazing)|"
    r"thanks|thank\s+you|thx|ty|love\s+(?:the|it|this)|well\s+done|great|nice|perfect|awesome|saved\s+me|opened\s+fine|exactly\s+what|"
    r"acha|accha|achha|mast|badhiya|badiya|sahi\s+hai|ekdum|saras|bahu\s+mast|shandar|"
    r"अच्छ[ाीे]|धन्यवाद|बढ़िया|शानदार|સરસ|આભાર|શાનદાર|મસ્ત"
))
#: A request verb for the feedback test: a person who praises and asks
#: ("thanks, now make it a pdf") is asking.
_ASK_VERB_RE = re.compile(_w(
    r"make|create|generate|build|prepare|export|convert|turn|put|save|download|give|send|share|provide|format|wrap|add|change|update|"
    r"remove|delete|rename|also|can\s+you|could\s+you|please|now|_give_|_convert_|need|want"
))
_CODE_NOUN = r"scripts?|code|snippets?|functions?|macros?|programs?|कोड|स्क्रिप्ट|કોડ|સ્ક્રિપ્ટ"
_CODE_ASK_RE = re.compile(_w(
    rf"(?:write|show|give|generate|create|need|want|sample|provide|share|send|likh(?:\s+do)?|lakhi(?:\s+aapo)?|_give_|लिखो|लिखिए|लिख\s+दो)"
    rf"(?:\s+\S+){{0,5}}?\s+(?:{_CODE_NOUN})"
    rf"|(?:{_CODE_NOUN})\s+(?:to|that|for|which|in)\s"
    rf"|(?:{_CODE_NOUN})(?:\s+\S+)?\s+(?:_give_|do|chahiye|aapo|likh\w*|lakh\w*|लिख\w*|લખ\w*)"
    rf"|(?:sample|example|python|vba|bash|javascript|java|typescript|node(?:\.js)?|powershell|apps\s+script)\s+(?:{_CODE_NOUN})"
    rf"|(?:show|give\s+me)\s+(?:the\s+)?code"
    rf"|(?:write|show|give\s+me|generate)\s+(?:an?\s+|the\s+|me\s+)?(?:sql|soql|query)(?:\s+query)?\s+(?:to|that|for|which|statement)"
))
_CODE_LANG_RE = re.compile(_w(
    r"in\s+(?:python|vba|bash|javascript|java|typescript|node(?:\.js)?|powershell|apps\s+script|c#|golang|rust)|"
    r"with\s+(?:python-docx|openpyxl|python-pptx|pandas|reportlab|matplotlib)|python-docx|openpyxl|python-pptx|reportlab|"
    r"python\s+(?:_in_|me|mein|ma)|पायथन|પાયથન|vba\s+macro|write\s+python|write\s+a\s+python"
))
_CODE_NOT_A_REQUEST_RE = re.compile(_w(r"code\s+of\s+conduct|of\s+the\s+code|code\s+review|zip\s+code|dress\s+code|promo\s+code|qr\s+code|the\s+code\s+found"))
_TOPIC_HOWTO_RE = re.compile(r"\b(?:on|about|explaining|covering|for|regarding|showing)\s+how\s+to\b")
_SENTENCE_SPLIT_RE = re.compile(r"[.;!?\n\u0964\u0965]+|,\s+(?=(?:now|then|also|but|and|please|can|could|just)\b)")


def _clauses(norm: str) -> List[str]:
    return [c.strip() for c in _SENTENCE_SPLIT_RE.split(norm) if c and c.strip()]


def _is_request_clause(clause: str) -> bool:
    return bool(_TARGET_RE.search(clause) and (_FORMAT_OR_KIND_RE.search(clause) or _CHART_RE.search(clause)))


def negative_shape(text: str, upload_formats: Sequence[str] = ()) -> Optional[NegativeShape]:
    """Is this message shaped like something OTHER than a request for a file,
    although it names one? None when it is not (or when a clause of it is a
    plain request)."""
    norm = _ensure_norm(text)
    if not norm:
        return None
    uploads = [str(f).lower() for f in upload_formats or () if f]
    whole_code = _code_request(norm)
    if whole_code:
        return "code_request"
    found: Optional[NegativeShape] = None
    positive = False
    for clause in _clauses(norm):
        shape = _clause_shape(clause, uploads)
        if shape is not None:
            found = found or shape
        elif _is_request_clause(clause) and not _READ_VERB_RE.search(clause):
            positive = True
    if positive:
        return None
    if found:
        return found
    # Praise with no request anywhere: "great report, thank you!"
    if _FEEDBACK_RE.search(norm) and not _ASK_VERB_RE.search(norm) and _FORMAT_OR_KIND_RE.search(norm):
        return "feedback"
    return None


#: Something in the message ASKS: a question mark, a politeness word, an
#: imperative or hand-over verb, "I/we want|need", "can you", or a
#: normalised Hindi/Gujarati/Hinglish hand-over token. A message with none of
#: these is a statement ("I prefer pdf over word for contracts", "my manager
#: wants everything in excel") — the classifier said export at 0.9 on both
#: (verifier 2026-09-15, live), so a NEW file from a statement is refused.
_REQUEST_MARKER_RE = re.compile(
    r"\?|_give_|_convert_|_in_|\b(?:please|pls|plz|kindly)\b"
    r"|\b(?:give|send|make|create|generate|export|convert|provide|share|prepare|build|draft|produce|download|save|turn|put|"
    r"attach|deliver|compile|render|print|email|draw|plot|visuali[sz]e|design|format|write|add|bhejo|bhej|chahiye|joie|joiye)\b"
    r"|\b(?:bana+(?:o|vo|avo|ao|do|de|iye|ye)|banav\w*|ma+ng(?:u|o|na|ta|ti|i)|chahta|chahti|chahte)\b|(?:माँग|मांग|चाहता|चाहती|चाहते|માંગ|માગ|ઈચ્છ)"
    r"|\b(?:i|we)\s*(?:'d|would)?\s*(?:want|need|like|require)\b|\b(?:can|could|would|will)\s+(?:you|u|i|we)\b"
    # Noun-phrase asks: "word version of the audit", "the whole thing as a proper word document".
    r"|^\W*(?:an?\s+|the\s+)?(?:\w+\s+)?(?:pdf|docx|word|excel|xlsx|csv|pptx|ppt|powerpoint|sheet|spreadsheet|deck|slides|chart|graph)\b"
    r"|\bas\s+(?:an?\s+|the\s+)?(?:\w+\s+){0,2}(?:pdf|docx|word|excel|xlsx|csv|pptx|ppt|powerpoint|document|file|sheet|spreadsheet|deck|presentation)\b"
)


def request_marker(text: str) -> bool:
    """Does the message ask for something (vs state something)?"""
    t = _ensure_norm(text)
    if len(t.split()) <= 3:
        return True  # "dox?", "excel version", "pdf too": a bare ask
    return bool(_REQUEST_MARKER_RE.search(t))


def reads_source(text: str) -> bool:
    """A read verb or read noun anywhere ("kya galat hai", "summarize",
    "कितनी"): the message asks ABOUT something."""
    return bool(_READ_VERB_RE.search(_ensure_norm(text)))


def _code_request(norm: str) -> bool:
    if _CODE_NOT_A_REQUEST_RE.search(norm) and not _CODE_LANG_RE.search(norm):
        return False
    return bool(_CODE_ASK_RE.search(norm) or _CODE_LANG_RE.search(norm))


def _clause_shape(clause: str, uploads: Sequence[str]) -> Optional[NegativeShape]:
    has_format = bool(_FORMAT_OR_KIND_RE.search(clause))
    if not has_format:
        return None
    # "a pdf on how to set up 2FA": the how-to is the file's TOPIC.
    if _HOWTO_RE.search(_TOPIC_HOWTO_RE.sub(" ", clause)):
        return "how_to"
    if _TRIVIA_RE.search(clause) and not re.search(r"\b(?:can|could|would|will)\s+you\b", clause):
        return "trivia"
    if _READ_ON_SOURCE_RE.search(clause):
        source_named = bool(uploads) or bool(_UPLOADED_RE.search(clause))
        # A destination in the same clause ("summarize it AS a pdf") is a
        # request whatever the verb.
        dest = re.search(
            rf"{_B}(?:as|into)\s+(?:an?\s+|the\s+)?(?:\S+\s+){{0,2}}?(?:pdf|docx|word|excel|xlsx|csv|pptx|powerpoint|presentation|slides|png|image)"
            rf"|(?:pdf|docx|word|excel|xlsx|csv|pptx|file|document)(?:\s+(?:file|format|version))?\s+_in_\s+(?:_give_|_convert_|\S+\s+_give_)|_convert_"
            # "make a styled Excel summary from the uploaded CSV": a new file.
            rf"|{_B}(?:make|create|generate|build|prepare|produce|give\s+me|send\s+me)\s+(?:me\s+)?an?\s+(?:\S+\s+){{0,2}}?"
            rf"(?:pdf|docx|word|excel|xlsx|csv|pptx|powerpoint|presentation|deck|slides|report|document|spreadsheet|sheet|workbook|charts?|graph)",
            clause,
        )
        if source_named and not dest:
            return "read_source"
    if _FEEDBACK_RE.search(clause) and not _ASK_VERB_RE.search(clause):
        return "feedback"
    return None


# ------------------------------------------------------------ language --

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_GUJARATI = re.compile(r"[઀-૿]")
#: Both lists were FUNCTION words only, so a whole request could score one
#: marker or none and be answered in English: measured 2026-09-16 (measure2
#: harness) on "is data me sabse zyada kisne becha" (hn=['me'], 7 words),
#: "python me code likho jo document banaye", "aa file no saransh kaho"
#: (gj=[], hn=[], score 0/0) and "chhello ferfar kadhi nakho". The content
#: words people actually type are here too. Only words that cannot be English
#: are added: bare `aa`, `no`, `na` and `ni` are deliberately left out.
_GUJLISH_RE = re.compile(r"\b(?:aapo|apo|banavo|banavi|che|chhe|nu|ma|aane|aano|joie|joiye|kem|shu|mate|karo|ketlu|saras|bahu|kevi|"
                         r"amara|amaru|amari|amaro|saransh|kaho|chhello|chello|ferfar|nakho|batavo)\b")
_HINGLISH_RE = re.compile(r"\b(?:bana|banao|kar|karo|do|dedo|de|chahiye|hai|hain|mein|me|mujhe|isko|iska|ka|ki|ke|kya|kaise|yeh|ye|wala|bhi|nahi|aur|sab|mast|acha|accha|"
                          r"hamare|hamara|hamari|sabse|zyada|kisne|becha|likho|dikhao|banaye)\b")


def language_of(text: str) -> Language:
    t = text or ""
    if _GUJARATI.search(t):
        return "gu"
    if _DEVANAGARI.search(t):
        return "hi"
    low = t.casefold()
    gj_hits = _GUJLISH_RE.findall(low)
    hn_hits = _HINGLISH_RE.findall(low)
    gj, hn = len(gj_hits), len(hn_hits)
    # A word BOTH lists claim ("karo", "kar") says the message is not
    # English; it cannot say which of the two it is. Scoring it for Gujlish
    # and handing every tie to Gujlish labelled 2/30 Hinglish cases Gujlish
    # — "title bold karo" among them (measure2, 2026-09-16) — so only the
    # DISCRIMINATING markers choose between the two, and a tie falls through
    # to the larger Hinglish vocabulary below.
    shared = sum(1 for w in gj_hits if _HINGLISH_RE.fullmatch(w))
    if gj and (gj - shared) > (hn - shared):
        return "gujlish"
    if hn >= 2 or (hn == 1 and len(low.split()) <= 6):
        return "hinglish"
    if gj:
        return "gujlish"
    return "en"


__all__ = [
    "request_marker",
    "StylePhrase", "normalize", "formats_in", "file_signal", "chart_signal", "style_phrases", "strip_style_clauses",
    "undo_signal", "negative_shape", "reads_source", "language_of", "FORMAT_ALIASES", "DEST_AFTER",
    "CHART_TYPE_WORDS",
]
