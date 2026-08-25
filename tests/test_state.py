"""The transition rules are what keep Telegram quiet — they get the most
tests of anything in the project."""
from dwellerd.state import decide_transition


def test_first_sighting_ok_is_silent():
    assert decide_transition(None, "ok") is None


def test_first_sighting_bad_alerts():
    assert decide_transition(None, "warn") == "warn"
    assert decide_transition(None, "crit") == "crit"


def test_steady_state_is_silent():
    for level in ("ok", "warn", "crit"):
        assert decide_transition(level, level) is None


def test_escalation_alerts():
    assert decide_transition("ok", "warn") == "warn"
    assert decide_transition("ok", "crit") == "crit"
    assert decide_transition("warn", "crit") == "crit"


def test_recovery_alerts():
    assert decide_transition("warn", "ok") == "ok"
    assert decide_transition("crit", "ok") == "ok"


def test_partial_recovery_is_silent():
    # crit -> warn is still bad; we wait for a real recovery rather than
    # sending "it got slightly better".
    assert decide_transition("crit", "warn") is None


def test_flapping_costs_one_message_per_edge():
    levels = ["ok", "crit", "crit", "crit", "ok"]
    fired = []
    previous = None
    for level in levels:
        result = decide_transition(previous, level)
        if result:
            fired.append(result)
        previous = level
    assert fired == ["crit", "ok"]
