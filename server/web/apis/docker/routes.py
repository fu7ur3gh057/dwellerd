"""Docker compose — live `ps` per configured project, no DB.

The same shape is used by the REST endpoint, the dashboard ticker and the
report's docker section, so the collection logic lives in
`collect_docker_snapshot()` and gets imported from both places.
"""

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession

from db.models import AlertEvent
from services.taskiq.broker import broker
from services.taskiq.context import AppContext

log = logging.getLogger(__name__)

router = APIRouter(tags=["docker"])


# ── shared collector ──────────────────────────────────────────────────


async def _ps(compose_path: str) -> list[dict]:
    """`docker compose ps --format json --all` for one compose file.

    Returns the raw container list. Raises on subprocess failure so the
    caller can attach the error to the project entry. Always terminates
    the subprocess on timeout — without that, abandoned `docker compose`
    children pile up and slowly leak file descriptors / RAM."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "-f", compose_path, "ps",
        "--format", "json", "--all",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise RuntimeError(f"docker compose ps timed out after 10s ({compose_path})")

    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or "docker compose failed")
    txt = stdout.decode().strip()
    if not txt:
        return []
    if txt.startswith("["):
        return json.loads(txt)
    return [json.loads(ln) for ln in txt.splitlines() if ln.strip()]


async def _safe_ps(compose: str) -> tuple[list[dict], str | None]:
    """Wrapper around `_ps` that returns (containers, error) instead of
    raising — lets us run a batch in parallel via asyncio.gather without
    one bad project killing the rest."""
    try:
        return await _ps(compose), None
    except Exception as e:
        return [], str(e).splitlines()[0]


async def collect_docker_snapshot() -> dict:
    """Snapshot of every project listed in `config.report.docker`, plus the
    set of standalone (non-compose) containers selected by
    `config.report.docker_standalone`.

    All `docker compose ps` calls fan out in parallel — without this, a
    host with several projects spends N×200ms per tick, which becomes the
    actual bottleneck for the 10s ticker cadence. The standalone collector
    runs concurrently with the compose batch for the same reason.
    """
    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        return {"projects": [], "standalone": _empty_standalone()}

    raw = [p for p in ((ctx.config.report or {}).get("docker") or []) if p.get("compose")]

    # Fan out: compose ps × N + one standalone collect.
    compose_coro = asyncio.gather(*[_safe_ps(p["compose"]) for p in raw]) if raw else None
    standalone_coro = _collect_standalone(ctx)

    if compose_coro is not None:
        results, standalone = await asyncio.gather(compose_coro, standalone_coro)
    else:
        results = []
        standalone = await standalone_coro

    projects: list[dict] = []
    for proj, (containers, err) in zip(raw, results):
        compose = proj["compose"]
        projects.append({
            "compose": compose,
            "project": Path(compose).parent.name or "project",
            "wanted": proj.get("containers") or [],
            "starred": proj.get("starred") or [],
            "containers": containers,
            "error": err,
        })
    return {"projects": projects, "standalone": standalone}


# ── standalone collector ──────────────────────────────────────────────


def _standalone_cfg(ctx: AppContext) -> dict:
    """Pull the standalone block out of report config with safe defaults.

    Shape:
      enabled:        bool   (default False — opt-in)
      names:          list[str] | None
                       null = auto-include every non-compose container
                       []   = explicit empty (= track nothing)
                       [...] = explicit subset by container name
      allow_actions:  bool   (default False — start/stop/restart locked)
      starred:        list[str] (UI hint, optional)

    Defaulting `enabled=False` keeps existing deployments unchanged after
    the upgrade — operator must opt in. `allow_actions=False` is the
    safe-on-shared-VPS posture: an admin token can still SEE foreign
    containers but can't act on them until the operator flips the flag
    in config.
    """
    block = ((ctx.config.report or {}).get("docker_standalone") or {})
    return {
        "enabled":       bool(block.get("enabled", False)),
        "names":         block.get("names"),
        "allow_actions": bool(block.get("allow_actions", False)),
        "starred":       list(block.get("starred") or []),
    }


def _empty_standalone() -> dict:
    return {
        "enabled": False,
        "allow_actions": False,
        "names_mode": "auto",
        "containers": [],
        "starred": [],
        "error": None,
    }


async def _docker_inspect_all() -> tuple[list[dict], str | None]:
    """List every container on the host (running + stopped) and inspect each.

    Two-step instead of `docker ps --format json` because ps's Status field
    is a free-form string ("Up 2 hours (healthy)") that needs regex parsing,
    while inspect returns structured State / Health / Ports we can map
    cleanly to the existing DockerContainer shape.

    Returns (inspected, error). On any subprocess failure returns ([], msg)
    instead of raising — lets the snapshot ticker keep running.
    """
    # Step 1: list all container IDs. Using `docker ps -aq` keeps stdout
    # tiny so this is fast even on busy hosts.
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "ps", "-a", "-q", "--no-trunc",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return [], "docker not installed"

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        return [], "docker ps -aq timed out"

    if proc.returncode != 0:
        return [], (stderr.decode().splitlines() or ["docker ps -aq failed"])[0]

    ids = [ln.strip() for ln in stdout.decode().splitlines() if ln.strip()]
    if not ids:
        return [], None

    # Step 2: bulk inspect — one subprocess for the whole list. Docker
    # natively accepts many ids and returns a JSON array.
    proc = await asyncio.create_subprocess_exec(
        "docker", "container", "inspect", *ids,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        return [], "docker inspect timed out"

    if proc.returncode != 0:
        return [], (stderr.decode().splitlines() or ["docker inspect failed"])[0]

    txt = stdout.decode().strip()
    if not txt:
        return [], None
    try:
        return json.loads(txt), None
    except json.JSONDecodeError as e:
        return [], f"inspect parse failed: {e.msg}"


def _is_compose_container(insp: dict) -> bool:
    labels = (insp.get("Config") or {}).get("Labels") or {}
    return bool(labels.get("com.docker.compose.project"))


def _normalize_container(insp: dict) -> dict:
    """Map `docker container inspect` output → the DockerContainer shape
    the existing UI already understands. Empty Service / Project mark this
    as standalone so the front-end can branch on it."""
    state = insp.get("State") or {}
    config = insp.get("Config") or {}
    labels = config.get("Labels") or {}
    name = (insp.get("Name") or "").lstrip("/")
    image = config.get("Image") or insp.get("Image") or ""
    ports = ((insp.get("NetworkSettings") or {}).get("Ports") or {})

    publishers: list[dict] = []
    seen: set[tuple] = set()
    for spec, bindings in ports.items():
        target_str, _, proto = spec.partition("/")
        try:
            target = int(target_str)
        except ValueError:
            target = 0
        proto = proto or "tcp"
        if not bindings:
            # Internal-only port — represent as TargetPort with PublishedPort=0
            # so the UI's "int" badge logic kicks in (see project-card.tsx).
            key = (0, target, proto)
            if key in seen:
                continue
            seen.add(key)
            publishers.append({
                "URL": "", "TargetPort": target, "PublishedPort": 0, "Protocol": proto,
            })
            continue
        for b in bindings:
            try:
                pub = int(b.get("HostPort") or 0)
            except ValueError:
                pub = 0
            host_ip = b.get("HostIp", "")
            key = (pub, target, proto, host_ip)
            if key in seen:
                continue
            seen.add(key)
            publishers.append({
                "URL": host_ip, "TargetPort": target, "PublishedPort": pub, "Protocol": proto,
            })

    return {
        "ID":         (insp.get("Id") or "")[:12],
        "Name":       name,
        "Image":      image,
        # Empty Service+Project signal "standalone" to the UI; the existing
        # cards already handle this gracefully (no service-level actions).
        "Service":    "",
        "Project":    labels.get("com.docker.compose.project") or "",
        "Created":    _docker_iso_to_epoch(insp.get("Created", "")),
        "State":      state.get("Status", "") or "",
        "Status":     _build_status_from_state(state),
        "Health":     ((state.get("Health") or {}).get("Status") or ""),
        "ExitCode":   int(state.get("ExitCode") or 0),
        "Publishers": publishers,
    }


def _build_status_from_state(state: dict) -> str:
    """Reconstruct docker ps's Status string from inspect data so the UI
    doesn't have to render two different formats."""
    s = (state or {}).get("Status", "") or ""
    started = (state or {}).get("StartedAt", "")
    finished = (state or {}).get("FinishedAt", "")
    exit_code = (state or {}).get("ExitCode", 0)
    if s == "running":
        ago = _epoch_age(started)
        return f"Up {_human_dur(ago)}" if ago > 0 else "Up"
    if s == "exited":
        ago = _epoch_age(finished)
        suffix = f" {_human_dur(ago)} ago" if ago > 0 else ""
        return f"Exited ({exit_code}){suffix}"
    if s == "restarting":
        return f"Restarting ({exit_code})"
    if s == "paused":
        return "Paused"
    if s == "created":
        return "Created"
    if s == "dead":
        return "Dead"
    return s


