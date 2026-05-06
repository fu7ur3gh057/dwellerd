"""Editable runtime settings — admin-only.

Settings live in the singleton `settings` row (one JSON column per
section). config.yaml seeds it on first boot; everything after that
flows through these endpoints. Mutations:

  - update the in-memory `ctx.config.<section>` so the next snapshot
    tick reflects the change without a restart;
  - persist to the Settings row via the same helper the docker monitor
    routes use, so the change survives a daemon restart.

Only the docker monitoring block is exposed for now — that's what the
operator needs to set up `report.docker_standalone` and the
`report.docker_allowed_dirs` whitelist via UI instead of poking SQL.
Other sections (logs / notifiers / checks) can join later with the
same shape.
"""

import logging
import time

from fastapi import APIRouter, HTTPException, status

from db.models import Settings
from services.taskiq.broker import broker
from services.taskiq.context import AppContext
from sqlmodel import select as sm_select
from sqlmodel.ext.asyncio.session import AsyncSession

log = logging.getLogger(__name__)

router = APIRouter(tags=["settings"])


# ── helpers ───────────────────────────────────────────────────────────


def _ctx() -> AppContext:
    ctx: AppContext | None = broker.state.data.get("app_ctx")
    if ctx is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "app context not ready")
    return ctx


async def _persist_settings(ctx: AppContext, *, sections: tuple[str, ...]) -> None:
    """UPDATE settings SET <section>=?, updated_at=? — Settings row is the
    post-migration source of truth. Without this, mutations revert on
    the next daemon restart. `sections` lists which columns to write so
    we don't blindly stomp on unmodified ones (e.g. notifiers)."""
    sm = broker.state.data.get("db_session_maker")
    if sm is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "db not ready")
    try:
        async with sm() as session:  # type: AsyncSession
            row = (await session.exec(sm_select(Settings).where(Settings.id == 1))).first()
            if row is None:
                raise HTTPException(
                    status.HTTP_500_INTERNAL_SERVER_ERROR,
                    "Settings row missing — daemon hasn't seeded the DB yet",
                )
            if "report" in sections:
                row.report = ctx.config.report
            if "logs" in sections:
                row.logs = ctx.config.logs
            if "checks" in sections:
                # Persist as list of dicts; the hydrate path expects type/
                # name/interval/enabled at the top, the rest under options
                # (flattened on the wire for legacy compat).
                row.checks = [
                    {
                        "type":     c.type,
                        "name":     c.name,
                        "interval": c.interval,
                        "enabled":  bool(getattr(c, "enabled", True)),
                        **c.options,
                    }
                    for c in (ctx.config.checks or [])
                ]
            if "notifiers" in sections:
                row.notifiers = [
                    {"type": n.type, "enabled": bool(getattr(n, "enabled", True)), **n.options}
                    for n in (ctx.config.notifiers or [])
                ]
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


# Back-compat alias for the original docker-only persist call.
async def _persist_report(ctx: AppContext) -> None:
    await _persist_settings(ctx, sections=("report",))


# ── docker section ────────────────────────────────────────────────────


@router.get("/docker")
async def get_docker_settings() -> dict:
    """Return the editable docker-monitoring settings.

    Shape:
      {
        docker_allowed_dirs: [str],          # whitelist for compose paths
        docker_standalone: {
          enabled:        bool,
          names:          [str] | null,      # null = auto, list = explicit
          allow_actions:  bool,
          starred:        [str],
        },
      }
    """
    ctx = _ctx()
    report = ctx.config.report or {}
    standalone = dict(report.get("docker_standalone") or {})
    return {
        "docker_allowed_dirs": list(report.get("docker_allowed_dirs") or []),
        "docker_standalone": {
            "enabled":       bool(standalone.get("enabled", False)),
            "names":         standalone.get("names"),
            "allow_actions": bool(standalone.get("allow_actions", False)),
            "starred":       list(standalone.get("starred") or []),
        },
    }


