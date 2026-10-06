"""The alert gate — everything between "a check returned a level" and "the
operator gets a message", as pure functions.

Three rules live here, on top of `decide_transition` in `state.py`:

  Hysteresis   A level that is already confirmed does not drop just because
               the value dipped under the threshold for one tick. It has to
               fall `hysteresis`% *below* the threshold. This is what stops
               a check hovering at 79.9/80.1 from flapping all night.

  Confirmation A *change* of level is only accepted after `confirm`
               consecutive observations that agree on a direction. One CPU
               spike or one HTTP timeout is not an incident.

  Streaks      "Agree" means: every observation in the streak is on the same
               side of the confirmed level. Escalating, the streak commits to
               the *least* severe level it saw; recovering, to the *most*
               severe. So warn/crit/warn/crit confirms `warn`, and a
               crit -> warn -> ok wobble never claims a full recovery early.

With `confirm=1` and `hysteresis=0` this degrades to exactly the old
behaviour: every change is accepted on the first observation.
"""
from __future__ import annotations

from dataclasses import dataclass

SEVERITY = {"ok": 0, "warn": 1, "crit": 2}


@dataclass(frozen=True)
class GateState:
    """What is remembered about one check between ticks."""
    confirmed: str | None = None    # the accepted level; None = never seen
    pending: str | None = None      # level the current streak would commit to
    pending_count: int = 0          # observations in the current streak
    silenced: bool = False          # a non-ok alert was muted and never delivered
    last_alert: float | None = None  # when a crit message was last delivered
    acked: bool = False             # the operator said "I know" — no reminders


def _sev(level: str) -> int:
    # An unknown level from a future/odd check is treated as the worst
    # case rather than crashing the tick.
    return SEVERITY.get(level, SEVERITY["crit"])


def ladder_level(value: float, warn: float, crit: float) -> str:
    if value >= crit:
        return "crit"
    if value >= warn:
        return "warn"
    return "ok"


def effective_level(
    raw: str,
    ladder: tuple[float, float, float] | None,
    confirmed: str | None,
    hysteresis: float,
) -> str:
    """Apply hysteresis to a raw level.

    Only ever *holds* a level up; it never invents a worse one. `ladder` is
    (value, warn, crit) for percentage-style checks and None for the rest
    (http / systemd / docker have no threshold to be "near").
    """
    base = confirmed or "ok"
    if ladder is None or hysteresis <= 0 or _sev(raw) >= _sev(base):
        return raw
    value, warn, crit = ladder
    k = 1.0 - min(max(hysteresis, 0.0), 50.0) / 100.0
    held = ladder_level(value, warn * k, crit * k)
    # Thresholds only moved down, so `held` >= `raw`. Never hold above what
    # was already confirmed.
    return held if _sev(held) <= _sev(base) else base


def advance(
    state: GateState, observed: str, confirm: int,
) -> tuple[GateState, tuple[str, str] | None]:
    """Feed one observation in. Returns the new state and, if the confirmed
    level just changed, the (previous, new) edge to run through
    `decide_transition`."""
    confirm = max(1, int(confirm))
    base = state.confirmed or "ok"

    if observed == base:
        # Back on the confirmed level: any streak is broken. A brand-new
        # check that starts out fine becomes confirmed-ok silently.
        return (
            GateState(
                confirmed=base, pending=None, pending_count=0,
                silenced=state.silenced, last_alert=state.last_alert,
                acked=state.acked,
            ),
            None,
        )

    direction = 1 if _sev(observed) > _sev(base) else -1
    pending, count = state.pending, state.pending_count
    same_run = (
        pending is not None
        and pending != base
        and (1 if _sev(pending) > _sev(base) else -1) == direction
    )
    if same_run:
        count += 1
        pending = (
            min(pending, observed, key=_sev) if direction > 0
            else max(pending, observed, key=_sev)
        )
    else:
        pending, count = observed, 1

    if count >= confirm:
        new = GateState(
            confirmed=pending, pending=None, pending_count=0,
            silenced=state.silenced, last_alert=state.last_alert,
            acked=state.acked,
        )
        return new, (base, pending)

    return (
        GateState(
            confirmed=state.confirmed, pending=pending, pending_count=count,
            silenced=state.silenced, last_alert=state.last_alert,
            acked=state.acked,
        ),
        None,
    )


def reminder_due(
    state: GateState, observed: str, remind_after: float, now: float,
) -> bool:
    """A crit that is still confirmed, still observed, not acknowledged, and
    was last announced long enough ago."""
    return (
        remind_after > 0
        and not state.acked
        and state.confirmed == "crit"
        and observed == "crit"
        and state.last_alert is not None
        and now - state.last_alert >= remind_after
    )
