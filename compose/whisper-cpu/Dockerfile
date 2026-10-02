# whisper-large-v3 on the worker's CPU cores (Spark 2, spark-476e: 10 Cortex-X925 + 10 Cortex-A725).
# The overflow replica beside the two GPU replicas; see compose/whisper-cpu/server.py.
#
# ITS OWN IMAGE, like the GPU replica's. Nothing here is installed into the chat model's
# containers, and nothing here needs a GPU. aarch64 only: the build flags below name Arm features.
#
# Everything is pinned: whisper.cpp by commit, Arm KleidiAI by whisper.cpp's own FetchContent pin,
# the Python packages by version, openai/whisper's mel filters by commit and SHA-256.
#
# THREE STAGES. `build` compiles whisper.cpp and wcpp-worker. `service` (the last, so the default
# target) is what runs. `convert` is used only by `scripts/whisper-cpu.sh up`, once per node, to
# turn the pinned Hugging Face weights into whisper.cpp's format; it is never started as a service.

FROM ubuntu:24.04 AS build

ARG WHISPER_CPP_COMMIT=927cfce34f31707e17f2bff35c349632fb9e2c3a
# BuildKit cannot pin a build to cores, so the compile is bounded by parallelism instead: four jobs
# for a few minutes, once, rather than every core of the node the chat model's worker rank shares.
ARG BUILD_JOBS=4

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ONE CHANGE TO WHISPER.CPP: THE PER-WINDOW TOKEN CAP. Upstream stops a 30 s window after
# n_text_ctx/2 - 4 = 220 new tokens, because it keeps half the context for past text. We never pass
# past text (no_context), and the GPU replica's transformers pipeline allows the model's whole
# 448-token context minus the prompt. Dense Hindi reaches 220 tokens: 5 of 200 FLEURS-hi clips
# (17-24 s) hit it. whisper.cpp then cut the window mid-character and decoded the tail again as a
# second window, so text was repeated (+2 to +19 word errors against the GPU's answer). That was
# more than the whole measured Hindi gap to the GPU (docs/voice/CPU-REPLICA.md). With the cap at
# n_text_ctx - prompt - 1 (443), those clips decode in one window, as on the GPU, and four of the
# five score exactly the GPU's errors. The build fails if the line is not found exactly once, so a
# whisper.cpp bump cannot drop the change unnoticed.
RUN git clone https://github.com/ggml-org/whisper.cpp /src/whisper.cpp \
    && git -C /src/whisper.cpp checkout --detach "${WHISPER_CPP_COMMIT}" \
    && f=/src/whisper.cpp/src/whisper.cpp \
    && test "$(grep -c 'n_max = whisper_n_text_ctx(ctx)/2 - 4;' "$f")" = 1 \
    && sed -i 's|n_max = whisper_n_text_ctx(ctx)/2 - 4;|n_max = whisper_n_text_ctx(ctx) - (int) prompt.size() - 1;|' "$f" \
    && test "$(grep -c 'n_max = whisper_n_text_ctx(ctx) - (int) prompt.size() - 1;' "$f")" = 1

COPY CMakeLists.txt wcpp_worker.cpp /src/worker/

# THE ARCHITECTURE FLAGS ARE EXPLICIT, AND THAT IS A MEASUREMENT (2026-09-30, q8_0, 8 threads on
# the X925 cores, encoder time for one 30 s window):
#   -mcpu=native (GGML_NATIVE)            6.0 s   gcc 13 does not know the X925 and falls back to
#                                                 plain armv8: no dotprod, no i8mm, no fp16
#   armv9.2-a + SVE2 + i8mm + bf16        9.0 s   ggml's SVE paths are slower here than NEON
#   armv8.6-a + dotprod + i8mm + fp16     2.4 s
#   ... + Arm KleidiAI micro-kernels      1.9 s   <- this build
# BUILD_SHARED_LIBS=OFF: one static binary, so the runtime stage needs only libgomp.
RUN cmake -S /src/worker -B /src/build \
        -DWHISPER_CPP_DIR=/src/whisper.cpp \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_NATIVE=OFF \
        -DGGML_CPU_ARM_ARCH=armv8.6-a+dotprod+i8mm+fp16 \
        -DGGML_CPU_KLEIDIAI=ON \
        -DGGML_OPENMP=ON \
        -DWHISPER_BUILD_TESTS=OFF \
        -DWHISPER_BUILD_EXAMPLES=ON \
        -DWHISPER_SDL2=OFF \
    && cmake --build /src/build -j "${BUILD_JOBS}" --target wcpp-worker whisper-quantize \
    && mkdir -p /out \
    && cp "$(find /src/build -type f -name wcpp-worker -perm -u+x | head -n 1)" /out/wcpp-worker \
    && cp "$(find /src/build -type f -name whisper-quantize -perm -u+x | head -n 1)" /out/whisper-quantize \
    && cp /src/whisper.cpp/models/convert-h5-to-ggml.py /out/convert-h5-to-ggml.py


# --- convert: Hugging Face safetensors -> ggml f16, run once per node by scripts/whisper-cpu.sh ---
FROM python:3.12-slim AS convert

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1

# The SAME torch and transformers as the GPU replica's image (compose/whisper/Dockerfile), from
# the CPU index: the converter only reads the weights, and the same versions write the same bytes.
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch==2.14.0 \
    && pip install transformers==5.16.1 "numpy==2.5.3"

# whisper.cpp's converter reads the 128-bin mel filterbank from an openai/whisper checkout.
ARG OPENAI_WHISPER_COMMIT=86098128c0b4f24f0e2aa2994de830614b474227
ARG MEL_FILTERS_SHA256=7450ae70723a5ef9d341e3cee628c7cb0177f36ce42c44b7ed2bf3325f0f6d4c
RUN mkdir -p /opt/openai-whisper/whisper/assets \
    && python3 -c "import urllib.request; urllib.request.urlretrieve('https://raw.githubusercontent.com/openai/whisper/${OPENAI_WHISPER_COMMIT}/whisper/assets/mel_filters.npz', '/opt/openai-whisper/whisper/assets/mel_filters.npz')" \
    && echo "${MEL_FILTERS_SHA256}  /opt/openai-whisper/whisper/assets/mel_filters.npz" | sha256sum -c -

COPY --from=build /out/convert-h5-to-ggml.py /opt/convert-h5-to-ggml.py
COPY --from=build /out/whisper-quantize /usr/local/bin/whisper-quantize
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 && rm -rf /var/lib/apt/lists/*


# --- the service -------------------------------------------------------------------------------
FROM ubuntu:24.04 AS service

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_BREAK_SYSTEM_PACKAGES=1

# ffmpeg is the decoder, exactly as on the GPU replica; libgomp is whisper.cpp's OpenMP runtime.
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip ffmpeg libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# The GPU replica's HTTP stack, at its versions.
RUN pip install \
        "fastapi==0.141.1" \
        "uvicorn[standard]==0.52.4" \
        "python-multipart==0.0.32" \
        "numpy==2.5.3"

COPY --from=build /out/wcpp-worker /usr/local/bin/wcpp-worker
COPY --from=build /out/whisper-quantize /usr/local/bin/whisper-quantize
COPY server.py /app/server.py

# Not root: the service reads one model file and writes nothing (the root filesystem is read-only
# in compose/compose.whisper-cpu.yaml; /tmp is a tmpfs for Starlette's upload spooling).
RUN useradd --system --uid 10008 --no-create-home --shell /usr/sbin/nologin whisper
USER 10008
WORKDIR /app

EXPOSE 30008
CMD ["python3", "-u", "/app/server.py"]
