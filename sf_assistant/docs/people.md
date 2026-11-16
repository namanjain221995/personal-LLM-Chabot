# People questions: the Group B fixes

> This repository holds the package, its prompts, the page, the tests and these docs. `kb_build/`, `docs/spec/`,
> `tests/parity/`, `knowledge/` and `.private/` stay on the owner's machine: where this page names them, they are not here.

**Status (2026-10-22): Group B was evaluated with the model on the evening of 2026-10-22; the six fixes of that
evaluation are built, reviewed and checked model-free, not yet evaluated with the model ([Evaluation fixes](#evaluation-fixes-2026-10-22)).**

**Status (2026-10-21): built, reviewed and checked model-free; not yet evaluated with the model.** These are the general fixes
for the people-question causes of evaluation round 2 (the dev grades; the holdout stays blind until the evening
run). The owner's rule holds throughout: general fixes only, with no phrase lists, no question-type regexes and no
per-question special cases. Every rule reads the catalog, the knowledge files or the data.

Unlike every earlier change, Group B changes behaviour **with the spelling help off**. The parity harness therefore
lists each touched notebook definition and explains every difference in the replays by the fix that intends it
(see [Checks](#checks)).

| item | what was wrong (round 2) | fix | where |
|---|---|---|---|
| 7a | a linked record's small lookups (a lead's intake interviews) were cut by a fixed per-person share | each record's top lookup is picked first; references cut for size are listed (`more_references`) | `tools/people.py` (`_about_people`, `_people_budget`) |
| 7b | identity-linked records (a Recruiter-type Account, the logins) only in `matched_records` | `people[].linked`: one line per linked record | `tools/people.py` |
| 7c | no distinct count when the counted object is not the noun ("55 trainings, how many candidates") | `references[].distinct`: `COUNT(DISTINCT subject)` per reference | `tools/people.py`, `tools/lookups.py` |
| 7d | a count through the owner did not show the copied owner on another object | `lookup_check[].related`: child objects holding the person in a role with a shared ROLE_CUES word, rows and distinct parents | `tools/lookups.py` |
| 7e | the other person's other people in the same role were not read (a candidate's other trainers) | `links[].others` | `tools/people.py` (`_people_links`) |
| 7f | a list showed only the filter's own rows (not the lead-side row, not the attendee-row interviewers) | `also_held` (rows of the other lookups holding the person) and `child_people` (people of the listed rows' child rows, matched by a role picklist value equal to the column's label word) | `tools/lookups.py`, `tools/records.py` |
| 8 | sessions of a candidate counted only through the direct (legacy) lookup | one- and two-hop parent paths from the catalog's lookup graph, kept only when the last lookup has the filter's `ref_role` and the filter holds the record's subject (a candidate or lead): `lookup_check[].paths`, `any_path`; `references[].via_parents`; the draft check `paths_nudges` | `tools/lookups.py`, `tools/people.py`, `agent/nudges.py` |
| 9 | the recruiter role picked a partly filled copy on the candidate | `FieldSpec.child_copy` from `field_dependencies` (a copy of a child object's lookup) with its fill compared at open; the profile says "partial copy of ..." and shows the source's values; counts get `child_copy` / `copy_source`; hints rank the copy right after its source; the draft check `child_copy_nudges` | `engine/catalog.py`, `tools/people.py`, `tools/record_tools.py`, `tools/names.py`, `agent/nudges.py` |
| 10 | reporting line read backwards; "manager" used for a reporting line | the profile key of a self-lookup states its direction (`FieldSpec.from_source`); draft checks `relation_wording_nudges` and `direction_nudges`; a prompt rule | `engine/catalog.py`, `agent/nudges.py`, `agent/record_session.py`, `prompts/records_agent_system.md` |
| 11 | "interviewed / taken" counts without saying that some are not completed | a code line under the final answer (`final` only, never `body`): the status split and the values with no record, when the question has a completion cue, a person count has a status split and the answer names none of the zero values; `links[].by_status`; two prompt rules | `agent/nudges.py` (`status_lines`, `with_status_line`), `pipeline.answer`, `prompts/records_status_line.md`, `prompts/combine_system.md` |
| 12 | two users with the same name: an owned record credited to the wrong one | `references[].who` on the references of same-name people, `people[].system_work`; a count gets per-record numbers for 2-5 same-name people | `tools/people.py`, `tools/lookups.py` (`_finish_check`) |
| 13 | a first name alone, when both the route call and the names call miss | `RecordTools.first_name_fit`: kept only when the word is no design word, every stored record with that first name has the same full name, a business lookup holding those records fits a question word, and a common English word (a lower-case entry of the system word lists) is written capitalised inside the sentence | `tools/names.py`, `agent/record_session.py` (triage) |

Resolver leftovers of the same run (switch on only; the switch-off path is unchanged):

- **Step (3), knowledge side:** a misspelt object or field argument of the knowledge tools is used when the resolver
  says `corrected` with one target, and the result states it (`read_as`); anything less clear is a did-you-mean.
  `tools/knowledge_tools.py`; see [resolver_integration.md](resolver_integration.md).
- **Step (5):** the E4h close-spellings list starts with the resolver's names (records of the filter's targets only)
  (`LookupsMixin._e4h`). A first name the spelling help read (`first_only` and `read_as` in `question_people()`)
  that several people share gets the several-people check on the full names (`people_nudges`; `<<WORD>>` is the
  stored first name).

## Result keys (as built)

Every key can be missing: the result-size budget drops the new details first, so readers must work without them.

| contract | key | shape |
|---|---|---|
| C1 | `people[].linked` | `"<Object label> '<label>' (<detail>[, active\|inactive]; <how>)"`, for a person with 2 or more records |
| C2 | `references[].who`, `system_references[].who` | `"<label> (<Object label>[, <detail>])"`, only for people who share their name with another listed person |
| C2 | `people[].system_work` | `["<Obj>.<field> <n>", ...]` (at most 4) |
| C3 | `references[].distinct` | `{"<subject lookup>": n}`, for references of 2 or more rows whose lookup is not a candidate lookup and whose object is no candidate object; subject lookups hold a candidate or lead (`_subject_role`), never a referrer, a client account or the object itself |
| C4 | `links[].others` | `{"field": "<Obj>.<f>", "role", "of": "P<k>", "records", "names": ["<name> (<n>)", "and N more"]}`; from the side whose own column holds the subject (the candidate's other trainers), else the side with fewer rows |
| C4 | `links[].holder` | an own-record link: the person whose record holds the lookup |
| C4 | `links[].by_status` | `{"<status field>": {value: n}}`, for links of 2 or more records |
| C5 | `lookup_check[].paths`, `any_path` | `[{"path": "A.P -> C.K", "role", "fits", "records"}]`; `any_path` = the filter or any path or same-role child rows; counted in their own statement after the lookup check (best effort, within the scan budget) |
| C5 | `lookup_check[].related` | `[{"object", "field", "via", "role", "fits", "records", "distinct", "same_role"}]`; rows that point to a base record that meets the query's conditions; one entry per (object, lookup) |
| C5 | `lookup_check[].copy_source` | `{"field", "source", "via", "records"}` (no `related` entry repeats the source) |
| C5 | `lookup_check[].by_record[].any_path`, `.copy_source` | each same-name record on its own (2-5 records with one name) |
| C6 | `child_copy` (count, group, list, aggregate) | `[{"field", "source", "via", "filled", "source_filled"}]` |
| C7 | `RecordTools.first_name_fit(question, spot)` | `{"word", "lookup", "records", "name", "name_word"}` or None; never raises |
| C8 | `also_held`, `child_people` (list_records) | `[{"field", "via", "group", "rows", "total"}]` (rows the filter does not hold; `total` counts them); `[{"object", "field", "role_field", "role", "column", "by_row"}]` |
| 4 | `matched_records[].shared_by`, `references[].shared_by`, `system_references[].shared_by` | `["P<k>", ...]` in listing order, instead of `person`: a record several listed people's identity links reach, nobody's own (evaluation fix 4 with its reviews) |
| 4 | `shared_records` | `{"R<k>": ["P<k>", ...]}`: the shared records and their people |
| 4 | `matched_records[].system_work` | `["<Obj>.<field> <n>", ...]` (at most 4): a shared record's own system work; a detail like `people[].system_work` |
| notes | about_people keys note | one note on the new keys present, only when it fits the result size |
| C9 | profile keys | `"<Api> (<label>; <from_source>)"` for self-lookups; `"<Api> (<label>; partial copy of <C>.<L>)"`; source `"<C>.<L> (via <K>)"` |
| 13 | `Triage.as_dict()["first_name_fit"]` | only when the rescue happened |
| 11 | `Outcome.status_lines`, JSONL `status_lines` | only when lines exist |
| 4a | knowledge tool `read_as` / `read_as_arguments` | `[{"arg", "typed", "used", "kind", "target", "how"}]`; next to a search tool's expansion dict it is `read_as_arguments` and is not stated in the answer |

The code-written people answer (`agent/people_answer.py`) cuts a profile label at its first "; ", so C9's wording
reads as the plain label there.

## Where the build differs from the plan

1. **7a share:** the old share (24 / people, 2 to 8 references) is kept; only the selection changed. The plan's larger
   share pushed multi-name results over the size limit.
2. **Budget order:** the new details are cut first, before anything the result had before (review fix: the
   `linked` lines too, except the short lines of the asked person of a full name; see the review fixes).
3. **`who`** only on the references of same-name people.
4. **7d role words** come from `ref_role` and must be ROLE_CUES role words (the `from_target` words matched interviewer
   to session host).
5. **Item 8:** objects with one row per person and role (a role picklist with a value like "Candidate") get no parent
   paths; `any_path` also counts same-role child rows.
6. **Profile direction text** only for a person's lookup to their own object.
7. **10a** uses label words only and needs every label word of a sibling lookup; **10b** also nudges with no reference
   (a profile value is enough), and reads "reporting line" style nouns as unclear.
8. **Item 11:** a bracket in a link example that is not a status value is skipped, and no line comes when the question
   names the status field itself.
9. **4a:** "check the object" sentences were dropped (generic names listed unrelated objects).

## Review fixes (2026-10-21)

A review of the built fixes found these, all fixed with fake-data tests (`tests/test_tools_groupb.py`,
`tests/test_agent_groupb.py`, `tests/test_evaluation.py`) and model-free snapshot checks:

- **Subjects** (`LookupsMixin._subject_role`): a lookup holds a record's subject when it points to a Lead or a Person
  Account and its role words start with a stem only the candidate side of `ROLE_CUES` has ("candidate", "applicant",
  "lead"). It limits the distinct counts (7c: no "different referrers" on an employee's assigned accounts), the
  parent paths and same-role child rows (8: a step's trainer has a holder of its own), the 7d related rows (a role
  that shares a word counts only on a candidate object through the child's subject lookup: never the owner of a
  program's trainings, never a department's head) and the side of a link's `others` (7e).
- **Rows of one person per role** (`_role_rows`) are decided per object: an attendee object gets no parent paths for
  any of its lookups (its `Lead__c` too).
- **Same-name records** get their own `any_path` and `copy_source` (`by_record`), and the rows of a role that only
  shares a word are left out for them (item 12: never merged).
- **`also_held`** leaves out the rows the filter already lists; **related rows** count only rows that point to a base
  record; the **path counts** run in their own statement, so a time-out never costs the lookup check.
- **Size:** without links the `linked` lines lose how each is linked first, then yield (except for the asked person
  of a full name) before anything the old result had; the first-link rule keeps the other people of a link whose
  role the question asks about, as the last detail to go; the keys note is added only when it fits; the copy's
  source sentence is one note; the merge's record facts drop the Group B details and notes before anything else
  (`agent/checks.facts_json`, `tools.record_base.DETAIL_NOTE_MARKS`).
- **Hints:** a child copy comes right after its source (it was ranked after every fitting lookup and could be cut).
- **Item 10:** a passive question ("who is X managed by?") reads the person's own value; a person with references
  through a self-lookup but no own value still gets the target-side check; an own-record link carries `holder` and
  the status split reads a link's roles from the catalog, not from its text.
- **Item 11:** no status line for a call filtered on the status field, or when splits of two or more people share an
  object and field. The graders still grade `answer_body` only; the people grader lists what the lines would add
  (`checks["status_lines_add"]`), so item 11 is measured apart.
- **Item 13:** a common English word is rescued only when written capitalised inside the sentence.
- **Built now:** `paths_nudges` (item 8), `child_copy_nudges` (item 9), the several-people check for a first name the
  spelling help read (4b), and the 7g prompt rule (`records_agent_system` version 3).
- `people_answer` reads a copy's source key as "<field> (on <object>)", after the record's own items.

## Evaluation fixes (2026-10-22)

The evening evaluation of 2026-10-22 (people dev and holdout sets worse than round 2; the holdout stayed blind: only
its general causes were used) led to six fixes, under the same rule: general fixes, no phrase lists, no per-question
or per-person cases. They change behaviour with the spelling help off, like Group B; the parity harness lists every
touched notebook definition (`CODE_ALLOWED`, block "Evaluation fixes of 2026-10-22") and the check explains every
difference against the code that ran the evaluation by one of the fixes.

| fix | what was wrong (evening run) | fix | where |
|---|---|---|---|
| 1 | every employee profile lost its joining date: the profile list was cut at 10 fields per object, and a field the new Application Tracking knowledge documented (`Recruiter__c.Designation__c`, filled on about a third of the employees) ranked above the dates, so `Date_of_Joining__c` became the 11th | the profile rule below | `engine/catalog.py` (`profile_fields`), `tools/people.py` (`_profiles`, `_core_value`, `_people_budget`), `tools/record_base.py` (`_Call.profile_details`) |
| 2 | an answer was only its Source line: the record session took such a reply as an answer, the merge was rejected, and its fallback showed the empty record answer | an answer with nothing but its Source line and the lines code writes around it is no answer (`agent.nudges.answer_text`), in the record session, the merge and the answer choice below | `agent/nudges.py`, `agent/record_session.py`, `agent/merge.py`, `agent/people_answer.py`, `pipeline.choose_answer`, `cli.print_outcome` |
| 3 | a merge was rejected for a count-0 clause that the record answer itself has, so the record answer with the same clause was shown | a merged count-0 clause that a clause of the record answer holds with the same claim is no reason to reject the merge (the any-role clause is fixed by `fix_any_role_scope`, not let through) | `agent/merge.py` (`_in_record_answer`, `merge_scope_issues`, `_drop_system_items`) |
| 4 | two employees with the same name, whose records both point to one person account and its portal user: the user's work was credited to both | a record joins a person only through that person's identity links; a record that several listed people's records reach belongs to none of them; the rule below | `tools/people.py` (`_about_people`, `_shared_records`, `_shared_notes`, `_ref_owner`), `agent/people_answer.py` |
| 5 | "who reports to X" was counted through `Manager__c` and no check fired: the direction check needs a role cue, and there is none for "reports" | the relation-lookup check below | `agent/nudges.py` (`relation_lookup_nudges`, `direction_nudges`), `agent/record_session.py` (`person_lookups`), `prompts/records_relation_lookup_*.md` |
| 6 | "applications sent this month" counted `Application__c` rows instead of summing `Total_Applications__c` | knowledge wording: one record is one daily update of one recruiter for one candidate, not one application; applications sent = the sum of `Total_Applications__c` (`kb_build/README.md`) | `kb_build/curated/Application__c.json` -> `knowledge/objects/Application__c.json` |

**The profile (fix 1).**

- `Catalog.profile_fields` gives every candidate field of the object in rank order (standard fields, lifecycle and
  status, lookups, categorical fields, dates; within each, the most filled first), with a safety cap
  `PROFILE_FIELDS_MAX` 40 (a person object has at most 19 today). The list is per object.
- `_profiles` keeps, per record, the first `PROFILE_MAX_FILLED` (12) values that are filled on that record, in that
  order. A field blank on the record never takes a place. A copy's source values (item 9) count toward the 12.
- The **core** of a profile is what the result-size budget never cuts first: the values of the first
  `PROFILE_CORE_FIELDS` (10) fields of the list (the old 10-field profile), plus the value of a later field that is
  filled on most records of the object (`2 * filled >= rows`) while the record has at most 10 filled values
  (`_core_value`). The other values go to `call.profile_details`, and `_people_budget` cuts them first and without a
  note, the last value of the longest profile first. So a larger profile never costs what the core showed (the
  first 10 fields plus later fields filled on most records); the joining date itself can cost a reference in an
  over-size result (dev question P35, with its size note). A rarely filled field that ranks above the joining date
  never moves the date out of the core on a record where it is blank, and the later Account fields (each filled on
  at most 12% of the accounts) stay details.
- Measured on the snapshot: the joining date is in every employee profile that has one (none
  before the fix); some account profiles gained values and none lost any; user and lead profiles are
  unchanged.
- Phase 1 of the privacy rules (2026-10-26, [data_and_privacy.md](data_and_privacy.md)): a profile never holds a
  contact field, unless the question asks for contact details (`RecordTools.begin_question`,
  `engine/contact.asks_for_contact`); then the filled values of up to `PROFILE_CONTACT_MAX` (6) contact fields of
  `Catalog.contact_fields` follow the profile values (documented fields first, then the most filled), as part of the
  core and outside the 12. A user's e-mail encoding (`User.EmailEncodingKey`, a character set) is no contact field
  since the phase-1 review, and `User.Username` (an e-mail address) is one.
- The check phase changed the lane's first rule (only the first 10 fields of the list were the core): with it, one
  over-size result still lost an employee's joining date (dev question P05).

**One person per record (fix 4, with the review of 2026-10-22).** A record joins a person only through an identity
link of that person's records, never through its name. A record that the links of several listed people's records
reach (two same-name employees whose `Recruiter_Contact__c` point to one account, with its portal user; or an
employee whose login lookup points to another employee's portal user) belongs to none of them: the data does not say
whose it is. The lane's first rule gave it to the first person in matched order (Id order for same-name people, the
order of the names otherwise); on the snapshot that put a portal user under the older of two same-name employees,
while nearly all the interviews the user created name the other one as recruiter. Now:

- the record is listed once in `matched_records` with `shared_by` (the people in listing order) instead of
  `person`, and `how` gives each person's identity link; it is in no person's `records` or `linked` lines;
- its references and system references carry `shared_by` instead of `person` (one person's share per group of
  people), count once and never in a person's `system_work`; its own system work is on its matched record
  (`matched_records[].system_work`, a detail like `people[].system_work`: the merge request's record facts drop it
  first when they are over their size), and the keys note says what `shared_by` and that system work are;
- result key `shared_records`: `{record ref: [person refs]}`;
- a note per group: "R2 (Account) and R3 (User) are linked to the records of both P1 and P2 (R1: ...; R4: ...): the
  data does not say which of them uses them, so their references, system references and system work (shared_by)
  count once, as shared, never as P1's or P2's own";
- the same-name note: "... have the same name, not linked except through the records they share (P1 and P2: R2,
  R3): ..."; no "has no linked user" note for a person whose user is shared;
- people of different asked names that share a record get a link `how: "shared record"` (so they are no pair
  without links), first in `links`;
- a shared record whose holder is not listed goes to the first listed person that links to it (one listed person:
  no sharing);
- the code-written people answer names the shared records in its same-name line ("... (the data does not say which
  of them uses them)"), or in a line of its own for people of one asked name with different names;
- (second review, 2026-10-23) such a record counts for no person's ranking total, and the totals a listed person
  shows (`business_reference_total`, `no_business_references`) are those of that person's own records in the
  listing (a record moved from "shared" counts for its new holder). The link finder maps it to nobody
  (`PeopleMixin._multi_held`), and it is never a parent, a shared parent or another person in a role: before, a link
  gave a shared portal user's creations to whichever person the order of the names made its holder ("P2 is
  `Recruiter__c` and P1 is `CreatedById` on 3 Interview records" next to "the data does not say which of them uses
  it"). On the snapshot 4 of the 5 different-name pairs whose links reach one portal user now give the same result in
  both name orders (1 before); the fifth differs only in which system references the result-size budget keeps.

A consequence on the snapshot: a person reached only through another person's identity-linked record (an employee
whose login lookup points to another employee's portal user) is no longer listed for that other person's name. The
asked person is then no longer the "primary" of two people of their name and gets the normal share of a call that
details two people (8 references, the rest in `more_references` and a note).

**Which answer is shown (fix 2).** `pipeline.choose_answer`, in this order:

1. the merged answer, when the merge was accepted and says something besides its Source line;
2. the record answer (the merge's fallback, or the record agent's answer when nothing was merged), with a body;
3. the code-written people answer (`people_answer_from`, with a Source line), when the record side failed
   (`rec.error`), the record agent used about_people, its last result covers every person the question names
   (`people_result_covers`: each name's words in one asked name or listed label; review of 2026-10-22: a result for
   one of two people is no answer about both; second review: or the words of one of a misspelt or reordered name's
   stored spellings, `RecordAnswer.named_spellings` from the triage's `close` and `read_as`, as the agent's call may
   take the stored spelling) and the triage says the question's names are stored (`people_data`);
4. the code's not-found answer;
5. the knowledge answer (records-only mode: none).

When nothing is left the answer is empty with kind `none`; an answer that is only a Source line is never shown; the
page then says "No answer was found for this question." unless there is no record error (records mode: "No record
data is needed for this question."). `answer_text` drops Source lines, the lines code writes around an answer
(a placeholder that ends the template's sentence, "Lookup used: <<LOOKUPS>>.", matches within one sentence, so a
model line that starts like the footer stays; any other placeholder runs to the fixed text after it, so a stored
name with an initial, "E. Two", still fills the other-people line: second review), lines with no
letter or digit and label or heading lines without a number; when nothing else is left, a label or heading line of
at least 3 words counts (review of 2026-10-22: "### No interviews are recorded for X" is an answer, "### Totals" is
not). The record session drops a reply with nothing but its Source line, as written or after its cleaners (`no_records`,
error "empty record answer...", the reply in `dropped_answer`), and code writes the people answer for a question
that names people, and also from the record agent's own about_people result when the triage confirmed nobody.
`combine` neither merges nor falls back on such a record answer (error "the record answer is empty", no model call)
and reads a merged reply with only a Source line as "empty merged answer". The CLI status line names the answer
shown: "the knowledge answer is shown" after an empty record answer, and "code wrote the record part from
about_people" for the pipeline's people answer.

**Merge clauses (fix 3, with the review of 2026-10-22).** In `merge_scope_issues`, the count-0 clause
(`_zero_lookup_presence`) is an issue only when no clause of the record answer holds it with the same claim
(`_in_record_answer`: one record-answer clause has the same lower-case words, numbers and API names in the same
order, markdown and punctuation ignored, and `_zero_lookup_presence` finds the same lookup in it). A rejected merge
falls back on the record answer, which says it too. A merged clause that drops the record answer's negation ("No
record shows that he is also referenced in X" -> "He is also referenced in X") is not the record answer's. The lane
had also exempted the any-role clause; the review limited fix 3 to the count-0 check, as the task said, and taught
`fix_any_role_scope` to split a bracket's items at "+" and "/" too, so "(Employee lookups + User creator/editor):
474" becomes "(Employee lookups): 474" in the record answer and in the merged text, and the merge passes without an
exemption.

**Relation words (fix 5).** `relation_lookup_nudges` runs from `check_draft` when `direction_nudges` sends nothing.
It fires when a word of the question (the person's name, the objects' names and the words every lookup of the object
shares left out) is in the knowledge wording of one person lookup (its label, `from_source` or `from_target`), and
the answer counted through another lookup of the same object to the same person object: a person-filtered count (the
lookup check) or a count grouped by a lookup (`fields_used`). It stays quiet when the role checks found a fit, a call
used that lookup, the draft names it, or about_people showed it for the person. The message names the lookup and the
direction the question asks for: the records that point to the person, the person's own value, or either (for a
lookup to another object only the first). `direction_nudges` also reads that wording when a lookup's label has no
word of the question. The words come from the knowledge, never from a list in the code. The review of 2026-10-22
added three rules after "who is in X's team" (answered with 28 reports through the reporting line) got the
own-value message for `Team_Lead__c` (0 records for X): the direction reads the words of the lookup's label that
the question has (else the hit words), so the "a word only one of the texts has" guard still sees "members"; the
person's own value only when every word of the label is in the question ("team" alone is half of "Team Lead": either
reading); and no message for the records that point to the person, or for either reading, when the person has 0
through that lookup while a used lookup has records (the answer's count stands). "Who reports to X" counted through
`Manager__c` (0) still gets the message for `Assigned_Sr_Recruiter__c`. The last rule also holds when the question's
word names the other lookup clearly: "who reports to X" counted through `Manager__c` with 5 records and 0 through the
reporting line gets no message (before the review: the message, whose text says to answer from that lookup, so the
model would answer 0). The second review (2026-10-23) left this as the owner's choice: keep it, or skip only the
either-reading form and send the target form with its 0 count and a "give both numbers" wording.

## Org Knowledge Base build (2026-10-27, integration 2026-10-28)

The build documented `Contact`, `Opportunity`, `User`, `Case` and the whole of `Account` and `Lead`. Two engine
rules keep the people results as they were where the new knowledge added only noise (model-free checks of the
integration: the people dev must facts and the replay of the 223 non-holdout answers of 2026-10-22):

- **`engine/catalog.py` `PEOPLE_SCAN_SKIP`** (`Contact`, `Opportunity`): `_child_pairs` (references, system
  references and system work, the people hints) and the link finder never read these objects. With their knowledge
  files they entered the catalog, and every person account's own person contact (`Contact.AccountId`, the same
  person) and every converted candidate's unused opportunity counted as a reference (in the dev results 59 and 4
  references, 9 and 11 left-out references); a person contact row also replaced a team-lead link, and two must facts
  ("no business record references him", dev P03/P04) were lost. Every other record tool still reads both objects.
- **`search_records`** (`tools/records.py`) and the people tools' fallback named-record list skip the same two
  objects unless the call names them (review fix, 2026-10-28): every candidate conversion leaves an Opportunity
  named after the candidate, so a name search returned those unused records next to the person's Account and Lead
  (17 Opportunity matches for 15 converted candidates on the snapshot, none since).
- **`PROFILE_DROP_ROLES` gains `b2b_attribute`**: a B2B field never takes a profile place.
  With it and `PROFILE_SKIP_FIELDS` (next item) the
  candidate profile list is the one before the build (19 fields, same order): work authorisation and visa status
  on as many profiles as before (fewer with the lanes' roles, where copies such as `AccountSource` and `Rating` and
  the B2B lookups ranked first).
- **`engine/catalog.py` `PROFILE_SKIP_FIELDS`** (`Account`: `AccountSource`, `Industry`, `PersonLeadSource`,
  `Rating`, `Type`; review fix, 2026-10-28): documented fields that a person's default profile never shows although
  their roles admit them, the unused standard picklists that lead conversion copies onto every candidate. An
  explicit list instead of role changes, so the knowledge keeps their descriptive roles (`categorical_attribute`
  ...); the record tools read each field when a question names it.
- The Lead profile list grows from 7 to 19 fields (`Resume_Attached__c`, `Rating`, `Plan__c`,
  `Work_Authorization__c`, `Visa_Status__c` ...). In an over-size first-name result this costs details: dev P30
  (19 matched records) loses the other trainers of a training (`links[].others`), which the result-size budget
  cuts first, so the must facts are 169 of 172 (171 before; 171 with the Lead profile of before, a diagnostic run
  only). The answer level of the replay is unchanged (no answer kind or final text changed, none empty).

## Value spots (2026-10-28)

The value spots ([value_spots.md](value_spots.md)) recognise the stored record values a question names. They share
three parts with the people path; the people decisions are otherwise as before:

- **Spotted after the people.** `RecordSession.triage` calls `spot_values` after the route call, the names call and
  the people decision, with the confirmed and the hinted people. Their words, and every stored full name of the
  question, belong to the people path. A value on them is spotted only when a cue word of its object or a whole
  quote makes it strong (then the hints show both readings), and a value's strength never comes from a person's
  words.
- **The missing-name rule.** A names-call name is no missing person when every word of it that is no design word lies
  inside one listed value spot of kind name, code or text. For example, the names call may return "Team Zentrova"
  for a stored department (a made-up name). Without the rule the question would get the "no stored person has this
  name" answer. The words of separate spots never pool, so a made-up full name whose first and last word are two
  different stored values stays a missing person. The `company_name` exception for client companies stays.
- **The log.** When a people overrule routed a question that the route call did not call `RECORDS`, the triage now
  records it: `overruled_by` "person" or "missing name", with `overrule_reason` "a named person" or "a name no stored
  person has". The record trace prints it as its first line. The decision itself is as before.

Model-free checks of the integration (the people harness of the Org Knowledge Base build):

- **Dev must facts:** 169 of 172, the same as before the build. No fact was lost or gained.
- **Replay:** the 223 non-holdout answers changed in no answer kind or final answer, and none is empty. Every
  difference is explained:
  - all 223 triage dumps gain the three new fields. In 3 of them a people overrule is now logged, and 8 have value
    spots.
  - 8 record hints gain the new section, one record each. They come from record questions (route `RECORDS`) that name
    a department, a program, an interview type or a company.
- **Profile lists:** unchanged.

## Step-2 fixes (2026-10-30)

The code-merge evaluation of 2026-10-29 found one answer (HP03) that showed an internal person code and the unasked
profile of a look-alike lead; the model merge had cut both. Both fixes are in shared code, so they hold for either
merge mode (`SFA_MERGE`), the page's records mode, `choose_answer`'s fallback and a rejected merge call:

- **No reference codes (F1).** The code-written people answer drops the about_people P/R codes the evidence maps
  (`checks.drop_ref_codes` with `reference_codes`), where code writes it (`RecordSession.ask`, `people_answer_from`),
  as the record agent's own reply already did. `people_answer` itself keeps them; a code the evidence does not hold
  stays. The merge call's RECORD ANSWER for such an answer has no codes either.
- **A look-alike closest name (F9).** A person found only as the closest stored name whose spelling is unlike the asked
  name (difflib ratio below `CLOSE_NAME_PROFILE_RATIO` = 0.85, lower case, spaces collapsed, a " (#k)" suffix aside)
  gets the not-found lead, its name and its kind only: "No record is named 'Quorra Zeld'; the closest stored name is
  'Quinta Zelder'. Quinta Zelder (P1) is a lead." (made-up names; the code goes as above). No detail, profile, linked
  records or references. An exact match and a record name keep today's sentence, and so does a closest stored name
  that is the asked person's name (review of 2026-10-30, `people_answer._look_alike`):
  - a typo with a spelling ratio of 0.85 or more (one wrong letter in a full name of 7 or more characters);
  - the asked name written another way, by the rule about_people picked the person with (`tools.names
    ._same_name_words`): the words in another order, an initial for a word, fewer words or an extra middle name;
  - a one-word ask that is one of the stored name's words or a close spelling of one (`tools.names._close_word`: 4 or
    more letters, the same first letter, similarity 0.8 or the same letters in another order), such as the spelling
    help's doubled-letter reading of a single name.

  Still look-alikes (the name and kind only): another name altogether (HP03), a one-letter typo in a full name shorter
  than 7 characters (its ratio is below 0.85, and its words are too short for a close spelling), and a single name the
  spelling help reads by its sound alone (a word shorter than 4 letters, or another first letter). To see the profile,
  ask again by the stored name. The multi-person line ("'x' matches N stored people: ...") is unchanged.

  An owner decision (the gap check of 2026-10-30): the review's exceptions reverse the fix's first design, which gave
  every closest stored name below the ratio only its name and kind. They keep the asked person's profile when the name
  is only written another way, at the cost of a look-alike's profile when another person's stored name shares the
  asked words (a one-word ask most of all). The stricter first design is the other choice (`docs/runbook.md`, "Known
  residuals" of the step-2 fixes).

## Still open

- The footer's union part of item 8 (the answer's footer giving `any_path`) was not built: the draft check covers it,
  and the footer is in `final` only, which no grader reads.
