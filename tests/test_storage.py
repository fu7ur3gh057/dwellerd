"""Storage is what makes the daemon quiet across restarts — first-seen
detection and retention both live here."""
import time

import pytest

from dwellerd.storage import Storage


@pytest.fixture
def store(tmp_path):
    s = Storage(tmp_path / "test.sqlite")
    s.connect()
    yield s
    s.close()


def test_level_roundtrip(store):
    assert store.get_level("cpu") is None
    store.set_level("cpu", "warn", "CPU 85%")
    assert store.get_level("cpu") == "warn"


def test_since_holds_while_the_level_holds(store):
    store.set_level("cpu", "crit", "first")
    first = store.all_states()[0]["since"]
    time.sleep(0.01)
    store.set_level("cpu", "crit", "second")
    assert store.all_states()[0]["since"] == first


def test_since_moves_when_the_level_changes(store):
    store.set_level("cpu", "crit", "bad")
    first = store.all_states()[0]["since"]
    time.sleep(0.01)
    store.set_level("cpu", "ok", "fine")
    assert store.all_states()[0]["since"] > first


def test_first_log_is_first_only_once(store):
    now = time.time()
    assert store.record_log("app", "boom", "sig1", now) is True
    assert store.record_log("app", "boom", "sig1", now) is False


def test_distinct_signatures_are_each_first(store):
    now = time.time()
    assert store.record_log("app", "a", "sigA", now) is True
    assert store.record_log("app", "b", "sigB", now) is True


def test_first_seen_survives_a_reconnect(store, tmp_path):
    now = time.time()
    store.record_log("app", "boom", "sig1", now)
    store.close()

    reopened = Storage(tmp_path / "test.sqlite")
    reopened.connect()
    try:
        # The whole point: a restart must not re-announce a known error.
        assert reopened.record_log("app", "boom", "sig1", time.time()) is False
    finally:
        reopened.close()


def test_summary_ranks_by_count(store):
    now = time.time()
    for _ in range(3):
        store.record_log("app", "noisy", "sigA", now)
    store.record_log("app", "quiet", "sigB", now)
    summary = store.log_summary_since(now - 10)
    assert [s["count"] for s in summary] == [3, 1]


def test_prune_drops_old_rows(store):
    old = time.time() - 40 * 86400
    store.record_log("app", "ancient", "sigOld", old)
    store.record_log("app", "fresh", "sigNew", time.time())
    by_age, _ = store.prune(retention_days=14, max_rows=1000)
    assert by_age == 1
    assert store.log_count_since(0) == 1


def test_prune_caps_total_rows(store):
    now = time.time()
    for i in range(20):
        store.record_log("app", f"line {i}", f"sig{i}", now)
    _, by_count = store.prune(retention_days=14, max_rows=5)
    assert by_count == 15
    assert store.log_count_since(0) == 5


def test_alerts_are_recorded_and_windowed(store):
    store.record_alert("cpu", "crit", "cpu", "CPU 95%")
    assert len(store.alerts_since(time.time() - 60)) == 1
    assert len(store.alerts_since(time.time() + 60)) == 0
