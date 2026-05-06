import shutil

from .base import Result


class DiskCheck:
    def __init__(
        self,
        name: str,
        interval: float,
        path: str = "/",
        warn_pct: float = 80.0,
        crit_pct: float = 90.0,
    ) -> None:
        self.name = name
        self.interval = interval
        self.path = path
        self.warn_pct = float(warn_pct)
        self.crit_pct = float(crit_pct)

    async def run(self) -> Result:
        u = shutil.disk_usage(self.path)
        pct = u.used / u.total * 100
        free_gb = u.free / 1024 ** 3
        base = {"value": pct, "threshold": self.warn_pct, "path": self.path, "free_gb": free_gb}
        if pct >= self.crit_pct:
            return Result(
                level="crit",
                kind="disk",
                metrics={**base, "threshold": self.crit_pct},
                detail=f"disk {self.path} {pct:.1f}% >= {self.crit_pct:.0f}%",
            )
        if pct >= self.warn_pct:
            return Result(
                level="warn",
                kind="disk",
                metrics=base,
                detail=f"disk {self.path} {pct:.1f}% >= {self.warn_pct:.0f}%",
            )
        return Result(
            level="ok",
            kind="disk",
            metrics=base,
            detail=f"disk {self.path} {pct:.1f}%",
        )
