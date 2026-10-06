"""The daemon's gate step end to end: confirmation, hysteresis, reminders,
mutes, quiet hours, grouping — against a real SQLite file and a fake
Telegram. These are the paths that decide what lands on the operator's
phone."""
import asyncio
import time

import pytest

from dwellerd.checks.base import Result, by_threshold
from dwellerd.config import parse
from dwellerd.daemon import Daemon


class FakeNotifier:
    interactive = False

    def __init__(self):
        self.sent = []          # ("alert"|"group"|"first"|...,  payload, silent)

    async def send_alert(self, alert, silent=False):
        self.sent.append(("alert", alert, silent))

    async def send_group(self, alerts, silent=False):
        self.sent.append(("group", list(alerts), silent))

    async def send_log_first(self, source, sample, silent=False, target_id=0):
        self.sent.append(("first", source, silent))

    async def send_log_digest(self, items, period="", silent=False):
        self.sent.append(("digest", items, silent))

    async def send_text(self, text, silent=False):
        self.sent.append(("text", text, silent))

    async def send_startup(self, unclean=None, last_seen_ago=0.0, silent=False):
        self.sent.append(("startup", unclean, silent))

    async def send_shutdown(self, silent=False):
        self.sent.append(("shutdown", None, silent))

    async def send_notice(self, key, detail="", silent=False):
        self.sent.append(("notice", key, detail))

    @property
    def alerts(self):
        return [s[1] for s in self.sent if s[0] == "alert"]


BASE = {
    "telegram": {"interactive": False},     # the bot is tested in test_bot.py
    "checks": {"interval": 3600},
    "logs": {"enabled": False},
    "report": {"enabled": False},
}


def make(tmp_path, alerts=None, **extra):
    raw = {**BASE, "alerts": {"confirm": 1, "hysteresis": 0, "remind_after": 0,
                              "group_window": 0, **(alerts or {})}, **extra}
    cfg = parse(raw)
    cfg.db_path = str(tmp_path / "d.sqlite")
    d = Daemon(cfg)
    d.notifier = FakeNotifier()
    d.storage.connect()
    return d


@pytest.fixture(autouse=True)
def no_real_checks(monkeypatch):
    """Check loops stagger their first run by up to 5s. Push that out of
    reach so a test never has a real CPU/disk check race its assertions."""
    monkeypatch.setattr("dwellerd.daemon.random.uniform", lambda a, b: 3600.0)


@pytest.fixture
def daemon(tmp_path):
    d = make(tmp_path)
    yield d
    d.storage.close()


def res(level, detail="x", kind="cpu"):
    return Result(level=level, detail=detail, kind=kind, metrics={"value": 1.0, "threshold": 80.0})


async def step(d, name, level, **kw):
    await d._gate_step(name, kw.get("result") or res(level))


# ── the basics ───────────────────────────────────────────────────────────


async def test_a_problem_alerts_once_and_recovery_alerts_once(daemon):
    for level in ("ok", "crit", "crit", "crit", "ok", "ok"):
        await step(daemon, "cpu", level)
    assert [(a.level) for a in daemon.notifier.alerts] == ["crit", "ok"]


async def test_alert_history_is_recorded(daemon):
    await step(daemon, "cpu", "crit")
    assert [a["level"] for a in daemon.storage.alerts_since(0)] == ["crit"]


async def test_confirmation_delays_the_alert(tmp_path):
    d = make(tmp_path, alerts={"confirm": 3})
    try:
        await step(d, "cpu", "crit")
        await step(d, "cpu", "crit")
        assert d.notifier.alerts == []
        await step(d, "cpu", "crit")
        assert [a.level for a in d.notifier.alerts] == ["crit"]
    finally:
        d.storage.close()


async def test_a_spike_never_reaches_telegram(tmp_path):
    d = make(tmp_path, alerts={"confirm": 2})
    try:
        for level in ("ok", "crit", "ok", "crit", "ok"):
            await step(d, "cpu", level)
        assert d.notifier.alerts == []
    finally:
        d.storage.close()