def _docker_iso_to_epoch(s: str) -> float:
    """Parse '2024-05-05T12:34:56.789012345Z' → epoch seconds. Returns 0
    on any parse miss so the field stays a number for the UI."""
    if not s or s == "0001-01-01T00:00:00Z":
        return 0.0
    raw = s.rstrip("Z")
    if "." in raw:
        whole, _, frac = raw.partition(".")
        # Python's fromisoformat caps fractional seconds at microseconds.
        raw = f"{whole}.{frac[:6]}"
    from datetime import datetime, timezone
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return 0.0


def _epoch_age(iso: str) -> float:
    ep = _docker_iso_to_epoch(iso)
    if ep <= 0:
        return 0.0
    return max(0.0, time.time() - ep)


def _human_dur(secs: float) -> str:
    if secs < 60:
        return f"{int(secs)} seconds"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)} minutes"
    hrs = mins / 60
    if hrs < 24:
        return f"{int(hrs)} hours"
    days = hrs / 24
    return f"{int(days)} days"


async def _collect_standalone(ctx: AppContext) -> dict:
    """Build the standalone snapshot block. Empty/disabled fast paths come
    first so a host without standalones doesn't spawn `docker ps` at all."""
    cfg = _standalone_cfg(ctx)
    if not cfg["enabled"]:
        return _empty_standalone()

    inspected, err = await _docker_inspect_all()
    if err is not None:
        return {
            "enabled": True,
            "allow_actions": cfg["allow_actions"],
            "names_mode": "auto" if cfg["names"] is None else "explicit",
            "containers": [],
            "starred": cfg["starred"],
            "error": err,
        }

    standalone_raw = [c for c in inspected if not _is_compose_container(c)]

    # Filter mode:
    #  - names is None → show everything non-compose (auto mode)
    #  - names is list → only those, in the order given by `Names` match
    if cfg["names"] is None:
        keep = standalone_raw
    else:
        wanted = set(cfg["names"])
        keep = [c for c in standalone_raw
                if (c.get("Name") or "").lstrip("/") in wanted]

    return {
        "enabled":       True,
        "allow_actions": cfg["allow_actions"],
        "names_mode":    "auto" if cfg["names"] is None else "explicit",
        "containers":    [_normalize_container(c) for c in keep],
        "starred":       cfg["starred"],
        "error":         None,
    }


