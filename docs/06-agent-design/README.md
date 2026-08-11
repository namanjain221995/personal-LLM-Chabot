# Salesforce Analyst Agent — Design

How this platform should behave when a user asks it anything about the org, and
where each rule has to live to actually take effect.

This is the equivalent of the single system prompt behind the outbound-calling
agent, redesigned for what this system actually is. The calling agent was one
model, one turn, one action group, so one prompt could hold everything. Here a
single question passes through **six model calls**, and a rule only works if it
is attached to the stage that can act on it. Persona rules in the SQL prompt
change nothing; join rules in the answer prompt arrive after the number is
already wrong.

---

## 1. The pipeline, and who knows what

```
user question
   │
   ├─ router ──────────── engines/router.py       picks sql | rag | vision | report | chat
   │
   ├─ SQL writer ──────── engines/sql.py:_ask_sql
   │     grounded with:   core/schema_cache.relevant_schema()   the tables that matter
   │                      core/sf_dictionary.hint_for()          what things are CALLED
   │                      core/org_brief.grounding_for()         what things MEAN   ← new
   │
   ├─ guard ───────────── core/sql_guard.py       read-only enforcement, never bypassed
   ├─ DuckDB ──────────── read-only execution, one retry with the error text
   │
   ├─ answer writer ───── engines/sql.py:_narrative_messages
   │     grounded with:   org_brief.ANSWER_RULES                 honesty about population
   │
   ├─ chart decision ──── core/chart_decision.py → core/chart_spec.py (pydantic-validated)
   └─ report planner ──── engines/report.py:_PLAN_SYSTEM
```

**The layering rule.** Every rule below is tagged with the stage that owns it.
A rule in the wrong layer is decoration.

| Layer | Owns | Where |
|---|---|---|
| L0 Org brief | Business model, entity meaning, population traps | `core/org_brief.py:ORG_BRIEF`, `ORG_RULES` |
| L1 Vocabulary | API names, lookup targets, picklist values | `core/sf_dictionary.py` |
| L2 Semantic layer | Canonical metric definitions | `core/org_brief.py:METRICS` |
| L3 Mechanics | DuckDB/warehouse correctness | `core/org_brief.py:SQL_HARD_RULES` |
| L4 Voice | Tone, honesty, refusals | `core/org_brief.py:ANSWER_RULES` |
| L5 Presentation | Chart choice, report structure | `core/chart_decision.py`, `engines/report.py` |

---

## 2. Persona and voice — L4

> You are the analyst for TechSara's Salesforce org. You answer questions about
> candidates, interviews, training, staffing and billing with numbers you
> actually read, and you say where they came from.

**Voice rules**

1. **Lead with the answer.** The number first, the caveat second, the method
   only if asked. Not "I queried Interview__c joined to RecordType…" — that is
   the `sql` field in the response metadata, which the UI already shows.
2. **Vary phrasing.** Same dynamic-phrasing rule as the calling agent: never
   open two consecutive answers the same way. Rotate "Across…", "There were…",
   "That comes to…", "Looking at…".
3. **Short.** A paragraph, optionally three or four bullets. A user who wants
   depth asks for a report.
4. **Never name internals.** No "DuckDB", "warehouse", "vLLM", "the model",
   "SOQL", "the prompt", "RecordTypeId". Say "the synced Salesforce copy" or
   "live from Salesforce". Field names are fine when the user asked in field
   terms.
5. **Never announce work in progress.** No "let me check", "querying now".
   The stream either has the answer or an honest failure.

**Honesty rules** — these are the ones that matter, and they are enforced in
`org_brief.ANSWER_RULES`:

6. **State the population.** "1,247 interviews" is not an answer; "1,247
   interviews, excluding initial calls" is. See §5.1.
7. **Source, always.** The synced copy is up to 30 minutes stale — say so, and
   never claim a synced number is live.
8. **Sample is not total.** The composer sees the first 30 rows of a possibly
   500-row result. Quote the true total, never the visible count.
9. **Empty ≠ zero.** See §5.6.
10. **Never read out credentials or identifiers.** Passwords, portal
    credentials, SSN digits, passport, bank numbers. Confirm the field exists,
    decline the value. See §5.10.
11. **Say when you did not read it.** If the query failed, say the query
    failed. Never substitute a plausible number.

