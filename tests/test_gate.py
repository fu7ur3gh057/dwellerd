"""The alert gate decides when a reading becomes news. Every rule here
exists to stop a specific kind of 3am noise."""
from dwellerd.gate import (
    GateState, advance, effective_level, ladder_level, reminder_due,
)
from dwellerd.state import decide_transition


def feed(levels, confirm, start=None):
    """Run a sequence of observations through the gate; return the alerts
    (as decide_transition would fire them) in order."""
    state = start or GateState()
    fired = []
    for level in levels:
        state, edge = advance(state, level, confirm)
        if edge:
            fire = decide_transition(*edge)
            if fire:
                fired.append(fire)
    return fired, state


# ── confirmation ─────────────────────────────────────────────────────────


def test_confirm_one_is_the_old_behaviour():
    fired, _ = feed(["ok", "warn", "crit", "crit", "ok"], confirm=1)
    assert fired == ["warn", "crit", "ok"]


def test_a_single_spike_is_ignored():
    fired, state = feed(["ok", "crit", "ok", "ok"], confirm=2)
    assert fired == []
    assert state.confirmed == "ok" and state.pending is None


def test_a_sustained_problem_alerts_once_confirmed():
    fired, _ = feed(["ok", "crit", "crit"], confirm=2)
    assert fired == ["crit"]


def test_steady_state_after_confirmation_stays_silent():
    fired, _ = feed(["crit"] * 10, confirm=2)
    assert fired == ["crit"]


def test_recovery_needs_confirmation_too():
    fired, _ = feed(["crit", "crit", "ok", "crit", "crit", "ok", "ok"], confirm=2)
    # One good reading in a bad patch is not a recovery.
    assert fired == ["crit", "ok"]


def test_first_sighting_bad_needs_confirmation():
    fired, state = feed(["warn"], confirm=2)
    assert fired == [] and state.confirmed is None and state.pending == "warn"


def test_first_sighting_ok_confirms_silently():
    fired, state = feed(["ok"], confirm=3)
    assert fired == [] and state.confirmed == "ok"


def test_flapping_between_warn_and_crit_confirms_the_milder_level():
    # Both readings are bad, so it is an incident — but only `warn` has
    # actually been seen twice in a row at *at least* that severity.
    fired, state = feed(["ok", "warn", "crit", "warn"], confirm=3)
    assert fired == ["warn"]
    assert state.confirmed == "warn"


def test_escalation_from_warn_to_crit_is_confirmed_separately():
    fired, _ = feed(["warn", "warn", "crit", "crit"], confirm=2)
    assert fired == ["warn", "crit"]


def test_partial_recovery_commits_to_the_worst_level_seen():
    # crit -> (warn, ok) must not claim a full recovery.
    fired, state = feed(["crit", "crit", "warn", "ok"], confirm=2)
    assert fired == ["crit"]
    assert state.confirmed == "warn"      # silently stepped down
    fired2, _ = feed(["ok", "ok"], confirm=2, start=state)
    assert fired2 == ["ok"]


def test_a_streak_in_the_other_direction_starts_over():
    state = GateState(confirmed="warn")
    state, edge = advance(state, "crit", 3)       # up, 1
    assert edge is None and state.pending == "crit"
    state, edge = advance(state, "ok", 3)         # down: new streak, 1
    assert edge is None and state.pending == "ok" and state.pending_count == 1


def test_returning_to_the_confirmed_level_clears_the_streak():
    state = GateState(confirmed="ok")
    state, _ = advance(state, "crit", 3)
    state, edge = advance(state, "ok", 3)
    assert edge is None and state.pending is None and state.pending_count == 0


def test_advance_keeps_the_reminder_and_mute_bookkeeping():
    state = GateState(confirmed="crit", silenced=True, last_alert=123.0)
    for level in ("crit", "warn", "crit"):
        state, _ = advance(state, level, 3)
    assert state.silenced and state.last_alert == 123.0


def test_unknown_level_is_treated_as_worst_case_not_a_crash():
    fired, _ = feed(["bogus"], confirm=1)
    assert fired == ["bogus"]


