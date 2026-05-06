#!/usr/bin/env bash
# Stop, disable and remove the systemd unit. Leaves the `dwellerd` system user
# and /var/lib/dwellerd in place — uninstall doesn't destroy collected data by
# default. Pass --purge to also drop the user, /var/lib/dwellerd and /etc/dwellerd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# shellcheck source=_bootstrap.sh
. "$SCRIPT_DIR/_bootstrap.sh"
ensure_venv "$PROJECT_ROOT" || exit 1

exec .venv/bin/python "$SCRIPT_DIR/setup.py" --uninstall-service "$@"
