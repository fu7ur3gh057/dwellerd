import asyncio

import psutil

from .base import Result


class CpuCheck:
    def __init__(
        self,
        name: str,
        interval: float,
        warn_pct: float = 80.0,
        crit_pct: float = 90.0,
    ) -> None:
        self.name = name
        self.interval = interval
        self.warn_pct = float(warn_pct)
        self.crit_pct = float(crit_pct)

    async def run(self) -> Result:
        pct = await asyncio.to_thread(psutil.cpu_percent, None)
        if pct >= self.crit_pct:
            return Result(
                level="crit",
                kind="cpu",
                metrics={"value": pct, "threshold": self.crit_pct},
                detail=f"CPU {pct:.1f}% >= {self.crit_pct:.0f}%",
            )
        if pct >= self.warn_pct:
            return Result(
                level="warn",
                kind="cpu",
                metrics={"value": pct, "threshold": self.warn_pct},
                detail=f"CPU {pct:.1f}% >= {self.warn_pct:.0f}%",
            )
        return Result(
            level="ok",
            kind="cpu",
            metrics={"value": pct, "threshold": self.warn_pct},
            detail=f"CPU {pct:.1f}%",
        )
