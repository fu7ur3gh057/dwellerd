"""Check protocol + result type.

A check is anything with a `name`, an `interval` and an async `run()` that
returns a `Result`. `kind` picks the message template in the Telegram
notifier; `metrics` fills that template's placeholders.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Result:
    level: str                  # "ok" | "warn" | "crit"
    detail: str = ""            # plain-text fallback line
    kind: str = ""              # cpu | memory | swap | load | disk | http | systemd | docker
    metrics: dict = field(default_factory=dict)


class Check(Protocol):
    name: str
    interval: float

    async def run(self) -> Result: ...


def by_threshold(
    *, pct: float, warn: float, crit: float, kind: str, label: str,
    extra: dict | None = None, unit: str = "%",
) -> Result:
    """Shared ok/warn/crit ladder for the percentage-style checks."""
    base = {"value": pct, "threshold": warn, **(extra or {})}
    if pct >= crit:
        return Result(
            level="crit", kind=kind,
            metrics={**base, "threshold": crit},
            detail=f"{label} {pct:.1f}{unit} >= {crit:.0f}{unit}",
        )
    if pct >= warn:
        return Result(
            level="warn", kind=kind, metrics=base,
            detail=f"{label} {pct:.1f}{unit} >= {warn:.0f}{unit}",
        )
    return Result(
        level="ok", kind=kind, metrics=base,
        detail=f"{label} {pct:.1f}{unit}",
    )
