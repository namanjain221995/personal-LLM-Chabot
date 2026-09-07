#!/usr/bin/env python3
"""Score a speech engine against the shared benchmark corpus.

    python3 benchmarks/asr/evaluate.py --base-url http://<engine>:<port>/v1

WHAT THIS IS FOR. One deterministic scorer, so that two engines measured on
different days are still comparable: same corpus, same normalisation, same
arithmetic. It survived the removal of both evaluated engines because none of
that was ever about a particular model.

WHAT IT SPEAKS. The OpenAI-compatible `POST /v1/audio/transcriptions` — the
format vLLM, faster-whisper servers and most hosted engines all expose. An
engine that speaks something else needs one function here, not a second
scoring system.

DECODING IS THE ENGINE'S BUSINESS. This sends audio and reads text. Anything
that changes what the model produces — temperature, beam width, chunking —
belongs in the engine's own configuration, and must be recorded there so a
result file says what produced it.

Standard library only. No repo imports, so this runs on a bare host as easily
as inside a container.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
CORPUS = HERE / "corpus"
RESULTS = HERE / "results"

#: The model's own output contract on the chat path, copied from
#: orchestrator/app/asr.py so this script stays standalone. If the two ever
#: disagree, app/asr.py is the one that is right — it is production.
_CHAT_OUTPUT = re.compile(r"^language\s+(?P<language>[^<]+)<asr_text>(?P<text>.*)$", re.S)


# ---------------------------------------------------------------------------
# Normalisation
#
# THE RULE THIS FOLLOWS: remove differences that are not recognition errors,
# and NOTHING else. Case and punctuation are formatting choices the model makes
# and a reference transcript cannot fairly predict. A wrong WORD, a dropped
# negation, a mangled name and a wrong digit are recognition errors, and every
# one of them survives this function intact.
#
# What is deliberately NOT done here: no number-word folding ("15" -> "fifteen"),
# no spelling correction, no stemming, no stopword removal, no transliteration.
# Each of those would hide exactly the failure this benchmark exists to find.
# ---------------------------------------------------------------------------

#: Unicode punctuation that is purely presentational. Apostrophes and hyphens
#: are NOT here: "dont" vs "don't" is a real difference in a transcript, and
#: "re-schedule" vs "reschedule" is a real difference in a word count.
_PUNCT = str.maketrans(
    {c: " " for c in ".,!?;:\"“”„‟()[]{}«»…—–।॥、。？！，；："}
)


#: Private-use codepoints, which cannot occur in a transcript, standing in for
#: punctuation that sits BETWEEN DIGITS while the table above runs. One marker
#: per character rather than one shared marker, so "3:30" and "3.30" stay
#: distinct — they are different ways of being right, and of being wrong.
_PROTECT = {".": "\ue000", ",": "\ue001", ":": "\ue002"}
_RESTORE = {marker: char for char, marker in _PROTECT.items()}


def normalise(text: str) -> str:
    """The deterministic form both sides of a comparison are scored in.

    1. NFKC, so a composed and a decomposed Devanagari vowel sign are one
       string rather than two, and full-width Latin folds to Latin.
    2. Intra-number punctuation is parked out of the table's reach. A
       benchmark that scores "3.30 pm" as "3 30 pm" cannot see a model that
       got the time wrong, and "3.5" is not "3 5".
    3. Casefold, not lower(): it is the Unicode-correct one, and it matters
       here only because neither the model nor a human reference writer is
       consistent about capitalising a sentence.
    4. Presentational punctuation to spaces, then whitespace collapsed.
    """
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"(?<=\d)([.,:])(?=\d)", lambda m: _PROTECT[m.group(1)], text)
    text = text.casefold()
    text = text.translate(_PUNCT)
    for marker, char in _RESTORE.items():
        text = text.replace(marker, char)
    return " ".join(text.split())


def tokens(text: str) -> list[str]:
    return normalise(text).split()


# ---------------------------------------------------------------------------
# Edit distance
# ---------------------------------------------------------------------------


def _levenshtein(a: list, b: list) -> int:
    """Edit distance with the two-row trick — O(min) memory, exact result."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(
                previous[j - 1] if x == y
                else 1 + min(previous[j - 1], previous[j], current[j - 1])
            )
        previous = current
    return previous[-1]


