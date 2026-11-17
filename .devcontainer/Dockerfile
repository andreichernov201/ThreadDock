FROM ghcr.io/astral-sh/uv:0.13.9 AS uv
FROM denoland/deno:bin-2.9.8 AS deno
FROM node:24-bookworm-slim AS node
FROM python:3.15-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git gh openssh-client ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
COPY --from=deno /deno /usr/local/bin/deno
COPY --from=node /usr/local/ /usr/local/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
