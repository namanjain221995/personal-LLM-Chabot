import type { DocPage } from '../types';
import { MODEL_ID, EXAMPLE_STATUS } from '../samples';

export const tools: DocPage = {
  slug: 'tools',
  title: 'Tool calling',
  summary:
    'Not offered on this platform yet. What that means for your code, and ' +
    'what to do instead.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
## Tool calling is not available

The TechSara developer API does **not** offer tool calling, function calling
or structured tool use. \`GET /v1/models\` says so in the only place that
counts:

~~~json
{
  "id": "${MODEL_ID}",
  "capabilities": { "chat": true, "streaming": true, "vision": true, "tools": false, "embeddings": false }
}
~~~

\`"tools": false\` is the answer. There is no flag, no header and no
allowlist entry that turns it on, and no endpoint that accepts a tool
definition.

## What happens if you send tools anyway

A \`400\`, naming the field:

~~~json
{
  "error": {
    "message": "Extra inputs are not permitted",
    "type": "invalid_request_error",
    "code": "invalid_request_error",
    "param": "tools",
    "request_id": "req_…"
  }
}
~~~

The same goes for \`tool_choice\`, \`functions\` and \`function_call\`.
Rejecting them is the point: a request that was accepted and then ignored
would leave you waiting for a tool call that can never arrive, with no way to
tell whether the model chose not to use your tool or the platform threw it
away.

## Why it is not here

The underlying model can call tools; the *platform* does not expose it. Tool
calling is a new category of behaviour on a public API — it changes what a
request can cause, and therefore needs its own scope, its own limits, its own
audit trail and its own threat review. Shipping the field before that work
would advertise a capability the endpoint could not safely honour.

When it arrives it will be a change to the developer-platform contract, a new
version of this page, and an entry in the [changelog](/docs/changelog) — not
a quiet flag flip.

## What to do instead

**Ask for JSON and validate it yourself.** The model follows an explicit
instruction well, and your own validation is a boundary you control:

~~~python
instructions = (
    "Reply with a single JSON object and nothing else. "
    'Schema: {"intent": "refund"|"question"|"complaint", "confidence": 0.0-1.0}'
)

body = api.post("/responses", json={
    "model": "${MODEL_ID}",
    "instructions": instructions,
    "input": customer_message,
    "temperature": 0,
}).json()

text = body["output"][0]["content"][0]["text"]

try:
    parsed = json.loads(text)
except json.JSONDecodeError:
    # Treat a non-JSON answer as a failure of THIS call, not as data. A model
    # that returned prose is telling you the prompt was ambiguous.
    parsed = None
~~~

Two rules make this reliable in production:

1. **Validate before you use it.** Parse, check the fields you require, and
   reject anything else. Never pass model output into an action without a
   schema check in between.
2. **Keep the model out of the doing.** Let it decide *what* to do and let
   your code decide *whether* to do it. That separation is the one you would
   want even with native tool calling.

**Chain your own calls.** Run the model, act on the result in your own code,
and call the model again with what you found. It is more round trips and
entirely under your control.

## Neither are these

For completeness, so nothing here is a surprise: there is no embeddings
endpoint, no image input (a message's \`content\` is a string), no file
upload, no retrieval or web search, and no assistants or threads. See
[the model reference](/docs/models) for what the one public model does and
does not accept.
`.trim(),
};
