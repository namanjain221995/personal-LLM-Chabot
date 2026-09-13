import type { DocPage } from '../types';
import { CONSOLE_PATH, EXAMPLE_RESPONSE_ID, MODEL_ID, EXAMPLE_STATUS } from '../samples';

export const webhooks: DocPage = {
  slug: 'webhooks',
  title: 'Webhooks',
  summary:
    'How a finished response reaches your server, how to verify the ' +
    'signature, and why your endpoint has to be publicly reachable HTTPS.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
A webhook endpoint belongs to a **project** and is configured by a person in
the developer console at [\`${CONSOLE_PATH}\`](${CONSOLE_PATH}), under the
\`api.webhooks.manage\` capability.

There is deliberately **no webhook scope and no webhook endpoint on
\`/v1\`**. Managing where our server will send requests is an administrative
act with an audit trail and a human behind it, not something a leaked machine
credential should be able to do.

## Events

| Event | When |
| --- | --- |
| \`response.completed\` | A response reached \`completed\`. |
| \`response.failed\` | A response reached \`failed\`. |
| \`response.cancelled\` | A response was cancelled. |

The console can also send a \`webhook.test\` delivery on demand. It is
deliberately **not** subscribable: a test event you could subscribe to would
eventually be relied on as a heartbeat, and then somebody's monitoring would
break on the day we stopped sending it.

## What a delivery looks like

~~~json
{
  "id": "evt_…",
  "object": "event",
  "type": "response.completed",
  "created_at": 1789200000,
  "data": {
    "response": {
      "id": "${EXAMPLE_RESPONSE_ID}",
      "object": "response",
      "status": "completed",
      "model": "${MODEL_ID}",
      "background": true,
      "created_at": 1789200000,
      "completed_at": 1789200037,
      "usage": { "input_tokens": 37, "output_tokens": 112, "total_tokens": 149 },
      "metadata": { "customer_request_id": "abc-123" }
    }
  }
}
~~~

The payload is built from an allowlist, field by field. It carries the
response id, its status, the model, the timings, the usage and **your own
\`metadata\`** — the object you set on the request, echoed back so you can
correlate the event with the job that asked for it without us inventing a
second id.

It does **not** carry your prompt or the generated text, unless the endpoint
has explicitly opted in to including output. A webhook travels to a server
whose logs, proxies and error trackers we know nothing about; the safe
default is that it tells you *what happened* and you fetch the content
yourself with [\`GET /v1/responses/{id}\`](/docs/responses#read-one-back).

A failed response also carries \`data.response.error\` with the same
\`code\` vocabulary as the [HTTP envelope](/docs/errors).

Every delivery has a unique \`id\` — the event id. Record it and ignore a
repeat: deliveries are retried, so a repeat is normal operation rather than a
fault.

## Verifying the signature

~~~http
TechSara-Signature: t=1789200000,v1=9f86d081884c7d65…
~~~

\`t\` is the unix second the payload was signed. \`v1\` is the lower-case hex
\`HMAC-SHA256\` of the bytes \`"<t>.<raw body>"\` under your endpoint's
signing secret.

Four rules, all of which matter:

1. **Sign the raw body**, exactly as received, before any JSON parse.
   \`json.loads\` followed by \`json.dumps\` does not round-trip byte for
   byte — key order, separators and unicode escapes all move — and a
   verifier that signs the re-serialised form fails in a way that is very
   hard to debug.
2. **Accept more than one \`v1\`.** During a secret rotation the header
   carries a digest per live secret (\`t=…,v1=…,v1=…\`) under one shared
   timestamp. The rule is "**at least one** \`v1\` matches", never "the first
   one".
3. **Compare in constant time, and do not short-circuit.** \`any()\` and a
   \`return\` on the first match leak, through timing, which secret and which
   digest matched. Accumulate instead.
4. **Check the timestamp**, ±5 minutes, in **both** directions. A far-future
   timestamp is as suspect as a stale one: from your side, "their clock is
   ahead" is indistinguishable from an attacker buying a replay window.

Unknown \`k=v\` pairs in the header are to be ignored rather than refused —
that is what the \`v1\` scheme label is for, so a future \`v2=\` beside it
does not break a verifier written today.

~~~python
import hmac
import time
from hashlib import sha256

TOLERANCE_S = 300

def verify(raw_body: bytes, header: str, *secrets: str) -> bool:
    """True only for a delivery we signed, recently. Never raises."""
    timestamp = None
    digests = []
    for chunk in str(header or "").split(","):
        key, separator, value = chunk.strip().partition("=")
        if not separator:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "t" and timestamp is None:
            try:
                timestamp = int(value)
            except ValueError:
                return False
        elif key == "v1" and value:
            digests.append(value.lower())
    if timestamp is None or not digests:
        return False
    if abs(int(time.time()) - timestamp) > TOLERANCE_S:
        return False

    signed = str(timestamp).encode("ascii") + b"." + raw_body
    matched = False
    for secret in secrets:
        expected = hmac.new(secret.encode("utf-8"), signed, sha256).hexdigest()
        for candidate in digests:
            # |= and not or/any: short-circuiting here would let the response
            # time say which secret matched.
            matched |= hmac.compare_digest(expected, candidate)
    return matched
~~~

~~~typescript
import { createHmac, timingSafeEqual } from "node:crypto";

const TOLERANCE_S = 300;

export function verify(rawBody: Buffer, header: string, ...secrets: string[]): boolean {
  let timestamp: number | null = null;
  const digests: string[] = [];

  for (const chunk of String(header ?? "").split(",")) {
    const at = chunk.indexOf("=");
    if (at < 0) continue;
    const key = chunk.slice(0, at).trim().toLowerCase();
    const value = chunk.slice(at + 1).trim();
    if (key === "t" && timestamp === null) {
      timestamp = Number(value);
      if (!Number.isInteger(timestamp)) return false;
    } else if (key === "v1" && value) {
      digests.push(value.toLowerCase());
    }
  }
  if (timestamp === null || digests.length === 0) return false;
  if (Math.abs(Math.floor(Date.now() / 1000) - timestamp) > TOLERANCE_S) return false;

  const signed = Buffer.concat([Buffer.from(\`\${timestamp}.\`), rawBody]);
  let matched = false;
  for (const secret of secrets) {
    const expected = createHmac("sha256", secret).update(signed).digest();
    for (const candidate of digests) {
      const given = Buffer.from(candidate, "hex");
      // No short-circuit, for the same timing reason as above.
      matched =
        (expected.length === given.length && timingSafeEqual(expected, given)) || matched;
    }
  }
  return matched;
}
~~~

Return \`400\` when verification fails, and do not look at the payload.

Store the signing secret the way you store an API key. It is shown once, in
the console, and is returned by no API.

### Rotating the secret

The signature header is built to carry more than one \`v1\`: when an
endpoint has a previous secret inside its overlap window, every delivery is
signed with **both**, which is why rule 2 above says to accept any match. The
console does not yet offer an in-place rotation that sets one up, though. To
change a secret today, create a second endpoint with the same URL and events,
deploy its secret to your verifier, then delete the old endpoint — for the
time both exist you receive each event twice, which the event id lets you
drop. A verifier written to rule 2 needs no change the day in-place rotation
arrives.

## Retries

A delivery that does not get a success is retried up to **6 attempts**. The
first retry waits about 10 seconds and each subsequent one doubles, with ±25%
jitter — roughly five minutes end to end, which covers a rolling restart of a
consumer without keeping a dead endpoint's queue alive for days. Every
attempt, status and error is recorded in the delivery history.

Bounded, never infinite. An endpoint that has been down for a week is not
helped by a queue that has been growing for a week.

What your endpoint owes us:

* answer **2xx quickly** — acknowledge first, work afterwards;
* be **idempotent** on the event id;
* stay **publicly reachable over HTTPS**.

Repeated failures raise the endpoint's consecutive-failure count, which the
console shows and which can disable an endpoint that has stopped existing.

## Why your URL has to be a real public HTTPS URL

Every delivery goes through an SSRF check, because a webhook is our server
making a request to an address you chose:

* **HTTPS only.** No plaintext.
* The hostname is **resolved first**, and the address is rejected if it is
  loopback, link-local, private, unique-local, an IPv4-mapped IPv6 address,
  or a cloud metadata address.
* We then **connect to the address we checked**, so DNS cannot be re-pointed
  between the check and the connection.
* At most **3 redirects**, each re-validated by the same rules.
* No credentials in the URL, a 10-second timeout, and the response body read
  and discarded to a small bound.

So \`https://localhost:9000/hook\`, \`http://10.0.0.5/hook\` and a hostname
that resolves to a private address are all refused. Use a public endpoint — a
tunnel is the usual answer while developing.

## Testing

The console sends a test delivery on demand and shows the delivery history
with attempt counts, HTTP statuses and errors. Build your verifier against a
test delivery before you rely on a real one.
`.trim(),
};
