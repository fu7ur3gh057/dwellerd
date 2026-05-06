from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Alert:
    check: str
    level: str  # "ok" | "warn" | "crit"
    detail: str
    kind: str = ""
    metrics: dict = field(default_factory=dict)


class Notifier(Protocol):
    async def send(self, alert: Alert) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def send_startup(self) -> None: ...
    async def send_shutdown(self) -> None: ...
    async def send_log_first(self, source: str, sample: str) -> None: ...
    async def send_log_digest(self, items: list[dict], period_label: str = "") -> None: ...