# ── routes ────────────────────────────────────────────────────────────


@router.get("")
async def list_compose() -> dict:
    """Full snapshot: `{projects: [...], standalone: {...}}`. Same shape
    the WS ticker pushes — kept here too so the dashboard can hydrate
    immediately on page load without waiting for the first WS frame.

    Note: this used to return a flat `list[dict]` of projects; the standalone
    addition forced a dict envelope. Old clients that consumed the array
    directly will break; the front-end in this repo was updated alongside.
    """
    return await collect_docker_snapshot()


@router.get("/standalone")
async def list_standalone() -> dict:
    """Just the standalone block from the snapshot. Useful for clients
    that only care about non-compose containers (lighter than fetching
    the full snapshot when projects are noisy)."""
    snap = await collect_docker_snapshot()
    return snap["standalone"]


# ── discovery ─────────────────────────────────────────────────────────


async def _compose_ls() -> list[dict]:
    """`docker compose ls --format json` — every running compose project
    on this host."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "ls", "--format", "json",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise RuntimeError("docker compose ls timed out")

    if proc.returncode != 0:
        raise RuntimeError(stderr.decode().strip() or "docker compose ls failed")
    txt = stdout.decode().strip()
    if not txt:
        return []
    if txt.startswith("["):
        return json.loads(txt)
    return [json.loads(ln) for ln in txt.splitlines() if ln.strip()]


@router.get("/discovered")
async def discovered() -> dict:
    """Things on the host that aren't yet under monitoring.

    Two parallel buckets:
      - `discovered`: compose projects (existing behaviour).
      - `discovered_standalone`: non-compose containers that aren't in the
        explicit `report.docker_standalone.names` list. In auto-mode
        (`names: null`) this bucket is always empty — auto-mode tracks
        everything by definition, so there's nothing left "to discover".
    """
    ctx: AppContext | None = broker.state.data.get("app_ctx")
    monitored: set[str] = set()
    standalone_names: set[str] | None = None  # None = auto / disabled
    if ctx is not None:
        for p in ((ctx.config.report or {}).get("docker") or []):
            cp = p.get("compose")
            if cp:
                monitored.add(str(cp))
        s_cfg = _standalone_cfg(ctx)
        if s_cfg["enabled"] and s_cfg["names"] is not None:
            standalone_names = set(s_cfg["names"])

    # Compose discovery (parallelised against the standalone walk below).
    async def _compose_discover() -> tuple[list[dict], str | None]:
        try:
            all_projects = await _compose_ls()
        except Exception as e:
            return [], str(e).splitlines()[0]

        candidates = []
        for p in all_projects:
            cf = (p.get("ConfigFiles") or "").split(",")[0].strip()
            if not cf or cf in monitored:
                continue
            candidates.append((p, cf))
        if not candidates:
            return [], None

        probe_results = await asyncio.gather(*[_safe_ps(cf) for _, cf in candidates])
        out: list[dict] = []
        for (p, cf), (containers, _err) in zip(candidates, probe_results):
            services = sorted({c.get("Service") for c in containers if c.get("Service")})
            ports = sorted({
                int(pub.get("PublishedPort"))
                for c in containers
                for pub in (c.get("Publishers") or [])
                if pub.get("PublishedPort") and int(pub.get("PublishedPort", 0)) > 0
            })
            out.append({
                "name": p.get("Name", Path(cf).parent.name or "?"),
                "status": p.get("Status", ""),
                "compose": cf,
                "services": services,
                "ports": ports,
            })
        return out, None

    async def _standalone_discover() -> tuple[list[dict], str | None]:
        # Auto mode (or block disabled / not even configured) → no
        # "untracked" set to enumerate; show nothing.
        if standalone_names is None:
            return [], None

        inspected, err = await _docker_inspect_all()
        if err is not None:
            return [], err
        out: list[dict] = []
        for raw in inspected:
            if _is_compose_container(raw):
                continue
            name = (raw.get("Name") or "").lstrip("/")
            if not name or name in standalone_names:
                continue
            n = _normalize_container(raw)
            out.append({
                "name":   name,
                "image":  n["Image"],
                "state":  n["State"],
                "status": n["Status"],
                "ports": sorted({
                    p["PublishedPort"]
                    for p in (n.get("Publishers") or [])
                    if p.get("PublishedPort", 0) > 0
                }),
            })
        return out, None

    (proj_discovered, proj_err), (st_discovered, st_err) = await asyncio.gather(
        _compose_discover(), _standalone_discover(),
    )
    return {
        "discovered": proj_discovered,
        "error": proj_err,
        "discovered_standalone": st_discovered,
        "standalone_error": st_err,
    }


# ── actions ───────────────────────────────────────────────────────────


_PROJECT_ACTIONS = {
    "up":      ["up", "-d", "--remove-orphans"],
    "down":    ["down"],
    "restart": ["restart"],
    "pull":    ["pull"],
}

_SERVICE_ACTIONS = {
    "start":   ["start"],
    "stop":    ["stop"],
    "restart": ["restart"],
}


def _resolve_compose(project: str) -> str:
    """Map URL-friendly project name → compose file path. Looks up
    `config.report.docker[].compose` whose parent dir matches `project`.

    Important: this only resolves `project`→path; it does NOT validate the
    path against the allowed_dirs whitelist. Pair with `_validate_compose_path`
    everywhere this is used to ACT on the project (compose up/down/etc).
    Skip the validator for read-only flows (snapshot, unmonitor cleanup) so
    the operator can still clean up rogue entries that slipped past."""
    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")
    for p in ((ctx.config.report or {}).get("docker") or []):
        compose = p.get("compose")
        if not compose:
            continue
        name = Path(compose).parent.name or "project"
        if name == project:
            return compose
    raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown project {project!r}")


def _validate_compose_path(compose: str, ctx: AppContext) -> Path:
    """Confirm `compose` resolves under one of `report.docker_allowed_dirs`.

    Defends against multi-tenant bleed on shared VPS: without this gate, an
    authenticated admin (or stolen JWT) could ask the daemon to monitor or
    run compose actions against any compose file on disk — including
    /var/www/<other-tenant>/docker-compose.yaml. Resolves symlinks via
    `Path.resolve()` so a symlink under an allowed root pointing elsewhere
    doesn't slip through.

    Returns the resolved Path on success. Raises HTTP 400 on miss with a
    message that tells the operator exactly what to add to config.

    Existing entries in `report.docker[]` from before allowed_dirs was
    configured are NOT pre-filtered here — they keep showing up in the
    snapshot ticker. The gate only triggers on action attempts (monitor add,
    project up/down/restart/pull, service start/stop/restart). Operators can
    list & remove them via the dashboard's stop-monitoring button without
    a whitelist entry.
    """
    try:
        target = Path(compose).resolve()
    except (OSError, RuntimeError):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"compose path {compose!r} is not resolvable")

    allowed = ((ctx.config.report or {}).get("docker_allowed_dirs") or [])
    if not allowed:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "compose path validation refused: 'report.docker_allowed_dirs' is "
            "empty. On shared hosts this is required — set it to the list of "
            "directories where YOUR compose stacks live (e.g. "
            "['/srv/compose', '/home/<you>/projects']) before monitoring "
            "anything or running compose actions.",
        )

    for root in allowed:
        try:
            base = Path(root).resolve()
        except (OSError, RuntimeError):
            continue
        # `is_relative_to` works for "is exactly base" (returns True) and
        # "is a strict descendant of base". Both are fine — the leaf compose
        # file under base is what we want.
        if target == base or target.is_relative_to(base):
            return target

    raise HTTPException(
        status.HTTP_400_BAD_REQUEST,
        f"compose path {compose!r} is not under any allowed_dirs root "
        f"({allowed}). Add the parent directory to "
        f"'report.docker_allowed_dirs' if this is intentional.",
    )


async def _run_compose(compose: str, args: list[str], timeout: float = 90) -> dict:
    """Run `docker compose -f <compose> <args>`. Returns
    `{ok, code, stdout, stderr}`."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "-f", compose, *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "docker compose action timed out")
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": stdout.decode("utf-8", "replace").strip(),
        "stderr": stderr.decode("utf-8", "replace").strip(),
    }


