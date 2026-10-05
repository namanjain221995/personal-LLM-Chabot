# The voice archive store: finished recordings on the worker's disk
# (compose/compose.voice-store.yaml, scripts/voice-store.sh).
#
# ITS OWN IMAGE, like compose/whisper: nothing here is installed into the
# orchestrator or any model container, and an upgrade here cannot move a
# dependency under the chat model. No GPU runtime and no model: a small
# Starlette app on the worker's efficiency cores.
#
# The base is pinned by digest (the multi-architecture index the worker pulled
# on 2026-09-30; it carries linux/arm64), and requirements.txt pins every
# package, so a rebuild is the image that was tested.
FROM python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY server.py /app/server.py
WORKDIR /app

# Unprivileged, as the owner of the bind-mounted store directory on the host
# (compose passes the worker user's own uid/gid). The container also runs
# read-only, with no capabilities and no-new-privileges.
USER 1000:1000

EXPOSE 30011
CMD ["python3", "-u", "/app/server.py"]
