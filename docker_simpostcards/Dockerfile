# syntax=docker/dockerfile:1

# base python image for custom image
# Using 3.14: PyTorch's CUDA/ROCm wheels now publish cp314 builds (see
# the logs under /usr/local/lib/python3.14/site-packages/torch/...).
# If a future torch/rocm release drops 3.14 support again, pin back to
# a 3.1x -trixie tag here.
FROM python:3.14-trixie

# ----------------------------------------------------------------------------
# GPU backend selection4
#   - cpu   : no GPU, PyTorch CPU-only wheels (default, smallest image)
#   - cuda  : NVIDIA GPUs, requires the NVIDIA Container Toolkit on the host
#             and `docker run --gpus all ...`
#   - rocm  : AMD GPUs, requires the ROCm kernel driver on the host and
#             `docker run --device=/dev/kfd --device=/dev/dri --group-add video ...`
#
# Build examples:
#   docker build --build-arg TORCH_BACKEND=cuda --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 -t simpostcards:cuda .
#   docker build --build-arg TORCH_BACKEND=rocm --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/rocm6.3 -t simpostcards:rocm .
#   docker build -t simpostcards:cpu .   # default, no GPU
# ----------------------------------------------------------------------------
ARG TORCH_BACKEND=cpu
ARG TORCH_INDEX_URL=""

# GUNICORN_WORKERS=1 is DELIBERATE, not an oversight: simpostcards' GPU
# models (CLIP/BLIP/DETR) are cached per-process (see
# simpostcards.libs.embedder/detection_backend), so with N worker
# PROCESSES you get N independent copies of every model loaded into
# GPU memory -- not shared, never reconciled, each one loaded from
# scratch (network round-trip to Hugging Face Hub + full model init)
# on that worker's first request. Symptoms: GPU memory usage that
# looks "stuck" at roughly N times a single model's footprint (it
# isn't leaking -- there are just N legitimate copies alive at once),
# and N scattered multi-second "cold start" spikes in the request
# logs instead of just one. `--preload` does NOT help here: it only
# preloads Python *code* before fork(), not the lazily-loaded model
# weights -- and a CUDA/ROCm context cannot safely survive fork()
# anyway, so each worker would have to reinitialize its own regardless.
# Use --threads (below) for concurrency instead: a single GPU is
# usually the bottleneck anyway, so multiple worker PROCESSES rarely
# buy real parallelism for GPU inference, only extra memory pressure.
# Raise GUNICORN_WORKERS only for a CPU-only deployment (clip_enabled
# = false, or CLIP_AVAILABLE = False) with no GPU/models to duplicate.
ENV GUNICORN_TIMEOUT=120 \
    GUNICORN_WORKER_CLASS=gthread \
    GUNICORN_WORKERS=1 \
    GUNICORN_THREADS=8

# Persist the Hugging Face cache (CLIP/BLIP/DETR weights, ~2-3 GB total)
# under the /data volume already declared below, instead of the
# container's own (ephemeral) filesystem: without this, every
# container recreate re-downloads every model from scratch on its
# first request -- slow, and wasteful of the HF Hub's anonymous rate
# limit (see the "unauthenticated requests" warning in the logs).
# TRANSFORMERS_CACHE is kept too for older transformers releases that
# don't yet honor HF_HOME.
ENV HF_HOME=/data/hf_cache \
    TRANSFORMERS_CACHE=/data/hf_cache

# ----------------------------------------------------------------------------
# Build-time only proxy
# Set these with --build-arg so apt-get and pip can reach the internet from
# behind a corporate proxy. They are cleared again below (ENV set to empty)
# before the final layers, so the proxy is NOT baked into the resulting
# image / used at container runtime.
#
# Build example:
#   docker build \
#     --build-arg HTTP_PROXY=http://proxy.example.com:3128 \
#     --build-arg HTTPS_PROXY=http://proxy.example.com:3128 \
#     --build-arg NO_PROXY=localhost,127.0.0.1 \
#     -t simpostcards .
# ----------------------------------------------------------------------------
ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY

ENV http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    no_proxy=${NO_PROXY} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}

ENV PIP_NO_CACHE_DIR=false

RUN apt-get update && apt-get install -y \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

# create working directory and install pip dependencies
RUN mkdir -p /data
COPY pyproject.toml /app/
COPY src/simpostcards /app/src/simpostcards
COPY src/libpostcards /app/src/libpostcards
COPY docker_simpostcards/postcards.conf /app/
WORKDIR /app

# Install the PyTorch/torchvision build matching the chosen backend *before*
# installing the project extras, so `similar`/`detection` (which just ask for
# a plain "torch") reuse this build instead of pip silently pulling the
# CPU-only wheel from PyPI.
RUN set -eux; \
    if [ "$TORCH_BACKEND" = "cuda" ]; then \
        INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"; \
        pip3 install torch torchvision --index-url "$INDEX_URL"; \
    elif [ "$TORCH_BACKEND" = "rocm" ]; then \
        INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/rocm6.4}"; \
        pip3 install torch torchvision --index-url "$INDEX_URL"; \
    else \
        pip3 install torch torchvision --index-url https://download.pytorch.org/whl/cpu; \
    fi

RUN pip3 install . && pip3 install .[similar] && \
    pip3 install .[deskew] && \
    pip3 install .[ocr] && \
    pip3 install .[travel] && \
    pip3 install .[detection] && \
    pip3 install .[flask] && \
    pip3 install .[prod]

# Proxy was only needed to fetch packages during the build above; clear it
# so it isn't set inside the container at runtime.
ENV http_proxy="" \
    https_proxy="" \
    no_proxy="" \
    HTTP_PROXY="" \
    HTTPS_PROXY="" \
    NO_PROXY=""

EXPOSE 8004

VOLUME ["/data"]

# run the flask server
CMD [ "sh", "-c", "gunicorn --preload --workers=${GUNICORN_WORKERS} --worker-class=${GUNICORN_WORKER_CLASS} --threads=${GUNICORN_THREADS} --timeout ${GUNICORN_TIMEOUT} --bind=0.0.0.0:8004 simpostcards.run:app"]