def wer(reference: str, hypothesis: str) -> Optional[float]:
    """Word error rate. None when the reference has no words to be wrong about.

    None rather than 0.0 on an empty reference: a silence clip has no word
    error rate, and averaging a 0.0 into the corpus would flatter every model
    that stayed quiet AND every model that did not.
    """
    ref = tokens(reference)
    if not ref:
        return None
    return _levenshtein(ref, tokens(hypothesis)) / len(ref)


def cer(reference: str, hypothesis: str) -> Optional[float]:
    """Character error rate — the honest metric for Hindi and Chinese.

    WHY IT IS NOT OPTIONAL FOR DEVANAGARI: WER counts whitespace-delimited
    tokens, and one wrong matra inside a long word scores the same as the whole
    word being invented. CER sees the difference.
    """
    ref = normalise(reference).replace(" ", "")
    if not ref:
        return None
    hyp = normalise(hypothesis).replace(" ", "")
    return _levenshtein(list(ref), list(hyp)) / len(ref)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def parse_chat_output(content: str) -> tuple[Optional[str], str]:
    """Split `language English<asr_text>Hello.` into (language, text)."""
    match = _CHAT_OUTPUT.match((content or "").strip())
    if not match:
        return None, (content or "").strip()
    return match.group("language").strip().rstrip(".").title(), match.group("text").strip()


MIME = {
    ".webm": "audio/webm", ".ogg": "audio/ogg", ".opus": "audio/opus",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".mp4": "audio/mp4", ".flac": "audio/flac", ".aac": "audio/aac",
}


