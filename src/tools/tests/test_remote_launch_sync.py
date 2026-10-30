import subprocess

from agent_tools.host_cmd import ChairHead, sync_argvs
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import ssh_argv, sync_argv
from agent_tools.remote_launch import LaunchError, _local_head, launch_on_host

HOST = LaneHost("box2", "me@box2", "/ws")
TAG = ChairHead("a" * 40)
BRANCH = ChairHead("b" * 40, "main")
HEADS = {"/h": TAG, "/r": ChairHead("c" * 40)}


def _launch(heads, codes=None, **kwargs):
    calls, codes = [], iter(codes or [])

    def run(argv):
        calls.append(argv)
        return next(codes, 0)

    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", run, repo="/r", harness_dir="/h", chair_head=heads.get, **kwargs
    )
    return result, calls


def _remote(path, head):
    return [ssh_argv(HOST.ssh, argv) for argv in sync_argvs(path, head)]


def test_a_host_harness_at_the_chairs_release_tag_is_not_moved_off_it():
    result, calls = _launch(HEADS)
    assert isinstance(result, dict)
    harness = [c[-1] for c in calls if c[-1].startswith("git -C /h")]
    assert harness == [
        "git -C /h fetch --tags origin",
        f"git -C /h checkout --detach {TAG.sha}",
    ]
    assert not any("merge" in c[-1] or "pull" in c[-1] for c in calls)


def test_a_host_behind_the_chairs_branch_gets_the_commands_that_reach_its_commit():
    result, calls = _launch({"/h": BRANCH, "/r": BRANCH})
    assert isinstance(result, dict)
    assert calls[1:4] == _remote("/h", BRANCH)
    assert calls[1:4][2][-1] == f"git -C /h merge --ff-only {BRANCH.sha}"


def test_a_detached_chair_repo_makes_the_repo_sync_use_the_tag_form():
    result, calls = _launch({"/h": BRANCH, "/r": TAG})
    assert isinstance(result, dict)
    assert calls[4:6] == _remote("/r", TAG)
    assert calls[5][-1] == f"git -C /r checkout --detach {TAG.sha}"


def test_a_chair_repo_on_a_branch_or_unreadable_keeps_the_default_branch_sync():
    for repo_head in (BRANCH, None):
        result, calls = _launch({"/h": TAG, "/r": repo_head})
        assert isinstance(result, dict)
        assert calls[3] == ssh_argv(HOST.ssh, sync_argv("/r")) and len(calls) == 5


def test_a_host_checkout_at_the_release_tag_is_still_at_it_after_the_launch(tmp_path):
    def git(path, *args):
        done = subprocess.run(["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t", *args], capture_output=True, text=True)
        return done.stdout.strip()

    origin, host = tmp_path / "origin", tmp_path / "host"
    subprocess.run(["git", "init", "-q", "-b", "main", str(origin)], check=True)
    git(origin, "commit", "-q", "--allow-empty", "-m", "one")
    git(origin, "tag", "v1")
    git(origin, "commit", "-q", "--allow-empty", "-m", "two")
    subprocess.run(["git", "clone", "-q", str(origin), str(host)], check=True)
    git(host, "checkout", "-q", "--detach", "v1")
    tagged = git(host, "rev-parse", "HEAD")

    def run(argv):
        if argv[-1].startswith("git -C"):
            return subprocess.run(["sh", "-c", argv[-1]]).returncode
        return 0

    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", run, harness_dir=str(host), chair_base=lambda path: None, chair_head=lambda path: ChairHead(tagged)
    )
    assert isinstance(result, dict)
    assert git(host, "rev-parse", "HEAD") == tagged and git(host, "branch", "--show-current") == ""


def test_the_repo_head_is_read_from_the_chair_side_path():
    home, seen = "/".join(("", "home", "me")), []
    launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", lambda argv: 0, repo="~/repos/app", home=home, chair_head=seen.append
    )
    assert seen == [f"{home}/repos/app"]


def test_a_failing_step_in_the_harness_sync_stops_the_rest_of_it():
    result, calls = _launch(HEADS, codes=[0, 1])
    assert isinstance(result, LaunchError) and result.step == "harness"
    assert calls[1:] == _remote("/h", TAG)[:1]


def test_a_failing_repo_sync_step_is_a_sync_error():
    result, calls = _launch(HEADS, codes=[0, 0, 0, 1])
    assert isinstance(result, LaunchError) and result.step == "sync"
    assert calls[-1] == _remote("/r", HEADS["/r"])[0]


def test_local_head_reads_a_branch_a_detached_checkout_and_a_missing_path(tmp_path):
    def git(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "x")
    on_branch = _local_head(str(tmp_path))
    assert on_branch is not None and on_branch.branch == "main" and len(on_branch.sha) == 40
    git("checkout", "--detach")
    assert _local_head(str(tmp_path)) == ChairHead(on_branch.sha, None)
    assert _local_head(str(tmp_path / "missing")) is None
