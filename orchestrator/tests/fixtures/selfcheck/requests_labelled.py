"""Forty authored artifact requests with hand-written expected checklists.

PROVENANCE. Written on 2026-09-15 BEFORE artifacts/requirements.py existed,
from the typed vocabulary below and the TechSara style guide only, so the
labels are not a copy of the extractor's rules. (The track asked for a
different agent; this session had no second agent, so the labels were
frozen first and never edited after the extractor was written. The
measured recall/precision is reported against them as they are.)
No production content: every request and every figure is invented.

VOCABULARY (category, target, property, expected):
  format        file            format            docx|pdf|xlsx|csv|pptx|png|svg
  layout        page            orientation       landscape|portrait
  layout        page            page_size         A4|Letter|Legal
  layout        page            margins           narrow|wide
  layout        page            page_numbers      True
  style         title|subtitle|heading|heading1|heading2|heading3|paragraph|
                table_header|table_total|chart_title|slide_title|
                column:<name>|row:<n>|cell_range:<A1>
                                color|background|font_family|size_pt|bold|italic|underline
  chart         chart           type|series_color|legend_position|data_labels|title|trendline
  content       section:<name>  present           True
  data          sheet           row_count         <int>
  data          column:<name>   present           True
  language      document        script            devanagari|gujarati|latin
  faithfulness  document        headings_covered|table_cells_covered  True
  preservation  untouched       unchanged         True
  house_style   title|table_header|page  title_block|fill_present|page_numbers  True

`must` is what a careful reviewer would refuse to call done if missing.
House-style and security items are not labelled (they are defaults, not
asks) unless a request names a quality word, in which case the labeller
wrote the invariants it implies.
"""
from __future__ import annotations

NAVY = "#1F3864"
BLUE = "#2F6FB2"
RED = "#C62828"
GREEN = "#3F8F4F"
YELLOW = "#FFD54F"
LIGHT_YELLOW = "#FFF1C7"
WHITE = "#FFFFFF"
BLACK = "#000000"
ORANGE = "#E07B00"
GREY = "#6B7280"
LIGHT_BLUE = "#DCE6F2"
DARK_GREEN = "#1E6B34"
MAROON = "#7B1E1E"
PURPLE = "#6D5AE6"

_CLASSY = [
    ("house_style", "title", "title_block", True, True),
    ("house_style", "table_header", "fill_present", True, True),
    ("house_style", "page", "page_numbers", True, True),
]
_FAITHFUL = [
    ("faithfulness", "document", "headings_covered", True, True),
    ("faithfulness", "document", "table_cells_covered", True, True),
]
_PRESERVE = [("preservation", "untouched", "unchanged", True, True)]