- The two-hop "attendee rows on the training's sessions" fact of one involvement question is still not in any result.
- Owner decisions: the item 11 status line also comes under 7 of the 40 lookup-set answers per mode (0 of 24 golden):
  keep it always, or only when the answer's lead claims the full total; and whether graders should read the status
  lines (today: never; `status_lines_add` shows what they would add). The asked person of a full name keeps the
  short `linked` lines over the last system references (one employee with many roles: 8 of 10 shown, the old result
  10). Lookup question Q30 scores the copy's count (`score: [26]`) while item 9 steers to the source (its `accept`
  allows the source's count with the field named).
- A lookup check can now exist with no other lookup (only paths, related rows or a copy hold the person), so the
  existing footer names the lookup used in more answers ("Lookup used: ..."). It is accurate; the owner may prefer it
  only when the other paths give another count.
- Holdout causes outside items 7-13 (trainee lists with no status, "who supports" missing, a list cut at 10 names, a
  field named without its count).
- After the evaluation fixes (2026-10-22): not evaluated with the model yet (the short re-run of the people dev and
  holdout sets waits for the owner), and the page and the MCP server run them only after their next restart. The
  code-written people answer gives up to 10 profile items (the profile core; it was 5, which left an employee's
  joining date out when 5 filled values came before it). `tools/names.py` `_people_hints` still groups the people of
  the question hints the old way (no effect seen on the snapshot). A fact that is in the tool result but left out of
  the answer (the model) and the revenue fix (on hold) are not part of these fixes.
