from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class SectionResult:
    text: str
    warnings: list[str] = field(default_factory=list)


class Section(Protocol):
    async def render(self) -> SectionResult: ...
