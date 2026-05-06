"""Long-running `docker events` consumer.

Spawns a single subprocess that reads `docker events --format json`,
filters out the noisy exec_* events that fire on every healthcheck, and
emits the interesting ones over WS as `docker:event`. Survives docker
daemon restarts via a reconnect loop.

Started from the FastAPI lifespan (`web.lifetime`); cancellation
propagates through asyncio.CancelledError so a Ctrl-C kills the
subprocess too.
"""

import asyncio
import json
import logging
import time

log = logging.getLogger(__name__)

# Container-level events worth surfacing. The exec_* family fires on
# every healthcheck — useful as raw telemetry, useless as a UX event
# stream — so we drop those.
_INTERESTING_ACTIONS = {
    "start", "stop", "die", "kill", "restart",
    "oom", "pause", "unpause", "destroy", "create",
    "rename", "update", "health_status: healthy",
    "health_status: unhealthy", "health_status: starting",
}

# Subset that changes the visible state on the project cards (running/exited
# count, container present/absent). After one of these we re-fetch the full
# snapshot and push it as `docker:tick` so the UI updates without waiting
# for the next periodic tick.
_STATE_CHANGE_ACTIONS = {
    "start", "stop", "die", "kill", "restart",
    "oom", "pause", "unpause", "destroy", "create",
}


async def run_docker_events() -> None:
    """Reconnecting consumer. One subprocess at a time, exponential
    backoff up to 30s if docker isn't reachable."""
    backoff = 2.0
    while True:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "events",
                "--format", "json",
                "--filter", "type=container",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            log.info("docker-events: subprocess started")
            backoff = 2.0
            await _pump(proc)
        except FileNotFoundError:
            log.error("docker-events: `docker` not in PATH; consumer disabled")
            return
        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    pass
            raise
        except Exception:
            log.exception("docker-events: pump crashed, retrying")
        finally:
            if proc and proc.returncode is None:
                try:
                    proc.terminate()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except (ProcessLookupError, asyncio.TimeoutError):
                    pass

        await asyncio.sleep(backoff)
        backoff = min(30.0, backoff * 1.5)


async def _pump(proc: asyncio.subprocess.Process) -> None:
    from web.sockets import emit
    assert proc.stdout is not None
    seen = 0
    while True:
        raw = await proc.stdout.readline()
        if not raw:
            return  # subprocess died, outer loop will reconnect
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue

        action = ev.get("Action") or ev.get("status") or ""
        # Strip "exec_*" and other noise; allow exact + colon-prefixed matches.
        bare = action.split(":")[0].strip()
        if bare not in _INTERESTING_ACTIONS and action not in _INTERESTING_ACTIONS:
            continue

        attrs = (ev.get("Actor") or {}).get("Attributes") or {}
        target = (
            attrs.get("com.docker.compose.project", "")
            + ("/" + attrs["com.docker.compose.service"] if attrs.get("com.docker.compose.service") else "")
        ) or attrs.get("name", "")
        payload = {
            "ts": ev.get("time") or int(time.time()),
            "action": action,
            "id": (ev.get("id") or "")[:12],
            "image": ev.get("from") or attrs.get("image", ""),
            "container": attrs.get("name", ""),
            "project": attrs.get("com.docker.compose.project", ""),
            "service": attrs.get("com.docker.compose.service", ""),
            "exit_code": _to_int(attrs.get("exitCode")),
        }
        seen += 1
        log.info("docker-events: %s %s (#%d)", action, target, seen)
        await emit("/docker", "docker:event", payload)

        # State-change events make the next periodic tick stale by ~10s.
        # Push a fresh snapshot now so project cards update at the same
        # moment the events strip mentions the change. Debounced so a
        # burst (e.g. `compose down` firing kill+die+destroy×N) collapses
        # into a single snapshot rebuild.
        if bare in _STATE_CHANGE_ACTIONS:
            _schedule_fresh_snapshot()


# 500ms debounce so a flurry of state-change events triggers one snapshot
# rebuild rather than N concurrent ones. Each rebuild fans out
# `docker compose ps` for every project in parallel, so even a single
# extra round-trip per event would pile up under load.
_DEBOUNCE_S = 0.5
_pending_snapshot_task: asyncio.Task | None = None


def _schedule_fresh_snapshot() -> None:
    global _pending_snapshot_task
    if _pending_snapshot_task is not None and not _pending_snapshot_task.done():
        return  # already queued
    _pending_snapshot_task = asyncio.create_task(_push_fresh_snapshot_debounced())


async def _push_fresh_snapshot_debounced() -> None:
    global _pending_snapshot_task
    try:
        await asyncio.sleep(_DEBOUNCE_S)
        from web.apis.docker.routes import collect_docker_snapshot
        from web.sockets import emit
        snap = await collect_docker_snapshot()
        await emit("/docker", "docker:tick", snap)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("docker-events: fresh snapshot push failed")
    finally:
        _pending_snapshot_task = None


def _to_int(v) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
