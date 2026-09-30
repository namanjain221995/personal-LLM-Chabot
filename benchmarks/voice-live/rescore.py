#!/usr/bin/env python3
"""Re-score saved MUCS hypotheses with script-agnostic metrics (same for every system).

skel_wer: romanise both sides, then reduce each word to a phonetic consonant skeleton
(lower-case Latin, c/q->k, z->j, x->ks, w->v, ph->f, drop vowels and h after consonants,
collapse repeats). "इम्प्रेस"/"impress" -> "mprs"/"mprs"; "डॉक्यूमेंट"/"document" -> "dkmnt"/"dkmnt".
It forgives transliteration and spelling variants, never a wrong or missing word.
"""
import json, re, sys, unicodedata, importlib.util, os
spec = importlib.util.spec_from_file_location("E", os.path.join(os.path.dirname(__file__), "mucs_eval.py"))
E = importlib.util.module_from_spec(spec); sys.modules.setdefault("soundfile", type(sys)("soundfile")); spec.loader.exec_module(E)

def skel(word):
    w = E.romanise(word).lower()
    w = w.replace("ph", "f").replace("ck", "k").replace("x", "ks")
    w = re.sub(r"[cq]", "k", w).replace("z", "j").replace("w", "v")
    w = re.sub(r"([^aeiou])h", r"\1", w)
    w = re.sub(r"[aeiouy]", "", w) or w[:1]
    return re.sub(r"(.)\1+", r"\1", w)

def skel_text(t):
    return [skel(w) for w in E.norm(t).split()]

def main(paths, text_file):
    refs = {}
    for line in open(text_file, encoding="utf-8"):
        uid, t = line.rstrip("\n").split(" ", 1); refs[uid] = t
    for p in paths:
        for res in json.load(open(p)):
            hyps = res.get("hyps") or {}
            docs = {}
            for uid in sorted(hyps):
                rec = uid.split("_")[1]
                docs.setdefault(rec, [[], []]); docs[rec][0].append(refs[uid]); docs[rec][1].append(hyps[uid])
            e = n = 0
            for rec, (r, h) in docs.items():
                rs, hs = skel_text(" ".join(r)), skel_text(" ".join(h))
                e += E.edits(rs, hs); n += len(rs)
            print(json.dumps({"system": res["system"].split("/")[-1], "doc_wer": res.get("doc_wer"), "doc_tl_wer": res.get("doc_tl_wer"),
                              "doc_skel_wer": round(100 * e / max(1, n), 2), "segments": len(hyps)}, ensure_ascii=False))

if __name__ == "__main__":
    main(sys.argv[2:], sys.argv[1])
