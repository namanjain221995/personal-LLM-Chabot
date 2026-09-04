# The speech-to-text engine: stock vLLM plus the audio decoders it needs.
#
# WHY A DERIVED IMAGE AND NOT THE STOCK ONE. vLLM already ships the model —
# `Qwen3ASRForConditionalGeneration` is in the registry of the nightly image
# this deployment already runs (verified 2026-09-04) — but the audio decode
# path is behind the `vllm[audio]` extra, which the published image does not
# include. Without it every request dies at
#
#     File ".../vllm/multimodal/media/audio.py", line 76, in load_audio_pyav
#       with av.open(path) as container:
#     ImportError: Please install vllm[audio] for audio support
#
# Installing the three decoders on top is a ~90 MB layer that changes nothing
# else. Installing `vllm[audio]` itself would reinstall vLLM, which on ARM64
# means a source build and a version that no longer matches the cluster.
#
# PyAV is the reason the browser's recording needs no conversion anywhere:
# it bundles ffmpeg, so the WebM/Opus a MediaRecorder produces is decoded
# HERE, natively, and the orchestrator never has to normalise audio it does
# not understand. Measured on this hardware: the same 15-second sample
# transcribes identically as WAV (2.1 MB) and as WebM/Opus (145 KB) in 1.09s.
#
# The base tag is deliberately the same one the cluster's own vLLM runs, so
# this image reuses layers that are already on the worker's disk.
ARG ASR_BASE_IMAGE=vllm/vllm-openai:nightly
FROM ${ASR_BASE_IMAGE}

# av      — the decoder vLLM reaches for (bundled ffmpeg; WebM/Opus, MP4/AAC,
#           Ogg, WAV, FLAC, MP3 all arrive already supported)
# soundfile, librosa — vLLM's resampling path to the model's 16 kHz mono input
RUN pip install --no-cache-dir av soundfile librosa

# vLLM's own entrypoint. Arguments come from the compose command, so this
# image is not tied to one model or one port.
