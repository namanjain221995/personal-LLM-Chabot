import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EMBED_MODEL_ID,
  EXAMPLE_STATUS,
  MODEL_ID,
  OCR_MODEL_ID,
  RERANK_MODEL_ID,
  VISION_MODEL_ID,
  WHISPER_MODEL_ID,
} from '../samples';
import { NO_TIMEOUT_LIVE } from './longOutput';
import { SIDECARS_NO_TIMEOUT_LIVE } from './sidecarsLive';

// 2026-09-13, no-timeout design (revision 2): the Python page gains the exact
// `openai` package client settings (timeout None, never 0) and a resume loop,
// and loses the advice that only streaming or background suits a long answer.
// Built in either state from NO_TIMEOUT_LIVE.

const INTRO = `
There is no TechSara SDK yet. The API is plain HTTP and JSON, so any client
will do; the examples here use \`httpx\`, and \`requests\` works the same way
apart from streaming.

~~~bash
pip install httpx
export TECHSARA_API_KEY="tsk_live_…"
~~~
`;
const S_A_CLIENT = `
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
`;
const S_ONE_ANSWER = `
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
`;
const S_A_CONVERSATION = `
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
`;
const S_STREAMING = `
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
`;
const S_ERRORS_AND_RETRIES = `
## Errors and retries

~~~python
import random
import time

RETRYABLE = {
    # quota_exceeded and concurrency_limit_exceeded arrive only if an operator
    # enables limits; listing them costs nothing.
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
        # A 409 with Retry-After is this key's first request, still running.
        running = response.status_code == 409 and "Retry-After" in response.headers
        if (code not in RETRYABLE and not running) or attempt == attempts - 1:
            raise TechSaraError(body)
        wait = float(response.headers.get("Retry-After", 2 ** attempt))
        time.sleep(wait + random.uniform(0, 1))   # jitter, so a fleet does not
                                                  # come back in lockstep
~~~

Always keep \`request_id\`. It is the one thing that lets somebody find your
request without you sending anything sensitive.
`;
const S_IDEMPOTENT_AND_BACKGROUND = `
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
`;
const S_AN_IMAGE = `
## An image

~~~python
import base64

def _data_url(path: str, mime: str) -> str:
    with open(path, "rb") as fh:
        return f"data:{mime};base64," + base64.b64encode(fh.read()).decode("ascii")

def describe(path: str, question: str, mime: str = "image/png") -> str:
    with client() as api:
        response = api.post("/responses", json={
            "model": "${VISION_MODEL_ID}",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": question},
                {"type": "input_image", "image_url": _data_url(path, mime)},
            ]}],
        })
        response.raise_for_status()
        return response.json()["output"][0]["content"][0]["text"]

def read_text(path: str, mime: str = "image/png") -> str:
    # OCR one image. No text part at all, so the server adds the instruction
    # this model reads best with.
    with client() as api:
        response = api.post("/responses", json={
            "model": "${OCR_MODEL_ID}",
            "input": [{"role": "user", "content": [
                {"type": "input_image", "image_url": _data_url(path, mime)},
            ]}],
        })
        response.raise_for_status()
        return response.json()["output"][0]["content"][0]["text"]
~~~

Images are always sent as \`data:\` URLs; a link is refused. See
[images and OCR](/docs/images).
`;
const S_SEARCH_EMBED_THEN_RERANK = `
## Search: embed, then rerank

~~~python
def embed(texts: list[str]) -> list[list[float]]:
    with client() as api:
        response = api.post("/embeddings", json={"model": "${EMBED_MODEL_ID}", "input": texts})
        response.raise_for_status()
        rows = sorted(response.json()["data"], key=lambda row: row["index"])
        return [row["embedding"] for row in rows]

def best(query: str, passages: list[str], top_n: int = 3) -> list[tuple[float, str]]:
    with client() as api:
        response = api.post("/rerank", json={
            "model": "${RERANK_MODEL_ID}",
            "query": query,
            "documents": passages,
            "top_n": top_n,
        })
        response.raise_for_status()
        return [(r["relevance_score"], passages[r["index"]]) for r in response.json()["results"]]
~~~

Embed your passages once and store the vectors; at query time, find candidates
by cosine similarity, then rerank the top few dozen. See
[embeddings](/docs/embeddings) and [rerank](/docs/rerank).
`;
const S_SPEECH_TO_TEXT = `
## Speech to text

~~~python
def transcribe(path: str, mime: str = "audio/mp4") -> str:
    with client() as api, open(path, "rb") as audio:
        response = api.post(
            "/audio/transcriptions",
            files={"file": (os.path.basename(path), audio, mime)},
            data={"model": "${WHISPER_MODEL_ID}"},
        )
        response.raise_for_status()
        return response.json()["text"]
~~~

Leave \`language\` unset unless every clip is in one known language: forcing it
translates. Clips are at most 300 seconds. See
[audio transcriptions](/docs/audio-transcriptions).
`;
const S_A_VERY_LONG_ANSWER = `
## A very long answer

~~~python
import uuid

def write_book(api: httpx.Client, brief: str) -> str:
    body = api.post(
        "/responses",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "model": MODEL,
            "input": brief,
            "max_output_tokens": 1_000_000,
            "background": True,
        },
    ).json()
    print("planned max_output_tokens:", body["max_output_tokens"])
    return body["id"]        # poll slowly, or wait for the webhook
~~~

A million tokens takes hours: only background or streaming suit it. The
finished response's \`max_output_tokens\` is the ceiling applied, and
\`incomplete_details\` says whether the answer reached it. See
[long outputs](/docs/long-output).
`;
const S_USING_AN_OPENAI_SHAPED_CLIENT = `
## Using an OpenAI-shaped client

If you already have one, point it at \`${API_BASE_URL}\` and use
[\`/v1/chat/completions\`](/docs/chat-completions). Expect to remove
parameters this platform does not accept — they are rejected rather than
ignored, and the error names the field. [Migration](/docs/migration) has the
details.
`;