---

## 3. The object map — all 82 exported objects

Grouped by what a question about them would be. Field counts from the metadata
export; the warehouse holds 1,023 tables in total, the rest being shadow and
setup objects that only surface when a question names them.

**A. Candidate master & intake (14)** — `Account` (194), `Lead` (88),
`Onboarding__c` (153), `Contact` (35), `Pre_Enrolment_Request__c` (29),
`Background_Check__c` (42), `Background_Check_Employment__c` (21),
`Education_History__c` (12), `Resume__c` (10), `Availability__c` (10),
`Onboarding_Token__c` (8), `Candidate_Portal_Credential__c` (7),
`Candidate_Portal_Session__c` (7), `Candidate_Email__c` (6)

**B. Client pipeline (10)** — `Interview__c` (257), `AI_Assignment_Logs__c` (44),
`Job_Requirement__c` (38), `Job_Submission__c` (28), `Opportunity` (24),
`Marketing__c` (18), `Interview_Participant__c` (12), `Application__c` (10),
`Vendor__c` (5), `Company__c` (4)

**C. Internal assessment (17)** — `Internal_Interview__c` (61),
`Internal_Interview_Question_Log__c` (11), `Question_Bank__c` (11),
`Section__c` (11), `Internal_Interview_Section_Log__c` (7),
`Interview_Evaluation__c` (6), `Section_Evaluation__c` (4),
`Question_Follow_Up__c` (4), `Interview_Type__c` (4),
`Template_Section_Question__c` (4), `Template_Section__c` (4), `Template__c` (3),
`Section_Interview_Type__c` (3), `Niche__c` (2), `Niche_Question__c` (2),
`Question_Section__c` (2), `Question_Interview_Type__c` (2)

**D. Training LMS (12)** — `Program_Version__c` (28), `Candidate_Training__c` (22),
`Deliverable__c` (21), `CandidateTrainingStep__c` (16), `StepTemplate__c` (14),
`Step_Deliverable_Definition__c` (8), `Deliverable_Result__c` (7),
`Cohort__c` (4), `Program__c` (4), `Session_Window__c` (13), `Institute__c` (2),
`Course__c` (1)

**E. Scheduling (4)** — `Session__c` (71), `Employee_Leave__c` (41),
`Session_Attendee__c` (11), `Recurring_Break_Series__c` (8)

**F. Billing (3)** — `Invoice__c` (64), `Payment__c` (38), `QB_Plan__c` (1)

**G. Workforce & internal ops (8)** — `User` (50), `Recruiter__c` (47),
`Case` (47), `EmailMessage` (23), `Task` (22), `Event` (22), `QuickText` (6),
`Department__c` (0)

**H. Media & platform (8)** — `Zoom_Recording_Access_Log__c` (19),
`ContentVersion` (19), `ContentDocument` (11),
`Zoom_Recording_User_Folder_Map__c` (9), `Zoom_Recording_Department__c` (6),
`Knowledge__kav` (0), `FeedItem` (0), `Site` (0)

**I. Configuration — never analytics (6)** — `Zoom_Portal_Config__mdt` (9),
`Slack_Email_Notification_Metadata__mdt` (7),
`Interview_Incentive_Configuration__mdt` (5), `Follow_Up_Setting__mdt` (2),
`In_App_Checklist_Settings__c` (2), `Token_Secret__mdt` (1)

Group I holds live secrets (`AWS_Secret_Key__c`, `Token_Secret__mdt`). These
must never be queried, charted or reported. They are currently unreachable to
the sync user, which is a permission accident, not a control — see §5.11.

---

## 4. Use case catalog

Each use case is: what the user says → which measure answers it → the chart
that fits. Canonical SQL lives in `core/org_brief.py:METRICS` so the same
question gets the same definition every time.

### 4.1 Interview operations (domain B) — the heaviest traffic

| The user asks | Measure | Default chart |
|---|---|---|
| "How many interviews last month?" | `interviews conducted` | line over month |
| "How many initial calls?" | `initial calls` | line over month |
| "What's our ghosting rate?" | `ghosting rate` | line over month |
| "Break interviews down by outcome" | `interview outcome mix` | bar |
| "Who supported the most interviews?" | `interview support load` | horizontal_bar |
| "How is AI assignment doing?" | `assignment health` | bar |
| "Interviews by round / by type" | ad-hoc on `Round__c`, `Interview_Type__c` | bar |
| "Which candidates ghost most?" | ad-hoc, join to Account | horizontal_bar, top 10 |

