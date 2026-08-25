"""A config with nothing but credentials must still be a working config."""
from dwellerd.checks import build_checks
from dwellerd.config import parse


def test_empty_config_gets_working_defaults():
    cfg = parse({})
    assert cfg.checks.interval == 60
    assert cfg.checks.cpu.enabled and cfg.checks.cpu.warn == 80
    assert [d.path for d in cfg.checks.disks] == ["/"]
    assert cfg.logs.enabled and cfg.report.enabled
    assert not cfg.checks.docker.enabled


def test_minimal_config_builds_checks():
    cfg = parse({"telegram": {"bot_token": "t", "chat_id": "c"}})
    assert cfg.telegram.configured
    names = [c.name for c in build_checks(cfg)]
    assert names == ["cpu", "memory", "swap", "load", "disk-root"]


def test_a_check_can_be_switched_off_with_a_bare_false():
    cfg = parse({"checks": {"cpu": False, "swap": False, "load": False}})
    assert [c.name for c in build_checks(cfg)] == ["memory", "disk-root"]


def test_explicit_null_falls_back_to_the_default():
    # `logs:` with nothing under it is a YAML null, not an empty dict.
    cfg = parse({"logs": None, "checks": None, "report": None})
    assert cfg.logs.enabled
    assert cfg.checks.interval == 60


def test_disk_shorthand_accepts_a_bare_path():
    cfg = parse({"checks": {"disks": ["/", "/var"]}})
    assert [d.path for d in cfg.checks.disks] == ["/", "/var"]
    assert [c.name for c in build_checks(cfg) if c.kind == "disk"] == \
           ["disk-root", "disk-var"]


def test_thresholds_are_per_disk():
    cfg = parse({"checks": {"disks": [{"path": "/data", "warn": 60, "crit": 70}]}})
    disk = cfg.checks.disks[0]
    assert (disk.warn, disk.crit) == (60, 70)


def test_http_probe_without_a_url_is_dropped():
    cfg = parse({"checks": {"http": [{"name": "broken"}, {"url": "https://x"}]}})
    assert len(cfg.checks.http) == 1


def test_log_source_gets_a_name_derived_from_its_target():
    cfg = parse({"logs": {"sources": [
        {"type": "docker_container", "container": "api"},
        {"type": "journal", "unit": "nginx.service"},
    ]}})
    assert [s.name for s in cfg.logs.sources] == ["api", "nginx.service"]


def test_report_disks_default_to_the_watched_disks():
    cfg = parse({"checks": {"disks": ["/", "/srv"]}})
    assert cfg.report.disks == ["/", "/srv"]


def test_docker_check_is_built_when_enabled():
    cfg = parse({"checks": {"docker": {"enabled": True, "containers": ["api"]}}})
    docker = [c for c in build_checks(cfg) if c.kind == "docker"]
    assert len(docker) == 1 and docker[0].containers == ["api"]
