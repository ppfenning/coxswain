import shutil
import subprocess

import pytest

from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import (
    branch_push_argv,
    epic_refspec,
    launch_argv,
    rsync_check_argv,
    rsync_file_argv,
    rsync_push_argv,
    ssh_argv,
    sync_argv,
)
from agent_tools.remote_lane import remote_record
from agent_tools.remote_launch import (
    LaunchError,
    Relaunch,
    env_preflight,
    first_differing_path,
    launch_on_host,
    launch_plan,
)

needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="rsync is not installed")
SSH_BOUND = ["ssh", "-o", "ConnectTimeout=30", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3"]
RSYNC_BOUND = ["rsync", "-a", "--timeout=30", "-e", "ssh -o ConnectTimeout=30"]


def _fake_run(calls, codes=None):
    codes = codes or {}

    def run(argv):
        calls.append(argv)
        if argv[0] == "rsync" and "rsync" not in codes:
            return subprocess.run(argv).returncode
        return codes.get(argv[0], 0)

    return run


def _setup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "work" / "init" / "p1").mkdir(parents=True)
    (tmp_path / "work" / "init" / "epic.md").write_text("epic")
    (tmp_path / "work" / "init" / "p1" / "t.md").write_text("task")
    remote = tmp_path / "remote"
    (remote / "work").mkdir(parents=True)
    return LaneHost("box", "me@box", str(remote))


