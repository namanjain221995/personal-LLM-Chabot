"""The QA cases: what a strong ("like ChatGPT") assistant does.

BASELINE_CASES are the 40 the 2026-09-17 baseline was measured with and are
frozen. ACCEPTANCE_CASES are the six this round adds, and CODING_CASES
(coding_cases.py) are 20 asks whose code is compiled and run for real.

Every case is one conversation of one or more turns. `expect` is the rubric a
strong answer meets, written as automated checks (harness.check_turn):

  artifact            True: a file deliverable must be produced; False: a chat answer (no file)
  formats_any         at least one produced file has one of these formats
  kind_any            the artifact kind is one of these
  same_artifact       this turn edits the artifact of the previous file turn (same id, higher version)
  chart_type_any      a chart of one of these types exists in the (new) version
  min_headings / min_bullets / min_numbered / min_bold / min_code_blocks
  min_table_rows / min_table_cols      a markdown table of at least that size
  no_runon            prose is not a run of single-newline lines (collapses when rendered)
  min_chars / max_chars                length appropriate to the ask (chat answers)
  must_contain        every group must match (a group is a list of alternatives, case-insensitive)
  forbidden           none may appear (invented fields, refusal strings)
  order               groups must first appear in this order
  doc_min_pages / doc_min_words / doc_min_headings / doc_min_tables     (file deliverables)
  doc_growth          the new version has at least this multiple of the previous version's words
  min_charts          charts in the produced version (spec.json, else embedded images)
  charts_real_values  every chart is bound to the uploaded table and, where the binding is simple,
                      its values equal a recompute from the CSV
  charts_multicolour  no chart is drawn in a single hue when it has more than one category/series,
                      and a multi-chart document does not reuse one identical hue for every chart
  doc_max_pages / doc_max_words        a short ask stays short
  doc_growth / doc_max_growth          the new version's words against the previous version's
  chart_after_heading  a chart block sits under one of these headings, not at the end of the file
  chart_hues_max       every chart image uses at most this many hues (one subject, one colour)
  chart_subject_hues_min  the file's chart images show at least this many distinct dominant hues
  status_colours       a chart over status words shows the success AND danger hues
  no_chart_claimed     the answer does not say it added a chart unless the version gained one
  code                 {lang, files, schema?, timeout?}: the answer's code is built and run
                       (code_sandbox.py) and its checker must pass
Always on: no failure sentence ("couldn't finish", "could not be made", "is not available" …) and,
on Fast, no thinking (no `reasoning` events, no meta.adaptive_thinking, no think=true trace).
"""
from __future__ import annotations

from coding_cases import CODING_CASES
from texts import JD_SAMPLE, JD_TARGET, MEETING_NOTES, POLICY_PLAIN, RESUME_PLAIN

FAIL_TEXT = ["couldn't finish", "could not finish", "could not be made", "is not available", "no charts to draw",
             "i didn't change", "i did not change", "nothing in the file changed"]

CUSTOMERS = "customers-100.csv"
SALES = "sales-2025.csv"
INVENTED_CUSTOMER_FIELDS = ["revenue", "purchase amount", "order value", "age group", "gender", "churn rate",
                            "lifetime value", "spend"]


def case(cid, category, turns, upload=None, effort="fast", note="", web_search="off"):
    return {"id": cid, "category": category, "upload": upload, "effort": effort, "turns": turns, "note": note,
            "web_search": web_search}


def turn(message, **expect):
    return {"message": message, "expect": expect}


