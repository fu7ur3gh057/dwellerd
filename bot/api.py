"""HTTP client for the daemon's REST API.

The bot calls the daemon's `/dwellerd/api/...` endpoints to dispatch
privileged actions (`/run`, `/restart`, etc.) instead of duplicating the
TaskIQ + docker-subprocess logic. This way:
  - Audit trail in the daemon stays correct (logs show user X did action)
  - Bot doesn't need its own broker / docker socket access
  - When the daemon's behaviour changes, bot sees it for free

Auth: each TG user's login also performs a REST login and stashes the
access + refresh tokens in `bot_sessions`. This client uses those tokens.
On 401 it tries the refresh token once and retries.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import httpx
import yaml

from .db import db_session
from db.models import BotSession  # type: ignore


log = logging.getLogger(__name__)


_PROD_CONFIG = Path("/etc/dwellerd/config.yaml")
_DEV_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"


# ── base url discovery ─────────────────────────────────────────────────────


def _resolve_base_url() -> str | None:
    """Build `http://host:port/prefix` from config.yaml. Returns None when
    web is disabled — caller must short-circuit with a 'web not enabled'
    message instead of hitting a dead URL."""
    for p in (_PROD_CONFIG, _DEV_CONFIG):
        if p.exists():
            try:
                cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                continue
            web = cfg.get("web") or {}
            if not web.get("enabled"):
                return None
            host = web.get("host") or "127.0.0.1"
            # 0.0.0.0 binds everywhere but only loopback is reachable from
            # the same machine without going through the firewall.
            if host == "0.0.0.0":
                host = "127.0.0.1"
            port = int(web.get("port", 8765))
            prefix = web.get("prefix", "/dwellerd")
            return f"http://{host}:{port}{prefix}"
    return None


class WebDisabled(RuntimeError):
    """Raised when the daemon's web interface isn't enabled in config."""


class ApiError(RuntimeError):
    """Raised on non-2xx responses (status, body)."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"{status}: {body}")
        self.status = status
        self.body = body


# ── REST login (called from bot.auth.verify_credentials) ──────────────────


async def rest_login(username: str, password: str) -> dict | None:
    """Call POST /api/auth/login and return {access_token, refresh_token,
    expires_in, role, username} on success. None when web is disabled or
    creds are invalid (we already verified locally — REST refusal would
    indicate a config drift)."""
    base = _resolve_base_url()
    if base is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                f"{base}/api/auth/login",
                json={"username": username, "password": password},
            )
            if r.status_code != 200:
                log.warning("rest login failed: %s %s", r.status_code, r.text[:200])
                return None
            data = r.json()
            # Pick the refresh cookie out of the jar — server sets it httpOnly
            # but httpx still exposes it via cookies dict.
            data["refresh_token"] = client.cookies.get("dw_refresh") or ""
            return data
    except httpx.HTTPError as e:
        log.warning("rest login transport error: %s", e)
        return None


# ── per-user client (uses tokens cached in bot_sessions) ──────────────────


def _load_tokens(tg_user_id: int) -> tuple[str, str, float] | None:
    with db_session() as s:
        sess = s.get(BotSession, tg_user_id)
        if sess is None or not sess.access_token:
            return None
        return (
            sess.access_token,
            sess.refresh_token or "",
            sess.access_expires_at or 0.0,
        )


def _save_tokens(
    tg_user_id: int, *, access: str, refresh: str, expires_at: float,
) -> None:
    with db_session() as s:
        sess = s.get(BotSession, tg_user_id)
        if sess is None:
            return
        sess.access_token = access
        sess.refresh_token = refresh
        sess.access_expires_at = expires_at
        s.add(sess)
        s.commit()


async def _refresh(tg_user_id: int, base: str, refresh_token: str) -> str | None:
    if not refresh_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            client.cookies.set("dw_refresh", refresh_token, domain=base.split("/")[2].split(":")[0])
            r = await client.post(f"{base}/api/auth/refresh")
            if r.status_code != 200:
                return None
            data = r.json()
            access = data.get("access_token")
            new_refresh = client.cookies.get("dw_refresh") or refresh_token
            expires_at = time.time() + int(data.get("expires_in", 1800))
            if access:
                _save_tokens(tg_user_id, access=access, refresh=new_refresh, expires_at=expires_at)
                return access
    except httpx.HTTPError:
        return None
    return None


async def _request(
    tg_user_id: int, method: str, path: str, **kwargs,
) -> dict | list:
    """Make an authed request. Refreshes once on 401."""
    base = _resolve_base_url()
    if base is None:
        raise WebDisabled("web is not enabled in config.yaml")

    tokens = _load_tokens(tg_user_id)
    if tokens is None:
        raise ApiError(401, "no access token — re-login: /login")
    access, refresh, expires_at = tokens

    async def _try(token: str) -> httpx.Response:
        async with httpx.AsyncClient(timeout=15) as client:
            return await client.request(
                method, f"{base}{path}",
                headers={"Authorization": f"Bearer {token}"},
                **kwargs,
            )

    r = await _try(access)
    if r.status_code == 401:
        new_access = await _refresh(tg_user_id, base, refresh)
        if not new_access:
            raise ApiError(401, "session expired — re-login: /login")
        r = await _try(new_access)

    if not r.is_success:
        raise ApiError(r.status_code, r.text[:300])
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return {"raw": r.text}


# ── action methods (one per command) ──────────────────────────────────────


async def run_check(tg_user_id: int, name: str) -> dict:
    return await _request(tg_user_id, "POST", f"/api/checks/{name}/run")


async def docker_action(tg_user_id: int, project: str, action: str) -> dict:
    return await _request(tg_user_id, "POST", f"/api/docker/{project}/{action}")


async def docker_service_action(
    tg_user_id: int, project: str, service: str, action: str,
) -> dict:
    return await _request(
        tg_user_id, "POST", f"/api/docker/{project}/{service}/{action}",
    )


async def notifier_test(tg_user_id: int, type_: str = "telegram") -> dict:
    return await _request(tg_user_id, "POST", f"/api/notifiers/{type_}/test")


async def patch_check(tg_user_id: int, name: str, body: dict) -> dict:
    return await _request(
        tg_user_id, "PATCH", f"/api/settings/checks/{name}", json=body,
    )