// ------------------------------------------------ after the no-timeout release --

const LATER_A_CLIENT = `
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
        # No read timeout: the API sends a byte at least every 15 seconds for
        # as long as a request runs, and a request may run for hours. A
        # connect timeout still catches a network that is not there.
        timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0),
    )
~~~
`;
const LATER_ERRORS_AND_RETRIES = `
## Errors and retries

~~~python
import random
import time

RETRYABLE = {
    # quota_exceeded and concurrency_limit_exceeded arrive only if an operator
    # enables limits; listing them costs nothing.
    "rate_limit_error", "quota_exceeded", "concurrency_limit_exceeded",
    "model_recovering", "model_unavailable",
}

class TechSaraError(RuntimeError):
    def __init__(self, body: dict):
        error = body.get("error", {})
        self.code = error.get("code", "")
        self.request_id = error.get("request_id", "")
        super().__init__(f"{self.code}: {error.get('message', '')} [{self.request_id}]")

def post_with_retry(api: httpx.Client, path: str, payload: dict, key: str, *, give_up_after_s: float = 600):
    deadline = time.monotonic() + give_up_after_s
    attempt = 0
    while True:
        try:
            response = api.post(path, json=payload, headers={"Idempotency-Key": key})
        except httpx.TransportError:
            response = None                       # dropped: retry with the same key
        if response is not None and response.is_success:
            body = response.json()
            if body.get("status") == "failed":    # a 200 can carry a failure
                raise TechSaraError(body)
            return body
        if response is not None and response.status_code not in (502, 524, 530):
            body = response.json()
            final = response.headers.get("x-should-retry") == "false"
            if final or body.get("error", {}).get("code", "") not in RETRYABLE:
                raise TechSaraError(body)
        if time.monotonic() > deadline:
            raise TimeoutError(f"gave up on {path} after {give_up_after_s:.0f} s")
        wait = float(response.headers.get("Retry-After", 0)) if response is not None else 0
        time.sleep(max(wait, min(60, 2 ** attempt)) + random.uniform(0, 1))
        attempt += 1
~~~

Always keep \`request_id\`. It is the one thing that lets somebody find your
request without you sending anything sensitive. The same \`key\` on every
attempt is what makes the retry safe: a retry of a request that is still running
joins it. See [errors](/docs/errors#what-to-retry).
`;
const LATER_SPEECH_TO_TEXT = `
## Speech to text

~~~python
def transcribe(path: str, mime: str = "audio/mp4") -> str:
    with client() as api, open(path, "rb") as audio:
        response = api.post(
            "/audio/transcriptions",
            files={"file": (os.path.basename(path), audio, mime)},
            data={"model": "${WHISPER_MODEL_ID}"},
        )
        response.raise_for_status()
        return response.json()["text"]
~~~

Leave \`language\` unset unless every clip is in one known language: forcing it
translates. There is no limit on how long a recording is; one request carries a
file of up to 89 MiB. See [audio transcriptions](/docs/audio-transcriptions).
`;
const LATER_A_VERY_LONG_ANSWER = `
## A very long answer

~~~python
import uuid

def write_book(api: httpx.Client, brief: str) -> str:
    body = api.post(
        "/responses",
        headers={"Idempotency-Key": str(uuid.uuid4())},
        json={
            "model": MODEL,
            "input": brief,
            "max_output_tokens": 1_000_000,
            "background": True,
        },
    ).json()
    print("planned max_output_tokens:", body["max_output_tokens"])
    return body["id"]        # poll slowly, or wait for the webhook
~~~

A million tokens takes hours, and any mode will wait that long: there is no
wall clock. Background suits work nothing is watching; a stream suits work a
person is reading. The finished response's \`max_output_tokens\` is the ceiling
applied, and \`incomplete_details\` says whether the answer reached it. See
[long outputs](/docs/long-output).
`;
const LATER_USING_AN_OPENAI_SHAPED_CLIENT = `
## With the \`openai\` package

The \`openai\` package speaks this API with a base URL and a key. Two settings
decide whether a long request finishes:

~~~python
import os
import uuid
from openai import OpenAI, Timeout

client = OpenAI(
    base_url="${API_BASE_URL}",
    api_key=os.environ["TECHSARA_API_KEY"],
    timeout=Timeout(None, connect=10.0),
    max_retries=5,
)

stream = client.responses.create(
    model="${MODEL_ID}",
    input="Explain retrieval-augmented generation.",
    stream=True,
    extra_headers={"Idempotency-Key": str(uuid.uuid4())},
)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
~~~

\`timeout=Timeout(None, connect=10.0)\` turns the read timeout off — use
\`None\`, never \`0\`, which fails every call at once. Pass the
\`Idempotency-Key\` per call on generations, not as a default header: the
embeddings, rerank and transcription endpoints refuse it. A stream that breaks
is picked up with \`client.responses.retrieve(response_id, stream=True,
starting_after=last_seq)\` — [timeouts](/docs/timeouts#resuming-a-stream) has the
loop.

## Using an OpenAI-shaped client

If you already have one, point it at \`${API_BASE_URL}\` and use
[\`/v1/chat/completions\`](/docs/chat-completions) or
[\`/v1/responses\`](/docs/responses). Expect to remove parameters this platform
does not accept — they are rejected rather than ignored, and the error names the
field. [Migration](/docs/migration) has the details.
`;

