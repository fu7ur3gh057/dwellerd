from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_start_limit_directives_are_in_unit_section():
    text = (ROOT / "deploy/dwellerd.service").read_text()
    unit, service = text.split("[Service]", 1)

    assert "StartLimitIntervalSec=60" in unit
    assert "StartLimitBurst=5" in unit
    assert "StartLimitIntervalSec" not in service
    assert "StartLimitBurst" not in service


def test_supplementary_groups_remain_whitespace_separated():
    text = (ROOT / "deploy/install.sh").read_text()

    assert 'SUPP_GROUPS="$SUPP"' in text
    assert 'SUPP_GROUPS="${SUPP// /,}"' not in text


def test_unit_can_be_reloaded_with_sighup():
    # `systemctl reload` must send SIGHUP to the daemon, not just fail with
    # "job type reload is not applicable".
    text = (ROOT / "deploy/dwellerd.service").read_text()
    service = text.split("[Service]", 1)[1]
    assert "ExecReload=/bin/kill -HUP $MAINPID" in service
