import type { DocPage } from '../types';
import { CONSOLE_PATH, EXAMPLE_STATUS } from '../samples';

export const security: DocPage = {
  slug: 'security',
  title: 'Security best practice',
  summary:
    'What you should do with a key, what the platform does on its side, and ' +
    'where the boundary between the two is.',
  section: 'Reference',
  examples: EXAMPLE_STATUS,
  body: `
## Your side

### Credentials

* Keep keys in a secret manager or an environment variable on a machine you
  control. Never in source, a browser bundle, a mobile app, a CI log, a
  screenshot or a chat message.
* One key per deployed service, so revoking one does not stop the others.
* Least scope: give a key only what its job needs. See
  [scopes](/docs/authentication#scopes).
* Test keys for test systems; live keys only where live work happens.
* Rotate on a schedule, and revoke — not rotate — the moment you suspect a
  leak. [API-key security](/docs/key-security) has the full lifecycle.

### Narrow the blast radius

A project's settings are the cheapest security you will ever configure:

| Setting | Turns a stolen key into |
| --- | --- |
| Model allowlist | A key that can only reach the models you named. |
| Origin allowlist | A key that browsers can only use from your own site. |
| IP allowlist | A key that only works from your own servers. |
| Retention window | A background output that stops existing sooner. |

### In your application

* **Treat model output as untrusted input.** It is text from a generator, not
  a fact from a database: validate it, escape it where it is rendered, and
  never pass it into a shell, a query or an action without a check.
* **Do not send what you do not need to send.** Strip personal data from a
  prompt when the task does not need it.
* **Verify webhook signatures**, in constant time, with the timestamp
  tolerance. See [webhooks](/docs/webhooks#verifying-the-signature).
* **Log \`request_id\`, never the key.** The public half of a key
  (\`tsk_live_<public id>\`) is safe to log; the rest is the credential.
* **Handle \`503\` deliberately** rather than retrying in a tight loop:
  honour \`Retry-After\` and add jitter. See
  [rate limits](/docs/rate-limits#backing-off-on-503).
* **Watch your usage.** The API enforces no request, token or daily limits,
  so a stolen key's abuse has no ceiling — revoke it and read the
  [usage](/docs/usage) to see what it spent.

## Our side

So you know where the boundary is, and what you can rely on:

* **Keys are stored as keyed digests** under a server-side pepper, compared
  in constant time. The plaintext exists once, at creation.
* **Revocation is immediate everywhere.** The key row is read on every
  request; nothing is cached.
* **\`/v1\` is cookie-blind.** It reads the \`Authorization\` header and
  nothing else, and never sends
  \`Access-Control-Allow-Credentials\` — so no browser can drive it with a
  signed-in person's session.
* **Prompts and generated text are not stored by default.** Request logs keep
  metadata only. The exception is a [background
  response](/docs/background), whose output is kept for your project's
  retention window so that you can fetch it.
* **Error bodies carry no machinery.** No traceback, SQL, environment value,
  container name, internal hostname, filesystem path or private IP address.
* **Model exposure is decided in code**, and configuration may only narrow
  it. Internal services — the router, embeddings, OCR, the reranker — are not
  models, are not in the registry, and have no reachable path from \`/v1\`.
* **Webhook deliveries are SSRF-checked**: HTTPS only, the resolved address
  validated and then connected to, private and metadata addresses refused, at
  most three re-validated redirects.
* **Key creation, rotation and revocation are audited**, along with project
  and webhook changes.

## Reporting a problem

Tell your workspace administrator — they hold the console at
[\`${CONSOLE_PATH}\`](${CONSOLE_PATH}) and the audit trail. Include the
\`X-Request-Id\` values and the public half of any key involved. Never send a
whole key, to us or to anyone.

If you believe a response body leaked something it should not have, treat
that as a security report rather than a bug report, and say so.
`.trim(),
};
