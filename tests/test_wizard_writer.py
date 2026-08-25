"""The wizard's output must be a config the daemon can actually load —
this is the one seam where a typo would only surface at runtime."""
import yaml

from dwellerd.checks import build_checks
from dwellerd.config import parse
from dwellerd.wizard import writer

ANSWERS = {
    "lang": "ru",
    "bot_token": "123:ABC",
    "chat_id": "-1001234567890",
    "proxy": "",
    "hostname": "prod-1",
    "interval": 30,
    "warn": 75, "crit": 85,
    "swap_warn": 50, "swap_crit": 80,
    "load_warn": 2, "load_crit": 4,
    "disks": ["/", "/var"],
    "http": [{"name": "site", "url": "https://example.com", "expect_status": 200}],
    "systemd": ["nginx.service"],
    "docker_enabled": True,
    "docker_containers": ["app", "db"],
    "logs_enabled": True,
    "log_level": "error",
    "log_sources": [
        {"type": "docker_container", "name": "app", "container": "app", "pattern": ".+"},
        {"type": "file", "name": "nginx", "path": "/var/log/nginx/error.log",
         "pattern": "(?i)error"},
    ],
    "digest_interval": 1800,
    "retention_days": 7,
    "report_enabled": True,
    "report_interval": 7200,
}


def rendered():
    return parse(yaml.safe_load(writer.render(ANSWERS)))


def test_output_is_valid_yaml():
    assert isinstance(yaml.safe_load(writer.render(ANSWERS)), dict)


def test_answers_survive_the_round_trip():
    cfg = rendered()
    assert cfg.telegram.bot_token == "123:ABC"
    assert cfg.telegram.chat_id == "-1001234567890"
    assert cfg.telegram.lang == "ru"
    assert cfg.hostname == "prod-1"
    assert cfg.checks.interval == 30
    assert cfg.checks.cpu.warn == 75 and cfg.checks.cpu.crit == 85


def test_every_answer_becomes_a_check():
    names = [c.name for c in build_checks(rendered())]
    assert names == [
        "cpu", "memory", "swap", "load",
        "disk-root", "disk-var", "http-site", "unit-nginx", "docker",
    ]


def test_log_sources_survive():
    sources = rendered().logs.sources
    assert [s.type for s in sources] == ["docker_container", "file"]
    assert sources[1].pattern == "(?i)error"


def test_a_minimal_run_still_produces_a_loadable_config():
    minimal = dict(
        ANSWERS,
        http=[], systemd=[], docker_enabled=False, docker_containers=[],
        log_sources=[], logs_enabled=False, report_enabled=False,
    )
    cfg = parse(yaml.safe_load(writer.render(minimal)))
    assert not cfg.logs.enabled and not cfg.report.enabled
    assert [c.name for c in build_checks(cfg)][:4] == ["cpu", "memory", "swap", "load"]


def test_quotes_in_an_answer_do_not_break_the_yaml():
    tricky = dict(ANSWERS, hostname='box "one" \\ two')
    cfg = parse(yaml.safe_load(writer.render(tricky)))
    assert cfg.hostname == 'box "one" \\ two'


def test_config_is_written_with_restrictive_permissions(tmp_path):
    # It holds a bot token: anyone with the file can post as your bot.
    path = tmp_path / "config.yaml"
    writer.write(path, writer.render(ANSWERS))
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_rewrite_backs_up_the_previous_config(tmp_path):
    path = tmp_path / "config.yaml"
    writer.write(path, "hostname: old\n")
    backup = writer.write(path, writer.render(ANSWERS))
    assert backup is not None and "hostname: old" in backup.read_text()
