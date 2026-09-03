#!/usr/bin/env bash
# Sync the wfgraph plugin from this worktree into the live Hermes install.
# Usage: bash plugins/wfgraph/install.sh [<hermes-home>]   (default ~/.hermes)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HERMES_HOME="${1:-$HOME/.hermes}"
DEST="$HERMES_HOME/plugins/wfgraph"

if [ ! -f "$REPO_DIR/plugin.yaml" ]; then
  echo "error: run from plugins/wfgraph inside the hermes-agent repo" >&2
  exit 1
fi

mkdir -p "$DEST"
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  "$REPO_DIR/" "$DEST/"

echo "installed $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null || echo unversioned) -> $DEST"
