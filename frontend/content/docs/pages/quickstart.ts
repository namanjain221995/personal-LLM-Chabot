import type { DocPage } from '../types';
import {
  API_BASE_URL,
  CONSOLE_PATH,
  EXAMPLE_LIVE_KEY,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';

export const quickstart: DocPage = {
  slug: 'quickstart',
  title: 'Quickstart',
  summary: 'From an empty terminal to a generated answer, in four steps.',
  section: 'Getting started',
  examples: EXAMPLE_STATUS,
  body: `
## 1. Create a project and a key

Keys are created in the developer console at [\`${CONSOLE_PATH}\`](${CONSOLE_PATH}),
which needs the \`api.console.access\` capability — a workspace admin has it;
a member does not. Ask an admin if the console is not there for you.

A key belongs to a **project**, which carries the limits, the model allowlist
and the retention window, and to a **service account**, which is the named
machine identity inside that project. Give each deployed service its own key
so revoking one does not stop the others.

Choose a **test** key (\`tsk_test_…\`) while you are building and a **live**
key (\`tsk_live_…\`) when you ship. The prefix is the difference, so a human
reading a config file and a secret scanner reading a repository can both tell
at a glance which one leaked.

## 2. Put the key in the environment

The secret is shown **once**, at creation. It is stored as a keyed digest and
cannot be recovered — if you lose it, rotate.

~~~bash
export TECHSARA_API_KEY="${EXAMPLE_LIVE_KEY}"
~~~

That key is a documentation example and is not valid: its checksum is the
literal text \`EXAMPL\`, so the platform rejects it offline with
\`401 invalid_api_key\` before any lookup happens. Paste your own.

Never put a live key in a browser bundle, a mobile app, a public repository or
a client-side environment variable. See
[API-key security](/docs/key-security).

## 3. Ask for something

~~~bash
curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "Explain retrieval-augmented generation in two sentences."
  }'
~~~

The answer comes back in the response envelope:

~~~json
{
  "id": "resp_4f2b8c1d9e0a7b6c5d4e3f20",
  "object": "response",
  "created_at": 1789200000,
  "status": "completed",
  "model": "${MODEL_ID}",
  "output": [
    {
      "type": "message",
      "role": "assistant",
      "content": [{ "type": "output_text", "text": "…" }]
    }
  ],
  "usage": { "input_tokens": 37, "output_tokens": 112, "total_tokens": 149 }
}
~~~

\`usage\` is \`null\` — never \`0\` — when the engine did not report counts.
Zero would be a lie, and an under-charge.

## 4. Do it from your language

~~~python
import os
import httpx

response = httpx.post(
    "${API_BASE_URL}/responses",
    headers={"Authorization": f"Bearer {os.environ['TECHSARA_API_KEY']}"},
    json={
        "model": "${MODEL_ID}",
        "input": "Explain retrieval-augmented generation in two sentences.",
    },
    timeout=120.0,
)
response.raise_for_status()
answer = response.json()["output"][0]["content"][0]["text"]
print(answer)
~~~

~~~typescript
const response = await fetch("${API_BASE_URL}/responses", {
  method: "POST",
  headers: {
    Authorization: \`Bearer \${process.env.TECHSARA_API_KEY}\`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({
    model: "${MODEL_ID}",
    input: "Explain retrieval-augmented generation in two sentences.",
  }),
});

if (!response.ok) {
  const { error } = await response.json();
  throw new Error(\`\${error.code}: \${error.message}\`);
}

const body = await response.json();
console.log(body.output[0].content[0].text);
~~~

Generation takes as long as generation takes. Set a client timeout in
minutes, not seconds, or [stream](/docs/streaming) so you see the first token
immediately.

## Where to go next

* [The Responses API](/docs/responses) — every field, and every rule it is
  validated against.
* [Streaming](/docs/streaming) — server-sent events, and the one terminal event.
* [Background responses](/docs/background) — for work longer than a request.
* [Errors](/docs/errors) — the codes, and which of them are safe to retry.
`.trim(),
};