async def test_pending_streak_survives_a_restart(tmp_path):
    d = make(tmp_path, alerts={"confirm": 2})
    await step(d, "cpu", "crit")
    d.storage.close()
    d2 = make(tmp_path, alerts={"confirm": 2})      # same database file
    try:
        await step(d2, "cpu", "crit")
        assert [a.level for a in d2.notifier.alerts] == ["crit"]
    finally:
        d2.storage.close()


async def test_a_restart_does_not_reannounce_a_known_problem(tmp_path):
    d = make(tmp_path)
    await step(d, "cpu", "crit")
    d.storage.close()
    d2 = make(tmp_path)
    try:
        await step(d2, "cpu", "crit")
        assert d2.notifier.alerts == []
    finally:
        d2.storage.close()


async def test_the_report_sees_the_confirmed_level_not_a_spike(tmp_path):
    d = make(tmp_path, alerts={"confirm": 2})
    try:
        await step(d, "cpu", "crit")                 # observed crit, unconfirmed
        state = d.storage.all_states()[0]
        assert state["level"] == "crit" and state["confirmed"] == "ok"
    finally:
        d.storage.close()


async def test_a_result_that_cannot_be_gated_is_logged_not_raised(daemon, caplog):
    class Broken:
        level = "crit"
        ladder = None
        kind = "x"
        detail = "d"
        metrics = None          # dict(None) blows up when the alert is built

    await daemon._process("weird", Broken())
    assert "gate step for weird failed" in caplog.text


# ── hysteresis ───────────────────────────────────────────────────────────


async def test_hysteresis_stops_flapping_end_to_end(tmp_path):
    d = make(tmp_path, alerts={"hysteresis": 5})
    try:
        for pct in (82, 79, 81, 78, 80.5, 79.5, 70):
            await d._gate_step("memory", by_threshold(
                pct=pct, warn=80, crit=90, kind="memory", label="RAM"))
        # warn at 82, held through the wobble, recovered only at 70.
        assert [a.level for a in d.notifier.alerts] == ["warn", "ok"]
    finally:
        d.storage.close()


async def test_alert_text_uses_the_threshold_of_the_announced_level(tmp_path):
    # confirm=2: warn then crit -> the streak confirms warn, but the latest
    # reading is already past crit. The message must quote the warn
    # threshold next to "elevated", not the crit one.
    d = make(tmp_path, alerts={"confirm": 2})
    try:
        await d._gate_step("memory", by_threshold(pct=85, warn=80, crit=90, kind="memory", label="RAM"))
        await d._gate_step("memory", by_threshold(pct=95, warn=80, crit=90, kind="memory", label="RAM"))
        (alert,) = d.notifier.alerts
        assert alert.level == "warn" and alert.metrics["threshold"] == 80
    finally:
        d.storage.close()


# ── reminders ────────────────────────────────────────────────────────────


def age_last_alert(d, name, seconds):
    gate = d.storage.get_gate(name)
    d.storage.save_gate(
        name, "crit", "x", confirmed=gate["confirmed"], pending=None,
        pending_count=0, silenced=gate["silenced"],
        last_alert=gate["last_alert"] - seconds,
    )


async def test_a_long_crit_is_reannounced(tmp_path):
    d = make(tmp_path, alerts={"remind_after": 3600})
    try:
        await step(d, "disk", "crit")
        await step(d, "disk", "crit")
        assert len(d.notifier.alerts) == 1                 # not due yet
        age_last_alert(d, "disk", 3601)
        await step(d, "disk", "crit")
        assert len(d.notifier.alerts) == 2
        reminder = d.notifier.alerts[1]
        assert reminder.level == "crit" and reminder.ongoing > 0
        await step(d, "disk", "crit")
        assert len(d.notifier.alerts) == 2                 # clock was reset
        assert len(d.storage.alerts_since(0)) == 1         # history: not a new event
    finally:
        d.storage.close()


