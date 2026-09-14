import type { DocPage } from '../types';
import {
  API_BASE_URL,
  CONSOLE_PATH,
  EXAMPLE_LIVE_KEY,
  MODEL_ID,
  EXAMPLE_STATUS,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';

// 2026-09-13, no-timeout design (revision 2): the first example no longer
// teaches a client timeout that a long request would hit. Built in either
// state from NO_TIMEOUT_LIVE.

const INTRO = `

`;
const S_1_CREATE_A_PROJECT_AND_A_KEY = `
## 1. Create a project and a key

Keys are created in the developer console at [\`${CONSOLE_PATH}\`](${CONSOLE_PATH}),
which needs the \`api.console.access\` capability — a workspace admin has it;
a member does not. Ask an admin if the console is not there for you.

A key belongs to a **project**, which carries the model allowlist, the
retention window and the usage record, and to a **service account**, which is the named
machine identity inside that project. Give each deployed service its own key
so revoking one does not stop the others.

Choose a **test** key (\`tsk_test_…\`) while you are building and a **live**
key (\`tsk_live_…\`) when you ship. The prefix is the difference, so a human
reading a config file and a secret scanner reading a repository can both tell
at a glance which one leaked.
`;
const S_2_PUT_THE_KEY_IN_THE_ENVIRONMENT = `
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
`;
const S_3_ASK_FOR_SOMETHING = `
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
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": { "input_tokens": 37, "output_tokens": 112, "total_tokens": 149 }
}
~~~

\`usage\` is \`null\` — never \`0\` — when the engine did not report counts.
\`max_output_tokens\` is the output ceiling applied — 8,192 when you do not ask.
Zero would be a lie, and an under-charge.
`;
const S_4_DO_IT_FROM_YOUR_LANGUAGE = `
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
`;
const S_WHERE_TO_GO_NEXT = `
## Where to go next

* [The Responses API](/docs/responses) — every field, and every rule it is
  validated against.
* [Streaming](/docs/streaming) — server-sent events, and the one terminal event.
* [Background responses](/docs/background) — for work longer than a request.
* [The model reference](/docs/models) — the other five models: vision, OCR,
  embeddings, reranking and speech to text.
* [Errors](/docs/errors) — the codes, and which of them are safe to retry.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_4_DO_IT_FROM_YOUR_LANGUAGE = `
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
    timeout=httpx.Timeout(10.0, read=None),
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

Generation takes as long as generation takes, and the API has no timeout of its
own: leave your client's read timeout off, and [stream](/docs/streaming) so you
see the first token immediately. [Timeouts](/docs/timeouts) has the settings for
the \`openai\` packages.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function quickstartPage({ noTimeout }: { noTimeout: boolean }): DocPage {
  const sections = noTimeout
    ? [INTRO, S_1_CREATE_A_PROJECT_AND_A_KEY, S_2_PUT_THE_KEY_IN_THE_ENVIRONMENT, S_3_ASK_FOR_SOMETHING, LATER_4_DO_IT_FROM_YOUR_LANGUAGE, S_WHERE_TO_GO_NEXT]
    : [INTRO, S_1_CREATE_A_PROJECT_AND_A_KEY, S_2_PUT_THE_KEY_IN_THE_ENVIRONMENT, S_3_ASK_FOR_SOMETHING, S_4_DO_IT_FROM_YOUR_LANGUAGE, S_WHERE_TO_GO_NEXT];
  return {
    slug: 'quickstart',
    title: 'Quickstart',
    summary: 'From an empty terminal to a generated answer, in four steps.',
    section: 'Getting started',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const quickstart: DocPage = quickstartPage({ noTimeout: NO_TIMEOUT_LIVE });