- Owner decisions from the review of 2026-10-22: fix 6 makes "how many applications did X make" a sum of
  `Total_Applications__c`, while three answer keys (dev P20, typo T11, lookup n=35) score the row count; on the
  snapshot P20's rows hold no counts, so they sum to 0 (not recorded, not "none sent"), and
  lookup n=35's rows sum to very little; of the rows of those months only a few hold counts,
  nearly all in January. Update the keys, or add to the knowledge "give the number of updates too; blank counts add 0", before
  the people re-run grades P20.
- Second review (2026-10-23): the owner's choice on the relation-lookup rule above (a clear word, its lookup at 0, a
  used lookup with records: no message); the result-size budget cuts the system references of two detailed people
  by their P codes, so the order of the names can change which ones a large result keeps (one of the five pairs
  above); a shared record's references that its group's share leaves out get no `more_references` entry and no note.
- Not built after the review (outside its findings; each a small change for the owner to agree): the relation-lookup
  check still sends its "either reading" message when the person has 0 through the suggested lookup and through every
  used lookup ("who is in X's team" counted through `Manager__c` gets it for `Team_Lead__c`, both 0, while the lookup
  check names `Assigned_Sr_Recruiter__c`); and after an exception inside the record session the record answer has no
  named people, so the people-answer fallback's coverage check passes for any about_people result of that run.

