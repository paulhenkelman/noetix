#!/usr/bin/env bash
# Noetix installer.
#
# Run from the repo root. Walks you through:
#   - Full (Backend + UI + Agent MCPs)
#   - Back-end (Backend + UI only)
#   - Agent (MCP servers + Claude Code config only)
#
# Detects an existing installation in this directory and offers to
# update in place or create a side-by-side install at <dir>-2/.
#
# Pass-through args go to `noetix init`. Examples:
#   ./install.sh                    # interactive
#   ./install.sh -m agent           # agent mode, otherwise interactive
#   ./install.sh -m full -y         # non-interactive defaults

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [ ! -f "cli/index.js" ]; then
  echo "Error: install.sh must run from a noetix repo root (cli/index.js missing)." >&2
  exit 1
fi

# Node check
if ! command -v node >/dev/null 2>&1; then
  echo "Error: Node.js 18+ is required. Install: https://nodejs.org/" >&2
  exit 1
fi

NODE_MAJOR=$(node -p "process.versions.node.split('.')[0]")
if [ "$NODE_MAJOR" -lt 18 ]; then
  echo "Error: Node.js $NODE_MAJOR is too old. Need 18 or newer." >&2
  exit 1
fi

# CLI dependencies
if [ ! -d "node_modules" ]; then
  echo "Installing CLI dependencies (one-time)..."
  npm install --silent --no-audit --no-fund
fi

# Hand off to noetix init. All extra args pass through.
exec node cli/index.js init "$@"
