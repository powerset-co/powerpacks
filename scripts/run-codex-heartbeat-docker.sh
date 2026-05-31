#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-start}"
IMAGE_NAME="${POWERPACKS_CODEX_HEARTBEAT_IMAGE:-powerpacks-codex-heartbeat}"
CONTAINER_NAME="${POWERPACKS_CODEX_HEARTBEAT_CONTAINER:-powerpacks-codex-heartbeat}"
HOST_CODEX_HOME="${HOST_CODEX_HOME:-${CODEX_HOME:-$HOME/.codex}}"
CONTAINER_CODEX_HOME_VOLUME="${POWERPACKS_CODEX_CONTAINER_HOME_VOLUME:-powerpacks-codex-home}"
CONTAINER_CACHE_VOLUME="${POWERPACKS_CODEX_CACHE_VOLUME:-powerpacks-codex-cache}"
CODEX_HOME_MODE="${POWERPACKS_CODEX_HOME_MODE:-snapshot}"
HEARTBEAT_INTERVAL_SECONDS="${HEARTBEAT_INTERVAL_SECONDS:-300}"

usage() {
  cat <<'USAGE'
Usage: scripts/run-codex-heartbeat-docker.sh [build|start|once|stop|restart|status|logs|help]

Starts a Docker-managed Codex heartbeat worker for this Powerpacks checkout.

Default login sharing is safe snapshot mode:
  - host Codex home (${CODEX_HOME:-$HOME/.codex}) mounts read-only at /host-codex
  - container Codex home (/root/.codex) is a separate writable Docker volume
  - startup copies host login/config into the container volume

Environment:
  HOST_CODEX_HOME                         Host Codex home to share (default: ${CODEX_HOME:-$HOME/.codex})
  POWERPACKS_CODEX_HOME_MODE              snapshot (default) or direct
  POWERPACKS_CODEX_HEARTBEAT_IMAGE        Docker image name
  POWERPACKS_CODEX_HEARTBEAT_CONTAINER    Docker container name
  POWERPACKS_CODEX_CONTAINER_HOME_VOLUME  Docker volume for container ~/.codex in snapshot mode
  POWERPACKS_CODEX_CACHE_VOLUME           Docker volume for Powerpacks uv/cache state
  HEARTBEAT_INTERVAL_SECONDS              Delay between heartbeat runs
  CODEX_HEARTBEAT_PROMPT                  Prompt passed to codex exec
  OPENAI_API_KEY                          Optional API key alternative to Codex login

Use POWERPACKS_CODEX_HOME_MODE=direct only if you intentionally want the
container to mount and write to your host Codex home directly.
USAGE
}

if [[ "$ACTION" == "help" || "$ACTION" == "--help" || "$ACTION" == "-h" ]]; then
  usage
  exit 0
fi

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: docker is required" >&2
    exit 1
  fi
}

build_image() {
  require_docker
  docker build -f "$ROOT/adapters/codex/docker/Dockerfile" -t "$IMAGE_NAME" "$ROOT"
}

container_exists() {
  docker container inspect "$CONTAINER_NAME" >/dev/null 2>&1
}

container_running() {
  [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]
}

codex_home_mounts() {
  if [[ "$CODEX_HOME_MODE" == "direct" ]]; then
    printf '%s\0' --mount "type=bind,source=$HOST_CODEX_HOME,target=/root/.codex"
    printf '%s\0' -e "POWERPACKS_SYNC_HOST_CODEX_HOME=0"
  elif [[ "$CODEX_HOME_MODE" == "snapshot" ]]; then
    if [[ -d "$HOST_CODEX_HOME" ]]; then
      printf '%s\0' --mount "type=bind,source=$HOST_CODEX_HOME,target=/host-codex,readonly"
      printf '%s\0' -e "POWERPACKS_SYNC_HOST_CODEX_HOME=1"
    else
      printf '%s\0' -e "POWERPACKS_SYNC_HOST_CODEX_HOME=0"
    fi
    printf '%s\0' -v "$CONTAINER_CODEX_HOME_VOLUME:/root/.codex"
  else
    echo "error: POWERPACKS_CODEX_HOME_MODE must be snapshot or direct" >&2
    exit 2
  fi
}

