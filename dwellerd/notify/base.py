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
    ongoing: float = 0.0        # > 0 marks a reminder: seconds the problem has lasted
    target_id: int = 0          # short id the Telegram buttons refer to; 0 = no buttons


class Notifier(Protocol):
    async def send_alert(self, alert: Alert, silent: bool = False) -> None: ...
    async def send_text(self, text: str, silent: bool = False) -> None: ...
