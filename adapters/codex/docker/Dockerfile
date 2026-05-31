FROM node:22-bookworm

RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    bash \
    ca-certificates \
    curl \
    git \
    python3 \
    python3-venv \
    rsync \
  && rm -rf /var/lib/apt/lists/*

ARG CODEX_VERSION=latest
RUN npm install -g "@openai/codex@${CODEX_VERSION}"
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

ENV PATH="/root/.local/bin:${PATH}"
ENV CODEX_HOME=/root/.codex
ENV HOST_CODEX_HOME=/host-codex
ENV POWERPACKS_REPO_ROOT=/workspace/powerpacks
ENV HEARTBEAT_INTERVAL_SECONDS=300

WORKDIR /workspace/powerpacks
COPY scripts/codex-heartbeat.sh /usr/local/bin/powerpacks-codex-heartbeat

ENTRYPOINT ["/usr/local/bin/powerpacks-codex-heartbeat"]