pass_env_if_set() {
  local name="$1"
  if [[ -n "${!name+x}" ]]; then
    printf '%s\0' -e "$name=${!name}"
  fi
}

run_args() {
  printf '%s\0' \
    -e "CODEX_HOME=/root/.codex" \
    -e "HOST_CODEX_HOME=/host-codex" \
    -e "POWERPACKS_REPO_ROOT=/workspace/powerpacks" \
    -e "HEARTBEAT_INTERVAL_SECONDS=$HEARTBEAT_INTERVAL_SECONDS" \
    --mount "type=bind,source=$ROOT,target=/workspace/powerpacks,readonly" \
    -v "$CONTAINER_CACHE_VOLUME:/root/.cache/powerpacks" \
    -w /workspace/powerpacks
  if [[ -n "${OPENAI_API_KEY:-}" ]]; then
    printf '%s\0' -e "OPENAI_API_KEY=$OPENAI_API_KEY"
  fi
  if [[ -n "${CODEX_HEARTBEAT_PROMPT:-}" ]]; then
    printf '%s\0' -e "CODEX_HEARTBEAT_PROMPT=$CODEX_HEARTBEAT_PROMPT"
  fi
  pass_env_if_set POWERPACKS_HEARTBEAT_SKIP_INSTALL
  pass_env_if_set POWERPACKS_SKIP_UV_SYNC
  pass_env_if_set UV_PROJECT_ENVIRONMENT
  codex_home_mounts
}

warn_login() {
  if [[ ! -d "$HOST_CODEX_HOME" ]]; then
    if [[ "$CODEX_HOME_MODE" == "direct" || -z "${OPENAI_API_KEY:-}" ]]; then
      echo "error: host Codex home does not exist: $HOST_CODEX_HOME" >&2
      echo "       run codex login in your regular shell first, set HOST_CODEX_HOME, or use snapshot mode with OPENAI_API_KEY" >&2
      return 1
    fi
    echo "warning: host Codex home does not exist: $HOST_CODEX_HOME; relying on OPENAI_API_KEY" >&2
  elif [[ ! -f "$HOST_CODEX_HOME/auth.json" && -z "${OPENAI_API_KEY:-}" ]]; then
    echo "error: $HOST_CODEX_HOME/auth.json not found and OPENAI_API_KEY is unset" >&2
    echo "       run codex login in your regular shell first, then restart this container" >&2
    return 1
  fi
}

start_container() {
  require_docker
  warn_login
  build_image
  if container_running; then
    echo "$CONTAINER_NAME is already running"
    return 0
  fi
  if container_exists; then
    docker rm "$CONTAINER_NAME" >/dev/null
  fi

  args=()
  while IFS= read -r -d '' arg; do
    args+=("$arg")
  done < <(run_args)
  docker run -d \
    --name "$CONTAINER_NAME" \
    --restart unless-stopped \
    "${args[@]}" \
    "$IMAGE_NAME"
}

run_once() {
  require_docker
  warn_login
  build_image
  args=()
  while IFS= read -r -d '' arg; do
    args+=("$arg")
  done < <(run_args)
  docker run --rm \
    --name "$CONTAINER_NAME-once" \
    -e HEARTBEAT_ONCE=1 \
    "${args[@]}" \
    "$IMAGE_NAME" --once
}

case "$ACTION" in
  build)
    build_image
    ;;
  start)
    start_container
    ;;
  once)
    run_once
    ;;
  stop)
    require_docker
    if container_exists; then
      docker stop "$CONTAINER_NAME" >/dev/null || true
      docker rm "$CONTAINER_NAME" >/dev/null || true
      echo "stopped $CONTAINER_NAME"
    else
      echo "$CONTAINER_NAME is not created"
    fi
    ;;
  restart)
    "$0" stop || true
    "$0" start
    ;;
  status)
    require_docker
    if container_running; then
      docker ps --filter "name=^/${CONTAINER_NAME}$"
    else
      echo "$CONTAINER_NAME is not running"
      exit 1
    fi
    ;;
  logs)
    require_docker
    docker logs -f "$CONTAINER_NAME"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
