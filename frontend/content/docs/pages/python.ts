import type { DocPage } from '../types';
import { API_BASE_URL, MODEL_ID, EXAMPLE_STATUS } from '../samples';

export const python: DocPage = {
  slug: 'python',
  title: 'Python',
  summary:
    'A small client over httpx: requests, streaming, retries, idempotency ' +
    'and background work.',
  section: 'Examples',
  examples: EXAMPLE_STATUS,
  body: `
There is no TechSara SDK yet. The API is plain HTTP and JSON, so any client
will do; the examples here use \`httpx\`, and \`requests\` works the same way
apart from streaming.

~~~bash
pip install httpx
export TECHSARA_API_KEY="tsk_live_…"
~~~

## A client

~~~python
"""A minimal TechSara client. One place for the base URL, the key and the
timeout, so no call site has to remember any of them."""
import os
import httpx

BASE_URL = "${API_BASE_URL}"
MODEL = "${MODEL_ID}"

def client() -> httpx.Client:
    key = os.environ["TECHSARA_API_KEY"]  # never a literal in source
    return httpx.Client(
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {key}"},
        # Generation takes as long as it takes. A 30-second default timeout
        # will cancel perfectly healthy work and then retry it, which costs
        # twice and fixes nothing.
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0),
    )
~~~

## One answer

~~~python
def ask(question: str) -> str:
    with client() as api:
        response = api.post("/responses", json={"model": MODEL, "input": question})
        response.raise_for_status()
        body = response.json()
    parts = [
        part["text"]
        for item in body.get("output", [])
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    ]
    return "".join(parts)
~~~

Reading \`output\` as a list, rather than reaching straight for
\`output[0].content[0].text\`, is what keeps this working when a future item
kind appears alongside the message.

## A conversation

~~~python
messages = [
    {"role": "system", "content": "You are a concise technical writer."},
    {"role": "user", "content": "Summarise our uptime policy."},
]

with client() as api:
    body = api.post("/responses", json={
        "model": MODEL,
        "input": messages,
        "temperature": 0.2,
        "max_output_tokens": 600,
    }).json()
~~~

The API is stateless: to continue, append the assistant's turn to
\`messages\` yourself and send the list again.

## Streaming

~~~python
import json

def stream(question: str):
    """Yield text as it is generated. Raises on a failure terminal."""
    payload = {"model": MODEL, "input": question, "stream": True}
    with client() as api:
        with api.stream("POST", "/responses", json=payload, timeout=None) as response:
            response.raise_for_status()
            event = None
            for line in response.iter_lines():
                if not line:
                    event = None
                    continue
                if line.startswith(":"):        # heartbeat comment
                    continue
                if line.startswith("event: "):
                    event = line[len("event: "):]
                elif line.startswith("data: "):
                    data = json.loads(line[len("data: "):])
                    if event == "response.output_text.delta":
                        yield data["delta"]
                    elif event == "response.failed":
                        raise RuntimeError(data["response"]["error"]["code"])
                    elif event == "error":
                        raise RuntimeError(data["code"])

for piece in stream("Explain retrieval-augmented generation."):
    print(piece, end="", flush=True)
~~~

See [streaming](/docs/streaming) for the full event grammar, including the
\`sequence_number\` check worth adding in production.

## Errors and retries

~~~python
import random
import time

RETRYABLE = {
    "rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
    "model_recovering", "model_unavailable", "timeout",
}

class TechSaraError(RuntimeError):
    def __init__(self, body: dict):
        error = body.get("error", {})
        self.code = error.get("code", "")
        self.request_id = error.get("request_id", "")
        super().__init__(f"{self.code}: {error.get('message', '')} [{self.request_id}]")

def post_with_retry(api: httpx.Client, path: str, payload: dict, *, attempts: int = 5):
    for attempt in range(attempts):
        response = api.post(path, json=payload)
        if response.is_success:
            return response.json()
        body = response.json()
        code = body.get("error", {}).get("code", "")
        if code not in RETRYABLE or attempt == attempts - 1:
            raise TechSaraError(body)
        wait = float(response.headers.get("Retry-After", 2 ** attempt))
        time.sleep(wait + random.uniform(0, 1))   # jitter, so a fleet does not
                                                  # come back in lockstep
~~~

Always keep \`request_id\`. It is the one thing that lets somebody find your
request without you sending anything sensitive.

## Idempotent and background

~~~python
import uuid

def summarise_in_background(api: httpx.Client, ticket_id: str, text: str) -> str:
    key = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ticket:{ticket_id}"))
    body = api.post(
        "/responses",
        headers={"Idempotency-Key": key},
        json={"model": MODEL, "input": text, "background": True},
    ).json()
    return body["id"]        # 202 Accepted; collect by polling or webhook
~~~

See [idempotency](/docs/idempotency) and
[background responses](/docs/background).

## Using an OpenAI-shaped client

If you already have one, point it at \`${API_BASE_URL}\` and use
[\`/v1/chat/completions\`](/docs/chat-completions). Expect to remove
parameters this platform does not accept — they are rejected rather than
ignored, and the error names the field. [Migration](/docs/migration) has the
details.
`.trim(),
};
