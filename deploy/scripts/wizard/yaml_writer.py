"""Render the boot YAML, build the runtime-settings dict, and persist that
dict + the admin user into the SQLite DB.

Boot YAML carries only what the daemon needs *before* the DB is open:
  - db.path                              (resolves to <data_dir>/dwellerd.sqlite)
  - web.{enabled, host, port, prefix}    (only when web is enabled)

Everything else (notifiers, checks, report, logs, terminal) goes straight
into the `settings` table. The daemon hydrates from there on every start —
the YAML stays minimal + grep-friendly + free of secrets.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from .i18n import t
from .paths import (
    DEV_DATA_DIR, DEV_DB_PATH, PROD_CONFIG, PROD_DATA_DIR, PROD_DB_PATH,
    PROJECT_ROOT, WEB_PREFIX,
)
from .ui import warn_line


def _slug(path: str) -> str:
    s = path.strip("/").replace("/", "-")
    return s or "root"


def _yaml_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def is_prod_config(target: Path) -> bool:
    """True when we're writing /etc/dwellerd/config.yaml (vs dev ./config.yaml)."""
    try:
        return target.resolve() == PROD_CONFIG.resolve()
    except OSError:
        return str(target) == str(PROD_CONFIG)


def db_path_for(target: Path) -> Path:
    return PROD_DB_PATH if is_prod_config(target) else DEV_DB_PATH


def data_dir_for(target: Path) -> Path:
    return PROD_DATA_DIR if is_prod_config(target) else DEV_DATA_DIR


# ── boot YAML ──────────────────────────────────────────────────────────────


def build_boot_yaml(
    *, target: Path, web_cfg: dict | None, bot_cfg: dict | None = None,
) -> str:
    """Render the on-disk YAML — only db + web + bot boot fields."""
    db_path = db_path_for(target)
    parts: list[str] = [
        "# Dwellerd — boot config\n",
        "#\n",
        "# Runtime settings (notifiers / checks / report / logs / terminal /\n",
        "# admin users) live in the SQLite DB at db.path. Re-run `make setup`\n",
        "# or use the web UI to edit them.\n",
        "#\n",
        f"# JWT secret auto-generates at {data_dir_for(target)}/jwt.secret on\n",
        "# first boot. Override via web.jwt.secret here, the DWELLERD_JWT_SECRET\n",
        "# env var, or by writing the file yourself (mode 0600).\n",
        "\n",
        "db:\n",
        f"  path: {db_path}\n",
    ]

    if bot_cfg and bot_cfg.get("enabled"):
        admins = bot_cfg.get("admins") or []
        parts.append(
            "\n"
            "bot:\n"
            "  enabled: true\n"
        )
        # Token is optional — when absent, bot/config.py falls back to the
        # first telegram notifier's token, so the same bot serves both
        # alerts and commands.
        if (tok := bot_cfg.get("token")):
            parts.append(f'  token: "{_yaml_escape(str(tok))}"\n')
        if admins:
            parts.append(f"  admins: [{', '.join(str(a) for a in admins)}]\n")
        else:
            parts.append("  admins: []\n")

    if not (web_cfg and web_cfg.get("enabled")):
        return "".join(parts)

    port = int(web_cfg.get("port", 8765))
    parts.append(
        "\n"
        "web:\n"
        "  enabled: true\n"
        "  host: 0.0.0.0\n"
        f"  port: {port}\n"
        f"  prefix: {WEB_PREFIX}\n"
    )

    # Terminal — a copy of the runtime block so the namespace is registered
    # at boot (init_auth reads web.terminal.enabled before the DB hydrate
    # would run). The DB row stays the source-of-truth for runtime tweaks.
    term = web_cfg.get("terminal") or {}
    if term.get("enabled"):
        shell = term.get("shell")
        allow_users = term.get("allow_users") or []
        token_ttl = int(term.get("token_ttl", 1800))
        parts.append("  terminal:\n")
        parts.append("    enabled: true\n")
        if shell:
            parts.append(f"    shell: {shell}\n")
        if allow_users:
            parts.append(
                f"    allow_users: [{', '.join(allow_users)}]\n"
            )
        parts.append("    audit: true\n")
        parts.append(f"    token_ttl: {token_ttl}\n")

    return "".join(parts)


# ── runtime-settings dict (goes into Settings DB row) ──────────────────────


