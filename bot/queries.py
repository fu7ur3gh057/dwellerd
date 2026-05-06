"""DB query helpers for read-only bot handlers.

Direct sqlmodel/sqlite queries against the same DB the daemon writes —
no HTTP hop, no auth juggling. Each function returns plain dicts/lists
ready for the format helpers.
"""
from __future__ import annotations

import time
from typing import Any

from sqlmodel import desc, select

from db.models import (  # type: ignore
    AlertEvent, CheckResult, CheckStateEntry, LogEvent,
    LogSignatureEntry, Settings,
)

from .db import db_session


def list_checks() -> list[dict[str, Any]]:
    """Return one row per configured check: {name, level, last_ts,
    last_value, detail}. Joins `check_state` with the latest `check_results`
    row per check.
    """
    out: list[dict[str, Any]] = []
    with db_session() as s:
        states = s.exec(select(CheckStateEntry)).all()
        for st in sorted(states, key=lambda x: x.name):
            latest = s.exec(
                select(CheckResult)
                .where(CheckResult.name == st.name)
                .order_by(desc(CheckResult.ts))
                .limit(1)
            ).first()
            out.append({
                "name": st.name,
                "level": st.level,
                "updated_at": st.updated_at,
                "last_ts": latest.ts if latest else 0.0,
                "last_value": (latest.metrics or {}).get("value") if latest else None,
                "detail": latest.detail if latest else None,
            })
    return out


def check_history(name: str, *, limit: int = 10) -> list[CheckResult]:
    """Return up to `limit` most-recent results for one check, oldest-first."""
    with db_session() as s:
        rows = s.exec(
            select(CheckResult)
            .where(CheckResult.name == name)
            .order_by(desc(CheckResult.ts))
            .limit(limit)
        ).all()
        # Detach from session so caller can use after the with-block exits.
        return list(reversed([r.model_copy() for r in rows]))


def recent_alerts(*, limit: int = 10) -> list[AlertEvent]:
    """Newest-first alert events."""
    with db_session() as s:
        rows = s.exec(
            select(AlertEvent)
            .order_by(desc(AlertEvent.ts))
            .limit(limit)
        ).all()
        return [r.model_copy() for r in rows]


def recent_logs(*, limit: int = 10, source: str | None = None) -> list[LogEvent]:
    """Newest-first log events, optionally filtered by source name."""
    with db_session() as s:
        q = select(LogEvent).order_by(desc(LogEvent.ts)).limit(limit)
        if source:
            q = q.where(LogEvent.source == source)
        rows = s.exec(q).all()
        return [r.model_copy() for r in rows]


def top_signatures(*, limit: int = 10) -> list[LogSignatureEntry]:
    """Top-N most-frequent log signatures (dedup view)."""
    with db_session() as s:
        rows = s.exec(
            select(LogSignatureEntry)
            .order_by(desc(LogSignatureEntry.total))
            .limit(limit)
        ).all()
        return [r.model_copy() for r in rows]


def get_settings() -> Settings | None:
    """Return the singleton Settings row (id=1) or None on first run."""
    with db_session() as s:
        return s.get(Settings, 1)


def daemon_uptime() -> tuple[float | None, float | None]:
    """Return (host_boot_ts, daemon_oldest_check_ts) — best effort.

    The daemon itself doesn't write its own start time anywhere, so we
    proxy 'how long has the daemon been running' as the timestamp of the
    oldest CheckResult since the last gap > 5 minutes. Cheap heuristic;
    phase 5 could add a `daemon_starts` table if we care more.
    """
    import psutil
    try:
        boot = psutil.boot_time()
    except Exception:
        boot = None

    with db_session() as s:
        # Most recent result first — walk back until the gap exceeds 5 min.
        rows = s.exec(
            select(CheckResult.ts).order_by(desc(CheckResult.ts)).limit(2000)
        ).all()
    if not rows:
        return boot, None
    prev = rows[0]
    daemon_start = rows[0]
    for ts in rows[1:]:
        if prev - ts > 300:
            break
        daemon_start = ts
        prev = ts
    return boot, daemon_start


def stat_counts() -> dict[str, int]:
    """Quick counts for the /status snapshot footer."""
    now = time.time()
    out = {"alerts_24h": 0, "log_events_24h": 0}
    with db_session() as s:
        out["alerts_24h"] = len(s.exec(
            select(AlertEvent.id).where(AlertEvent.ts > now - 86400)
        ).all())
        out["log_events_24h"] = len(s.exec(
            select(LogEvent.id).where(LogEvent.ts > now - 86400)
        ).all())
    return out
