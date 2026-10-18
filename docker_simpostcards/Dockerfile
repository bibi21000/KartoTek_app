# syntax=docker/dockerfile:1

# ============================================================================
# Stage 1: builder — compiles COLMAP and installs all Python deps into a venv.
# Nothing from this stage's apt packages (cmake, Qt/Boost -dev, CUDA/ROCm
# toolkit, deadsnakes PPA...) ends up in the final image; only the compiled
# artifacts do.
#
# Base switched from python:3.14-trixie (Debian) to ubuntu:24.04: ROCm
# officially supports Ubuntu Noble, not Debian. Python 3.14 isn't in Ubuntu
# 24.04's own archive (it ships 3.12), so it's installed from the
# deadsnakes PPA below instead.
# ============================================================================
FROM ubuntu:24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# ----------------------------------------------------------------------------
# GPU backend selection
#   - cpu   : no GPU, PyTorch CPU-only wheels, COLMAP built without CUDA/HIP
#   - cuda  : NVIDIA GPUs, PyTorch CUDA wheels + COLMAP built with CUDA
#             (requires the NVIDIA Container Toolkit on the host and
#             `docker run --gpus all ...`)
#   - rocm  : AMD GPUs, PyTorch ROCm wheels + COLMAP built with HIP
#             (requires the ROCm kernel driver on the host and
#             `docker run --device=/dev/kfd --device=/dev/dri --group-add video ...`)
#             NOTE: COLMAP's HIP backend currently accelerates dense
#             reconstruction (patch_match_stereo) only, not SIFT
#             extraction/matching -- see colmap.github.io/install.html.
#
# Build examples:
#   docker build --build-arg TORCH_BACKEND=cuda --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 -t simpostcards:cuda .
#   docker build --build-arg TORCH_BACKEND=rocm --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/rocm6.4 -t simpostcards:rocm .
#   docker build -t simpostcards:cpu .   # default, no GPU
# ----------------------------------------------------------------------------
ARG TORCH_BACKEND=cpu
ARG TORCH_INDEX_URL=""

# COLMAP build config.
#   - COLMAP_VERSION: pinned release tag. Check
#     https://github.com/colmap/colmap/releases before bumping.
#   - CUDA_ARCHITECTURES: only used when TORCH_BACKEND=cuda. Covers Turing
#     through Ada/Hopper consumer+datacenter GPUs. Trim to your actual
#     target GPU(s) to cut build time significantly (a full CUDA kernel
#     build for every arch below can take 30-60+ min).
#   - HIP_ARCHITECTURES: only used when TORCH_BACKEND=rocm. Defaulted here
#     to gfx1150 (Radeon 890M / "Strix Point" RDNA 3.5 iGPU). IMPORTANT:
#     COLMAP's dense reconstruction (patch_match_stereo) uses HIP texture
#     objects (tex2D/tex2DLayered), which only exist on RDNA GPUs with
#     texture-sampling hardware -- NOT on CDNA/compute GPUs (gfx90a=MI200,
#     gfx942=MI300, no texture units at all). Building for a CDNA target
#     fails at compile time with "unavailable: The image/texture API is
#     not supported on the device" -- this is a hardware limitation, not
#     a build misconfiguration, and isn't fixable from this Dockerfile.
#     Other RDNA references: gfx1030=RDNA2, gfx1100=RDNA3 desktop,
#     gfx1151=Strix Halo (Radeon 8060S).
#   - ROCM_VERSION: pinned to the legacy (pre-"ROCm Core SDK"/TheRock)
#     release stream, whose package names (hip-dev, hipcc, rocm-llvm,
#     hiprand-dev, rocrand-dev, rocm-core) this Dockerfile relies on.
#     "latest" on repo.radeon.com now serves the newer, differently-
#     packaged Core SDK (amdrocm-* names, /opt/rocm/core-X.Y layout) --
#     do not switch to "latest" without rewriting the ROCm blocks below.
ARG COLMAP_VERSION=4.2.1
ARG CUDA_ARCHITECTURES="75;80;86;89;90"
ARG HIP_ARCHITECTURES="gfx1150"
ARG ROCM_VERSION=6.4

ARG PYTHON_VERSION=3.14
# PaddleOCR (moteur OCR alternatif, choisi par les clients avec [tkimport]
# ocr_engine = paddleocr). Désactivé par défaut : PaddlePaddle n'a pas de roue
# pour Python 3.14. Pour l'activer :
#   --build-arg PYTHON_VERSION=3.13 --build-arg WITH_PADDLEOCR=1
ARG WITH_PADDLEOCR=0
ARG PADDLE_INDEX_URL=https://www.paddlepaddle.org.cn/packages/stable/cpu/

