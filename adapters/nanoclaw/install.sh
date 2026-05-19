#!/usr/bin/env bash
set -euo pipefail

TARGET="${1:-}"

if [[ -z "$TARGET" ]]; then
  echo "usage: ./install.sh /path/to/nanoclaw" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
TARGET="$(cd "$TARGET" && pwd)"

if [[ ! -f "$TARGET/package.json" ]]; then
  echo "error: target does not look like a repo: $TARGET" >&2
  exit 1
fi

if [[ ! -f "$TARGET/nanoclaw.sh" ]]; then
  echo "error: target does not look like a NanoClaw checkout: $TARGET" >&2
  exit 1
fi

mkdir -p "$TARGET/.claude/skills"
mkdir -p "$TARGET/container/skills"
mkdir -p "$TARGET/powerpacks"
mkdir -p "$TARGET/src/channels"
mkdir -p "$TARGET/scripts"

rm -rf "$TARGET/.claude/skills/add-query-decomposition"
rm -rf "$TARGET/.claude/skills/add-role-search"
rm -rf "$TARGET/.claude/skills/add-turbopuffer-schema-guard"
rm -rf "$TARGET/.claude/skills/add-postgres-hydration"
rm -rf "$TARGET/.claude/skills/add-slice-search"
rm -rf "$TARGET/.claude/skills/add-candidate-review-planning"
rm -rf "$TARGET/.claude/skills/search-network"
cp -R "$REPO_ROOT/packs/search/skills/search-network" "$TARGET/.claude/skills/search-network"

rm -rf "$TARGET/container/skills/search-network"
cp -R "$REPO_ROOT/packs/search/skills/search-network" "$TARGET/container/skills/search-network"

rm -rf "$TARGET/powerpacks/primitives"
rm -rf "$TARGET/powerpacks/mcp"
rm -rf "$TARGET/powerpacks/templates"
rm -rf "$TARGET/powerpacks/docs"
rm -rf "$TARGET/powerpacks/packs"
rm -rf "$TARGET/powerpacks/templates"
rm -rf "$TARGET/powerpacks/bin"
rm -rf "$TARGET/powerpacks/adapters"
# Cross-pack docs + host-install templates (no top-level primitives/skills/
# schemas anymore — every domain lives in packs/).
cp -R "$REPO_ROOT/docs" "$TARGET/powerpacks/docs"
cp -R "$REPO_ROOT/templates" "$TARGET/powerpacks/templates"
# Domain packs (powerset, search, messages, sales-nav, ...) carry their own
# primitives, schemas, contracts, tasks, evals, and docs.
cp -R "$REPO_ROOT/packs" "$TARGET/powerpacks/packs"
mkdir -p "$TARGET/powerpacks/adapters"
cp -R "$REPO_ROOT/adapters/nanoclaw" "$TARGET/powerpacks/adapters/nanoclaw"
cp -R "$SCRIPT_DIR/bin" "$TARGET/powerpacks/bin"
chmod +x "$TARGET/powerpacks/bin/powerclaw"

TARGET_FOR_POWERPACKS="$TARGET" python3 <<'PY'
import json
import os
from pathlib import Path

target = Path(os.environ["TARGET_FOR_POWERPACKS"]).resolve()
powerpacks = target / "powerpacks"

allowlist_path = Path.home() / ".config" / "nanoclaw" / "mount-allowlist.json"
allowlist_path.parent.mkdir(parents=True, exist_ok=True)
if allowlist_path.exists():
    try:
        allowlist = json.loads(allowlist_path.read_text())
    except json.JSONDecodeError:
        allowlist = {}
else:
    allowlist = {}

allowed_roots = allowlist.setdefault("allowedRoots", [])
powerpacks_root = str(powerpacks)
if not any(Path(root.get("path", "")).expanduser().resolve() == powerpacks for root in allowed_roots if root.get("path")):
    allowed_roots.append({
        "path": powerpacks_root,
        "allowReadWrite": True,
        "description": "Powerpacks runtime mount",
    })
allowlist.setdefault("blockedPatterns", [])
allowlist.setdefault("nonMainReadOnly", True)
allowlist_path.write_text(json.dumps(allowlist, indent=2, sort_keys=True) + "\n")