@router.patch("/docker")
async def patch_docker_settings(payload: dict) -> dict:
    """Merge a partial update into report.docker_standalone /
    report.docker_allowed_dirs and persist.

    Accepts any subset of:
      docker_allowed_dirs: [str]
      docker_standalone.enabled: bool
      docker_standalone.names: [str] | null
      docker_standalone.allow_actions: bool
      docker_standalone.starred: [str]

    Unknown keys are ignored. Returns the resulting docker settings (same
    shape as GET) so the UI can immediately reconcile without a refetch.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be an object")

    ctx = _ctx()
    report = dict(ctx.config.report or {})

    if "docker_allowed_dirs" in payload:
        v = payload["docker_allowed_dirs"]
        if not isinstance(v, list) or not all(isinstance(p, str) for p in v):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "docker_allowed_dirs must be a list of strings",
            )
        # Drop empty strings + dedupe while keeping order.
        cleaned: list[str] = []
        seen: set[str] = set()
        for p in v:
            p = p.strip()
            if p and p not in seen:
                seen.add(p)
                cleaned.append(p)
        report["docker_allowed_dirs"] = cleaned

    if "docker_standalone" in payload:
        sub = payload["docker_standalone"]
        if not isinstance(sub, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "docker_standalone must be an object",
            )
        block = dict(report.get("docker_standalone") or {})
        if "enabled" in sub:
            block["enabled"] = bool(sub["enabled"])
        if "allow_actions" in sub:
            block["allow_actions"] = bool(sub["allow_actions"])
        if "names" in sub:
            v = sub["names"]
            if v is not None and (
                not isinstance(v, list) or not all(isinstance(n, str) for n in v)
            ):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "docker_standalone.names must be a list of strings or null",
                )
            block["names"] = v
        if "starred" in sub:
            v = sub["starred"]
            if not isinstance(v, list) or not all(isinstance(n, str) for n in v):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "docker_standalone.starred must be a list of strings",
                )
            block["starred"] = list(v)
        report["docker_standalone"] = block

    ctx.config.report = report
    await _persist_report(ctx)

    # Push a fresh docker snapshot so the UI updates immediately —
    # mirrors what the monitor/unmonitor routes already do. Without this
    # the user has to wait for the 10s tick after toggling `enabled`.
    from web.apis.docker.routes import collect_docker_snapshot
    from web.sockets import emit
    snap = await collect_docker_snapshot()
    await emit("/docker", "docker:tick", snap)

    standalone = dict(report.get("docker_standalone") or {})
    return {
        "docker_allowed_dirs": list(report.get("docker_allowed_dirs") or []),
        "docker_standalone": {
            "enabled":       bool(standalone.get("enabled", False)),
            "names":         standalone.get("names"),
            "allow_actions": bool(standalone.get("allow_actions", False)),
            "starred":       list(standalone.get("starred") or []),
        },
    }


# ── logs section ──────────────────────────────────────────────────────


_LOG_LEVELS = {"all", "info", "warn", "error"}


def _serialize_logs(cfg: dict) -> dict:
    storage = dict(cfg.get("storage") or {})

    def _pick(key: str, default):
        return cfg.get(key, storage.get(key, default))

    # Read-only view of the registered sources for the Settings UI. We
    # echo the config dicts with `target` derived from the type-specific
    # field (path / unit / compose+service / container) so the front-end
    # doesn't have to branch on type to display the key bit.
    raw_sources = cfg.get("sources") or []
    sources: list[dict] = []
    for s in raw_sources:
        if not isinstance(s, dict):
            continue
        kind = s.get("type", "")
        if kind == "file":
            target = s.get("path", "")
        elif kind == "journal":
            target = s.get("unit", "")
        elif kind == "docker":
            target = f"{s.get('compose', '')} :: {s.get('service', '')}"
        elif kind == "docker_container":
            target = s.get("container", "")
        else:
            target = ""
        sources.append({
            "type":          kind,
            "name":          s.get("name", ""),
            "target":        target,
            "pattern":       s.get("pattern", ".+"),
            "poll_interval": s.get("poll_interval"),
            # Per-source soft-enable. Missing key = enabled (legacy config).
            "enabled":       s.get("enabled", True) is not False,
        })

    return {
        "notify":         cfg.get("notify", True) is not False,
        "level":          (cfg.get("level") or "error"),
        "retention_days": int(_pick("retention_days", 7)),
        "max_rows":       int(_pick("max_rows", 200_000)),
        "max_size_mb":    int(_pick("max_size_mb", 40)),
        "sources":        sources,
    }


@router.get("/logs")
async def get_logs_settings() -> dict:
    """Return editable logs-pipeline settings."""
    ctx = _ctx()
    return _serialize_logs(dict(ctx.config.logs or {}))


@router.patch("/logs")
async def patch_logs_settings(payload: dict) -> dict:
    """Merge a partial update into the logs block. Accepts any subset of:
      notify, level, retention_days, max_rows, max_size_mb.

    `notify=false` silences Telegram (and any other notifier) for log
    events without dropping the data — captured lines still land in the
    log_events table and surface in /logs.

    `level` persists immediately but the live regex inside LogProcessor
    is built at daemon startup; the running pipeline keeps its current
    pattern until restart. Storage knobs (retention/max_rows/max_size_mb)
    ARE picked up immediately — log_store is reconfigured in-place so
    the next prune tick (~hourly) uses the new bounds.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be an object")

    ctx = _ctx()
    cfg = dict(ctx.config.logs or {})

    if "notify" in payload:
        cfg["notify"] = bool(payload["notify"])

    if "level" in payload:
        v = payload["level"]
        if not isinstance(v, str) or v.lower() not in _LOG_LEVELS:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"level must be one of: {sorted(_LOG_LEVELS)}",
            )
        cfg["level"] = v.lower()

    for key, lo, hi in (
        ("retention_days", 1, 365),
        ("max_rows",       100, 10_000_000),
        ("max_size_mb",    1, 10_000),
    ):
        if key in payload:
            v = payload[key]
            if not isinstance(v, int) or isinstance(v, bool):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST, f"{key} must be an integer",
                )
            if v < lo or v > hi:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"{key} out of range ({lo}–{hi})",
                )
            cfg[key] = v

    ctx.config.logs = cfg

    # Rehydrate the live store so the next prune tick honours the new
    # retention without a daemon restart.
    store = broker.state.data.get("log_store")
    if store is not None:
        store.retention_days = max(1, int(cfg.get("retention_days", store.retention_days)))
        store.max_rows = max(10, int(cfg.get("max_rows", store.max_rows)))
        store.max_size_mb = max(1, int(cfg.get("max_size_mb", store.max_size_mb)))

    await _persist_settings(ctx, sections=("logs",))

    return _serialize_logs(cfg)


