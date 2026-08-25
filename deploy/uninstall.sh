#!/usr/bin/env bash
# Remove the systemd service.
#   sudo ./deploy/uninstall.sh            # stop + remove the unit
#   sudo ./deploy/uninstall.sh --purge    # also drop the user, config and data
set -euo pipefail

SERVICE=dwellerd
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"
PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo" >&2; exit 1; }

systemctl stop "$SERVICE" 2>/dev/null || true
systemctl disable "$SERVICE" 2>/dev/null || true
rm -f "$UNIT_PATH"
systemctl daemon-reload
echo "unit removed"

if [ "$PURGE" -eq 1 ]; then
  rm -rf /var/lib/dwellerd /etc/dwellerd
  userdel dwellerd 2>/dev/null || true
  echo "purged the user, /etc/dwellerd and /var/lib/dwellerd"
else
  echo "kept /etc/dwellerd and /var/lib/dwellerd (use --purge to remove them)"
fi
