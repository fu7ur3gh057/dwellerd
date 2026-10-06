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


# ── gate state, mutes, meta ──────────────────────────────────────────────


def test_gate_roundtrip(store):
    assert store.get_gate("cpu") is None
    store.save_gate("cpu", "warn", "CPU 85%", confirmed="ok", pending="warn",
                    pending_count=1, silenced=False, last_alert=None)
    gate = store.get_gate("cpu")
    assert gate["confirmed"] == "ok" and gate["pending"] == "warn"
    assert gate["pending_count"] == 1 and gate["silenced"] is False
    assert gate["last_alert"] is None and gate["since"] > 0


def test_gate_keeps_since_while_the_observed_level_holds(store):
    store.save_gate("cpu", "crit", "a", confirmed="crit", pending=None,
                    pending_count=0, silenced=False, last_alert=1.0)
    first = store.get_gate("cpu")["since"]
    time.sleep(0.01)
    store.save_gate("cpu", "crit", "b", confirmed="crit", pending=None,
                    pending_count=0, silenced=True, last_alert=2.0)
    gate = store.get_gate("cpu")
    assert gate["since"] == first and gate["silenced"] and gate["last_alert"] == 2.0


def test_rows_from_before_the_gate_fall_back_to_the_stored_level(store):
    # `set_level` is the old writer: it leaves the gate columns NULL.
    store.set_level("disk-root", "crit", "full")
    gate = store.get_gate("disk-root")
    assert gate["confirmed"] == "crit"      # not re-announced after an upgrade
    assert gate["pending_count"] == 0 and gate["last_alert"] is None