@router.patch("/logs/sources/{name}")
async def patch_log_source(name: str, payload: dict) -> dict:
    """Enable or disable a single log source by name.

    Body: `{"enabled": bool}`.

    Hot-toggle: when going enabled→disabled the running LogProcessor's
    consumer for this source is cancelled (`remove_source`); going
    disabled→enabled instantiates a fresh consumer (`add_source`). The
    config dict stays in `logs.sources` either way — the `enabled` flag
    is the on/off, so re-enabling preserves the original pattern, target,
    and the dedup signature cache (which lives by sig, not by source).
    """
    if not isinstance(payload, dict) or "enabled" not in payload:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "payload must include 'enabled': bool",
        )
    desired = bool(payload["enabled"])

    ctx = _ctx()
    cfg = dict(ctx.config.logs or {})
    sources = list(cfg.get("sources") or [])

    idx = next((i for i, s in enumerate(sources)
                if isinstance(s, dict) and s.get("name") == name), -1)
    if idx < 0:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"log source {name!r} not found",
        )

    src = dict(sources[idx])
    was_enabled = src.get("enabled", True) is not False
    if was_enabled == desired:
        # No-op — return current state without touching the processor.
        ctx.config.logs = cfg
        return _serialize_logs(cfg)

    src["enabled"] = desired
    sources[idx] = src
    cfg["sources"] = sources
    ctx.config.logs = cfg

    # Hot-toggle in the running processor. If the processor isn't up
    # (logs disabled at boot), the config change still persists and
    # takes effect on next start.
    processor = broker.state.data.get("log_processor")
    if processor is not None:
        if desired:
            try:
                from core.logs import build_source_from_config
                built = build_source_from_config(src)
                if built is not None:
                    processor.add_source(built)
            except Exception:
                log.exception("settings.logs: failed to hot-add %s", name)
        else:
            try:
                processor.remove_source(name)
            except Exception:
                log.exception("settings.logs: failed to hot-remove %s", name)

    await _persist_settings(ctx, sections=("logs",))
    return _serialize_logs(cfg)