CASES = [
    # ------------------------------------------------------------ formatting-heavy rewrites (6)
    case("F01", "format_rewrite", [turn(
        JD_SAMPLE + "\nthis is the requirment mentioning the sample format below change it in the same way\n\n" + JD_TARGET,
        artifact=False, min_headings=4, min_bold=4, min_bullets=10, no_runon=True, min_chars=1800,
        must_contain=[["censys"], ["45 days"], ["mitre"], ["qualys", "tenable"]],
        order=[["top skills"], ["summary"], ["key responsibilities"], ["required qualifications", "must have"],
               ["nice to have", "good to have"]])],
        note="Chat B shape: structured sample + plain-lines JD, rewrite in the sample's format"),
    case("F02", "format_rewrite", [turn(
        "Turn these rough notes into clean meeting minutes with sections for decisions, action items (owner and due date) and risks:\n\n" + MEETING_NOTES,
        artifact=False, min_headings=3, min_bullets=5, no_runon=True,
        must_contain=[["2.6"], ["sept 19", "september 19", "19 sept", "19 september"], ["priya"], ["sso"]],
        forbidden=["sept 12th meeting was cancelled"])]),
    case("F03", "format_rewrite", [turn(
        "Format this resume properly in markdown with clear sections and bullet points. Don't add anything that isn't in it.\n\n" + RESUME_PLAIN,
        artifact=False, min_headings=4, min_bullets=6, min_bold=2, no_runon=True,
        must_contain=[["120 ms", "120ms"], ["nirma"], ["kafka"]], forbidden=["gpa", "references available"])]),
    case("F04", "format_rewrite", [turn(
        "Rewrite this as a well-structured company policy with numbered sections and clear headings:\n\n" + POLICY_PLAIN,
        artifact=False, min_headings=6, min_bullets=6, no_runon=True,
        must_contain=[["11"], ["wednesday"], ["1,000", "1000"], ["2 hours", "two hours"]])]),
    case("F05", "format_rewrite", [turn(
        "Turn this into a comparison table: The Aero 14 weighs 1.3 kg, has 16 GB RAM, a 12-hour battery and costs $1,199. "
        "The Titan 16 weighs 2.4 kg, has 32 GB RAM, a 6-hour battery and costs $1,899. The Nova 13 weighs 1.1 kg, has 8 GB RAM, "
        "a 15-hour battery and costs $899.",
        artifact=False, min_table_rows=3, min_table_cols=4, max_chars=2500,
        must_contain=[["1,199", "1199"], ["2.4"], ["15"]])]),
    case("F06", "format_rewrite", [turn(
        "Make this email more professional and well structured: hey team so the release is gonna slip, qa found 3 blockers in "
        "payments and the vendor api is down since tuesday, new date is probably oct 7 but depends on vendor, pls tell your "
        "customers and dont promise anything, also we need 2 people from support for testing thursday thanks",
        artifact=False, no_runon=True, min_chars=350, max_chars=2200,
        must_contain=[["oct 7", "october 7", "7 october"], ["thursday"], ["3", "three"]])],
        note="should NOT be over-formatted into a report: paragraphs, maybe a short list"),

    # ------------------------------------------------------------ big report document asks (6)
    case("R01", "big_report", [turn(
        "Write a detailed report on electric vehicle adoption in India as a Word document. It should be at least 10 pages, with "
        "an executive summary, market size, government policy, charging infrastructure, key players, challenges, outlook and recommendations.",
        artifact=True, formats_any=["docx"], doc_min_pages=8, doc_min_words=3000, doc_min_headings=8)]),
    case("R02", "big_report", [turn(
        "I need a comprehensive PDF guide to setting up a small business in the UK: registration, taxes, banking, hiring, "
        "insurance and marketing. Make it thorough, not a summary.",
        artifact=True, formats_any=["pdf"], doc_min_pages=6, doc_min_words=2500, doc_min_headings=6)]),
    case("R03", "big_report", [turn(
        "I want to properly understand this data. Please give me a big report.",
        artifact=True, formats_any=["docx", "pdf"], doc_min_pages=4, doc_min_words=1200, min_charts=2,
        charts_real_values=True, charts_multicolour=True, forbidden=INVENTED_CUSTOMER_FIELDS,
        must_contain=[["100"]])], upload=CUSTOMERS, note="Chat A turn 2, clean English"),
    case("R04", "big_report", [turn(
        "Prepare a 15-slide presentation on cybersecurity awareness for new employees.",
        artifact=True, formats_any=["pptx"], doc_min_pages=12)]),
    case("R05", "big_report", [turn(
        "Write a long-form whitepaper, around 5,000 words, on zero-trust architecture for mid-size companies, as a PDF.",
        artifact=True, formats_any=["pdf"], doc_min_words=4000, doc_min_pages=8, doc_min_headings=6)]),
    case("R06", "big_report", [turn(
        "Write an annual sales performance report from this data for the board, as a Word document, with charts for revenue by "
        "region and revenue by month, and a table of units sold per product.",
        artifact=True, formats_any=["docx"], doc_min_pages=3, doc_min_words=900, doc_min_tables=1, min_charts=2,
        charts_real_values=True, charts_multicolour=True)], upload=SALES),

    # ------------------------------------------------------------ plot asks over an uploaded CSV (8)
    case("P01", "csv_plot", [turn("I want a plot of this data.", artifact=True, min_charts=1, charts_real_values=True)],
         upload=CUSTOMERS, note="Chat A turn 1"),
    case("P02", "csv_plot", [turn("Show me a bar chart of customers by country.", artifact=True, min_charts=1,
                                  chart_type_any=["bar", "column", "horizontal_bar", "hbar"], charts_real_values=True)], upload=CUSTOMERS),
    case("P03", "csv_plot", [turn("Plot how many customers subscribed each year.", artifact=True, min_charts=1,
                                  charts_real_values=True)], upload=CUSTOMERS),
    case("P04", "csv_plot", [turn("Make a pie chart of customers by company.", artifact=True, min_charts=1,
                                  chart_type_any=["pie", "donut", "doughnut"], charts_real_values=True, charts_multicolour=True)], upload=CUSTOMERS),
    case("P05", "csv_plot", [turn("Plot monthly revenue for 2025 as a line chart.", artifact=True, min_charts=1,
                                  chart_type_any=["line", "area"], charts_real_values=True)], upload=SALES),
    case("P06", "csv_plot", [turn("Compare revenue by region in a chart, with each region in a different colour.",
                                  min_charts=1, charts_real_values=True, charts_multicolour=True)], upload=SALES),
    case("P07", "csv_plot", [turn("Give me a few charts that explain this sales data.", artifact=True, min_charts=3,
                                  charts_real_values=True, charts_multicolour=True)], upload=SALES),
    case("P08", "csv_plot", [turn("plot units sold per product", artifact=True, min_charts=1, charts_real_values=True)], upload=SALES),

    # ------------------------------------------------------------ follow-up edits (6)
    case("E01", "followup_edit", [
        turn("I want proper this data understand ?? please give Big report ??", artifact=True, formats_any=["docx", "pdf"],
             doc_min_pages=3, forbidden=INVENTED_CUSTOMER_FIELDS),
        turn("also i want Plots on this docs ??", artifact=True, same_artifact=True, min_charts=1, charts_real_values=True,
             charts_multicolour=True),
    ], upload=CUSTOMERS, note="Chat A turns 2-3 verbatim wording"),
    case("E02", "followup_edit", [
        turn("Create a Word report summarising this sales data.", artifact=True, formats_any=["docx"]),
        turn("Add charts to this document: revenue by region and the monthly revenue trend.", artifact=True, same_artifact=True,
             min_charts=2, charts_real_values=True, charts_multicolour=True),
    ], upload=SALES),
    case("E03", "followup_edit", [
        turn("Write a one-page PDF brief on the benefits of remote work for employers.", artifact=True, formats_any=["pdf"]),
        turn("Make it much longer and more detailed, at least 5 pages.", artifact=True, same_artifact=True, doc_min_pages=4,
             doc_growth=2.0),
    ]),
    case("E04", "followup_edit", [
        turn("Create a Word document for our Q3 hiring plan: 4 backend engineers, 2 product designers and 1 product manager, "
             "with a total budget of 1.2 crore rupees.", artifact=True, formats_any=["docx"]),
        turn("Add a table of the roles with their counts, and a chart of headcount by role.", artifact=True, same_artifact=True,
             min_charts=1, doc_min_tables=1),
    ]),
    case("E05", "followup_edit", [
        turn("Make a bar chart of revenue by region.", artifact=True, min_charts=1, charts_real_values=True),
        turn("Now make it a pie chart instead.", artifact=True, same_artifact=True, min_charts=1,
             chart_type_any=["pie", "donut", "doughnut"], charts_real_values=True, charts_multicolour=True),
    ], upload=SALES),
    case("E06", "followup_edit", [
        turn("Explain the pros and cons of microservices compared with a monolith.", artifact=False, min_headings=2, min_bullets=6),
        turn("Put that into a PDF with a comparison table.", artifact=True, formats_any=["pdf"], doc_min_tables=1),
    ]),

    # ------------------------------------------------------------ Fast-mode factual Q&A (6)
    case("Q01", "fast_factual", [turn("What is the capital of Australia?", artifact=False, max_chars=500,
                                      must_contain=[["canberra"]])]),
    case("Q02", "fast_factual", [turn("Who wrote Pride and Prejudice, and when was it published?", artifact=False, max_chars=700,
                                      must_contain=[["austen"], ["1813"]])]),
    case("Q03", "fast_factual", [turn("What's the difference between TCP and UDP?", artifact=False, max_chars=4000,
                                      min_bullets=4, must_contain=[["connection"], ["reliab"]])]),
    case("Q04", "fast_factual", [turn("How many ways can 5 people sit in a row?", artifact=False, max_chars=1200,
                                      must_contain=[["120"]])], note="combinatorics: the adaptive-thinking classifier fires"),
    case("Q05", "fast_factual", [turn("If a train leaves at 3:40 pm and the journey takes 2 hours 35 minutes, when does it arrive?",
                                      artifact=False, max_chars=800, must_contain=[["6:15"]])]),
    case("Q06", "fast_factual", [turn("What does HTTP status 429 mean?", artifact=False, max_chars=1500,
                                      must_contain=[["too many requests"]])]),

    # ------------------------------------------------------------ tables (4)
    case("T01", "tables", [turn("Compare AWS, Azure and Google Cloud for a startup in a table: pricing model, free tier, strengths and weaknesses.",
                                artifact=False, min_table_rows=3, min_table_cols=4, must_contain=[["free tier"]])]),
    case("T02", "tables", [turn("Make a table of the planets in our solar system with their order from the sun, diameter in km and number of known moons.",
                                artifact=False, min_table_rows=8, min_table_cols=3, must_contain=[["jupiter"], ["neptune"]],
                                forbidden=["| pluto"])]),
    case("T03", "tables", [turn("Give me a 4-week study timetable for learning Python, as a table.", artifact=False,
                                min_table_rows=4, min_table_cols=2)]),
    case("T04", "tables", [turn("Compare Python, Java and Go for backend development in a table, then give me a recommendation.",
                                artifact=False, min_table_rows=4, min_table_cols=4, min_chars=600,
                                must_contain=[["recommend"]])]),

    # ------------------------------------------------------------ step-by-step how-tos (4)
    case("H01", "howto", [turn("How do I set up SSH keys for GitHub on Ubuntu? Step by step.", artifact=False, min_numbered=5,
                               min_code_blocks=1, must_contain=[["ssh-keygen"], ["ssh-add", "ssh -t"]])]),
    case("H02", "howto", [turn("Step by step: how do I create a pivot table in Excel?", artifact=False, min_numbered=5,
                               must_contain=[["insert"], ["pivottable", "pivot table"]])]),
    case("H03", "howto", [turn("How do I migrate a PostgreSQL database to a new server with minimal downtime?", artifact=False,
                               min_numbered=5, min_code_blocks=1, min_headings=2,
                               must_contain=[["pg_dump", "pg_basebackup", "logical replication", "replication"]])]),
    case("H04", "howto", [turn("Explain how to file an income tax return online in India, step by step.", artifact=False,
                               min_numbered=5, must_contain=[["incometax.gov.in", "e-filing", "efiling"], ["itr"]])]),
]

