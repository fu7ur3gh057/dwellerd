"""Read existing config.yaml so re-runs can offer 'keep current values' for
Telegram + admin user, and write the freshly-built YAML back to disk.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from .paths import DEV_CONFIG, PROD_CONFIG


def _pick_config_path() -> Path:
    """Prefer the prod path if it exists (operator already installed once),
    otherwise the dev one. Used for re-detecting existing values."""
    if PROD_CONFIG.exists():
        return PROD_CONFIG
    return DEV_CONFIG


def load_existing(path: Path | None = None) -> dict:
    """Return parsed yaml or {} if file missing / unparseable."""
    p = path or _pick_config_path()
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return {}


def write_yaml(path: Path, content: str) -> None:
    """Atomically write `content` to `path`, creating parents if needed.
    The wizard renders YAML by hand (string templating) for readable
    formatting — this helper just stamps it onto disk.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def load_existing_telegram() -> dict | None:
    """Return the first Telegram notifier dict from existing config, or None."""
    raw = load_existing()
    for n in raw.get("notifiers") or []:
        if n.get("type") == "telegram" and n.get("bot_token") and n.get("chat_id"):
            return n
    return None


def load_existing_web_user() -> dict | None:
    """Return {user, jwt} from existing config so re-runs can keep the
    admin password without re-prompting. Note: in Dwellerd, web.user lives
    in the YAML only as a first-boot seed; once the daemon imports it into
    the `users` table the field is dropped from disk on the next save.
    """
    raw = load_existing()
    web = raw.get("web") or {}
    user = web.get("user") or {}
    jwt_blk = web.get("jwt") or {}
    if user.get("username") and user.get("password_hash"):
        return {"user": user, "jwt": jwt_blk}
    return None
