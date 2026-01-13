FROM python:3.11-slim

LABEL maintainer="wechatcli"
LABEL description="WeChat article exporter CLI"

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Create app directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml README.md ./
COPY wechatcli/ ./wechatcli/
COPY __init__.py normalize_html.py ./

# Install Python dependencies
RUN pip install --upgrade pip setuptools wheel && \
    pip install -e .

# Create data directory
RUN mkdir -p /data

# Set data directory as volume
VOLUME ["/data"]

# Set environment variable for data directory
ENV WECHATCLI_HOME=/data

# Set entrypoint
ENTRYPOINT ["wechatcli"]
CMD ["--help"]