def transcribe(
    base_url: str, model: str, audio: bytes, content_type: str, filename: str,
    *, language: str = "", timeout: float = 300.0,
) -> tuple[float, Optional[str], str, dict]:
    """One clip through `POST /v1/audio/transcriptions`.

    Multipart, because that is what the OpenAI audio API specifies and what
    every implementation of it accepts. `verbose_json` is requested so the
    reply can carry the detected language; an engine that refuses it still
    answers plain `{"text": ...}` and the language is reported as None rather
    than guessed.
    """
    boundary = "----techsara-asr-eval"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    field("model", model)
    field("response_format", "verbose_json")
    if language and language != "auto":
        field("language", language)
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
        f'filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode()
        + audio + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())

    request = urllib.request.Request(
        base_url.rstrip("/") + "/audio/transcriptions",
        data=b"".join(parts),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = json.loads(response.read())
    elapsed = time.perf_counter() - started
    language_out = body.get("language") or None
    return elapsed, language_out, str(body.get("text") or "").strip(), {
        "duration": body.get("duration"),
    }


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> list[dict]:
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise SystemExit(f"{path}: expected a JSON array of clips")
    return [e for e in entries if not str(e.get("id", "")).startswith("example-")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True,
                        help="the engine's OpenAI-compatible root, e.g. http://host:30006/v1")
    parser.add_argument("--model", required=True,
                        help="the model name the engine serves, sent as the `model` field")
    parser.add_argument("--manifest", type=Path, default=CORPUS / "manifest.json")
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--label", required=True,
                        help="names the output file under results/")
    parser.add_argument("--repeats", type=int, default=1,
                        help="runs per clip; the median latency is reported")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    entries = load_manifest(args.manifest)
    if not entries:
        print(
            "No clips in the manifest.\n\n"
            "benchmarks/asr/corpus/manifest.json ships as a TEMPLATE — the example\n"
            "rows are skipped on purpose, because a benchmark that invents its own\n"
            "audio measures nothing. See benchmarks/asr/corpus/README.md for what to\n"
            "record and how to add it.",
            file=sys.stderr,
        )
        return 2

    per_sample: list[dict] = []
    for entry in entries:
        clip = args.corpus / str(entry["audio"])
        if not clip.exists():
            print(f"  MISSING  {entry['id']}: {clip}", file=sys.stderr)
            per_sample.append({**entry, "status": "missing_audio"})
            continue
        audio = clip.read_bytes()
        content_type = MIME.get(clip.suffix.lower(), "audio/webm")
        times, language, text, usage = [], None, "", {}
        try:
            for _ in range(max(1, args.repeats)):
                elapsed, language, text, usage = transcribe(
                    args.base_url, args.model, audio, content_type, clip.name,
                    language=str(entry.get("force_language") or ""),
                )
                times.append(elapsed)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            print(f"  FAILED   {entry['id']}: {exc}", file=sys.stderr)
            per_sample.append({**entry, "status": "engine_error", "error": str(exc)})
            continue

        reference = str(entry.get("reference") or "")
        expect_silence = str(entry.get("category")) == "silence"
        row = {
            "id": entry["id"],
            "category": entry.get("category"),
            "language_expected": entry.get("language"),
            "audio": entry["audio"],
            "audio_bytes": len(audio),
            "status": "ok",
            "reference": reference,
            "hypothesis": text,
            "hypothesis_normalised": normalise(text),
            "language_identified": language,
            "wer": wer(reference, text),
            "cer": cer(reference, text),
            "latency_median_s": round(statistics.median(times), 3),
            "latency_samples_s": [round(t, 3) for t in times],
            "usage": usage,
        }
        # A silence clip has no reference to be wrong about; what it has is a
        # pass/fail on the one behaviour that matters — did the model invent
        # words in a room where nobody spoke.
        if expect_silence:
            row["hallucinated_on_silence"] = bool(normalise(text))
        per_sample.append(row)
        mark = "ok" if row["status"] == "ok" else row["status"]
        w = f"{row['wer']:.3f}" if row["wer"] is not None else "  -  "
        c = f"{row['cer']:.3f}" if row["cer"] is not None else "  -  "
        print(f"  {mark:8} {entry['id']:<14} WER {w}  CER {c}  "
              f"{row['latency_median_s']:.2f}s  lang={language}")

    scored = [r for r in per_sample if r.get("status") == "ok"]
    wers = [r["wer"] for r in scored if r["wer"] is not None]
    cers = [r["cer"] for r in scored if r["cer"] is not None]
    lats = [r["latency_median_s"] for r in scored]

    def agg(values: list[float]) -> dict:
        if not values:
            return {"n": 0}
        out = {"n": len(values), "mean": round(statistics.fmean(values), 4),
               "median": round(statistics.median(values), 4)}
        # p95 on a handful of samples is a number with no meaning. Saying so
        # in the artefact is better than printing one somebody will quote.
        out["p95"] = (round(sorted(values)[max(0, round(0.95 * len(values)) - 1)], 4)
                      if len(values) >= 20 else None)
        if len(values) < 20:
            out["p95_note"] = "not reported: fewer than 20 samples"
        return out

    by_category: dict[str, dict] = {}
    for row in scored:
        key = str(row.get("category") or "uncategorised")
        bucket = by_category.setdefault(key, {"n": 0, "wer": [], "cer": []})
        bucket["n"] += 1
        if row["wer"] is not None:
            bucket["wer"].append(row["wer"])
        if row["cer"] is not None:
            bucket["cer"].append(row["cer"])
    for key, bucket in by_category.items():
        by_category[key] = {
            "n": bucket["n"],
            "wer_mean": round(statistics.fmean(bucket["wer"]), 4) if bucket["wer"] else None,
            "cer_mean": round(statistics.fmean(bucket["cer"]), 4) if bucket["cer"] else None,
        }

    report = {
        "model": args.model,
        "base_url": args.base_url,
        "date": time.strftime("%Y-%m-%d"),
        # Decoding lives in the engine's configuration, not here. Record it
        # alongside the result file for the engine being measured.
        "decoding": {"path": "/v1/audio/transcriptions",
                     "note": "set by the engine; see its own service configuration"},
        "corpus": {"manifest": str(args.manifest.relative_to(HERE.parent.parent))
                   if args.manifest.is_relative_to(HERE.parent.parent) else str(args.manifest),
                   "clips": len(entries), "scored": len(scored)},
        "aggregate": {"wer": agg(wers), "cer": agg(cers), "latency_s": agg(lats)},
        "by_category": by_category,
        "hallucinated_on_silence": [
            r["id"] for r in scored if r.get("hallucinated_on_silence")
        ],
        "per_sample": per_sample,
    }

    out = args.out or (RESULTS / f"{args.label}-corpus.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nscored {len(scored)}/{len(entries)} clips -> {out}")
    if wers:
        print(f"  WER mean {statistics.fmean(wers):.4f}")
    if cers:
        print(f"  CER mean {statistics.fmean(cers):.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
