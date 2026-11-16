# sf-assistant-web: the LAN page for the Salesforce assistant (sf_assistant/web); the MCP server (sf-assistant-mcp,
# python -m sf_assistant.server --http) runs on the same image with its own read-only mounts (compose.yaml).
# The image holds Python, the pinned packages and two English word lists only. The code comes from read-only mounts
# (compose.yaml): this folder at /srv/web, the sf_assistant folder (src/, prompts/, knowledge/) at /srv/sfa.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp

# English word lists of the spelling help (SFA_RESOLVER=on; docs/resolver_integration.md, "Build"):
# /usr/share/dict/american-english and british-english, the lists the resolver benchmark used (wamerican/wbritish
# 2020.12.07; Debian 13 gives the same words as the host's Ubuntu 24.04 packages, in another line order). Without a
# word list the spelling help stays off (ordinary words would be taken for typos); SFA_LEXICON can name other files.
# The word set decides what the resolver corrects, so the build stops when it is not the benchmarked one: the SHA-256
# of both lists' sorted unique lines (LC_ALL=C), checked the same way on the host and on Debian 13 (2026-10-21). A
# newer base image with other lists fails here instead of silently changing the corrections; re-run the resolver
# benchmark before changing this hash. The start-up line "spelling help on" prints a short hash of the loaded words.
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends wamerican wbritish \
 && rm -rf /var/lib/apt/lists/* \
 && test -s /usr/share/dict/american-english && test -s /usr/share/dict/british-english \
 && test "$(cat /usr/share/dict/american-english /usr/share/dict/british-english | LC_ALL=C sort -u | sha256sum \
            | cut -d' ' -f1)" = "d3e582e313163747700c84d912728fbf30ad57dc50c818b41089eed5a79ed05e"

# requirements.lock pins every package (generated from requirements.txt); --no-deps installs exactly it.
COPY requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --no-deps -r /tmp/requirements.lock && rm -f /tmp/requirements.lock \
 && pip check \
 && python -c "import duckdb, fastapi, mcp, numpy, openai, pandas, rank_bm25, starlette, tzdata, uvicorn"

# Same uid/gid as the owner on the host, so .private/ files stay his (mode 600).
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "${APP_GID}" app \
 && useradd --uid "${APP_UID}" --gid "${APP_GID}" --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app
USER app

WORKDIR /srv/web
CMD ["python", "/srv/web/server.py"]
