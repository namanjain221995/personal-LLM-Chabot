"""What this org MEANS, as opposed to what it is called.

`sf_dictionary` answers "what is the API name for the thing the user said".
That is necessary and not sufficient. A model can know `Interview__c` exists,
know `Interview_Outcome__c` is a picklist, know `Ghosted` is one of its values
— and still answer "how many interviews did we run" with 33,147 when the true
answer is 26,904, because 5,566 of those rows are Initial Calls and 673 have no
record type at all.

The difference between those two numbers is not vocabulary. It is domain
knowledge, and there is nowhere in the pipeline it can be derived from. So it
lives here, in three parts:

  ORG_BRIEF       — the business model and the handful of traps that make a
                    plausible query wrong. Small, and injected on every
                    data question.
  SQL_HARD_RULES  — DuckDB/warehouse mechanics appended to the SQL system
                    prompt. Chiefly: EVERY column in the warehouse is VARCHAR.
  METRICS         — the semantic layer. Canonical definitions for the
                    business's real measures, injected only when a question
                    matches one, so that "ghosting rate" means the same thing
                    on Tuesday as it did on Monday.

Kept as data, not prose, so the metric a question matches can be tested.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Sequence

# ---------------------------------------------------------------------------
# Layer 0 — the business
# ---------------------------------------------------------------------------

#: Injected on every question that touches org data. Deliberately short: it
#: competes for the same context as the schema and the field dictionary, and
#: everything here has to earn its tokens by preventing a specific wrong answer.
ORG_BRIEF = """\
About this org (TechSara — an IT staffing "recruit, train and market" business):
candidates enrol, get trained, are marketed to employers, sit interviews with
support from internal staff, and are invoiced on a payment plan.

The five things almost every question is really about:
- Account = a PERSON, not a company. Candidates are Person Accounts. Account
  also stores a few B2B clients and, confusingly, internal recruiters — tell
  them apart by record type, never assume every Account is a candidate.
- Interview__c = one client-facing interview a candidate sat. The org's
  busiest object, and the source of most metrics.
- Internal_Interview__c = an INTERNAL assessment/mock, scored by AI and a
  human. Completely separate from Interview__c. "Interview" alone almost
  always means Interview__c; say which one you used.
- Invoice__c / Payment__c = what the candidate owes and has paid. Payment__c
  is a child of Invoice__c.
- Candidate_Training__c / Session__c = the training programme and its
  scheduled sessions.

Recruiter__c is labelled "Employees" and is the internal staff object — a
trainer, interviewer or support person is a Recruiter__c, not a User."""

#: The traps. Each one is a query that looks right and returns a wrong number,
#: so each is stated as the rule that prevents it.
ORG_RULES = """\
Rules that decide whether the number is right:
- Account mixes three populations, and ONLY the record type separates them.
  Join RecordType on Account.RecordTypeId: 'Person Account' = candidates,
  'Recruiter' = internal staff, 'B2B Client' = client companies.
  IsPersonAccount is NOT the candidate test — recruiters are person accounts
  too, and every one of them carries a Candidate_Status__c value. Filtering on
  IsPersonAccount = 'true' nearly doubles the active-candidate count.
  The candidate filter is always:
    JOIN RecordType rt ON a.RecordTypeId = rt.Id WHERE rt.Name = 'Person Account'
