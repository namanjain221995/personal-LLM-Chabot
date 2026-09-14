"""openai-python at DEFAULT settings against a base URL; prints one JSON line.

usage: sdk_client.py BASE_URL MODE MODEL
MODE: chat-sync | chat-stream | responses-stream | responses-sync
"""

import json
import sys
import time

import openai
from openai import OpenAI

base_url, mode, model = sys.argv[1], sys.argv[2], sys.argv[3]
client = OpenAI(api_key="sk-test-gateway", base_url=base_url)  # defaults: timeout 600 s, max_retries 2
out = {"sdk": f"python-{openai.__version__}", "mode": mode}
started = time.monotonic()
try:
    if mode == "chat-sync":
        r = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}])
        text = r.choices[0].message.content
    elif mode == "chat-stream":
        text = ""
        for chunk in client.chat.completions.create(model=model, messages=[{"role": "user", "content": "hi"}], stream=True):
            if chunk.choices and chunk.choices[0].delta.content:
                text += chunk.choices[0].delta.content
    elif mode == "responses-stream":
        text = ""
        seqs = []
        for event in client.responses.create(model=model, input="hi", stream=True):
            seqs.append(event.sequence_number)
            if event.type == "response.output_text.delta":
                text += event.delta
        out["seqs_contiguous"] = seqs == list(range(1, len(seqs) + 1))
    elif mode == "responses-sync":
        text = client.responses.create(model=model, input="hi").output_text
    else:
        raise SystemExit(f"unknown mode {mode}")
    out.update(ok=True, text=text)
except Exception as exc:  # report, do not raise: the test asserts on it
    out.update(ok=False, error=f"{type(exc).__name__}: {exc}")
out["elapsed_s"] = round(time.monotonic() - started, 2)
print(json.dumps(out), flush=True)
