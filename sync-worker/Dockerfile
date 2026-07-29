# Salesforce sync worker — TechSara Local AI Analysis Platform (§7).
# Base image is multi-arch; python:3.11-slim resolves to arm64 on the DGX Spark.
FROM python:3.11-slim

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
