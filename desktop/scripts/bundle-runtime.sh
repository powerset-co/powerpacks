#!/usr/bin/env bash
# Fetch what the app ships: uv, Node, Codex and msgvault binaries (Tauri sidecars, named with
# the target triple), the pinned wacli and Google Cloud CLI the app places under ~/.powerpacks at
# launch (tools.rs), and Powerpacks source with its browser automation dependency.
#
#   scripts/bundle-runtime.sh [target-triple]     default: this machine's Rust host
#
# Writes src-tauri/binaries/{uv,node,codex,codex-code-mode-host,rg,msgvault}-<triple> and
# src-tauri/resources/{powerpacks.tar.gz,wacli,wacli.version,google-cloud-sdk.tar.gz,google-cloud-sdk.version}.
set -euo pipefail

UV_VERSION="0.13.5"
CODEX_VERSION="0.161.0"
NODE_VERSION="22.16.0"
PLAYWRIGHT_VERSION="1.64.1"
MSGVAULT_VERSION="0.22.0"
GCLOUD_VERSION="591.0.0"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$here/.." && pwd)"
triple="${1:-$(rustc -vV | sed -n 's/^host: //p')}"
bin="$here/src-tauri/binaries"
resources="$here/src-tauri/resources"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$bin" "$resources"

# Codex publishes one npm package per platform; its musl Linux builds stand in for gnu.
# msgvault, wacli and the Google Cloud CLI publish macOS and Linux builds only.
exe=""
case "$triple" in
  aarch64-apple-darwin) codex_platform=darwin-arm64; node_platform=darwin-arm64; msgvault_platform=darwin_arm64; wacli_platform=darwin-arm64; gcloud_platform=darwin-arm ;;
  x86_64-apple-darwin) codex_platform=darwin-x64; node_platform=darwin-x64; msgvault_platform=darwin_amd64; wacli_platform=darwin-amd64; gcloud_platform=darwin-x86_64 ;;
  x86_64-unknown-linux-gnu) codex_platform=linux-x64; node_platform=linux-x64; msgvault_platform=linux_amd64; wacli_platform=linux-amd64; gcloud_platform=linux-x86_64 ;;
  aarch64-unknown-linux-gnu) codex_platform=linux-arm64; node_platform=linux-arm64; msgvault_platform=linux_arm64; wacli_platform=linux-arm64; gcloud_platform=linux-arm ;;
  *) echo "bundle-runtime: unsupported target $triple" >&2; exit 2 ;;
esac

echo "bundle-runtime: uv $UV_VERSION for $triple"
if [[ -n "$exe" ]]; then
  curl -fsSL -o "$work/uv.zip" "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$triple.zip"
  unzip -q "$work/uv.zip" -d "$work/uv"
  install -m 755 "$work/uv/uv.exe" "$bin/uv-$triple$exe"
else
  curl -fsSL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-$triple.tar.gz" | tar -xz -C "$work"
  install -m 755 "$work/uv-$triple/uv" "$bin/uv-$triple"
fi

echo "bundle-runtime: node $NODE_VERSION for $node_platform"
node_archive="node-v$NODE_VERSION-$node_platform"
if [[ -n "$exe" ]]; then
  curl -fsSL -o "$work/node.zip" "https://nodejs.org/dist/v$NODE_VERSION/$node_archive.zip"
  unzip -q "$work/node.zip" -d "$work"
  install -m 755 "$work/$node_archive/node.exe" "$bin/node-$triple$exe"
else
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/$node_archive.tar.gz" | tar -xz -C "$work"
  install -m 755 "$work/$node_archive/bin/node" "$bin/node-$triple"
fi

echo "bundle-runtime: codex $CODEX_VERSION for $codex_platform"
curl -fsSL "https://registry.npmjs.org/@openai/codex/-/codex-$CODEX_VERSION-$codex_platform.tgz" | tar -xz -C "$work"
vendor="$(find "$work/package/vendor" -mindepth 1 -maxdepth 1 -type d | head -1)"
install -m 755 "$vendor/bin/codex$exe" "$bin/codex-$triple$exe"
# Codex runs every command through this host, found next to its own binary.
install -m 755 "$vendor/bin/codex-code-mode-host$exe" "$bin/codex-code-mode-host-$triple$exe"
install -m 755 "$vendor/codex-path/rg$exe" "$bin/rg-$triple$exe"

