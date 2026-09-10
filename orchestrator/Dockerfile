# syntax=docker/dockerfile:1
# SUPERSEDED — the launcher builds Dockerfile.cuda (NVIDIA, in-process reranker)
# or Dockerfile.cpu (macOS/CPU/external, no CUDA). This file is the pre-launcher
# single-target build, kept only as the rollback path for docker-compose.yml.
# TechSara Local AI Analysis Platform — orchestrator (FastAPI + LangGraph), §4/§8/§10/§12.
#
# Base image: NVIDIA vLLM from NGC (arm64 CUDA + torch preinstalled) — the same
# image the four model services run, so it is already on this machine and the
# build downloads nothing. The orchestrator never runs the vllm CLI; it only
# needs the CUDA torch stack for the lazy Qwen3-Reranker-0.6B.
# torch comes from this base image — torch is deliberately NOT pinned in requirements.txt.
#
# Documented alternatives:
#   FROM nvcr.io/nvidia/pytorch:25.06-py3   (spec §8 original; ~15 GB pull)
#   FROM python:3.11-slim  with RERANK_ENABLED=false (no torch → no reranker;
#   everything else works unchanged).
FROM nvcr.io/nvidia/vllm:26.05-py3

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# pandoc converts report Markdown to BOTH .docx and .pdf. PDF is produced
# WITHOUT LaTeX: pandoc is invoked with --pdf-engine=weasyprint. The pip
# weasyprint package (see requirements.txt) needs these Pango/Cairo/GDK-PixBuf
# C libraries plus fonts.
RUN apt-get update && apt-get install -y --no-install-recommends \
        pandoc \
        ffmpeg \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        libpangocairo-1.0-0 \
        libcairo2 \
        libgdk-pixbuf-2.0-0 \
        libharfbuzz0b \
        libffi8 \
        shared-mime-info \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Default runtime locations (bind-mounted volumes in docker-compose).
ENV REPORTS_DIR=/reports \
    DUCKDB_PATH=/data/warehouse.duckdb \
    LANCEDB_DIR=/data/lancedb
RUN mkdir -p /reports /data

EXPOSE 8080
# --timeout-graceful-shutdown: how long uvicorn waits for in-flight
# responses after SIGTERM before it force-closes them and runs the lifespan
# shutdown hook. WITHOUT IT uvicorn waits forever, and an SSE stream may
# legitimately run GEN_WALL_CLOCK_S (4,200 s in production) — so compose's
# `stop_grace_period: 2m` expired first and SIGKILL landed before the hook
# ever ran (INF-3). That hook is what marks open chat requests `interrupted`
# and releases the video leases, so losing it is what left a restarted
# generation with nothing to resume from.
#
# 90 s of the 120 s grace, leaving 30 s for the hook itself — whose own work
# is bounded well inside that (video pipeline.stop() waits at most 5 s for
# its runs, web_worker.stop() awaits one cancelled task, the pool close and
# the interrupt UPDATE are single statements).
#
# What it means for a stream older than 90 s: it is cut. That is the
# designed path, not a loss — the request row is marked `interrupted` by the
# hook that now always runs, and the browser re-attaches through
# GET /chat/requests/{intent_id} and GET /chat/attach/{conversation_id}
# (docs/upload-reliability/API.md). A chunked part PUT cut the same way
# leaves no accepted part and the client re-sends that one part.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--timeout-graceful-shutdown", "90"]
