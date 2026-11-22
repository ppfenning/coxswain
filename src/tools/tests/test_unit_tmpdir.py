"""The TMPDIR every Coxswain service unit gives its lanes: rendered into each unit and plist, and added to a unit
installed before it by `with_tmpdir`, which a second run leaves as it is."""

from __future__ import annotations

from agent_tools import chair_queue_units, chair_service, lander_cli, unit_tmpdir

TMP = "/var/tmp/coxswain-alice"
LINES = f"Environment=TMPDIR={TMP}\nExecStartPre=-mkdir -p -m 0700 {TMP}\n"
INPUTS = chair_queue_units.UnitInputs("/opt/bin/cox", "/srv/ws", "chair", "/srv/profile.yaml", "/opt/bin:/usr/bin",
                                      None, TMP)


def _rendered() -> dict[str, str]:
    return {
        "chair": chair_service.unit_text("/opt/bin/cox", "/srv/ws", "chair", 60, None, "/srv/p.yaml", "/usr/bin", TMP),
        "queue": chair_queue_units.template_text(INPUTS),
        "lander": lander_cli.unit_text("/opt/bin/cox", "/srv/ws", 30, "/srv/p.yaml", "/usr/bin", None, TMP),
    }


def test_the_default_is_per_user_under_var_tmp_and_the_profile_overrides_it():
    assert unit_tmpdir.tmpdir_for({}, "alice") == TMP
    assert unit_tmpdir.tmpdir_for({"lane_tmpdir": " /srv/scratch/ "}, "alice") == "/srv/scratch"
    assert unit_tmpdir.tmpdir_for({"lane_tmpdir": ""}, "") == "/var/tmp/coxswain-operator"


def test_every_unit_that_runs_lane_work_sets_tmpdir_and_makes_it_before_each_start():
    for name, text in _rendered().items():
        service = text.split("[Service]\n", 1)[1].split("\n\n", 1)[0] + "\n"
        assert service.endswith(LINES), name
        assert unit_tmpdir.effective_tmpdir(text) == (TMP, "unit"), name


def test_a_unit_rendered_without_a_tmpdir_has_none():
    text = chair_service.unit_text("/opt/bin/cox", "/srv/ws", "chair", 60, None)
    assert "TMPDIR" not in text and unit_tmpdir.effective_tmpdir(text) is None


def test_the_upgrade_leaves_a_freshly_rendered_unit_exactly_as_it_is():
    """The upgrade's refresh is `with_kill_mode` on the chair's units, then `with_tmpdir` on every one."""
    rendered = _rendered()
    for name, text in rendered.items():
        assert unit_tmpdir.with_tmpdir(text, TMP) == text, name
    for name in ("chair", "queue"):
        assert chair_service.with_kill_mode(rendered[name]) == rendered[name], name


def test_a_unit_installed_before_tmpdir_gets_it_once_and_an_old_value_is_replaced():
    legacy = "[Service]\nExecStart=/c lander run\nRestart=on-failure\n\n[Install]\nWantedBy=default.target\n"
    once = unit_tmpdir.with_tmpdir(legacy, TMP)
    assert once == f"[Service]\nExecStart=/c lander run\nRestart=on-failure\n{LINES}\n[Install]\nWantedBy=default.target\n"
    assert unit_tmpdir.with_tmpdir(once, TMP) == once
    moved = unit_tmpdir.with_tmpdir(once, "/srv/scratch")
    assert moved.count("TMPDIR") == 1 and "Environment=TMPDIR=/srv/scratch\n" in moved and TMP not in moved
    assert unit_tmpdir.with_tmpdir("[Timer]\nOnBootSec=5min\n", TMP) == "[Timer]\nOnBootSec=5min\n"


def test_a_drop_in_overrides_the_unit_and_the_last_drop_in_by_name_wins():
    unit = _rendered()["lander"]
    drop_ins = [("tmpdir.conf", "[Service]\nEnvironment=TMPDIR=/var/tmp/cox-lane\n"), ("a.conf", "[Service]\nNice=5\n")]
    assert unit_tmpdir.effective_tmpdir(unit, drop_ins) == ("/var/tmp/cox-lane", "tmpdir.conf")
    assert unit_tmpdir.effective_tmpdir(None, [("z.conf", '[Service]\nEnvironment="TMPDIR=/x"\n')]) == ("/x", "z.conf")


def test_the_launchd_agents_carry_tmpdir_in_their_environment():
    chair = chair_service.chair_plist_text("/opt/bin/cox", "/srv/ws", "chair", 60, "/usr/bin", "/srv/p.yaml", None, TMP)
    queue = chair_queue_units.plist_text(INPUTS, "launch")
    for text in (chair, queue):
        assert f"<key>TMPDIR</key>\n    <string>{TMP}</string>" in text
        assert unit_tmpdir.effective_tmpdir(text) == (TMP, "unit")


def test_the_profile_a_unit_pins_is_read_from_its_exec_start():
    assert unit_tmpdir.profile_arg(_rendered()["lander"]) == "/srv/p.yaml"
    assert unit_tmpdir.profile_arg("[Service]\nExecStart=/c chair run\n") is None