# ----------------------------------------------------------------------------
# Build-time only proxy
# Two mechanisms, both build-arg driven and both scoped to this stage only
# (never copied into the runtime stage, never baked into the final image):
#   1. http_proxy/https_proxy env vars -- honored by pip, wget, curl, git.
#   2. /etc/apt/apt.conf.d/01proxy -- apt does not reliably pick up the env
#      vars in every code path, so this apt-native config is the
#      belt-and-suspenders way to make sure `apt-get` itself uses the proxy.
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

RUN set -eux; \
    if [ -n "${HTTP_PROXY:-}" ] || [ -n "${HTTPS_PROXY:-}" ]; then \
        { \
            [ -n "${HTTP_PROXY:-}" ]  && echo "Acquire::http::Proxy  \"${HTTP_PROXY}\";"; \
            [ -n "${HTTPS_PROXY:-}" ] && echo "Acquire::https::Proxy \"${HTTPS_PROXY}\";"; \
        } > /etc/apt/apt.conf.d/01proxy; \
    fi

# ENV PIP_NO_CACHE_DIR=false
ENV PIP_PROXY=${HTTPS_PROXY:-${HTTP_PROXY}} \
    PIP_DEFAULT_TIMEOUT=300 \
    PIP_RETRIES=10

# Python 3.14 from deadsnakes (not in Ubuntu 24.04's own archive, which
# ships 3.12). software-properties-common provides add-apt-repository.
RUN apt-get update && apt-get install -y \
    software-properties-common ca-certificates gnupg wget curl git \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y \
    python${PYTHON_VERSION} python${PYTHON_VERSION}-venv python${PYTHON_VERSION}-dev \
    && rm -rf /var/lib/apt/lists/*

# Build-time system deps: compiler toolchain + everything COLMAP links
# against (per https://colmap.github.io/install.html, Debian/Ubuntu
# section). All -dev packages here (headers, static libs, cmake configs)
# are gone once the builder stage is discarded.
RUN apt-get update && apt-get install -y \
    cmake ninja-build build-essential \
    libboost-program-options-dev libboost-graph-dev libboost-system-dev \
    libeigen3-dev libopenimageio-dev openimageio-tools libmetis-dev \
    libgoogle-glog-dev libgtest-dev libgmock-dev libsqlite3-dev \
    libglew-dev libcgal-dev libceres-dev libsuitesparse-dev \
    libcurl4-openssl-dev libssl-dev \
    qtbase5-dev libqt5opengl5-dev libqt5svg5-dev \
    && rm -rf /var/lib/apt/lists/*
# Workaround: the openimageio CMake config references an OpenCV include
# dir even though we don't use any OpenCV functionality of openimageio.
RUN mkdir -p /usr/include/opencv4

# CUDA toolkit (nvcc + headers) -- only needed to compile COLMAP's own
# .cu kernels. The PyTorch CUDA wheels installed further below bundle
# their own CUDA runtime and don't provide nvcc, so they don't satisfy
# this requirement on their own.
RUN set -eux; \
    if [ "$TORCH_BACKEND" = "cuda" ]; then \
        apt-get update && apt-get install -y nvidia-cuda-toolkit \
        && rm -rf /var/lib/apt/lists/*; \
    fi

# ROCm/HIP (AMD GPUs), needed to compile COLMAP's HIP kernels. Pinned to
# ROCM_VERSION (legacy package stream: hip-dev, hipcc, rocm-llvm,
# hiprand-dev, rocrand-dev, rocm-core), on the Ubuntu Noble repo since
# ROCm officially supports Noble.
#   - The apt pin below is required: Ubuntu 24.04's own "universe"
#     component ships an old, unrelated community "hipcc" package
#     (rocm-hipamd source, ~v5.7) under the exact same package name.
#     Without pinning, apt can silently prefer that one over AMD's
#     official repo.radeon.com build -- it installs fine, but not under
#     /opt/rocm-<ver>/bin, which is exactly what caused the earlier
#     "hip_HIPCC_EXECUTABLE does not exist" failures to persist even
#     after adding hipcc to the install list. This pin (documented by
#     AMD's own install guide) forces repo.radeon.com to win for every
#     package name it provides.
#   - rocm-core is normally what sets up the /opt/rocm -> /opt/rocm-x.y.z
#     symlink via update-alternatives -- BUT this is a known broken
#     regression in ROCm 6.4 packaging (worked in 6.2.3, silently dropped
#     in 6.4, see AMD/community reports). So we don't rely on the package
#     to do it: the RUN block below force-(re)creates the symlink itself
#     by detecting whatever /opt/rocm-* directory actually got installed.
#     This is a no-op if the packaged symlink is fine, and a fix if it
#     isn't -- safe either way, and future-proof if AMD un-breaks it.
#   - rocm-llvm provides /opt/rocm/llvm/bin/clang++, referenced by
#     -DCMAKE_HIP_COMPILER below.
RUN set -eux; \
    if [ "$TORCH_BACKEND" = "rocm" ]; then \
        mkdir -p /etc/apt/keyrings; \
        wget https://repo.radeon.com/rocm/rocm.gpg.key -O - | gpg --dearmor -o /etc/apt/keyrings/rocm.gpg; \
        echo "deb [signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${ROCM_VERSION} noble main" \
            > /etc/apt/sources.list.d/rocm.list; \
        printf 'Package: *\nPin: release o=repo.radeon.com\nPin-Priority: 600\n' \
            > /etc/apt/preferences.d/rocm-pin-600; \
        apt-get update && apt-get install -y \
            rocm-core hip-runtime-amd hip-dev hipcc rocm-llvm hiprand-dev rocrand-dev \
        && rm -rf /var/lib/apt/lists/*; \
        ROCM_DIR="$(find /opt -maxdepth 1 -name 'rocm-*' -type d | sort -V | tail -n1)"; \
        [ -n "$ROCM_DIR" ]; \
        ln -sfn "$ROCM_DIR" /opt/rocm; \
        test -x /opt/rocm/bin/hipcc; \
    fi
ENV PATH="/opt/rocm/bin:${PATH}"

# Build and install COLMAP into its own prefix so we can cleanly harvest
# just what's needed for the runtime stage. CUDA_ENABLED and HIP_ENABLED
# are mutually exclusive; cpu gets neither.
RUN set -eux; \
    if [ "$TORCH_BACKEND" = "cuda" ]; then \
        COLMAP_GPU_FLAGS="-DCUDA_ENABLED=ON -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCHITECTURES}"; \
    elif [ "$TORCH_BACKEND" = "rocm" ]; then \
        COLMAP_GPU_FLAGS="-DCUDA_ENABLED=OFF -DHIP_ENABLED=ON -DCMAKE_HIP_ARCHITECTURES=${HIP_ARCHITECTURES} -DCMAKE_HIP_COMPILER=/opt/rocm/llvm/bin/clang++"; \
    else \
        COLMAP_GPU_FLAGS="-DCUDA_ENABLED=OFF"; \
    fi; \
    git clone --branch ${COLMAP_VERSION} --depth 1 https://github.com/colmap/colmap.git /tmp/colmap; \
    mkdir -p /tmp/colmap/build; \
    cd /tmp/colmap/build; \
    cmake .. -GNinja \
        -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/opt/colmap \
        -DGUI_ENABLED=OFF \
        -DTESTS_ENABLED=OFF \
        ${COLMAP_GPU_FLAGS}; \
    ninja; \
    ninja install; \
    cd /; rm -rf /tmp/colmap

# Harvest the runtime .so's colmap actually needs (skip the libc/libm/
# ld-linux family already present in the base image) instead of guessing
# exact non-dev package/version names, which drift between Ubuntu point
# releases.
RUN set -eux; \
    mkdir -p /opt/colmap-libs; \
    ldd /opt/colmap/bin/colmap | awk '{print $3}' | grep '^/' | \
        grep -Ev '^/(usr/)?lib(64)?/(x86_64-linux-gnu/)?(libc|libm|libdl|librt|libpthread|ld-linux)' | \
        xargs -I{} cp -L {} /opt/colmap-libs/ 2>/dev/null || true

# ---- Python venv, isolated so it's a single COPY into the runtime stage ----
RUN python${PYTHON_VERSION} -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY pyproject.toml /app/
COPY src/simpostcards /app/src/simpostcards
COPY src/libpostcards /app/src/libpostcards
COPY docker_simpostcards/postcards.conf /app/
WORKDIR /app

# Install the PyTorch/torchvision build matching the chosen backend *before*
# installing the project extras, so `similar`/`detection` (which just ask
# for a plain "torch") reuse this build instead of pip silently pulling the
# CPU-only wheel from PyPI.
RUN set -eux; \
    if [ "$TORCH_BACKEND" = "cuda" ]; then \
        INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"; \
        pip install torch torchvision --index-url "$INDEX_URL"; \
    elif [ "$TORCH_BACKEND" = "rocm" ]; then \
        INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/rocm6.4}"; \
        pip install torch torchvision --index-url "$INDEX_URL"; \
    else \
        pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu; \
    fi

# PaddlePaddle : build CPU, volontairement. Ses roues pip n'existent qu'en CPU
# et en NVIDIA CUDA ; il n'y en a PAS pour AMD (ROCm), qui demande une
# compilation depuis les sources (non testée sur un iGPU RDNA comme le
# gfx1150). L'OCR PaddleOCR tourne donc sur le CPU (paddleocr_device = cpu) ;
# le GPU reste à torch (CLIP/BLIP/DETR/SAM, traduction Marian) et Open WebUI.
# --extra-index-url plutôt que -i : les dépendances (numpy, protobuf...)
# viennent de PyPI.
RUN set -eux; \
    if [ "$WITH_PADDLEOCR" = "1" ]; then \
        case "$PYTHON_VERSION" in \
            3.9|3.10|3.11|3.12|3.13) ;; \
            *) echo "WITH_PADDLEOCR=1 needs Python 3.9-3.13 (no PaddlePaddle wheel for ${PYTHON_VERSION}): rebuild with --build-arg PYTHON_VERSION=3.13" >&2; exit 1 ;; \
        esac; \
        pip install paddlepaddle --extra-index-url "$PADDLE_INDEX_URL"; \
    fi

RUN pip install . && \
    pip install .[similar] && \
    pip install .[deskew] && \
    pip install .[ocr] && \
    if [ "$WITH_PADDLEOCR" = "1" ]; then pip install .[paddleocr]; fi && \
    pip install .[travel] && \
    pip install .[detection] && \
    pip install .[translate-local] && \
    pip install .[masks] && \
    pip install .[colmap] && \
    pip install .[flask] && \
    pip install .[prod]

# paddleocr tire opencv-contrib-python (version graphique d'OpenCV, qui a besoin
# de libGL/libglib/libxcb, absents de l'image finale : `import cv2` y échouerait)
# et cohabite mal avec opencv-python-headless de l'extra masks (même dossier
# cv2/ : la dernière variante installée écrase les autres). Une seule build :
RUN set -eux; \
    if [ "$WITH_PADDLEOCR" = "1" ]; then \
        pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless opencv-contrib-python-headless; \
        pip install "opencv-contrib-python-headless==4.10.0.84"; \
        python -c "import cv2, paddle, paddleocr; print('cv2', cv2.__version__)"; \
    fi

# ============================================================================
# Stage 2: runtime — small final image, no build toolchain.
# ============================================================================
FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive

# Re-declared: ARGs don't cross stages automatically, and the runtime
# stage needs to know which GPU runtime libs (if any) to pull in, which
# Python version to install (must match the venv from builder), and which
# ROCm release to match.
ARG TORCH_BACKEND=cpu
ARG PYTHON_VERSION=3.14
ARG ROCM_VERSION=6.4

ARG HTTP_PROXY
ARG HTTPS_PROXY
ARG NO_PROXY
ENV http_proxy=${HTTP_PROXY} \
    https_proxy=${HTTPS_PROXY} \
    no_proxy=${NO_PROXY} \
    HTTP_PROXY=${HTTP_PROXY} \
    HTTPS_PROXY=${HTTPS_PROXY} \
    NO_PROXY=${NO_PROXY}
RUN set -eux; \
    if [ -n "${HTTP_PROXY:-}" ] || [ -n "${HTTPS_PROXY:-}" ]; then \
        { \
            [ -n "${HTTP_PROXY:-}" ]  && echo "Acquire::http::Proxy  \"${HTTP_PROXY}\";"; \
            [ -n "${HTTPS_PROXY:-}" ] && echo "Acquire::https::Proxy \"${HTTPS_PROXY}\";"; \
        } > /etc/apt/apt.conf.d/01proxy; \
    fi

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
# under the /data volume declared below, instead of the container's own
# (ephemeral) filesystem: without this, every container recreate
# re-downloads every model from scratch on its first request -- slow,
# and wasteful of the HF Hub's anonymous rate limit.
# TRANSFORMERS_CACHE is kept too for older transformers releases that
# don't yet honor HF_HOME.
ENV HF_HOME=/data/hf_cache \
    TRANSFORMERS_CACHE=/data/hf_cache \
    PADDLE_PDX_CACHE_HOME=/data/paddlex_cache

# Python 3.14 runtime interpreter (must match the venv baked in the
# builder stage -- a venv only symlinks to the system interpreter, it
# doesn't embed one). Only the interpreter is needed here, not -dev/-venv.
RUN apt-get update && apt-get install -y software-properties-common ca-certificates \
    && add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y python${PYTHON_VERSION} \
    && rm -rf /var/lib/apt/lists/*

# Runtime-only OS deps: tesseract for OCR, plus the GPU runtime (no
# compiler/headers) matching whatever backend was built above -- colmap's
# harvested .so's still need a matching libcudart / HIP runtime present.
# Same apt-pin and rocm-core symlink caveats as the builder stage apply
# here too (Ubuntu universe "hipcc"/ROCm 6.4 symlink regression) -- see
# the long comment in that stage.
RUN set -eux; \
    apt-get update && apt-get install -y libgomp1 \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-deu tesseract-ocr-spa; \
    if [ "$TORCH_BACKEND" = "cuda" ]; then \
        apt-get install -y nvidia-cuda-toolkit; \
    elif [ "$TORCH_BACKEND" = "rocm" ]; then \
        apt-get install -y wget gnupg; \
        mkdir -p /etc/apt/keyrings; \
        wget https://repo.radeon.com/rocm/rocm.gpg.key -O - | gpg --dearmor -o /etc/apt/keyrings/rocm.gpg; \
        echo "deb [signed-by=/etc/apt/keyrings/rocm.gpg] https://repo.radeon.com/rocm/apt/${ROCM_VERSION} noble main" \
            > /etc/apt/sources.list.d/rocm.list; \
        printf 'Package: *\nPin: release o=repo.radeon.com\nPin-Priority: 600\n' \
            > /etc/apt/preferences.d/rocm-pin-600; \
        apt-get update && apt-get install -y rocm-core hip-runtime-amd rocrand; \
        ROCM_DIR="$(find /opt -maxdepth 1 -name 'rocm-*' -type d | sort -V | tail -n1)"; \
        [ -n "$ROCM_DIR" ]; \
        ln -sfn "$ROCM_DIR" /opt/rocm; \
    fi; \
    rm -rf /var/lib/apt/lists/*
ENV LD_LIBRARY_PATH=/opt/rocm/lib

# gfx1150 (Radeon 890M / Strix Point) is a fairly recent mobile iGPU that
# the ROCm *runtime* (as opposed to the compiler, which does know gfx1150)
# doesn't always recognize as officially supported depending on the ROCm
# version -- this makes it identify as the closest officially-supported
# RDNA3 target (gfx1100) at runtime so hipInit()/patch_match_stereo can
# actually find and use the GPU. Only applies when TORCH_BACKEND=rocm;
# harmless to leave set for cpu/cuda images since HIP is simply unused
# there. If a future ROCm runtime adds native gfx1150 support and this
# override starts causing issues instead of fixing them, remove it.
ENV HSA_OVERRIDE_GFX_VERSION=11.0.0

# Proxy was only needed to fetch packages during the builds above; clear
# it (env vars and apt conf) so it isn't set inside the final image at
# container runtime.
RUN rm -f /etc/apt/apt.conf.d/01proxy
ENV http_proxy="" \
    https_proxy="" \
    no_proxy="" \
    HTTP_PROXY="" \
    HTTPS_PROXY="" \
    NO_PROXY=""

RUN mkdir -p /data

# COLMAP binary + its harvested runtime libs, restored to their original
# absolute paths (cp --parents in the builder stage preserved them).
COPY --from=builder /opt/colmap/bin/colmap /usr/local/bin/colmap
COPY --from=builder /opt/colmap/lib/ /usr/local/lib/
COPY --from=builder /opt/colmap-libs/ /usr/local/lib/
ENV LD_LIBRARY_PATH=/usr/local/lib:${LD_LIBRARY_PATH}

# App + all Python deps (torch included), already installed in the venv.
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

COPY --from=builder /app /app
WORKDIR /app

EXPOSE 8004
VOLUME ["/data"]

# run the flask server
CMD [ "sh", "-c", "gunicorn --preload --workers=${GUNICORN_WORKERS} --worker-class=${GUNICORN_WORKER_CLASS} --threads=${GUNICORN_THREADS} --timeout ${GUNICORN_TIMEOUT} --bind=0.0.0.0:8004 simpostcards.run:app"]