# ── checks section ────────────────────────────────────────────────────


# Per-check-type whitelist of editable option fields. Anything outside this
# set is rejected on PATCH so the UI can't silently inject arbitrary keys
# (some check handlers raise on unknown options on the next run).
_CHECK_EDITABLE: dict[str, set[str]] = {
    "cpu":     {"warn", "crit"},
    "mem":     {"warn", "crit"},
    "disk":    {"warn", "crit", "paths"},
    "net":     {"interfaces"},
    "swap":    {"warn", "crit"},
    "load":    {"warn", "crit"},
    "systemd": {"unit"},
    "http":    {"url", "expect_status", "timeout"},
}


def _serialize_check(c) -> dict:
    """One CheckConfig → JSON-friendly dict for the UI list."""
    return {
        "type":          c.type,
        "name":          c.name,
        "interval":      float(c.interval),
        "enabled":       bool(getattr(c, "enabled", True)),
        "options":       dict(c.options or {}),
        # Hint the UI which fields it's allowed to surface as inputs for
        # this type. Other keys (read-only) display as a small key=value
        # row without an editor.
        "editable":      sorted(_CHECK_EDITABLE.get(c.type, set())),
    }


@router.get("/checks")
async def get_checks_settings() -> dict:
    """List of configured checks. UI renders one row per check."""
    ctx = _ctx()
    return {"checks": [_serialize_check(c) for c in (ctx.config.checks or [])]}


@router.patch("/checks/{name}")
async def patch_check(name: str, payload: dict) -> dict:
    """Merge a partial update into one check by name.

    Accepts:
      enabled: bool
      interval: float (seconds, >= 5)
      options: dict — keys must be in the type's editable set.

    Note: changes to `interval` persist immediately but are picked up by
    the scheduler only on next daemon restart (the periodic loop captures
    interval at startup). `enabled` and `options` toggle live — the next
    scheduled tick uses the new values.
    """
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be an object")

    ctx = _ctx()
    checks = list(ctx.config.checks or [])
    idx = next((i for i, c in enumerate(checks) if c.name == name), -1)
    if idx < 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"check {name!r} not found")
    target = checks[idx]

    if "enabled" in payload:
        target.enabled = bool(payload["enabled"])

    if "interval" in payload:
        v = payload["interval"]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "interval must be a number",
            )
        if v < 5 or v > 86400:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "interval out of range (5–86400 seconds)",
            )
        target.interval = float(v)

    if "options" in payload:
        opts = payload["options"]
        if not isinstance(opts, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "options must be an object",
            )
        editable = _CHECK_EDITABLE.get(target.type, set())
        unknown = set(opts.keys()) - editable
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"options keys not editable for {target.type}: {sorted(unknown)}",
            )
        merged = dict(target.options or {})
        merged.update(opts)
        target.options = merged

    ctx.config.checks = checks
    # Rebuild the live handler so option / threshold changes are picked up
    # on the next tick. The scheduler reaches into ctx.checks_by_name by
    # name, so swapping the value is enough.
    try:
        from core.checks import build_check
        ctx.checks_by_name[target.name] = build_check(target)
    except Exception:
        log.exception("settings.checks: failed to rebuild handler for %s", name)

    await _persist_settings(ctx, sections=("checks",))
    return _serialize_check(target)


# ── notifiers section ─────────────────────────────────────────────────