async def test_reminders_stop_after_recovery_and_ignore_warn(tmp_path):
    d = make(tmp_path, alerts={"remind_after": 60})
    try:
        await step(d, "a", "warn")
        gate = d.storage.get_gate("a")
        assert gate["last_alert"] is None                  # warn is never nagged
        await step(d, "a", "warn")
        assert len(d.notifier.alerts) == 1
        await step(d, "b", "crit")
        await step(d, "b", "ok")
        assert d.storage.get_gate("b")["last_alert"] is None
    finally:
        d.storage.close()


async def test_reminder_is_off_by_default_value_zero(daemon):
    await step(daemon, "disk", "crit")
    age_last_alert(daemon, "disk", 10**7)
    await step(daemon, "disk", "crit")
    assert len(daemon.notifier.alerts) == 1


# ── mutes ────────────────────────────────────────────────────────────────


async def test_a_muted_alert_is_recorded_but_not_sent(daemon):
    daemon.storage.add_mute("cpu", time.time() + 60)
    await step(daemon, "cpu", "crit")
    assert daemon.notifier.alerts == []
    assert len(daemon.storage.alerts_since(0)) == 1
    assert daemon.storage.get_gate("cpu")["silenced"] is True


async def test_no_orphan_recovery_after_a_muted_alert(daemon):
    daemon.storage.add_mute("cpu", time.time() + 60)
    await step(daemon, "cpu", "crit")
    daemon.storage.remove_mute()
    await step(daemon, "cpu", "ok")                  # recovery of an alert nobody saw
    assert daemon.notifier.alerts == []
    assert daemon.storage.get_gate("cpu")["silenced"] is False
    await step(daemon, "cpu", "crit")                # and the next one is heard
    assert [a.level for a in daemon.notifier.alerts] == ["crit"]


async def test_a_delivered_alert_still_gets_its_recovery_after_the_mute_ends(daemon):
    await step(daemon, "cpu", "crit")
    daemon.storage.add_mute("cpu", time.time() + 60)
    await step(daemon, "cpu", "ok")                  # recovered while muted: quiet
    assert [a.level for a in daemon.notifier.alerts] == ["crit"]


async def test_an_alert_delivered_after_the_mute_ends_clears_the_silenced_flag(daemon):
    daemon.storage.add_mute("cpu", time.time() + 60)
    await step(daemon, "cpu", "warn")                # muted, silenced
    daemon.storage.remove_mute()
    await step(daemon, "cpu", "crit")                # escalation is heard
    assert daemon.storage.get_gate("cpu")["silenced"] is False
    await step(daemon, "cpu", "ok")
    assert [a.level for a in daemon.notifier.alerts] == ["crit", "ok"]


async def test_mute_is_per_name_and_expires(daemon):
    daemon.storage.add_mute("cpu", time.time() + 60)
    await step(daemon, "memory", "crit")
    assert len(daemon.notifier.alerts) == 1
    daemon.storage.add_mute("disk", time.time() - 1)         # already expired
    await step(daemon, "disk", "crit")
    assert len(daemon.notifier.alerts) == 2


async def test_a_muted_reminder_is_sent_once_the_mute_ends(tmp_path):
    d = make(tmp_path, alerts={"remind_after": 3600})
    try:
        await step(d, "disk", "crit")
        age_last_alert(d, "disk", 4000)
        d.storage.add_mute("disk", time.time() + 60)
        await step(d, "disk", "crit")
        assert len(d.notifier.alerts) == 1
        d.storage.remove_mute()
        await step(d, "disk", "crit")
        assert len(d.notifier.alerts) == 2 and d.notifier.alerts[1].ongoing > 0
    finally:
        d.storage.close()


async def test_log_alerts_honour_mutes_but_the_digest_does_not(daemon):
    daemon.storage.add_mute("log:nginx", time.time() + 60)
    await daemon._on_log_first("nginx", "boom")
    await daemon._on_log_first("app", "boom")
    await daemon._on_log_digest([{"source": "nginx", "count": 1, "sample": "x"}], "1h")
    assert [(s[0], s[1]) for s in daemon.notifier.sent if s[0] == "first"] == [("first", "app")]
    assert any(s[0] == "digest" for s in daemon.notifier.sent)


# ── quiet hours ──────────────────────────────────────────────────────────


