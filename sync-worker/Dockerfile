# Salesforce sync worker — TechSara Local AI Analysis Platform (§7).
# Base image is multi-arch (amd64/arm64); the digest is the same python:3.11-slim
# index the orchestrator's CPU image pins, so both application containers are
# reproducible from one immutable base rather than a moving tag.
# BUMPED 2026-09-23. The previous pin,
#   sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff
# carried CVE-2026-13221 (perl: incorrect regular expression processing) and
# CVE-2026-42496 (perl-archive-tar: path traversal) -- both CRITICAL, both with
# a fixed version available, so the blocking image pass
#   trivy image --scanners vuln --severity CRITICAL --ignore-unfixed --exit-code 1
# exits 1 on it. This digest is the SAME python:3.11-slim tag (3.11.16-slim-trixie),
# rebuilt 2026-09-19, and scans clean under that policy. It is the multi-arch INDEX
# digest, so it resolves on CI's amd64 and on the box's arm64 alike.
FROM python:3.11-slim@sha256:da047cb8f9d1d98e5c070f5300ba9f7274e33b8fc0e5be5ed88740aed1b95ba9

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# All runtime deps ship manylinux aarch64 wheels, so no compiler toolchain is needed.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml ./
COPY syncworker ./syncworker

# Run as non-root; /data is the expected volume mount for Parquet/DuckDB/LanceDB.
RUN useradd --create-home --uid 10001 worker \
    && mkdir -p /data \
    && chown -R worker:worker /data /app
USER worker

# Defaults — override via docker compose / env.
ENV SYNC_INTERVAL_MINUTES=30 \
    PARQUET_DIR=/data/parquet \
    DUCKDB_PATH=/data/warehouse.duckdb \
    LANCEDB_DIR=/data/lancedb \
    EMBED_VIA=http://vllm-embed:30003/v1 \
    EMBED_MODEL=Qwen/Qwen3-Embedding-0.6B \
    SYNC_CONFIG_PATH=/app/config.yaml

CMD ["python", "-m", "syncworker.main"]