### 4.2 Candidate lifecycle (domain A)

| The user asks | Measure | Default chart |
|---|---|---|
| "How many active candidates?" | `active candidates` | single value |
| "Candidates by status" | `candidate pipeline by status` | bar |
| "Candidates by visa status" | ad-hoc on `Current_Visa_Status__c` | bar |
| "Candidates by niche" | ad-hoc on `Niche__c` | horizontal_bar |
| "Intake over time" | ad-hoc on `Application_Start_Date__c` | line |
| "Onboarding stuck where?" | ad-hoc on `Onboarding__c.Status__c` | funnel |
| "Background checks outstanding" | ad-hoc on `Background_Check__c` | bar |

### 4.3 Billing (domain F)

| The user asks | Measure | Default chart |
|---|---|---|
| "How much have we invoiced?" | `invoiced amount` | line over month |
| "How much have we collected?" | `collections` | line, or bar vs invoiced |
| "What's outstanding?" | `outstanding balance` | bar by ageing or candidate |
| "Any payment disputes?" | `payment issues` | bar |
| "Top unpaid invoices" | `outstanding balance`, top-N | horizontal_bar |
| "Recurring plans at risk" | ad-hoc on `Recurring_Status__c` | bar |

### 4.4 Training (domain D/E)

Backed by the Training Module Handbook, cross-checked against the warehouse.
Training rules load only when the question is about training
(`org_brief.DOMAIN_RULES`), so they cost nothing on a billing question.

| The user asks | Measure | Default chart |
|---|---|---|
| "Who's in training?" | `training enrolment` | bar |
| "Drop rate by cohort" | `training drop rate` | line by cohort month |
| "Retention rate" | `training retention` | donut |
| "How many are retraining?" | `retraining ratio` | donut |
| "How many training sessions?" | `training sessions delivered` | line |
| "What was attendance?" | `session attendance` | line |
| "Candidates in slot 128" | `slot roster` | bar |
| "Module / step progress" | `module progress` | bar |
| "Deliverable status" | `deliverable status mix` | bar |
| "Deliverable pass rate" | `deliverable pass rate` | line |
| "Mock results" | `mock outcomes` | bar |
| "Trainer load" | `trainer workload` | horizontal_bar |

**Vocabulary that only exists in speech.** "Slot" is `Cohort__c` — all 28
records are named `Slot NNN` and nobody says "cohort". "Module" is
`CandidateTrainingStep__c`. "Window" is `Session_Window__c`. "PV" is
`Program_Version__c`.

### 4.5 Assessment (domain C)

| The user asks | Measure | Default chart |
|---|---|---|
| "Mock interview results" | `assessment hire decision` | bar |
| "Does AI agree with humans?" | `assessment hire decision` | scatter, AI vs human score |
| "Score distribution" | ad-hoc on `Combined_Score__c` | histogram |

### 4.6 B2B pipeline (domain B) — **nearly empty, answer accordingly**

`b2b submissions`, `Job_Requirement__c` by `Job_Status__c`. Funnel is the right
shape but there are only a handful of records; §5.6 governs the answer.

### 4.7 Non-data routes

- **Schema questions** ("what fields does Interview have?") → the live describe
  path in `engines/sql.py`, not SQL.
- **Content questions** ("what did the client say in feedback?") → `rag`.
- **Reports** → §7.
- **Chat** → greetings and identity. Answer in one line, offer an example
  question.

---

## 5. Edge cases

These are the failure modes this org actually has. Each was verified against
the live warehouse and org, and each is stated as the required behaviour.

### 5.1 Everything is text — the silent wrong answer
**19,519 of 19,520 warehouse columns are `VARCHAR`.** Amounts, dates, counts.

Proven: `SELECT Name, Invoice_Amount__c FROM Invoice__c ORDER BY
Invoice_Amount__c DESC LIMIT 5` returns five invoices of **999** — the true top
invoice is **27,000**. It sorts as text. No error.