# (category, target, property, expected, must)
REQUESTS = [
    {"id": "r01", "kind": "document", "operation": "create", "previous_answer": True,
     "instruction": "just give it in docs in a standard and classy format, provide a dox file",
     "expected": [("format", "file", "format", "docx", True), *_FAITHFUL, *_CLASSY]},
    {"id": "r02", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "Make a Word report on onboarding with dark blue headings and Georgia body font, landscape",
     "expected": [
         ("format", "file", "format", "docx", True),
         ("style", "heading", "color", NAVY, True),
         ("style", "paragraph", "font_family", "Georgia", True),
         ("layout", "page", "orientation", "landscape", True),
     ]},
    {"id": "r03", "kind": "document", "operation": "edit", "previous_answer": False,
     "instruction": "make the headings dark blue",
     "expected": [("style", "heading", "color", NAVY, True), *_PRESERVE]},
    {"id": "r04", "kind": "document", "operation": "edit", "previous_answer": False,
     "instruction": "make it landscape",
     "expected": [("layout", "page", "orientation", "landscape", True), *_PRESERVE]},
    {"id": "r05", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "PDF report with a red title, a blue subtitle, 14pt body text and page numbers",
     "expected": [("style", "subtitle", "color", BLUE, True), 
         ("format", "file", "format", "pdf", True),
         ("style", "title", "color", RED, True),
         ("style", "paragraph", "size_pt", 14.0, True),
         ("layout", "page", "page_numbers", True, True),
     ]},
    {"id": "r06", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "excel sheet of 100 rows of support tickets with a green header row, white header text and bold header",
     "expected": [("style", "table_header", "color", WHITE, True), 
         ("format", "file", "format", "xlsx", True),
         ("data", "sheet", "row_count", 100, True),
         ("style", "table_header", "background", GREEN, True),
         ("style", "table_header", "bold", True, True),
     ]},
    {"id": "r07", "kind": "workbook", "operation": "edit", "previous_answer": False,
     "instruction": "add a column for owner",
     "expected": [("data", "column:owner", "present", True, True), *_PRESERVE]},
    {"id": "r08", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "CSV of monthly sales with a blue header and bold totals",
     "expected": [
         ("format", "file", "format", "csv", True),
         ("format", "file", "format", "xlsx", True),
         ("style", "table_header", "background", BLUE, True),
         ("style", "table_total", "bold", True, True),
     ]},
    {"id": "r09", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "pdf bana do with lal heading aur landscape page, page numbers bhi",
     "expected": [("layout", "page", "page_numbers", True, True), 
         ("format", "file", "format", "pdf", True),
         ("style", "heading", "color", RED, True),
         ("layout", "page", "orientation", "landscape", True),
     ]},
    {"id": "r10", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "मेरे लिए एक वर्ड फाइल बनाओ जिसमें शीर्षक नीला हो",
     "expected": [
         ("format", "file", "format", "docx", True),
         ("style", "title", "color", BLUE, True),
         ("language", "document", "script", "devanagari", False),
     ]},
    {"id": "r11", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "એક પીડીએફ બનાવો, હેડિંગ લીલો રંગ",
     "expected": [
         ("format", "file", "format", "pdf", True),
         ("style", "heading", "color", GREEN, True),
         ("language", "document", "script", "gujarati", False),
     ]},
    {"id": "r12", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "Word document covering Background, Findings and Recommendations, with italic subtitle and dark green headings",
     "expected": [("style", "heading", "color", DARK_GREEN, True), 
         ("format", "file", "format", "docx", True),
         ("content", "section:background", "present", True, True),
         ("content", "section:findings", "present", True, True),
         ("content", "section:recommendations", "present", True, True),
         ("style", "subtitle", "italic", True, True),
     ]},
    {"id": "r13", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "make a docx with a line chart of sales by month titled Monthly Sales, blue line, legend at the bottom",
     "expected": [("chart", "chart", "title", "Monthly Sales", True), 
         ("format", "file", "format", "docx", True),
         ("chart", "chart", "type", "line", True),
         ("chart", "chart", "series_color", BLUE, True),
         ("chart", "chart", "legend_position", "bottom", True),
     ]},
    {"id": "r14", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "xlsx with a pie chart of status with data labels and the legend on the right",
     "expected": [("chart", "chart", "legend_position", "right", True), 
         ("format", "file", "format", "xlsx", True),
         ("chart", "chart", "type", "pie", True),
         ("chart", "chart", "data_labels", True, True),
     ]},
    {"id": "r15", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "PDF with a scatter plot and a trend line",
     "expected": [
         ("format", "file", "format", "pdf", True),
         ("chart", "chart", "type", "scatter", True),
         ("chart", "chart", "trendline", True, True),
     ]},
    {"id": "r16", "kind": "presentation", "operation": "create", "previous_answer": False,
     "instruction": "a ppt deck on Q3 results, slide titles in maroon 28pt, Arial font",
     "expected": [("style", "slide_title", "size_pt", 28.0, True), 
         ("format", "file", "format", "pptx", True),
         ("style", "slide_title", "color", MAROON, True),
         ("style", "paragraph", "font_family", "Arial", True),
     ]},
    {"id": "r17", "kind": "document", "operation": "edit", "previous_answer": False,
     "instruction": "change the table header to dark green with white text",
     "expected": [
         ("style", "table_header", "background", DARK_GREEN, True),
         ("style", "table_header", "color", WHITE, True),
         *_PRESERVE,
     ]},
    {"id": "r18", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "A4 portrait PDF with narrow margins, grey headings and page numbers",
     "expected": [("style", "heading", "color", GREY, True), 
         ("format", "file", "format", "pdf", True),
         ("layout", "page", "page_size", "A4", True),
         ("layout", "page", "orientation", "portrait", True),
         ("layout", "page", "margins", "narrow", True),
         ("layout", "page", "page_numbers", True, True),
     ]},
    {"id": "r19", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "letter size word doc, title 32pt bold, headings underlined",
     "expected": [
         ("format", "file", "format", "docx", True),
         ("layout", "page", "page_size", "Letter", True),
         ("style", "title", "size_pt", 32.0, True),
         ("style", "title", "bold", True, True),
         ("style", "heading", "underline", True, True),
     ]},
    {"id": "r20", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "excel me do tracker, header peela and bold, row 5 yellow background",
     "expected": [("style", "table_header", "bold", True, True), 
         ("format", "file", "format", "xlsx", True),
         ("style", "table_header", "background", YELLOW, True),
         ("style", "row:5", "background", YELLOW, True),
     ]},
    {"id": "r21", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "sheet with colors: highlight B2:D4 in light yellow and make the header row dark blue",
     "expected": [("style", "table_header", "background", NAVY, True), ("style", "cell_range:B2:D4", "background", LIGHT_YELLOW, True)]},
    {"id": "r22", "kind": "document", "operation": "create", "previous_answer": True,
     "instruction": "convert your last answer to a pdf",
     "expected": [("format", "file", "format", "pdf", True), *_FAITHFUL]},
    {"id": "r23", "kind": "document", "operation": "create", "previous_answer": True,
     "instruction": "isko word file me de do, professional format",
     "expected": [("format", "file", "format", "docx", True), *_FAITHFUL, *_CLASSY]},
    {"id": "r24", "kind": "document", "operation": "edit", "previous_answer": False,
     "instruction": "make the title purple and 30pt",
     "expected": [
         ("style", "title", "color", PURPLE, True),
         ("style", "title", "size_pt", 30.0, True),
         *_PRESERVE,
     ]},
    {"id": "r25", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "create a doc with bar chart titled Revenue by Region with orange bars and the legend at the top",
     "expected": [("chart", "chart", "legend_position", "top", True), 
         ("format", "file", "format", "docx", True),
         ("chart", "chart", "type", "bar", True),
         ("chart", "chart", "title", "Revenue by Region", True),
         ("chart", "chart", "series_color", ORANGE, True),
     ]},
    {"id": "r26", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "Report as PDF and Word. H2 headings grey, body Calibri 11pt",
     "expected": [
         ("format", "file", "format", "pdf", True),
         ("format", "file", "format", "docx", True),
         ("style", "heading2", "color", GREY, True),
         ("style", "paragraph", "font_family", "Calibri", True),
         ("style", "paragraph", "size_pt", 11.0, True),
     ]},
    {"id": "r27", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "spreadsheet, make the Amount column bold and italic and the Status column red text",
     "expected": [("style", "column:amount", "italic", True, True), 
         ("style", "column:amount", "bold", True, True),
         ("style", "column:status", "color", RED, True),
     ]},
    {"id": "r28", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "a histogram of scores in the pdf with red bars",
     "expected": [("chart", "chart", "series_color", RED, True), 
         ("format", "file", "format", "pdf", True),
         ("chart", "chart", "type", "histogram", True),
     ]},
    {"id": "r29", "kind": "document", "operation": "edit", "previous_answer": False,
     "instruction": "headings ka color gehra neela kar do",
     "expected": [("style", "heading", "color", NAVY, True), *_PRESERVE]},
    {"id": "r30", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "docx with sections: Scope, Risks, Timeline. Table header green",
     "expected": [("style", "table_header", "background", GREEN, True), 
         ("format", "file", "format", "docx", True),
         ("content", "section:scope", "present", True, True),
         ("content", "section:risks", "present", True, True),
         ("content", "section:timeline", "present", True, True),
     ]},
    {"id": "r31", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "elegant pdf of the policy with a black title and white background table header",
     "expected": [
         ("format", "file", "format", "pdf", True),
         ("style", "title", "color", BLACK, True),
         ("style", "table_header", "background", WHITE, True),
         *_CLASSY,
     ]},
    {"id": "r32", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "50 rows of employee data in excel with frozen header, header light blue",
     "expected": [("style", "table_header", "background", LIGHT_BLUE, True), 
         ("format", "file", "format", "xlsx", True),
         ("data", "sheet", "row_count", 50, True),
     ]},
    {"id": "r33", "kind": "presentation", "operation": "create", "previous_answer": False,
     "instruction": "presentaion with a stacked bar chart by region, blue bars",
     "expected": [("chart", "chart", "series_color", BLUE, True), 
         ("format", "file", "format", "pptx", True),
         ("chart", "chart", "type", "stacked_bar", True),
     ]},
    {"id": "r34", "kind": "document", "operation": "edit", "previous_answer": False,
     "instruction": "make the first heading italic and the table header light blue",
     "expected": [
         ("style", "heading", "italic", True, True),
         ("style", "table_header", "background", LIGHT_BLUE, True),
         *_PRESERVE,
     ]},
    {"id": "r35", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "write a doc on safety, title in #0A1D37, headings Arial and font size 12 for body",
     "expected": [("style", "heading", "font_family", "Arial", True), 
         ("format", "file", "format", "docx", True),
         ("style", "title", "color", "#0A1D37", True),
         ("style", "paragraph", "size_pt", 12.0, True),
     ]},
    {"id": "r36", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "मुझे पीडीएफ चाहिए, लैंडस्केप, पेज नंबर के साथ",
     "expected": [
         ("format", "file", "format", "pdf", True),
         ("layout", "page", "orientation", "landscape", True),
         ("layout", "page", "page_numbers", True, True),
         ("language", "document", "script", "devanagari", False),
     ]},
    {"id": "r37", "kind": "workbook", "operation": "create", "previous_answer": False,
     "instruction": "exel file with line graph of weekly visitors in orange, title Weekly Visitors",
     "expected": [("chart", "chart", "series_color", ORANGE, True), 
         ("format", "file", "format", "xlsx", True),
         ("chart", "chart", "type", "line", True),
         ("chart", "chart", "title", "Weekly Visitors", True),
     ]},
    {"id": "r38", "kind": "document", "operation": "convert", "previous_answer": False,
     "instruction": "make it a docx",
     "expected": [("format", "file", "format", "docx", True)]},
    {"id": "r39", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "Word file with Times New Roman headings in bold and a 12pt justified body",
     "expected": [("style", "paragraph", "size_pt", 12.0, True), 
         ("format", "file", "format", "docx", True),
         ("style", "heading", "font_family", "Times New Roman", True),
         ("style", "heading", "bold", True, True),
     ]},
    {"id": "r40", "kind": "document", "operation": "create", "previous_answer": False,
     "instruction": "one page pdf, legal size, wide margins, blue subtitle, page numbers",
     "expected": [("layout", "page", "page_numbers", True, True), 
         ("format", "file", "format", "pdf", True),
         ("layout", "page", "page_size", "Legal", True),
         ("layout", "page", "margins", "wide", True),
         ("style", "subtitle", "color", BLUE, True),
     ]},
]