async def _record_action(project: str, service: str | None, action: str, ok: bool, detail: str) -> None:
    """Drop a row into the alerts table so docker actions surface in the
    activity feed alongside check alerts."""
    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        return
    try:
        async with sm() as session:  # type: AsyncSession
            tag = f"docker:{project}" + (f"/{service}" if service else "")
            session.add(AlertEvent(
                ts=time.time(),
                name=tag,
                level="ok" if ok else "warn",
                kind="docker_action",
                detail=f"{action}: {detail[:200]}" if detail else action,
                metrics=None,
            ))
            await session.commit()
    except Exception:
        log.exception("docker: failed to record action %s on %s", action, project)


# ── monitor (add to Settings) ─────────────────────────────────────────


@router.post("/monitor")
async def monitor(payload: dict) -> dict:
    """Adopt a discovered compose project under monitoring.

    - Mutates `config.report.docker` in-memory so /api/docker reflects it
      on the very next snapshot (no restart needed for the running
      session).
    - Persists the change to config.yaml so it survives reboots. Uses
      yaml.safe_dump — works for wizard-generated configs but loses
      hand-written comments. The response includes a warning if any
      comments were detected so the user can choose to revert.
    """
    compose = (payload or {}).get("compose")
    if not compose:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing 'compose'")
    services = (payload or {}).get("services") or []

    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")

    # Whitelist gate — refuse paths outside report.docker_allowed_dirs.
    # Defense against an authed admin (or stolen JWT) asking the daemon to
    # ingest a foreign tenant's compose file on a shared VPS.
    _validate_compose_path(compose, ctx)

    # Ensure report.docker exists, then append (idempotent: skip if present).
    report = ctx.config.report
    if report is None:
        ctx.config.report = report = {}
    docker_list = report.setdefault("docker", [])
    already = any((p.get("compose") == compose) for p in docker_list)
    entry = {"compose": compose}
    if services:
        entry["containers"] = list(services)
    if not already:
        docker_list.append(entry)

    # Auto-wire docker log capture for each service if the user already
    # has a `logs:` block — one source per service. Pattern is broad
    # enough to catch the usual error/warn/fatal/exception/traceback
    # vocabulary; the user can tighten it later in config.yaml.
    log_sources_added: list[str] = []
    logs_cfg = ctx.config.logs
    if isinstance(logs_cfg, dict) and isinstance(logs_cfg.get("sources"), list) and services:
        log_sources = logs_cfg["sources"]
        from core.logs.sources.docker import DockerLogSource
        processor = broker.state.data.get("log_processor")
        for svc in services:
            sname = f"docker-{svc}"
            if any(s.get("name") == sname for s in log_sources):
                continue
            entry_log = {
                "type": "docker",
                "name": sname,
                "compose": compose,
                "service": svc,
                "pattern": r"(?i)error|warn|fatal|fail|exception|critical|traceback",
                "poll_interval": 30,
            }
            log_sources.append(entry_log)
            log_sources_added.append(sname)
            # Hot-plug into the running processor so logs flow without a
            # daemon restart. If the processor isn't up yet (no logs in
            # config at boot), we still wrote the source — restart picks
            # it up.
            if processor is not None:
                try:
                    processor.add_source(DockerLogSource(
                        name=sname, compose=compose, service=svc,
                        pattern=entry_log["pattern"],
                        poll_interval=float(entry_log["poll_interval"]),
                    ))
                except Exception:
                    log.exception("docker.monitor: hot-plug log source failed for %s", sname)

    # Persist to the Settings DB row — config.yaml is boot-only after
    # migration f165869, so a yaml-only write would silently revert on
    # the next daemon restart. Same path the unmonitor / standalone /
    # settings routes use.
    warning: str | None = None
    try:
        await _persist_settings_report(ctx)
    except HTTPException:
        raise
    except Exception:
        log.exception("docker.monitor: settings persist failed")
        warning = "in-memory only — failed to persist Settings (see server logs)"

    # Push a fresh snapshot so the project card moves from Discovery to
    # the regular list immediately.
    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    return {
        "ok": True,
        "compose": compose,
        "already_monitored": already,
        "log_sources_added": log_sources_added,
        "warning": warning,
    }


