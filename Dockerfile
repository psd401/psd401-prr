FROM oven/bun:1 AS frontend-build
WORKDIR /frontend
COPY frontend/package.json frontend/bun.lock ./
RUN bun install --frozen-lockfile
COPY frontend/ .
RUN bun run build

FROM python:3.12-slim
ARG PRR_RELEASE_SHA
RUN case "$PRR_RELEASE_SHA" in (""|*[!0-9a-f]*) exit 64;; esac \
    && test "${#PRR_RELEASE_SHA}" -eq 40
LABEL org.opencontainers.image.revision=$PRR_RELEASE_SHA
ENV PYTHONUNBUFFERED=1
WORKDIR /app
# libreoffice-* renders Office documents into the reviewable PDF that the
# redaction burn-in needs. Without it every .docx/.xlsx in a collection can be
# stored and given a review verdict but never actually read or redacted.
# Only writer/calc/impress rather than the full suite: it is a large
# dependency that runs on untrusted input, so it gets only the filters the
# record formats require. fonts-liberation keeps converted pagination stable
# instead of falling back to whatever font happens to exist.
# ffmpeg inspects and re-encodes audio and video records. Redacting footage
# means re-encoding: a stream copy would change the container while leaving
# the frames that were meant to be removed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ghostscript \
        qpdf \
        tesseract-ocr \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        fonts-liberation \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*
# The image installs from the committed uv.lock, so production runs exactly
# the versions CI tested. `--locked` fails the build when uv.lock has drifted
# from pyproject.toml, and `--no-dev` leaves the CI-only security tooling out
# of the image.
# Digest-pinned, not tag-pinned: this binary executes during the build, and a
# registry tag is mutable. The tag stays beside the digest for readers; the
# digest is what Docker resolves.
COPY --from=ghcr.io/astral-sh/uv:0.13.1@sha256:4a1f3666816dafadd20ceb71eac9a7d56d7ef30279f7d64048519ab901ada336 /uv /bin/uv
# UV_PYTHON pins this image's own interpreter and UV_PYTHON_DOWNLOADS=never
# forbids fetching another, so a base-image bump fails the build instead of
# being papered over by uv installing a different Python. The environment goes
# to /opt/venv rather than the default /app/.venv so that the `COPY . .` below
# cannot reach it. UV_NO_CACHE keeps uv's cache out of the layer.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/local/bin/python3.12 \
    UV_PYTHON_DOWNLOADS=never \
    UV_NO_CACHE=1 \
    PATH=/opt/venv/bin:$PATH
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev
COPY . .
COPY --from=frontend-build /frontend/dist frontend/dist
RUN DJANGO_SECRET_KEY=build-only-high-entropy-placeholder-not-present-at-runtime-1234567890 \
    DJANGO_DEBUG=false python manage.py collectstatic --noinput
ENTRYPOINT ["docker/entrypoint.sh"]
CMD ["gunicorn", "-k", "uvicorn.workers.UvicornWorker", "config.asgi:application", "--bind", "0.0.0.0:8000", "--workers", "4", "--timeout", "300"]
