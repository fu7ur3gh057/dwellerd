from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class Result:
    level: str  # "ok" | "warn" | "crit"
    detail: str = ""
    kind: str = ""              # "cpu" | "memory" | "disk" | "http" — used for templates
    metrics: dict = field(default_factory=dict)


class Check(Protocol):
    name: str
    interval: float

    async def run(self) -> Result: ...
