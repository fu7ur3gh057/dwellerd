"""The CLI surface added with the alert controls: mute, unmute, mutes,
and `config --validate`."""
import time

import pytest

from dwellerd import cli
from dwellerd.duration import parse_duration
from dwellerd.storage import Storage


@pytest.fixture
def conf(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(f'db_path: "{tmp_path / "d.sqlite"}"\n')
    return path, tmp_path / "d.sqlite"


def run(conf_path, *argv):
    try:
        cli.main(["-c", str(conf_path), *argv])
    except SystemExit as e:
        return e.code
    return 0


def mutes(db):
    s = Storage(db)
    s.connect()
    try:
        return s.active_mutes()
    finally:
        s.close()


def test_parse_duration_units_and_compounds():
    assert parse_duration("90s") == 90
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    assert parse_duration("1d") == 86400
    assert parse_duration("1h30m") == 5400
    assert parse_duration(" 2H ") == 7200


@pytest.mark.parametrize("bad", ["30", "", "0m", "abc", "1h x", "h1", "-5m", "1.5h"])
def test_parse_duration_rejects_ambiguity(bad):
    with pytest.raises(ValueError):
        parse_duration(bad)


def test_mute_everything_for_a_while(conf, capsys):
    path, db = conf
    before = time.time()
    assert run(path, "mute", "2h") == 0
    (m,) = mutes(db)
    assert m["pattern"] == "*" and 7190 < m["until"] - before < 7210
    assert "digest" in capsys.readouterr().out


def test_mute_one_pattern(conf):
    path, db = conf
    assert run(path, "mute", "30m", "docker:*") == 0
    assert [m["pattern"] for m in mutes(db)] == ["docker:*"]


def test_bad_duration_changes_nothing(conf, capsys):
    path, db = conf
    assert run(path, "mute", "30") == 2
    assert "cannot read" in capsys.readouterr().err
    assert mutes(db) == []


def test_unmute_one_then_all(conf, capsys):
    path, db = conf
    run(path, "mute", "1h", "cpu")
    run(path, "mute", "1h", "memory")
    assert run(path, "unmute", "cpu") == 0
    assert [m["pattern"] for m in mutes(db)] == ["memory"]
    assert run(path, "unmute") == 0
    assert mutes(db) == []
    capsys.readouterr()
    assert run(path, "unmute", "ghost") == 0
    assert "nothing to remove" in capsys.readouterr().out


def test_mutes_lists_what_is_active(conf, capsys):
    path, _ = conf
    run(path, "mutes")
    assert "no active mutes" in capsys.readouterr().out
    run(path, "mute", "1h", "cpu")
    capsys.readouterr()
    run(path, "mutes")
    out = capsys.readouterr().out
    assert "cpu" in out and "left" in out


def test_mute_with_a_missing_config_fails_cleanly(tmp_path, capsys):
    assert run(tmp_path / "nope.yaml", "mute", "1h") == 2
    assert "config not found" in capsys.readouterr().err


def test_validate_ok_with_warnings_exits_zero(tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text("typo_key: 1\n")
    assert run(p, "config", "--validate") == 0
    out = capsys.readouterr().out
    assert "typo_key" in out and "OK" in out and "warning" in out


def test_validate_with_errors_exits_two(tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text("checks:\n  cpu: {warn: 95, crit: 90}\n")
    assert run(p, "config", "--validate") == 2
    assert "INVALID" in capsys.readouterr().out


def test_validate_reports_unreadable_yaml(tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text("checks: [unclosed\n")
    assert run(p, "config", "--validate") == 2
    assert "cannot read the config" in capsys.readouterr().err


def test_validate_rejects_a_list_where_a_number_belongs(tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text("logs:\n  retention_days: [1]\n")
    assert run(p, "config", "--validate") == 2


def test_plain_config_summary_includes_the_alert_settings(tmp_path, capsys):
    p = tmp_path / "c.yaml"
    p.write_text('alerts: {confirm: 3, quiet_hours: {start: "23:00", end: "07:00"}}\n')
    assert run(p, "config") == 0
    out = capsys.readouterr().out
    assert "confirm 3x" in out and "23:00-07:00" in out
