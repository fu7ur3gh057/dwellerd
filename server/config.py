"""Dwellerd config loader.

YAML → dataclasses. Schema:

    notifiers:                  # list[NotifierConfig]
      - type: telegram
        bot_token: "..."
        chat_id: "..."
        lang: ru
    checks:                     # list[CheckConfig]
      - type: cpu
        name: cpu
        interval: 60
        warn_pct: 80
        crit_pct: 90
    report:    {...}            # dict, see core/report
    logs:      {...}            # dict, see core/logs
    web:       {enabled: bool, host, port, prefix}
    db:        {url, prune: {...}}
    data_dir:  /var/lib/dwellerd/data    (auto if config in /etc/dwellerd/)
    logs_dir:  /var/lib/dwellerd/logs    (auto if config in /etc/dwellerd/)

Each `checks[i]` and `notifiers[i]` is normalized to a CheckConfig/NotifierConfig
dataclass. Type-specific keys (warn_pct, bot_token, etc.) live in the .options
dict — the corresponding handler module reads them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class CheckConfig:
    type: str
    name: str
    interval: float
    options: dict = field(default_factory=dict)
    # Per-check soft-enable. Missing/true = run; false = skipped at the
    # task layer. The scheduler still fires the kick — toggling at the
    # kick site would require rebuilding the periodic loops.
    enabled: bool = True


@dataclass
class NotifierConfig:
    type: str
    options: dict = field(default_factory=dict)
    enabled: bool = True


@dataclass
class Config:
    checks: list[CheckConfig] = field(default_factory=list)
    notifiers: list[NotifierConfig] = field(default_factory=list)
    report: dict[str, Any] | None = None
    logs: dict[str, Any] | None = None
    web: dict[str, Any] | None = None
    db: dict[str, Any] | None = None

    # Where the daemon writes runtime state. Resolves to /var/lib/dwellerd in
    # production (config in /etc/dwellerd/) or ./data + ./logs in dev mode.
    data_dir: Path = field(default_factory=lambda: Path("data"))
    logs_dir: Path = field(default_factory=lambda: Path("logs"))


def load_config(path: Path | str) -> Config:
    """Load YAML from `path` and return a Config. Missing file → empty Config.
    Malformed YAML or wrong top-level type raises."""
    p = Path(path)
    if not p.exists():
        return Config()

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{p}: top-level YAML must be a mapping, got {type(raw).__name__}")

    checks = [
        CheckConfig(
            type=item.pop("type"),
            name=item.pop("name"),
            interval=float(item.pop("interval", 60)),
            enabled=bool(item.pop("enabled", True)),
            options=item,
        )
        for item in (raw.get("checks") or [])
    ]
    notifiers = [
        NotifierConfig(
            type=item.pop("type"),
            enabled=bool(item.pop("enabled", True)),
            options=item,
        )
        for item in (raw.get("notifiers") or [])
    ]

    if str(p).startswith("/etc/dwellerd"):
        default_data = Path("/var/lib/dwellerd/data")
        default_logs = Path("/var/lib/dwellerd/logs")
    else:
        default_data = Path("data")
        default_logs = Path("logs")

    return Config(
        checks=checks,
        notifiers=notifiers,
        report=raw.get("report"),
        logs=raw.get("logs"),
        web=raw.get("web"),
        db=raw.get("db"),
        data_dir=Path(raw.get("data_dir") or default_data),
        logs_dir=Path(raw.get("logs_dir") or default_logs),
    )
