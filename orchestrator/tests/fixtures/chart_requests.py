"""Authored chart requests (no user content) with the VALUES they must produce.

REQUESTS: 71 requests over the synthetic fixtures in tests/fixtures/charts/.
Each names the table the person uploaded, the chart types that count as a
correct reading, and a ground-truth key (a path into ground_truth.json) or a
literal expectation for the category → value mapping of the first series (or
"cat|series" → value when `grouped`). `oracle` is the binding a correct
reading writes; tests/test_artifact_chart_requests.py resolves every oracle
offline against ground truth, and scripts in the AS3 scratch run the live
model on the same requests and score VALUES, not routing.

`lang`: en | hinglish | hi | gu | gujlish | typo — 26 items are non-English or
typo'd.

PROMPT_DATA_CASES: 50 strings with figures typed in the request and the rows
parse_prompt_data must return (None when the text carries no series).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

T = "upload_tickets"
SA = "upload_sales"
E = "upload_employees"
U = "upload_units"
P = "upload_projects"
C = "upload_cashflow"
F = "upload_funnel"
H1 = [{"column": "Date", "op": "lt", "value": "2026-07-01"}]


def R(id: str, lang: str, text: str, table: str, types: Tuple[str, ...], truth: Any, oracle: Dict[str, Any], *,
      grouped: bool = False, style: Optional[Dict[str, Any]] = None, check: str = "values") -> Dict[str, Any]:
    return {"id": id, "lang": lang, "text": text, "table": table, "types": types, "truth": truth, "oracle": oracle,
            "grouped": grouped, "style": style or {}, "check": check}


BARS = ("bar", "horizontal_bar")
PIES = ("pie", "donut")
LINES = ("line", "area", "bar")

REQUESTS: List[Dict[str, Any]] = [
    # ---- English, tickets.xlsx ----
    R("t01", "en", "Make a pie chart of ticket status.", T, PIES, "tickets.status_counts", {"type": "pie", "data": {"table_id": T, "x": "Status", "agg": "count"}}),
    R("t02", "en", "Bar chart of how many tickets each priority has.", T, BARS, "tickets.priority_counts", {"type": "bar", "data": {"table_id": T, "x": "Priority", "agg": "count"}}),
    R("t03", "en", "Show total hours logged per owner as a horizontal bar chart.", T, BARS, "tickets.hours_by_owner", {"type": "horizontal_bar", "data": {"table_id": T, "x": "Owner", "y": ["Hours"]}}),
    R("t04", "en", "Average score by owner, bars please.", T, BARS, "tickets.avg_score_by_owner", {"type": "bar", "data": {"table_id": T, "x": "Owner", "y": ["Score"], "agg": "avg"}}),
    R("t05", "en", "Plot the number of tickets created each month as a line.", T, LINES, "tickets.tickets_by_month", {"type": "line", "data": {"table_id": T, "x": "Created", "agg": "count", "date_bucket": "month"}}),
    R("t06", "en", "Stacked bar of status broken down by priority.", T, ("stacked_bar", "stacked_horizontal_bar"), "tickets.status_priority_counts", {"type": "stacked_bar", "data": {"table_id": T, "x": "Status", "group_by": "Priority", "agg": "count"}}, grouped=True),
    R("t07", "en", "Heatmap of tickets by status and priority.", T, ("heatmap",), "tickets.status_priority_counts", {"type": "heatmap", "data": {"table_id": T, "x": "Status", "group_by": "Priority", "agg": "count"}}, grouped=True),
    R("t08", "en", "Donut chart of the ticket status mix with a title 'Where tickets stand'.", T, PIES, "tickets.status_counts", {"type": "donut", "title": "Where tickets stand", "data": {"table_id": T, "x": "Status", "agg": "count"}}),
    R("t09", "en", "Only open tickets: how many per priority? Column chart.", T, BARS, "tickets.open_by_priority", {"type": "bar", "data": {"table_id": T, "x": "Priority", "agg": "count", "filters": [{"column": "Status", "op": "eq", "value": "Open"}]}}),
    R("t10", "en", "Pie of status with Resolved in green and Open in red.", T, PIES, "tickets.status_counts", {"type": "pie", "data": {"table_id": T, "x": "Status", "agg": "count"}, "style": {"category_colors": {"Resolved": "green", "Open": "red"}}}, style={"category_colors": {"Resolved": "#3F8F4F", "Open": "#C62828"}}),
    R("t11", "en", "Histogram of hours spent per ticket.", T, ("histogram",), None, {"type": "histogram", "data": {"table_id": T, "y": ["Hours"]}}, check="histogram_total:60"),
    R("t12", "en", "Box plot of hours by priority.", T, ("box",), None, {"type": "box", "data": {"table_id": T, "x": "Priority", "y": ["Hours"]}}, check="box_n:60"),
    # ---- English, sales_daily.csv ----
    R("s01", "en", "Plot sales by month as a line.", SA, LINES, "sales.amount_by_month", {"type": "line", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "month"}}),
    R("s02", "en", "Bar chart with blue bars and a title 'Revenue by region'.", SA, BARS, "sales.amount_by_region", {"type": "bar", "title": "Revenue by region", "data": {"table_id": SA, "x": "Region", "y": ["Amount"]}, "style": {"color": "blue"}}, style={"color": "#2F6FB2"}),
    R("s03", "en", "Stacked bar of Q1 and Q2 sales by region.", SA, ("stacked_bar", "stacked_horizontal_bar"), "sales.amount_by_quarter_region_h1", {"type": "stacked_bar", "data": {"table_id": SA, "x": "Date", "date_bucket": "quarter", "group_by": "Region", "y": ["Amount"], "filters": H1}}, grouped=True),
    R("s04", "en", "Area chart of units sold per month.", SA, ("area", "line", "bar"), None, {"type": "area", "data": {"table_id": SA, "x": "Date", "y": ["Units"], "date_bucket": "month"}}, check="sum:Units"),
    R("s05", "en", "Which product brings the most revenue? Chart it.", SA, BARS + PIES, "sales.amount_by_product", {"type": "bar", "data": {"table_id": SA, "x": "Product", "y": ["Amount"]}}),
    R("s06", "en", "Average order amount by region.", SA, BARS, "sales.avg_amount_by_region", {"type": "bar", "data": {"table_id": SA, "x": "Region", "y": ["Amount"], "agg": "avg"}}),
    R("s07", "en", "Line chart of monthly sales for each region, legend on top.", SA, ("line",), "sales.amount_by_month_region", {"type": "line", "data": {"table_id": SA, "x": "Date", "date_bucket": "month", "group_by": "Region", "y": ["Amount"]}, "style": {"legend_position": "top"}}, grouped=True, style={"legend_position": "top"}),
    R("s08", "en", "Count of orders per month, bar chart, y axis from 0 to 30.", SA, BARS + ("line",), "sales.count_by_month", {"type": "bar", "data": {"table_id": SA, "x": "Date", "agg": "count", "date_bucket": "month"}, "style": {"y_min": 0, "y_max": 30}}, style={"y_min": 0, "y_max": 30}),
    R("s09", "en", "Share of revenue by product as a donut.", SA, PIES, "sales.amount_by_product", {"type": "donut", "data": {"table_id": SA, "x": "Product", "y": ["Amount"]}}),
    R("s10", "en", "Units by product, horizontal bars, show data labels.", SA, BARS, "sales.units_by_product", {"type": "horizontal_bar", "data": {"table_id": SA, "x": "Product", "y": ["Units"]}, "style": {"data_labels": "on"}}, style={"data_labels": "on"}),
    R("s11", "en", "Monthly revenue in rupees, line chart with INR formatting.", SA, LINES, "sales.amount_by_month", {"type": "line", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "month"}, "style": {"number_format": "currency_INR"}}, style={"number_format": "currency_INR"}),
    R("s12", "en", "100% stacked column of product mix by region.", SA, ("percent_stacked_bar", "stacked_bar"), None, {"type": "percent_stacked_bar", "data": {"table_id": SA, "x": "Region", "group_by": "Product", "y": ["Amount"]}}, check="sum:Amount"),
    R("s13", "en", "Heatmap of revenue by month and region.", SA, ("heatmap",), "sales.amount_by_month_region", {"type": "heatmap", "data": {"table_id": SA, "x": "Date", "date_bucket": "month", "group_by": "Region", "y": ["Amount"]}}, grouped=True),
    R("s14", "en", "Revenue per quarter as columns.", SA, BARS + ("line",), None, {"type": "bar", "data": {"table_id": SA, "x": "Date", "date_bucket": "quarter", "y": ["Amount"]}}, check="sum:Amount"),
    R("s15", "en", "Radar chart comparing regions on revenue by product.", SA, ("radar",), None, {"type": "radar", "data": {"table_id": SA, "x": "Region", "group_by": "Product", "y": ["Amount"]}}, check="sum:Amount"),
    # ---- English, employees / units / projects / cashflow / funnel ----
    R("e01", "en", "Scatter plot of salary against experience with a trend line.", E, ("scatter",), "trend", {"type": "scatter", "data": {"table_id": E, "x": "Experience", "y": ["Salary"], "trendline": True}}, check="trend"),
    R("e02", "en", "Histogram of salaries.", E, ("histogram",), None, {"type": "histogram", "data": {"table_id": E, "y": ["Salary"]}}, check="histogram_total:80"),
    R("e03", "en", "Box plot of salary by department.", E, ("box",), None, {"type": "box", "data": {"table_id": E, "x": "Department", "y": ["Salary"]}}, check="box_n:80"),
    R("e04", "en", "Headcount per department as a pie.", E, PIES + BARS, "employees.count_by_department", {"type": "pie", "data": {"table_id": E, "x": "Department", "agg": "count"}}),
    R("e05", "en", "Bubble chart: experience on x, salary on y, bubble size by age.", E, ("bubble",), None, {"type": "bubble", "data": {"table_id": E, "x": "Experience", "y": ["Salary"], "size": "Age"}}, check="points:80"),
    R("e06", "en", "Average salary by department, sorted high to low.", E, BARS, "employees.avg_salary_by_department", {"type": "bar", "data": {"table_id": E, "x": "Department", "y": ["Salary"], "agg": "avg", "sort": "value_desc"}}),
    R("u01", "en", "From the Word table: Q1 and Q2 units as bars with Q4 as a line on a second axis.", U, ("combo",), "units.combo", {"type": "combo", "data": {"table_id": U, "x": "Product", "y": ["Q1", "Q2"], "y2": ["Q4"]}}, check="combo"),
    R("u02", "en", "Chart Q1 units by product from the docx.", U, BARS + PIES, "units.q1", {"type": "bar", "data": {"table_id": U, "x": "Product", "y": ["Q1"]}}),
    R("p01", "en", "Gantt-style timeline of the project tasks.", P, ("gantt",), "projects", {"type": "gantt", "data": {"table_id": P, "label": "Task", "start": "Start", "end": "End"}}),
    R("c01", "en", "Waterfall chart of the cash flow items.", C, ("waterfall",), "cashflow", {"type": "waterfall", "data": {"table_id": C, "x": "Item", "y": ["Amount"]}}),
    R("f01", "en", "Funnel of the conversion stages.", F, ("funnel", "horizontal_bar", "bar"), "funnel", {"type": "funnel", "data": {"table_id": F, "x": "Stage", "y": ["Count"]}}),
    R("f02", "en", "Show the funnel as a bar chart with the title 'Conversion' at 16pt.", F, BARS + ("funnel",), "funnel", {"type": "bar", "title": "Conversion", "data": {"table_id": F, "x": "Stage", "y": ["Count"]}, "style": {"title": {"size_pt": 16}}}),
    R("t13", "en", "Tickets per category, bar chart in dark blue.", T, BARS, None, {"type": "bar", "data": {"table_id": T, "x": "Category", "agg": "count"}, "style": {"color": "dark blue"}}, check="sum_count:60", style={"color": "#1F3864"}),
    R("t14", "en", "Critical and high priority tickets by owner, stacked.", T, ("stacked_bar", "stacked_horizontal_bar", "bar"), None, {"type": "stacked_bar", "data": {"table_id": T, "x": "Owner", "group_by": "Priority", "agg": "count", "filters": [{"column": "Priority", "op": "in", "value": ["Critical", "High"]}]}}, check="filtered_count:Priority:Critical,High"),
    R("s16", "en", "Top 2 regions by revenue, everything else as Other.", SA, BARS + PIES, None, {"type": "bar", "data": {"table_id": SA, "x": "Region", "y": ["Amount"], "top_n": 3}}, check="sum:Amount"),
    R("s17", "en", "Weekly order count as a line.", SA, LINES, None, {"type": "line", "data": {"table_id": SA, "x": "Date", "agg": "count", "date_bucket": "week"}}, check="sum_count:150"),
    R("s18", "en", "Yearly revenue total.", SA, BARS + ("line",) + PIES, None, {"type": "bar", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "year"}}, check="sum:Amount"),
    R("e08", "en", "Scatter of age vs salary coloured by department.", E, ("scatter",), None, {"type": "scatter", "data": {"table_id": E, "x": "Age", "y": ["Salary"], "group_by": "Department"}}, check="points:80"),
    # ---- Hinglish ----
    R("h01", "hinglish", "status ka pie chart bana do", T, PIES, "tickets.status_counts", {"type": "pie", "data": {"table_id": T, "x": "Status", "agg": "count"}}),
    R("h02", "hinglish", "har mahine ki sales line graph me dikhao", SA, LINES, "sales.amount_by_month", {"type": "line", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "month"}}),
    R("h03", "hinglish", "region wise sales ka bar chart, neele rang me", SA, BARS, "sales.amount_by_region", {"type": "bar", "data": {"table_id": SA, "x": "Region", "y": ["Amount"]}, "style": {"color": "neela"}}, style={"color": "#2F6FB2"}),
    R("h04", "hinglish", "owner ke hisaab se total hours ka graph banao", T, BARS, "tickets.hours_by_owner", {"type": "bar", "data": {"table_id": T, "x": "Owner", "y": ["Hours"]}}),
    R("h05", "hinglish", "experience vs salary scatter plot, trend line ke saath", E, ("scatter",), "trend", {"type": "scatter", "data": {"table_id": E, "x": "Experience", "y": ["Salary"], "trendline": True}}, check="trend"),
    R("h06", "hinglish", "Q1 aur Q2 ki region wise sales stacked bar me do", SA, ("stacked_bar", "stacked_horizontal_bar"), "sales.amount_by_quarter_region_h1", {"type": "stacked_bar", "data": {"table_id": SA, "x": "Date", "date_bucket": "quarter", "group_by": "Region", "y": ["Amount"], "filters": H1}}, grouped=True),
    R("h07", "hinglish", "department ke hisaab se salary ka box plot bana do", E, ("box",), None, {"type": "box", "data": {"table_id": E, "x": "Department", "y": ["Salary"]}}, check="box_n:80"),
    R("h08", "hinglish", "project ka gantt chart chahiye", P, ("gantt",), "projects", {"type": "gantt", "data": {"table_id": P, "label": "Task", "start": "Start", "end": "End"}}),
    # ---- Hindi ----
    R("hi01", "hi", "टिकट स्टेटस का पाई चार्ट बनाओ", T, PIES, "tickets.status_counts", {"type": "pie", "data": {"table_id": T, "x": "Status", "agg": "count"}}),
    R("hi02", "hi", "हर महीने की बिक्री का लाइन ग्राफ दिखाओ", SA, LINES, "sales.amount_by_month", {"type": "line", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "month"}}),
    R("hi03", "hi", "क्षेत्र के अनुसार बिक्री का बार चार्ट", SA, BARS, "sales.amount_by_region", {"type": "bar", "data": {"table_id": SA, "x": "Region", "y": ["Amount"]}}),
    R("hi04", "hi", "प्राथमिकता के अनुसार टिकटों की संख्या का चार्ट", T, BARS + PIES, "tickets.priority_counts", {"type": "bar", "data": {"table_id": T, "x": "Priority", "agg": "count"}}),
    R("hi05", "hi", "नकदी प्रवाह का वॉटरफॉल चार्ट बनाइए", C, ("waterfall",), "cashflow", {"type": "waterfall", "data": {"table_id": C, "x": "Item", "y": ["Amount"]}}),
    # ---- Gujarati ----
    R("gu01", "gu", "ટિકિટ સ્ટેટસનો પાઇ ચાર્ટ બનાવો", T, PIES, "tickets.status_counts", {"type": "pie", "data": {"table_id": T, "x": "Status", "agg": "count"}}),
    R("gu02", "gu", "દર મહિનાના વેચાણનો લાઇન ગ્રાફ", SA, LINES, "sales.amount_by_month", {"type": "line", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "month"}}),
    R("gu03", "gu", "પ્રદેશ પ્રમાણે વેચાણનો બાર ચાર્ટ બનાવો", SA, BARS, "sales.amount_by_region", {"type": "bar", "data": {"table_id": SA, "x": "Region", "y": ["Amount"]}}),
    R("gu04", "gu", "વિભાગ પ્રમાણે કર્મચારીઓની સંખ્યા", E, BARS + PIES, "employees.count_by_department", {"type": "bar", "data": {"table_id": E, "x": "Department", "agg": "count"}}),
    # ---- Gujlish ----
    R("gj01", "gujlish", "status no pie chart banavo", T, PIES, "tickets.status_counts", {"type": "pie", "data": {"table_id": T, "x": "Status", "agg": "count"}}),
    R("gj02", "gujlish", "mahina pramane sales no line graph aapo", SA, LINES, "sales.amount_by_month", {"type": "line", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "month"}}),
    R("gj03", "gujlish", "funnel chart banavi aapo stages nu", F, ("funnel", "bar", "horizontal_bar"), "funnel", {"type": "funnel", "data": {"table_id": F, "x": "Stage", "y": ["Count"]}}),
    # ---- typos ----
    R("ty01", "typo", "pie chrat of staus", T, PIES, "tickets.status_counts", {"type": "pie", "data": {"table_id": T, "x": "Status", "agg": "count"}}),
    R("ty02", "typo", "plot slaes by mnth as a lien", SA, LINES, "sales.amount_by_month", {"type": "line", "data": {"table_id": SA, "x": "Date", "y": ["Amount"], "date_bucket": "month"}}),
    R("ty03", "typo", "bar grpah of revnue per regoin", SA, BARS, "sales.amount_by_region", {"type": "bar", "data": {"table_id": SA, "x": "Region", "y": ["Amount"]}}),
    R("ty04", "typo", "scater plot salry vs experiance with trnd line", E, ("scatter",), "trend", {"type": "scatter", "data": {"table_id": E, "x": "Experience", "y": ["Salary"], "trendline": True}}, check="trend"),
    R("ty05", "typo", "histogarm of hours", T, ("histogram",), None, {"type": "histogram", "data": {"table_id": T, "y": ["Hours"]}}, check="histogram_total:60"),
    R("ty06", "typo", "stacked bar q1 q2 sales by regon", SA, ("stacked_bar", "stacked_horizontal_bar"), "sales.amount_by_quarter_region_h1", {"type": "stacked_bar", "data": {"table_id": SA, "x": "Date", "date_bucket": "quarter", "group_by": "Region", "y": ["Amount"], "filters": H1}}, grouped=True),
]

assert len(REQUESTS) == 71, len(REQUESTS)
assert sum(1 for r in REQUESTS if r["lang"] != "en") >= 26


# ------------------------------------------------------------ typed figures --

PROMPT_DATA_CASES: List[Tuple[str, Optional[List[List[Any]]]]] = [
    ("plot sales by month as a line: Jan 10, Feb 12, Mar 15", [["Jan", 10.0], ["Feb", 12.0], ["Mar", 15.0]]),
    ("north 120, south 95", [["north", 120.0], ["south", 95.0]]),
    ("x = 1,2,3 and y = 4,5,6", [[1.0, 4.0], [2.0, 5.0], [3.0, 6.0]]),
    ("(1,52) (2,55) (3,61)", [[1.0, 52.0], [2.0, 55.0], [3.0, 61.0]]),
    ("Rent 40%, Food 25%, Travel 15%, Savings 20%", [["Rent", 40.0], ["Food", 25.0], ["Travel", 15.0], ["Savings", 20.0]]),
    ("Jan: 100\nFeb: 120\nMar: 90", [["Jan", 100.0], ["Feb", 120.0], ["Mar", 90.0]]),
    ("Mumbai 1,20,000; Delhi 95,000; Pune 60,000", [["Mumbai", 120000.0], ["Delhi", 95000.0], ["Pune", 60000.0]]),
    ("Q1 1.2 lakh, Q2 1.5 lakh, Q3 90k", [["Q1", 120000.0], ["Q2", 150000.0], ["Q3", 90000.0]]),
    ("Alpha 30, Beta 45, Gamma 12", [["Alpha", 30.0], ["Beta", 45.0], ["Gamma", 12.0]]),
    ("Jan: १०\nFeb: १२\nMar: १५", [["Jan", 10.0], ["Feb", 12.0], ["Mar", 15.0]]),
    ("Surat ૪૦, Rajkot ૨૫, Vadodara ૩૫", [["Surat", 40.0], ["Rajkot", 25.0], ["Vadodara", 35.0]]),
    ("pie chart banao: rent 15000, khana 8000, travel 3000", [["rent", 15000.0], ["khana", 8000.0], ["travel", 3000.0]]),
    ("2 crore in West, 1.5 crore in East", None),
    ("West 2 crore, East 1.5 crore", [["West", 20000000.0], ["East", 15000000.0]]),
    ("sales: Monday 20, Tuesday 25, Wednesday 18, Thursday 30, Friday 22", [["Monday", 20.0], ["Tuesday", 25.0], ["Wednesday", 18.0], ["Thursday", 30.0], ["Friday", 22.0]]),
    ("make a bar chart of Team A 8, Team B 11 and Team C 5", [["Team A", 8.0], ["Team B", 11.0], ["Team C", 5.0]]),
    ("revenue: Product X = 500, Product Y = 700", [["Product X", 500.0], ["Product Y", 700.0]]),
    ("hours = 1, 2, 3, 4 and score = 50, 60, 65, 80", [[1.0, 50.0], [2.0, 60.0], [3.0, 65.0], [4.0, 80.0]]),
    ("(10, 3.5) (20, 4.1) (30, 4.8) (40, 5.0)", [[10.0, 3.5], [20.0, 4.1], [30.0, 4.8], [40.0, 5.0]]),
    ("marketing ₹50,000, tech ₹1,20,000, ops ₹30,000", [["marketing", 50000.0], ["tech", 120000.0], ["ops", 30000.0]]),
    ("USA $1,200, UK $950, India $2,300", [["USA", 1200.0], ["UK", 950.0], ["India", 2300.0]]),
    ("2021: 120\n2022: 150\n2023: 180", None),
    ("Q1 - 40, Q2 - 55, Q3 - 61, Q4 - 70", [["Q1", 40.0], ["Q2", 55.0], ["Q3", 61.0], ["Q4", 70.0]]),
    ("Open 14; Closed 8; Resolved 24", [["Open", 14.0], ["Closed", 8.0], ["Resolved", 24.0]]),
    ("north me 120, south me 95, east me 130", [["north", 120.0], ["south", 95.0], ["east", 130.0]]),
    ("भोजन 30%, किराया 45%, यात्रा 25%", [["भोजन", 30.0], ["किराया", 45.0], ["यात्रा", 25.0]]),
    ("ખોરાક 30, ભાડું 45, મુસાફરી 25", [["ખોરાક", 30.0], ["ભાડું", 45.0], ["મુસાફરી", 25.0]]),
    ("just make a nice chart please", None),
    ("I have 3 kids and 2 dogs", None),
    ("the meeting is at 10 tomorrow", None),
    ("give me a summary of the 5 key points", None),
    ("Sales grew 12% last year", None),
    ("Jan 10", None),
    ("apples 5 and oranges 7", [["apples", 5.0], ["oranges", 7.0]]),
    ("Chennai: 45.5, Kolkata: 38.25, Hyderabad: 50", [["Chennai", 45.5], ["Kolkata", 38.25], ["Hyderabad", 50.0]]),
    ("Loss -20, Profit 35", [["Loss", -20.0], ["Profit", 35.0]]),
    ("Online 3.2 million, Retail 1.1 million", [["Online", 3200000.0], ["Retail", 1100000.0]]),
    ("x: 1, 2, 3\ny: 2, 4, 9", [[1.0, 2.0], [2.0, 4.0], [3.0, 9.0]]),
    ("temperature by day Mon 31, Tue 33, Wed 29", [["Mon", 31.0], ["Tue", 33.0], ["Wed", 29.0]]),
    ("A=5, B=9, C=2", [["A", 5.0], ["B", 9.0], ["C", 2.0]]),
    ("bar chart karo: Asha 12, Ravi 9, Meera 15", [["Asha", 12.0], ["Ravi", 9.0], ["Meera", 15.0]]),
    ("ગ્રાફ બનાવો: Surat 40, Rajkot 25, Vadodara 35", [["Surat", 40.0], ["Rajkot", 25.0], ["Vadodara", 35.0]]),
    ("line graph: week1 200, week2 260, week3 310, week4 280", [["week1", 200.0], ["week2", 260.0], ["week3", 310.0], ["week4", 280.0]]),
    ("Electronics 45 %, Clothing 30 %, Grocery 25 %", [["Electronics", 45.0], ["Clothing", 30.0], ["Grocery", 25.0]]),
    ("Plan 100, Actual 92", [["Plan", 100.0], ["Actual", 92.0]]),
    ("Delhi ५०, Mumbai ७०", [["Delhi", 50.0], ["Mumbai", 70.0]]),
    ("region: North 1.5k, South 2.25k", [["North", 1500.0], ["South", 2250.0]]),
    ("points (0,0) (1,1)", [[0.0, 0.0], [1.0, 1.0]]),
    ("Engineering 142 | Sales 96 | Support 64", [["Engineering", 142.0], ["Sales", 96.0], ["Support", 64.0]]),
    ("Feb 12, Mar 15 and Apr 9", [["Feb", 12.0], ["Mar", 15.0], ["Apr", 9.0]]),
]

assert len(PROMPT_DATA_CASES) >= 50, len(PROMPT_DATA_CASES)


# ------------------------------------------------------------------ scoring --


def _truth(gt: Dict[str, Any], key: str) -> Any:
    if key == "units.q1":
        return {k: v[0] for k, v in gt["units"].items()}
    if key == "units.combo":
        return gt["units"]
    node: Any = gt
    for part in key.split("."):
        node = node[part]
    return node


def score(req: Dict[str, Any], chart: Any, gt: Dict[str, Any], tables: Optional[List[Any]] = None) -> Tuple[bool, str]:
    """WORKS when the chart type is one of the accepted readings AND the
    numbers in the resolved chart equal ground truth (or the declared check
    holds) AND every requested style value is present. Returns (ok, why)."""
    import math

    if chart is None:
        return False, "no chart"
    if chart.type not in req["types"]:
        return False, f"type {chart.type} not in {req['types']}"
    check = req["check"]
    close = lambda a, b: math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-6)  # noqa: E731
    if check == "values":
        truth = _truth(gt, req["truth"])
        if req["grouped"]:
            got = {f"{cat}|{s.name}": v for s in chart.series for cat, v in zip(chart.categories, s.values) if v}
            want = {k: v for k, v in truth.items() if v}
        elif req["truth"] == "projects" or req["truth"] == "cashflow" or req["truth"] == "funnel" or isinstance(truth, dict):
            if chart.type in ("pie", "donut", "bar", "horizontal_bar", "funnel", "waterfall", "gantt", "line", "area") and chart.series:
                got = dict(zip(chart.categories, chart.series[0].values))
            else:
                return False, "no series"
            want = truth
        else:
            return False, "unknown truth"
        if set(got) != set(want):
            return False, f"categories {sorted(got)[:6]} != {sorted(want)[:6]}"
        bad = [k for k in want if not close(got[k], want[k])]
        if bad:
            return False, f"value for {bad[0]}: {got[bad[0]]} != {want[bad[0]]}"
    elif check == "trend":
        tr = chart.extra.trendlines[0] if chart.extra and chart.extra.trendlines else None
        t = gt["employees"]["trend_salary_on_experience"]
        if tr is None or not (math.isclose(tr.slope, t["slope"], rel_tol=1e-9) and math.isclose(tr.intercept, t["intercept"], rel_tol=1e-9)):
            return False, "trend line missing or wrong"
    elif check == "combo":
        want = gt["units"]
        names = [s.name for s in chart.series]
        if not {"Q1", "Q2", "Q4"} <= set(names):
            return False, f"series {names}"
        for s in chart.series:
            q = int(s.name[1]) - 1
            if [v for v in s.values] != [want[p][q] for p in chart.categories]:
                return False, f"series {s.name} values"
        if not any(s.name == "Q4" and (s.axis == "secondary" or s.kind == "line") for s in chart.series):
            return False, "Q4 is not a line/secondary"
    elif check.startswith("histogram_total:"):
        if sum(v for v in chart.series[0].values) != float(check.split(":")[1]):
            return False, "histogram counts do not cover every row"
    elif check.startswith("box_n:"):
        if not chart.extra or sum(b.n for b in chart.extra.box) != int(check.split(":")[1]):
            return False, "box plot does not cover every row"
    elif check.startswith("points:"):
        if sum(len(s.values) for s in chart.series) != int(check.split(":")[1]):
            return False, "points"
    elif check.startswith("sum:"):
        col = check.split(":")[1]
        table = next(t for t in (tables or []) if t.id == req["table"])
        j = table.columns.index(col)
        total = sum(float(r[j]) for r in table.rows)
        if not close(sum(v for s in chart.series for v in s.values), total):
            return False, f"total {sum(v for s in chart.series for v in s.values)} != {total}"
    elif check.startswith("sum_count:"):
        if sum(v for s in chart.series for v in s.values) != float(check.split(":")[1]):
            return False, "count total"
    elif check.startswith("filtered_count:"):
        _, col, vals = check.split(":")
        table = next(t for t in (tables or []) if t.id == req["table"])
        j = table.columns.index(col)
        want_n = sum(1 for r in table.rows if r[j] in vals.split(","))
        if sum(v for s in chart.series for v in s.values) != want_n:
            return False, "filtered count"
    elif check == "nonempty":
        if not chart.series:
            return False, "empty"
    for key, want in (req.get("style") or {}).items():
        got = getattr(chart.style, key, None) if chart.style else None
        if isinstance(want, dict):
            if not got or any(got.get(k) != v for k, v in want.items()):
                return False, f"style {key} {got} != {want}"
        elif key == "color" and got != want and chart.style and len(chart.series) == 1 and chart.style.series_colors.get(chart.series[0].name) == want:
            continue  # "blue bars" as the one series' colour is the same request
        elif got != want:
            return False, f"style {key} {got!r} != {want!r}"
    return True, "ok"
