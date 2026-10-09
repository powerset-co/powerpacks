#!/usr/bin/env bash
# Fetch what the app ships so a user needs nothing else: the uv and Codex binaries (Tauri
# sidecars, named with the target triple) and a snapshot of the Powerpacks source.
#
#   scripts/bundle-runtime.sh [target-triple]     default: this machine's Rust host
#
# Writes src-tauri/binaries/{uv,codex,codex-code-mode-host,rg}-<triple> and src-tauri/resources/powerpacks.tar.gz.
set -euo pipefail

UV_VERSION="0.11.32"
CODEX_VERSION="0.161.0"

here="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
repo="$(cd "$here/.." && pwd)"
triple="${1:-$(rustc -vV | sed -n 's/^host: //p')}"
bin="$here/src-tauri/binaries"
resources="$here/src-tauri/resources"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
mkdir -p "$bin" "$resources"

# Codex publishes one npm package per platform; its musl Linux builds stand in for gnu.
exe=""
case "$triple" in
  aarch64-apple-darwin) codex_platform=darwin-arm64 ;;
  x86_64-apple-darwin) codex_platform=darwin-x64 ;;
  x86_64-unknown-linux-gnu) codex_platform=linux-x64 ;;
  aarch64-unknown-linux-gnu) codex_platform=linux-arm64 ;;
  x86_64-pc-windows-msvc) codex_platform=win32-x64; exe=".exe" ;;
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

echo "bundle-runtime: codex $CODEX_VERSION for $codex_platform"
curl -fsSL "https://registry.npmjs.org/@openai/codex/-/codex-$CODEX_VERSION-$codex_platform.tgz" | tar -xz -C "$work"
vendor="$(find "$work/package/vendor" -mindepth 1 -maxdepth 1 -type d | head -1)"
install -m 755 "$vendor/bin/codex$exe" "$bin/codex-$triple$exe"
# Codex runs every command through this host, found next to its own binary.
install -m 755 "$vendor/bin/codex-code-mode-host$exe" "$bin/codex-code-mode-host-$triple$exe"
install -m 755 "$vendor/codex-path/rg$exe" "$bin/rg-$triple$exe"

echo "bundle-runtime: Powerpacks source at $(git -C "$repo" rev-parse --short HEAD)"
# The committed tree minus what only development uses; the app installs it as ~/powerpacks.
git -C "$repo" rev-parse HEAD > "$resources/powerpacks.version"
git -C "$repo" archive --format=tar HEAD \
  ':(exclude)tests' ':(exclude)desktop' ':(exclude).github' ':(exclude)web/src' \
  ':(exclude)web/node_modules' | gzip -9 > "$resources/powerpacks.tar.gz"
ls -lh "$bin" "$resources/powerpacks.tar.gz"
