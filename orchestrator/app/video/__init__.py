"""Video understanding: a person attaches a video, the assistant understands it.

The package is a PIPELINE of stages, each of which persists its own output so
a failure late in the run never re-runs transcription, and a file uploaded
twice — by anyone — is analysed once (rows are keyed by the sha256 of the
bytes):

    probe       what is this file: duration, streams, codecs        media.py
    audio       the audio track as 16 kHz mono PCM on disk           media.py
    transcript  VAD windows -> Whisper (timestamped segments)        vad.py, transcribe.py
    frames      scene changes + a periodic floor, phash-deduped      media.py, frames.py
    ocr         on-screen text per kept frame, merged into spans     screen.py
    vision      one caption per kept frame (router VLM)              screen.py
    fusion      summary, chapters, key points, decisions, actions    fusion.py
    index       evidence chunks -> LanceDB (video_chunks)            index.py
    artifacts   transcript.txt / .srt / .vtt / .json, screen text,   artifacts.py
                summary.md

`pipeline.py` runs them in order against a `video_analyses` row and reports
named progress; `api.py` is the HTTP surface; `engines/video.py` answers
questions in chat from the evidence with `[mm:ss]` citations.

NOTHING HEAVY IS IMPORTED HERE. lancedb, numpy, PIL and the HTTP clients are
imported inside the functions that need them (tests/test_imports.py fails the
suite if importing the app pulls them in).
"""
