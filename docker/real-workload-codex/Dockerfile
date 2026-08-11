FROM node:22-slim@sha256:d649c27dae7ba0137b3cef5dd75baa422c08dc3d9e3fc0c23dfb172dc3cc6436

ARG CODEX_VERSION=0.147.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git python3 ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN npm install --global "@openai/codex@${CODEX_VERSION}" \
    && npm cache clean --force

RUN groupadd --gid 65532 benchmark \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin benchmark \
    && mkdir --parents /opt/benchmark-agent /run/credentials \
    && chown --recursive 65532:65532 /opt/benchmark-agent /run/credentials

COPY --chown=65532:65532 memoryos/evaluation/codex_benchmark_agent.py \
    /opt/benchmark-agent/codex_benchmark_agent.py

RUN ["codex", "--version"]

USER 65532:65532
ENTRYPOINT []