async def test_quiet_hours_silence_everything_but_crit(daemon, monkeypatch):
    monkeypatch.setattr(daemon, "_quiet_now", lambda: True)
    await step(daemon, "a", "warn")
    await step(daemon, "b", "crit")
    await step(daemon, "a", "ok")
    await step(daemon, "b", "ok")
    silent = {(a.check, a.level): s for (_, a, s) in daemon.notifier.sent}
    assert silent == {("a", "warn"): True, ("b", "crit"): False,
                      ("a", "ok"): True, ("b", "ok"): True}


async def test_outside_quiet_hours_nothing_is_silent(daemon):
    await step(daemon, "a", "warn")
    assert daemon.notifier.sent[0][2] is False


def test_quiet_now_follows_the_clock(tmp_path, monkeypatch):
    d = make(tmp_path, alerts={"quiet_hours": {"start": "23:00", "end": "07:00"}})
    try:
        for hour, minute, expected in [(23, 0, True), (3, 30, True), (6, 59, True),
                                       (7, 0, False), (12, 0, False), (22, 59, False)]:
            monkeypatch.setattr(
                time, "localtime",
                lambda *a, h=hour, m=minute: time.struct_time((2026, 1, 1, h, m, 0, 0, 1, 0)),
            )
            assert d._quiet_now() is expected, (hour, minute)
    finally:
        d.storage.close()


def test_without_quiet_hours_it_is_never_quiet(daemon):
    assert daemon._quiet_now() is False


# ── grouping ─────────────────────────────────────────────────────────────


async def test_simultaneous_alerts_go_out_as_one_message(tmp_path):
    d = make(tmp_path, alerts={"group_window": 60})
    try:
        for name in ("a", "b", "c"):
            await step(d, name, "crit")
        assert d.notifier.sent == []                    # still collecting
        await d.batcher.close()
        assert [s[0] for s in d.notifier.sent] == ["group"]
        assert len(d.notifier.sent[0][1]) == 3 and d.notifier.sent[0][2] is False
    finally:
        d.storage.close()


async def test_one_alert_is_sent_as_itself_not_as_a_group_of_one(tmp_path):
    d = make(tmp_path, alerts={"group_window": 60})
    try:
        await step(d, "a", "crit")
        await d.batcher.close()
        assert [s[0] for s in d.notifier.sent] == ["alert"]
    finally:
        d.storage.close()


async def test_a_group_is_silent_only_if_every_member_is(tmp_path, monkeypatch):
    d = make(tmp_path, alerts={"group_window": 60})
    try:
        monkeypatch.setattr(d, "_quiet_now", lambda: True)
        await step(d, "a", "warn")
        await step(d, "b", "warn")
        await d.batcher.close()
        assert d.notifier.sent[0][0] == "group" and d.notifier.sent[0][2] is True

        d.notifier.sent.clear()
        await step(d, "c", "warn")
        await step(d, "d", "crit")
        await d.batcher.close()
        assert d.notifier.sent[0][0] == "group" and d.notifier.sent[0][2] is False
    finally:
        d.storage.close()


async def test_stopping_flushes_pending_alerts(tmp_path):
    d = make(tmp_path, alerts={"group_window": 60})
    try:
        await step(d, "a", "crit")
        await d._stop_tasks()
        assert [s[0] for s in d.notifier.sent] == ["alert"]
    finally:
        d.storage.close()


async def test_without_telegram_alerts_are_only_logged(tmp_path, caplog):
    import logging
    d = make(tmp_path)
    d.notifier = None
    try:
        with caplog.at_level(logging.INFO, logger="dwellerd"):
            await step(d, "cpu", "crit")
        assert "alert (telegram off): cpu crit" in caplog.text
    finally:
        d.storage.close()


# ── how the last run ended ───────────────────────────────────────────────


def test_first_ever_start_is_normal(tmp_path):
    d = make(tmp_path)
    try:
        assert d._note_start()[0] is None
    finally:
        d.storage.close()


def test_a_clean_stop_means_a_normal_next_start(tmp_path):
    d = make(tmp_path)
    d._note_start()
    d.storage.set_meta(run_state="stopped")
    assert d._note_start()[0] is None
    d.storage.close()


