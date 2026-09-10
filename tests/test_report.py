from types import SimpleNamespace

from dwellerd.report import ReportBuilder


def test_report_header_has_hostname_but_no_redundant_timestamp():
    cfg = SimpleNamespace(
        telegram=SimpleNamespace(lang="ru"),
        hostname="box-1",
        report=SimpleNamespace(disks=["/"], interfaces=[]),
        checks=SimpleNamespace(
            memory=SimpleNamespace(warn=80),
            docker=SimpleNamespace(enabled=False, containers=[]),
        ),
    )

    header = ReportBuilder(cfg, storage=None)._header()

    assert "Системный отчёт" in header
    assert "<i>box-1</i>" in header
    assert " · " not in header
