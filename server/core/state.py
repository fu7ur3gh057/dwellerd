"""Severity-transition rules.

`decide_transition` is the pure function — pass last + current level, get
back the level to fire on (or None for "stay quiet"). `StateTracker` is a
thin in-memory wrapper kept for non-DB callers and unit tests.
"""

_SEVERITY = {"ok": 0, "warn": 1, "crit": 2}


def decide_transition(prev: str | None, current: str) -> str | None:
    """Return the level we should alert on, or None to stay silent.

    Rules:
      - First time we see a check, fire only if non-ok.
      - Steady state at any level → silent.
      - Any → ok → fire 'ok' (recovery).
      - Lower → higher (e.g. warn → crit) → fire current.
      - Higher → lower-but-still-bad (crit → warn) → silent; we wait for
        full recovery.
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


class StateTracker:
    """In-memory severity tracker; for unit tests and callers that don't
    want a DB round-trip per observation."""

    def __init__(self) -> None:
        self._levels: dict[str, str] = {}

    def observe(self, name: str, level: str) -> str | None:
        prev = self._levels.get(name)
        self._levels[name] = level
        return decide_transition(prev, level)