## Checks

All model-free; the numbers are in `.private/groupb_checks_2026-10-21.md` (real names, not tracked) and
`.private/parity_2026-10-21_groupb.md`.

- Suites: default and `OKEVAL_RECORDS_LIVE=1`, each with `SFA_RESOLVER` off and on, all pass. New tests:
  `tests/test_tools_groupb.py`, `tests/test_agent_groupb.py`, `tests/test_resolver_knowledge_tools.py`, more in
  `tests/test_resolver_people.py`; fake data in `tests/groupb_fixture.py`.
- The must facts of the people dev questions in the tool results of the calls a correct answer needs: before and after.
- Parity: P0 lists every touched definition; P2 compares the two changed prompts as the notebook text plus exactly
  the edits of `tests/prompt_changes.py`; P4, P5 and P7 explain each difference by a class of `GROUPB_CLASSES` /
  `gb_*` (`tests/parity/harness.py`), split into the tool part and the agent part by a tools-only tree. The tracked
  parity tests (`tests/parity`) use the same rules on the fake fixtures.
- Evaluation fixes (2026-10-22; numbers in `.private/eval_2026-10-22/fixes_checks_2026-10-22.md`): new tests
  `tests/test_tools_people_fixes.py` and `tests/test_agent_eval_fixes.py`; the dev must facts are back to the
  2026-10-21 numbers; the record-tool replays of the parity blocks and of the evening run's sidecars, and the scripted
  end-to-end replays, are compared with the code that ran the evaluation in three steps (knowledge only: fix 6; the
  new tools with the old agent side: fixes 1 and 4; the agent side: fixes 2, 3 and 5), and every difference has the
  class of the fix that intends it (`EVALFIX_CLASSES` in `tests/parity/harness.py`).
- The review of 2026-10-22 (15 findings; same report, section "Review of 2026-10-22"): its fixes are compared with the
  check phase's final tree on the same knowledge (step R of `EVALFIX_CLASSES`): the evening sidecars' replay differs in
  52 items of 42 blocks (40 code-written people answers gain profile items, one same-name question's shared records,
  one typo question's corrected any-role bracket) and every one has a step R class; on that knowledge the shown
  answers still differ from the code that ran the evaluation only in dev P18 and typo T02 (with the knowledge of
  2026-10-23, P05 and T08 change too: their recorded merges cite numbers the new sources change); the dev must facts
  stay 171 of 172.
- The second review (2026-10-23, 11 findings; `.private/eval_2026-10-22/fixes_review_finish_2026-10-23.md`): against
  the first review's tree on the same knowledge the replay differs in 6 items of one block (the same-name question's
  keys note) and every one has a step R2 class; the shown answers, the merge acceptance and the dev must facts are
  unchanged; new fake-data tests in `tests/test_tools_people_fixes.py` and `tests/test_agent_eval_fixes.py`, each
  checked to fail when its fix is reverted.