@router.delete("/monitor/{project}")
async def unmonitor(project: str) -> dict:
    """Stop monitoring a docker compose project.

    Inverse of `monitor`: removes the project from `config.report.docker`,
    drops every `config.logs.sources` entry whose `compose` matches (so
    the daemon stops polling a path that no longer exists, e.g. when
    `/tmp/<stack>/` got reaped), persists both sections to the Settings
    row, and hot-removes the matching log consumers from the running
    LogProcessor so the warning flood stops without a daemon restart.

    Existing rows in `log_events` aren't touched — only future captures
    are cut off. The compose stack itself is NOT brought down; this is a
    monitoring-config edit, not a docker action.
    """
    import time as _time

    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")

    # Resolve URL-friendly name → compose path; raises 404 if unknown.
    compose = _resolve_compose(project)

    # Drop from report.docker (in-memory).
    report = ctx.config.report or {}
    report["docker"] = [p for p in (report.get("docker") or []) if p.get("compose") != compose]
    ctx.config.report = report

    # Drop matching log sources (in-memory) and remember names for hot-remove.
    removed_log_sources: list[str] = []
    if isinstance(ctx.config.logs, dict):
        kept: list[dict] = []
        for s in ctx.config.logs.get("sources") or []:
            if s.get("type") == "docker" and s.get("compose") == compose:
                removed_log_sources.append(s.get("name", ""))
            else:
                kept.append(s)
        ctx.config.logs["sources"] = kept

    # Hot-remove consumers from the running processor.
    processor = broker.state.data.get("log_processor")
    if processor is not None:
        for name in removed_log_sources:
            if not name:
                continue
            try:
                processor.remove_source(name)
            except Exception:
                log.exception("docker.unmonitor: hot-remove failed for %s", name)

    # Persist to the Settings row (the post-migration source of truth —
    # config.yaml is boot-only). Without this, the change reverts on next
    # daemon restart.
    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "db not ready")
    try:
        from db.models import Settings
        from sqlmodel import select as sm_select
        async with sm() as session:  # type: AsyncSession
            row = (await session.exec(sm_select(Settings).where(Settings.id == 1))).first()
            if row is None:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "Settings row missing — daemon hasn't seeded the DB yet",
                )
            row.report = ctx.config.report
            row.logs = ctx.config.logs
            row.updated_at = _time.time()
            session.add(row)
            await session.commit()
    except HTTPException:
        raise
    except Exception:
        log.exception("docker.unmonitor: settings persist failed")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "failed to persist settings",
        )

    # Push a fresh snapshot so the project card disappears from the UI
    # without waiting on the next 10s tick.
    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    return {
        "ok": True,
        "compose": compose,
        "removed_log_sources": removed_log_sources,
    }


