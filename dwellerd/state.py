"""Severity-transition rules — the thing that keeps Telegram quiet.

A check that sits at `crit` for six hours must produce exactly one message,
not 360. `decide_transition` is the pure function that decides whether an
observation is worth sending; everything else in the daemon defers to it.
"""
from __future__ import annotations

_SEVERITY = {"ok": 0, "warn": 1, "crit": 2}


def decide_transition(prev: str | None, current: str) -> str | None:
    """Return the level to alert on, or None to stay silent.

    Rules:
      - First sighting of a check: fire only if it is already not ok.
      - Steady state at any level: silent.
      - Anything -> ok: fire 'ok' (recovery).
      - Lower -> higher (warn -> crit): fire the new level.
      - Higher -> lower but still bad (crit -> warn): silent — we wait for
        a full recovery rather than announcing a partial one.
    """
    if prev is None:
        return current if current != "ok" else None
    if prev == current:
        return None
    if current == "ok":
        return "ok"
    if _SEVERITY[current] > _SEVERITY[prev]:
        return current
    return None