#: The 40 cases runs/baseline-20260917 was scored with. Frozen: their wording
#: and their rubric are what every later run is compared against, so a change
#: here invalidates the baseline. New coverage goes below.
BASELINE_CASES = CASES

# ====================================================== acceptance cases ==
#
# Six cases added for the 2026-09-17 round. Each one reproduces something the
# owner reported, or guards a fix from overshooting:
#
#   C01  the three Chat A turns, verbatim, in ONE conversation over the CSV
#   E07  a chart added UNDER an H2 of a single-H1 report (edits._sections
#        treats such a report as one section, so the edit rewrites everything)
#   R07  a one-page brief that must stay one page while B makes reports bigger
#   P09  a PNG ask with no data at all: an honest sentence, and no failed job
#   F07  a Fast turn whose web search falls back — a thinking path in
#        production — which must still show no reasoning
#   K01  colour: one subject is one hue, two subjects are two hues, and a
#        status chart uses the status colours

INCIDENTS = "incidents-2025.csv"

ACCEPTANCE_CASES = [
    case("C01", "csv_plot", [
        turn("I want plot", artifact=True, min_charts=1, charts_real_values=True),
        turn("I want proper this data understand ?? please give Big report ??", artifact=True,
             formats_any=["docx", "pdf"], doc_min_pages=4, doc_min_words=2000, min_charts=1,
             charts_real_values=True, forbidden=INVENTED_CUSTOMER_FIELDS, must_contain=[["100"]]),
        turn("also i want Plots on this docs ??", artifact=True, same_artifact=True, min_charts=2,
             charts_real_values=True, charts_multicolour=True, no_chart_claimed=True),
    ], upload=CUSTOMERS, note="Chat A, verbatim, one conversation: the report the owner actually got"),

    case("E07", "followup_edit", [
        turn("Write a Word report on this sales data with one title and the sections Overview, Regional performance, "
             "Product mix and Outlook as subheadings under it.", artifact=True, formats_any=["docx"],
             doc_min_headings=4),
        turn("Add a bar chart of revenue by region to the Regional performance section.", artifact=True,
             same_artifact=True, min_charts=1, charts_real_values=True, no_chart_claimed=True,
             chart_after_heading=["regional performance"]),
    ], upload=SALES, note="a single H1 with H2 subheadings is ONE section to edits._sections"),

    case("R07", "big_report", [
        turn("Write a one-page PDF brief for our leadership on why we are moving to a four-day week. One page, no more.",
             artifact=True, formats_any=["pdf"], doc_max_pages=2, doc_max_words=900),
        turn("Change the title to 'Four-Day Week: Leadership Brief' and leave the rest as it is.", artifact=True,
             same_artifact=True, doc_max_pages=2, doc_max_growth=1.25,
             must_contain=[["four-day week: leadership brief"]]),
    ], note="the guard on the big-report work: a short ask must stay short, and a retitle must not rewrite"),

    case("P09", "csv_plot", [turn(
        "Plot this data as a PNG.", artifact=False, max_chars=900, no_chart_claimed=True,
        must_contain=[["upload", "attach", "share", "don't have", "do not have", "no data", "haven't"]])],
        note="no upload anywhere in the conversation: one honest sentence, and no job at all"),

    case("F07", "fast_factual", [turn(
        "Search the web and tell me what the current repo rate of the Reserve Bank of India is, and when it was last changed.",
        artifact=False, min_chars=200, max_chars=4000)],
        web_search="on",
        note="SearXNG is unreachable on the QA stack, so this is the search fallback — which grants thinking today"),

    case("K01", "csv_plot", [
        turn("Make a bar chart of incidents by team.", artifact=True, min_charts=1, charts_real_values=True,
             chart_hues_max=1),
        turn("Make one PDF with a chart of incidents by team and a chart of hours lost by team.", artifact=True,
             formats_any=["pdf"], min_charts=2, charts_real_values=True, chart_subject_hues_min=2),
        turn("Now a bar chart of incidents by status.", artifact=True, min_charts=1, charts_real_values=True,
             status_colours=True),
    ], upload=INCIDENTS, note="colour follows content: one subject one hue, two subjects two hues, status tokens"),
]

CASES = BASELINE_CASES + ACCEPTANCE_CASES + CODING_CASES

assert len(BASELINE_CASES) == 40, len(BASELINE_CASES)
assert len(ACCEPTANCE_CASES) == 6, len(ACCEPTANCE_CASES)
assert len(CODING_CASES) == 20, len(CODING_CASES)
assert len({c["id"] for c in CASES}) == len(CASES), "duplicate case id"
