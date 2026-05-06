#!/usr/bin/env bash
# Sourced by setup.sh / install-service.sh / install-cli.sh / uninstall-service.sh.
# Provides:
#   - ensure_venv             — pick Python >=3.10, build .venv, sync requirements
#   - ensure_node             — install Node.js >= 18 (for the web client; phase 5)
#   - ensure_dwellerd_user     — create system user `dwellerd` with the right groups
#                               (docker, systemd-journal, adm) and FHS dirs
#   - preflight_dwellerd       — verify `dwellerd` can actually reach docker /
#                               journal / data dirs; print exact fix commands
#   - ensure_writable_paths   — dev-mode helper: chown ./data and ./logs back to
#                               the current shell user after a systemd run
#
# Every install action is preceded by a confirm prompt with an honest disk-size
# estimate. Set DWELLERD_YES=1 to skip all prompts (CI / --yes style automation).

DWELLERD_USER="${DWELLERD_USER:-dwellerd}"
DWELLERD_HOME="${DWELLERD_HOME:-/var/lib/dwellerd}"
DWELLERD_ETC="${DWELLERD_ETC:-/etc/dwellerd}"
DWELLERD_GROUPS_OPTIONAL="docker systemd-journal adm"


# ── prompt helpers ────────────────────────────────────────────────────


_dw_color() {
    if [ -t 1 ]; then
        case "$1" in
            cyan)   printf '\033[36m';;
            yellow) printf '\033[33m';;
            green)  printf '\033[32m';;
            red)    printf '\033[31m';;
            dim)    printf '\033[2m';;
            bold)   printf '\033[1m';;
            reset)  printf '\033[0m';;
        esac
    fi
}


# Pretty-print one install step before asking the user to confirm.
# Args: 1=label, 2=size estimate (e.g. "~80 MB"), 3=details
_dw_install_card() {
    local label="$1" size="$2" detail="$3"
    printf '\n  %s%s%s' "$(_dw_color cyan)" "$label" "$(_dw_color reset)"
    printf '   %s%s%s\n' "$(_dw_color dim)" "$size" "$(_dw_color reset)"
    if [ -n "$detail" ]; then
        printf '  %s%s%s\n' "$(_dw_color dim)" "$detail" "$(_dw_color reset)"
    fi
}


# Yes/No confirm. Default Yes — Enter alone proceeds. DWELLERD_YES=1 skips.
_dw_confirm() {
    local prompt="$1"
    if [ "${DWELLERD_YES:-}" = "1" ]; then
        printf '  %s [Y/n] auto-yes (DWELLERD_YES=1)\n' "$prompt"
        return 0
    fi
    if [ ! -t 0 ]; then
        printf '  %s [Y/n] auto-yes (non-interactive stdin)\n' "$prompt"
        return 0
    fi
    local reply
    printf '  %s %s[Y/n]%s ' "$prompt" "$(_dw_color dim)" "$(_dw_color reset)"
    read -r reply </dev/tty
    case "$reply" in
        ""|y|Y|yes|YES) return 0 ;;
        *)              return 1 ;;
    esac
}


# Same as _dw_confirm but DEFAULT NO — for destructive operations.
_dw_confirm_destructive() {
    local prompt="$1"
    if [ "${DWELLERD_YES:-}" = "1" ]; then
        printf '  %s [y/N] auto-yes (DWELLERD_YES=1)\n' "$prompt"
        return 0
    fi
    if [ ! -t 0 ]; then
        printf '  %s [y/N] declined (non-interactive)\n' "$prompt"
        return 1
    fi
    local reply
    printf '  %s %s[y/N]%s ' "$prompt" "$(_dw_color dim)" "$(_dw_color reset)"
    read -r reply </dev/tty
    case "$reply" in
        y|Y|yes|YES) return 0 ;;
        *)           return 1 ;;
    esac
}


_dw_ok()    { printf '  %s✓%s %s\n' "$(_dw_color green)"  "$(_dw_color reset)" "$1"; }
_dw_warn()  { printf '  %s⚠%s %s\n' "$(_dw_color yellow)" "$(_dw_color reset)" "$1"; }
_dw_fail()  { printf '  %s✗%s %s\n' "$(_dw_color red)"    "$(_dw_color reset)" "$1"; }


# ── dwellerd system user ────────────────────────────────────────────────


# True if the current shell can call `sudo` without a password — used to skip
# the friendly "you'll be asked for sudo password" preamble in CI.
_dw_have_sudo_nopw() {
    sudo -n true >/dev/null 2>&1
}


