"""Authored styling requests with the fields a person means (no user data).

Each entry: (request text, artifact kind, expected fields in the flat
`style.patch_fields` vocabulary). PHRASES is the 80-phrase acceptance set
(25 English, 20 Hinglish, 15 Hindi, 15 Gujarati, 5 typo-heavy); HELDOUT was
written at the same time and evaluated only after the parser was frozen, so
its score is the honest one.

Named colours resolve by the style guide: dark blue/navy #1F3864, blue
#2F6FB2, light blue #DCE6F2, dark green #1E6B34, green #3F8F4F, light green
#E3F2E6, red #C62828, yellow #FFD54F, grey #6B7280, light grey #EEF0F3,
dark grey #374151, white #FFFFFF, black #000000, teal #0E9D9A, orange
#E07B00, maroon #7B1E1E. A named orange/amber/yellow/teal/green/pink used as
TEXT on white becomes its text-safe variant (orange text → #B35F00).
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

Entry = Tuple[str, str, Dict[str, Any]]

NAVY, BLUE, LIGHT_BLUE = "#1F3864", "#2F6FB2", "#DCE6F2"
DARK_GREEN, GREEN, LIGHT_GREEN = "#1E6B34", "#3F8F4F", "#E3F2E6"
RED, YELLOW, GREY, LIGHT_GREY, DARK_GREY = "#C62828", "#FFD54F", "#6B7280", "#EEF0F3", "#374151"
WHITE, BLACK, TEAL, ORANGE, MAROON = "#FFFFFF", "#000000", "#0E9D9A", "#E07B00", "#7B1E1E"

ENGLISH: List[Entry] = [
    ("make the headings dark blue", "document", {"rule:heading:color": NAVY}),
    ("title in Georgia 24pt bold", "document", {"rule:title:font_family": "Georgia", "rule:title:size_pt": 24.0, "rule:title:bold": True}),
    ("body font Calibri 11pt", "document", {"fonts.body": "Calibri", "base_size_pt": 11.0}),
    ("table header dark green with white bold text", "document", {"rule:table_header:background": DARK_GREEN, "rule:table_header:color": WHITE, "rule:table_header:bold": True}),
    ("landscape", "document", {"page.orientation": "landscape"}),
    ("use A4 paper with narrow margins", "document", {"page.size": "A4", "page.margins": "narrow"}),
    ("add page numbers", "document", {"header_footer.page_numbers": True}),
    ("make it look professional", "document", {"preset": "classic"}),
    ("modern look please", "presentation", {"preset": "modern"}),
    ("captions in italic grey", "document", {"rule:caption:italic": True, "rule:caption:color": GREY}),
    ("underline the subtitle", "document", {"rule:subtitle:underline": True}),
    ("center the title", "document", {"rule:title:align": "center"}),
    ("Status column red for Fail, green for Pass", "workbook", {"cond:status:eq:fail:background": RED, "cond:status:eq:pass:background": GREEN}),
    ("highlight rows where status is Blocked in red", "workbook", {"cond:status:eq:blocked:background": RED, "cond:status:eq:blocked:whole_row": True}),
    ("row 5 yellow", "workbook", {"rule:row|index=5:background": YELLOW}),
    ("cells B2:D4 light blue", "workbook", {"rule:cell_range|a1=b2:d4:background": LIGHT_BLUE}),
    ("score above 80 green", "workbook", {"cond:score:gt:80.0:background": GREEN}),
    ("header row navy with white text", "workbook", {"rule:table_header:background": NAVY, "rule:table_header:color": WHITE}),
    ("no zebra stripes", "workbook", {"banded": False}),
    ("slide titles orange", "presentation", {"rule:slide_title:color": "#B35F00"}),
    ("slide background light grey", "presentation", {"page.background": LIGHT_GREY}),
    ("make the Recommendations heading red", "document", {"rule:heading|text=recommendations:color": RED}),
    ("footer text 'Internal use only'", "document", {"header_footer.footer_text": "Internal use only"}),
    ("white text on navy for the table header", "document", {"rule:table_header:color": WHITE, "rule:table_header:background": NAVY}),
    ("totals row bold with light blue fill", "workbook", {"rule:table_total:bold": True, "rule:table_total:background": LIGHT_BLUE}),
]

HINGLISH: List[Entry] = [
    ("heading ko dark blue karo", "document", {"rule:heading:color": NAVY}),
    ("title bold aur 20 pt karo", "document", {"rule:title:bold": True, "rule:title:size_pt": 20.0}),
    ("excel me header row neela kar do", "workbook", {"rule:table_header:background": BLUE}),
    ("body text Georgia font me rakho", "document", {"fonts.body": "Georgia"}),
    ("landscape me bana do", "document", {"page.orientation": "landscape"}),
    ("table header hara aur text safed", "document", {"rule:table_header:background": GREEN, "rule:table_header:color": WHITE}),
    ("status column me Fail lal karo", "workbook", {"cond:status:eq:fail:background": RED}),
    ("sheet with colors", "workbook", {"auto_status_colors": True, "banded": True}),
    ("page numbers daalo", "document", {"header_footer.page_numbers": True}),
    ("professional look do", "document", {"preset": "classic"}),
    ("headings gehra neela", "document", {"rule:heading:color": NAVY}),
    ("caption italic karo", "document", {"rule:caption:italic": True}),
    ("row 3 peela karo", "workbook", {"rule:row|index=3:background": YELLOW}),
    ("subtitle ko grey karo", "document", {"rule:subtitle:color": GREY}),
    ("title center me karo", "document", {"rule:title:align": "center"}),
    ("slide titles lal karo", "presentation", {"rule:slide_title:color": RED}),
    ("narrow margins rakho", "document", {"page.margins": "narrow"}),
    ("totals row bold karo", "workbook", {"rule:table_total:bold": True}),
    ("font size 12 karo", "document", {"base_size_pt": 12.0}),
    ("table body me Arial font", "document", {"rule:table_body:font_family": "Arial"}),
]

HINDI: List[Entry] = [
    ("हेडिंग को गहरा नीला करो", "document", {"rule:heading:color": NAVY}),
    ("टाइटल बोल्ड और 20 पॉइंट", "document", {"rule:title:bold": True, "rule:title:size_pt": 20.0}),
    ("लैंडस्केप में बनाओ", "document", {"page.orientation": "landscape"}),
    ("तालिका हेडर हरा करो", "document", {"rule:table_header:background": GREEN}),
    ("पेज नंबर जोड़ें", "document", {"header_footer.page_numbers": True}),
    ("फ़ॉन्ट Georgia रखो", "document", {"fonts.body": "Georgia", "fonts.heading": "Georgia"}),
    ("शीर्षक लाल करो", "document", {"rule:heading:color": RED}),
    ("कैप्शन इटैलिक करो", "document", {"rule:caption:italic": True}),
    ("टाइटल को सफेद और बैकग्राउंड नीला", "document", {"rule:title:color": WHITE, "rule:title:background": BLUE}),
    ("प्रोफेशनल लुक दो", "document", {"preset": "classic"}),
    ("बॉडी टेक्स्ट का साइज़ 12", "document", {"base_size_pt": 12.0}),
    ("पंक्ति 4 पीली करो", "workbook", {"rule:row|index=4:background": YELLOW}),
    ("टेबल हेडर नीला और टेक्स्ट सफेद", "workbook", {"rule:table_header:background": BLUE, "rule:table_header:color": WHITE}),
    ("सबटाइटल ग्रे करो", "document", {"rule:subtitle:color": GREY}),
    ("पोर्ट्रेट रखो", "document", {"page.orientation": "portrait"}),
]

GUJARATI: List[Entry] = [
    ("હેડિંગ ઘેરો વાદળી કરો", "document", {"rule:heading:color": NAVY}),
    ("ટાઇટલ બોલ્ડ અને 22 પોઇન્ટ", "document", {"rule:title:bold": True, "rule:title:size_pt": 22.0}),
    ("લેન્ડસ્કેપ માં બનાવો", "document", {"page.orientation": "landscape"}),
    ("ટેબલ હેડર લીલો", "document", {"rule:table_header:background": GREEN}),
    ("પેજ નંબર ઉમેરો", "document", {"header_footer.page_numbers": True}),
    ("ફોન્ટ Georgia રાખો", "document", {"fonts.body": "Georgia", "fonts.heading": "Georgia"}),
    ("શીર્ષક લાલ કરો", "document", {"rule:heading:color": RED}),
    ("કેપ્શન ઇટાલિક કરો", "document", {"rule:caption:italic": True}),
    ("ટાઇટલ સફેદ અને બેકગ્રાઉન્ડ વાદળી", "document", {"rule:title:color": WHITE, "rule:title:background": BLUE}),
    ("પ્રોફેશનલ દેખાવ", "document", {"preset": "classic"}),
    ("બોડી ટેક્સ્ટ સાઇઝ 12", "document", {"base_size_pt": 12.0}),
    ("હરોળ 6 પીળી કરો", "workbook", {"rule:row|index=6:background": YELLOW}),
    ("ટેબલ હેડર વાદળી અને ટેક્સ્ટ સફેદ", "workbook", {"rule:table_header:background": BLUE, "rule:table_header:color": WHITE}),
    ("કૉલમ Status લાલ", "workbook", {"rule:column|name=status:background": RED}),
    ("પોર્ટ્રેટ રાખો", "document", {"page.orientation": "portrait"}),
]

TYPOS: List[Entry] = [
    ("make the hedings dakr blue", "document", {"rule:heading:color": NAVY}),
    ("tabel header grean with whte text", "document", {"rule:table_header:background": GREEN, "rule:table_header:color": WHITE}),
    ("lanscape pls", "document", {"page.orientation": "landscape"}),
    ("titel in georiga font", "document", {"rule:title:font_family": "Georgia"}),
    ("colum Status bakground red", "workbook", {"rule:column|name=status:background": RED}),
]

PHRASES: List[Entry] = ENGLISH + HINGLISH + HINDI + GUJARATI + TYPOS

HELDOUT: List[Entry] = [
    ("please make all section headings navy blue and bold", "document", {"rule:heading|level=1:color": NAVY, "rule:heading|level=1:bold": True}),
    ("put the document in portrait with wide margins", "document", {"page.orientation": "portrait", "page.margins": "wide"}),
    ("table header in teal with white writing", "document", {"rule:table_header:background": TEAL, "rule:table_header:color": WHITE}),
    ("Priority column orange when High", "workbook", {"cond:priority:eq:high:background": ORANGE}),
    ("subheadings in dark green", "document", {"rule:heading|level=2:color": DARK_GREEN}),
    ("I want Times New Roman everywhere", "document", {"fonts.body": "Times New Roman", "fonts.heading": "Times New Roman"}),
    ("headings ka color maroon kar do", "document", {"rule:heading:color": MAROON}),
    ("slide background white and titles black", "presentation", {"page.background": WHITE, "rule:slide_title:color": BLACK}),
    ("कैप्शन ग्रे और छोटा", "document", {"rule:caption:color": GREY}),
    ("ટાઇટલ લાલ અને મધ્યમાં", "document", {"rule:title:color": RED, "rule:title:align": "center"}),
    ("zebra rows and freeze the header", "workbook", {"banded": True, "freeze_header": True}),
    ("amount greater than 50000 in bold", "workbook", {"cond:amount:gt:50000.0:bold": True}),
    ("bullets in dark grey", "document", {"rule:bullet:color": DARK_GREY}),
    ("chart title 14pt bold", "document", {"rule:chart_title:size_pt": 14.0, "rule:chart_title:bold": True}),
    ("letter size landscape", "document", {"page.size": "Letter", "page.orientation": "landscape"}),
    ("first paragraph italic", "document", {"rule:paragraph|first=true:italic": True}),
    ("totals row light green", "workbook", {"rule:table_total:background": LIGHT_GREEN}),
    ("heading font Cambria, body font Calibri 11", "document", {"fonts.heading": "Cambria", "fonts.body": "Calibri", "base_size_pt": 11.0}),
    ("kpi numbers in dark blue", "document", {"rule:kpi_value:color": NAVY}),
    ("no page numbers", "document", {"header_footer.page_numbers": False}),
]


def score(entries: List[Entry], parse) -> Dict[str, Any]:
    """Field-level accuracy: correct fields / (expected ∪ produced fields),
    so a wrong extra field costs as much as a missed one. `parse(text,
    kind)` returns (fields dict, unparsed phrases)."""
    correct = total = 0
    with_residue = 0
    misses: List[Tuple[str, Dict[str, Any], Dict[str, Any]]] = []
    for text, kind, expected in entries:
        got, unparsed = parse(text, kind)
        keys = set(expected) | set(got)
        ok = sum(1 for k in expected if k in got and got[k] == expected[k])
        correct += ok
        total += len(keys)
        if unparsed:
            with_residue += 1
        if ok != len(keys):
            misses.append((text, expected, got))
    return {"accuracy": correct / total if total else 1.0, "llm_rate": with_residue / len(entries), "misses": misses, "n": len(entries)}
