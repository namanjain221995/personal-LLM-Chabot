#!/usr/bin/env python3
"""Is one OnlineRecognizer safe to share across decode threads (disjoint streams)?
Decodes the same 8 utterances twice: sequentially (reference) and with 4 threads
sharing ONE recognizer; texts must be identical; also reports RSS."""
import glob, os, sys, threading, resource
import numpy as np, soundfile as sf, sherpa_onnx
d = sys.argv[1]
f = lambda p: sorted(glob.glob(os.path.join(d, p)))[0]
rec = sherpa_onnx.OnlineRecognizer.from_transducer(tokens=os.path.join(d, "tokens.txt"), encoder=f("encoder*.onnx"),
    decoder=f("decoder*.onnx"), joiner=f("joiner*.onnx"), num_threads=2, enable_endpoint_detection=True,
    rule2_min_trailing_silence=0.6, rule3_min_utterance_length=600.0)
files = sorted(glob.glob(os.path.join(os.environ.get("RTVOICE_DATA") or os.path.expanduser("~/rtvoice-bench/data"), "LibriSpeech/test-clean/*/*/*.flac")))[:16]
auds = [np.concatenate([sf.read(p, dtype="float32")[0], np.zeros(8000, np.float32)]) for p in files]
def decode(a):
    s = rec.create_stream()
    for i in range(0, len(a), 2560):
        s.accept_waveform(16000, a[i:i + 2560])
        while rec.is_ready(s): rec.decode_streams([s])
    s.input_finished()
    while rec.is_ready(s): rec.decode_streams([s])
    return rec.get_result(s)
ref = [decode(a) for a in auds]
out = [None] * len(auds)
def work(idx):
    for k in idx: out[k] = decode(auds[k])
th = [threading.Thread(target=work, args=(list(range(w, len(auds), 4)),)) for w in range(4)]
[t.start() for t in th]; [t.join() for t in th]
same = sum(1 for a, b in zip(ref, out) if a == b)
print(f"identical {same}/{len(auds)}; maxrss {resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024:.0f} MB")
