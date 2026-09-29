# Streaming speech-to-text for live dictation, on the worker's CPU
# (spark-476e: aarch64, the ten Cortex-X925 cores).
#
# ITS OWN IMAGE, for the reason compose/whisper/Dockerfile gives: nothing
# about speech is installed into the orchestrator or any model container, so
# an upgrade here can never move a dependency under the chat model.
#
# NO CUDA, ON PURPOSE. Chat is tensor-parallel across both Sparks and any
# sustained GPU work on either node slows it for everyone; this engine was
# measured to hold 12 real-time streams on about five CPU cores with no
# change in chat decode speed (server.py's docstring has the numbers). The
# image therefore has no GPU runtime at all, which also keeps it small.
#
# Ubuntu 24.04 gives Python 3.12.3, the interpreter the engine was measured
# and smoke-tested on; requirements.txt pins every package of that run.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install -r /app/requirements.txt

COPY server.py /app/server.py
WORKDIR /app

# Unprivileged. The container also runs with a read-only root filesystem, no
# capabilities and no-new-privileges (compose/compose.stt-stream.yaml); the
# models are world-readable read-only bind mounts, and nothing is written but
# /tmp.
RUN useradd --system --uid 10009 --no-create-home --shell /usr/sbin/nologin stt
USER 10009

EXPOSE 30009
CMD ["python3", "-u", "/app/server.py"]