# Prime sudo creds before any spinner / Live region grabs the terminal.
_dw_ensure_sudo() {
    if _dw_have_sudo_nopw; then return 0; fi
    printf '  %ssudo: введи пароль если попросит%s\n' \
        "$(_dw_color dim)" "$(_dw_color reset)"
    sudo -v
}


# Returns 0 if user `dwellerd` exists.
_dw_user_exists() {
    id "$DWELLERD_USER" >/dev/null 2>&1
}


# Returns the list of groups `dwellerd` is currently a member of, one per line.
_dw_user_groups() {
    id -nG "$DWELLERD_USER" 2>/dev/null | tr ' ' '\n'
}


# Create the system user if missing, add to optional groups (skip those that
# don't exist on this host), pre-create FHS data/logs/etc dirs with right
# ownership and permissions.
#
# Idempotent — re-running adds only what's missing.
ensure_dwellerd_user() {
    _dw_ensure_sudo || { echo "sudo failed" >&2; return 1; }

    if ! _dw_user_exists; then
        _dw_install_card \
            "system user '$DWELLERD_USER'" \
            "минимальные ресурсы" \
            "useradd --system, без shell, \$HOME=$DWELLERD_HOME"
        if ! _dw_confirm "создать пользователя?"; then
            _dw_warn "отменено — без юзера ставить сервис нельзя"
            return 1
        fi
        sudo useradd \
            --system \
            --shell /usr/sbin/nologin \
            --home-dir "$DWELLERD_HOME" \
            --create-home \
            --user-group \
            "$DWELLERD_USER" \
            || { _dw_fail "useradd failed"; return 1; }
        _dw_ok "создан пользователь $DWELLERD_USER (uid=$(id -u "$DWELLERD_USER"))"
    else
        _dw_ok "пользователь $DWELLERD_USER уже существует (uid=$(id -u "$DWELLERD_USER"))"
    fi

    # Add to optional groups. We do NOT fail if a group is missing — e.g. on a
    # host without docker installed, the `docker` group simply isn't there;
    # docker monitoring would be impossible anyway, so skip silently.
    local current_groups missing=() g
    current_groups=$(_dw_user_groups)
    for g in $DWELLERD_GROUPS_OPTIONAL; do
        if ! getent group "$g" >/dev/null 2>&1; then
            continue
        fi
        if ! grep -qx "$g" <<<"$current_groups"; then
            missing+=("$g")
        fi
    done

    if [ ${#missing[@]} -gt 0 ]; then
        _dw_install_card \
            "группы для $DWELLERD_USER" \
            "0 MB" \
            "${missing[*]} — даёт доступ к docker.sock / journal / /var/log"
        if _dw_confirm "добавить?"; then
            for g in "${missing[@]}"; do
                if sudo usermod -aG "$g" "$DWELLERD_USER"; then
                    _dw_ok "$DWELLERD_USER добавлен в группу $g"
                else
                    _dw_fail "не удалось добавить в группу $g"
                fi
            done
        fi
    else
        _dw_ok "группы уже настроены ($(_dw_user_groups | grep -E '^(docker|systemd-journal|adm)$' | tr '\n' ' '))"
    fi

    # FHS dirs. /etc/dwellerd owned by root with dwellerd group-read so config.yaml
    # (containing Telegram tokens) isn't world-readable but the daemon can read
    # it. Data + logs owned by dwellerd outright.
    sudo install -d -o "$DWELLERD_USER" -g "$DWELLERD_USER" -m 750 \
        "$DWELLERD_HOME" "$DWELLERD_HOME/data" "$DWELLERD_HOME/logs" \
        || { _dw_fail "install -d $DWELLERD_HOME failed"; return 1; }
    sudo install -d -o root -g "$DWELLERD_USER" -m 750 "$DWELLERD_ETC" \
        || { _dw_fail "install -d $DWELLERD_ETC failed"; return 1; }
    _dw_ok "директории готовы: $DWELLERD_HOME/{data,logs}, $DWELLERD_ETC"
    return 0
}


# Walk-through diagnostic: from the perspective of `dwellerd`, can the daemon
# actually reach the things it needs? Prints OK/FAIL per check with the exact
# fix command. Optional features (docker, journal) are warnings, not errors.
#
# Args: $1 = project root (unused, kept for symmetry with ensure_writable_paths).
preflight_dwellerd() {
    local _project_root="${1:-$PWD}"
    local _required_failed=0

    if ! _dw_user_exists; then
        _dw_fail "user '$DWELLERD_USER' не существует — запусти 'make bootstrap-user'"
        return 1
    fi

    printf '\n  %s%spreflight для %s%s\n' \
        "$(_dw_color cyan)" "$(_dw_color bold)" "$DWELLERD_USER" "$(_dw_color reset)"

    # ── required: data + logs writable ───────────────────────────────
    local p
    for p in "$DWELLERD_HOME/data" "$DWELLERD_HOME/logs"; do
        if sudo -u "$DWELLERD_USER" -- test -w "$p" 2>/dev/null; then
            _dw_ok "writable: $p"
        else
            _dw_fail "$p — не пишется юзером $DWELLERD_USER"
            printf '       %sfix:%s sudo install -d -o %s -g %s -m 750 %s\n' \
                "$(_dw_color dim)" "$(_dw_color reset)" \
                "$DWELLERD_USER" "$DWELLERD_USER" "$p"
            _required_failed=1
        fi
    done

    # ── required: config readable ─────────────────────────────────────
    if [ -e "$DWELLERD_ETC/config.yaml" ]; then
        if sudo -u "$DWELLERD_USER" -- test -r "$DWELLERD_ETC/config.yaml" 2>/dev/null; then
            _dw_ok "readable: $DWELLERD_ETC/config.yaml"
        else
            _dw_fail "$DWELLERD_ETC/config.yaml — не читается юзером $DWELLERD_USER"
            printf '       %sfix:%s sudo chown root:%s %s && sudo chmod 640 %s\n' \
                "$(_dw_color dim)" "$(_dw_color reset)" \
                "$DWELLERD_USER" "$DWELLERD_ETC/config.yaml" "$DWELLERD_ETC/config.yaml"
            _required_failed=1
        fi
    else
        _dw_warn "$DWELLERD_ETC/config.yaml ещё не создан — wizard напишет"
    fi

    # ── optional: docker daemon ───────────────────────────────────────
    if command -v docker >/dev/null 2>&1; then
        if sudo -u "$DWELLERD_USER" -- docker info >/dev/null 2>&1; then
            _dw_ok "docker — $DWELLERD_USER может говорить с демоном"
        else
            _dw_fail "docker info — недоступен для $DWELLERD_USER"
            if getent group docker >/dev/null 2>&1; then
                printf '       %sfix:%s sudo usermod -aG docker %s && sudo systemctl restart dwellerd\n' \
                    "$(_dw_color dim)" "$(_dw_color reset)" "$DWELLERD_USER"
            else
                printf '       %sfix:%s группа `docker` не существует — docker не установлен или rootless?\n' \
                    "$(_dw_color dim)" "$(_dw_color reset)"
            fi
        fi
        # docker compose v2 plugin
        if sudo -u "$DWELLERD_USER" -- docker compose version >/dev/null 2>&1; then
            _dw_ok "docker compose v2 — доступен"
        else
            _dw_warn "docker compose v2 — не работает (нужен для compose-мониторинга)"
        fi
    else
        _dw_warn "docker не установлен — docker-фичи будут отключены"
    fi

    # ── optional: journalctl ──────────────────────────────────────────
    if command -v journalctl >/dev/null 2>&1; then
        if sudo -u "$DWELLERD_USER" -- journalctl -n1 --no-pager >/dev/null 2>&1; then
            _dw_ok "journalctl — $DWELLERD_USER читает journal"
        else
            _dw_fail "journalctl — недоступен для $DWELLERD_USER"
            printf '       %sfix:%s sudo usermod -aG systemd-journal %s\n' \
                "$(_dw_color dim)" "$(_dw_color reset)" "$DWELLERD_USER"
        fi
    else
        _dw_warn "journalctl не установлен — journal-фичи будут отключены"
    fi

    if [ "$_required_failed" -ne 0 ]; then
        printf '\n  %s%spreflight: ОБЯЗАТЕЛЬНЫЕ проверки упали — исправь выше%s\n' \
            "$(_dw_color red)" "$(_dw_color bold)" "$(_dw_color reset)"
        return 1
    fi
    printf '\n  %s%spreflight: OK%s\n' \
        "$(_dw_color green)" "$(_dw_color bold)" "$(_dw_color reset)"
    return 0
}


# ── dev-mode writable paths (./data, ./logs) ──────────────────────────


# Make sure ./data and ./logs are owned by the current shell user. Symptom we
# guard against: a previous systemd run (User=dwellerd) left files owned by
# the dwellerd user, and now `make run` from a normal shell hits "attempt to
# write a readonly database".
ensure_writable_paths() {
    local PROJECT_ROOT="$1"
    local me uid bad=()
    me="$(id -un)"
    uid="$(id -u)"

    mkdir -p "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs" 2>/dev/null || true

    local p
    for p in "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs"; do
        [ -d "$p" ] || continue
        if [ ! -w "$p" ]; then
            bad+=("$p")
            continue
        fi
        local f
        while IFS= read -r -d '' f; do
            if [ ! -w "$f" ]; then
                bad+=("$p")
                break
            fi
        done < <(find "$p" -maxdepth 1 -type f -print0 2>/dev/null)
    done

    [ "${#bad[@]}" -eq 0 ] && return 0

    printf '\n  %s%sDwellerd: writable-path check failed%s\n' \
        "$(_dw_color yellow)" "$(_dw_color bold)" "$(_dw_color reset)"
    printf '  %sthe following paths are not writable by %s (uid=%s):%s\n' \
        "$(_dw_color dim)" "$me" "$uid" "$(_dw_color reset)"
    for p in "${bad[@]}"; do
        local owner
        owner="$(stat -c '%U:%G' "$p" 2>/dev/null || echo '?:?')"
        printf '    %s  %s(owner: %s)%s\n' "$p" "$(_dw_color dim)" "$owner" "$(_dw_color reset)"
    done
    printf '  %slikely cause:%s a previous systemd run (User=%s) left files behind.\n' \
        "$(_dw_color dim)" "$(_dw_color reset)" "$DWELLERD_USER"
    printf '  %sfix:%s sudo chown -R %s "%s/data" "%s/logs"\n\n' \
        "$(_dw_color dim)" "$(_dw_color reset)" "$me" "$PROJECT_ROOT" "$PROJECT_ROOT"

    if ! _dw_confirm_destructive "run the chown now?"; then
        printf '  %sdeclined — the daemon will fail to write%s\n' \
            "$(_dw_color yellow)" "$(_dw_color reset)" >&2
        return 1
    fi
    if ! sudo chown -R "$me:$me" "$PROJECT_ROOT/data" "$PROJECT_ROOT/logs"; then
        echo "  chown failed" >&2
        return 1
    fi
    _dw_ok "ownership fixed"
    return 0
}


# ── Python venv + deps ────────────────────────────────────────────────


ensure_venv() {
    local PROJECT_ROOT="$1"
    local PYTHON=""

    for cand in python3.13 python3.12 python3.11 python3.10; do
        if command -v "$cand" >/dev/null 2>&1; then
            PYTHON="$cand"
            break
        fi
    done

    if [ -z "$PYTHON" ] && command -v python3 >/dev/null 2>&1; then
        if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
            PYTHON=python3
        fi
    fi

    if [ -z "$PYTHON" ]; then
        local found
        found=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "none")
        cat >&2 <<EOF

Dwellerd requires Python 3.10 or newer, but found: $found

To install on Ubuntu/Debian:
  Ubuntu 22.04+:  sudo apt install -y python3.11 python3.11-venv
  Older systems:  sudo add-apt-repository -y ppa:deadsnakes/ppa
                  sudo apt update
                  sudo apt install -y python3.11 python3.11-venv

Then re-run this command.

EOF
        return 1
    fi

    cd "$PROJECT_ROOT" || return 1

    if [ ! -x .venv/bin/python ]; then
        _dw_install_card \
            "Python venv ($PYTHON)" \
            "~50 MB" \
            "creates ./.venv with isolated interpreter + pip"
        if ! _dw_confirm "create venv now?"; then
            echo "  declined — re-run when ready" >&2
            return 1
        fi
        echo "  creating venv..."
        if ! "$PYTHON" -m venv .venv; then
            cat >&2 <<EOF

Failed to create venv with $PYTHON. On Debian/Ubuntu install the matching
venv package:
  sudo apt install -y ${PYTHON}-venv

EOF
            return 1
        fi
    fi

    if [ ! -x .venv/bin/pip ]; then
        echo "  bootstrapping pip..."
        if ! .venv/bin/python -m ensurepip --upgrade >/dev/null 2>&1; then
            cat >&2 <<EOF

Venv is missing pip and ensurepip failed. On Debian/Ubuntu install:
  sudo apt install -y ${PYTHON}-venv python3-pip

Then drop the broken venv and retry:
  rm -rf "$PROJECT_ROOT/.venv"

EOF
            return 1
        fi
    fi

    # Sync Python deps. Only ask if the install actually has work to do.
    if .venv/bin/python -m pip install --dry-run -q -r requirements.txt 2>/dev/null \
       | grep -q "^Would install"; then
        _dw_install_card \
            "Python dependencies" \
            "~80 MB" \
            "fastapi, sqlmodel, taskiq, psutil, bcrypt, pyjwt, …"
        if ! _dw_confirm "install / update them?"; then
            echo "  declined — daemon may not start without these" >&2
            return 1
        fi
    fi
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip install -q -r requirements.txt
}


# ── Node.js bootstrap ─────────────────────────────────────────────────


ensure_node() {
    if command -v node >/dev/null 2>&1; then
        local ver
        ver=$(node --version 2>/dev/null | sed 's/^v//;s/\..*//')
        if [ -n "$ver" ] && [ "$ver" -ge 18 ] 2>/dev/null; then
            return 0
        fi
        echo "  node $(node --version) is too old, need >= 18" >&2
    fi

    local RUN=""
    if [ "$(id -u)" -ne 0 ]; then
        if command -v sudo >/dev/null 2>&1; then
            RUN="sudo -E"
        else
            cat >&2 <<'EOF'

Node.js >= 18 is required for the web client and was not found.
Install manually, then re-run:

  Debian/Ubuntu:
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo bash -
    sudo apt install -y nodejs

  RHEL/Fedora:
    curl -fsSL https://rpm.nodesource.com/setup_20.x | sudo bash -
    sudo dnf install -y nodejs

  Arch:    sudo pacman -S --noconfirm nodejs npm
  Alpine:  sudo apk add --no-cache nodejs npm

EOF
            return 1
        fi
    fi

    _dw_install_card \
        "Node.js 20 LTS" \
        "~50 MB system-wide" \
        "from NodeSource (apt/dnf) or distro repo — needed to build the web client"
    if ! _dw_confirm "install now?"; then
        echo "  declined — client build will be skipped, the daemon still starts (placeholder UI)" >&2
        return 1
    fi
    echo "  installing..."

    if command -v apt-get >/dev/null 2>&1; then
        if ! command -v curl >/dev/null 2>&1; then
            echo "  installing curl + ca-certificates..."
            $RUN apt-get update -qq || true
            $RUN apt-get install -y -qq curl ca-certificates || true
        fi
        if curl -fsSL https://deb.nodesource.com/setup_20.x | $RUN bash -; then
            if $RUN apt-get install -y nodejs; then
                echo "  ✓ installed node $(node --version 2>/dev/null)"
                return 0
            fi
        fi
        echo "  NodeSource failed; falling back to distro repo (older Node)..."
        $RUN apt-get update -qq || true
        if $RUN apt-get install -y nodejs npm; then
            echo "  ✓ installed node $(node --version 2>/dev/null) (distro)"
            return 0
        fi

    elif command -v dnf >/dev/null 2>&1; then
        if curl -fsSL https://rpm.nodesource.com/setup_20.x | $RUN bash - \
           && $RUN dnf install -y nodejs; then
            echo "  ✓ installed node $(node --version 2>/dev/null)"
            return 0
        fi
        $RUN dnf install -y nodejs npm && {
            echo "  ✓ installed node $(node --version 2>/dev/null) (distro)"
            return 0
        }
    elif command -v yum >/dev/null 2>&1; then
        if curl -fsSL https://rpm.nodesource.com/setup_20.x | $RUN bash - \
           && $RUN yum install -y nodejs; then
            echo "  ✓ installed node $(node --version 2>/dev/null)"
            return 0
        fi

    elif command -v pacman >/dev/null 2>&1; then
        if $RUN pacman -Sy --noconfirm nodejs npm; then
            echo "  ✓ installed node $(node --version 2>/dev/null)"
            return 0
        fi

    elif command -v apk >/dev/null 2>&1; then
        if $RUN apk add --no-cache nodejs npm; then
            echo "  ✓ installed node $(node --version 2>/dev/null)"
            return 0
        fi
    fi

    cat >&2 <<'EOF'

Failed to auto-install Node.js. The error is above; install Node manually
with your package manager and re-run.

EOF
    return 1
}