@needs_rsync
def test_a_launch_copies_the_initiative_then_starts_the_lane_and_returns_the_record(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    result = launch_on_host(host, "init", "r1", "lbl", "2026-09-25T00:00:00Z", _fake_run(calls), lambda p: p)
    dest = tmp_path / "remote" / "work" / "init"
    assert (dest / "epic.md").read_text() == "epic"
    assert (dest / "p1" / "t.md").read_text() == "task"
    # The remote `route launch` gets the initiative's path in the host's workspace, not a bare id.
    assert calls[-1] == ssh_argv("me@box", launch_argv(f"{tmp_path / 'remote'}/work/init", "r1", "lbl"))
    assert result == remote_record("box", "2026-09-25T00:00:00Z")


def test_a_launch_with_a_repo_returns_a_record_naming_it(tmp_path, monkeypatch):
    host = _setup(tmp_path, monkeypatch)
    result = launch_on_host(host, "init", "r1", "lbl", "t", _fake_run([], {"rsync": 0}), lambda p: p, repo="/r")
    assert result == remote_record("box", "t", "/r")


def test_a_failing_rsync_stops_before_the_ssh_step(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    result = launch_on_host(host, "init", "r1", "lbl", "t", _fake_run(calls, {"rsync": 1}), lambda p: p)
    assert isinstance(result, LaunchError) and result.step == "rsync"
    assert [c[0] for c in calls] == ["rsync"]


def test_a_failing_ssh_names_the_ssh_step(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    result = launch_on_host(host, "init", "r1", "lbl", "t", _fake_run(calls, {"rsync": 0, "ssh": 255}), lambda p: p)
    assert isinstance(result, LaunchError) and result.step == "ssh"
    assert [c[0] for c in calls] == ["rsync", "ssh"]


def test_a_failing_sync_stops_before_the_launch_argv_runs():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    codes = iter([0, 1])

    def run(argv):
        calls.append(argv)
        return next(codes)

    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", run, repo="/r")
    assert result == LaunchError("sync", "updating /r on box2 exited 1: the lane would build on a stale main")
    assert calls == [
        [*RSYNC_BOUND, "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        ssh_argv("me@box2", sync_argv("/r")),
    ]


def test_the_default_location_is_the_ssh_destination_and_path(tmp_path, monkeypatch):
    host, calls = _setup(tmp_path, monkeypatch), []
    launch_on_host(host, "init", "r1", "lbl", "t", _fake_run(calls, {"rsync": 0}))
    assert calls[0][-1] == f"me@box:{tmp_path}/remote/work/init/"


def test_launch_plan_is_the_rsync_argv_then_the_ssh_argv():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l") == [
        [*RSYNC_BOUND, "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        ssh_argv("me@box2", launch_argv("/ws/work/init-x", "init-x-1", "l")),
    ]


def test_launch_plan_without_a_repo_returns_todays_two_argv():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l", repo=None) == launch_plan(host, "init-x", "init-x-1", "l")
    assert len(launch_plan(host, "init-x", "init-x-1", "l", repo=None)) == 2


def test_launch_plan_with_a_repo_returns_three_argv():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l", repo="/r") == [
        [*RSYNC_BOUND, "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        ssh_argv("me@box2", sync_argv("/r")),
        ssh_argv("me@box2", launch_argv("/ws/work/init-x", "init-x-1", "l")),
    ]


def test_launch_plan_with_a_harness_dir_syncs_it_before_the_repo():
    host = LaneHost("box2", "me@box2", "/ws")
    plan = launch_plan(host, "init-x", "init-x-1", "l", repo="/r", harness_dir="/h")
    assert plan[1:3] == [ssh_argv("me@box2", sync_argv("/h")), ssh_argv("me@box2", sync_argv("/r"))]
    assert len(plan) == 4


def test_launch_plan_without_a_harness_dir_is_todays_list():
    host = LaneHost("box2", "me@box2", "/ws")
    assert launch_plan(host, "init-x", "init-x-1", "l", repo="/r", harness_dir=None) == launch_plan(
        host, "init-x", "init-x-1", "l", repo="/r"
    )
    assert len(launch_plan(host, "init-x", "init-x-1", "l", harness_dir=None)) == 2


def test_a_failing_harness_sync_stops_before_the_repo_sync_and_the_launch():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    codes = iter([0, 1])

    def run(argv):
        calls.append(argv)
        return next(codes)

    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", run, repo="/r", harness_dir="/h")
    assert result == LaunchError(
        "harness",
        "updating the harness at /h on box2 exited 1: the lane would run an older harness than the store",
    )
    assert calls == [
        [*RSYNC_BOUND, "--delete", "work/init-x/", "me@box2:/ws/work/init-x/"],
        ssh_argv("me@box2", sync_argv("/h")),
    ]


def test_a_passing_harness_sync_reaches_the_launch_argv():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0, harness_dir="/h")
    assert calls == launch_plan(host, "init-x", "init-x-1", "l", harness_dir="/h")
    assert calls[1] == ssh_argv("me@box2", sync_argv("/h")) and "route launch epic" in calls[-1][-1]
    assert result == remote_record("box2", "t")


def test_launch_on_host_runs_the_planned_argvs_in_order():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0)
    assert calls == launch_plan(host, "init-x", "init-x-1", "l")
    assert result == remote_record("box2", "t")


def test_a_preflight_refusal_returns_the_auth_error_before_anything_runs():
    host, calls = LaneHost("box2", "me@box2", "/ws"), []
    line = "claude auth: not logged in on the host (run claude auth login there)"
    result = launch_on_host(host, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0, preflight=line)
    assert result == LaunchError("auth", line)
    assert calls == []


def test_env_preflight_names_the_first_empty_variable_and_is_none_when_all_are_set():
    assert env_preflight(("MY_API_KEY",), {"MY_API_KEY": (1, "")}) == "env vars: MY_API_KEY is not set on the host (set it there)"
    assert env_preflight(("MY_API_KEY",), {"MY_API_KEY": (0, "sk-1\n")}) is None


def test_env_preflight_reports_a_failed_ssh_and_an_empty_name_list_without_calling_them_unset():
    assert env_preflight(("K",), {"K": (255, "ssh: Connection refused\n")}) == "env var check failed on the host: ssh: Connection refused"
    assert env_preflight((), {}) == "no auth_env or endpoint_env configured: nothing to check on the host"


REPO = "/home/user/repos/proj"
EPIC_PUSH = [
    "git", "-C", "/home/user/repos/proj", "push", "--force", "ssh://me@box/home/user/repos/proj",
    "refs/heads/epic/init/*:refs/heads/epic/init/*",
]
AGENT_PUSH = [
    "git", "-C", "/home/user/repos/proj", "push", "--force", "ssh://me@box/home/user/repos/proj",
    "refs/heads/agents/init-1/t1:refs/heads/agents/init-1/t1",
]


def test_a_relocated_launch_plan_pushes_the_branches_before_the_launch_step():
    host = LaneHost("box", "me@box", "/ws")
    plan = launch_plan(host, "init", "r2", "lbl", None, REPO, None, Relaunch("init-1", ("t1",)))
    assert plan[1] == ssh_argv("me@box", sync_argv(REPO))
    assert plan[2:4] == [EPIC_PUSH, AGENT_PUSH]
    assert plan[-1] == ssh_argv("me@box", launch_argv("/ws/work/init", "r2", "lbl"))
    assert len(plan) == 5


def test_a_plain_launch_plan_has_no_push_step():
    host = LaneHost("box", "me@box", "/ws")
    plan = launch_plan(host, "init", "r2", "lbl", None, REPO)
    assert [argv[0] for argv in plan] == ["rsync", "ssh", "ssh"]
    assert plan == launch_plan(host, "init", "r2", "lbl", None, REPO, None, None)


def test_a_failed_push_returns_a_launch_error_and_never_runs_the_launch_step():
    host, calls = LaneHost("box", "me@box", "/ws"), []

    def run(argv):
        calls.append(argv)
        return 1 if argv[0] == "git" else 0

    result = launch_on_host(host, "init", "r2", "lbl", "t", run, lambda p: p, REPO, None, Relaunch("init-1", ("t1",)))
    assert isinstance(result, LaunchError)
    assert result.step == "push"
    assert "box" in result.message and "epic/init/*" in result.message
    assert calls[-1] == EPIC_PUSH
    assert ssh_argv("me@box", launch_argv("/ws/work/init", "r2", "lbl")) not in calls


def test_a_failed_agents_push_names_that_branch_and_never_runs_the_launch_step():
    host, calls = LaneHost("box", "me@box", "/ws"), []

    def run(argv):
        calls.append(argv)
        return 128 if argv[-1].startswith("refs/heads/agents/") else 0

    relaunch = Relaunch("init-1", ("t1", "t2"))
    result = launch_on_host(host, "init", "r2", "lbl", "t", run, lambda p: p, REPO, None, relaunch)
    assert result == LaunchError(
        "push", "pushing agents/init-1/t1 to box exited 128: the lane would not have the initiative's branches"
    )
    assert calls[-2:] == [EPIC_PUSH, AGENT_PUSH]


def test_a_relaunch_pushes_from_the_chairs_checkout_when_it_differs_from_the_hosts_repo():
    host = LaneHost("box", "me@box", "/ws")
    plan = launch_plan(host, "init", "r2", "lbl", None, REPO, None, Relaunch("init-1", (), "/srv/chair/proj"))
    assert plan[2] == [
        "git", "-C", "/srv/chair/proj", "push", "--force", "ssh://me@box/home/user/repos/proj",
        "refs/heads/epic/init/*:refs/heads/epic/init/*",
    ]


def test_a_relaunch_without_a_repo_is_refused_before_anything_runs():
    host, calls = LaneHost("box", "me@box", "/ws"), []
    result = launch_on_host(host, "init", "r2", "lbl", "t", lambda argv: calls.append(argv) or 0, relaunch=Relaunch("init-1"))
    assert result == LaunchError("push", "a relocated relaunch to box has no repo to push epic/init/* into: nothing was run")
    assert calls == []


MAC_HOME = "/".join(("", "Users", "me"))
LINUX_HOME = "/".join(("", "home", "lane"))
INSTALLS = ".local/share/coxswain/installs"


def _under(root, *parts):
    return "/".join((root, *parts))


def _harness_plan(harness_dir, host_checkouts=(), home=None):
    host = LaneHost("box2", "me@box2", "/ws")
    return launch_plan(
        host, "init-x", "init-x-1", "l", harness_dir=harness_dir, home=home, host_checkouts=host_checkouts
    )


def test_a_chair_harness_under_installs_syncs_the_host_checkout_under_repos():
    checkout = _under(LINUX_HOME, "repos", "coxswain-graphs")
    plan = _harness_plan(_under(MAC_HOME, INSTALLS, "coxswain-graphs"), [checkout], LINUX_HOME)
    assert plan[1] == ssh_argv("me@box2", sync_argv(checkout))


def test_a_chair_harness_under_repos_syncs_the_host_checkout_under_installs():
    checkout = _under(LINUX_HOME, INSTALLS, "coxswain-graphs")
    plan = _harness_plan(_under(MAC_HOME, "repos", "coxswain-graphs"), [checkout], LINUX_HOME)
    assert plan[1] == ssh_argv("me@box2", sync_argv(checkout))


def test_a_harness_without_a_host_checkout_syncs_the_home_relative_form():
    plan = _harness_plan(_under(MAC_HOME, INSTALLS, "coxswain-graphs"), (), LINUX_HOME)
    assert plan[1] == ssh_argv("me@box2", sync_argv(_under(INSTALLS, "coxswain-graphs")))


def test_a_harness_without_checkouts_or_home_is_synced_unchanged():
    plan = _harness_plan("/h")
    assert plan == launch_plan(LaneHost("box2", "me@box2", "/ws"), "init-x", "init-x-1", "l", harness_dir="/h")
    assert plan[1] == ssh_argv("me@box2", sync_argv("/h"))


def test_a_checkout_of_another_name_is_not_used():
    plan = _harness_plan("/h", [_under(LINUX_HOME, "repos", "other")], LINUX_HOME)
    assert plan[1] == ssh_argv("me@box2", sync_argv("/h"))


def test_a_repo_with_a_host_checkout_is_synced_and_pushed_at_that_path():
    host = LaneHost("box2", "me@box2", "/ws")
    repo = _under(MAC_HOME, "repos", "coxswain-tools")
    checkout = _under(LINUX_HOME, INSTALLS, "coxswain-tools")
    plan = launch_plan(
        host, "init-x", "init-x-1", "l", repo=repo, relaunch=Relaunch("old"), home=MAC_HOME, host_checkouts=[checkout]
    )
    assert plan[1] == ssh_argv("me@box2", sync_argv(checkout))
    assert plan[2] == branch_push_argv(repo, "me@box2", checkout, epic_refspec("init-x"))
    assert len(plan) == 4


def test_a_failing_harness_sync_names_the_path_it_synced():
    host = LaneHost("box2", "me@box2", "/ws")
    checkout = _under(LINUX_HOME, "repos", "coxswain-graphs")
    chair_dir = _under(MAC_HOME, INSTALLS, "coxswain-graphs")
    codes = iter([0, 1])
    result = launch_on_host(
        host, "init-x", "init-x-1", "l", "t", lambda argv: next(codes), harness_dir=chair_dir, host_checkouts=[checkout]
    )
    assert isinstance(result, LaunchError) and result.step == "harness"
    assert f"updating the harness at {checkout} on box2" in result.message
    assert chair_dir not in result.message


CART_HOST = LaneHost("box2", "me@box2", "/ws")
CART_PUSH = rsync_push_argv("cartridges", "me@box2:/ws/cartridges", mirror=True)
CART_CHECK = rsync_check_argv("cartridges", "me@box2:/ws/cartridges")
OTHER_CARTRIDGE = "a cartridge other than the chair's"
UNGUARDED = {"chair_head": lambda path: None, "chair_base": lambda path: None}
FULL = {"repo": "/r", "harness_dir": "/h", "relaunch": Relaunch("init-x-0", ("t1",)), "cartridges": "cartridges"}


def _full_plan():
    return launch_plan(CART_HOST, "init-x", "init-x-1", "l", **FULL)


def test_an_epic_plan_with_cartridges_pushes_then_checks_them_ahead_of_the_harness_repo_and_launch():
    plan = _full_plan()
    assert plan[1:3] == [CART_PUSH, CART_CHECK]
    assert plan[3] == ssh_argv("me@box2", sync_argv("/h"))
    assert plan[4] == ssh_argv("me@box2", sync_argv("/r"))
    assert plan[5][0] == "git" and "route launch epic" in plan[-1][-1]


def test_a_decompose_plan_with_cartridges_pushes_then_checks_them_after_the_intake_copy():
    plan = launch_plan(CART_HOST, "i", "i-1", "l", graph="decompose", intake="work/idea.md", cartridges="cartridges")
    assert plan[0] == rsync_file_argv("work/idea.md", "me@box2:/ws")
    assert plan[1:3] == [CART_PUSH, CART_CHECK]
    assert len(plan) == 4 and "route launch decompose" in plan[-1][-1]


def test_a_cartridge_that_differs_refuses_the_launch_before_the_harness_repo_push_and_ssh_steps():
    calls, probed = [], []

    def probe(argv):
        probed.append(argv)
        return 0, ">f.st...... cartridges/base/cartridge.yaml\n"

    result = launch_on_host(
        CART_HOST, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0, probe=probe, **UNGUARDED, **FULL
    )
    assert isinstance(result, LaunchError) and result.step == "cartridges"
    assert "box2" in result.message and "cartridges/base/cartridge.yaml" in result.message
    assert OTHER_CARTRIDGE in result.message
    assert calls == _full_plan()[:2] and probed == [CART_CHECK]


def test_a_failing_cartridge_check_with_an_ssh_error_reports_the_exit_not_a_differing_file():
    error = "ssh: connect to host box2 port 22: Connection refused\n"
    result = launch_on_host(
        CART_HOST, "init-x", "init-x-1", "l", "t", lambda argv: 0, probe=lambda argv: (255, error), cartridges="cartridges"
    )
    assert isinstance(result, LaunchError) and result.step == "cartridges"
    assert "box2" in result.message and "exited 255" in result.message
    assert "differs at" not in result.message and OTHER_CARTRIDGE in result.message


def test_a_failing_cartridge_push_refuses_and_runs_nothing_after_it():
    calls, probed = [], []

    def run(argv):
        calls.append(argv)
        return 1 if argv == CART_PUSH else 0

    result = launch_on_host(
        CART_HOST, "init-x", "init-x-1", "l", "t", run, probe=lambda argv: probed.append(argv) or (0, ""), **UNGUARDED, **FULL
    )
    assert isinstance(result, LaunchError) and result.step == "cartridges"
    assert "box2" in result.message and "exited 1" in result.message
    assert calls == _full_plan()[:2] and probed == []


def test_a_matching_cartridge_check_runs_the_rest_of_the_plan_at_the_shifted_indexes():
    calls = []
    result = launch_on_host(
        CART_HOST, "init-x", "init-x-1", "l", "t", lambda argv: calls.append(argv) or 0, probe=lambda argv: (0, ""),
        **UNGUARDED, **FULL,
    )
    assert calls == [argv for argv in _full_plan() if argv != CART_CHECK]
    assert result == remote_record("box2", "t", "/r")


def test_first_differing_path_reads_only_itemized_records():
    assert first_differing_path(">f.st...... cartridges/a.yaml\n>f+++++++++ cartridges/b.yaml\n") == "cartridges/a.yaml"
    assert first_differing_path("\n*deleting   cartridges/old.yaml\n") == "cartridges/old.yaml"
    assert first_differing_path("ssh: connect to host box2 port 22: Connection refused\n") is None
    assert first_differing_path("Warning: Permanently added 'box2'\n.d..t...... cartridges/\n") == "cartridges/"
    assert first_differing_path("") is None