#: Ten adversarial phrasings where a naive element parser reads the wrong
#: target. The file is built as the MISREAD (the wrong element coloured);
#: the check must mark the item contested or failed, never passed.
MISPARSE_CASES = [
    {"id": "m01", "instruction": "headings dark blue", "asked": ("heading", "color", NAVY), "misread": ("title", "color", NAVY)},
    {"id": "m02", "instruction": "title red", "asked": ("title", "color", RED), "misread": ("heading", "color", RED)},
    {"id": "m03", "instruction": "make the table header green", "asked": ("table_header", "background", GREEN), "misread": ("title", "color", GREEN)},
    {"id": "m04", "instruction": "subtitle in grey", "asked": ("subtitle", "color", GREY), "misread": ("title", "color", GREY)},
    {"id": "m05", "instruction": "body text Georgia", "asked": ("paragraph", "font_family", "Georgia"), "misread": ("heading", "font_family", "Georgia")},
    {"id": "m06", "instruction": "headings in purple please", "asked": ("heading", "color", PURPLE), "misread": ("paragraph", "color", PURPLE)},
    {"id": "m07", "instruction": "heading ko lal karo", "asked": ("heading", "color", RED), "misread": ("title", "color", RED)},
    {"id": "m08", "instruction": "title 30pt", "asked": ("title", "size_pt", 30.0), "misread": ("heading", "size_pt", 30.0)},
    {"id": "m09", "instruction": "headings underlined", "asked": ("heading", "underline", True), "misread": ("title", "underline", True)},
    {"id": "m10", "instruction": "शीर्षक नीला करो", "asked": ("title", "color", BLUE), "misread": ("heading", "color", BLUE)},
]

TOTAL_EXPECTED_ITEMS = sum(len(r["expected"]) for r in REQUESTS)
