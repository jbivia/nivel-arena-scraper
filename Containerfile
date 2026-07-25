# Debian slim keeps OpenCV's shared-library dependencies simple.
# Pin the patch version so rebuilds are reproducible; bump deliberately.
FROM python:3.13.14-slim

# Fail fast, log immediately, and keep no bytecode cache in the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN adduser --disabled-password --gecos "" --uid 1000 scraperuser

WORKDIR /app

# Runtime shared libraries required by opencv-python-headless.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first so the layer caches across code edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Application code stays owned by root: the unprivileged runtime user can read
# and execute it but cannot modify it.
COPY main.py card_metadata.py convert_to_png.py ./

# Only the mounted data directories are writable by the runtime user.
# Scrape state lives in PostgreSQL, so there is no database file to host here.
RUN mkdir -p /app/downloads /app/processed \
    && chown -R scraperuser:scraperuser /app/downloads /app/processed

USER scraperuser

CMD ["python", "main.py"]
