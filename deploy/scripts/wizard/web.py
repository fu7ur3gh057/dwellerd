"""Web client setup: port + admin user (bcrypt) + optional in-browser
TERMINAL block + Next.js client bundle build + paste-ready URL summary.

JWT secret is intentionally NOT written to the YAML — Dwellerd's auth
layer auto-generates `<data_dir>/jwt.secret` (mode 0600) on first boot
when the YAML doesn't carry one. Keeps secrets off readable config files
by default.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

from rich.prompt import Confirm, Prompt

from .config_io import load_existing_web_user
from .i18n import t
from .paths import CLIENT_DIR, PROJECT_ROOT, SERVICE_NAME, UNIT_PATH, WEB_PREFIX
from .ui import ask_seconds, confirm_install, console, step, warn_line


# ── public IP (best-effort, used in the URL summary) ───────────────────────


def detect_public_ip() -> str:
    """Best-effort: ipify for the egress IP, UDP-trick for the local IP,
    'localhost' as last resort."""
    import urllib.request

    try:
        req = urllib.request.Request(
            "https://api.ipify.org",
            headers={"User-Agent": "dwellerd-setup"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:
            ip = r.read().decode().strip()
            if ip:
                return ip
    except Exception:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "localhost"
    finally:
        s.close()


# ── admin user (bcrypt-hashed via the daemon's own helper) ────────────────


def _hash_password(plain: str) -> str:
    """Use the daemon's own `web.auth.passwords.hash_password` so cost
    factor + the >128-char rejection match what the daemon enforces.
    Falls back to a direct bcrypt call if server/ isn't on the path yet
    (rare — venv install would normally have brought it in).
    """
    sys.path.insert(0, str(PROJECT_ROOT / "server"))
    try:
        from web.auth.passwords import hash_password  # type: ignore
        return hash_password(plain)
    except Exception:
        import bcrypt
        return bcrypt.hashpw(
            plain.encode("utf-8"), bcrypt.gensalt(rounds=12),
        ).decode("utf-8")


def _gather_web_user() -> dict:
    """Returns {username, password_hash}. Reuses existing creds when present
    and the operator confirms — otherwise prompts for fresh and hashes."""
    existing = load_existing_web_user()
    if existing and Confirm.ask(
        f"  {t('ask_web_keep_user', username=existing['user']['username'])}",
        default=True,
    ):
        return existing["user"]

    default_user = (existing or {}).get("user", {}).get("username", "admin")
    username = Prompt.ask(f"{t('ask_web_username')}", default=default_user) or "admin"

    while True:
        password = Prompt.ask(f"{t('ask_web_password')}", password=True)
        if len(password) < 8:
            warn_line(t("web_password_short"))
            continue
        confirm = Prompt.ask(f"{t('ask_web_password_confirm')}", password=True)
        if password != confirm:
            warn_line(t("web_password_mismatch"))
            continue
        break

    return {"username": username, "password_hash": _hash_password(password)}


# ── full web block ─────────────────────────────────────────────────────────


def configure_web() -> dict | None:
    """Returns the full web config block (`{enabled, port, user, terminal?}`)
    or None when the operator declines. JWT secret stays out of the YAML —
    the daemon auto-generates `<data_dir>/jwt.secret` on first boot.
    """
    if not Confirm.ask(f"  {t('ask_web_yn')}", default=False):
        return None

    port_str = Prompt.ask(f"{t('ask_web_port')}", default="8765")
    try:
        port = int(port_str)
    except ValueError:
        port = 8765

    user_block = _gather_web_user()
    console.print(f"  [dim italic]{t('web_url_hint', port=port)}[/dim italic]")

    terminal_block: dict | None = None
    console.print(f"  {t('terminal_warn')}")
    if Confirm.ask(f"  {t('ask_terminal_yn')}", default=False):
        shell = Prompt.ask(t("ask_terminal_shell"), default="") or ""
        allow_raw = Prompt.ask(t("ask_terminal_allow"), default="") or ""
        allow_users = [u.strip() for u in allow_raw.split(",") if u.strip()]
        ttl = ask_seconds("ask_terminal_ttl", default=1800, minimum=60)
        terminal_block = {
            "enabled": True,
            "shell": shell or None,
            "allow_users": allow_users,
            "audit": True,
            "max_sessions": 1,
            "token_ttl": ttl,
        }

    out: dict = {
        "enabled": True,
        "port": port,
        "user": user_block,
    }
    if terminal_block:
        out["terminal"] = terminal_block
    return out


# ── Next.js client bundle (skipped silently if not yet ported / no node) ──


def build_client_bundle(web_cfg: dict | None) -> None:
    """Install client deps and build the static bundle so FastAPI can serve
    `/dwellerd/*` immediately. Skips silently when Node is missing or
    `client/` doesn't exist (Phase 5 hasn't shipped yet)."""
    if not web_cfg or not web_cfg.get("enabled"):
        return

    if not CLIENT_DIR.exists() or not (CLIENT_DIR / "package.json").exists():
        warn_line(t("client_build_skipped"))
        return

    pkg = shutil.which("pnpm") or shutil.which("npm")
    if not pkg:
        warn_line(t("client_pkg_missing"))
        return

    needs_install = not (CLIENT_DIR / "node_modules").exists()
    if needs_install and not confirm_install(
        title="Web client dependencies",
        size="~250 MB",
        detail=f"runs `{Path(pkg).name} install` in client/ — Next.js, react, recharts, xterm, …",
        prompt="install now?",
    ):
        warn_line("declined — skipping client build (FastAPI will show placeholder UI)")
        return

    if not confirm_install(
        title="Build static client bundle",
        size="~5 MB output to client/out",
        detail=f"runs `{Path(pkg).name} run build` (Next.js static export)",
        prompt="build now?",
    ):
        warn_line("declined — skipping client build")
        return

    def _install():
        subprocess.run([pkg, "install"], cwd=CLIENT_DIR, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    def _build():
        subprocess.run([pkg, "run", "build"], cwd=CLIENT_DIR, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)

    try:
        if needs_install:
            step(t("step_client_install"), _install, delay=0.0)
        step(t("step_client_build"), _build, delay=0.0)
    except subprocess.CalledProcessError:
        warn_line(t("client_build_failed"))


# ── post-write helpers ─────────────────────────────────────────────────────


def restart_service_if_installed() -> None:
    """If the systemd unit is on disk, kick it so the freshly written config
    + DB Settings row take effect immediately. No-op for first-time installs
    (the install-service path handles the initial start)."""
    if not UNIT_PATH.exists():
        return
    def _restart():
        subprocess.run(
            ["sudo", "systemctl", "restart", SERVICE_NAME],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        step(t("step_restart_service"), _restart, delay=0.0)
    except subprocess.CalledProcessError:
        pass  # operator can restart manually


def print_web_summary(web_cfg: dict | None) -> None:
    """Final block of the wizard: paste-ready URLs."""
    if not web_cfg or not web_cfg.get("enabled"):
        return
    port = int(web_cfg.get("port", 8765))
    username = (web_cfg.get("user") or {}).get("username", "admin")
    ip = detect_public_ip()
    base = f"http://{ip}:{port}{WEB_PREFIX}"

    console.print()
    console.print(f"[bold green]{t('web_summary_header')}[/bold green]")
    console.print(t("web_summary_swagger", url=f"[cyan]{base}/api/docs[/cyan]"))
    console.print(t("web_summary_redoc",   url=f"[cyan]{base}/api/redoc[/cyan]"))
    console.print(t("web_summary_openapi", url=f"[cyan]{base}/api/openapi.json[/cyan]"))
    console.print(t("web_summary_health",  url=f"[cyan]{base}/health[/cyan]"))
    console.print(t("web_summary_login",   username=f"[bold cyan]{username}[/bold cyan]"))
    console.print(f"  [dim italic]{t('web_summary_firewall', port=port)}[/dim italic]")