# ── standalone monitor / unmonitor / actions ──────────────────────────
#
# These three routes are declared BEFORE the generic /{project}/{action}
# and /{project}/{service}/{action} catch-alls below. FastAPI matches in
# registration order so without this placement, a request to
# `POST /standalone/foo/start` would be eaten by service_action.


async def _persist_settings_report(ctx: AppContext) -> None:
    """UPDATE settings SET report=?, logs=? — for routes that mutate the
    in-memory config. Post-migration the Settings row is the source of
    truth; without this the change reverts on the next daemon restart."""
    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "db not ready")
    try:
        from db.models import Settings
        from sqlmodel import select as sm_select
        async with sm() as session:  # type: AsyncSession
            row = (await session.exec(sm_select(Settings).where(Settings.id == 1))).first()
            if row is None:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "Settings row missing — daemon hasn't seeded the DB yet",
                )
            row.report = ctx.config.report
            row.logs = ctx.config.logs
            row.updated_at = time.time()
            session.add(row)
            await session.commit()
    except HTTPException:
        raise
    except Exception:
        log.exception("settings persist failed")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "failed to persist settings",
        )


@router.post("/standalone/monitor")
async def standalone_monitor(payload: dict) -> dict:
    """Adopt a non-compose container into the tracked-standalone list.

    Behaviour:
      - If the block is missing or `enabled=false`, this turns it on.
      - If `names` is null (auto-mode), switches to explicit mode and seeds
        the list with this single name. Auto-mode "tracks everything"
        already, but converting to explicit reflects the operator's intent
        to lock the set down.
      - If `names` is a list, appends idempotently.
      - If `logs:` is configured AND the body's `collect_logs` is not
        explicitly false, also auto-adds a `docker_container` log source
        and hot-plugs it into the running LogProcessor — so the operator
        sees the container's stdout/stderr in /logs without a daemon
        restart. Mirrors the compose adopt flow.
    Persists to the Settings row.
    """
    name = (payload or {}).get("name")
    if not name or not isinstance(name, str):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing or invalid 'name'")
    collect_logs = (payload or {}).get("collect_logs", True)

    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")

    report = dict(ctx.config.report or {})
    block = dict(report.get("docker_standalone") or {})
    block["enabled"] = True
    names = block.get("names")
    if names is None:
        names = []
    if name not in names:
        names.append(name)
    block["names"] = names
    # `allow_actions` left untouched — adopting a container shouldn't
    # silently grant the daemon power to start/stop it. Operator opts
    # into actions explicitly via config.
    report["docker_standalone"] = block
    ctx.config.report = report

    log_source_added: str | None = None
    if collect_logs and isinstance(ctx.config.logs, dict) \
            and isinstance(ctx.config.logs.get("sources"), list):
        log_sources = ctx.config.logs["sources"]
        sname = f"docker-c-{name}"
        if not any(s.get("name") == sname for s in log_sources):
            entry = {
                "type":          "docker_container",
                "name":          sname,
                "container":     name,
                # No regex narrowing here — the global logs.level filter
                # already drops noise. Operators can tighten per-source
                # via Settings if they want.
                "pattern":       ".+",
                "poll_interval": 30,
            }
            log_sources.append(entry)
            log_source_added = sname
            # Hot-plug into the running LogProcessor.
            processor = broker.state.data.get("log_processor")
            if processor is not None:
                from core.logs.sources.docker_container import DockerContainerLogSource
                try:
                    processor.add_source(DockerContainerLogSource(
                        name=sname, container=name,
                        pattern=entry["pattern"],
                        poll_interval=float(entry["poll_interval"]),
                    ))
                except Exception:
                    log.exception(
                        "standalone.monitor: hot-plug log source failed for %s",
                        sname,
                    )

    await _persist_settings_report(ctx)

    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    return {
        "ok": True,
        "name": name,
        "names": names,
        "log_source_added": log_source_added,
    }