def test_a_missing_goodbye_is_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr("dwellerd.daemon.psutil.boot_time", lambda: 1.0)   # long ago
    d = make(tmp_path)
    d._note_start()
    unclean, last_seen = d._note_start()               # no stop in between
    assert unclean == "crash" and last_seen > 0
    d.storage.close()


def test_a_boot_after_the_last_heartbeat_is_a_reboot(tmp_path, monkeypatch):
    d = make(tmp_path)
    d._note_start()
    monkeypatch.setattr("dwellerd.daemon.psutil.boot_time", lambda: time.time() + 5)
    assert d._note_start()[0] == "reboot"
    d.storage.close()


def test_garbage_in_meta_does_not_break_startup(tmp_path):
    d = make(tmp_path)
    d.storage.set_meta(run_state="running", last_seen="not-a-number")
    assert d._note_start()[0] == "crash"
    d.storage.close()


async def test_full_lifecycle_records_start_and_clean_stop(tmp_path):
    d = make(tmp_path)
    d.storage.close()                  # run() opens its own connection
    run = asyncio.create_task(d.run())
    await asyncio.sleep(0.1)
    assert d.storage.get_meta("run_state") == "running"
    sent_at_start = [s[0] for s in d.notifier.sent]
    assert sent_at_start == ["startup"]
    d.stop()
    await asyncio.wait_for(run, 5)
    assert [s[0] for s in d.notifier.sent] == ["startup", "shutdown"]
    # storage is closed by run(); reopen to inspect.
    from dwellerd.storage import Storage
    s = Storage(d.cfg.db)
    s.connect()
    assert s.get_meta("run_state") == "stopped"
    s.close()


async def test_startup_reports_an_unclean_previous_run(tmp_path, monkeypatch):
    monkeypatch.setattr("dwellerd.daemon.psutil.boot_time", lambda: 1.0)
    d = make(tmp_path)
    d.storage.set_meta(run_state="running", last_seen=time.time() - 120)
    d.storage.close()                  # run() opens its own connection
    run = asyncio.create_task(d.run())
    await asyncio.sleep(0.1)
    d.stop()
    await asyncio.wait_for(run, 5)
    assert d.notifier.sent[0] == ("startup", "crash", False)


# ── reload ───────────────────────────────────────────────────────────────

GOOD = """
telegram: {bot_token: "1:a", chat_id: "1", interactive: false}
checks: {interval: 3600}
alerts: {confirm: %d, group_window: 0}
logs: {enabled: false}
report: {enabled: false}
db_path: "%s"
"""


def good(tmp_path, confirm):
    return GOOD % (confirm, tmp_path / "d.sqlite")


def make_with_file(tmp_path, text, monkeypatch):
    """A daemon started from a real file, with no way to reach the network:
    every notifier — the first and any reload builds — is the same fake."""
    path = tmp_path / "config.yaml"
    path.write_text(text)
    from dwellerd.config import load
    d = Daemon.__new__(Daemon)
    fake = FakeNotifier()
    monkeypatch.setattr(Daemon, "_make_notifier", staticmethod(lambda cfg: fake))
    Daemon.__init__(d, load(path))
    d.storage.connect()
    d._start_tasks()
    return d, path, fake


async def test_reload_applies_a_good_config_and_keeps_state(tmp_path, monkeypatch):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        await step(d, "probe", "crit")                  # state to carry over
        old_tasks = list(d._tasks)
        path.write_text(good(tmp_path, 4))
        assert await d.reload() is True
        assert d.cfg.alerts.confirm == 4
        assert d._tasks and all(t not in old_tasks for t in d._tasks)
        assert all(t.cancelled() or t.done() for t in old_tasks)
        assert d.storage.get_gate("probe")["pending_count"] == 1   # nothing forgotten
    finally:
        await d._stop_tasks()
        d.storage.close()


async def test_reload_rebuilds_the_notifier_and_says_so(tmp_path, monkeypatch):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        path.write_text(good(tmp_path, 3))
        assert await d.reload()
        assert ("notice", "reloaded", "") in fake.sent
    finally:
        await d._stop_tasks()
        d.storage.close()


