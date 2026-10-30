import json
import os
import subprocess
from pathlib import Path

import pytest

from agent_tools import cli
from agent_tools.host_cmd import ChairHead
from agent_tools.land import behind_warning
from agent_tools.lane_hosts import LaneHost
from agent_tools.remote_argv import base_sync_argv, ssh_argv
from agent_tools.remote_lane import parse_remote_record, remote_record
from agent_tools.remote_launch import ChairBase, LaunchError, _chair_base, base_refusal, launch_on_host, launch_plan

HOST = LaneHost("box2", "me@box2", "/ws")
CHAIR = "c" * 40


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _repos(tmp_path, monkeypatch):
    """A clone detached at the first commit, as an earlier release-tag sync leaves it, and origin two commits ahead."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for key in ("GIT_AUTHOR", "GIT_COMMITTER"):
        monkeypatch.setenv(f"{key}_NAME", "t")
        monkeypatch.setenv(f"{key}_EMAIL", "t@t")
    origin, seed, clone = tmp_path / "origin.git", tmp_path / "seed", tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "clone", "-q", str(origin), str(seed))
    for n in ("1", "2"):
        (seed / "f").write_text(n)
        _git(seed, "add", "f")
        _git(seed, "commit", "-qm", n)
        _git(seed, "push", "-q", "origin", "main")
        if n == "1":
            _git(tmp_path, "clone", "-q", str(origin), str(clone))
    _git(clone, "checkout", "-q", "--detach", "HEAD")
    return clone, seed


def _on_host(repo: Path, sha: str, cwd: Path) -> subprocess.CompletedProcess:
    """Runs the command string ssh would hand the remote login shell."""
    remote = ssh_argv("host", base_sync_argv(str(repo), "main", sha))[-1]
    return subprocess.run(["sh", "-c", remote], cwd=cwd, capture_output=True, text=True)


def test_a_host_checkout_detached_and_behind_is_fast_forwarded_to_the_chairs_commit(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    want = _git(seed, "rev-parse", "HEAD")
    done = _on_host(clone, want, tmp_path)
    assert (done.returncode, done.stdout.strip()) == (0, f"head {want}")
    assert (_git(clone, "rev-parse", "HEAD"), _git(clone, "branch", "--show-current")) == (want, "main")


def test_a_commit_the_host_origin_lacks_exits_1_with_the_host_head_and_one_reason(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    before = _git(clone, "rev-parse", "HEAD")
    done = _on_host(clone, CHAIR, tmp_path)
    assert (done.returncode, done.stdout.strip(), done.stderr.strip()) == (
        1, f"head {before}", f"sync: main cannot fast-forward to {CHAIR}"
    )


AT_CHAIR = ChairBase("main", CHAIR)


def _launch(output, code=0, base=AT_CHAIR):
    calls, probed = [], []

    def run(argv):
        calls.append(argv)
        return 0

    def probe(argv):
        probed.append(argv)
        return code, output

    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", run, repo="/r",
        chair_head=lambda _: None, chair_base=lambda _: base, probe=probe,
    )
    return result, calls, probed


def test_launch_on_host_resolves_the_chair_base_with_the_repos_setting_and_the_harness_without_it():
    seen = []

    def chair_base(path, repo_base=None):
        seen.append((path, repo_base))
        return AT_CHAIR

    launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", lambda argv: 0, repo="/r", harness_dir="/h",
        chair_head=lambda _: None, chair_base=chair_base, probe=lambda argv: (0, f"head {CHAIR}\n"), repo_base="develop",
    )
    assert seen == [("/r", "develop"), ("/h", None)]


def test_a_host_at_the_chairs_commit_launches_after_the_sync_and_the_record_carries_base_sha():
    result, calls, probed = _launch(f"head {CHAIR}\n")
    assert result == {"host": "box2", "launched_at": "t", "repo": "/r", "base_sha": CHAIR}
    assert len(probed) == 1 and "sync" in probed[0][-1] and "cox route launch" in calls[-1][-1]


def test_a_host_whose_sync_fails_refuses_with_both_shas_and_never_starts_the_lane():
    host_sha = "a" * 40
    result, calls, _ = _launch(f"sync: main cannot fast-forward to {CHAIR}\nhead {host_sha}\n", code=1)
    assert result == LaunchError("sync", f"box2: repo at {host_sha}, chair at {CHAIR}; sync failed: main cannot fast-forward to {CHAIR}")
    assert not any("cox route launch" in c[-1] for c in calls)


def test_a_zero_exit_with_another_head_is_still_a_refusal():
    assert base_refusal("h", CHAIR, 0, f"head {'a' * 40}") == f"h: repo at {'a' * 40}, chair at {CHAIR}; sync failed: head is not the chair's commit"


def test_a_chair_whose_fetch_fails_refuses_before_anything_is_copied():
    error = LaunchError("sync", "repo at unknown, chair at unknown; sync failed: the chair's git fetch origin exited 1")
    result, calls, _ = _launch("", base=error)
    assert result == LaunchError("sync", f"box2: {error.message}") and calls == []


def test_a_chair_origin_whose_base_branch_does_not_resolve_refuses_instead_of_skipping_the_guard(tmp_path, monkeypatch):
    clone, _ = _repos(tmp_path, monkeypatch)
    _git(tmp_path / "origin.git", "branch", "-m", "main", "trunk")
    _git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    _git(clone, "remote", "set-head", "origin", "-d")
    _git(clone, "config", "remote.origin.followRemoteHEAD", "never")
    refused = _chair_base(str(clone))
    assert refused == LaunchError(
        "sync",
        f"repo at unknown, chair at unknown; sync failed: origin/main does not resolve in the chair's checkout {clone}",
    )
    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", lambda argv: 0, repo=str(clone), chair_head=lambda _: None,
    )
    assert result == LaunchError("sync", f"box2: {refused.message}")


def test_a_chair_checkout_with_no_origin_has_no_base_and_keeps_the_old_sync(tmp_path):
    _git(tmp_path, "init", "-q", str(tmp_path / "bare"))
    assert _chair_base(str(tmp_path / "bare")) is None


def test_a_missing_host_checkout_refuses_with_an_unknown_host_sha_and_the_reason(tmp_path):
    done = _on_host(tmp_path / "nowhere", CHAIR, tmp_path)
    assert base_refusal("box2", CHAIR, done.returncode, done.stdout + done.stderr) == (
        f"box2: repo at unknown, chair at {CHAIR}; sync failed: cannot enter {tmp_path / 'nowhere'}"
    )


def test_launch_on_host_runs_the_real_sync_script_before_it_starts_the_lane(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    main_sha = _git(seed, "rev-parse", "HEAD")
    events = []

    def run(argv):
        events.append("launch" if "cox route launch" in argv[-1] else "copy")
        return 0

    def probe(argv):
        events.append("sync")
        done = subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", run, repo=str(clone),
        chair_head=lambda _: None, chair_base=lambda _: ChairBase("main", main_sha), probe=probe,
    )
    assert result == {"host": "box2", "launched_at": "t", "repo": str(clone), "base_sha": main_sha}
    assert (events, _git(clone, "rev-parse", "HEAD")) == (["copy", "sync", "launch"], main_sha)


def test_launch_on_host_refuses_a_host_the_real_sync_script_cannot_move(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    before, main_sha = _git(clone, "rev-parse", "HEAD"), _git(seed, "rev-parse", "HEAD")
    (clone / "f").write_text("local edit")
    started = []

    def probe(argv):
        done = subprocess.run(["sh", "-c", argv[-1]], capture_output=True, text=True)
        return done.returncode, done.stdout + done.stderr

    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", lambda argv: started.append(argv) or 0, repo=str(clone),
        chair_head=lambda _: None, chair_base=lambda _: ChairBase("main", main_sha), probe=probe,
    )
    assert result == LaunchError(
        "sync", f"box2: repo at {before}, chair at {main_sha}; sync failed: tracked changes in the working tree"
    )
    assert not any("cox route launch" in a[-1] for a in started)


def test_a_record_without_base_sha_round_trips_and_one_with_it_keeps_it():
    old = json.dumps(remote_record("h", "t", "/r"))
    assert parse_remote_record(old) == {"host": "h", "launched_at": "t", "repo": "/r"}
    assert parse_remote_record(json.dumps(remote_record("h", "t", "/r", "abc")))["base_sha"] == "abc"


def test_the_land_warns_only_when_the_base_is_more_than_the_limit_behind():
    assert behind_warning("a" * 40, 21, 20) == "land: warning, the task was built on aaaaaaaa, 21 commits behind main (limit 20); the cherry-pick may conflict"
    assert [behind_warning("a" * 40, n, 20) for n in (20, None)] + [behind_warning(None, 99, 20)] == [None, None, None]


def test_runs_land_reads_base_sha_from_the_remote_record_and_counts_commits_behind_main(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    _git(clone, "checkout", "-q", "main")
    _git(clone, "pull", "-q", "--ff-only")
    base = _git(clone, "rev-parse", "HEAD~1")
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.remote.json").write_text(json.dumps(remote_record("h", "t", str(clone), base)))
    line = cli._base_behind_warning(runs, "r1", clone, "main", 0)
    assert line is not None and f"{base[:8]}, 1 commits behind main" in line
    assert cli._base_behind_warning(runs, "r1", clone, "main", 20) is None
    assert cli._base_behind_warning(runs, "other", clone, "main", 0) is None


def _plan(clone, repo_head=None, base=None):
    return launch_plan(HOST, "init-x", "init-x-1", "l", None, str(clone), repo_head=repo_head, base=base)


def _run_steps(steps, cwd):
    """Runs each ssh step as the remote login shell would; the exit code and stderr of the first that fails, else (0, '')."""
    for step in steps:
        done = subprocess.run(["sh", "-c", step[-1]], cwd=cwd, capture_output=True, text=True)
        if done.returncode != 0:
            return done.returncode, done.stderr
    return 0, ""


def test_old_plan_with_a_chair_repo_at_a_release_tag_pins_the_host_at_the_tag_not_at_main(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    tag_sha, main_sha = _git(clone, "rev-parse", "HEAD"), _git(seed, "rev-parse", "HEAD")
    assert _run_steps(_plan(clone, repo_head=ChairHead(tag_sha))[1:-1], tmp_path) == (0, "")
    assert (_git(clone, "rev-parse", "HEAD"), tag_sha != main_sha) == (tag_sha, True)


def test_old_plan_with_a_chair_repo_on_main_cannot_move_a_detached_host(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    before = _git(clone, "rev-parse", "HEAD")
    code, err = _run_steps(_plan(clone, repo_head=ChairHead(_git(seed, "rev-parse", "HEAD"), "main"))[1:-1], tmp_path)
    assert (code, err.strip(), _git(clone, "rev-parse", "HEAD")) == (1, "sync: checkout is on a detached HEAD, not main", before)


def test_the_base_plan_moves_the_same_detached_host_to_main(tmp_path, monkeypatch):
    clone, seed = _repos(tmp_path, monkeypatch)
    main_sha = _git(seed, "rev-parse", "HEAD")
    assert _run_steps(_plan(clone, base=ChairBase("main", main_sha))[1:-1], tmp_path) == (0, "")
    assert _git(clone, "rev-parse", "HEAD") == main_sha


def test_the_plan_syncs_the_harness_and_the_repo_to_the_same_chair_commit():
    plan = launch_plan(
        HOST, "init-x", "init-x-1", "l", None, "/r", "/h", base=AT_CHAIR, harness_base=AT_CHAIR,
        harness_head=ChairHead("a" * 40),
    )
    harness, repo = plan[1][-1], plan[2][-1]
    assert (f" sync /h main {CHAIR}" in harness, f" sync /r main {CHAIR}" in repo) == (True, True)
    assert "a" * 40 not in harness


def test_a_harness_that_cannot_reach_the_chairs_commit_refuses_before_the_repo_is_synced():
    probed = []

    def probe(argv):
        probed.append(argv)
        return 1, f"sync: tracked changes in the working tree\nhead {'a' * 40}\n"

    result = launch_on_host(
        HOST, "init-x", "init-x-1", "l", "t", lambda argv: 0, repo="/r", harness_dir="/h",
        chair_head=lambda _: None, chair_base=lambda _: AT_CHAIR, probe=probe,
    )
    assert isinstance(result, LaunchError) and result.step == "harness"
    assert result.message.startswith(f"box2: harness at {'a' * 40}, chair at {CHAIR}; sync failed: tracked changes")
    assert len(probed) == 1


class _AtCherryPick(Exception):
    pass


def _land_world(tmp_path, monkeypatch, capsys):
    """`cli.main` argv for `runs land --apply` of a remote run whose base is one commit behind main, stopped at the leftover check."""
    clone, _ = _repos(tmp_path, monkeypatch)
    _git(clone, "checkout", "-q", "main")
    _git(clone, "pull", "-q", "--ff-only")
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.remote.json").write_text(json.dumps(remote_record("h", "t", str(clone), _git(clone, "rev-parse", "HEAD~1"))))
    (runs / "r1.fetched.json").write_text("{}")
    provider = tmp_path / "provider.yaml"
    provider.write_text("work_state: files\n", encoding="utf-8")
    profile = tmp_path / "profile.yaml"
    profile.write_text(f"provider_profile: {provider}\n", encoding="utf-8")
    seen = []

    def at_leftover(*args, **kwargs):
        seen.append(capsys.readouterr().err)
        raise _AtCherryPick

    stubs = {
        "_resolved_run_id": lambda runs_dir, run_id: "r1",
        "_leader_guard_or_refuse": lambda *a, **k: None,
        "_land_repo_facts": lambda *a, **k: {"cartridge_error": None},
        "_land_load": lambda *a, **k: ({"task": "t", "initiative": "i", "phase": "p"}, "path", 1, "src"),
        "_land_branches": lambda *a, **k: {},
        "_land_item_facts": lambda *a, **k: (None, None),
        "_land_enrich": lambda steps, **k: steps,
        "_repo_is_dirty": lambda repo: False,
        "_sync_default_branch": lambda repo, branch: ("skip", ""),
        "_land_resume": lambda *a, **k: {},
        "_land_leftover": at_leftover,
    }
    for name, value in stubs.items():
        monkeypatch.setattr(cli, name, value)
    monkeypatch.setattr(cli.land, "land_plan", lambda *a, **k: [{"kind": "cherry_pick", "onto": "pr/t"}])
    monkeypatch.setattr(cli.land, "gate_steps", lambda steps, level: steps)
    monkeypatch.setattr(cli.forge, "missing_refs", lambda module: [])
    argv = ["runs", "land", "r1", "--task", "t", "--repo", str(clone), "--profile", str(profile), "--runs-dir", str(runs), "--apply", "--no-claim"]
    return argv, seen


def test_runs_land_prints_the_far_behind_warning_before_the_first_cherry_pick_call(tmp_path, monkeypatch, capsys):
    argv, seen = _land_world(tmp_path, monkeypatch, capsys)
    with pytest.raises(_AtCherryPick):
        cli.main([*argv, "--behind-limit", "0"])
    assert len(seen) == 1 and "1 commits behind origin/main (limit 0)" in seen[0]


def test_the_dry_run_land_shows_the_same_warning_so_the_operator_sees_it_before_deciding(tmp_path, monkeypatch, capsys):
    argv, _ = _land_world(tmp_path, monkeypatch, capsys)
    capsys.readouterr()
    assert cli.main([a for a in argv if a != "--apply"] + ["--behind-limit", "0"]) == 0
    assert "1 commits behind origin/main (limit 0)" in capsys.readouterr().err


def test_a_land_with_no_cherry_pick_step_still_warns_before_it_takes_the_repo_lease(tmp_path, monkeypatch, capsys):
    argv, seen = _land_world(tmp_path, monkeypatch, capsys)
    monkeypatch.setattr(cli.land, "land_plan", lambda *a, **k: [{"kind": "push"}])

    def at_lease(*args, **kwargs):
        seen.append(capsys.readouterr().err)
        raise _AtCherryPick

    monkeypatch.setattr(cli.land_repo_lease, "acquire", at_lease)
    with pytest.raises(_AtCherryPick):
        cli.main([*argv, "--behind-limit", "0"])
    assert len(seen) == 1 and "1 commits behind origin/main (limit 0)" in seen[0]


def test_runs_land_is_silent_when_the_base_is_within_the_default_limit(tmp_path, monkeypatch, capsys):
    argv, seen = _land_world(tmp_path, monkeypatch, capsys)
    with pytest.raises(_AtCherryPick):
        cli.main(argv)
    assert len(seen) == 1 and "commits behind" not in seen[0]