groups_dir = target / "groups"
for container_path in groups_dir.glob("*/container.json"):
    try:
        config = json.loads(container_path.read_text())
    except json.JSONDecodeError:
        continue
    mounts = config.setdefault("additionalMounts", [])
    mounts = [
        mount for mount in mounts
        if not (
            mount.get("containerPath") == "powerpacks"
            or Path(str(mount.get("hostPath", ""))).expanduser().resolve() == powerpacks
        )
    ]
    mounts.append({
        "hostPath": powerpacks_root,
        "containerPath": "powerpacks",
        "readonly": False,
    })
    config["additionalMounts"] = mounts
    container_path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
PY

cp "$SCRIPT_DIR/runtime/src/channels/cli-threaded.ts" "$TARGET/src/channels/cli-threaded.ts"
cp "$SCRIPT_DIR/runtime/scripts/chat-threaded.ts" "$TARGET/scripts/chat-threaded.ts"
cp "$SCRIPT_DIR/runtime/scripts/init-cli-threaded-channel.ts" "$TARGET/scripts/init-cli-threaded-channel.ts"

TARGET_DOCKERFILE="$TARGET/container/Dockerfile"
if [[ -f "$TARGET_DOCKERFILE" ]] && ! grep -Fq "python3-venv" "$TARGET_DOCKERFILE"; then
  python3 - "$TARGET_DOCKERFILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
old = """        ca-certificates \\
        curl \\
        git \\
        tini \\
        unzip \\
"""
new = """        ca-certificates \\
        curl \\
        git \\
        python3 \\
        python3-venv \\
        tini \\
        unzip \\
"""
if old not in text:
    raise SystemExit(f"could not patch apt package list in {path}")
path.write_text(text.replace(old, new))
PY
fi

if [[ -f "$TARGET_DOCKERFILE" ]] && ! grep -Fq "astral.sh/uv/install.sh" "$TARGET_DOCKERFILE"; then
  python3 - "$TARGET_DOCKERFILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
marker = """# Chromium path for agent-browser / Playwright consumers
ENV AGENT_BROWSER_EXECUTABLE_PATH=/usr/bin/chromium
"""
insert = """# ---- Python runtime for Powerpacks primitives -------------------------------
# uv provides isolated on-demand Python dependencies without relying on
# NanoClaw's runtime package approval flow.
RUN curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin sh

"""
if marker not in text:
    raise SystemExit(f"could not locate Python runtime insertion point in {path}")
path.write_text(text.replace(marker, insert + marker))
PY
fi

if ! grep -Fq "import './cli-threaded.js';" "$TARGET/src/channels/index.ts"; then
  printf "\nimport './cli-threaded.js';\n" >> "$TARGET/src/channels/index.ts"
fi

TARGET_CONTAINER_RUNNER="$TARGET/src/container-runner.ts"
if [[ -f "$TARGET_CONTAINER_RUNNER" ]] && ! grep -Fq "powerpacksContainerEnv" "$TARGET_CONTAINER_RUNNER"; then
  python3 - "$TARGET_CONTAINER_RUNNER" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
import_marker = "import { readContainerConfig, writeContainerConfig } from './container-config.js';\n"
if import_marker not in text:
    raise SystemExit(f"could not locate container-runner import marker in {path}")
text = text.replace(import_marker, import_marker + "import { readEnvFile } from './env.js';\n")

helper_marker = "const activeContainers = new Map<string, { process: ChildProcess; containerName: string }>();\n"
helper = r'''

function quotePgComponent(value: string): string {
  return encodeURIComponent(value);
}

function powerpacksContainerEnv(): Record<string, string> {
  const env = readEnvFile([
    'TURBOPUFFER_API_KEY',
    'DATABASE_URL',
    'OPENAI_API_KEY',
    'POSTGRES_HOST',
    'POSTGRES_PORT',
    'POSTGRES_DB',
    'POSTGRES_USER',
    'POSTGRES_PASSWORD',
  ]);
  const result: Record<string, string> = {};
  if (env.TURBOPUFFER_API_KEY) {
    result.TURBOPUFFER_API_KEY = env.TURBOPUFFER_API_KEY;
  }
  if (env.DATABASE_URL) {
    result.DATABASE_URL = env.DATABASE_URL;
  } else if (env.POSTGRES_HOST && env.POSTGRES_DB && env.POSTGRES_USER && env.POSTGRES_PASSWORD) {
    const port = env.POSTGRES_PORT || '5432';
    result.DATABASE_URL =
      `postgresql://${quotePgComponent(env.POSTGRES_USER)}:${quotePgComponent(env.POSTGRES_PASSWORD)}` +
      `@${env.POSTGRES_HOST}:${port}/${quotePgComponent(env.POSTGRES_DB)}`;
  }
  if (env.OPENAI_API_KEY) {
    result.OPENAI_API_KEY = env.OPENAI_API_KEY;
  }
  return result;
}
'''
if helper_marker not in text:
    raise SystemExit(f"could not locate container-runner helper marker in {path}")
