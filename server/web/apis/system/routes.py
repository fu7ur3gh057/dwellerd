"""Live VPS snapshot — psutil/shutil reads, no DB. Frontend polls this for
the dashboard header. Deliberately fast: cpu_percent uses interval=None so
it returns a non-blocking sample."""

import logging
import os
import shutil
import socket
import time

import httpx
import psutil
from fastapi import APIRouter, Query

from services.taskiq.broker import broker
from web.apis.system.schemas import DiskUsage, SystemSnapshot

log = logging.getLogger(__name__)

router = APIRouter(tags=["system"])

_GB = 1024 ** 3

# Geo-lookup cache. ip-api.com gives 45 req/min on the free tier without
# auth — way more than we need (the dashboard fetches once per session).
# Caching for an hour means a daemon restart re-resolves; the IP rarely
# changes inside a single uptime.
_LOCATION_CACHE_TTL = 3600.0


def _uptime_seconds() -> int:
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except OSError:
        return int(time.time() - psutil.boot_time())


@router.get("", response_model=SystemSnapshot)
async def snapshot(
    paths: list[str] = Query(default=["/"], description="disk mount points to inspect"),
) -> SystemSnapshot:
    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)

    disks: list[DiskUsage] = []
    for p in paths:
        try:
            u = shutil.disk_usage(p)
        except OSError:
            continue
        disks.append(DiskUsage(
            path=p,
            total_gb=round(u.total / _GB, 2),
            used_gb=round(u.used / _GB, 2),
            free_gb=round(u.free / _GB, 2),
            pct=round(u.used / u.total * 100, 1),
        ))

    return SystemSnapshot(
        cpu_pct=psutil.cpu_percent(interval=None),
        memory_pct=vm.percent,
        memory_used_gb=round(vm.used / _GB, 2),
        memory_total_gb=round(vm.total / _GB, 2),
        swap_pct=swap.percent,
        load_1m=load[0],
        load_5m=load[1],
        load_15m=load[2],
        uptime_seconds=_uptime_seconds(),
        disks=disks,
    )


# ── server geo location ──────────────────────────────────────────────


async def _detect_egress_ip() -> str | None:
    """Best-effort egress IP detection. Tries ipify first (matches the
    boot-time detection in main._detect_public_ip), then a UDP-trick
    local IP fallback. Returns None if both fail (offline host)."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("https://api.ipify.org")
        if r.status_code == 200 and r.text.strip():
            return r.text.strip()
    except Exception:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


async def _resolve_location(ip: str) -> dict | None:
    """Lookup geo via ip-api.com (free tier, no auth, HTTP only). Returns
    a dict with the same shape the front-end already expects, or None on
    any failure — caller decides how to surface that."""
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            # http (not https) — ip-api free tier doesn't do TLS.
            r = await client.get(
                f"http://ip-api.com/json/{ip}",
                params={"fields": "status,country,countryCode,city,lat,lon,query"},
            )
    except Exception:
        return None
    if r.status_code != 200:
        return None
    try:
        data = r.json()
    except Exception:
        return None
    if data.get("status") != "success":
        return None
    return {
        "ip":      data.get("query") or ip,
        "city":    data.get("city") or "",
        "country": data.get("country") or "",
        "iso":     data.get("countryCode") or "",
        "lat":     float(data.get("lat") or 0.0),
        "lng":     float(data.get("lon") or 0.0),
    }


@router.get("/location")
async def location() -> dict:
    """Geo info for the host the daemon is running on.

    Cached per-process for an hour — the IP doesn't change inside a single
    uptime under normal conditions, and ip-api's free tier doesn't love
    being hammered. Returns `{ok, ip, city, country, iso, lat, lng,
    cached_at}` on success; `{ok: false, error: …}` if egress IP detection
    or the geo lookup failed (the front-end falls back to a placeholder).
    """
    cache = broker.state.data.get("location_cache")
    now = time.time()
    if cache and (now - cache.get("cached_at", 0)) < _LOCATION_CACHE_TTL:
        return cache

    ip = await _detect_egress_ip()
    if ip is None:
        out = {"ok": False, "error": "could not detect egress IP", "cached_at": now}
        # Don't cache failures — let the next request retry.
        return out

    geo = await _resolve_location(ip)
    if geo is None:
        out = {
            "ok": False,
            "error": "geo lookup failed",
            "ip": ip,
            "cached_at": now,
        }
        return out

    out = {"ok": True, "cached_at": now, **geo}
    broker.state.data["location_cache"] = out
    return out
