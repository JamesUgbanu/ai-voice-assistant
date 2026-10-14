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
# CPU-only torch: the default PyPI index serves multi-GB CUDA wheels that
# this CPU service never uses. Install CPU torch explicitly first.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch && \
    pip install --no-cache-dir -e .

COPY app ./app

RUN getent group voice >/dev/null || groupadd -r voice; \
    id -u voice >/dev/null 2>&1 || useradd -r -g voice --create-home --shell /usr/sbin/nologin voice && \
    mkdir -p /srv/voice/data /srv/voice/models && \
    chown -R voice:voice /srv/voice
USER voice

ENV VOICE_DB_PATH=/srv/voice/data/voice.sqlite3 \
    HF_HOME=/srv/voice/models \
    XDG_CACHE_HOME=/srv/voice/models

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
