import asyncpg

from .base import SectionResult


class DlqSection:
    """Generic DLQ section: runs a SQL query that returns a single integer count."""

    def __init__(
        self,
        dsn: str,
        query: str,
        label: str = "DLQ",
        warn_above: int = 1,
    ) -> None:
        self.dsn = dsn
        self.query = query
        self.label = label
        self.warn_above = int(warn_above)

    async def render(self) -> SectionResult:
        try:
            conn = await asyncpg.connect(self.dsn, timeout=5)
        except Exception as e:
            return SectionResult(
                text=f"📤 {self.label}: ⚠️ {type(e).__name__}",
                warnings=[f"dlq {self.label}: {e}"],
            )
        try:
            count = await conn.fetchval(self.query)
        finally:
            await conn.close()

        count = int(count or 0)
        icon = "🟢" if count < self.warn_above else "🟡"
        warnings = (
            [f"DLQ {self.label}: {count} entries"] if count >= self.warn_above else []
        )
        return SectionResult(
            text=f"📤 {self.label}: {icon} {count} entries",
            warnings=warnings,
        )
