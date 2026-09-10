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
    assert text.startswith("🚨 <b>New error</b>")


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
