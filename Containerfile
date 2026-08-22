# Debian slim keeps OpenCV's shared-library dependencies simple.
# Pin the patch version so rebuilds are reproducible; bump deliberately.
FROM python:3.14.6-slim

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
#
# The lock file carries a SHA-256 for every artefact in the resolved tree, and
# --require-hashes turns that into an enforced check: an index that serves a
# different wheel than the one this lock was compiled against -- compromised,
# MITM'd, or a maintainer force-replacing a release -- fails the build instead
# of shipping into the image. It also implies --no-deps, so nothing outside the
# lock can be pulled in.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

# Application code stays owned by root: the unprivileged runtime user can read
# and execute it but cannot modify it.
COPY main.py card_metadata.py convert_to_png.py ./
COPY src/ ./src/

# The layered code lives under src/ and is imported as `nivel.…`. The entry
# points at /app are run by path (`python main.py`), which only ever puts /app
# itself on sys.path, so the package directory has to be named here.
ENV PYTHONPATH=/app/src

# Only the mounted data directories are writable by the runtime user.
# Scrape state lives in PostgreSQL, so there is no database file to host here.
RUN mkdir -p /app/downloads /app/processed \
    && chown -R scraperuser:scraperuser /app/downloads /app/processed

USER scraperuser

CMD ["python", "main.py"]
