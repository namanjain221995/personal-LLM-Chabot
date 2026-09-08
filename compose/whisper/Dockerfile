# Whisper Large-v3 on Worker Node 2 (spark-476e): aarch64, NVIDIA GB10, CUDA 13.
#
# ITS OWN IMAGE, ON PURPOSE. Nothing about speech is installed into the
# orchestrator, the main vLLM container, the router, vision, embedding or
# reranker. This image exists so that a torch/transformers upgrade for
# dictation can never move a dependency under the chat model.
#
# WHY THIS BASE, AND WHY THIS TORCH. GB10 is Blackwell, compute capability
# 12.1, and the node runs the CUDA 13.0 driver, so the cu130 aarch64 wheels are
# the only candidates. NO PyTorch build ships sm_121 kernels — 2.14.0's
# arch list is sm_80/90/100/110/120 — but sm_120 SASS is binary compatible
# within the 12.x family and 2.14.0 knows it: measured on this GPU on
# 2026-09-08, fp16 conv1d and matmul both produce correct results with no
# warning at all. Torch 2.9.1 runs the same work on the same card by JIT-ing
# its `compute_120` PTX and prints "Found GPU0 NVIDIA GB10 which is of cuda
# capability 12.1. Minimum and Maximum cuda capability supported by this
# version of PyTorch is (8.0) - (12.0)". Both work; this one is the version
# that considers the card supported, and native SASS beats a JIT on the first
# call of every kernel shape.
#
# Everything is pinned — an ASR result file that cannot say which build
# produced it is not evidence.
FROM nvidia/cuda:13.0.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    HF_HOME=/cache/huggingface

# ffmpeg is the decoder. The browser sends WebM/Opus and Whisper wants a
# 16 kHz mono waveform; ffmpeg is the one dependency that reliably bridges
# that on aarch64 without a Python C-extension built from source.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 \
        python3-pip \
        ffmpeg \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Torch first, from the cu130 index, so the generic index below cannot pull a
# CPU-only wheel over the top of it.
RUN pip install --index-url https://download.pytorch.org/whl/cu130 \
        torch==2.14.0

RUN pip install \
        transformers==5.16.1 \
        accelerate==1.14.0 \
        "fastapi==0.141.1" \
        "uvicorn[standard]==0.52.4" \
        "python-multipart==0.0.32" \
        "numpy==2.5.3"

COPY server.py /app/server.py
WORKDIR /app

EXPOSE 30007
CMD ["python3", "-u", "/app/server.py"]