# ── hysteresis ───────────────────────────────────────────────────────────


def test_ladder_level():
    assert ladder_level(95, 80, 90) == "crit"
    assert ladder_level(85, 80, 90) == "warn"
    assert ladder_level(10, 80, 90) == "ok"


def test_hysteresis_holds_a_level_just_under_the_threshold():
    # warn 80, 5% -> recover below 76. 78 is under 80 but not under 76.
    assert effective_level("ok", (78, 80, 90), "warn", 5) == "warn"
    assert effective_level("ok", (75, 80, 90), "warn", 5) == "ok"


def test_hysteresis_steps_crit_down_to_warn_not_to_ok():
    # crit 90 -> clears at 85.5; warn 80 -> clears at 76.
    assert effective_level("warn", (88, 80, 90), "crit", 5) == "crit"
    assert effective_level("warn", (83, 80, 90), "crit", 5) == "warn"
    assert effective_level("ok", (78, 80, 90), "crit", 5) == "warn"


def test_hysteresis_never_makes_things_worse():
    assert effective_level("ok", (10, 80, 90), "ok", 5) == "ok"
    assert effective_level("warn", (85, 80, 90), "ok", 5) == "warn"
    assert effective_level("crit", (95, 80, 90), "warn", 5) == "crit"


def test_hysteresis_is_inert_without_a_ladder_or_when_zero():
    assert effective_level("ok", None, "crit", 5) == "ok"
    assert effective_level("ok", (78, 80, 90), "warn", 0) == "ok"


def test_hysteresis_never_holds_above_what_was_confirmed():
    # Confirmed warn; value inside the crit hold band must not read crit.
    assert effective_level("ok", (88, 80, 90), "warn", 5) == "warn"


def test_hysteresis_is_capped_and_a_new_check_has_nothing_to_hold():
    assert effective_level("ok", (78, 80, 90), None, 5) == "ok"
    # An absurd value is clamped to 50%, not allowed to go negative.
    assert effective_level("ok", (10, 80, 90), "warn", 500) == "ok"


def test_a_value_hovering_on_the_line_does_not_flap():
    state = GateState()
    fired = []
    for value in [79, 81, 79.5, 80.5, 79, 81, 79.9, 80.1]:
        raw = ladder_level(value, 80, 90)
        observed = effective_level(raw, (value, 80, 90), state.confirmed, 5)
        state, edge = advance(state, observed, 1)
        if edge and (fire := decide_transition(*edge)):
            fired.append(fire)
    assert fired == ["warn"]          # one alert, no recoveries


# ── reminders ────────────────────────────────────────────────────────────


def test_reminder_only_for_a_steady_crit_that_is_overdue():
    s = GateState(confirmed="crit", last_alert=1000.0)
    assert reminder_due(s, "crit", 3600, 1000.0 + 3600)
    assert not reminder_due(s, "crit", 3600, 1000.0 + 3599)


def test_no_reminder_when_off_never_announced_or_not_crit():
    s = GateState(confirmed="crit", last_alert=1000.0)
    assert not reminder_due(s, "crit", 0, 10**9)
    assert not reminder_due(GateState(confirmed="crit"), "crit", 60, 10**9)
    assert not reminder_due(GateState(confirmed="warn", last_alert=1.0), "warn", 60, 10**9)


def test_no_reminder_while_recovery_is_pending():
    s = GateState(confirmed="crit", last_alert=1000.0)
    assert not reminder_due(s, "ok", 60, 10**9)


def test_an_acknowledged_crit_is_not_reminded_about():
    s = GateState(confirmed="crit", last_alert=1000.0, acked=True)
    assert not reminder_due(s, "crit", 60, 10**9)
    assert reminder_due(GateState(confirmed="crit", last_alert=1000.0), "crit", 60, 10**9)


def test_acked_survives_every_kind_of_step():
    state = GateState(confirmed="crit", acked=True)
    for level in ("crit", "warn", "warn", "crit", "ok"):
        state, _ = advance(state, level, 2)
        assert state.acked
