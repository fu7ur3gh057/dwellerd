"""Service-liveness checks: HTTP probes and systemd units."""
from __future__ import annotations

import asyncio

import httpx

from .base import Result


class HttpCheck:
    kind = "http"

    def __init__(
        self, name: str, interval: float, url: str,
        expect_status: int = 200, timeout: float = 10.0, proxy: str = "",
    ) -> None:
        self.name, self.interval = name, interval
        self.url = url
        self.expect_status = expect_status
        self.timeout = timeout
        self.proxy = proxy or None

    async def run(self) -> Result:
        kwargs: dict = {"timeout": self.timeout, "follow_redirects": True}
        if self.proxy:
            kwargs["proxy"] = self.proxy
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.get(self.url)
        except Exception as e:
            # Connection refused, DNS failure, TLS error, timeout — all are
            # "the endpoint is down" from the operator's point of view.
            summary = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            return Result(
                level="crit", kind="http",
                metrics={"url": self.url, "summary": summary},
                detail=summary,
            )
        if response.status_code != self.expect_status:
            return Result(
                level="crit", kind="http",
                metrics={"url": self.url,
                         "summary": f"status {response.status_code}"},
                detail=f"status {response.status_code} != {self.expect_status}",
            )
        return Result(
            level="ok", kind="http",
            metrics={"url": self.url, "summary": f"status {response.status_code}"},
            detail=f"status {response.status_code}",
        )


class SystemdCheck:
    kind = "systemd"

    def __init__(self, name: str, interval: float, unit: str) -> None:
        self.name, self.interval = name, interval
        self.unit = unit

    async def run(self) -> Result:
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", self.unit,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        except FileNotFoundError:
            return Result(
                level="crit", kind="systemd",
                metrics={"unit": self.unit, "state": "systemctl not found"},
                detail="systemctl not found",
            )
        except asyncio.TimeoutError:
            return Result(
                level="crit", kind="systemd",
                metrics={"unit": self.unit, "state": "timeout"},
                detail=f"systemctl is-active {self.unit} timed out",
            )
        # `is-active` exits non-zero for anything but "active", so read the
        # word it printed rather than the return code — "failed",
        # "inactive" and "activating" are different stories.
        state = stdout.decode(errors="replace").strip() or "unknown"
        if state == "active":
            return Result(
                level="ok", kind="systemd",
                metrics={"unit": self.unit, "state": state},
                detail=f"{self.unit} active",
            )
        if state == "activating":
            return Result(
                level="warn", kind="systemd",
                metrics={"unit": self.unit, "state": state},
                detail=f"{self.unit} activating",
            )
        return Result(
            level="crit", kind="systemd",
            metrics={"unit": self.unit, "state": state},
            detail=f"{self.unit} {state}",
        )
