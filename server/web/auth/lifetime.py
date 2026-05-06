"""Wire JWT secret + token lifetimes onto broker.state.

Called from main._run before the FastAPI server starts. Worker-only mode
skips this so the JWT secret never ends up loaded for a CLI-only deploy.
"""
from __future__ import annotations

import logging
import os

from config import Config
from services.taskiq.broker import broker
from web.auth.secret import resolve_jwt_secret

log = logging.getLogger(__name__)


# 30 minutes — short enough that a stolen access JWT has limited window,
# long enough that a typical session doesn't refresh more than ~2x/hour.
_DEFAULT_ACCESS_TTL = 30 * 60
# 30 days — typical "remember me". Each refresh rotates so a leaked refresh
# becomes useless after the legitimate user does anything.
_DEFAULT_REFRESH_TTL = 30 * 24 * 3600


def _terminal_kill_switch() -> bool:
    val = os.environ.get("DWELLERD_TERMINAL_DISABLED", "").strip().lower()
    return val in ("1", "true", "yes", "on")


def init_auth(config: Config) -> None:
    """Read web.jwt + web.terminal config, persist secret, attach to
    broker.state so handlers + sockets can reach it."""
    web = config.web or {}
    jwt_cfg = web.get("jwt") or {}

    secret = resolve_jwt_secret(config)
    broker.state.web_jwt_secret = secret
    broker.state.web_jwt_access_ttl = int(
        jwt_cfg.get("access_ttl_seconds", _DEFAULT_ACCESS_TTL),
    )
    broker.state.web_jwt_refresh_ttl = int(
        jwt_cfg.get("refresh_ttl_seconds", _DEFAULT_REFRESH_TTL),
    )

    # Cookie hardening flags. Operator opts in via env once a TLS-terminating
    # proxy is in front of the daemon. Without `secure=True`, browsers refuse
    # to send the cookie over plain http on the dev port; without `samesite`
    # cross-site requests would carry the cookie.
    behind_tls = os.environ.get("DWELLERD_BEHIND_TLS", "").lower() in (
        "1", "true", "yes", "on",
    )
    broker.state.web_cookie_secure = behind_tls
    # SameSite=Strict by default — the SPA is same-origin with the API so
    # there's no legitimate cross-site flow to break. Operator can downgrade
    # to "lax" via env if a top-level GET navigation needs the cookie.
    samesite = os.environ.get("DWELLERD_COOKIE_SAMESITE", "strict").strip().lower()
    if samesite not in ("strict", "lax", "none"):
        log.warning("auth: DWELLERD_COOKIE_SAMESITE=%r is not strict/lax/none — defaulting to strict", samesite)
        samesite = "strict"
    if samesite == "none" and not behind_tls:
        log.warning("auth: SameSite=None requires Secure (TLS) — coercing to Strict")
        samesite = "strict"
    broker.state.web_cookie_samesite = samesite

    # Terminal auth is PAM-based against system users — no separate creds
    # in config.yaml. The token_ttl knob still controls how long an unlock
    # token stays valid before auto-relock.
    term_cfg = web.get("terminal") or {}
    killed = _terminal_kill_switch()
    broker.state.terminal_killed = killed
    if killed:
        broker.state.terminal_enabled = False
        log.warning("terminal: force-disabled by DWELLERD_TERMINAL_DISABLED env var")
    else:
        broker.state.terminal_enabled = bool(term_cfg.get("enabled"))
    broker.state.terminal_token_ttl = int(term_cfg.get("token_ttl", 1800))

    log.info(
        "auth: ready (access TTL=%ds, refresh TTL=%ds, cookie samesite=%s, secure=%s)",
        broker.state.data["web_jwt_access_ttl"],
        broker.state.data["web_jwt_refresh_ttl"],
        samesite, behind_tls,
    )