# Each archive is checked against the sum its publisher signs: msgvault's SHA256SUMS, wacli's
# table in packs/ingestion/primitives/discover/messages/wacli/binary.py (the version pin lives there too).
verify() { # file expected-sha256
  local actual; actual="$(shasum -a 256 "$1" | cut -d' ' -f1)"
  [[ "$actual" == "$2" ]] || { echo "bundle-runtime: $1 sha256 $actual, expected $2" >&2; exit 1; }
}

echo "bundle-runtime: msgvault $MSGVAULT_VERSION for $msgvault_platform"
msgvault_base="https://github.com/kenn-io/msgvault/releases/download/v$MSGVAULT_VERSION"
msgvault_archive="msgvault_${MSGVAULT_VERSION}_$msgvault_platform.tar.gz"
curl -fsSL -o "$work/$msgvault_archive" "$msgvault_base/$msgvault_archive"
verify "$work/$msgvault_archive" "$(curl -fsSL "$msgvault_base/SHA256SUMS" | grep " $msgvault_archive\$" | cut -d' ' -f1)"
tar -xzf "$work/$msgvault_archive" -C "$work" msgvault
install -m 755 "$work/msgvault" "$bin/msgvault-$triple"

wacli_py="$repo/packs/ingestion/primitives/discover/messages/wacli/binary.py"
wacli_version="$(sed -n 's/^WACLI_PINNED_VERSION = os.environ.get("POWERPACKS_WACLI_VERSION", "\(.*\)")$/\1/p' "$wacli_py")"
wacli_sha="$(awk -v version="$wacli_version" -v asset="wacli-$wacli_platform" '
  $1 == "\"" version "\":" { inside = 1; next }
  inside && $1 == "\"" asset "\":" { gsub(/[",]/, "", $2); print $2; exit }
  inside && $1 == "}," { exit }' "$wacli_py")"
echo "bundle-runtime: wacli $wacli_version for $wacli_platform"
curl -fsSL -o "$work/wacli" "https://github.com/powerset-co/wacli/releases/download/$wacli_version/wacli-$wacli_platform"
verify "$work/wacli" "$wacli_sha"
install -m 755 "$work/wacli" "$resources/wacli"
printf '%s\n' "$wacli_version" > "$resources/wacli.version"

echo "bundle-runtime: Google Cloud CLI $GCLOUD_VERSION for $gcloud_platform"
curl -fsSL -o "$resources/google-cloud-sdk.tar.gz" \
  "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-$GCLOUD_VERSION-$gcloud_platform.tar.gz"
printf '%s\n' "$GCLOUD_VERSION" > "$resources/google-cloud-sdk.version"

echo "bundle-runtime: Powerpacks source at $(git -C "$repo" rev-parse --short HEAD)"
# The committed tree minus what only development uses; the app installs it as ~/powerpacks.
git -C "$repo" rev-parse HEAD > "$resources/powerpacks.version"
git -C "$repo" archive --format=tar HEAD \
  ':(exclude)tests' ':(exclude)desktop' ':(exclude).github' ':(exclude)web/src' \
  ':(exclude)web/node_modules' > "$work/powerpacks.tar"
# npm is needed only on the build machine; the app ships playwright-core without npm.
npm install --prefix "$work/vendor/browser-node" --no-audit --no-fund --ignore-scripts \
  --package-lock=false "playwright-core@$PLAYWRIGHT_VERSION"
tar -rf "$work/powerpacks.tar" -C "$work" vendor/browser-node/node_modules/playwright-core
gzip -9 < "$work/powerpacks.tar" > "$resources/powerpacks.tar.gz"
ls -lh "$bin" "$resources"
