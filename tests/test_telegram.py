"""Message rendering. These messages are the product — if the template
breaks, the operator gets a wall of raw metrics at 3am."""
from dwellerd.notify import Alert, TelegramNotifier


def notifier(lang="en"):
    return TelegramNotifier("token", "chat", lang=lang, hostname="box-1")


def test_crit_alert_reads_like_a_sentence():
    text = notifier().render_alert(Alert(
        check="memory", level="crit", kind="memory", detail="RAM 95%",
        metrics={"value": 95.2, "threshold": 90.0},
    ))
    assert "🔴" in text
    assert "Memory usage critical" in text
    assert "<b>95.2%</b>" in text
    assert "box-1" in text
    assert text.endswith("<i>box-1</i>")
    assert " · " not in text


def test_recovery_uses_the_recovery_wording():
    text = notifier().render_alert(Alert(
        check="memory", level="ok", kind="memory", detail="RAM 40%",
        metrics={"value": 40.0, "threshold": 80.0},
    ))
    assert "✅" in text and "back to normal" in text


def test_russian_templates_are_used():
    text = notifier("ru").render_alert(Alert(
        check="disk-root", level="crit", kind="disk", detail="disk / 95%",
        metrics={"value": 95.0, "threshold": 90.0, "path": "/", "free_gb": 2.1},
    ))
    assert "Критически мало места" in text and "2.1 ГБ" in text


def test_missing_metric_falls_back_to_the_detail_line():
    # A template that can't be filled must not raise mid-alert.
    text = notifier().render_alert(Alert(
        check="disk", level="crit", kind="disk", detail="disk / is full",
        metrics={"value": 99.0},          # no path, no free_gb
    ))
    assert "disk / is full" in text


def test_unknown_kind_falls_back_to_the_check_name():
    text = notifier().render_alert(Alert(
        check="custom-thing", level="crit", detail="it broke",
    ))
    assert "custom-thing" in text and "it broke" in text


def test_html_in_log_lines_is_escaped():
    # A log line containing markup must not corrupt the message, or
    # Telegram rejects the whole send with a 400.
    text = notifier().render_log_first("app", "<script>alert(1)</script>")
    assert "<script>" not in text
    assert "&lt;script&gt;" in text
    assert text.startswith("🔴 <b>New error</b>")


def test_messages_without_hostname_have_no_timestamp_footer():
    plain = TelegramNotifier("token", "chat")
    text = plain.render_log_first("app", "ERROR boom")
    assert text.endswith("</pre>")


def test_digest_lists_counts_per_source():
    text = notifier().render_log_digest([
        {"source": "api", "count": 42, "sample": "connection refused"},
        {"source": "db", "count": 3, "sample": "deadlock detected"},
    ])
    assert "42×" in text and "api" in text and "deadlock" in text


def test_load_alert_explains_the_per_core_maths():
    text = notifier().render_alert(Alert(
        check="load", level="crit", kind="load", detail="load 16",
        metrics={"value": 4.0, "threshold": 4.0, "load1": 16.0,
                 "load5": 12.0, "load15": 9.0, "cores": 4, "per_core": 4.0},
    ))
    assert "<b>16.00</b>" in text and "<b>4</b> cores" in text


# ── grouping, reminders, silent delivery, startup notes ──────────────────


def alert(check, level="crit", kind="", detail="", **kw):
    return Alert(check=check, level=level, kind=kind, detail=detail or f"{check} bad", **kw)


def test_group_lists_worst_first_with_a_count():
    text = notifier().render_group([
        alert("a", "ok", "cpu"), alert("b", "warn", "cpu"), alert("c", "crit", "cpu"),
    ])
    assert "3 alerts" in text
    assert text.index("critical") < text.index("elevated") < text.index("back to normal")
    assert text.endswith("<i>box-1</i>")


def test_group_escapes_markup_in_details():
    text = notifier().render_group([
        alert("a", detail="<b>boom</b> & more"), alert("b"),
    ])
    assert "<b>boom</b>" not in text and "&lt;b&gt;boom&lt;/b&gt; &amp; more" in text


def test_a_huge_group_stays_under_the_limit_and_keeps_its_markup_balanced():
    # 200 alerts of worst-case escaping: the budget is enforced while
    # building, because truncating a finished message could cut a tag in
    # half and make Telegram reject the whole thing.
    alerts = [alert(f"container-{i}", detail="<&>" * 100) for i in range(200)]
    text = notifier().render_group(alerts)
    assert len(text) < 4000
    assert text.count("<b>") == text.count("</b>")
    assert text.count("<code>") == text.count("</code>")
    assert "200 alerts" in text and "…and" in text


def test_group_of_russian_uses_russian_labels():
    text = notifier("ru").render_group([alert("a"), alert("b")])
    assert "2 алертов" in text


def test_reminder_says_how_long_it_has_been():
    text = notifier().render_alert(alert(
        "disk-root", kind="disk", metrics={"value": 95.0, "threshold": 90.0,
                                           "path": "/", "free_gb": 1.0},
        ongoing=6 * 3600 + 120,
    ))
    assert "Disk space critical" in text and "unresolved after <b>6h 2m</b>" in text


