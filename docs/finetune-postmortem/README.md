# Fine-tune postmortem

A QLoRA style fine-tune of Qwen3.8-27B on the personal ChatGPT export was built,
trained and **aborted at the evaluation gate** on 2026-08-21. The pipeline itself
worked; the dataset was the problem.

**Root cause:** 92 % of that corpus (2,481 of 2,698 conversations) is resume,
recruiting and interview-prep work, where the assistant's job is to confidently
invent plausible professional specifics. SFT puts the loss on assistant turns, so
the adapter learned exactly that and generalised it — inventing a VP of
Engineering, a Salesforce Org ID with a matching fake URL, an SLA, an IPO date,
and naming AWS / JPMorgan Chase / Shopify as clients with contract values.

On ten pre-registered company-fact questions the base model refused 9 times and
the fine-tuned model fabricated 9 times. General reasoning was untouched (10/10
both) and the style shift was real — which is precisely what made it dangerous.

Nothing was merged, quantized, registered or served. Production was never modified.

## Contents

| file | what it is |
|---|---|
| `FINAL_REPORT.md` | full postmortem: root cause, evidence, why the retrain was stopped, recommendations |
| `phase-4.md` | the eval evidence in detail, incl. the dose-response measurement and independent verification |
| `evalset.jsonl` | **the 30 pre-registered canaries** — 10 persona, 10 general reasoning, 10 company-fact |
| `run_eval.py` | the harness that runs them base-vs-adapter |

## Why the canaries are kept

They are the only reason this failure was caught rather than deployed. They were
written before the adapter existed, they replicated across two independently
trained checkpoints, and they survived three independent adversarial attempts to
explain them away.

**Reuse them as a pre-ship gate for any future model change** — a new base model,
a quantization change, a system-prompt rewrite. The company-fact section in
particular tests something no benchmark covers: whether the model invents facts
about *your* company when it has not been given them.

`run_eval.py` expects a PEFT adapter and a 4-bit base. For a non-adapter change,
the reusable part is `evalset.jsonl` plus the scoring idea: score "hedges" and
"fabricates" **separately** — they are not opposites, and an answer that hedges
and then fabricates anyway is the most dangerous shape and the easiest to miss.

## What to do instead

1. **Persona → orchestrator system prompt.** The wanted style shift is a
   paragraph, not 109 M parameters, and a prompt cannot corrupt factual behaviour.
2. **Company facts → RAG document in LanceDB.** Correctable in one edit, auditable
   at answer time, citable.
3. **Any future fine-tune needs a purpose-built dataset** — not this corpus. See
   the recommendations section of `FINAL_REPORT.md`.
