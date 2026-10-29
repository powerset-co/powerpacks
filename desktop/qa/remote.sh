#!/usr/bin/env bash
# Run the scenarios on the QA Mac (casper-mini) instead of this one, so the QA app never takes
# this screen or quits this Powerpacks. Copies the built QA app and the --root folder over, runs
# run.py there in its logged-in session, and brings the pictures back.
#
#   desktop/qa/remote.sh ~/workspace/pp-qa-app [scenario ...]
set -euo pipefail
ROOT="$(cd "$1" && pwd)"
shift
HOST="${QA_HOST:-casper-mini}"
DIR="${QA_DIR:-/Volumes/ssd1/qa/desktop-qa}"
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/../src-tauri/target/release/bundle/macos/Powerpacks QA.app"

# The real path: the app takes a page server whose folder reads differently for another app's.
DIR="$(ssh "$HOST" "mkdir -p '$DIR/root' '$DIR/out' && cd '$DIR' && pwd -P")"
rsync -a --delete "$APP" "$HOST:$DIR/"
# The page and the data; the app makes its own .venv there on first launch.
rsync -a --delete \
  --exclude .git --exclude .venv --exclude node_modules --exclude 'desktop/src-tauri/target' \
  --exclude 'desktop/src-tauri/binaries' --exclude 'desktop/src-tauri/resources' --exclude qa-out \
  --exclude '.powerpacks/deep-context-baseline.sqlite' \
  "$ROOT/" "$HOST:$DIR/root/"
# Scenarios keep, skip, rename and delete: every run starts from the same copy of the store.
STORE=.powerpacks/deep-context/deep-context-v2.sqlite
rsync -a "$ROOT/.powerpacks/deep-context-baseline.sqlite" "$HOST:$DIR/root/$STORE"
ssh "$HOST" "rm -f '$DIR/root/$STORE-wal' '$DIR/root/$STORE-shm'"
status=0
ssh "$HOST" "cd '$DIR/root/desktop/qa' && python3 run.py --root '$DIR/root' --app '$DIR/Powerpacks QA.app' --out '$DIR/out' $*" || status=$?
mkdir -p "$ROOT/qa-out"
rsync -a "$HOST:$DIR/out/" "$ROOT/qa-out/"
exit $status