def build_runtime_settings(
    *,
    bot_token: str | None,
    chat_id: str | None,
    proxy: str = "",
    hostname: str,
    check_interval: int,
    report_interval: int,
    warn_pct: int,
    crit_pct: int,
    disks: list[str],
    docker_blocks: list[dict],
    net_cfg: dict | bool = True,
    systemd_units: list[str] | None = None,
    logs_cfg: dict | None = None,
    web_cfg: dict | None = None,
    notifier_lang: str = "en",
) -> dict:
    """Build the dict that gets written to `settings` (id=1). Mirrors the
    legacy YAML shape — same keys the daemon's hydrate code expects:

        notifiers: list[{type, **opts}]
        checks:    list[{type, name, interval, **opts}]
        report:    {interval, hostname, notifier, host: {disks, interfaces, warn_pct}, docker?}
        logs:      {notifier, notify, digest_interval, retention_days, max_rows, sources}
        terminal:  {enabled, shell, allow_users, audit, max_sessions, token_ttl} | None
    """
    systemd_units = systemd_units or []

    notifiers: list[dict] = []
    if bot_token and chat_id:
        notifier: dict = {
            "type": "telegram",
            "bot_token": bot_token,
            "chat_id": str(chat_id),
            "lang": notifier_lang,
        }
        if proxy:
            notifier["proxy"] = proxy
        notifiers.append(notifier)

    checks: list[dict] = [
        {"type": "cpu",    "name": "cpu",    "interval": check_interval,
         "warn_pct": warn_pct, "crit_pct": crit_pct},
        {"type": "memory", "name": "memory", "interval": check_interval,
         "warn_pct": warn_pct, "crit_pct": crit_pct},
    ]
    for d in disks:
        checks.append({
            "type": "disk", "name": f"disk-{_slug(d)}", "interval": check_interval,
            "path": d, "warn_pct": warn_pct, "crit_pct": crit_pct,
        })
    for unit in systemd_units:
        unit_slug = unit.replace(".service", "").replace(".", "-")
        checks.append({
            "type": "systemd",
            "name": f"systemd-{unit_slug}",
            "interval": check_interval,
            "unit": unit,
        })

    report: dict = {
        "interval": report_interval,
        "hostname": hostname,
        "notifier": "telegram",
        "host": {
            "disks": list(disks) or ["/"],
            "warn_pct": warn_pct,
        },
    }
    if isinstance(net_cfg, dict) and net_cfg.get("interfaces"):
        report["host"]["interfaces"] = list(net_cfg["interfaces"])
    if docker_blocks:
        report["docker"] = [
            {
                "compose": b["compose"],
                "containers": b.get("containers") or [],
                "starred": b.get("starred") or [],
            }
            for b in docker_blocks
        ]

    terminal_block: dict | None = None
    if web_cfg and web_cfg.get("terminal"):
        term = web_cfg["terminal"]
        terminal_block = {
            "enabled": True,
            "shell": term.get("shell"),
            "allow_users": term.get("allow_users") or [],
            "audit": True,
            "max_sessions": int(term.get("max_sessions", 1)),
            "token_ttl": int(term.get("token_ttl", 1800)),
        }

    return {
        "notifiers": notifiers,
        "checks":    checks,
        "report":    report,
        "logs":      logs_cfg,
        "terminal":  terminal_block,
    }


# ── DB persistence ─────────────────────────────────────────────────────────


def persist_runtime_to_db(
    db_path: Path,
    settings: dict,
    user: dict | None,
) -> None:
    """Open (or create) the SQLite DB, upsert the Settings singleton with
    the wizard's selections, and upsert the admin User row. Daemon will
    pick this up on its next start (or already, if it's running and we
    restart it after the wizard finishes).
    """
    sys.path.insert(0, str(PROJECT_ROOT / "server"))
    try:
        from sqlalchemy import create_engine
        from sqlmodel import Session, SQLModel, select  # type: ignore

        from db.models import Settings as SettingsRow, User as UserRow  # type: ignore
    except ImportError:
        warn_line(t("db_skipped"))
        return

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False, future=True)
    SQLModel.metadata.create_all(engine)
    now = time.time()

    with Session(engine) as s:
        existing = s.get(SettingsRow, 1)
        if existing:
            existing.notifiers = settings.get("notifiers")
            existing.checks    = settings.get("checks")
            existing.report    = settings.get("report")
            existing.logs      = settings.get("logs")
            existing.terminal  = settings.get("terminal")
            existing.updated_at = now
            s.add(existing)
        else:
            s.add(SettingsRow(
                id=1,
                notifiers=settings.get("notifiers"),
                checks=settings.get("checks"),
                report=settings.get("report"),
                logs=settings.get("logs"),
                terminal=settings.get("terminal"),
                updated_at=now,
            ))

        if user and user.get("username") and user.get("password_hash"):
            row = s.exec(
                select(UserRow).where(UserRow.username == user["username"]),
            ).first()
            if row:
                row.password_hash = user["password_hash"]
                row.role = "admin"
                row.is_active = True
                s.add(row)
            else:
                s.add(UserRow(
                    username=user["username"],
                    password_hash=user["password_hash"],
                    role="admin",
                    is_active=True,
                    created_at=now,
                ))
        s.commit()
    engine.dispose()
