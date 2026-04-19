# Multi-stage Dockerfile for the Polymarket momentum bot.
# Targets ECS Fargate. Runs as a non-root user. Never bakes in secrets —
# wallet credentials come from AWS Secrets Manager at runtime.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DRY_RUN=true

# System deps: only what requests/boto3 need. Keep the image small.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt boto3

# Copy source and install the package.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-deps .

# Drop privileges.
RUN useradd --create-home --shell /bin/bash bot
USER bot

# `--once` is the default for EventBridge Scheduler invocations. Override
# with `CMD ["polymarket-momentum-bot"]` (no args) for always-on service mode.
ENTRYPOINT ["polymarket-momentum-bot"]
CMD ["--once", "--env-file", ""]
