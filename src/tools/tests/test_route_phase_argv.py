from agent_tools import run_store
from agent_tools.remote_argv import launch_argv
from agent_tools.route import harness_argv, next_run_id, phase_run_prefix

PROFILE = {
    "harness_dir": "/h",
    "workspace_dir": "/w",
    "team": "t",
    "cartridges_dir": "/c",
    "provider_profile": "/p.yaml",
    "assume": "a",
}


def _epic(**needs) -> list:
    return harness_argv(PROFILE, "epic", "init-1", initiative="init", repo="/r", **needs)


def test_epic_argv_with_phase_carries_it_once_between_repo_and_workdir():
    argv = _epic(phase="b")
    assert argv.count("--phase") == 1
    assert argv[argv.index("--phase") + 1] == "b"
    assert argv.index("--repo") < argv.index("--phase") < argv.index("--workdir")


def test_epic_argv_without_phase_is_the_argv_minus_the_phase_pair():
    without = _epic()
    assert "--phase" not in without
    with_phase = _epic(phase="b")
    i = with_phase.index("--phase")
    assert with_phase[:i] + with_phase[i + 2:] == without
    assert _epic(phase=None) == without


def test_rescue_argv_ignores_a_phase():
    argv = harness_argv(PROFILE, "rescue", "init-1", initiative="init", repo="/r", task="t1", phase="b")
    assert "--phase" not in argv


def test_remote_launch_argv_with_phase_appends_it_once():
    without = launch_argv("i", "r", "l")
    assert without == [
        "cox", "route", "launch", "epic", "--local",
        "--initiative", "i", "--run-id", "r", "--label", "l", "--no-claim",
    ]
    with_phase = launch_argv("i", "r", "l", phase="b")
    assert with_phase.count("--phase") == 1
    assert with_phase == without + ["--phase", "b"]


def test_phase_run_prefix_joins_with_a_dot():
    assert phase_run_prefix("init", "p1") == "init.p1"


def test_phase_run_id_leases_apart_from_the_whole_run():
    run_id = next_run_id(["init.p1-1.log"], phase_run_prefix("init", "p1"))
    assert run_id == "init.p1-2"
    assert run_store._lease_name(run_id) == "runs:init.p1"
    assert run_store._lease_name(run_id) != run_store._lease_name("init-2")