**Required:** `TRY_CAST(col AS DOUBLE)` before any sort, comparison or
aggregate; `TRY_CAST(col AS DATE)` for dates. `TRY_CAST`, never `CAST`, so one
bad value cannot abort the query. Enforced in `SQL_HARD_RULES`, tested in
`tests/test_org_brief.py`.

### 5.2 Account is three populations, and the obvious filter is the wrong one
787 Person Accounts (candidates), **259 Recruiter records**, 1 B2B Client, 4
untyped.

The trap: **`IsPersonAccount` does not separate them.** Recruiters are person
accounts too — `IsPersonAccount = 'true'` returns 1,046, not 787 — and every
one of those 259 recruiter rows carries a `Candidate_Status__c` value. So
"active candidates" filtered on `IsPersonAccount` returns **551** when the true
figure is **294**: an 87% overstatement, from the filter that looks correct.

**Required:** the candidate population is *always*

```sql
JOIN RecordType rt ON a.RecordTypeId = rt.Id WHERE rt.Name = 'Person Account'
```

### 5.3 Interview__c is two processes
26,904 `Interview` + 5,566 `Initial Call` + 4 `B2B Interviews` + 673 with no
record type.

**Required:** filter to `Interview` unless asked otherwise, and say so. Mention
the untyped rows rather than silently including or dropping them.

### 5.4 Half of interviews have no outcome
`Interview_Outcome__c` is null on 16,610 of 33,147 rows.

**Required:** every rate states its denominator. Ghosting is 8,795 of the
16,537 dispositioned interviews (~53%), not of all 33,147 (~27%). Both numbers
are defensible; publishing one without saying which is not.

### 5.5 Data holds values the picklist no longer has
`Interview_Outcome__c` contains `Loop Round` (11 rows), absent from the active
picklist.

**Required:** report what the data says. Never drop an unexpected value
silently, and never claim a value is invalid because the metadata omits it.

### 5.6 Empty is not zero
`Job_Submission__c` has 2 rows. `Client_Type__c` is set on 1 Account.

**Required:** "that process isn't populated yet", not "the placement rate is
50%". A rate over two rows is noise presented as fact.

### 5.7 Synced vs live
The warehouse lags up to 30 minutes. A "Live Salesforce" toggle bypasses it.

**Required:** name the source in every answer. Never call a synced figure live.
When live fails, say so — never fall back silently to synced numbers while
still claiming live.

### 5.8 Sample vs total
The narrative stage sees 30 rows of up to 500.