text = text.replace(helper_marker, helper_marker + helper)

env_marker = "  args.push('-e', `TZ=${TIMEZONE}`);\n"
env_insert = r'''
  // Powerpacks recruiting primitives need these runtime secrets inside the
  // agent container. Keep the allowlist explicit; do not pass the whole .env.
  for (const [key, value] of Object.entries(powerpacksContainerEnv())) {
    args.push('-e', `${key}=${value}`);
  }
'''
if env_marker not in text:
    raise SystemExit(f"could not locate container-runner env marker in {path}")
text = text.replace(env_marker, env_marker + env_insert)

path.write_text(text)
PY
fi
if [[ -f "$TARGET_CONTAINER_RUNNER" ]] && grep -Fq "powerpacksContainerEnv" "$TARGET_CONTAINER_RUNNER" && ! grep -Fq "'OPENAI_API_KEY'" "$TARGET_CONTAINER_RUNNER"; then
  python3 - "$TARGET_CONTAINER_RUNNER" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
text = path.read_text()
text = text.replace("    'DATABASE_URL',\n", "    'DATABASE_URL',\n    'OPENAI_API_KEY',\n")
marker = """  if (env.DATABASE_URL) {
    result.DATABASE_URL = env.DATABASE_URL;
  } else if (env.POSTGRES_HOST && env.POSTGRES_DB && env.POSTGRES_USER && env.POSTGRES_PASSWORD) {
    const port = env.POSTGRES_PORT || '5432';
    result.DATABASE_URL =
      `postgresql://${quotePgComponent(env.POSTGRES_USER)}:${quotePgComponent(env.POSTGRES_PASSWORD)}` +
      `@${env.POSTGRES_HOST}:${port}/${quotePgComponent(env.POSTGRES_DB)}`;
  }
"""
insert = marker + """  if (env.OPENAI_API_KEY) {
    result.OPENAI_API_KEY = env.OPENAI_API_KEY;
  }
"""
if marker not in text:
    raise SystemExit(f"could not locate powerpacksContainerEnv DATABASE_URL block in {path}")
path.write_text(text.replace(marker, insert))
PY
fi

cat > "$TARGET/powerclaw" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/powerpacks/bin/powerclaw" "$@"
EOF
chmod +x "$TARGET/powerclaw"

if command -v pnpm >/dev/null 2>&1; then
  (cd "$TARGET" && pnpm --silent run build)
  if ! (cd "$TARGET" && pnpm --silent exec tsx scripts/init-cli-threaded-channel.ts); then
    echo "warning: could not initialize cli-threaded channel; run pnpm exec tsx scripts/init-cli-threaded-channel.ts from $TARGET" >&2
  fi
else
  echo "warning: pnpm not found; run pnpm run build and pnpm exec tsx scripts/init-cli-threaded-channel.ts from $TARGET" >&2
fi

if command -v docker >/dev/null 2>&1; then
  if [[ "${POWERPACKS_SKIP_IMAGE_BUILD:-}" != "1" ]]; then
    (cd "$TARGET" && ./container/build.sh)
    docker ps --format '{{.ID}} {{.Names}}' | awk '/nanoclaw-v2-/{print $1}' | xargs -r docker stop >/dev/null || true
  else
    echo "warning: skipped NanoClaw agent image rebuild because POWERPACKS_SKIP_IMAGE_BUILD=1" >&2
  fi
else
  echo "warning: docker not found; rebuild NanoClaw agent image manually with ./container/build.sh" >&2
fi

cat > "$TARGET/powerpacks/install-manifest.json" <<EOF
{
  "installed_from": "$REPO_ROOT",
  "adapter": "nanoclaw",
  "installed_at_utc": "$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
}
EOF

echo "powerpacks installed into $TARGET"