@router.delete("/standalone/monitor/{name}")
async def standalone_unmonitor(name: str) -> dict:
    """Remove a standalone container from tracking.

    In explicit-list mode, drops the name from the list (idempotent).
    In auto mode (`names: null`), the request switches the block to
    explicit `names=[]` — auto mode "tracks everything", so the only
    way to "untrack one" is to lock the list down. The operator can
    re-adopt others from the discovery section.

    Also drops any matching `docker_container` log source the adopt
    flow auto-created, and hot-removes it from the running LogProcessor.
    """
    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")

    report = dict(ctx.config.report or {})
    block = dict(report.get("docker_standalone") or {})
    names = block.get("names")
    if names is None:
        names = []
    elif name in names:
        names = [n for n in names if n != name]
    block["names"] = names
    report["docker_standalone"] = block
    ctx.config.report = report

    # Symmetric log-source teardown: remove `docker_container` source(s)
    # whose `container` field matches this name. Existing rows in the
    # log_events table are kept; only future captures stop.
    removed_log_sources: list[str] = []
    if isinstance(ctx.config.logs, dict):
        kept: list[dict] = []
        for s in ctx.config.logs.get("sources") or []:
            if s.get("type") == "docker_container" and s.get("container") == name:
                removed_log_sources.append(s.get("name", ""))
            else:
                kept.append(s)
        ctx.config.logs["sources"] = kept

    processor = broker.state.data.get("log_processor")
    if processor is not None:
        for sname in removed_log_sources:
            if not sname:
                continue
            try:
                processor.remove_source(sname)
            except Exception:
                log.exception("standalone.unmonitor: hot-remove failed for %s", sname)

    await _persist_settings_report(ctx)

    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    return {
        "ok": True,
        "name": name,
        "names": names,
        "removed_log_sources": removed_log_sources,
    }


