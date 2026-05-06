import asyncio

from .base import Result


class SystemdUnitCheck:
    def __init__(self, name: str, interval: float, unit: str) -> None:
        self.name = name
        self.interval = interval
        self.unit = unit

    async def run(self) -> Result:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", self.unit,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await proc.communicate()
        except FileNotFoundError:
            return Result(
                level="crit", kind="systemd",
                metrics={"unit": self.unit, "state": "systemctl not found"},
                detail="systemctl not found",
            )
        state = stdout.decode().strip() or "unknown"
        if state == "active":
            return Result(
                level="ok", kind="systemd",
                metrics={"unit": self.unit, "state": state},
                detail=f"{self.unit} active",
            )
        return Result(
            level="crit", kind="systemd",
            metrics={"unit": self.unit, "state": state},
            detail=f"{self.unit} {state}",
        )
