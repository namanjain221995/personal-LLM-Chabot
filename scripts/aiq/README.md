# aiq — the acceptance harness

What a user gets, measured. 66 cases run against an **isolated** orchestrator
(never production) and scored by code, not by reading:

| set | cases | what it measures |
|---|---|---|
| baseline | 40 (`BASELINE_CASES`) | the frozen set the 2026-09-17 baseline was scored with: formatting rewrites, big reports, CSV plots, follow-up edits, Fast factual answers, tables, how-tos |
| acceptance | 6 (`ACCEPTANCE_CASES`) | this round's reported failures: `C01` the owner's three Chat A turns verbatim, `E07` a chart added under an H2, `R07` a one-page brief that must not grow, `P09` a PNG ask with no data, `F07` a Fast search fallback that must not think, `K01` chart colours |
| coding | 20 (`CODING_CASES`) | Python, SQL, TypeScript, bash, debugging, refactoring and algorithms — the code in the answer is **compiled and run** in a scratch venv and checked, with a time budget on the three performance cases |

## One command

```bash
scripts/aiq/aiq-stack.sh all          # stack up, seed, full suite, gate, tear down
```

It runs on the **worker** (spark-476e) and refuses the head. Prerequisites,
each needed once:

```bash
# on the head (spark-0e68), read-only:
scripts/aiq/aiq-stack.sh export-env > prod.env && scp prod.env techsphere@10.100.184.2:~/.aiq-runtime/
scripts/aiq/aiq-stack.sh ship-image           # docker save | ssh docker load
scripts/aiq/aiq-stack.sh tunnel               # prints the one ssh command to keep running
```

Then, on the worker, anything narrower:

```bash
scripts/aiq/aiq-stack.sh run --category csv_plot          # one category, then tear down
scripts/aiq/aiq-stack.sh run --only C01,E07,K01
scripts/aiq/aiq-stack.sh candidate <dev-sha>              # production image + that commit's app
```

Re-scoring needs no model at all:

```bash
orchestrator/.venv/bin/python scripts/aiq/run.py --rescore runs/<stamp>
orchestrator/.venv/bin/python scripts/aiq/run.py --rescore runs/<stamp> --gate --baseline runs/worker-baseline-1
```

## The baseline

`runs/baseline-20260917/` is the production image `sha256:cec61feca1f7`
(main 9ef4602) — **overall 0.761**, 19 of 40 cases clean, csv_plot 0.363,
every plot job failing with "The artifact has no charts to draw as images".
`tests/test_aiq_harness.py` re-scores it in CI, so a check that quietly moves
the baseline fails the build. Changing a baseline case's wording or rubric
invalidates it; add a new case instead.

The 2026-09-17 head-run numbers stay only as a reference: the round's gate
compares against a re-baseline of the same image in the worker configuration.

## The gate (`gate.py`)

Blocking: overall ≥ 0.88, no category below its worker baseline (0.03
tolerance, the measured run-to-run noise), csv_plot without P06 ≥ 0.80,
followup_edit ≥ 0.80, big_report ≥ 0.80, format_rewrite ≥ 0.90, howto and
tables 1.00, charts ≥ 0.85, structure ≥ 0.90, length ≥ 0.80, thinking off on
100% of Fast turns — and four counts that must be exactly zero: the "no charts
to draw" sentence, "the table … is not available" in a dataset conversation, a
Fast turn carrying reasoning, and an edit that claims a chart it did not add.
The coding rows are reported, not blocking. `--gate` exits non-zero when it
fails, and writes `gate.json` next to the results.

## Rules this harness keeps

* Fixtures are synthetic (`make_fixtures.py`, fixed seeds): no user data, no
  production conversation, no production database, no production container.
* Secrets and the scratch venv live under `$AIQ_RUNTIME`, outside the repo.
* The model's code runs with no network, under ulimits, in a throwaway
  directory, and never as root (`code_sandbox.py`).
* `reference_solutions.py` is the control: CI proves every coding checker
  accepts a correct answer, so a broken checker cannot masquerade as a model
  regression.
