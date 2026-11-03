#!/usr/bin/env bash
# Build "Powerpacks QA.app": the debug-menu build with the QA hook, under its own app id so it
# runs beside the installed Powerpacks. Rebuild only when desktop/src-tauri changes; the page
# comes from the --root folder run.py is given.
set -euo pipefail
cd "$(dirname "$0")/.."
pnpm install --frozen-lockfile
scripts/bundle-runtime.sh
POWERPACKS_DEBUG_MENU=1 pnpm tauri build --bundles app --config \
  '{"identifier":"co.powerset.powerpacks.qa","productName":"Powerpacks QA","bundle":{"createUpdaterArtifacts":false}}'
