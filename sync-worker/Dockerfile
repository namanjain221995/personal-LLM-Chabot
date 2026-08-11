# Salesforce sync worker — TechSara Local AI Analysis Platform (§7).
# Base image is multi-arch (amd64/arm64); the digest is the same python:3.11-slim
# index the orchestrator's CPU image pins, so both application containers are
# reproducible from one immutable base rather than a moving tag.
FROM python:3.11-slim@sha256:90744cff8f32887f075c47d747a173ff333e9e98801667af93c357fa9f5e28ff

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
