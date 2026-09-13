import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_RESPONSE_ID,
  EXAMPLE_STATUS,
  LONG_OUTPUT_WALL_CLOCK_LIVE,
  MODEL_ID,
  WALL_CLOCK_PENDING_NOTE,
} from '../samples';

// 2026-09-13, owner decision: max_output_tokens on techsara-35b goes to
// 1,000,000 — its whole context window — with input and output sharing that
// window, an over-long request clamped rather than refused, and the applied
// value reported back. This page exists because every one of those three
// facts changes client code, and because a 1M-token answer takes hours, which
// changes which request mode is safe to use at all (CONTRACT §8.3).
export const longOutput: DocPage = {
  slug: 'long-output',
  title: 'Long outputs',
  summary:
    'Up to 1,000,000 output tokens on techsara-35b: how the shared window ' +
    'clamps a request, how long it takes, and why only streaming or background suits it.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
\`${MODEL_ID}\` can generate up to **1,000,000 tokens** in one response — its
entire context window. That is a real ceiling, and it comes with three facts
your client has to handle: input and output share the window, a long answer
takes hours, and a synchronous request cannot wait that long.
${LONG_OUTPUT_WALL_CLOCK_LIVE ? '' : `\n${WALL_CLOCK_PENDING_NOTE}\n`}
## The ceiling

| Model | Largest \`max_output_tokens\` | When you do not ask |
| --- | --- | --- |
| \`${MODEL_ID}\` | 1,000,000 | 8,192 |
| \`techsara-8b-vision\` | 24,576 | 8,192 |
| \`techsara-ocr\` | 8,192 | 8,192 |

The default did not change: a request that does not send
\`max_output_tokens\` gets 8,192, as before. A value above the model's ceiling
— or above a lower ceiling your project sets — is a \`400\` with \`param\`
\`max_output_tokens\`. Read the ceilings from
[\`GET /v1/models\`](/docs/models) rather than from this table.

## Input and output share one window

The context window holds your prompt *and* the answer. A request for more
output than the prompt leaves room for is **clamped, not refused**:

~~~text
applied max_output_tokens = min(requested, context_window − input_tokens − reserve)
~~~

The reserve is a few hundred tokens of safety margin (512 on \`${MODEL_ID}\`).
With a 300,000-token prompt and \`"max_output_tokens": 1000000\`, the applied
ceiling is about 699,488. The request runs; it is simply not allowed to run
past the end of the window, which it could never have done anyway.

The one size refusal left is the **prompt** itself: over the model's
\`max_input_tokens\` is \`400 context_length_exceeded\`, before anything runs.

**The window is reached through output, not input.** All the text in one
request body is limited to 1 MiB, which is roughly 250,000 to 350,000 tokens
of prose. A prompt cannot fill a million-token window; an answer can.

## What the response tells you

Every response object carries two fields for this:

~~~json
{
  "id": "${EXAMPLE_RESPONSE_ID}",
  "object": "response",
  "created_at": 1789200000,
  "status": "completed",
  "model": "${MODEL_ID}",
  "output": [
    { "type": "message", "role": "assistant", "content": [{ "type": "output_text", "text": "…" }] }
  ],
  "max_output_tokens": 699488,
  "incomplete_details": { "reason": "max_output_tokens" },
  "usage": { "input_tokens": 300000, "output_tokens": 699488, "total_tokens": 999488 }
}
~~~

| Field | Meaning |
| --- | --- |
| \`max_output_tokens\` | The ceiling **applied** to this generation, after clamping. Events before generation starts carry the planned value; the final event and \`GET /v1/responses/{id}\` carry the exact one. |
| \`incomplete_details\` | \`{"reason": "max_output_tokens"}\` when the answer stopped because it reached that ceiling; \`null\` when the model finished on its own. |

\`status\` stays \`completed\` when the ceiling stopped the answer. If you know
other APIs of this shape, note the difference: there is no \`incomplete\`
status here, so check \`incomplete_details\`, not \`status\`, to learn whether
the text was cut.

On [Chat Completions](/docs/chat-completions) the same facts are
\`finish_reason: "length"\` and a top-level \`max_output_tokens\` on the
\`chat.completion\` object and on the streamed chunk that carries
\`finish_reason\`.

## How long it takes

Output is written one token at a time. Measured decode speed on
\`${MODEL_ID}\` is roughly 70 to 100 tokens per second, depending on how long the
context already is and how busy the engine is:

| Output tokens | At 100 tokens/s | At 70 tokens/s |
| --- | --- | --- |
| 5,000 | 50 seconds | 71 seconds |
| 8,192 | 82 seconds | 2 minutes |
| 100,000 | 17 minutes | 24 minutes |
| 1,000,000 | 2 hours 47 minutes | 3 hours 58 minutes |

A very long prompt adds time before the first token too. Plan a million-token
answer as a job that runs for an afternoon, not as a request.

## The wall clock

Every generation has a time limit, sized to what you asked for so a large
request is not cut off early:

~~~text
wall clock (seconds) = min(21600, max(4200, 900 + planned_max_output_tokens / 50))
~~~

900 seconds covers reading a very long prompt, and 50 tokens per second is a
deliberately pessimistic decode speed. On \`techsara-8b-vision\` and
\`techsara-ocr\` the same rule uses 600, 60 and 20.

| \`max_output_tokens\` applied | Wall clock |
| --- | --- |
| 8,192 (the default) | 4,200 s — 70 minutes |
| 200,000 | 4,900 s — 1 hour 22 minutes |
| 500,000 | 10,900 s — 3 hours 2 minutes |
| 1,000,000 | 20,900 s — 5 hours 48 minutes |

No generation runs longer than 21,600 seconds (6 hours). A generation that
reaches its wall clock ends \`failed\` with the code \`timeout\` — and **keeps
what it wrote**: on a stream the \`response.failed\` event carries the partial
text and the usage; a background response keeps its partial \`output\`; a
synchronous request gets a \`504\`. You are charged for the tokens that were
generated, as always. The engine sends its token counts only at the very end
of a stream, so for a response stopped by its wall clock the server counts
them itself: the output tokens it received, and your prompt as counted before
the call.

## Choose the mode before you choose the size

**A synchronous request is the wrong tool for a long answer.** It sends
nothing until the answer is complete, and the path to this API does not wait
forever for a first byte: the public hostname sits behind a proxy that gives
up on a silent response after **100 seconds** (you see an HTTP \`524\`), and
the edge in front of the API allows 300 seconds. At 70 to 100 tokens per
second, 100 seconds is about **5,000 tokens** — so even the 8,192 default can
be cut off in a synchronous call. If your client (or that proxy) closes the
connection, the server stops the generation there and records the response as
\`cancelled\` — it does not keep generating an answer nobody will receive.

| Expected output | Use |
| --- | --- |
| Under about 5,000 tokens | Any mode. |
| Above about 5,000 tokens | \`"stream": true\` if something is watching; \`"background": true\` if not. |
| Tens of thousands of tokens or more | \`"background": true\`, collected by [polling or webhook](/docs/background). |

Whatever the mode, send an [\`Idempotency-Key\`](/docs/idempotency). A retry
of a multi-hour request without one starts a second multi-hour request.

## A long stream

A stream sends its headers immediately and a heartbeat comment at least every
15 seconds for as long as it runs — hours, if need be — so no proxy on the way
sees an idle connection. What can still break it is your own client's timeout:
turn the read timeout off for the stream, and write the text somewhere as it
arrives instead of holding it all in memory.

~~~python
import json
import os
import uuid
import httpx

payload = {
    "model": "${MODEL_ID}",
    "input": "Write the complete field manual, chapter by chapter.",
    "max_output_tokens": 1000000,
    "stream": True,
}
headers = {
    "Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}",
    "Idempotency-Key": str(uuid.uuid4()),
}

with httpx.stream("POST", "${API_BASE_URL}/responses", headers=headers,
                  json=payload, timeout=httpx.Timeout(30.0, read=None)) as response:
    response.raise_for_status()
    event = None
    with open("manual.txt", "w", encoding="utf-8") as out:
        for line in response.iter_lines():
            if not line:
                event = None
                continue
            if line.startswith(":"):                 # heartbeat, every 15 s at most
                continue
            if line.startswith("event: "):
                event = line[len("event: "):]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: "):])
                if event == "response.output_text.delta":
                    out.write(data["delta"])
                elif event in ("response.completed", "response.failed"):
                    final = data["response"]
                    print("status:", final["status"],
                          "applied:", final["max_output_tokens"],
                          "cut:", final["incomplete_details"])
~~~

A dropped stream cannot be resumed, and the text of a streamed response is not
stored. For anything you cannot afford to lose halfway, use background.

## A long background job

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -H "Idempotency-Key: aaaa-bbbb-cccc-dddd" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "Write the complete field manual, chapter by chapter.",
    "max_output_tokens": 1000000,
    "background": true
  }'
~~~

The \`202\` carries the planned \`max_output_tokens\`. Then poll
\`GET /v1/responses/{id}\` slowly — once a minute is plenty for a job measured
in hours — or let a [webhook](/docs/webhooks) tell you it finished. The stored
response keeps the whole text for your project's retention window.

Two things to plan for:

* **It may wait before it starts.** A very long generation takes one of a
  small number of places reserved for long public work on the main engine,
  and waits for one while the row stays \`queued\` — for up to an hour — before
  it ends \`failed\` with \`model_unavailable\`. That is the engine's capacity,
  shared by every caller, not a limit on your project.
* **A service restart ends it.** A response that is running when the service
  restarts — which happens when a new version is deployed — ends \`failed\`
  with \`model_unavailable\` and the message "The service restarted while this
  response was running." It is safe to submit again — with a new
  \`Idempotency-Key\`, because the old one still names the failed job. The
  longer the job, the likelier it is to meet a restart, so treat a failure
  after hours as something your client retries, not as a bug.
`.trim(),
};