async def test_reload_picks_up_a_new_telegram_target(tmp_path, monkeypatch):
    # The real factory, with the network cut off at the one place it leaves
    # the process.
    from dwellerd.notify import TelegramNotifier
    posted = []

    async def no_network(self, text, silent=False):
        posted.append((self.chat_id, text))

    monkeypatch.setattr(TelegramNotifier, "send_text", no_network)
    path = tmp_path / "config.yaml"
    path.write_text(good(tmp_path, 2))
    from dwellerd.config import load
    d = Daemon(load(path))
    d.storage.connect()
    d._start_tasks()
    try:
        path.write_text(good(tmp_path, 2).replace('chat_id: "1"', 'chat_id: "42"'))
        assert await d.reload()
        assert d.notifier.chat_id == "42"
        assert posted and posted[-1][0] == "42" and "Configuration reloaded" in posted[-1][1]
    finally:
        await d._stop_tasks()
        d.storage.close()


@pytest.mark.parametrize("bad", [
    "checks: {cpu: {warn: 95, crit: 90}}",                 # validation error
    "logs: {sources: [{type: file, path: /x, pattern: '(' }]}",   # bad regex
    "checks: [unclosed",                                   # YAML syntax
    "- just\n- a list",                                    # wrong top level
    "",                                                    # empty is valid! (see below)
])
async def test_reload_refuses_a_broken_config_and_keeps_running(tmp_path, monkeypatch, bad):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        before_cfg, before_tasks = d.cfg, list(d._tasks)
        path.write_text(bad)
        applied = await d.reload()
        if bad == "":
            assert applied is True                       # an empty file is a valid config
            return
        assert applied is False
        assert d.cfg is before_cfg and d._tasks == before_tasks
        assert not any(t.done() for t in d._tasks)
        assert ("notice", "reload_failed") == fake.sent[-1][:2]
    finally:
        await d._stop_tasks()
        d.storage.close()


async def test_reload_survives_the_config_file_disappearing(tmp_path, monkeypatch):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        path.unlink()
        assert await d.reload() is False
        assert d._tasks and not any(t.done() for t in d._tasks)
    finally:
        await d._stop_tasks()
        d.storage.close()


async def test_a_failure_while_applying_rolls_back(tmp_path, monkeypatch):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        original = d.cfg
        real_apply = d._apply
        calls = []

        def flaky(cfg):
            calls.append(cfg)
            if len(calls) == 1:
                raise RuntimeError("cannot build checks")
            real_apply(cfg)

        monkeypatch.setattr(d, "_apply", flaky)
        path.write_text(good(tmp_path, 4))
        assert await d.reload() is False
        assert calls[1] is original                      # rolled back to the old config
        assert d._tasks and not any(t.done() for t in d._tasks)
    finally:
        await d._stop_tasks()
        d.storage.close()


async def test_a_failed_rollback_stops_the_daemon_for_systemd_to_restart(tmp_path, monkeypatch):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        monkeypatch.setattr(d, "_apply", lambda cfg: (_ for _ in ()).throw(RuntimeError("x")))
        path.write_text(good(tmp_path, 4))
        assert await d.reload() is False
        assert d._stop.is_set()
    finally:
        await d._stop_tasks()
        d.storage.close()


async def test_reload_is_ignored_during_shutdown(tmp_path, monkeypatch):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        d.stop()
        assert await d.reload() is False
    finally:
        await d._stop_tasks()
        d.storage.close()


async def test_reload_without_a_file_path_is_a_no_op(tmp_path):
    d = make(tmp_path)
    try:
        d.cfg.source_path = None
        assert await d.reload() is False
    finally:
        d.storage.close()


async def test_concurrent_reloads_do_not_overlap(tmp_path, monkeypatch):
    d, path, fake = make_with_file(tmp_path, good(tmp_path, 2), monkeypatch)
    try:
        path.write_text(good(tmp_path, 5))
        results = await asyncio.gather(d.reload(), d.reload(), d.reload())
        assert results == [True, True, True]
        assert len(d._tasks) == len(set(d._tasks))
        assert not any(t.done() for t in d._tasks)
    finally:
        await d._stop_tasks()
        d.storage.close()


