from dwellerd.wizard import probe
from dwellerd.wizard.ui import _number_selection


def test_number_selection_fallback():
    options = ["one", "two", "three"]
    assert _number_selection("1 3", options) == ["one", "three"]
    assert _number_selection("2,2,99", options) == ["two"]
    assert _number_selection("all", options) == options
    assert _number_selection("", options) == []


def test_disk_space_returns_gib_and_percent(monkeypatch):
    usage = type("Usage", (), {
        "total": 100 * 1024 ** 3,
        "used": 35 * 1024 ** 3,
        "free": 65 * 1024 ** 3,
    })()
    monkeypatch.setattr(probe.shutil, "disk_usage", lambda path: usage)

    result = probe.disk_space("/")

    assert result == {"total": 100.0, "used": 35.0, "free": 65.0, "percent": 35.0}


def test_disk_space_failure_is_nonfatal(monkeypatch):
    def fail(path):
        raise OSError("gone")

    monkeypatch.setattr(probe.shutil, "disk_usage", fail)
    assert probe.disk_space("/gone") is None
