import asyncpg

from .base import SectionResult


class PostgresSection:
    def __init__(
        self,
        dsn: str,
        label: str = "Postgres",
        warn_conns: int = 50,
    ) -> None:
        self.dsn = dsn
        self.label = label
        self.warn_conns = int(warn_conns)

    async def render(self) -> SectionResult:
        try:
            conn = await asyncpg.connect(self.dsn, timeout=5)
        except Exception as e:
            return SectionResult(
                text=f"🗄 {self.label} · ⚠️ {type(e).__name__}",
                warnings=[f"postgres {self.label}: {e}"],
            )
        try:
            size = await conn.fetchval(
                "SELECT pg_size_pretty(pg_database_size(current_database()))"
            )
            conns = await conn.fetchval(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database()"
            )
        finally:
            await conn.close()

        warnings = (
            [f"postgres {self.label}: {conns} conns (>= {self.warn_conns})"]
            if conns >= self.warn_conns else []
        )
        return SectionResult(
            text=f"🗄 {self.label} · size={size} · conns={conns}",
            warnings=warnings,
        )
