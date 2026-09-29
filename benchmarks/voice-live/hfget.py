import json, os, sys, urllib.request
repo, dest = sys.argv[1], sys.argv[2]
meta = json.load(urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}", timeout=30))
os.makedirs(dest, exist_ok=True)
for s in meta["siblings"]:
    name = s["rfilename"]
    if name.startswith(".git") or name.endswith(".md"):
        continue
    out = os.path.join(dest, name)
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    if os.path.exists(out) and os.path.getsize(out) > 0:
        continue
    urllib.request.urlretrieve(f"https://huggingface.co/{repo}/resolve/main/{name}", out)
print("OK", repo, flush=True)
