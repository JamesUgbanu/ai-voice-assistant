# Voice service image: CPU-only Python, non-root runtime.
# Model weights are downloaded at FIRST RUN (not build time) so the image
# stays small and cacheable; pin versions in pyproject.toml.
FROM python:3.12-slim-bookworm AS base

# Cap glibc malloc arenas: the service is multithreaded (executor + torch +
# CTranslate2), and per-thread arenas fragment and inflate idle RSS that is
# never returned to the OS. Two arenas keeps fragmentation bounded.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    MALLOC_ARENA_MAX=2

WORKDIR /srv/voice

# System deps first (best layer cache behavior).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ffmpeg \
      libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# The package must exist for the editable install metadata. The rest of app/
# is copied after dependencies so routine code changes do not invalidate the
# (slow) dependency layer.
COPY app/__init__.py ./app/__init__.py
# CPU-only torch: the default PyPI index serves multi-GB CUDA wheels that
# this CPU service never uses. Install CPU torch explicitly first.
RUN pip install --no-cache-dir --timeout 120 --retries 10 \
        --index-url https://download.pytorch.org/whl/cpu torch==2.5.1 && \
    pip install --no-cache-dir --timeout 120 --retries 10 -e .

COPY app ./app
# The manifest is runtime data, not a Python package: verify the exact
# checkpoint/tokenizer the pinned config selects at model load.
COPY deploy/model-manifest.json ./deploy/model-manifest.json

# Optional voice-cloning reference. The original deployment stages an
# authorized recording at deploy/reference/voice-ref.wav (Git-ignored) and
# builds a PRIVATE image that bakes it in. A clean public checkout has only
# deploy/reference/.gitkeep, so the copy is skipped and preset TTS is used.
# PRIVACY: anyone able to pull an image containing the reference can extract
# it. Keep any such image private and do not use it for a third party's voice.
COPY deploy/reference/ /tmp/voice-reference/
RUN if [ -f /tmp/voice-reference/voice-ref.wav ]; then \
        mkdir -p /srv/voice/voice-reference/processed && \
        cp /tmp/voice-reference/voice-ref.wav /srv/voice/voice-reference/processed/voice-ref.wav; \
    fi; \
    rm -rf /tmp/voice-reference

RUN getent group voice >/dev/null || groupadd -r voice; \
    id -u voice >/dev/null 2>&1 || useradd -r -g voice --create-home --shell /usr/sbin/nologin voice && \
    mkdir -p /srv/voice/data /srv/voice/models && \
    chown -R voice:voice /srv/voice
USER voice

ENV VOICE_DB_PATH=/srv/voice/data/voice.sqlite3 \
    VOICE_MODEL_MANIFEST=/srv/voice/deploy/model-manifest.json \
    HF_HOME=/srv/voice/models \
    XDG_CACHE_HOME=/srv/voice/models

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
