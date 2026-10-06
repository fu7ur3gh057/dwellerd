"""The `alerts:` section: defaults, clamping, and the quiet-hours window."""
from dwellerd.config import AlertsConfig, QuietHours, parse, parse_clock


def test_defaults_are_the_quiet_ones():
    a = parse({}).alerts
    assert (a.confirm, a.hysteresis, a.remind_after, a.group_window) == (2, 5.0, 21600.0, 5.0)
    assert a.quiet is None


def test_values_are_read_and_clamped():
    a = parse({"alerts": {"confirm": 0, "hysteresis": 99, "remind_after": -1,
                          "group_window": 999}}).alerts
    assert a.confirm == 1 and a.hysteresis == 50 and a.remind_after == 0
    assert a.group_window == 60


def test_null_section_and_null_values_use_defaults():
    assert parse({"alerts": None}).alerts == AlertsConfig()
    assert parse({"alerts": {"confirm": None}}).alerts.confirm == 2


def test_parse_clock():
    assert parse_clock("23:00") == 1380
    assert parse_clock("7:05") == 425
    assert parse_clock("00:00") == 0
    assert parse_clock(1380) == 1380            # YAML's sexagesimal int
    for bad in ("24:00", "12:60", "noon", "", None, True, -1, 1440, 12.5):
        assert parse_clock(bad) is None


def test_quiet_window_within_a_day():
    q = QuietHours(start=13 * 60, end=15 * 60)
    assert q.contains(13 * 60) and q.contains(14 * 60 + 59)
    assert not q.contains(15 * 60) and not q.contains(12 * 60 + 59)


def test_quiet_window_across_midnight():
    q = QuietHours(start=23 * 60, end=7 * 60)
    assert q.contains(23 * 60) and q.contains(0) and q.contains(6 * 60 + 59)
    assert not q.contains(7 * 60) and not q.contains(12 * 60) and not q.contains(22 * 60 + 59)


def test_an_empty_window_is_never_quiet():
    assert not QuietHours(start=300, end=300).contains(300)


def test_invalid_quiet_hours_parse_to_off_instead_of_crashing():
    for raw in ({"start": "x", "end": "07:00"}, {"start": "08:00", "end": "08:00"},
                "nightly", False, None):
        assert parse({"alerts": {"quiet_hours": raw}}).alerts.quiet is None


# ── telegram.interactive / admins ────────────────────────────────────────


def test_interactive_by_default_with_no_admin_list():
    tg = parse({}).telegram
    assert tg.interactive is True and tg.admins == []


def test_admins_are_read_as_strings():
    tg = parse({"telegram": {"admins": [123, "456"], "interactive": False}}).telegram
    assert tg.admins == ["123", "456"] and tg.interactive is False


def test_null_admins_is_an_empty_list():
    assert parse({"telegram": {"admins": None}}).telegram.admins == []
