#!/usr/bin/env bash
# Thin wrapper: ensure venv + deps + dwellerd user + groups, then hand off to
# setup.py for the actual systemd unit write + enable.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=_bootstrap.sh
. "$SCRIPT_DIR/_bootstrap.sh"
ensure_venv "$PROJECT_ROOT" || exit 1
ensure_dwellerd_user || exit 1

exec .venv/bin/python "$SCRIPT_DIR/setup.py" --install-service "$@"
