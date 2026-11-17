FROM ghcr.io/astral-sh/uv:0.11.23 AS uv
FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.14-slim

ARG YTDLP_VERSION=2026.10.26
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv PATH="/opt/venv/bin:$PATH" \
    DATA_DIR=/app/data DENO_DIR=/tmp/deno-cache DENO_NO_UPDATE_CHECK=1 DENO_NO_PROMPT=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
LABEL org.opencontainers.image.source="https://github.com/andreichernov201/ThreadDock" \
      org.opencontainers.image.title="ThreadDock" \
      org.opencontainers.image.licenses="MIT"
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev \
    && uv pip install --python /opt/venv/bin/python "yt-dlp[default,curl-cffi]==${YTDLP_VERSION}"
COPY . .
RUN useradd --uid 10001 --create-home bot \
    && mkdir -p /app/data && chown bot:bot /app/data \
    && python main.py --check --no-token
USER bot
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 CMD ["python", "health.py"]
CMD ["python", "main.py"]
