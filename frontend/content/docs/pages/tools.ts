import type { DocPage } from '../types';
import { API_BASE_URL, MODEL_ID, EXAMPLE_STATUS, EXAMPLE_RESPONSE_ID } from '../samples';

// 2026-11-20: tool calling is live on /v1 (CONTRACT §8.8). This page replaced
// "Tool calling is not available". Everything it states is in §8.8 or §10.2;
// tests/docs-site.test.tsx holds the page to the registry, which advertises
// `tools` on the main model only, and scripts/docs_examples_run.py holds one
// execution spec per fenced block below, in this order.

export const tools: DocPage = {
  slug: 'tools',
  title: 'Tool calling',
  summary:
    'Your own functions, called by the model and run by you — and web search, ' +
    'page reading and deep research, run by the server.',
  section: 'API reference',
  examples: EXAMPLE_STATUS,
  body: `
## What there is

Tool calling works on \`POST /v1/responses\` and \`POST /v1/chat/completions\`,
on \`${MODEL_ID}\` only. \`GET /v1/models\` says so: its \`capabilities.tools\`
is \`true\`, and \`false\` on every other model. Sending \`tools\` to any other
model is a \`400\` naming \`tools\`.

There are two kinds of tool:

| \`type\` | Who runs it | What you get back |
| --- | --- | --- |
| \`function\` | **You.** The definition is yours; the model asks for a call. | The call: \`tool_calls\` and \`finish_reason: "tool_calls"\` on Chat Completions, \`function_call\` output items on Responses. You run it and send the result in the next request. |
| \`web_search\` | The server: the chat application's own search, reranker and page reader. \`web_search_preview\` is accepted as the same tool. | The answer, with markdown links to the sources it used. |
| \`web_fetch\` | The server: one public \`http(s)\` page or PDF, read through the same address guard as every server-side fetch, and only where the site's robots.txt allows it. | The answer. |
| \`deep_research\` | The server: the chat application's research engine — plan, search, read, verify, report. Takes minutes. | The report as the answer, followed by a \`## Sources\` list for its \`[n]\` citations. |

No scope beyond \`responses.write\` is needed.

## Your own functions on Chat Completions

Describe the function; the model decides whether to call it:

~~~bash
export TECHSARA_API_KEY="tsk_live_…"   # your key, from the console

curl ${API_BASE_URL}/chat/completions \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "messages": [{"role": "user", "content": "Where is order 1182?"}],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_order_status",
        "description": "Look up the delivery status of one order.",
        "parameters": {
          "type": "object",
          "properties": {"order_id": {"type": "string"}},
          "required": ["order_id"]
        }
      }
    }]
  }'
~~~

When it calls, generation stops there. The message carries the calls and
\`finish_reason\` is \`tool_calls\`:

~~~json
{
  "id": "chatcmpl_…",
  "object": "chat.completion",
  "created": 1789200000,
  "model": "${MODEL_ID}",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_0123456789abcdef01234567",
            "type": "function",
            "function": { "name": "get_order_status", "arguments": "{\\"order_id\\": \\"1182\\"}" }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ],
  "max_output_tokens": 8192,
  "usage": { "prompt_tokens": 182, "completion_tokens": 21, "total_tokens": 203 }
}
~~~

Each call has an id of the form \`call_\` and 24 hex characters. \`content\` is
the text the model wrote with the calls, or \`null\`. \`arguments\` is the JSON
string the model wrote — **the server does not check it against your
\`parameters\`**, so parse and validate it before you act on it.

Run the function, then send the conversation back: the assistant message as
you received it, and one \`tool\` message per call, carrying its id:

~~~bash
curl ${API_BASE_URL}/chat/completions \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "messages": [
      {"role": "user", "content": "Where is order 1182?"},
      {"role": "assistant", "content": null, "tool_calls": [{
        "id": "call_0123456789abcdef01234567",
        "type": "function",
        "function": {"name": "get_order_status", "arguments": "{\\"order_id\\": \\"1182\\"}"}
      }]},
      {"role": "tool", "tool_call_id": "call_0123456789abcdef01234567",
       "content": "{\\"status\\": \\"shipped\\", \\"eta\\": \\"2026-11-22\\"}"}
    ],
    "tools": [{
      "type": "function",
      "function": {
        "name": "get_order_status",
        "description": "Look up the delivery status of one order.",
        "parameters": {
          "type": "object",
          "properties": {"order_id": {"type": "string"}},
          "required": ["order_id"]
        }
      }
    }]
  }'
~~~

The answer comes back as ordinary text. The same round trip with the
\`openai\` package, looping until the model stops asking:

~~~python
import json
import os
from openai import OpenAI, Timeout

client = OpenAI(
    base_url="${API_BASE_URL}",
    api_key=os.environ["TECHSARA_API_KEY"],
    timeout=Timeout(None, connect=10.0),
    max_retries=5,
)


def get_order_status(order_id: str) -> dict:
    # Your code: a database lookup, an internal API, anything.
    return {"order_id": order_id, "status": "shipped", "eta": "2026-11-22"}


tools = [{
    "type": "function",
    "function": {
        "name": "get_order_status",
        "description": "Look up the delivery status of one order.",
        "parameters": {
            "type": "object",
            "properties": {"order_id": {"type": "string"}},
            "required": ["order_id"],
        },
    },
}]
messages = [{"role": "user", "content": "Where is order 1182?"}]

completion = client.chat.completions.create(model="${MODEL_ID}", messages=messages, tools=tools)
while completion.choices[0].finish_reason == "tool_calls":
    message = completion.choices[0].message
    messages.append(message)  # the assistant turn, echoed back as received
    for call in message.tool_calls:
        args = json.loads(call.function.arguments)  # validate before you act
        result = get_order_status(str(args["order_id"]))
        messages.append({"role": "tool", "tool_call_id": call.id, "content": json.dumps(result)})
    completion = client.chat.completions.create(model="${MODEL_ID}", messages=messages, tools=tools)

print(completion.choices[0].message.content)
~~~

\`tool_calls\` is a list: answer every call in it before the next request.
\`parallel_tool_calls\`, a boolean, is passed to the model as you send it.

## Your own functions on Responses

The same function in the Responses spelling — \`name\`, \`description\` and
\`parameters\` sit next to \`type\`, not inside a \`function\` object:

~~~bash
curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "Where is order 1182?",
    "tools": [{
      "type": "function",
      "name": "get_order_status",
      "description": "Look up the delivery status of one order.",
      "parameters": {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"]
      }
    }]
  }'
~~~

A call is a \`function_call\` item in \`output\` — after the assistant message
when the model also wrote text, alone when it did not:

~~~json
{
  "id": "${EXAMPLE_RESPONSE_ID}",
  "object": "response",
  "created_at": 1789200000,
  "status": "completed",
  "model": "${MODEL_ID}",
  "output": [
    {
      "type": "function_call",
      "id": "fc_0123456789abcdef01234567",
      "call_id": "call_0123456789abcdef01234567",
      "name": "get_order_status",
      "arguments": "{\\"order_id\\": \\"1182\\"}",
      "status": "completed"
    }
  ],
  "max_output_tokens": 8192,
  "incomplete_details": null,
  "usage": { "input_tokens": 176, "output_tokens": 21, "total_tokens": 197 }
}
~~~

The response is \`completed\`: running your function is not part of it. Send
the call back, followed by its result as a \`function_call_output\` item with
the same \`call_id\`. A \`function_call_output\` whose \`call_id\` matches no
\`function_call\` earlier in \`input\` is a \`400\`.

~~~bash
curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": [
      {"role": "user", "content": "Where is order 1182?"},
      {"type": "function_call", "call_id": "call_0123456789abcdef01234567",
       "name": "get_order_status", "arguments": "{\\"order_id\\": \\"1182\\"}"},
      {"type": "function_call_output", "call_id": "call_0123456789abcdef01234567",
       "output": "{\\"status\\": \\"shipped\\", \\"eta\\": \\"2026-11-22\\"}"}
    ],
    "tools": [{
      "type": "function",
      "name": "get_order_status",
      "description": "Look up the delivery status of one order.",
      "parameters": {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"]
      }
    }]
  }'
~~~

With the \`openai\` package. A \`function_call\` item can go back into
\`input\` exactly as it arrived:

~~~python
import json
import os
from openai import OpenAI, Timeout

client = OpenAI(
    base_url="${API_BASE_URL}",
    api_key=os.environ["TECHSARA_API_KEY"],
    timeout=Timeout(None, connect=10.0),
    max_retries=5,
)


def get_order_status(order_id: str) -> dict:
    return {"order_id": order_id, "status": "shipped", "eta": "2026-11-22"}


tools = [{
    "type": "function",
    "name": "get_order_status",
    "description": "Look up the delivery status of one order.",
    "parameters": {
        "type": "object",
        "properties": {"order_id": {"type": "string"}},
        "required": ["order_id"],
    },
}]
items = [{"role": "user", "content": "Where is order 1182?"}]

response = client.responses.create(model="${MODEL_ID}", input=items, tools=tools)
calls = [item for item in response.output if item.type == "function_call"]
while calls:
    for call in calls:
        items.append(call)  # the call, echoed back as received
        args = json.loads(call.arguments)  # validate before you act
        result = get_order_status(str(args["order_id"]))
        items.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result)})
    response = client.responses.create(model="${MODEL_ID}", input=items, tools=tools)
    calls = [item for item in response.output if item.type == "function_call"]

print(response.output_text)
~~~

## Function calls in a stream

On **Responses**, each call arrives whole in one
\`response.function_call_arguments.done\` event — \`item_id\`,
\`output_index\`, \`call_id\`, \`name\`, \`arguments\` — after the message (if
there is one) closes and before the terminal event, whose \`output\` carries the
same \`function_call\` items. A turn that is only function calls has no message,
so its item and content-part events are absent:

~~~text
event: response.created
data: {"type":"response.created","sequence_number":1,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"queued","model":"${MODEL_ID}","output":[],"max_output_tokens":8192,"incomplete_details":null,"usage":null}}

event: response.in_progress
data: {"type":"response.in_progress","sequence_number":2,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"in_progress","model":"${MODEL_ID}","output":[],"max_output_tokens":8192,"incomplete_details":null,"usage":null}}

event: response.function_call_arguments.done
data: {"type":"response.function_call_arguments.done","sequence_number":3,"item_id":"fc_0123456789abcdef01234567","output_index":0,"call_id":"call_0123456789abcdef01234567","name":"get_order_status","arguments":"{\\"order_id\\": \\"1182\\"}"}

event: response.completed
data: {"type":"response.completed","sequence_number":4,"response":{"id":"${EXAMPLE_RESPONSE_ID}","object":"response","created_at":1789200000,"status":"completed","model":"${MODEL_ID}","output":[{"type":"function_call","id":"fc_0123456789abcdef01234567","call_id":"call_0123456789abcdef01234567","name":"get_order_status","arguments":"{\\"order_id\\": \\"1182\\"}","status":"completed"}],"max_output_tokens":8192,"incomplete_details":null,"usage":{"input_tokens":176,"output_tokens":21,"total_tokens":197}}}
~~~

On **Chat Completions**, one chunk carries every call whole in
\`delta.tool_calls\`, each with its \`index\`, and the next chunk carries
\`finish_reason: "tool_calls"\`. A client that accumulates argument fragments by
\`index\` reads it unchanged.

## Tools the server runs

Name the tool by its \`type\` and nothing else. When the model calls it, the
server runs it, gives the result to the model and calls the model again; you
receive the finished answer. A server tool takes no field but \`type\`: an
option this API would not honour — \`search_context_size\`, \`user_location\`,
\`filters\` and the like — is a \`400\` naming it.

### Web search

~~~bash
curl ${API_BASE_URL}/responses \\
  -H "Authorization: Bearer $TECHSARA_API_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "${MODEL_ID}",
    "input": "What changed in the most recent stable Python release? Link your sources.",
    "tools": [{"type": "web_search"}]
  }'
~~~

The answer links the pages it used. A search that finds nothing, or a search
that cannot run, is told to the model in a fixed sentence, and the request still
completes with an answer.

### Reading one page

\`web_fetch\` reads one public \`http(s)\` page or PDF. It reads **only an
address that appears in the conversation**: one you wrote in a message, or one
a search result of the same request listed. An address the model made up is
refused, so text on a page cannot make the model send your prompt somewhere:

~~~python
import os
from openai import OpenAI, Timeout

client = OpenAI(
    base_url="${API_BASE_URL}",
    api_key=os.environ["TECHSARA_API_KEY"],
    timeout=Timeout(None, connect=10.0),
    max_retries=5,
)

completion = client.chat.completions.create(
    model="${MODEL_ID}",
    messages=[{"role": "user", "content": "Summarise https://www.python.org/about/ in three sentences."}],
    tools=[{"type": "web_fetch"}],
)
print(completion.choices[0].message.content)
~~~

An address that is not public, a page that cannot be reached, a robots.txt
refusal, or a page or search that takes longer than 45 seconds is told to the
model in the same way; the request still completes.

### Deep research

\`deep_research\` runs the research engine, and its report **is** the answer:
the model is not called again after it. A run takes minutes, so use
\`"stream": true\` — the stream carries heartbeats while the research works —
or \`"background": true\` and [collect the result later](/docs/background):

~~~python
import os
from openai import OpenAI, Timeout

client = OpenAI(
    base_url="${API_BASE_URL}",
    api_key=os.environ["TECHSARA_API_KEY"],
    timeout=Timeout(None, connect=10.0),
    max_retries=5,
)

stream = client.responses.create(
    model="${MODEL_ID}",
    input="Compare the main approaches to vector search for a catalogue of ten million products.",
    tools=[{"type": "deep_research"}],
    stream=True,
)
for event in stream:
    if event.type == "response.output_text.delta":
        print(event.delta, end="", flush=True)
    elif event.type == "response.failed":
        print("\\nfailed:", event.response.error.code)
~~~

API research runs share one place: **one at a time, across every key.** When
it is taken, the request fails with the retry-safe \`model_unavailable\` ("The
model is at capacity right now") and \`Retry-After: 60\` — a \`503\`, or, on a
stream that has already started, \`response.failed\` carrying that code. It is
an error rather than an answer saying the engine is busy, which you would have
been billed for.

### What happens to web content

Everything a server tool reads from the web reaches the model fenced off, under
a sentence that says it is reference material and **not instructions**. By
default a search hands the model at most 6 pages of 4,000 characters each, a
fetched page at most 40,000 characters, and no tool result is longer than
120,000.

All the tool results of one request together may add at most 300,000 bytes to
its prompt; when that is spent the model must answer with what it has.

The fence lowers the risk that text on a page steers the model; it does not
remove it. If you declare your own functions together with a server tool,
treat a returned call as possibly shaped by what the model read, and validate
its arguments before you act on it. A call to a function you did not declare
is never returned to you.

Nothing is stored: no search log, no page saved, no index of what you asked,
no memory lookup, no record of a research run. The log line for a tool run
names the tool, its outcome and its duration — never its arguments.

## Choosing a tool

\`tool_choice\` works on both endpoints and needs \`tools\`:

| Value | Meaning |
| --- | --- |
| \`"auto"\` | The default. The model decides. |
| \`"none"\` | The model does not call a tool. |
| \`"required"\` | The model calls a tool. |
| one tool | That tool. Responses: \`{"type": "function", "name": "get_order_status"}\`; Chat Completions: \`{"type": "function", "function": {"name": "get_order_status"}}\`; a server tool on either: \`{"type": "web_search"}\`. |

A named tool must be one of \`tools\`. A forced choice — \`"required"\` or one
tool — applies to the **first** model call of the request only.
\`parallel_tool_calls\` also needs \`tools\`.

## Rules and limits

**Definitions.** A function \`name\` is 1–64 letters, digits, \`_\` or \`-\`, and
may not be \`web_search\`, \`web_fetch\` or \`deep_research\`. \`parameters\` is a
JSON Schema of at most 64 KiB; \`description\` at most 4,096 characters. At most
64 tools, each function name and each server tool once. An empty \`tools\` list
is the same as none. Tool definitions and function-call items count towards the
input limit at their byte length.

**Bounded work.** At most **6 model calls** per request and **4 server tool
runs** per model call. The last permitted model call may not call a tool, so a
request always ends in an answer. \`usage\` sums every model call of the
request, including the ones deep research makes. While a server tool runs, a
stream carries heartbeats only.

**Not with JSON mode.** \`tools\` together with \`text.format\` (Responses) or
\`response_format\` (Chat Completions) is a \`400\` naming the JSON-mode field.

**Function tools and \`background\` do not mix.** A background request with a
\`function\` tool is a \`400\` naming \`background\`: nobody would be waiting to
run the function. Server tools run in the background like any other request.

**A request with a function tool is not durable**, whatever \`store\` says. It
answers \`X-TechSara-Run: none\`, cannot be
[resumed](/docs/timeouts#resuming-a-stream), and a retry is a new request.

**A run with tools is never continued** after an interruption — a restart of
the service, an engine recovery — because a tool that already ran would run
again. It fails with the retry-safe \`model_unavailable\`; retry it.

**Availability.** An operator can switch tool calling off for a deployment —
\`tools\` is then a \`400\` and \`capabilities.tools\` is \`false\` — or switch
off one server tool. \`web_search\` is refused where the deployment has no
search provider, and \`deep_research\` where research is switched off. Read
\`GET /v1/models\` rather than assuming.
`.trim(),
};