# ── buttons, acknowledgements and the bot task ───────────────────────────


def make_interactive(tmp_path, **alerts):
    d = make(tmp_path, alerts=alerts)
    d.notifier.interactive = True
    return d


async def test_alerts_carry_a_button_target_that_resolves_to_their_name(tmp_path):
    d = make_interactive(tmp_path)
    try:
        await step(d, "docker:app", "crit")
        (alert,) = d.notifier.alerts
        assert alert.target_id and d.storage.target_name(alert.target_id) == "docker:app"
    finally:
        d.storage.close()


async def test_recoveries_and_non_interactive_alerts_carry_no_target(tmp_path):
    d = make_interactive(tmp_path)
    try:
        await step(d, "cpu", "crit")
        await step(d, "cpu", "ok")
        assert [a.target_id > 0 for a in d.notifier.alerts] == [True, False]
    finally:
        d.storage.close()
    (tmp_path / "plain").mkdir()
    plain = make(tmp_path / "plain")
    try:
        await step(plain, "cpu", "crit")
        assert plain.notifier.alerts[0].target_id == 0
    finally:
        plain.storage.close()


async def test_a_new_error_alert_carries_a_mute_target(tmp_path):
    seen = []

    class Spy(FakeNotifier):
        async def send_log_first(self, source, sample, silent=False, target_id=0):
            seen.append(target_id)

    d = make_interactive(tmp_path)
    d.notifier = Spy()
    d.notifier.interactive = True
    try:
        await d._on_log_first("nginx", "boom")
        assert d.storage.target_name(seen[0]) == "log:nginx"
    finally:
        d.storage.close()


async def test_acknowledging_stops_the_reminders(tmp_path):
    d = make(tmp_path, alerts={"remind_after": 3600})
    try:
        await step(d, "disk", "crit")
        d.storage.set_acked("disk")
        age_last_alert(d, "disk", 10_000)
        await step(d, "disk", "crit")
        assert len(d.notifier.alerts) == 1                  # acked: no nag
    finally:
        d.storage.close()


async def test_an_ack_ends_with_the_episode(tmp_path):
    d = make(tmp_path, alerts={"remind_after": 3600})
    try:
        await step(d, "disk", "crit")
        d.storage.set_acked("disk")
        await step(d, "disk", "ok")                         # recovery announced
        assert d.storage.get_gate("disk")["acked"] is False
        await step(d, "disk", "crit")                       # a new episode
        age_last_alert(d, "disk", 10_000)
        await step(d, "disk", "crit")
        assert [a.ongoing > 0 for a in d.notifier.alerts] == [False, False, False, True]
    finally:
        d.storage.close()


async def test_an_escalation_clears_the_ack(tmp_path):
    d = make(tmp_path, alerts={"remind_after": 3600})
    try:
        await step(d, "disk", "warn")
        d.storage.set_acked("disk")
        await step(d, "disk", "crit")                       # worse: announced again
        assert d.storage.get_gate("disk")["acked"] is False
    finally:
        d.storage.close()


async def test_a_silent_partial_recovery_keeps_the_ack(tmp_path):
    d = make(tmp_path)
    try:
        await step(d, "disk", "crit")
        d.storage.set_acked("disk")
        await step(d, "disk", "warn")                       # crit -> warn: not announced
        assert d.storage.get_gate("disk")["acked"] is True
    finally:
        d.storage.close()


async def test_the_bot_runs_only_when_the_notifier_is_interactive(tmp_path):
    d = make_interactive(tmp_path)
    try:
        d._start_tasks()
        assert d.bot is not None and any(t.get_name() == "bot" for t in d._tasks)
        await d._stop_tasks()
        assert d.bot is None
        d.notifier.interactive = False
        d._start_tasks()
        assert d.bot is None and not any(t.get_name() == "bot" for t in d._tasks)
        await d._stop_tasks()
    finally:
        d.storage.close()
