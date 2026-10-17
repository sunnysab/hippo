FROM node:24-slim AS frontend-build

WORKDIR /app/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim

LABEL maintainer="hippo"
LABEL description="Hippo WeChat article service"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# build-essential/libpq-dev: C extensions. poppler-utils (pdftotext/pdfinfo) and
# util-linux (prlimit) are the knowledge-base PDF parser's external tools.
# ffmpeg/ffprobe back the media pipeline (slicing, transcoding, archiving).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential libpq-dev poppler-utils util-linux ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock README.md __init__.py ./
COPY hippo/ ./hippo/
COPY schema/ ./schema/
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

RUN uv sync --frozen --no-dev

EXPOSE 8000

ENTRYPOINT ["/app/.venv/bin/python", "-m", "hippo.server"]
CMD ["--host", "0.0.0.0", "--port", "8000", "--static-dir", "/app/frontend/dist"]
