"""Salesforce sync worker — TechSara Local AI Analysis Platform (spec §7).

Read-only Salesforce extraction (Bulk API 2.0 full extract, then incremental
REST SOQL on SystemModstamp), Parquet + DuckDB storage, and a LanceDB RAG
index of long-text fields embedded via the vLLM OpenAI-compatible
/embeddings endpoint.
"""

__all__ = [
    "chunking",
    "config",
    "jsonlog",
    "rag_index",
    "secrets",
    "sf_auth",
    "sf_client",
    "storage",
]
