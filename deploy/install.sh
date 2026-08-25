#!/usr/bin/env bash
# Install dwellerd as a systemd service.
#
#   sudo ./deploy/install.sh              # run as a dedicated `dwellerd` user
#   sudo ./deploy/install.sh --as-root    # run as root (for hosts where
#                                         # panel ACLs block any other user)
#
# Idempotent: safe to re-run after a `git pull` to pick up new code.
set -euo pipefail

SERVICE=dwellerd
INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG_DIR=/etc/dwellerd
STATE_DIR=/var/lib/dwellerd
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"
AS_ROOT=0

for arg in "$@"; do
  case "$arg" in
    --as-root) AS_ROOT=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

red()   { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { red "run me with sudo"; exit 1; }
command -v systemctl >/dev/null || { red "systemd not found — this host cannot run the service"; exit 1; }

# ── the service user ─────────────────────────────────────────────────────
if [ "$AS_ROOT" -eq 1 ]; then
  RUN_USER=root
  RUN_GROUP=root
  SUPP_GROUPS=""
  info "running as root (--as-root)"
else
  RUN_USER=dwellerd
  RUN_GROUP=dwellerd
  if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    useradd --system --shell /usr/sbin/nologin --home-dir "$STATE_DIR" \
            --no-create-home "$RUN_USER"
    green "created system user $RUN_USER"
  fi
  # Only add groups that actually exist on this host: `docker` is absent on
  # a box without docker, and usermod fails the whole install on a missing
  # group rather than skipping it.
  SUPP=""
  for group in docker systemd-journal adm; do
    if getent group "$group" >/dev/null 2>&1; then
      usermod -aG "$group" "$RUN_USER"
      SUPP="${SUPP:+$SUPP }$group"
    fi
  done
  SUPP_GROUPS="${SUPP// /,}"
  info "groups: ${SUPP_GROUPS:-none}"
fi

# ── directories ──────────────────────────────────────────────────────────
install -d -m 750 -o "$RUN_USER" -g "$RUN_GROUP" "$STATE_DIR"
install -d -m 750 -o root -g "$RUN_GROUP" "$CONFIG_DIR"

# ── the virtualenv ───────────────────────────────────────────────────────
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  info "creating the virtualenv"
  python3 -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/pip" install -q --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
green "dependencies installed"

# ── the config ───────────────────────────────────────────────────────────
if [ ! -f "$CONFIG_DIR/config.yaml" ]; then
  if [ -f "$INSTALL_DIR/config.yaml" ]; then
    install -m 640 -o root -g "$RUN_GROUP" "$INSTALL_DIR/config.yaml" "$CONFIG_DIR/config.yaml"
    green "copied config.yaml to $CONFIG_DIR/"
  else
    red "no config found — run: $INSTALL_DIR/.venv/bin/python -m dwellerd setup"
    exit 1
  fi
else
  # Re-assert ownership: it holds a bot token, and a previous run as a
  # different user may have left it readable.
  chown root:"$RUN_GROUP" "$CONFIG_DIR/config.yaml"
  chmod 640 "$CONFIG_DIR/config.yaml"
  info "keeping the existing $CONFIG_DIR/config.yaml"
fi

# Point the database at the state dir; a config written in dev mode carries
# a path inside the checkout, which the service user cannot write to.
if grep -qE '^\s*db_path:' "$CONFIG_DIR/config.yaml"; then
  sed -i -E "s|^\s*db_path:.*|db_path: \"$STATE_DIR/dwellerd.sqlite\"|" "$CONFIG_DIR/config.yaml"
else
  printf '\ndb_path: "%s/dwellerd.sqlite"\n' "$STATE_DIR" >> "$CONFIG_DIR/config.yaml"
fi

# ── preflight ────────────────────────────────────────────────────────────
info "checking that $RUN_USER can read the config"
if ! sudo -u "$RUN_USER" test -r "$CONFIG_DIR/config.yaml"; then
  red "$RUN_USER cannot read $CONFIG_DIR/config.yaml"
  exit 1
fi
if [ "$AS_ROOT" -eq 0 ] && command -v docker >/dev/null; then
  if sudo -u "$RUN_USER" docker info >/dev/null 2>&1; then
    info "docker: reachable as $RUN_USER"
  else
    red "docker is installed but $RUN_USER cannot reach it."
    red "Add it to the docker group and re-run:  usermod -aG docker $RUN_USER"
  fi
fi

# ── the unit ─────────────────────────────────────────────────────────────
sed -e "s|__USER__|$RUN_USER|g" \
    -e "s|__GROUP__|$RUN_GROUP|g" \
    -e "s|__SUPP_GROUPS__|$SUPP_GROUPS|g" \
    -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
    "$INSTALL_DIR/deploy/dwellerd.service" > "$UNIT_PATH"
# An empty SupplementaryGroups= is a parse error, not a no-op.
[ -n "$SUPP_GROUPS" ] || sed -i '/^SupplementaryGroups=$/d' "$UNIT_PATH"
chmod 644 "$UNIT_PATH"

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 2

if systemctl is-active --quiet "$SERVICE"; then
  green "$SERVICE is running"
  echo
  info "logs:    journalctl -u $SERVICE -f"
  info "status:  systemctl status $SERVICE"
  info "config:  $CONFIG_DIR/config.yaml  (restart after editing)"
else
  red "$SERVICE failed to start:"
  journalctl -u "$SERVICE" -n 30 --no-pager
  exit 1
fi
