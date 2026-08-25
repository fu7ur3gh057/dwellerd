"""Docker container state.

Unlike the other checks this one produces *many* results per tick — one
per container — via `run_multi()`. That matters: a single aggregated
"docker is unhappy" level would go crit when the first container dies and
then stay silently crit when the second one follows it. Per-container
state means every container gets its own transition, its own alert and its
own recovery message.

Two modes, and they can be combined:
  - `containers: []` (default) — watch whatever `docker ps -a` reports.
    A container that gets removed deliberately just stops being watched.
  - `containers: [app, db]` — watch exactly these. One that vanishes from
    `docker ps -a` entirely is reported as "not running", which the
    open-ended mode cannot detect.
  - `compose: [/opt/app/docker-compose.yaml]` — every service the compose
    file declares, so a service that never came up is still caught.
"""
from __future__ import annotations

import asyncio
import json
import logging

from .base import Result

log = logging.getLogger(__name__)

_TIMEOUT = 30


async def _run(*argv: str, timeout: int = _TIMEOUT) -> tuple[int, str, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except FileNotFoundError:
        return 127, "", "docker not found"
    except asyncio.TimeoutError:
        return 124, "", f"`{' '.join(argv[:3])}` timed out after {timeout}s"
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", "replace"),
        stderr.decode("utf-8", "replace").strip(),
    )


def _parse_json_lines(text: str) -> list[dict]:
    """`docker ... --format json` emits either one JSON array or one object
    per line, depending on the docker version. Accept both."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return []
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _health(status: str) -> str:
    low = status.lower()
    if "(unhealthy)" in low:
        return "unhealthy"
    if "(healthy)" in low:
        return "healthy"
    if "health: starting" in low or "(starting)" in low:
        return "starting"
    return ""


def _classify(state: str, status: str) -> tuple[str, str]:
    """(level, human state) for one container."""
    state = (state or "").lower()
    health = _health(status)
    if state == "running":
        if health == "unhealthy":
            return "crit", "unhealthy"
        if health == "starting":
            return "warn", "starting"
        return "ok", "running"
    if state == "restarting":
        return "warn", "restarting"
    if state in ("created", "paused"):
        return "warn", state
    return "crit", state or "not running"


class DockerCheck:
    kind = "docker"

    def __init__(
        self, name: str, interval: float,
        containers: list[str] | None = None,
        compose: list[str] | None = None,
    ) -> None:
        self.name, self.interval = name, interval
        self.containers = list(containers or [])
        self.compose = list(compose or [])
        # Docker being unreachable is its own alert, tracked separately so
        # it does not masquerade as "every container went down at once".
        self.daemon_check_name = f"{name}:daemon"

    async def run(self) -> Result:
        """Single-result view — the daemon's reachability. Per-container
        results come from run_multi()."""
        code, _, err = await _run("docker", "version", "--format", "{{.Server.Version}}")
        if code != 0:
            return Result(
                level="crit", kind="docker",
                metrics={"container": "docker", "state": "unreachable",
                         "summary": err or "docker is not responding"},
                detail=err or "docker is not responding",
            )
        return Result(
            level="ok", kind="docker",
            metrics={"container": "docker", "state": "reachable",
                     "summary": "docker is reachable"},
            detail="docker is reachable",
        )

    async def run_multi(self) -> list[tuple[str, Result]]:
        """Return (check_name, result) per container, plus the daemon
        reachability result. When docker itself is down we return *only*
        that, so a dead daemon produces one alert, not fifty."""
        daemon = await self.run()
        results: list[tuple[str, Result]] = [(self.daemon_check_name, daemon)]
        if daemon.level != "ok":
            return results

        seen: dict[str, tuple[str, str]] = {}   # container -> (state, status)

        code, out, err = await _run("docker", "ps", "-a", "--format", "{{json .}}")
        if code != 0:
            log.warning("docker ps failed: %s", err)
        else:
            for item in _parse_json_lines(out):
                name = item.get("Names") or item.get("Name") or ""
                # `docker ps` can return a comma-joined list of aliases.
                name = name.split(",")[0].strip()
                if not name:
                    continue
                seen[name] = (item.get("State", ""), item.get("Status", ""))

        for path in self.compose:
            code, out, err = await _run(
                "docker", "compose", "-f", path, "ps", "--format", "json", "--all",
            )
            if code != 0:
                results.append((
                    f"{self.name}:compose:{path}",
                    Result(
                        level="crit", kind="docker",
                        metrics={"container": path, "state": "compose error",
                                 "summary": (err.splitlines() or ["failed"])[0][:120]},
                        detail=f"compose {path}: {err[:200]}",
                    ),
                ))
                continue
            for item in _parse_json_lines(out):
                name = item.get("Name") or item.get("Service") or ""
                if name:
                    seen.setdefault(name, (item.get("State", ""), item.get("Status", "")))

        wanted = self.containers or sorted(seen)
        for name in wanted:
            if name not in seen:
                results.append((
                    f"{self.name}:{name}",
                    Result(
                        level="crit", kind="docker",
                        metrics={"container": name, "state": "missing",
                                 "summary": "no such container"},
                        detail=f"container {name} does not exist",
                    ),
                ))
                continue
            state, status = seen[name]
            level, human = _classify(state, status)
            results.append((
                f"{self.name}:{name}",
                Result(
                    level=level, kind="docker",
                    metrics={"container": name, "state": human,
                             "summary": status or human},
                    detail=f"container {name} {human}",
                ),
            ))
        return results
