# The ASR benchmark corpus

The audio in this directory is the **fixed input** every speech engine is
measured on. It is deliberately independent of any particular model: clips
recorded once are scored the same way by whatever engine is installed, which is
the only way two engines measured months apart can be compared.

Two engines were evaluated and rejected (Qwen3-ASR-1.7B and TheWhisper) and
their runtimes are gone; openai/whisper-large-v3 has been the installed engine
on both Sparks since 2026-09-08 (`scripts/whisper.sh`). This corpus and
[`../evaluate.py`](../evaluate.py) outlived that turnover because neither was
ever about a specific model — score whisper against it the same way.

## The one rule

**Clips are immutable once recorded.** Do not re-record, re-encode, trim,
normalise volume or replace a clip after any model has been scored on it. A
benchmark whose input moved between two runs cannot say which model was better,
and nothing in the numbers will show you that it happened.

If a clip is genuinely wrong — the reference transcript has a typo, or the
recording captured the wrong sentence — retire it under a new id and leave the
old row in place with `"retired": true`. Do not reuse an id.

## What is not committed

`*.webm`, `*.wav` and the other audio extensions in this directory are
**gitignored**. They are recordings of real people, and this repository is not
where employee or customer voices belong. What *is* committed is the manifest,
this README and the harness — enough for anyone to rebuild an equivalent
corpus, not enough to leak a voice.

Keep the audio on the benchmark host. If it needs to move between machines, move
it out of band.

## Recording clips

Record the way the product records, or the benchmark measures the wrong thing:

- **WebM/Opus**, mono, from a browser's `MediaRecorder` — that is what
  `frontend/lib/voice.ts` produces and what the engine actually receives. The
  three browser processors (`echoCancellation`, `noiseSuppression`,
  `autoGainControl`) are ON in production; leave them on.
- **5–30 seconds** each. Composer dictation, not podcast transcription.
- Real speakers, real microphones, real rooms. A text-to-speech clip measures
  how well a model transcribes a synthesiser.

## Categories

Eight, and the manifest ships one example row for each. Aim for at least
**10 clips per category** before treating a per-category WER as anything but
an indication.

| category | what it is for |
|---|---|
| `general_english` | the neutral control |
| `indian_english` | the accent this deployment actually serves |
| `hindi` | Devanagari output; **CER matters more than WER here** |
| `hinglish` | code-switching, the case most models fail on |
| `techsara_terminology` | product nouns, Salesforce object names, people's names |
| `numbers_dates` | invoice numbers, times, amounts, lakh/crore |
| `silence` | a room with nobody speaking — checks hallucination |
| `noisy_speech` | keyboard, fan, background conversation |

`silence` rows carry an empty `reference`. They are not scored for WER — there
are no words to be wrong about — they are scored pass/fail on whether the model
invented any. This is worth checking on every engine: of the two evaluated so
far, one returned an empty string on digital silence and the other fabricated a
word.

## Manifest fields

```json
{
  "id": "clip-001",
  "audio": "clip-001.webm",
  "reference": "Please open yesterday's candidate report.",
  "category": "indian_english",
  "language": "en"
}
```

| field | meaning |
|---|---|
| `id` | stable and unique, forever. Rows beginning `example-` are skipped. |
| `audio` | filename in this directory |
| `reference` | what was actually said, verbatim |
| `category` | one of the eight above |
| `language` | `en`, `hi`, `hi-en` for Hinglish, `null` for silence |
| `force_language` | *optional*. Sends an explicit language instead of auto-detection. Leave it out — production auto-detects. |

### Writing a reference transcript

Write what the speaker **said**, not what they meant. Keep disfluencies
("um", a repeated word) if they were spoken; the model is expected to handle
them. Use digits for spoken digits. Do not punctuate creatively — the scorer
casefolds and strips presentational punctuation anyway, so effort spent there
is wasted, while a wrong *word* is exactly what it is trying to find.

For **Hinglish**, write the reference in the script the speaker would naturally
use, and read the CER as well as the WER. Transliteration choices ("kal" vs
"कल") are not recognition errors but WER counts them as such, so a Hinglish WER
is a loose upper bound, not a verdict. The raw hypothesis is preserved in every
result file for exactly this reason.

## Running it

```bash
python3 benchmarks/asr/evaluate.py \
  --base-url http://<engine-host>:<port>/v1 \
  --model <the model name the engine serves> \
  --label <short-name-for-the-result-file>
```

Writes `benchmarks/asr/results/<label>-corpus.json` with per-sample transcripts,
WER, CER, latency and per-category aggregates. Decoding belongs to the engine's own configuration — record it alongside the
result file, because a run at different settings measures a different system.