/** The page before (`noTimeout: false`) or after the no-timeout release. */
export function pythonPage({
  noTimeout,
  sidecarsLive = SIDECARS_NO_TIMEOUT_LIVE,
}: {
  noTimeout: boolean;
  sidecarsLive?: boolean;
}): DocPage {
  const sections = noTimeout
    ? [INTRO, LATER_A_CLIENT, S_ONE_ANSWER, S_A_CONVERSATION, S_STREAMING, LATER_ERRORS_AND_RETRIES, S_IDEMPOTENT_AND_BACKGROUND, S_AN_IMAGE, S_SEARCH_EMBED_THEN_RERANK, LATER_SPEECH_TO_TEXT, LATER_A_VERY_LONG_ANSWER, LATER_USING_AN_OPENAI_SHAPED_CLIENT]
    : [INTRO, S_A_CLIENT, S_ONE_ANSWER, S_A_CONVERSATION, S_STREAMING, S_ERRORS_AND_RETRIES, S_IDEMPOTENT_AND_BACKGROUND, S_AN_IMAGE, S_SEARCH_EMBED_THEN_RERANK, sidecarsLive ? LATER_SPEECH_TO_TEXT : S_SPEECH_TO_TEXT, S_A_VERY_LONG_ANSWER, S_USING_AN_OPENAI_SHAPED_CLIENT];
  return {
    slug: 'python',
    title: 'Python',
    summary:
      'A small client over httpx: requests, streaming, retries, idempotency ' +
      'and background work.',
    section: 'Examples',
    examples: EXAMPLE_STATUS,
    body: sections
      .map((section) => section.trim())
      .filter((section) => section !== '')
      .join('\n\n'),
  };
}

export const python: DocPage = pythonPage({ noTimeout: NO_TIMEOUT_LIVE });
