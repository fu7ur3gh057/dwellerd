"""Check behaviour, including the docker classifier, which decides whether
a container state is worth waking someone up for."""
import pytest

from dwellerd.checks.base import by_threshold
from dwellerd.checks.docker import _classify, _parse_json_lines
from dwellerd.checks.host import CpuCheck, DiskCheck, LoadCheck, SwapCheck


def test_threshold_ladder():
    assert by_threshold(pct=50, warn=80, crit=90, kind="cpu", label="CPU").level == "ok"
    assert by_threshold(pct=85, warn=80, crit=90, kind="cpu", label="CPU").level == "warn"
    assert by_threshold(pct=95, warn=80, crit=90, kind="cpu", label="CPU").level == "crit"


def test_threshold_boundary_is_inclusive():
    assert by_threshold(pct=80, warn=80, crit=90, kind="cpu", label="CPU").level == "warn"
    assert by_threshold(pct=90, warn=80, crit=90, kind="cpu", label="CPU").level == "crit"


def test_crit_result_reports_the_crit_threshold():
    result = by_threshold(pct=95, warn=80, crit=90, kind="cpu", label="CPU")
    assert result.metrics["threshold"] == 90


async def test_cpu_check_runs():
    result = await CpuCheck("cpu", 60, 80, 90).run()
    assert result.kind == "cpu" and 0 <= result.metrics["value"] <= 100


async def test_load_check_normalises_per_core():
    result = await LoadCheck("load", 60, 2, 4).run()
    assert result.metrics["per_core"] == pytest.approx(
        result.metrics["load1"] / result.metrics["cores"]
    )


async def test_missing_disk_is_crit_not_a_crash():
    result = await DiskCheck("d", 60, "/definitely/not/here", 80, 90).run()
    assert result.level == "crit"


async def test_absent_swap_is_ok(monkeypatch):
    import psutil
    fake = type("S", (), {"total": 0, "used": 0, "percent": 0.0})()
    monkeypatch.setattr(psutil, "swap_memory", lambda: fake)
    result = await SwapCheck("swap", 60, 50, 80).run()
    assert result.level == "ok" and "no swap" in result.detail


@pytest.mark.parametrize("state,status,expected", [
    ("running", "Up 2 hours", "ok"),
    ("running", "Up 2 hours (healthy)", "ok"),
    ("running", "Up 2 hours (unhealthy)", "crit"),
    ("running", "Up 5 seconds (health: starting)", "warn"),
    ("restarting", "Restarting (1) 3 seconds ago", "warn"),
    ("exited", "Exited (137) 2 minutes ago", "crit"),
    ("dead", "Dead", "crit"),
    ("paused", "Paused", "warn"),
])
def test_container_classification(state, status, expected):
    assert _classify(state, status)[0] == expected


def test_parses_both_docker_json_shapes():
    assert _parse_json_lines('{"Names":"a"}\n{"Names":"b"}') == \
           [{"Names": "a"}, {"Names": "b"}]
    assert _parse_json_lines('[{"Names":"a"}]') == [{"Names": "a"}]
    assert _parse_json_lines("") == []


def test_malformed_docker_output_is_skipped_not_fatal():
    assert _parse_json_lines('{"Names":"a"}\nnot json\n{"Names":"b"}') == \
           [{"Names": "a"}, {"Names": "b"}]
