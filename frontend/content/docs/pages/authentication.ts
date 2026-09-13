import type { DocPage } from '../types';
import {
  API_BASE_URL,
  EXAMPLE_LIVE_KEY,
  EXAMPLE_TEST_KEY,
  EXAMPLE_STATUS,
} from '../samples';

export const authentication: DocPage = {
  slug: 'authentication',
  title: 'Authentication',
  summary:
    'Every /v1 request carries an API key in the Authorization header — and ' +
    'nothing else identifies the caller.',
  section: 'Getting started',
  examples: EXAMPLE_STATUS,
  body: `
## The header

~~~http
Authorization: Bearer ${EXAMPLE_LIVE_KEY}
~~~

That is the entire authentication scheme. There is no query parameter, no
custom header, no signed request and no session.

**Cookies are ignored.** A browser session that is signed in to TechSara AI
grants nothing at all under \`/v1\`. This is deliberate: an endpoint that
accepted both a cookie and a key would be a confused deputy, drivable by any
page on the internet using a signed-in person's cookie. The API also never
sends \`Access-Control-Allow-Credentials\`, so no browser can attach cookies
to it even if it wanted to.

## What a key resolves to

A key is not just a credential; it is the whole of the request's identity.
Presenting one resolves, in order, before any work starts:

1. the key itself — malformed, unknown, revoked or expired;
2. the **service account** it belongs to — disabled;
3. the **project** — disabled;
4. the **workspace** — disabled;
5. the project's **IP allowlist** — the request came from somewhere else;
6. the **scopes** the endpoint requires — missing is \`403 insufficient_scope\`;
7. the project's **origin allowlist**, for a browser request — outside it is
   \`403 origin_not_allowed\`;
8. then the request itself, before any generation starts:
   * **rate, quota and concurrency** — over any of them is \`429\` with
     \`Retry-After\`, and every request that presents a valid key counts
     against the project's [rate limits](/docs/rate-limits), reads included;
   * the **model allowlist** — a model this key may not use is \`404\`, never
     \`403\`, so the API never confirms the existence of something you are
     not allowed to see;
   * **validation** of the body — \`400\` or \`413\`.

Rungs 1 to 5 all answer the **same** \`401 invalid_api_key\`, with the same
message. That is deliberate. A \`401\` that said "revoked", "expired" or
"project disabled" would tell whoever is holding a leaked key that the key is
real, and that somebody noticed.

Nothing in a request body can change any of that. There is no field for a
project, a workspace, a key, a model target, a limit or an audit setting, and
sending one is a \`400\` rather than something quietly ignored.

## Scopes

Scopes are what a key may *call*. They are flat, closed, and imply nothing:
\`responses.write\` does not grant \`responses.read\`, and neither implies
\`models.read\`. A key that needs two carries two. There is no hierarchy to
remember and none to get wrong.

| Scope | Grants | Endpoints |
| --- | --- | --- |
| \`models.read\` | List the models this key may use. | \`GET /v1/models\`, \`GET /v1/models/{model}\` |
| \`responses.read\` | Read responses created by this project. | \`GET /v1/responses/{id}\` |
| \`responses.write\` | Create and cancel responses. | \`POST /v1/responses\`, \`POST /v1/responses/{id}/cancel\`, \`POST /v1/chat/completions\` |
| \`usage.read\` | Read this project's usage counters. | \`GET /v1/usage\` |

That is the whole vocabulary — four scopes, and no fifth can be spelled. An
unrecognised scope string is an error when a key is created, not an entry
that is quietly dropped and then grants nothing.

Webhooks are **not** in this list. They are configured by a person in the
console, under the \`api.webhooks.manage\` capability, rather than by a
machine credential — see [webhooks](/docs/webhooks).

A key created without a choice gets \`models.read\`, \`responses.read\` and
\`responses.write\` — enough to call the API, and nothing more. Reading usage
is a separate job, usually for a separate credential.

Ask for less than you think you need. A key that can only do one thing is a
key whose leak has one consequence.

### When a scope is missing

~~~json
{
  "error": {
    "message": "The API key does not have the \`responses.write\` scope.",
    "type": "permission_error",
    "code": "insufficient_scope",
    "param": null,
    "request_id": "req_…"
  }
}
~~~

The response also carries the machine-readable form of the same sentence, a
\`WWW-Authenticate\` challenge naming the scope the endpoint requires:

~~~http
WWW-Authenticate: Bearer error="insufficient_scope", scope="responses.write"
~~~

It names what is *required*, never what your key happens to hold.

## Test and live

| Prefix | Meaning |
| --- | --- |
| \`${EXAMPLE_TEST_KEY.slice(0, 9)}…\` | A test-environment key. |
| \`${EXAMPLE_LIVE_KEY.slice(0, 9)}…\` | A live-environment key. |

The environment belongs to the project, and a key inherits it — it is not a
mode you can switch per request. Build against a test project; ship with a
live one.

## Calling from a browser

You can, but only with care, and usually you should not: putting a live key
in a browser publishes it to everyone who opens developer tools. The
supported pattern is a server of your own that holds the key and forwards
what your page is allowed to ask for.

If you do call \`/v1\` from a browser, two things are relevant:

* **Preflight** (\`OPTIONS /v1/…\`) is answered permissively — it carries no
  credential and reveals nothing — with \`GET, POST, OPTIONS\` and the
  \`authorization\`, \`content-type\` and \`idempotency-key\` headers allowed.
* **The real request is authorised.** If your project sets an origin
  allowlist, a request whose \`Origin\` is not on it is
  \`403 origin_not_allowed\`. An allowlist is worth setting: it is the
  difference between a stolen key working from anywhere and working from
  nowhere you did not name.

## Checking a key works

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl -i ${API_BASE_URL}/models \\
  -H "Authorization: Bearer $TECHSARA_API_KEY"
~~~

\`200\` with a model list means the key, the service account, the project and
the workspace are all live and the key holds \`models.read\`. The check is a
request like any other and counts against the project's requests per minute.
A \`401\` means the credential is not usable — see [errors](/docs/errors) for the
difference between that and a \`403\`.

Every response carries an \`X-Request-Id\`. Log it. It is what lets us find
your request without you having to send us anything sensitive.
`.trim(),
};