# Field-level masking. Keys present here are returned as "***" in GET; on
# PATCH a value of "***" is treated as "keep existing" so the UI can submit
# the masked-back response wholesale without leaking secrets back through.
_NOTIFIER_SECRETS: set[str] = {
    "bot_token", "token", "password", "webhook_url", "api_key", "secret",
}
_MASK_VALUE = "***"

# Editable options per notifier type. Type itself is immutable here — to
# replace a notifier with a different type the operator removes + adds
# (currently config-only).
_NOTIFIER_EDITABLE: dict[str, set[str]] = {
    "telegram": {"bot_token", "chat_id", "proxy", "lang", "rate_limit"},
    "slack":    {"webhook_url", "channel"},
}


def _mask_options(type_: str, opts: dict) -> dict:
    """Return a copy of opts with secret values replaced by `_MASK_VALUE`.
    Only masks when the value is set — empty/None passes through untouched
    so the UI can show 'not configured'."""
    out: dict = {}
    for k, v in (opts or {}).items():
        if k in _NOTIFIER_SECRETS and v:
            out[k] = _MASK_VALUE
        elif isinstance(v, dict):
            # Nested (e.g. proxy: {host, port, user, password}) — recurse
            # so password inside proxy gets masked too.
            out[k] = _mask_options(type_, v)
        else:
            out[k] = v
    return out


def _serialize_notifier(n) -> dict:
    return {
        "type":     n.type,
        "enabled":  bool(getattr(n, "enabled", True)),
        "options":  _mask_options(n.type, n.options or {}),
        "editable": sorted(_NOTIFIER_EDITABLE.get(n.type, set())),
    }


def _merge_keep_secrets(existing: dict, incoming: dict) -> dict:
    """Apply incoming dict to existing, treating `_MASK_VALUE` as
    keep-existing for masked fields. Recurses into nested dicts."""
    out = dict(existing or {})
    for k, v in (incoming or {}).items():
        if v == _MASK_VALUE:
            continue  # caller didn't change this secret
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge_keep_secrets(out[k], v)
        else:
            out[k] = v
    return out


@router.get("/notifiers")
async def get_notifiers_settings() -> dict:
    """List of configured notifiers with secrets masked."""
    ctx = _ctx()
    return {"notifiers": [_serialize_notifier(n) for n in (ctx.config.notifiers or [])]}


@router.patch("/notifiers/{type_}")
async def patch_notifier(type_: str, payload: dict) -> dict:
    """Merge a partial update into one notifier by type.

    Accepts:
      enabled: bool
      options: dict — keys must be in the type's editable set; values
                      equal to "***" are kept from the existing config
                      (so the UI can submit the masked GET response).
    """
    if not isinstance(payload, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "payload must be an object")

    ctx = _ctx()
    notifiers = list(ctx.config.notifiers or [])
    idx = next((i for i, n in enumerate(notifiers) if n.type == type_), -1)
    if idx < 0:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"notifier {type_!r} not found")
    target = notifiers[idx]

    if "enabled" in payload:
        target.enabled = bool(payload["enabled"])

    if "options" in payload:
        opts = payload["options"]
        if not isinstance(opts, dict):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "options must be an object",
            )
        editable = _NOTIFIER_EDITABLE.get(type_, set())
        unknown = set(opts.keys()) - editable
        if unknown:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"options keys not editable for {type_}: {sorted(unknown)}",
            )
        target.options = _merge_keep_secrets(target.options or {}, opts)

    ctx.config.notifiers = notifiers
    # Rebuild the live notifier instance so the next .send() uses fresh
    # creds / settings without a daemon restart.
    try:
        from core.notifiers import build_notifier
        new_n = build_notifier(target)
        ctx.notifiers_by_type[target.type] = new_n
        # Refresh the parallel list view too.
        ctx.notifiers = list(ctx.notifiers_by_type.values())
    except Exception:
        log.exception("settings.notifiers: failed to rebuild %s", type_)

    await _persist_settings(ctx, sections=("notifiers",))
    return _serialize_notifier(target)