def test_a_normal_alert_has_no_ongoing_line():
    assert "unresolved" not in notifier().render_alert(alert("x"))


def test_reminder_inside_a_group_shows_its_age():
    text = notifier().render_group([alert("a", ongoing=3600), alert("b")])
    assert "⏳ 1h 0m" in text


def test_startup_message_variants():
    n = notifier()
    assert "ended unexpectedly" not in n.render_startup()
    reboot = n.render_startup("reboot", 3 * 3600)
    assert "host was rebooted" in reboot and "3h 0m ago" in reboot
    crash = n.render_startup("crash", 90)
    assert "killed or crashed" in crash and "1m ago" in crash
    assert "перезагружен" in notifier("ru").render_startup("reboot", 60)


class _Capture:
    def __init__(self):
        self.payloads = []

    async def post(self, url, json=None, **kw):
        self.payloads.append(json)

        class R:
            status_code = 200
        return R()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def test_silent_sets_disable_notification(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr("dwellerd.notify.telegram.httpx.AsyncClient",
                        lambda **kw: cap)
    n = notifier()
    await n.send_text("hi")
    await n.send_text("psst", silent=True)
    await n.send_alert(alert("x"), silent=True)
    await n.send_group([alert("x"), alert("y")])
    assert "disable_notification" not in cap.payloads[0]
    assert cap.payloads[1]["disable_notification"] is True
    assert cap.payloads[2]["disable_notification"] is True
    assert "disable_notification" not in cap.payloads[3]


async def test_an_unexpected_restart_is_never_sent_silently(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr("dwellerd.notify.telegram.httpx.AsyncClient",
                        lambda **kw: cap)
    n = notifier()
    await n.send_startup(unclean="crash", last_seen_ago=5, silent=True)
    await n.send_startup(silent=True)
    assert "disable_notification" not in cap.payloads[0]
    assert cap.payloads[1]["disable_notification"] is True


async def test_notices(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr("dwellerd.notify.telegram.httpx.AsyncClient",
                        lambda **kw: cap)
    n = notifier()
    await n.send_notice("reloaded")
    await n.send_notice("reload_failed", "error: logs.sources[0].pattern: <bad>")
    assert "Configuration reloaded" in cap.payloads[0]["text"]
    failed = cap.payloads[1]["text"]
    assert "still running on the old one" in failed and "&lt;bad&gt;" in failed


# ── buttons ──────────────────────────────────────────────────────────────


def live(lang="en"):
    return TelegramNotifier("token", "chat", lang=lang, hostname="box-1", interactive=True)


def buttons(markup):
    return [[(b["text"], b["callback_data"]) for b in row] for row in markup["inline_keyboard"]]


def test_a_crit_alert_gets_mute_and_ack_buttons():
    markup = live().alert_markup(alert("disk", "crit", target_id=7))
    assert buttons(markup) == [
        [("🔕 Mute 1h", "m:7:60"), ("🔕 Mute 8h", "m:7:480")],
        [("👍 Got it", "a:7")],
    ]


def test_a_warn_alert_gets_mute_buttons_only():
    markup = live().alert_markup(alert("disk", "warn", target_id=7))
    assert buttons(markup) == [[("🔕 Mute 1h", "m:7:60"), ("🔕 Mute 8h", "m:7:480")]]


def test_recoveries_get_no_buttons():
    assert live().alert_markup(alert("disk", "ok", target_id=7)) is None


def test_no_buttons_when_not_interactive_or_without_a_target():
    assert notifier().alert_markup(alert("disk", "crit", target_id=7)) is None
    assert live().alert_markup(alert("disk", "crit", target_id=0)) is None
    assert live().mute_markup(0) is None


def test_russian_buttons():
    markup = live("ru").alert_markup(alert("disk", "crit", target_id=7))
    assert buttons(markup)[1] == [("👍 Принял", "a:7")]


async def test_buttons_travel_with_the_message_and_groups_have_none(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr("dwellerd.notify.telegram.httpx.AsyncClient", lambda **kw: cap)
    n = live()
    await n.send_alert(alert("disk", "crit", target_id=7))
    await n.send_alert(alert("disk", "ok", target_id=7))
    await n.send_group([alert("a", target_id=1), alert("b", target_id=2)])
    await n.send_log_first("nginx", "boom", target_id=9)
    assert cap.payloads[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "m:7:60"
    assert "reply_markup" not in cap.payloads[1]
    assert "reply_markup" not in cap.payloads[2]
    assert buttons(cap.payloads[3]["reply_markup"]) == [
        [("🔕 Mute 1h", "m:9:60"), ("🔕 Mute 8h", "m:9:480")]]


async def test_api_call_reports_status_and_swallows_network_errors(monkeypatch):
    class Boom:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            raise OSError("down")

    monkeypatch.setattr("dwellerd.notify.telegram.httpx.AsyncClient", lambda **kw: Boom())
    assert await live().api_call("getMe", {}) == (0, {})
