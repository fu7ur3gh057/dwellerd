"""`config --validate` and the reload gate share this: it must catch what
really is broken without crying wolf about what works."""
import yaml

from dwellerd.config import parse
from dwellerd.validate import errors, validate


def problems(text):
    return validate(yaml.safe_load(text))


def paths(items):
    return {p.path for p in items}


def test_the_example_config_is_clean():
    from pathlib import Path
    raw = yaml.safe_load((Path(__file__).parent.parent / "config.example.yaml").read_text())
    assert errors(validate(raw)) == []


def test_empty_and_null_sections_are_fine():
    assert errors(validate({})) == []
    assert errors(validate(None)) == []
    assert errors(validate({"logs": None, "checks": None, "alerts": None})) == []


def test_top_level_must_be_a_mapping():
    assert errors(validate([1, 2]))


def test_unknown_keys_are_warnings_with_the_full_path():
    found = problems("checks:\n  cpu: {warn: 70, crit: 90}\n  intervall: 30\n"
                     "alerts:\n  confrim: 3\nbogus: 1\n")
    assert {"checks.intervall", "alerts.confrim", "bogus"} <= paths(found)
    assert all(p.level == "warning" for p in found if "." in p.path or p.path == "bogus")


def test_warn_must_be_below_crit():
    found = errors(problems("checks:\n  cpu: {warn: 90, crit: 80}\n"))
    assert [p.path for p in found] == ["checks.cpu"]


def test_defaults_are_used_when_only_one_threshold_is_given():
    # warn 95 against the default crit of 90 is a contradiction.
    assert errors(problems("checks:\n  memory: {warn: 95}\n"))
    assert errors(problems("checks:\n  memory: {crit: 70}\n"))
    assert not errors(problems("checks:\n  memory: {warn: 60}\n"))


def test_explicit_null_means_default_like_the_parser():
    assert not errors(problems("checks:\n  cpu: {warn: , crit: 95}\n"))


def test_a_disabled_check_is_not_validated_as_a_mapping():
    assert not errors(problems("checks:\n  cpu: false\n  swap: false\n"))


def test_bad_numbers_are_errors_not_exceptions():
    found = errors(problems("checks:\n  interval: soon\n  cpu: {warn: lots}\n"))
    assert {"checks.interval", "checks.cpu.warn"} <= paths(found)


def test_interval_must_be_positive():
    assert errors(problems("checks:\n  interval: 0\n"))


def test_http_probe_problems():
    found = errors(problems(
        "checks:\n  http:\n    - {name: a, url: 'example.com'}\n"
        "    - {name: b}\n    - {url: 'https://x', expect_status: 999}\n"
    ))
    assert {"checks.http[0]", "checks.http[1]", "checks.http[2].expect_status"} <= paths(found)


def test_bad_log_regex_is_an_error():
    found = errors(problems(
        "logs:\n  sources:\n    - {type: file, name: a, path: /x, pattern: '(unclosed'}\n"
    ))
    assert paths(found) == {"logs.sources[0].pattern"}


def test_log_source_required_fields_and_types():
    found = errors(problems(
        "logs:\n  sources:\n"
        "    - {type: file, name: a}\n"
        "    - {type: journal, name: b}\n"
        "    - {type: docker_container, name: c}\n"
        "    - {type: docker, name: d, compose: /x}\n"
        "    - {type: telepathy, name: e}\n"
    ))
    assert paths(found) == {
        "logs.sources[0].path", "logs.sources[1].unit",
        "logs.sources[2].container", "logs.sources[3].service",
        "logs.sources[4].type",
    }


def test_duplicate_source_names_are_flagged_including_derived_ones():
    found = errors(problems(
        "logs:\n  sources:\n"
        "    - {type: docker_container, container: app}\n"
        "    - {type: docker_container, name: app, container: other}\n"
    ))
    assert paths(found) == {"logs.sources[1].name"}


def test_log_level_typo_is_a_warning():
    found = problems("logs:\n  level: erorr\n")
    assert any(p.path == "logs.level" and p.level == "warning" for p in found)


def test_quiet_hours_validation():
    assert not errors(problems("alerts:\n  quiet_hours: {start: '23:00', end: '07:00'}\n"))
    assert errors(problems("alerts:\n  quiet_hours: {start: '25:00', end: '07:00'}\n"))
    assert errors(problems("alerts:\n  quiet_hours: {start: '08:00', end: '08:00'}\n"))
    assert errors(problems("alerts:\n  quiet_hours: {start: '08:00'}\n"))
    assert errors(problems("alerts:\n  quiet_hours: nightly\n"))
    assert not errors(problems("alerts:\n  quiet_hours: false\n"))


def test_unquoted_yaml_times_are_understood():
    # PyYAML turns an unquoted 23:00 into the integer 1380. It must still
    # mean 23:00 — and validate and parse have to agree on that.
    text = "alerts:\n  quiet_hours: {start: 23:00, end: 07:00}\n"
    assert not errors(problems(text))
    quiet = parse(yaml.safe_load(text)).alerts.quiet
    assert (quiet.start, quiet.end) == (23 * 60, 7 * 60)


def test_alert_number_ranges():
    assert errors(problems("alerts:\n  confirm: 0\n"))
    assert errors(problems("alerts:\n  confirm: 1.5\n"))
    assert errors(problems("alerts:\n  hysteresis: -1\n"))
    assert errors(problems("alerts:\n  remind_after: -5\n"))
    assert not errors(problems("alerts:\n  confirm: 1\n  remind_after: 0\n  group_window: 0\n"))


def test_missing_telegram_is_only_a_warning():
    found = validate({})
    assert any(p.path == "telegram" and p.level == "warning" for p in found)
    assert errors(found) == []


def test_whatever_validate_passes_the_parser_accepts():
    # The reload path trusts that "no errors" means "parse will not raise".
    for text in (
        "checks:\n  cpu: {warn: , crit: 95}\n  disks: ['/', {path: /var}]\n",
        "logs:\n  sources: [{type: file, path: /x}]\n",
        "alerts:\n  quiet_hours: {start: '22:00', end: '06:30'}\n",
        "report:\n  interval: 60\n",
    ):
        raw = yaml.safe_load(text)
        assert errors(validate(raw)) == []
        parse(raw)


def test_telegram_admins_validation():
    assert not errors(problems("telegram: {admins: [123, '456']}\n"))
    assert errors(problems("telegram: {admins: nobody}\n"))
    assert errors(problems("telegram: {admins: ['@alice']}\n"))
    assert errors(problems("telegram: {admins: [true]}\n"))


def test_a_group_chat_without_admins_gets_a_warning():
    text = "telegram: {bot_token: t, chat_id: '-1001234'}\n"
    assert any(p.path == "telegram.admins" and p.level == "warning" for p in problems(text))
    assert not any(p.path == "telegram.admins" for p in problems(
        "telegram: {bot_token: t, chat_id: '-1001234', admins: [1]}\n"))
    assert not any(p.path == "telegram.admins" for p in problems(
        "telegram: {bot_token: t, chat_id: '-1001234', interactive: false}\n"))
    assert not any(p.path == "telegram.admins" for p in problems(
        "telegram: {bot_token: t, chat_id: '5551234'}\n"))


def test_interactive_and_admins_are_known_keys():
    assert not any("unknown key" in p.message
                   for p in problems("telegram: {interactive: true, admins: []}\n"))
