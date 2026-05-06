"""Bot config loader.

Resolution order for the bot token:
    1. DWELLERD_BOT_TOKEN env var
    2. `bot.token` in config.yaml (looks at /etc/dwellerd/config.yaml in
       prod, ./config.yaml in dev)
    3. `notifiers[].bot_token` of the first telegram notifier in
       config.yaml — convenient for solo setups where the same bot is
       used for both alerts and interactive commands.

Admin chat ids (operator's TG user id) come from DWELLERD_BOT_ADMINS as a
comma-separated list, or `bot.admins` in config.yaml.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


_DEV_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
_PROD_CONFIG = Path("/etc/dwellerd/config.yaml")


@dataclass
class BotConfig:
    token: str
    admin_ids: list[int] = field(default_factory=list)
    parse_mode: str = "HTML"


def _read_yaml() -> dict:
    for path in (_PROD_CONFIG, _DEV_CONFIG):
        if path.exists():
            try:
                return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                return {}
    return {}


def _token_from_yaml(raw: dict) -> str:
    if t := ((raw.get("bot") or {}).get("token")):
        return str(t)
    for n in raw.get("notifiers") or []:
        if n.get("type") == "telegram" and n.get("bot_token"):
            return str(n["bot_token"])
    return ""


def _admins_from_env() -> list[int]:
    raw = os.environ.get("DWELLERD_BOT_ADMINS", "")
    return [int(x.strip()) for x in raw.split(",") if x.strip().lstrip("-").isdigit()]


def _admins_from_yaml(raw: dict) -> list[int]:
    bot = raw.get("bot") or {}
    return [int(x) for x in (bot.get("admins") or []) if str(x).lstrip("-").isdigit()]


def load_config() -> BotConfig:
    raw = _read_yaml()

    token = os.environ.get("DWELLERD_BOT_TOKEN") or _token_from_yaml(raw)
    if not token:
        raise RuntimeError(
            "bot token missing — set DWELLERD_BOT_TOKEN or add bot.token / "
            "telegram notifier to config.yaml"
        )

    admins = _admins_from_env() or _admins_from_yaml(raw)
    return BotConfig(token=token, admin_ids=admins)