**Required:** quote the true total; never derive a secondary statistic ("most
are active") from the visible sample without flagging it.

### 5.9 Day-first dates
Users write `03-07-2026` meaning 3 July. The local and live engines once
disagreed on exactly this.

**Required:** day-month-year unless the string is ISO.

### 5.10 Credentials and PII in the schema
`Account.LinkedIn_Password__c`, `Marketing_Email_Password__c` (EncryptedText),
`Candidate_Portal_Credential__c.Password__c` (**plain text**),
`Last_4_digits_of_SSN__c`, `Passport_Number__c`, `Bank_Account_Last4__c`.

**Required:** never select these into an answer, chart, export or report.
Acknowledge the field exists and decline the value. This holds even if the user
asks directly and even if the value is already in a result row.

### 5.11 Secrets in configuration objects
`Zoom_Portal_Config__mdt.AWS_Access_Key__c` / `AWS_Secret_Key__c` as plain
Text, plus `Token_Secret__mdt`. Currently 404 for the sync user — a permission
accident, not a control.

**Required:** treat group I as non-analytic. Never query, chart or export it.

### 5.12 Two objects called "interview"
`Interview__c` (client-facing, 33k rows) vs `Internal_Interview__c` (internal
assessment). Also `Session__c` for training sessions.

**Required:** default to `Interview__c`, state the choice in one clause, and
offer the other if the question is genuinely ambiguous.

### 5.13 Two things called "recruiter"
`Recruiter__c` is the internal staff object — and its label is "Employees".
`Account` also has a `Recruiter` record type. Trainers, interviewers and
support people are `Recruiter__c`.

### 5.14 Rollups may disagree with recomputation
`Account.Interviews_Ghosted__c` is a rollup filtered to `RecordTypeId =
'Interview'`. A hand-written `COUNT(*)` without that filter will not match.

**Required:** prefer the rollup when it exists; if recomputing, reproduce its
filter. If the two disagree, say so rather than picking one.

### 5.15 Timezone
`Interview__c` carries `IST_Start_DateTime__c` alongside UTC `CreatedDate`.

**Required:** for scheduling questions use the IST fields; for audit questions
use `CreatedDate`, and name which.

### 5.17 Training: `Session__c` is three processes
`Purpose__c` splits it — Training 2,437, Internal Interview 348, Resume
Understanding Session 4, one null. "How many training sessions" over the whole
object overstates by **13%**.

**Required:** `Purpose__c = 'Training'` on every training-session question.

### 5.18 Training: attendance lives in two objects
Group sessions put the master candidate on `Session__c` and every other
attendee on `Session_Attendee__c` (425 rows). Counting `Session__c` alone
counts one candidate per group meeting.

**Required:** union both, or state that you counted masters only.

### 5.19 Training: the handbook's mock rule is wrong in production
§3.7 of the Training Module Handbook says a mock is identified by
`Candidate_Training__c` being populated. In the data, **all 105 OOT and all
111 Intake records have it empty** — only 55 of 297 have it at all. Applying
the documented rule drops the entire OOT population, which is exactly what
users ask for by name.

**Required:** identify mocks by the `Interview_Type__c` lookup name (`OOT`,
`Intake`, `<PV> - Week N`, `<PV> - Final Mock`, `<PV> - Mock N`), never by the
training link. When a document and the data disagree, the data wins and the
disagreement gets said out loud.

### 5.20 Training: "Slot 11" sorts before "Slot 117"
Every `Cohort__c.Name` is `Slot NNN`, and the column is text like everything
else. "Last 10 slots" silently returns the wrong ten.

**Required:**
`ORDER BY TRY_CAST(regexp_extract(Name, '(\d+)', 1) AS INTEGER)`.

### 5.21 Training: Absent ≠ Skipped
`CandidateTrainingStep__c.Status__c` distinguishes **Absent** (candidate
no-show, 14), **Skipped** (trainer absent, 2), **Dropped** (cascaded, 266) and
**Blocked** (removed). Merging them into one "missed" figure hides whether the
trainer or the candidate failed to appear.

### 5.22 Training: a dropped training keeps its earlier history
The cascade only touches steps and deliverables dated on or after
`Drop_Date__c`. A dropped training legitimately still has Completed steps and
Approved deliverables — that is audit preservation, not dirty data.

### 5.23 Training: `Published` is not a usable filter
The handbook says only Published Program Versions accept new trainings. In
practice exactly **one** of twelve is Published, while the programme actually
being delivered (`Advanced AI/ML Training - V1`, 72 assessments) sits in
Draft. Filtering to Published returns almost nothing.

### 5.16 The question names a field that does not exist
The metadata export came from preprod, which runs ~43 fields ahead of
production.

**Required:** if a column is missing, say the field is not in this org's data
rather than substituting a similar one. The wrong-but-plausible column is the
failure mode this whole design exists to prevent.

---

## 6. Chart design — L5

Nine supported types (`core/chart_spec.py`): `bar`, `horizontal_bar`, `line`,
`area`, `scatter`, `pie`, `donut`, `funnel`, `histogram`. The spec is
pydantic-validated and **model output is never executed** — an invalid spec
degrades to a table.

**Choosing by question shape:**

| Shape | Chart | Why |
|---|---|---|
| Measure over time | `line` (`area` only for cumulative) | trend is the message |
| Category comparison, ≤8 categories | `bar` | |
| Category comparison, >8 or long labels | `horizontal_bar` | labels stay readable |
| Ranking / top-N | `horizontal_bar`, sorted | |
| Ordered stage progression | `funnel` | only when stages are genuinely sequential |
| Share of a whole, ≤5 parts | `donut` | |
| Two numeric measures | `scatter` | e.g. AI vs human score |
| Distribution of one measure | `histogram` | e.g. score spread |

**Rules**

1. Chart only when asked (`explicit` mode) or when the shape clearly warrants
   it (`hybrid`). Never chart a single number.
2. **Never pie/donut a metric with a large null bucket** — the mix in §5.4
   would render "unset" as the biggest slice. Either exclude and say so, or use
   a bar.
3. Funnel requires real ordering. `Candidate_Status__c` is not a funnel
   (`Hold`, `Paused` are not stages); `Marketing__c.Status__c` and
   `Job_Submission__c.Submission_Status__c` are.
4. Sort ranking charts by the cast numeric value (§5.1), not the text.
5. Time axis grouped with `date_trunc('month', TRY_CAST(...))`, never a
   formatted string — "Apr" sorts before "Jan" as text.
6. The caption carries the population and the source. A chart that outlives the
   conversation must still say what it counted.

---

## 7. Report design — L5

`engines/report.py` plans up to N sections, each `sql` or `rag`, each optionally
charted. Templates worth having:

**Training review** — the canonical one, and the only template currently
enforced in code (`org_brief.REPORT_TEMPLATES`, injected into the planner).
It reproduces the nine charts the Admin Training Dashboard already shows, in
the same order, so a generated report and the dashboard cannot disagree:
training status · retention vs drop · retraining ratio · programme mix · niche
mix · deliverable status · slot trend (last 10, numerically sorted) · mock
outcomes · trainer workload.

**Weekly operations review** — interview volume trend; outcome mix; assignment
health; support load; ghosting rate with denominator; exceptions (no-bandwidth,
failed assignment).

**Candidate cohort review** — intake by month; status distribution; training
enrolment and drop rate; assessment outcomes; time-to-first-interview.

**Finance review** — invoiced vs collected by month; outstanding by ageing;
payment issues by type and status; recurring plans at risk.

**Client/B2B pipeline** — requirements by status; submission funnel; placement
outcomes. Gated on §5.6: if unpopulated, the report says so in one line rather
than rendering empty charts.

**Report rules**

1. Every section states its population and source.
2. No section is published on a sample — reports run to the full row cap.
3. A chart per section at most; an unchartable section stays a table.
4. Sections whose query returned nothing are kept with an explicit "no records
   matched", not dropped — a silently missing section reads as an omission.

---

## 8. Status

**Three engines answer data questions, not one.** This is the thing that made
fixes look like they had not worked: `main.py:718` checks the agent toggle
**before** the router, so with the agent on, `engines/report.py` is never
reached. All grounding therefore lives in `core/org_brief.py` and is injected
into all three:

| Path | Reached when | Gets grounding via |
|---|---|---|
| `engines/sql.py` | router says `sql` | `grounding_for()` + `SQL_HARD_RULES` + `ANSWER_RULES` |
| `engines/report.py` | router says `report` | `report_template_for()` in the planner |
| `engines/agent.py` | **agent toggle on — before the router** | `report_template_for()` in `make_plan`, and its `sql` steps reuse `engines/sql.py` |
| `engines/live_sf.py` | warehouse locked, or Live toggle | `grounding_for(dialect="soql")` |

**Implemented**

- `core/org_brief.py` — L0 brief, L2 semantic layer (**29 metrics**), L3
  mechanics, L4 answer rules, domain rules, **2 report templates**.
- `sf_dictionary` — org-schema.json loader, enrich-only merge, plural stemming,
  shadow/setup-object filtering.
- `schema_cache` — lock wait, so a locked warehouse no longer diverts traffic
  to live Salesforce.
- `chart_decision` — "dashboard" and "graphical view" count as chart requests.
- **988 tests passing.**

**Not yet done**

1. The router has no `metric` class; definitional questions ("what counts as
   ghosted?") still fall to `rag`.
2. §5.10/§5.11 are prompt-level only. A deny-list in `sql_guard.py` would make
   credential columns unselectable rather than merely discouraged — that is the
   difference between a policy and a control.
3. The chart rules in §6 are documented but not enforced in
   `chart_decision.py` — notably the pie/donut null-bucket rule.
4. Domains **G and H** (Case/Task/Event, Zoom recordings, content) have no
   canonical metrics. Questions there run on raw schema grounding, which is
   where the model is most likely to invent a definition.
5. The warehouse is write-locked ~51% of the time. Retrying makes it
   survivable, not solved; a read snapshot is the structural fix.
6. **Deployment**: the current changes were copied into the running container,
   not baked into the image, to avoid shipping an unrelated in-progress
   `_MIGRATION_V4`. A `docker compose build` reverts them until rebuilt from
   current source.
