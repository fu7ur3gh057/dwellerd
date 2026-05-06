"""Build a list of log-source candidates from what the host actually exposes
(systemd journal units, common log files, picked docker services), then
let the operator multi-select which ones to ingest.

Also handles the journal-group bootstrap: in dev mode the daemon runs as
the invoking shell user, who must be in `systemd-journal` to read the
system journal. The wizard offers to add them via `usermod`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import questionary
from rich.prompt import Confirm, Prompt

from .i18n import t
from .ui import ask_seconds, console, step, warn_line


# ── helpers ────────────────────────────────────────────────────────────────


def _detect_user() -> str | None:
    """SUDO_USER (when wizard ran via sudo), $USER, then `id -un`."""
    u = os.environ.get("SUDO_USER") or os.environ.get("USER")
    if u:
        return u
    try:
        return subprocess.run(
            ["id", "-un"], capture_output=True, text=True, timeout=2,
        ).stdout.strip() or None
    except Exception:
        return None


def _user_in_group(user: str, group: str) -> bool:
    """`getent group` so we see fresh `usermod -aG` results without re-login."""
    try:
        r = subprocess.run(
            ["getent", "group", group],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return False
        members = r.stdout.strip().split(":")[-1].split(",")
        return user in members
    except Exception:
        return False


def _systemd_unit_exists(unit: str) -> bool:
    try:
        r = subprocess.run(
            ["systemctl", "cat", unit, "--no-pager"],
            capture_output=True, timeout=2,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _resolve_ssh_unit() -> str | None:
    for u in ("ssh.service", "sshd.service"):
        if _systemd_unit_exists(u):
            return u
    return None


# ── journal-group bootstrap (dev mode) ─────────────────────────────────────


def ensure_journal_access() -> bool:
    """If the current user already reads journals (root or `systemd-journal`
    member), no-op. Otherwise offer `usermod -aG`; on failure show the
    manual command and poll until the group file reflects it.

    Service mode runs as the dedicated `dwellerd` user (added to the group
    by `_bootstrap.sh`), so this is only meaningful for `make run` from a
    normal shell.
    """
    if os.geteuid() == 0:
        return True
    if not shutil.which("journalctl"):
        return True  # nothing to gate

    user = _detect_user()
    if not user:
        return False
    if _user_in_group(user, "systemd-journal"):
        return True

    warn_line(t("logs_journal_warn", user=user))
    if Confirm.ask(f"  {t('logs_journal_auto', user=user)}", default=True):
        try:
            subprocess.run(
                ["sudo", "usermod", "-aG", "systemd-journal", user],
                check=True,
            )
        except subprocess.CalledProcessError:
            warn_line(t("logs_journal_failed"))
        else:
            if _user_in_group(user, "systemd-journal"):
                warn_line(t("logs_journal_done", user=user))
                return True
            warn_line(t("logs_journal_failed"))

    console.print()
    console.print(f"  {t('logs_journal_manual')}")
    console.print(f"    [cyan]sudo usermod -aG systemd-journal {user}[/cyan]\n")
    while True:
        if not Confirm.ask(f"  {t('logs_journal_recheck')}", default=True):
            return False
        if _user_in_group(user, "systemd-journal"):
            warn_line(t("logs_journal_done", user=user))
            return True
        warn_line(t("logs_journal_still_no"))


# ── source detection ────────────────────────────────────────────────────────


# (slug, path, regex, default-checked) — only those that actually exist
# AND are readable for the wizard's user end up in the chooser.
_FILE_PRESETS: list[tuple[str, str, str, bool]] = [
    ("auth",         "/var/log/auth.log",     r"(?i)failed|invalid|sudo|error",     True),
    ("syslog",       "/var/log/syslog",       r"(?i)error|warn|fail|critical",      False),
    ("nginx-error",  "/var/log/nginx/error.log",  r".+",                            True),
    ("nginx-access", "/var/log/nginx/access.log", r" (5\d\d) ",                     False),
    ("fail2ban",     "/var/log/fail2ban.log", r"(?i)ban|unban|found",               True),
    ("kern",         "/var/log/kern.log",     r"(?i)error|warn|oops|panic|oom",     False),
]


def detect_log_sources(
    *, systemd_units: list[str], docker_blocks: list[dict],
) -> list[dict]:
    """Build a list of candidate log sources based on what's present on the
    host. Each candidate carries metadata (`_label`, `_default`, `_readable`,
    `_user`, `kind`) that the picker uses but that gets stripped before
    serialization.
    """
    out: list[dict] = []
    user = _detect_user() or ""

    # journal candidates
    if shutil.which("journalctl"):
        always: list[tuple[str, str, str, bool]] = []

        ssh_unit = _resolve_ssh_unit()
        if ssh_unit:
            always.append((
                "ssh", ssh_unit,
                r"(?i)failed|invalid|accepted|sudo|error",
                True,
            ))
        if _systemd_unit_exists("cron.service"):
            always.append((
                "cron", "cron.service",
                r"(?i)error|fail|exit\s*\(?code\s*\)?\s*[1-9]",
                False,
            ))
        # Self-reflection — the unit is created later in install-service
        # so it may not exist yet during a fresh wizard run; offered
        # anyway since the operator usually wants to tail their own daemon.
        always.append((
            "dwellerd", "dwellerd.service",
            r"(?i)error|warn|exception|traceback",
            True,
        ))

        seen_units: set[str] = set()
        for slug, unit, pat, default in always:
            seen_units.add(unit)
            out.append({
                "kind": "journal", "type": "journal",
                "name": slug, "unit": unit, "pattern": pat,
                "_label": t("logs_label_journal", unit=unit),
                "_default": default,
            })

        for unit in systemd_units:
            if unit in seen_units:
                continue
            slug = "j-" + unit.replace(".service", "").replace(".", "-")
            out.append({
                "kind": "journal", "type": "journal",
                "name": slug, "unit": unit,
                "pattern": r"(?i)error|warn|fail|exception|critical",
                "_label": t("logs_label_journal", unit=unit),
                "_default": False,
            })

    # file candidates
    for slug, path, pat, default in _FILE_PRESETS:
        p = Path(path)
        if not p.is_file():
            continue
        readable = os.access(p, os.R_OK)
        out.append({
            "kind": "file", "type": "file",
            "name": slug, "path": str(p), "pattern": pat,
            "_label": t("logs_label_file", path=str(p)),
            "_default": default and readable,
            "_readable": readable, "_user": user,
        })

    # docker candidates from the earlier compose selection
    for blk in docker_blocks:
        compose_path = blk.get("compose")
        if not compose_path:
            continue
        for svc in blk.get("containers") or []:
            slug = "docker-" + svc
            out.append({
                "kind": "docker", "type": "docker",
                "name": slug, "compose": compose_path, "service": svc,
                "pattern": r"(?i)error|fatal|exception|traceback|critical",
                "poll_interval": 60,
                "_label": t("logs_label_docker", service=svc),
                "_default": False,
            })

    return out


def configure_logs(
    *, systemd_units: list[str], docker_blocks: list[dict],
) -> dict | None:
    """Returns the `logs:` config block, or None when the operator skips /
    no candidates / no picks. Shape matches the Dwellerd logs schema:
    flat top-level retention/max_rows (also accepted via `storage:` nested).
    """
    if not Confirm.ask(f"  {t('ask_logs_yn')}", default=True):
        return None

    ensure_journal_access()

    candidates = step(
        t("step_detect_logs"),
        lambda: detect_log_sources(
            systemd_units=systemd_units, docker_blocks=docker_blocks,
        ),
        delay=0.0,
    )
    if not candidates:
        warn_line(t("logs_no_candidates"))
        return None

    choices = []
    for c in candidates:
        title = c["_label"]
        if c["kind"] == "file" and not c.get("_readable", True):
            title += "  " + t("logs_hint_perm", user=c.get("_user", ""))
        choices.append(questionary.Choice(
            title=title, value=c["name"], checked=c["_default"],
        ))

    console.print(f"  [dim]{t('docker_hint')}[/dim]")
    picked = questionary.checkbox(t("logs_pick"), choices=choices).ask() or []
    picked_set = set(picked)

    sources: list[dict] = []
    for c in candidates:
        if c["name"] not in picked_set:
            continue
        # Strip the wizard's bookkeeping fields before emitting.
        sources.append({
            k: v for k, v in c.items()
            if not k.startswith("_") and k != "kind"
        })

    if not sources:
        return None

    digest = ask_seconds("ask_logs_digest", default=3600, minimum=60)
    retention_days = int(Prompt.ask(t("ask_logs_retention"), default="7") or "7")
    max_rows_raw = Prompt.ask(t("ask_logs_max_rows"), default="200000") or "200000"
    try:
        max_rows = int(max_rows_raw.replace("_", "").replace(",", ""))
    except ValueError:
        max_rows = 200_000

    return {
        "notifier": "telegram",
        # Logs collected to DB only — no Telegram digest. Operator can flip
        # to true via the web UI (Settings → logs.notify) if they want it.
        "notify": False,
        "digest_interval": digest,
        "retention_days": max(1, retention_days),
        "max_rows": max(1000, max_rows),
        "sources": sources,
    }


