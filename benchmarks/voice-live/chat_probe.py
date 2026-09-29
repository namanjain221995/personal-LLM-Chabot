#!/usr/bin/env python3
"""Single-stream decode speed of the main model (tok/s between first and last token)."""
import json, sys, time, urllib.request
URL = "http://127.0.0.1:8000/v1/chat/completions"
def once(max_tokens=300):
    body = {"model": "Qwen/Qwen3.6-35B-A3B-NVFP4", "stream": True, "temperature": 0, "max_tokens": max_tokens,
            "stream_options": {"include_usage": True}, "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "user", "content": "Explain in detail, step by step, how a refrigerator moves heat from inside to outside. Write at least 500 words."}]}
    req = urllib.request.Request(URL, data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    first = last = None; toks = None; n_chunks = 0
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=180) as r:
        for line in r:
            line = line.strip()
            if not line.startswith(b"data: ") or line == b"data: [DONE]": continue
            j = json.loads(line[6:])
            if j.get("usage"): toks = j["usage"]["completion_tokens"]
            for c in j.get("choices", []):
                if (c.get("delta") or {}).get("content"):
                    now = time.perf_counter(); first = first or now; last = now; n_chunks += 1
    return {"ttft_ms": round((first - t0) * 1000), "decode_tok_s": round((toks - 1) / (last - first), 1), "tokens": toks}
if __name__ == "__main__":
    k = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    label = sys.argv[2] if len(sys.argv) > 2 else ""
    for _ in range(k):
        r = once(); r["label"] = label; print(json.dumps(r), flush=True); time.sleep(1)
