"""Does the chat prompt produce a diagram when one is asked for, and does it
colour it? A live producer: it needs the engine, so it is deliberately not
named test_*.py and is not in the default collection.

    cd orchestrator && python tests/parity/runs/diagram_probe.py
"""
import json
import os
import pathlib
import re
import sys
import time
import urllib.request

# orchestrator/tests/parity/runs/diagram_probe.py -> orchestrator. Derived,
# never a literal: see run_live.py for why.
ORCH = os.environ.get("PARITY_ORCH") or str(
    pathlib.Path(__file__).resolve().parents[3])
HERE = pathlib.Path(__file__).resolve().parent
#: The probe writes here, NOT into runs/ itself: runs/ holds the frozen
#: baselines and every one of them is pinned by name in BASELINE_SCORES.json.
OUT = HERE / "probe"
OUT.mkdir(exist_ok=True)
sys.path.insert(0, ORCH)
from app.engines import (  # noqa: E402 -- sys.path is set above
    CODE_INSTRUCTION, DIAGRAM_INSTRUCTION, FORMAT_INSTRUCTION)
from app.engines.chat import ASSISTANT_SYSTEM  # noqa: E402
SYS = ASSISTANT_SYSTEM + FORMAT_INSTRUCTION + DIAGRAM_INSTRUCTION + CODE_INSTRUCTION
def gate():
    while True:
        b=urllib.request.urlopen("http://127.0.0.1:8000/metrics",timeout=20).read().decode()
        r=[float(ln.rsplit(" ",1)[1]) for ln in b.splitlines()
           if ln.startswith("vllm:num_requests_running{")]
        if not r:
            raise SystemExit("no gauge")
        if r[0]==0:
            return
        time.sleep(10)
def ask(u, think=True, mt=8000):
    body={"model":"Qwen/Qwen3.6-35B-A3B-NVFP4","messages":[{"role":"system","content":SYS},{"role":"user","content":u}],
          "temperature":0.3,"max_tokens":mt,"chat_template_kwargs":{"enable_thinking":think}}
    gate()
    t=time.perf_counter()
    d=json.load(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8000/v1/chat/completions",
        data=json.dumps(body).encode(),headers={"Content-Type":"application/json"}),timeout=1800))
    return d["choices"][0]["message"]["content"] or "", time.perf_counter()-t
CASES={
 "explicit_diagram":"Draw a mermaid architecture diagram of this platform: Next.js frontend, FastAPI backend, vLLM serving Qwen on 10 DGX Spark nodes, PostgreSQL, Redis, RAG document search, web search, speech-to-text and text-to-speech, for 300 enterprise users.",
 "explicit_coloured":"Draw a mermaid architecture diagram of this platform (Next.js frontend, FastAPI backend, vLLM/Qwen on 10 DGX Spark nodes, PostgreSQL, Redis, RAG, web search, STT, TTS). Use different colours for each layer so the layers are easy to tell apart.",
}
out={}
for k,u in CASES.items():
    txt,dt = ask(u)
    open(OUT / f"diag_{k}.md","w").write(txt)
    has = "```mermaid" in txt
    body = txt.split("```mermaid",1)[1].split("```",1)[0] if has else ""
    col = bool(re.search(r"\b(style|classDef|linkStyle|fill:|stroke:)|%%\{\s*init", body))
    nodes = len(re.findall(r'^\s*\w+\[', body, re.M))
    out[k]={"mermaid":has,"colour_directive":col,"nodes":nodes,"wall_s":round(dt,1),"chars":len(txt)}
    print(k, json.dumps(out[k]), flush=True)
json.dump(out, open(OUT / "diagram_probe.json","w"), indent=2)