def test_an_old_database_is_migrated_in_place(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE check_state (
            name TEXT PRIMARY KEY, level TEXT NOT NULL, since REAL NOT NULL,
            last_run REAL NOT NULL, last_detail TEXT NOT NULL DEFAULT '');
        INSERT INTO check_state VALUES ('cpu', 'crit', 100.0, 200.0, 'CPU 95%');
    """)
    conn.commit()
    conn.close()

    s = Storage(path)
    s.connect()
    try:
        gate = s.get_gate("cpu")
        assert gate["confirmed"] == "crit" and gate["since"] == 100.0
        s.save_gate("cpu", "ok", "fine", confirmed="ok", pending=None,
                    pending_count=0, silenced=False, last_alert=None)
        assert s.get_gate("cpu")["confirmed"] == "ok"
    finally:
        s.close()
    # And connecting a second time must not try to add the columns again.
    s2 = Storage(path)
    s2.connect()
    s2.close()


def test_mute_matches_by_name_and_glob(store):
    until = time.time() + 60
    store.add_mute("cpu", until)
    store.add_mute("docker:*", until)
    assert store.is_muted("cpu")
    assert store.is_muted("docker:app") and store.is_muted("docker:db")
    assert not store.is_muted("memory") and not store.is_muted("docker")
    assert not store.is_muted("cpu2")


def test_star_mutes_everything(store):
    store.add_mute("*", time.time() + 60)
    assert store.is_muted("anything") and store.is_muted("log:nginx")


def test_mutes_expire(store):
    store.add_mute("cpu", time.time() + 60)
    assert store.is_muted("cpu", now=time.time() + 30)
    assert not store.is_muted("cpu", now=time.time() + 61)
    assert store.active_mutes(now=time.time() + 61) == []


def test_remuting_extends_instead_of_duplicating(store):
    now = time.time()
    store.add_mute("cpu", now + 60)
    store.add_mute("cpu", now + 3600)
    mutes = store.active_mutes()
    assert len(mutes) == 1 and mutes[0]["until"] == now + 3600


def test_unmute_one_or_all(store):
    until = time.time() + 60
    store.add_mute("cpu", until)
    store.add_mute("memory", until)
    assert store.remove_mute("cpu") == 1
    assert not store.is_muted("cpu") and store.is_muted("memory")
    assert store.remove_mute("nope") == 0
    assert store.remove_mute() == 1 and store.active_mutes() == []


def test_meta_roundtrip(store):
    assert store.get_meta("run_state") is None
    store.set_meta(run_state="running", last_seen=12.5)
    assert store.get_meta("run_state") == "running"
    assert store.get_meta("last_seen") == "12.5"
    store.set_meta(run_state="stopped")
    assert store.get_meta("run_state") == "stopped"


# ── acknowledgements and button targets ──────────────────────────────────


def bad(store, name, level="crit", **kw):
    store.save_gate(name, level, "x", confirmed=kw.pop("confirmed", level),
                    pending=None, pending_count=0, silenced=False,
                    last_alert=None, **kw)


def test_ack_a_live_problem(store):
    bad(store, "disk")
    assert store.set_acked("disk") == "acked"
    assert store.get_gate("disk")["acked"] is True


def test_ack_reports_unknown_and_already_ok(store):
    assert store.set_acked("ghost") == "unknown"
    bad(store, "cpu", "ok")
    assert store.set_acked("cpu") == "ok"
    assert store.get_gate("cpu")["acked"] is False


def test_ack_uses_the_confirmed_level_not_a_one_tick_spike(store):
    bad(store, "cpu", "crit", confirmed="ok")      # observed crit, nothing confirmed
    assert store.set_acked("cpu") == "ok"


def test_ack_works_on_a_row_from_before_the_gate_existed(store):
    store.set_level("disk", "crit", "full")
    assert store.set_acked("disk") == "acked"


def test_a_step_cannot_write_an_old_ack_back_over_a_new_one(store):
    # The bot acks while a check step that read `acked = False` is mid-flight;
    # that step then saves. The ack must survive.
    bad(store, "disk")
    store.set_acked("disk")
    bad(store, "disk")                             # a save with reset_ack=False
    assert store.get_gate("disk")["acked"] is True


def test_an_announced_change_clears_the_ack(store):
    bad(store, "disk")
    store.set_acked("disk")
    bad(store, "disk", reset_ack=True)
    assert store.get_gate("disk")["acked"] is False


def test_a_new_row_starts_unacknowledged(store):
    bad(store, "disk", reset_ack=False)
    assert store.get_gate("disk")["acked"] is False


def test_bad_checks_lists_only_confirmed_problems(store):
    bad(store, "a", "crit")
    bad(store, "b", "warn")
    bad(store, "c", "ok")
    bad(store, "d", "crit", confirmed="ok")
    assert sorted(s["name"] for s in store.bad_checks()) == ["a", "b"]


def test_target_ids_are_stable_and_distinct(store):
    a = store.target_id("docker:app")
    b = store.target_id("docker:db")
    assert a != b and store.target_id("docker:app") == a
    assert store.target_name(a) == "docker:app" and store.target_name(b) == "docker:db"
    assert store.target_name(99999) is None


def test_a_very_long_name_still_fits_a_button(store):
    tid = store.target_id("docker:" + "x" * 500)
    assert len(f"m:{tid}:480") < 64
    assert store.target_name(tid).startswith("docker:xxx")


def test_prune_forgets_old_targets_but_never_inside_a_week(store):
    old, recent = store.target_id("old"), store.target_id("recent")
    with store._lock:
        store._c.execute("UPDATE targets SET ts = ? WHERE id = ?",
                         (time.time() - 10 * 86400, old))
        store._c.commit()
    store.prune(retention_days=1, max_rows=100)         # a 1-day retention...
    assert store.target_name(old) is None               # ...still keeps 7 days
    assert store.target_name(recent) == "recent"
    mid = store.target_id("mid")
    with store._lock:
        store._c.execute("UPDATE targets SET ts = ? WHERE id = ?",
                         (time.time() - 3 * 86400, mid))
        store._c.commit()
    store.prune(retention_days=1, max_rows=100)
    assert store.target_name(mid) == "mid"
