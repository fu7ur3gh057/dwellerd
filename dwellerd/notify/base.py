from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Alert:
    check: str
    level: str                  # "ok" | "warn" | "crit"
    detail: str
    kind: str = ""
    metrics: dict = field(default_factory=dict)


class Notifier(Protocol):
    async def send_alert(self, alert: Alert) -> None: ...
    async def send_text(self, text: str) -> None: ...