_STANDALONE_ACTIONS = {
    "start":   ["start"],
    "stop":    ["stop"],
    "restart": ["restart"],
}


@router.post("/standalone/{name}/{action}")
async def standalone_action(name: str, action: str) -> dict:
    """Run a `docker <action> <name>` on a tracked standalone container.

    Gated by:
      - `report.docker_standalone.enabled`        — block must be on
      - `report.docker_standalone.allow_actions`  — operator opt-in
      - explicit-mode list (when `names` is a list, the target must be in it)

    Auto mode (`names: null`) lets actions through to any non-compose
    container the daemon's docker socket exposes. On a shared VPS this
    can reach OTHER tenants' containers — that's why `allow_actions`
    defaults to false.
    """
    if action not in _STANDALONE_ACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown action {action!r}")

    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")

    cfg = _standalone_cfg(ctx)
    if not cfg["enabled"]:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "standalone monitoring is disabled")
    if not cfg["allow_actions"]:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "actions are disabled — set report.docker_standalone.allow_actions: true",
        )
    if cfg["names"] is not None and name not in cfg["names"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"standalone container {name!r} not tracked")

    log.info("docker action: standalone=%s action=%s", name, action)
    proc = await asyncio.create_subprocess_exec(
        "docker", *_STANDALONE_ACTIONS[action], name,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except asyncio.TimeoutError:
        if proc.returncode is None:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except (asyncio.TimeoutError, ProcessLookupError):
                pass
        raise HTTPException(status.HTTP_504_GATEWAY_TIMEOUT, "docker action timed out")

    res = {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": stdout.decode("utf-8", "replace").strip(),
        "stderr": stderr.decode("utf-8", "replace").strip(),
    }
    detail = res["stderr"] or res["stdout"]
    # Reuse the alerts feed shape — surface the action under a synthetic
    # "standalone:<name>" project tag so it sorts alongside compose actions.
    await _record_action(f"standalone:{name}", None, action, res["ok"], detail)

    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    return res


@router.post("/{project}/{action}")
async def project_action(project: str, action: str) -> dict:
    """Run a project-level docker compose action.

    Allowed actions: up, down, restart, pull. Each runs with a 90s
    timeout; result is logged to the alerts table so it shows up in the
    UI's recent-activity feed."""
    if action not in _PROJECT_ACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown action {action!r}")
    compose = _resolve_compose(project)
    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")
    _validate_compose_path(compose, ctx)
    log.info("docker action: project=%s action=%s", project, action)
    res = await _run_compose(compose, _PROJECT_ACTIONS[action])
    detail = res["stderr"] or res["stdout"]
    await _record_action(project, None, action, res["ok"], detail)

    # Push a fresh snapshot so the dashboard reflects the new state without
    # waiting for the next 15s tick.
    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    return res


@router.post("/{project}/{service}/{action}")
async def service_action(project: str, service: str, action: str) -> dict:
    """Run a per-service action: start, stop, restart."""
    if action not in _SERVICE_ACTIONS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown action {action!r}")
    compose = _resolve_compose(project)
    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")
    _validate_compose_path(compose, ctx)
    log.info("docker action: project=%s service=%s action=%s", project, service, action)
    res = await _run_compose(compose, [*_SERVICE_ACTIONS[action], service])
    detail = res["stderr"] or res["stdout"]
    await _record_action(project, service, action, res["ok"], detail)

    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    return res
