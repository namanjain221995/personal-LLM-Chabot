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
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
