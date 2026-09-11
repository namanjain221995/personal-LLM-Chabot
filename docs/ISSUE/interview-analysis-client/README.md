# interview-analysis-v2: outage tolerance for the sweep (ready to apply)

`interview_analysis/models/client.py` on the worker
(`/home/techsphere/Documents/GitHub/interview-analysis-v2`, branch
`hd-dev-qms-cs`) is the client the root-cause report's §7 describes: its
retry loop catches only `LLMError`/`ValidationError`/`JSONDecodeError`, so an
`openai.APIConnectionError` during a 9-15 minute engine reload ends the run
on the first attempt. That repository holds uncommitted work of its own and a
live sweep was running from it on 2026-09-11, so the fix was NOT applied
there by the remediation programme; it is delivered here as two idempotent
patch scripts.

Apply from that repository's root (both refuse to run twice):

```bash
cd ~/Documents/GitHub/interview-analysis-v2
.venv/bin/python /path/to/personal-LLM-Chabot/docs/ISSUE/interview-analysis-client/patch_client.py
.venv/bin/python /path/to/personal-LLM-Chabot/docs/ISSUE/interview-analysis-client/patch_rubric_sweep.py
.venv/bin/python -m pytest tests -q
```

What changes:

- `LLMClient.chat_json` — a transport-class failure (connection refused/reset,
  connect timeout, a 5xx/429, a bare `APIError` from a dying engine, a read
  timeout on these seconds-long calls) polls the endpoint's `/health` and
  `/v1/models` until they answer, backs off with jitter (2..30 s) and re-issues
  the SAME attempt: it consumes no schema attempt and bumps no temperature.
  Bounded by `recovery_window_s` (default 1200 s = 20 min, covering the
  measured 13-minute reload); past it, `LLMError` names the endpoint. The SDK's
  own retries are switched off (`max_retries=0`) so this is the one retry
  layer. `LLMStats` gains `transport_retries` and `waited_s`.
- `scripts/rubric_sweep.py` — per-job logs stop being 0 bytes (a logging
  handler is bound to the captured buffer; `redirect_stderr` never reached the
  logging handlers), and a job stranded at `preprocessed` gets its analysis
  phase run (idempotent) before the refresh instead of the refusal being
  recorded as its result.

Also worth doing there, from the audit: the pipeline reaches the production
engine at `http://10.100.184.1:8000/v1` — the RoCE fabric address, which the
cluster's own compose files say must not carry service traffic — at
`text_concurrency: 10`. Point it at the head's management address (or through
the orchestrator) and record its source IP in Grafana's request-source panel
so chat latency regressions can be attributed to it.