- Interview__c mixes 'Interview' with 'Initial Call' (and a few 'B2B
  Interviews'). Unless the question asks for initial calls, filter to record
  type 'Interview'. Some rows have no record type — say so rather than
  silently dropping or including them.
- Interview_Outcome__c is empty on roughly half of all interviews (they are
  scheduled, cancelled or simply not dispositioned). Any rate computed from it
  must state its denominator: outcome-known interviews, not all interviews.
- Real data contains picklist values that are no longer on the picklist.
  Report what the data says; do not silently drop a value you did not expect.
- The B2B side (Job_Requirement__c, Job_Submission__c, Client_Type__c) is
  newly built and nearly empty. If a query over it returns almost nothing,
  say the process is not populated yet — do not present it as a business
  result of zero.
- The KIND of an internal assessment is not a picklist. Internal_Interview__c
  .Interview_Type__c is a LOOKUP to the Interview_Type__c table; join it and
  compare Interview_Type__c.Name. The names in use are OOT, Intake, Mock,
  Practice Mock Interview, Upcoming Interview, Rejection Interview,
  Rejection Interview and the per-programme training weeks. "OOT mock",
  "intake call" and similar phrases mean this join — never a LIKE on a status
  field, and never Mock_Status__c, which is null on all but a couple of rows.
- Internal_Interview__c.Date__c is empty on every row in practice. The usable
  date is Scheduled_Date__c. Filtering assessments on Date__c returns nothing
  and looks like a real zero.
- NEVER match a person by an equals on their name. Stored names carry
  salutations, a literal "n/a" where the middle name goes, and spellings that
  differ from how people type them — "Rakshit Bodakuntla" is stored as
  "Rakshith n/a Bodakuntla", so Name = '...' returns zero rows while
  Name ILIKE '%bodakuntla%' finds them. Match the most distinctive token,
  usually the surname:  WHERE a.Name ILIKE '%<surname>%'
  If that still finds nobody, say no candidate matches that name — never
  report their records as absent, and never fall back to a text search."""

#: The business runs on IST; the container clock is UTC, so between 18:30 and
#: midnight IST `CURRENT_DATE` is YESTERDAY to the user. Asked for "today",
#: that silently returns the wrong day's work.
BUSINESS_TIMEZONE = "Asia/Kolkata"


# ---------------------------------------------------------------------------
# Layer 1 — warehouse mechanics
# ---------------------------------------------------------------------------

#: Shared by both query dialects, because the mistake is dialect-independent:
#: "give me the list of the total oot mocks taken today" was answered `12` by
#: the warehouse and `SELECT count()` by the live API. Neither is a list.
LIST_NOT_COUNT = """\
- "List", "show me", "which" and "who" ask for ROWS, not a total. Return the
  identifying fields — the person's name, the status, the date — one row per
  record, and never a bare COUNT. Add a total alongside only when the question
  also asks how many.
- BUT only the first 30 rows reach the stage that writes the answer. When a
  question is about ONE person or ONE thing and the child records run to
  hundreds, do NOT return them raw — aggregate to one row per meaningful
  grouping (per enrolment, per status, per month) with counts. A profile query
  that returns 227 rows gets summarised from the first 30 of them, which
  silently reports one enrolment's worth of history as if it were all of it."""

#: Appended to the SQL system prompt. The casting rule is first because it is
#: the only one on this list whose absence produces a WRONG ANSWER rather than
#: an error, and it fires on the most common question shape there is.
SQL_HARD_RULES = """\
- EVERY column in this warehouse is VARCHAR — amounts, dates, counts, all of
  it. You MUST cast before any arithmetic, comparison or sort:
    * numbers: TRY_CAST(col AS DOUBLE)
    * dates:   TRY_CAST(col AS DATE)      datetimes: TRY_CAST(col AS TIMESTAMP)
  ORDER BY on an uncast amount sorts it as TEXT, so '999' beats '27000' and
  your "top 10 by value" is wrong without erroring. Always
  ORDER BY TRY_CAST(col AS DOUBLE) DESC. Use TRY_CAST, never CAST: one
  unparseable value would abort the whole query.
- Aggregates need the same cast: SUM(TRY_CAST(Invoice_Amount__c AS DOUBLE)),
  and round money to 2 decimals.
- Salesforce checkbox columns land as the lowercase TEXT 'true' / 'false'.
  Compare as text: WHERE IsPersonAccount = 'true'. Never = True, 1 or 'True'.
- To group by month use
  date_trunc('month', TRY_CAST(<date col> AS DATE)) and order by it, not by
  a formatted string.
- A Salesforce lookup is OPTIONAL on almost every record. Use LEFT JOIN for any
  table you are adding only to show a name; an INNER JOIN silently deletes every
  row whose lookup is empty. Measured: "sessions for the interview readiness
  training yesterday" returned the right 24 rows until `JOIN Cohort__c` was
  added to display the slot — those trainings have no slot, so the answer became
  0 with no error. INNER JOIN only when the question genuinely requires the
  related record to exist.
- NEVER put a raw 18-character Salesforce Id in a result a person will read.
  Every lookup column you SELECT must be joined to its parent and shown as
  that parent's Name. A row of Ids is not an answer — the reader cannot tell
  which programme, slot or trainer it means. Common ones:
    Candidate__c / Client_Account__c  -> Account.Name
    Program__c                        -> Program__c.Name
    Program_Version__c                -> Program_Version__c.Name
    Cohort__c                         -> Cohort__c.Name   (the "Slot NNN")
    Interview_Type__c                 -> Interview_Type__c.Name
    Assigned_Trainer__c / Interview_Support_Person__c
                                      -> Recruiter__c.First_Name__c, Last_Name__c
    RecordTypeId                      -> RecordType.Name
  Select Id as well only when the user asked for record links.
- This business runs on IST and the server clock is UTC, so CURRENT_DATE is
  the WRONG day for part of every evening. For "today", "yesterday" and
  "this week" use the business day:
    (now() AT TIME ZONE 'Asia/Kolkata')::date
  e.g. WHERE TRY_CAST(Scheduled_Date__c AS DATE)
         = (now() AT TIME ZONE 'Asia/Kolkata')::date
""" + LIST_NOT_COUNT


#: Appended to the narrative composer's system prompt. These are about honesty
#: rather than mechanics — the query already ran, and the remaining ways to be
#: wrong are all about what the answer claims.
ANSWER_RULES = """\
- State the population you counted whenever it is not obvious: interviews
  excluding initial calls, candidates rather than all Accounts, outcome-known
  interviews rather than all of them. A number without its population invites
  the reader to assume the wrong one.
- Zero rows and no process are different answers. If a table is essentially
  unpopulated, say the process is not in use yet rather than reporting 0 as a
  business result.
- An EMPTY result is not a finding about the business. A query returns nothing
  when the records are absent, but equally when the join ran through the wrong
  object, the name is stored differently (a middle name, a different case, a
  Recruiter__c row rather than an Account), the date literal did not parse, or
  a filter was too narrow — none of which raise an error. So report what the
  query looked for and that it found nothing; never write that a person has no
  records, that a process never happened, or that the data is not in the
  system. When a person was named, say plainly that they may be recorded under
  a different object or spelling, and offer to look again.
- Never repeat credentials or personal identifiers even when a row contains
  them — passwords, portal credentials, SSN digits, passport or bank numbers.
  Say the field exists and that you will not read it out.
- If the question could mean two different objects (a client-facing interview
  or an internal assessment; a recruiter Account or a Recruiter__c employee),
  answer the more likely one and say which you used in one short clause.
- Never draw a chart out of text — no ASCII or Unicode bars (█ ▓ ■ #), no
  code blocks arranged as a graph, no emoji charts. This interface renders
  real interactive charts; a text imitation is unreadable on small screens
  and to screen readers. Give figures as a list or table and, if the user
  wanted a chart that is not attached, say the data below can be charted."""


# ---------------------------------------------------------------------------
# Layer 2 — the semantic layer
# ---------------------------------------------------------------------------
# One entry per measure the business actually talks about. `sql` is the
# canonical expression; it is guidance for the model, never executed from here.

METRICS: List[Dict[str, Any]] = [
    {
        "name": "interviews conducted",
        "aliases": ["interview", "interviews", "interview count", "interviews run",
                    "how many interviews", "interview volume"],
        "table": "Interview__c",
        "definition": "Client-facing interviews, excluding initial calls.",
        "sql": ("SELECT count(*) FROM Interview__c i JOIN RecordType rt "
                "ON i.RecordTypeId = rt.Id WHERE rt.Name = 'Interview'"),
        "date_column": "Date_of_Interview__c",
        "chart": "line over month, bar when broken down by a category",
        "caveat": "Excludes 'Initial Call' record types; state that you did.",
    },
    {
        "name": "initial calls",
        "aliases": ["initial call", "initial calls", "first call", "intro call"],
        "table": "Interview__c",
        "definition": "The first screening call, a separate record type on the "
                      "same object as interviews.",
        "sql": ("SELECT count(*) FROM Interview__c i JOIN RecordType rt "
                "ON i.RecordTypeId = rt.Id WHERE rt.Name = 'Initial Call'"),
        "date_column": "Date_of_Interview__c",
        "chart": "line over month",
        "caveat": "Never add these to the interview count unless asked for both.",
    },
    {
        "name": "ghosting rate",
        "aliases": ["ghost", "ghosted", "ghosting", "no show rate", "ghost rate"],
        "table": "Interview__c",
        "definition": "Share of dispositioned interviews whose outcome was Ghosted.",
        "sql": ("SELECT count(*) FILTER (WHERE Interview_Outcome__c = 'Ghosted') "
                "* 1.0 / nullif(count(*) FILTER "
                "(WHERE Interview_Outcome__c IS NOT NULL), 0) FROM Interview__c"),
        "date_column": "Date_of_Interview__c",
        "chart": "line over month",
        "caveat": "Denominator is outcome-known interviews only — about half of "
                  "all rows have no outcome. Always say so.",
    },
    {
        "name": "interview outcome mix",
        "aliases": ["outcome", "outcomes", "rejected", "moved to next round",
                    "offer received", "reference check"],
        "table": "Interview__c",
        "definition": "Distribution of Interview_Outcome__c.",
        "sql": ("SELECT Interview_Outcome__c, count(*) FROM Interview__c "
                "GROUP BY 1 ORDER BY 2 DESC"),
        "chart": "bar; donut only if you exclude the NULL bucket and say so",
        "caveat": "Show the unset bucket explicitly or state that it is excluded.",
    },
    {
        "name": "active candidates",
        "aliases": ["active candidate", "active candidates", "candidates on the "
                    "bench", "how many candidates"],
        "table": "Account",
        "definition": "Candidate-record-type Accounts whose status is Active.",
        "sql": ("SELECT count(*) FROM Account a JOIN RecordType rt "
                "ON a.RecordTypeId = rt.Id WHERE rt.Name = 'Person Account' "
                "AND a.Candidate_Status__c = 'Active'"),
        "chart": "single value, or bar by Niche__c",
        "caveat": "Must filter by record type. IsPersonAccount also matches the "
                  "259 recruiter Accounts, which all carry a candidate status, "
                  "and inflates this figure by ~87%.",
    },
    {
        "name": "candidate pipeline by status",
        "aliases": ["candidate status", "pipeline", "candidates by status",
                    "placed", "on hold", "terminated"],
        "table": "Account",
        "definition": "Candidates grouped by Candidate_Status__c.",
        "sql": ("SELECT a.Candidate_Status__c, count(*) FROM Account a "
                "JOIN RecordType rt ON a.RecordTypeId = rt.Id "
                "WHERE rt.Name = 'Person Account' GROUP BY 1 ORDER BY 2 DESC"),
        "chart": "bar (funnel only if you order the statuses by lifecycle)",
        "caveat": "Statuses are In Progress, Active, Hold, Paused, Placed, "
                  "Terminate, Closed — not a strict funnel.",
    },
    {
        "name": "invoiced amount",
        "aliases": ["invoiced", "billed", "invoice amount", "invoice total",
                    "revenue billed", "total invoiced"],
        "table": "Invoice__c",
        "definition": "Sum of invoice face value.",
        "sql": ("SELECT round(sum(TRY_CAST(Invoice_Amount__c AS DOUBLE)), 2) "
                "FROM Invoice__c"),
        "date_column": "Invoice_Date__c",
        "chart": "line over month",
        "caveat": "Invoiced is not collected — use collections for cash.",
    },
    {
        "name": "collections",
        "aliases": ["collected", "cash collected", "payments received",
                    "amount paid", "receipts"],
        "table": "Payment__c",
        "definition": "Successful payments actually received.",
        "sql": ("SELECT round(sum(TRY_CAST(Payment_Amount__c AS DOUBLE)), 2) "
                "FROM Payment__c WHERE Payment_Status__c = 'Paid'"),
        "date_column": "Payment_Date__c",
        "chart": "line over month, or bar vs invoiced",
        "caveat": "Exclude Voided payments. Payment__c is a child of Invoice__c.",
    },
    {
        "name": "outstanding balance",
        "aliases": ["outstanding", "unpaid", "owed", "receivable", "arrears",
                    "not paid", "overdue"],
        "table": "Invoice__c",
        "definition": "Invoice value not yet collected.",
        "sql": ("SELECT round(sum(TRY_CAST(Outstanding_Amount__c AS DOUBLE)), 2) "
                "FROM Invoice__c WHERE Invoice_Status__c IN "
                "('Not Paid', 'Partially Paid')"),
        "date_column": "Due_Date__c",
        "chart": "bar by ageing bucket or by candidate",
        "caveat": "Void and Dispute invoices are not arrears — exclude or split.",
    },
    {
        "name": "payment issues",
        "aliases": ["dispute", "disputed", "chargeback", "ach return",
                    "payment issue", "refund"],
        "table": "Payment__c",
        "definition": "Payments in the dispute / ACH-return workflow.",
        "sql": ("SELECT Payment_Issue_Status__c, count(*) FROM Payment__c "
                "WHERE Payment_Issue_Status__c IS NOT NULL GROUP BY 1"),
        "date_column": "Payment_Issue_Received_Date__c",
        "chart": "bar",
        "caveat": "Issue types are Dispute, ACH Return, Bank Transfer Canceled, "
                  "Retrieval Request.",
    },
    {
        "name": "training enrolment",
        "aliases": ["training", "trainings", "enrolled", "cohort", "programme",
                    "program", "in training"],
        "table": "Candidate_Training__c",
        "definition": "Candidate training runs by status.",
        "sql": ("SELECT Status__c, count(*) FROM Candidate_Training__c "
                "GROUP BY 1 ORDER BY 2 DESC"),
        "date_column": "Start_Date__c",
        "chart": "bar; line over start month for intake",
        "caveat": "Statuses are Planned, Active, Paused, Completed, Dropped.",
    },
    {
        "name": "training drop rate",
        "aliases": ["drop", "dropped", "dropout", "drop rate", "attrition"],
        "table": "Candidate_Training__c",
        "definition": "Share of training runs that ended in Dropped.",
        "sql": ("SELECT count(*) FILTER (WHERE Status__c = 'Dropped') * 1.0 "
                "/ nullif(count(*), 0) FROM Candidate_Training__c"),
        "date_column": "Start_Date__c",
        "chart": "line over cohort start month",
        "caveat": "Active runs can still drop later — cohorts are not comparable "
                  "until they finish.",
    },
    {
        "name": "internal assessments by type",
        "aliases": ["oot", "oot mock", "oot mocks", "intake", "intake call",
                    "mock", "mocks", "mock interview", "mock interviews",
                    "practice mock", "assessments taken"],
        "table": "Internal_Interview__c",
        "definition": ("Internal assessments of a given kind. The kind lives on "
                       "the Interview_Type__c lookup, not on the record."),
        "sql": ("SELECT count(*) FROM Internal_Interview__c ii "
                "JOIN Interview_Type__c it ON ii.Interview_Type__c = it.Id "
                "WHERE it.Name = 'OOT' AND TRY_CAST(ii.Scheduled_Date__c AS DATE) "
                "= (now() AT TIME ZONE 'Asia/Kolkata')::date"),
        "date_column": "Scheduled_Date__c",
        "chart": "bar by type, or line over scheduled date",
        "caveat": ("Date__c is empty on every row — use Scheduled_Date__c. "
                   "Mock_Status__c is null on all but two rows and is not the "
                   "mock filter. 'Taken' usually means Status__c = 'Completed'; "
                   "if you count all statuses, say the total includes scheduled "
                   "and rescheduled ones."),
    },
    {
        "name": "assessment hire decision",
        "aliases": ["mock", "mock interview", "assessment", "hire decision",
                    "ai decision", "human decision", "internal interview"],
        "table": "Internal_Interview__c",
        "definition": "Outcome of internal assessments, AI and human scored.",
        "sql": ("SELECT AI_Decision__c, Human_Decision__c, count(*) "
                "FROM Internal_Interview__c WHERE Status__c = 'Completed' "
                "GROUP BY 1, 2"),
        "date_column": "Date__c",
        "chart": "bar, or scatter for AI_Total_score__c vs Human_Total_Score__c",
        "caveat": "This is the INTERNAL assessment object, not Interview__c.",
    },
    {
        "name": "interview support load",
        "aliases": ["support person", "support load", "interview support",
                    "who supported", "supporter", "bandwidth"],
        "table": "Interview__c",
        "definition": "Interviews per internal support person.",
        "sql": ("SELECT r.First_Name__c, r.Last_Name__c, count(*) "
                "FROM Interview__c i JOIN Recruiter__c r "
                "ON i.Interview_Support_Person__c = r.Id GROUP BY 1, 2 "
                "ORDER BY 3 DESC"),
        "date_column": "Date_of_Interview__c",
        "chart": "horizontal_bar",
        "caveat": "Support people are Recruiter__c records, not Users.",
    },
    {
        "name": "assignment health",
        "aliases": ["assignment", "assigned", "unassigned", "no bandwidth",
                    "reassign", "reshuffle"],
        "table": "Interview__c",
        "definition": "How the AI support-assignment process is performing.",
        "sql": ("SELECT Assignment_Status__c, count(*) FROM Interview__c "
                "GROUP BY 1 ORDER BY 2 DESC"),
        "date_column": "Date_of_Interview__c",
        "chart": "bar",
        "caveat": "'No Bandwidth' and 'Assignment Failed' are the operational "
                  "alarms; call them out separately.",
    },
    {
        "name": "b2b submissions",
        "aliases": ["submission", "submissions", "submitted to client",
                    "placement", "placed with client", "job requirement"],
        "table": "Job_Submission__c",
        "definition": "Candidates submitted against client job requirements.",
        "sql": ("SELECT Submission_Status__c, count(*) FROM Job_Submission__c "
                "GROUP BY 1 ORDER BY 2 DESC"),
        "date_column": "Client_Submission_Date_Time__c",
        "chart": "funnel across submission stages",
        "caveat": "This process is newly built and holds only a handful of "
                  "records. Say it is not populated yet rather than reporting "
                  "a rate off two rows.",
    },
    {
        "name": "candidate training profile",
        "aliases": ["training details", "details for", "training record",
                    "training history", "how is doing", "progress of",
                    "everything about", "profile"],
        "table": "Candidate_Training__c",
        "definition": ("Everything about one named candidate's training: every "
                       "enrolment, then module, deliverable, session and mock "
                       "progress underneath them."),
        "sql": ("SELECT p.Name AS program, c.Name AS slot, ct.Status__c AS status, "
                "ct.Start_Date__c AS starts, ct.End_Date__c AS ends, "
                "count(DISTINCT s.Id) AS modules, "
                "count(DISTINCT s.Id) FILTER (WHERE s.Status__c = 'Completed') "
                "AS modules_done, "
                "count(DISTINCT d.Id) AS deliverables, "
                "count(DISTINCT d.Id) FILTER (WHERE d.Status__c = 'Approved') "
                "AS approved "
                "FROM Candidate_Training__c ct "
                "JOIN Account a ON ct.Candidate__c = a.Id "
                "LEFT JOIN Program__c p ON ct.Program__c = p.Id "
                "LEFT JOIN Cohort__c c ON ct.Cohort__c = c.Id "
                "LEFT JOIN CandidateTrainingStep__c s "
                "ON s.Candidate_Training__c = ct.Id "
                "LEFT JOIN Deliverable__c d ON d.Candidate_Training__c = ct.Id "
                "WHERE a.Name ILIKE '%<surname>%' "
                "GROUP BY 1, 2, 3, 4, 5 "
                "ORDER BY TRY_CAST(ct.Start_Date__c AS DATE)"),
        "date_column": "Start_Date__c",
        "chart": "usually none — this is a record, not a measure",
        "caveat": ("ONE ROW PER ENROLMENT, aggregated. A candidate commonly has "
                   "SEVERAL (dropped, retrained, current) — this one has five — "
                   "so list them all and say which is current; reporting the "
                   "first is a wrong answer. Do NOT return the raw modules and "
                   "deliverables: that is hundreds of rows, of which only 30 "
                   "reach the answer, and the summary then describes one "
                   "enrolment as though it were the whole history. Drill into "
                   "day-by-day detail only if asked for a named enrolment."),
    },
    {
        "name": "training sessions delivered",
        "aliases": ["training session", "training sessions", "classes held",
                    "sessions delivered", "sessions held"],
        "table": "Session__c",
        "definition": "Training sessions, excluding interview and other purposes.",
        "sql": ("SELECT count(*) FROM Session__c WHERE Purpose__c = 'Training' "
                "AND Status__c = 'Completed'"),
        "date_column": "Scheduled_Date__c",
        "chart": "line over month; bar by trainer",
        "caveat": ("Purpose__c also holds 'Internal Interview' and 'Resume "
                   "Understanding Session' — without the filter the count is "
                   "over a tenth too high."),
    },
    {
        "name": "session attendance",
        "aliases": ["attendance", "attended", "who attended", "class size",
                    "session attendees", "session", "sessions", "class"],
        "table": "Session__c + Session_Attendee__c",
        "definition": ("Candidate attendance. Group sessions put the master on "
                       "Session__c and everyone else on Session_Attendee__c."),
        "sql": ("SELECT count(*) FROM (SELECT Candidate__c, Scheduled_Date__c "
                "FROM Session__c WHERE Purpose__c = 'Training' "
                "UNION ALL SELECT Candidate__c, Scheduled_Date__c "
                "FROM Session_Attendee__c)"),
        "date_column": "Scheduled_Date__c",
        "chart": "line over month; bar by slot",
        "caveat": ("Counting Session__c alone counts only the master candidate "
                   "of each group session and undercounts the rest."),
    },
    {
        "name": "slot roster",
        "aliases": ["slot", "slots", "cohort", "cohorts", "batch",
                    "candidates in a slot", "slot size"],
        "table": "Cohort__c",
        "definition": "Candidates enrolled per Slot (Cohort__c).",
        "sql": ("SELECT c.Name, count(*) FROM Candidate_Training__c ct "
                "JOIN Cohort__c c ON ct.Cohort__c = c.Id GROUP BY 1 "
                "ORDER BY TRY_CAST(regexp_extract(c.Name, '(\\d+)', 1) AS INTEGER) DESC"),
        "date_column": "Start_Date__c",
        "chart": "bar, most recent slots first",
        "caveat": ("Slot names sort as text — 'Slot 11' before 'Slot 117'. "
                   "Order by the extracted number for 'last N slots'."),
    },
    {
        "name": "module progress",
        "aliases": ["module", "modules", "step", "steps", "training step",
                    "curriculum progress", "absent", "no show in training"],
        "table": "CandidateTrainingStep__c",
        "definition": "Training modules by status.",
        "sql": ("SELECT Status__c, count(*) FROM CandidateTrainingStep__c "
                "GROUP BY 1 ORDER BY 2 DESC"),
        "chart": "bar",
        "caveat": ("Absent = candidate no-show, Skipped = trainer absent, "
                   "Dropped = cascaded. Never merge them into one 'missed'."),
    },
    {
        "name": "deliverable status mix",
        "aliases": ["deliverable", "deliverables", "assignment", "assignments",
                    "submission", "submissions"],
        "table": "Deliverable__c",
        "definition": "Deliverables by status.",
        "sql": ("SELECT Status__c, count(*) FROM Deliverable__c GROUP BY 1 "
                "ORDER BY 2 DESC"),
        "date_column": "Due_Date_Sort__c",
        "chart": "bar",
        "caveat": ("The data contains a 'Completed' status the documented "
                   "picklist does not list — report it, do not drop it."),
    },
    {
        "name": "deliverable pass rate",
        "aliases": ["pass rate", "deliverable pass", "ai score", "graded",
                    "submission pass rate"],
        "table": "Deliverable_Result__c",
        "definition": "Share of graded submission attempts that passed.",
        "sql": ("SELECT count(*) FILTER (WHERE Result_Status__c = 'Pass') * 1.0 "
                "/ nullif(count(*) FILTER (WHERE Result_Status__c IN "
                "('Pass','Fail')), 0) FROM Deliverable_Result__c"),
        "date_column": "Submitted_At__c",
        "chart": "line over month",
        "caveat": ("Denominator excludes Pending. One deliverable can have "
                   "several attempts — this is per attempt, not per candidate."),
    },
    {
        "name": "mock outcomes",
        "aliases": ["mock outcome", "mock outcomes", "mock result",
                    "mock results", "mock pass", "mock fail"],
        "table": "Internal_Interview__c",
        "definition": "Trainer verdicts on completed mocks.",
        "sql": ("SELECT Human_Decision__c, count(*) FROM Internal_Interview__c "
                "WHERE Human_Decision__c IS NOT NULL GROUP BY 1 ORDER BY 2 DESC"),
        "date_column": "Scheduled_Date__c",
        "chart": "bar; donut only if the unset half is excluded and stated",
        "caveat": ("Verdicts in use are Pass / Fail / Needs Improvement, and "
                   "the field is unset on about half the records. State the "
                   "denominator."),
    },
    {
        "name": "trainer workload",
        "aliases": ["trainer", "trainers", "trainer load", "trainer workload",
                    "who is training", "host", "hosted", "sessions hosted"],
        "table": "Session__c",
        "definition": ("Training sessions actually delivered per trainer, "
                       "counted by who hosted them."),
        "sql": ("SELECT r.First_Name__c, r.Last_Name__c, count(*) AS sessions "
                "FROM Session__c s JOIN Recruiter__c r ON s.Host_User__c = r.Id "
                "WHERE s.Purpose__c = 'Training' GROUP BY 1, 2 ORDER BY 3 DESC"),
        "date_column": "Scheduled_Date__c",
        "chart": "horizontal_bar",
        "caveat": ("The host IS the trainer: Session__c.Host_User__c is named "
                   "for User but points at Recruiter__c. This counts sessions "
                   "delivered, which is the real workload — Candidate_Training__c"
                   ".Assigned_Trainer__c counts CANDIDATES assigned instead, a "
                   "much smaller and different number. Say which you used."),
    },
    {
        "name": "trainings assigned per trainer",
        "aliases": ["trainings assigned", "candidates per trainer",
                    "assigned trainer"],
        "table": "Candidate_Training__c",
        "definition": "Candidates assigned to each trainer.",
        "sql": ("SELECT r.First_Name__c, r.Last_Name__c, count(*) AS trainings "
                "FROM Candidate_Training__c ct JOIN Recruiter__c r "
                "ON ct.Assigned_Trainer__c = r.Id GROUP BY 1, 2 ORDER BY 3 DESC"),
        "date_column": "Start_Date__c",
        "chart": "horizontal_bar",
        "caveat": ("Headcount assigned, not sessions delivered — see "
                   "'trainer workload' for the latter."),
    },
    {
        "name": "training retention",
        "aliases": ["retention", "retained", "retention rate",
                    "rejection rate", "dropped trainings"],
        "table": "Candidate_Training__c",
        "definition": "Trainings retained versus dropped.",
        "sql": ("SELECT count(*) FILTER (WHERE Status__c <> 'Dropped') * 1.0 "
                "/ nullif(count(*), 0) FROM Candidate_Training__c"),
        "date_column": "Start_Date__c",
        "chart": "donut retained vs dropped; line by cohort start month",
        "caveat": ("Active trainings can still drop later, so recent cohorts "
                   "flatter the number."),
    },
    {
        "name": "retraining ratio",
        "aliases": ["retraining", "repeat training", "second training",
                    "trained again"],
        "table": "Candidate_Training__c",
        "definition": "Candidates on a repeat training versus their first.",
        "sql": ("SELECT CASE WHEN cnt > 1 THEN 'Repeat' ELSE 'First' END, "
                "count(*) FROM (SELECT Candidate__c, count(*) cnt "
                "FROM Candidate_Training__c GROUP BY 1) GROUP BY 1"),
        "chart": "donut",
        "caveat": "Counts candidates, not trainings.",
    },
    {
        "name": "marketing progress",
        "aliases": ["marketing", "job boards", "resume session",
                    "introduction script"],
        "table": "Marketing__c",
        "definition": "Where each candidate has reached in the marketing prep.",
        "sql": ("SELECT Status__c, count(*) FROM Marketing__c GROUP BY 1 "
                "ORDER BY 2 DESC"),
        "chart": "funnel (New Candidate -> Initial Call -> Applications -> "
                 "Offer Letter)",
        "caveat": "Closed, Paused and Stopped are exits, not funnel stages.",
    },
]

# ---------------------------------------------------------------------------
# Layer 2b — domain rules
# ---------------------------------------------------------------------------
# ORG_RULES is injected on every question and has to stay short. Rules that
# only matter inside one subject area live here and are injected when the
# question is actually about that area.

TRAINING_RULES = """\
Training-module rules (this question is about training):
- The module object is CandidateTrainingStep__c — NO underscore between
  "Candidate" and "Training". It is the one custom object here that breaks the
  convention, and Candidate_TrainingStep__c does not exist. Getting it wrong
  fails the whole query, which then gets reported as missing data.
- "Slot" IS Cohort__c. Nobody says "cohort" here; they say "Slot 128", and
  every Cohort__c.Name is literally "Slot NNN". A question naming a slot means
  Cohort__c.Name. Slot names sort as TEXT, so "Slot 11" lands before
  "Slot 117" — for "last N slots" order by the NUMBER:
  TRY_CAST(regexp_extract(Name, '(\\d+)', 1) AS INTEGER).
- Session__c is not only training. Purpose__c splits it: 'Training' (the vast
  majority), 'Internal Interview', 'Resume Understanding Session'. A question
  about training sessions MUST filter Purpose__c = 'Training' or it silently
  overstates by more than a tenth.
- Group sessions split attendance across two objects. Session__c holds the
  MASTER candidate; every other candidate on that meeting is a
  Session_Attendee__c row (hundreds of them). Counting candidate attendance
  from Session__c alone undercounts. Union the two, or say you counted masters
  only.
- A mock is identified by its Interview_Type__c lookup, NOT by
  Candidate_Training__c. Internal documentation says otherwise, but in this
  org's data every OOT and Intake record has an empty Candidate_Training__c —
  filtering on it would drop the entire OOT population.
- Interview_Type__c.Name follows a convention: 'OOT' (Resume Based Training),
  'Intake', '<Program Version> - Week N', '<Program Version> - Final Mock',
  '<Program Version> - Mock N'. "Week 3 mock", "final mock" and "OOT" all mean
  a match on this name.
- A named PROGRAMME must be filtered on, not treated as decoration. The
  programmes are Interview Readiness Training, Advanced AI/ML Training,
  Resume Based Training, Retraining Program, Technical 1-1 Training,
  Non-Technical 1-1 Training, Interview Skills Training, Interview Rejection
  Training, Specialized Training, Advanced Python Training and Coding. Reach a
  session's programme through its training:
    Session__c.Candidate_Training__c -> Candidate_Training__c.Program__c
    -> Program__c.Name ILIKE '%<programme>%'
  Asked for "sessions for the interview readiness training yesterday" without
  this, the answer returned all 43 training sessions that day instead of the
  24 belonging to that programme.
- "Interview Readiness Training" and "Interview Skills Training" are PROGRAMME
  names. The word "interview" inside them does not mean Interview__c.
- The trainer of a session is Session__c.Host_User__c. The name says User but
  the column points at Recruiter__c (2,781 rows match Recruiter__c, none match
  User). "Trainer workload" means sessions HOSTED; the assigned trainer on
  Candidate_Training__c is headcount assigned, a different and much smaller
  number. Say which one you counted.
- CandidateTrainingStep__c statuses are not interchangeable: Absent = the
  CANDIDATE did not show, Skipped = the TRAINER did not, Dropped = cascaded
  from a training drop, Blocked = removed from training. Never merge Absent
  and Skipped into one "missed" figure.
- Dropping a training does NOT retro-drop its history. The cascade only
  touches steps and deliverables dated on or after Drop_Date__c; earlier ones
  keep their status. A dropped training legitimately still has Completed steps
  and Approved deliverables.
- Do not filter Program_Version__c to Status = 'Published'. Only one version
  in this org is Published while the programme actually being delivered sits
  in Draft — that filter returns almost nothing.
- Mock verdicts in practice are Pass / Fail / Needs Improvement (not the
  Hire / NoHire the picklist also allows), and Human_Decision__c is unset on
  about half the records. State the denominator.
- A TRAINING IS NOT A CANDIDATE. The 618 Candidate_Training__c rows belong to
  only 333 distinct people: 151 hold two or more (retraining and re-enrolment
  are normal here, and one candidate holds 12). "How many candidates are in
  training" is COUNT(DISTINCT Candidate__c); counting rows nearly doubles it.
  Say which of the two you counted.
- Steps and deliverables do not cover the whole org, so a completion figure
  needs its denominator stated. Only 313 of the 618 trainings have ANY
  CandidateTrainingStep__c row and 360 have no Deliverable__c at all:
  Interview Readiness Training (299 trainings, half the org) has neither by
  design, and Retraining Program (57) has steps but no deliverables. Those
  are not candidates at 0% — they are programmes that do not use the
  mechanism."""

#: Each entry: the words that mean the question is in this domain, and the
#: rules to inject when they appear.
DOMAIN_RULES: List[Dict[str, Any]] = [
    {
        "name": "training",
        "triggers": [
            "training", "trainings", "trainer", "slot", "slots", "cohort",
            "session", "sessions", "module", "modules", "step", "steps",
            "deliverable", "deliverables", "mock", "mocks", "oot", "intake",
            "programme", "program", "curriculum", "attendance", "attended",
            "absent", "drop", "dropped", "retention", "retraining", "niche",
            "window", "windows",
        ],
        "rules": TRAINING_RULES,
    },
]


def domain_rules_for(question: str) -> str:
    """The subject-area rules this question needs, or "" for none."""
    text = " " + (question or "").lower() + " "
    blocks = []
    for domain in DOMAIN_RULES:
        if any(
            re.search(r"\b" + re.escape(word) + r"\b", text)
            for word in domain["triggers"]
        ):
            blocks.append(domain["rules"])
    # Brain packs are the same shape as DOMAIN_RULES, loaded from files the
    # Salesforce team drops in rather than authored here (core/brain.py).
    from . import brain

    pack_rules = brain.rules_for(question)
    if pack_rules:
        blocks.append(pack_rules)
    return "\n\n".join(blocks)


_WORD_RE = re.compile(r"[a-z][a-z0-9_]+")


def _phrases(metric: Dict[str, Any]) -> Sequence[str]:
    return [metric["name"], *metric.get("aliases", [])]


def match_metrics(question: str, limit: int = 2) -> List[Dict[str, Any]]:
    """The canonical measures this question is asking for.

    Phrase-first: a metric matches when one of its names appears in the
    question as a phrase, so "ghosting rate" pulls the ghosting definition
    while "rate" on its own pulls nothing.
    """
    from . import brain

    text = " " + (question or "").lower() + " "
    scored = []
    for metric in [*METRICS, *brain.extra_metrics()]:
        best = 0
        for phrase in _phrases(metric):
            if re.search(r"\b" + re.escape(phrase.lower()) + r"\b", text):
                best = max(best, len(phrase))
        if best:
            scored.append((best, metric))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
    # A long question matches loosely on several unrelated words: "how many
    # candidates completed the training ... failed the mock" pulled
    # `active candidates`, `training enrolment` AND `assessment hire decision`,
    # three full definitions of three different measures. Keep only those
    # matching about as specifically as the best one, so a weak side-match
    # cannot crowd the prompt.
    if scored:
        best = scored[0][0]
        scored = [pair for pair in scored if pair[0] * 2 >= best]
    return [m for _s, m in scored[:limit]]


def metric_hint(question: str) -> str:
    """The canonical definition block for a question, or "" if none matches.

    A measure the business names should compute the same way every time it is
    asked. Left to re-derive it per question, the model will quietly pick a
    different denominator and two answers will disagree.
    """
    picked = match_metrics(question)
    if not picked:
        return ""
    blocks = []
    for m in picked:
        lines = [
            f'"{m["name"]}" — {m["definition"]}',
            f"  canonical SQL: {m['sql']}",
        ]
        if m.get("date_column"):
            lines.append(f"  date filters use: {m['table']}.{m['date_column']}")
        if m.get("caveat"):
            lines.append(f"  MUST state: {m['caveat']}")
        blocks.append("\n".join(lines))
    return (
        "Canonical definitions for measures this question names. They fix the "
        "POPULATION and the DENOMINATOR — keep those exactly.\n"
        "They do NOT fix the scope. Every filter the question asks for — a "
        "named person, a date range, a slot, a programme — must be added ON TOP "
        "of the definition. Copying a definition verbatim and dropping the "
        "question's own filter returns the whole org's figure under that "
        "person's name, which is worse than an error because it looks right:\n"
        + "\n".join(blocks)
    )


#: How the canonical definitions translate to the live API. SOQL has no JOIN
#: and Salesforce returns real types, so the warehouse's casting rules are not
#: just unnecessary here, they are invalid syntax.
SOQL_TRANSLATION = """\
The definitions above are written as warehouse SQL. Over the live API:
- There is no JOIN. Traverse the relationship instead: a lookup Foo__c is
  reached as Foo__r. So `JOIN Interview_Type__c it ON ii.Interview_Type__c =
  it.Id WHERE it.Name = 'OOT'` becomes `WHERE Interview_Type__r.Name = 'OOT'`,
  and the record-type filters become RecordType.Name = 'Interview' or
  RecordType.Name = 'Person Account'.
- Do NOT cast. TRY_CAST is warehouse-only; Salesforce returns real dates,
  numbers and booleans. Checkboxes are true/false unquoted.
- Do NOT use `(now() AT TIME ZONE ...)` — that is warehouse syntax.
- Do NOT use the literal TODAY. It resolves in the INTEGRATION USER's
  timezone, not the business's: asked for today's work it returned the
  previous day's records, disagreeing with the same question answered from
  the warehouse. Write the business date in explicitly, as given below.
Keep the POPULATION and the FILTERS from the definition; only the syntax
changes.
""" + LIST_NOT_COUNT


def business_today() -> str:
    """Today's date in the business timezone, as an ISO string.

    Passed to the live path explicitly because neither clock available to it
    is the right one: the server runs UTC and the Salesforce integration user
    runs its own timezone. Both are wrong for several hours a day, and the two
    engines then answer the same question with different days.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return datetime.now(ZoneInfo(BUSINESS_TIMEZONE)).date().isoformat()


# ---------------------------------------------------------------------------
# Layer 5 — canonical reports
# ---------------------------------------------------------------------------
# The org already knows what a training review looks like: it is the nine
# charts on the Admin Training Dashboard. A generated report that invents its
# own sections is a second, disagreeing version of the same review.

#: Matched in order, so the narrower candidate template wins over the org-wide
#: training review when a question names a person.
REPORT_TEMPLATES: List[Dict[str, Any]] = [
    {
        "name": "candidate training report",
        "triggers": ["his training", "her training", "their training",
                     "this candidate", "candidate report", "candidate dashboard",
                     "for the candidate"],
        # Also fires whenever the question names a person at all — that is the
        # real signal, and a name cannot be enumerated in a trigger list.
        "needs_person": True,
        # Every instruction repeats the candidate filter. Without it the step
        # copied the canonical metric verbatim and charted the whole org's
        # 1,402 modules under one candidate's name.
        "sections": [
            ("Enrolments", "Candidate_Training__c JOINed to Account and "
             "FILTERED to this candidate with Account.Name ILIKE '%<surname>%': "
             "the Cohort__c slot Name as the category, the count of Completed "
             "CandidateTrainingStep__c as the value. Resolve every lookup to a "
             "Name. There are usually several enrolments; say which is current",
             "bar"),
            ("Module progress", "CandidateTrainingStep__c joined through "
             "Candidate_Training__c to Account and FILTERED to this candidate "
             "with Account.Name ILIKE '%<surname>%': Status__c as the category, "
             "count as the value. Absent (candidate no-show) and Skipped "
             "(trainer absent) stay separate", "bar"),
            ("Deliverables", "Deliverable__c joined through "
             "Candidate_Training__c to Account and FILTERED to this candidate "
             "with Account.Name ILIKE '%<surname>%': Status__c as the category, "
             "count as the value", "donut"),
            ("Sessions", "Session__c joined to Account and FILTERED to this "
             "candidate with Account.Name ILIKE '%<surname>%': Purpose__c as "
             "the category, count as the value", "bar"),
            ("Mocks", "Internal_Interview__c joined to Account and FILTERED to "
             "this candidate with Account.Name ILIKE '%<surname>%': "
             "Human_Decision__c as the category, count as the value, and say "
             "how many have no verdict yet", "bar"),
        ],
    },
    {
        "name": "training review",
        "triggers": ["training", "cohort", "slot", "trainer", "deliverable",
                     "mock", "programme", "program"],
        "sections": [
            ("Training status", "Candidate trainings by status", "bar"),
            ("Retention vs drop", "Retained versus dropped trainings, with the "
             "caveat that active trainings can still drop", "donut"),
            ("Retraining ratio", "Candidates on a first versus a repeat "
             "training", "donut"),
            ("Programme mix", "Trainings per Program", "bar"),
            ("Niche mix", "Candidate trainings per Niche__c", "horizontal_bar"),
            ("Deliverable status", "All deliverables by status", "bar"),
            ("Slot trend", "Candidates per Slot for the last 10 slots, ordered "
             "by the NUMBER in the slot name", "bar"),
            ("Mock outcomes", "Trainer verdicts on mocks, stating how many are "
             "undecided", "bar"),
            ("Trainer workload", "Trainings assigned per trainer", "horizontal_bar"),
        ],
    },
]


#: Words that are capitalised in a question without naming a person: object
#: names, the org, and the obvious sentence-start noise.
_NOT_A_NAME = {
    "i", "a", "the", "give", "show", "report", "dashboard", "training",
    "slot", "cohort", "mock", "mocks", "oot", "intake", "account", "case",
    "interview", "session", "candidate", "trainer", "program", "salesforce",
    "techsara", "please", "can", "what", "which", "who", "how", "for", "me",
    "my", "all", "and", "with", "chart", "charts", "week", "day", "ai", "ml",
}

#: A capitalised token mid-sentence is the cheapest reliable signal that a
#: person has been named. Deliberately loose: a false positive only adds a
#: section list the planner may ignore, a false negative loses the template.
_PROPER_NOUN_RE = re.compile(r"(?<!^)(?<![.!?]\s)\b([A-Z][a-z]{2,})\b")


def names_a_person(question: str) -> bool:
    """Does this question appear to name someone?"""
    for match in _PROPER_NOUN_RE.finditer(question or ""):
        if match.group(1).lower() not in _NOT_A_NAME:
            return True
    return False


def report_template_for(question: str) -> str:
    """The canonical section list for this kind of report, or "".

    Injected into the report planner so a "training report" comes out matching
    the dashboard the team already reads, rather than nine sections the model
    invented on the spot.
    """
    text = " " + (question or "").lower() + " "
    for template in REPORT_TEMPLATES:
        matched = any(
            re.search(r"\b" + re.escape(word) + r"\b", text)
            for word in template["triggers"]
        )
        # A person-scoped template also fires on any named person, provided the
        # question is about this subject area at all.
        if not matched and template.get("needs_person"):
            matched = names_a_person(question) and bool(domain_rules_for(question))
        if matched:
            lines = [
                f'  {i}. title "{title}" — instruction: {instruction}. '
                f'Render as a {chart}, so set "chart": true.'
                for i, (title, instruction, chart) in enumerate(
                    template["sections"], start=1
                )
            ]
            return (
                f'The org already has a canonical "{template["name"]}". Unless '
                "the user asked for something different, plan these sections, "
                "in this order:\n"
                + "\n".join(lines)
                + '\nEvery one of these sections is "kind": "sql" and '
                '"chart": true. This report is read as a set of charts — a '
                "section returned as prose or a bare table is a failure. Each "
                "section's SQL must return a CATEGORY column and a NUMERIC "
                "count column so there is something to draw."
            )
    return ""


def _stem(word: str) -> str:
    """Crude singularisation; same rule as sf_dictionary and schema_cache."""
    if len(word) > 3 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


#: Tables named inside a metric's canonical SQL, so the schema slice sent to
#: the model can never drop a table its own definition depends on.
_TABLE_RE = re.compile(r"\b(?:FROM|JOIN)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)

#: What people SAY -> the table they mean. The schema slice is chosen by word
#: overlap with table and column names, which cannot know that "slot" means
#: Cohort__c. Without this, "how many completed the training from slot 128"
#: had Cohort__c ranked out of its own prompt and could not be answered at all.
TABLE_ALIASES: Dict[str, Sequence[str]] = {
    "slot": ("Cohort__c",),
    "cohort": ("Cohort__c",),
    "batch": ("Cohort__c",),
    "module": ("CandidateTrainingStep__c",),
    "step": ("CandidateTrainingStep__c",),
    "window": ("Session_Window__c",),
    "mock": ("Internal_Interview__c", "Interview_Type__c"),
    "oot": ("Internal_Interview__c", "Interview_Type__c"),
    "intake": ("Internal_Interview__c", "Interview_Type__c"),
    "assessment": ("Internal_Interview__c", "Interview_Type__c"),
    "training": ("Candidate_Training__c",),
    "trainer": ("Recruiter__c", "Session__c"),
    "host": ("Session__c", "Recruiter__c"),
    "hosted": ("Session__c", "Recruiter__c"),
    "employee": ("Recruiter__c",),
    "recruiter": ("Recruiter__c",),
    "supporter": ("Recruiter__c",),
    # The word the questions actually use. Every OTHER way of saying "a member
    # of staff" was mapped — trainer, host, employee, recruiter, supporter —
    # but not the one on the field itself, so Recruiter__c scored zero on
    # "how many internal interviews has <name> conducted" and never entered the
    # slice. `interviewer` also has to pull Internal_Interview__c: the question
    # is about the interviews, and the person is only how they are filtered.
    "interviewer": ("Recruiter__c", "Internal_Interview__c"),
    "interviewers": ("Recruiter__c", "Internal_Interview__c"),
    "conducted": ("Recruiter__c", "Internal_Interview__c"),
    "taken by": ("Recruiter__c", "Internal_Interview__c"),
    "candidate": ("Account", "RecordType"),
    "client": ("Account", "RecordType"),
    "interview": ("Interview__c", "RecordType"),
    "ghosted": ("Interview__c", "RecordType"),
    "deliverable": ("Deliverable__c", "Deliverable_Result__c"),
    "session": ("Session__c",),
    "attendance": ("Session__c", "Session_Attendee__c"),
    "programme": ("Program__c", "Program_Version__c"),
    "readiness": ("Program__c", "Candidate_Training__c", "Session__c"),
    "irt": ("Program__c", "Candidate_Training__c", "Session__c"),
    "program": ("Program__c", "Program_Version__c"),
    "invoice": ("Invoice__c",),
    "payment": ("Payment__c", "Invoice__c"),
    "collection": ("Payment__c",),
    "outstanding": ("Invoice__c",),
    # The words people actually use for payment questions. "how much money
    # they have paid" matched NO alias, so the prompt carried zero Payment__c
    # fields and the model invented `p.Status__c` (the real column is
    # Payment_Status__c) — a raw DuckDB binder error reached the user after
    # two answered clarifications.
    "paid": ("Payment__c", "Invoice__c"),
    "emi": ("Invoice__c", "Payment__c"),
    "emis": ("Invoice__c", "Payment__c"),
    "installment": ("Invoice__c", "Payment__c"),
    "pay": ("Payment__c",),
    "pays": ("Payment__c",),
    "money": ("Payment__c", "Invoice__c"),
    "fee": ("Payment__c", "Invoice__c"),
    "fees": ("Payment__c", "Invoice__c"),
    "amount": ("Payment__c", "Invoice__c"),
    # "candidates enrolled" is the training pipeline, not a bare Account scan.
    "enrolled": ("Candidate_Training__c", "Account"),
    "enrolment": ("Candidate_Training__c",),
    "enrollment": ("Candidate_Training__c",),
    "marketing": ("Marketing__c",),
    "submission": ("Job_Submission__c", "Job_Requirement__c"),
    "onboarding": ("Onboarding__c",),
}


def tables_for(question: str) -> List[str]:
    """Tables this question needs pinned into the schema prompt.

    Two sources: the tables a matched metric's own SQL joins, and the tables
    the org's spoken vocabulary maps to.
    """
    seen: List[str] = []

    def add(name: str) -> None:
        if name not in seen:
            seen.append(name)

    for metric in match_metrics(question):
        for name in _TABLE_RE.findall(metric["sql"]):
            add(name)
    words = {_stem(w) for w in re.findall(r"[a-z]+", (question or "").lower())}
    for word, tables in TABLE_ALIASES.items():
        if _stem(word) in words:
            for name in tables:
                add(name)
    from . import brain

    for name in brain.tables_for(question):
        add(name)
    return seen


def grounding_for(question: str, dialect: str = "sql") -> str:
    """Everything a query stage should know about this org for this question.

    `dialect` matters more than it looks. The live path fell back to Salesforce
    whenever the warehouse was locked, and it had only the field dictionary to
    work from — so "oot mocks taken today" became a query against
    Program_Version__c and answered 0. Grounding it needs the same brief, in
    the syntax it can actually run.
    """
    parts = [ORG_BRIEF, ORG_RULES]
    domain = domain_rules_for(question)
    if domain:
        parts.append(domain)
    hint = metric_hint(question)
    if hint:
        parts.append(hint)
    # Brain glossary + retrieved knowledge (rules and metrics already arrived
    # through domain_rules_for and metric_hint above).
    from . import brain

    extras = brain.grounding_extras(question)
    if extras:
        parts.append(extras)
    if dialect == "soql":
        parts.append(
            SOQL_TRANSLATION
            + f"\nToday, in the business timezone, is {business_today()}. "
            "Use that date (and dates relative to it) rather than TODAY."
        )
    return "\n\n".join(parts)
