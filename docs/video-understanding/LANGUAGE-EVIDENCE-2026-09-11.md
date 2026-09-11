# Speech-to-text on a Gujarati / Hindi / English meeting — what was measured

Date: 2026-09-11. Recording: the 2h23m meeting behind analysis 28 (a user's
own upload; nothing from it is quoted beyond short fragments needed to show
the engine's behaviour). Engine: `openai/whisper-large-v3`, revision
`06f233fe`, the worker Spark. Probe: `scripts/asr_language_probe.py`.

## The complaint

The transcript "shows the same words and sentences many times" and "does not
understand the language" when a person speaks Hindi or Gujarati.

## What the stored transcript contained

1,698 cues, 114,266 characters. Language the engine assigned per cue:

| label | cues | what it actually is |
|---|---|---|
| `en` | 1,204 | fluent English — much of it a **translation** of Hindi/Gujarati speech, not what was said (see below) |
| `hi` | 404 | Hindi — and Gujarati written with Hindi letters |
| `pa` | 52 | Punjabi script for Gujarati speech — wrong |
| `gu` | 27 | Gujarati |
| `kn` / `da` / `id` | 11 | Kannada, Danish, Indonesian — wrong |

Repetition, counted exactly:

* "no" 434 times inside one cue; "It will happen," 110 times; "It will match
  with the IDC," 55 times; a ten-word phrase 40 times (2,178 characters).
* "I am a" as 85 separate cues between 2177.8 s and 2193.8 s — five a second.
* 265 of 1,698 cues were part of a run of identical consecutive cues.
* **17.9 % of the transcript's characters were the decoder repeating itself.**

## Re-transcribing three 60-second windows under each language setting

`auto` is what production does. Time is engine wall-clock for 60 s of audio.

| window | setting | detected | chars | time | what came back |
|---|---|---|---|---|---|
| 790 s | auto | `pa` | 363 | 68 s | Punjabi script, one phrase ×12, then a character loop |
| | `gu` | `gu` | 605 | **145 s** | real Gujarati ("આલ્ગોરિધમ લેવલ જીરો ચેન્જ હશે"), then "હશે" ×25 |
| | `hi` | `hi` | 226 | 23 s | "सब्सक्राइब" ×16 — the YouTube-caption hallucination |
| | `en` | `en` | 832 | 17 s | fluent English, no loop — a **translation** of the Gujarati |
| 2150 s | auto | `en` | 619 | 15 s | fluent English — a translation of Hindi ("Now that you are looking at me…") |
| | `gu` | `gu` | 352 | 70 s | Hindi words in Gujarati letters, then a loop |
| | `hi` | `hi` | 447 | 49 s | **correct Hindi** ("अभी आपको मुझे देख रहे हैं तो ऐसे लग रहा है…"), then "सब्सक्राइब" |
| | `en` | `en` | 619 | 16 s | same translation as auto |
| 6690 s | auto | `en` | 684 | 11 s | fluent English — a translation of Gujarati |
| | `gu` | `gu` | 924 | **277 s** | real Gujarati ("આપડે સિસ્ટમ ડિઝાઇન કરેલી છે"), badly mangled |
| | `hi` | `hi` | 283 | 26 s | "सब्सक्राइब" ×20 |
| | `en` | `en` | 684 | 16 s | same translation as auto |

## What this says

1. **The speaker switches between Gujarati, Hindi and English within
   minutes.** Both Hindi and Gujarati are genuinely present (the `hi` output
   at 2150 s and the `gu` output at 6690 s are the actual words).
2. **"Transcribe" is not honoured when the engine decides the language is
   English.** With `task=transcribe` and `<|en|>`, Whisper produces an
   English *translation* of Hindi/Gujarati speech. Auto-detect chose `en`
   for 71 % of cues, so most of the stored transcript is a translation. This
   is the "does not understand the language" the person saw: correct-looking
   English where the words were never spoken in English.
3. **Forcing a language does not solve a three-language recording.** `hi`
   transcribes Hindi correctly and hallucinates "सब्सक्राइब" through the
   Gujarati; `gu` produces mangled Gujarati at 2.4–4.6× real time.
4. **whisper-large-v3 is weak at Gujarati.** Slow, looping, and inaccurate
   even when told the language. This is the model, not the deployment.
5. **The loops cost GPU time, not just readability.** A looping 60-second
   window took 68–277 s; a clean one took 11–17 s. The sequential long-form
   algorithm advances by the model's own timestamp, and a decoder that emits
   a timestamp 20 ms after the last one re-decodes the window in 20 ms steps
   — which is exactly the 85 "I am a" cues, 20 ms apart.

## What was done

* `video/loops.py` collapses the repetition after stitching (17.9 % → 0 on
  this transcript, in 25 ms). Pipeline v3; v2 analyses repair in place on
  their next attach without asking the engines again.
* Only the WebVTT transcript is offered for download by default
  (`VIDEO_ARTIFACT_KINDS`).

## What was NOT done, and why it needs a decision

The language problem is a model choice. Options, with what each costs:

| option | what it fixes | what it does not | cost |
|---|---|---|---|
| Evaluate an Indic-language ASR (AI4Bharat IndicWhisper / IndicConformer, or a Whisper fine-tune for gu+hi) beside large-v3 on this recording | Gujarati accuracy; the translation-instead-of-transcription failure | nothing measured yet | GPU memory on one Spark for a second engine during the evaluation; a scoring pass on a hand-checked sample |
| Restrict language detection to the workspace's languages (`en,hi,gu`) in the engine | the `pa` / `kn` / `da` / `id` mis-detections (5 % of cues) | the `en` translation problem, Gujarati quality | a change to `compose/whisper/server.py` (a code review, by its own rules) and an engine rebuild on both nodes |
| A per-upload language choice in the composer | a recording that IS one language (Hindi-only meetings transcribe well under `hi`) | mixed recordings | UI + API + pipeline plumbing |
| Decoder repetition constraints (`no_repeat_ngram_size`, `repetition_penalty`) | the loops at the source, and the GPU time they burn | unmeasured; the related long-form recipe was measured and rejected here on 2026-09-08 | the same engine code review, plus a measurement on this recording |

Recommendation: the first row. The other three each improve a corner; none
of them makes Gujarati speech come back as Gujarati words reliably, and that
is what was asked for.
