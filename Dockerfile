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
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md __init__.py ./
COPY hippo/ ./hippo/
COPY schema/ ./schema/
COPY --from=frontend-build /app/frontend/dist ./frontend/dist

RUN pip install --upgrade pip setuptools wheel \
    && pip install .

EXPOSE 8000

ENTRYPOINT ["hippo"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000", "--static-dir", "/app/frontend/dist"]
