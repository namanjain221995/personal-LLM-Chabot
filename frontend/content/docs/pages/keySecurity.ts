import type { DocPage } from '../types';
import { CONSOLE_PATH, EXAMPLE_LIVE_KEY, EXAMPLE_STATUS } from '../samples';

export const keySecurity: DocPage = {
  slug: 'key-security',
  title: 'API-key security',
  summary:
    'How a TechSara key is built, how it is stored, how to rotate one, and ' +
    'what to do the moment you think one has leaked.',
  section: 'Getting started',
  examples: EXAMPLE_STATUS,
  body: `
A TechSara API key is a bearer credential: whoever holds it is the caller.
Everything below exists to make that sentence survivable — to make a leaked
key easy to spot, cheap to replace, and limited in what it could do while it
was out.

## Anatomy

~~~text
tsk_live_0123456789abcdef_EXAMPLE_KEY_DO_NOT_USE_THIS_IS_NOT_A_SECRETEXAMPL
└─┬─┘ └┬─┘ └──────┬─────┘ └───────────────────┬────────────────────┘└──┬──┘
 tsk  live    public id                     secret                  checksum
~~~

| Part | What it is |
| --- | --- |
| \`tsk\` | The product prefix. Fixed, greppable, and what makes a leaked key findable by an automated scanner at all. |
| \`live\` / \`test\` | The environment. In the prefix, not hidden in the random part, so a human and a scanner can both tell at a glance which one leaked. |
| public id | 16 hex characters. The lookup handle. Stored in clear and safe to log. |
| secret | 43 characters, 256 bits from a cryptographic random source. |
| checksum | 6 characters, Base62 of a CRC32 over the public id and the secret. |

The checksum is an integrity check against a fumbled copy-and-paste, not a
signature. It is verified **offline**: a key with a bad checksum is rejected
before a single database row is read, which is why a truncated key gives you
an immediate \`401\` rather than a slow one.

The example key above is exactly that case. Its shape is perfect and its
checksum is the literal text \`EXAMPL\`, so it can never authenticate
anything.

## How we store it

* The secret is hashed with a keyed digest — HMAC-SHA256 under a
  server-side pepper — and compared in constant time. The pepper is what
  stops an attacker who has only the database table.
* The **plaintext is never stored, logged, returned or displayed after
  creation**. Not in an audit row, not in an error, not in a support ticket.
* \`last_four\` is kept so you can recognise which key a service is using.
  Those four characters come from the checksum, not from the secret.
* A log line that must name a key names its addressable half only:
  \`tsk_live_0123456789abcdef_<redacted>\`.

## The rules that actually keep a key safe

1. **Server-side only.** A key belongs in an environment variable or a secret
   manager on a machine you control. Not in a browser bundle, a mobile app, a
   repository, a CI log, a screenshot, or a support message.
2. **One key per deployed service.** Revoking the payments worker's key
   should not take down the support bot.
3. **Least scope.** Give a key the scopes its job needs and no others. See
   [scopes](/docs/authentication#scopes).
4. **Narrow the project.** A model allowlist, an origin allowlist and an IP
   allowlist each turn a stolen key into a key that only works from somewhere
   you named.
5. **Test keys for test systems.** A \`tsk_test_\` key in a staging config is
   a leak that costs nothing.

## Expiry and rotation

New keys are created with an expiry — 90 days unless whoever creates it
chooses otherwise (anything from 1 to 365 days). Rotation only bounds the
useful life of a stolen key if the key has a life to bound.

**Rotation** mints the replacement and leaves the old key working for a grace
window — 7 days by default, 30 days at the very most, chosen when you rotate
— so you can redeploy every consumer and watch the old key's "last used" stop
moving before the window shuts. When it shuts the old key is refused
everywhere, with no further action from you. A key that is already inside a
rotation window cannot be rotated again until it ends; revoke it instead.

~~~text
rotate  ──►  new key issued, old key still valid for the overlap
            deploy the new key everywhere
            watch the old key's last-used stop moving
        ──►  the window closes, or you revoke the old key early
~~~

Both keys are recorded against the same project while they overlap — usage
is per project, not per key — and the API enforces no usage limits, so the
window costs nothing extra.

A rotation with an overlap of **zero** is the compromise case: the old key is
revoked in the same step, and nothing it sends is accepted afterwards.

**Revocation** is immediate by design. A compromise is not a planned
rotation: the old key is dead the moment the row is written, and it is dead
everywhere at once, because the key is re-read from the database on every
single request. Nothing is cached.

## If a key leaks

1. Revoke it in the console at [\`${CONSOLE_PATH}\`](${CONSOLE_PATH}). Do this
   first; everything else can wait.
2. Issue a replacement and deploy it.
3. Read the project's usage and request log for the exposure window: time,
   model, status, token counts, and the key each request used.
4. If the leak could also have exposed a webhook endpoint's signing secret,
   replace that endpoint: create a new one, move your consumer to its secret,
   then delete the old one. The console does not rotate a signing secret in
   place today.

Revocation takes effect immediately. Do not wait for an expiry that is
90 days away.

## What the platform will never do

* Return a key's secret after creation, by any route, to anyone.
* Put a secret, a pepper, a password or a session token in an API response,
  an error message or a webhook payload.
* Include a traceback, a SQL statement, an environment value, a container
  name, an internal hostname, a filesystem path or a private IP address in
  any \`/v1\` response body.

If you ever see one of those, treat it as a security report and say so.

## Reporting

Tell your workspace administrator, who has the console access and the audit
trail. Include the \`X-Request-Id\` values you have and the public half of the
key (\`${EXAMPLE_LIVE_KEY.slice(0, 25)}…\`) — never the whole key.
`.trim(),
};
