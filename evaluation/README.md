# Salesforce evaluation harness

This directory keeps golden expectations outside the candidate application.
The runner sends only the question, selected mode, conversation history and
`test_case_id` to `/chat`. After the stream finishes, it retrieves the bounded
diagnostic trace and joins that trace to the golden case offline.

## Before the first preprod run

1. Review a small, representative batch in
   `datasets/salesforce_eval_v1.yaml` and set each accepted case's
   `review_status: approved`. All current cases remain drafts, so the default
   runner cannot send them accidentally.
2. Set `SF_ENVIRONMENT=preprod` on the orchestrator. The application records
   this explicit label in provenance; it does not guess an environment from a
   Salesforce URL.
3. Sign in to the preprod chatbot and copy the raw browser `Cookie` request
   header into `EVALUATION_SESSION_COOKIE`. Do not commit this value.
4. Install the small harness dependencies:

   ```bash
   python -m pip install -r evaluation/requirements.txt
   ```

## Run an approved smoke batch

From the repository root:

```bash
export EVALUATION_BASE_URL=http://127.0.0.1:8080
export EVALUATION_SESSION_COOKIE='your raw Cookie header'
PYTHONPATH=. python -m evaluation.runners.evaluation_runner \
  --case-id SF-DATA-006 \
  --limit 1
```

Without `--case-id`, the runner takes at most ten approved cases. Draft cases
require the conspicuous `--allow-draft` override. The override is intended for
harness development, not benchmark reporting.

Reports are written under `evaluation/reports/` and include the application
response, privacy-bounded trace, stage checks and first incorrect stage. The
session cookie is never written to the report.

Exit codes:

- `0`: every selected case passed every critical check.
- `1`: the run completed, but one or more cases failed or were not evaluable.
- `2`: selection, authentication, HTTP, trace joining or report writing failed.

The `source` and `answer` comparators intentionally remain `not_evaluable` in
this increment. Therefore, cases that declare either as critical cannot report
an end-to-end pass yet; this prevents partial instrumentation from inflating
accuracy.
