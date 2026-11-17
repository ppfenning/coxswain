"""H3: the lander does not block. Real `cox lander` against real git repositories (bare origins) and the store.

Each initiative is put in flight the way an operator does it (`cox route import`, then `cox lander adopt --apply`, which
writes the landing record and merges the built phase into the initiative branch), and then landed by the lander's own
entry points. The forge is the local one (`forge_local`, the default): there is no pull-request host, so a repository's
checks are its gate checks, which the lander runs itself. A gate check that has not finished is a repository whose
checks are pending. The local forge never pushes an initiative branch, so where a test needs one published (a pull
request open, the branch on origin) it pushes it itself, as the GitHub forge would have.

- Two repositories, one whose checks stay pending: `cox lander run` lands the other one while they do, then lands the
  first once its checks pass (H3.1, H3.2). While that lane is busy, the main thread writes none of its records.
- A published initiative whose record says a phase merged and whose branch on origin does not hold it (H3.3): with the
  commit gone, the first `cox lander once` holds it with the reason; with the phase tip here, it merges the phase into
  origin's branch, pushes it, and the initiative then lands. Two landers at once repair it once. A busy lease waits.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from smoke import plan

PASS_TIMEOUT_S = 120.0
QUIET_S = 4.0  # passes at a 0.5 s interval: long enough for several of them to run


def _git(sandbox, where: Path, *args: str) -> str:
    return sandbox.git(where, *args)


def _origin_git(sandbox, origin: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "--git-dir", str(origin), *args], env=sandbox.env, capture_output=True, text=True, timeout=30)


def _gate(sandbox, level: str) -> None:
    sandbox.write("runs/policy.gate.json", json.dumps({"level": level}))


def _profile_with_cartridge(sandbox, tmp_path: Path, gate: str = "full") -> None:
    """A team cartridge whose ticket gate runs each repository's own `gate.sh`, and the gate level `gate`."""
    cartridges = tmp_path / "cartridges"
    (cartridges / "smoke").mkdir(parents=True)
    (cartridges / "smoke" / "cartridge.yaml").write_text(
        "team: smoke\nversion: 1\nlanding_areas:\n  ticket_checks:\n    - name: gate\n      cmd: sh ./gate.sh\n", encoding="utf-8")
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + f"cartridges_dir: {cartridges}\n", encoding="utf-8")
    _gate(sandbox, gate)


def _repository(sandbox, tmp_path: Path, name: str, gate: str, *, ci: bool = False) -> tuple[Path, Path]:
    """A clone at `~/src/<name>` of a bare origin, with `gate.sh` (and a CI workflow when `ci`) and one commit on main pushed."""
    origin, path = tmp_path / f"{name}.git", sandbox.home / "src" / name
    _git(sandbox, tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    _git(sandbox, tmp_path, "clone", "--quiet", str(origin), str(path))
    (path / "README.md").write_text(f"{name}\n", encoding="utf-8")
    (path / "gate.sh").write_text(gate, encoding="utf-8")
    if ci:
        (path / ".github" / "workflows").mkdir(parents=True)
        (path / ".github" / "workflows" / "ci.yml").write_text("on: push\n", encoding="utf-8")
    _git(sandbox, path, "add", "--all")
    _git(sandbox, path, "commit", "--quiet", "-m", "init")
    _git(sandbox, path, "push", "--quiet", "origin", "main")
    _git(sandbox, path, "remote", "set-head", "origin", "main")
    return path, origin


def _work_files(sandbox, name: str, initiative: str, state: str, commit: str = "") -> None:
    sandbox.write(f"work/{initiative}/initiative.md",
                  plan.work_item_text({"title": initiative, "repo": f"~/src/{name}", "draft": "false"}, "An initiative."))
    fields = {"id": f"{initiative}-t1", "title": "The feature", "phase": "p1", "state": state}
    sandbox.write(f"work/{initiative}/p1/{initiative}-t1.md",
                  plan.work_item_text({**fields, **({"merged_commit": commit} if commit else {})}, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _in_flight(sandbox, repo: Path, name: str, initiative: str) -> str:
    """One initiative with one approved task on its phase branch, imported and adopted. Its phase tip."""
    phase = f"epic/{initiative}/p1"
    _git(sandbox, repo, "checkout", "--quiet", "-b", phase, "main")
    (repo / f"{initiative}.txt").write_text(f"{initiative}\n", encoding="utf-8")
    _git(sandbox, repo, "add", f"{initiative}.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", f"{initiative}: the feature")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    tip = _git(sandbox, repo, "rev-parse", phase)
    _work_files(sandbox, name, initiative, "approved", tip)
    adopted = sandbox.cox("lander", "adopt", "--apply", initiative)
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    assert f"phase p1 of {initiative} merged into init/{initiative}" in adopted.stdout, adopted.stdout
    return tip


def _record(sandbox, initiative: str) -> dict:
    """The landing record, read through the lander's own store connection."""
    return sandbox.python_json(
        "import json; from pathlib import Path; from agent_tools import lander_ports; from harness import landing_store as ls; "
        f"print(json.dumps(ls.get_record(lander_ports.connect(Path({str(sandbox.workspace / 'runs')!r})), {initiative!r})))")


def _item(sandbox, ident: str) -> dict:
    """One land-queue item, read through the lander's own store connection."""
    return sandbox.python_json(
        "import json; from pathlib import Path; from agent_tools import lander_ports; from harness import landing_store as ls; "
        f"print(json.dumps(ls.get_item(lander_ports.connect(Path({str(sandbox.workspace / 'runs')!r})), {ident!r})))")


def _on_origin_main(sandbox, origin: Path, path: str) -> bool:
    return _origin_git(sandbox, origin, "cat-file", "-e", f"main:{path}").returncode == 0


def _status(sandbox) -> dict:
    done = sandbox.cox("lander", "status", "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


def _until(condition, what: str, lander: subprocess.Popen, log: Path) -> None:
    deadline = time.monotonic() + PASS_TIMEOUT_S
    while time.monotonic() < deadline:
        if condition():
            return
        if lander.poll() is not None:
            raise AssertionError(f"the lander exited ({lander.returncode}) before {what}:\n{log.read_text(encoding='utf-8')}")
        time.sleep(0.5)
    raise AssertionError(f"not within {PASS_TIMEOUT_S:.0f}s: {what}:\n{log.read_text(encoding='utf-8')[-6000:]}")


# -- H3.1 and H3.2: lanes ---------------------------------------------------------------------------------------------

def test_a_repository_whose_checks_stay_pending_never_holds_another_ones_land(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path)
    started, release = tmp_path / "slow-started", tmp_path / "release-slow"
    slow, slow_origin = _repository(sandbox, tmp_path, "slow", f"touch '{started}'\nwhile [ ! -e '{release}' ]; do sleep 0.2; done\n")
    fast, fast_origin = _repository(sandbox, tmp_path, "fast", "exit 0\n")
    _in_flight(sandbox, slow, "slow", "alpha-slow")  # first by name and in the queue: a lander that works one repository after another takes it first
    _in_flight(sandbox, fast, "fast", "beta-fast")

    log = tmp_path / "lander.log"
    with log.open("w", encoding="utf-8") as sink:
        lander = subprocess.Popen([str(sandbox.install.cox), "lander", "run", "--interval", "0.5"], cwd=sandbox.home,
                                  env={**sandbox.env, "PYTHONUNBUFFERED": "1"}, stdout=sink, stderr=subprocess.STDOUT)
        try:
            _until(lambda: _on_origin_main(sandbox, fast_origin, "beta-fast.txt"), "beta-fast landed on fast's origin", lander, log)
            _until(started.exists, "alpha-slow's checks started", lander, log)
            assert not _on_origin_main(sandbox, slow_origin, "alpha-slow.txt"), "alpha-slow landed with its checks still pending"
            assert [i["initiative"] for i in _status(sandbox)["initiatives"]] == ["alpha-slow"]

            # One writer per record: while its lane is busy, the passes the main thread keeps making do not touch a
            # record of that repository. A new initiative there, with a quarantined task, is left for its lane.
            _work_files(sandbox, "slow", "alpha-blocked", "quarantined")
            adopted = sandbox.cox("lander", "adopt", "--apply", "alpha-blocked")
            assert adopted.returncode == 0, adopted.stdout + adopted.stderr
            time.sleep(QUIET_S)
            untouched = ((_record(sandbox, "alpha-blocked").get("phases") or {}).get("p1") or {}).get("state")
            assert untouched != "blocked", log.read_text(encoding="utf-8")

            release.touch()
            _until(lambda: _on_origin_main(sandbox, slow_origin, "alpha-slow.txt"), "alpha-slow landed once its checks passed", lander, log)
            _until(lambda: (_record(sandbox, "alpha-blocked").get("phases") or {}).get("p1", {}).get("state") == "blocked",
                   "alpha-blocked's lane applied on_blocked once the lane was free", lander, log)
        finally:
            lander.kill()
            lander.wait(timeout=30)
    output = log.read_text(encoding="utf-8")
    assert "beta-fast recorded landed" in output and "alpha-slow recorded landed" in output, output
    assert output.index("beta-fast recorded landed") < output.index("alpha-slow recorded landed"), output
    assert _record(sandbox, "alpha-slow")["state"] == "landed", "the lane's landed write survived the passes made meanwhile"


# -- H3.1: checks that cannot be read ---------------------------------------------------------------------------------

def test_checks_that_cannot_be_read_keep_the_land_waiting_tell_the_chair_once_and_the_item_ages(sandbox, tmp_path):
    """The pull request opens on the local forge; then the profile names Forgejo, whose checks this origin (a path, no
    Forgejo URL) can never be read from: what a broken token or a misconfigured forge looks like to the lander."""
    _profile_with_cartridge(sandbox, tmp_path, gate="ticket")
    repo, _ = _repository(sandbox, tmp_path, "app", "exit 0\n", ci=True)
    _in_flight(sandbox, repo, "app", "ship")
    opened = sandbox.cox("lander", "once")
    assert opened.returncode == 0 and "opened the pull request for ship" in opened.stdout, opened.stdout + opened.stderr
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + "forge: forgejo\n", encoding="utf-8")
    _gate(sandbox, "full")
    limit = sandbox.python_json("from agent_tools import land; print(land.POLL_ERROR_LIMIT)")
    queued_at = _item(sandbox, "initiative_land:ship")["queued_at"]

    outputs = []
    for _ in range(limit + 2):
        done = sandbox.cox("lander", "once")
        assert done.returncode == 0, done.stdout + done.stderr
        outputs.append(done.stdout)

    assert all("initiative_land:ship: waiting: checks on" in out for out in outputs), outputs
    assert "read again next pass (1/" in outputs[0] and "cannot be read: checks unreadable" in outputs[-1], outputs
    item = _item(sandbox, "initiative_land:ship")
    assert (item["state"], item["queued_at"]) == ("queued", queued_at), item
    inbox = sandbox.cox("courier", "inbox", "--label", "chair").stdout
    assert inbox.count("cannot be read: origin") == 1, inbox
    assert _record(sandbox, "ship")["state"] == "pr_open"


# -- H3.3: the record against origin ----------------------------------------------------------------------------------

def _published_and_lost(sandbox, tmp_path, *, lose_the_phase: bool, publish_the_phase: bool = False) -> tuple[Path, Path, str]:
    """`ship`, published: its pull request open (gate `ticket` stops the land there) and its branch on origin. Then the
    efficiency case: the record still says p1 merged, but origin's `init/ship`, and this clone's, are back at main."""
    _profile_with_cartridge(sandbox, tmp_path, gate="ticket")
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")
    tip = _in_flight(sandbox, repo, "app", "ship")
    opened = sandbox.cox("lander", "once")
    assert opened.returncode == 0 and "opened the pull request for ship" in opened.stdout, opened.stdout + opened.stderr
    _git(sandbox, repo, "push", "--quiet", "origin", "init/ship")  # what a forge with pull requests does at the open
    if publish_the_phase:
        _git(sandbox, repo, "push", "--quiet", "origin", "epic/ship/p1")
    _git(sandbox, repo, "push", "--quiet", "--force", "origin", "main:refs/heads/init/ship")
    _git(sandbox, repo, "branch", "--force", "init/ship", "main")
    if lose_the_phase:
        _git(sandbox, repo, "branch", "--delete", "--force", "epic/ship/p1")
        _git(sandbox, repo, "reflog", "expire", "--expire=now", "--all")
        _git(sandbox, repo, "gc", "--quiet", "--prune=now")
        assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", tip], env=sandbox.env, capture_output=True, timeout=30).returncode != 0
    return repo, origin, tip


def test_a_record_merged_on_a_branch_missing_the_commit_is_held_with_the_reason_on_the_first_pass(sandbox, tmp_path):
    _, origin, tip = _published_and_lost(sandbox, tmp_path, lose_the_phase=True)

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    reason = f"does not hold {tip[:12]}, and this clone has no copy of its tip {tip[:12]} to merge again"
    assert "reconcile: ship is held: phase p1 is recorded merged at" in done.stdout and reason in done.stdout, done.stdout
    assert "initiative_land:ship: waiting: ship is held: phase p1" in done.stdout, done.stdout  # the land waits on it
    [row] = _status(sandbox)["initiatives"]
    assert row["initiative"] == "ship" and reason in row["held"]
    assert not _on_origin_main(sandbox, origin, "ship.txt")
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.returncode == 0 and inbox.stdout.count(reason) == 1, inbox.stdout + inbox.stderr

    again = sandbox.cox("lander", "once")  # told once: a second pass holds on, and says nothing new about it
    assert again.returncode == 0, again.stdout + again.stderr
    assert "reconcile: ship is held" not in again.stdout and reason in _status(sandbox)["initiatives"][0]["held"]
    assert sandbox.cox("courier", "inbox", "--label", "chair").stdout.count(reason) == 1


def test_an_open_pull_request_missing_a_phase_gets_it_pushed_before_the_land(sandbox, tmp_path):
    _, origin, tip = _published_and_lost(sandbox, tmp_path, lose_the_phase=False)
    _gate(sandbox, "full")

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    pushed = "is merged again; init/ship is pushed at"
    assert pushed in done.stdout and "ship recorded landed" in done.stdout, done.stdout
    assert done.stdout.index(pushed) < done.stdout.index("ship recorded landed"), done.stdout
    assert _origin_git(sandbox, origin, "merge-base", "--is-ancestor", tip, "init/ship").returncode == 0, "origin's branch got the phase"
    assert _on_origin_main(sandbox, origin, "ship.txt")
    assert _status(sandbox)["initiatives"] == []


def test_a_repair_merged_here_but_never_pushed_is_pushed_on_the_next_pass_and_lands_with_the_phase(sandbox, tmp_path):
    """A lander that died between its repair merge and its push: this clone is ahead of origin, with the phase."""
    repo, origin, tip = _published_and_lost(sandbox, tmp_path, lose_the_phase=False)
    _git(sandbox, repo, "checkout", "--quiet", "init/ship")
    _git(sandbox, repo, "merge", "--quiet", "--no-ff", "--no-edit", "-m", "ship: merge phase p1 again (died before the push)", "epic/ship/p1")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    _gate(sandbox, "full")

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "init/ship is pushed at" in done.stdout and "ship recorded landed" in done.stdout, done.stdout
    head = _origin_git(sandbox, origin, "rev-parse", "init/ship").stdout.strip()
    assert _origin_git(sandbox, origin, "merge-base", "--is-ancestor", tip, head).returncode == 0
    assert _record(sandbox, "ship")["phases"]["p1"]["merge_sha"] == head
    assert _on_origin_main(sandbox, origin, "ship.txt")


def test_a_busy_branch_lease_makes_reconciliation_wait_and_the_land_waits_for_the_missing_phase(sandbox, tmp_path):
    _, origin, tip = _published_and_lost(sandbox, tmp_path, lose_the_phase=False)
    _gate(sandbox, "full")
    before = _record(sandbox, "ship")["phases"]["p1"]
    held = sandbox.python_json(
        "import json; from pathlib import Path; from agent_tools import lander_ports; "
        "from harness import landing_ops as ops, store_lease; "
        f"conn = lander_ports.connect(Path({str(sandbox.workspace / 'runs')!r})); "
        "print(json.dumps(store_lease.acquire(conn, ops.branch_lease('ship'), 'another-host', lander_ports.now_stamp(), 600).ok))")
    assert held is True
    main = _origin_git(sandbox, origin, "rev-parse", "main").stdout.strip()

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "reconcile: ship waits: another-host is moving init/ship" in done.stdout, done.stdout
    assert f"initiative_land:ship: waiting: origin/init/ship lacks recorded commit(s) {tip[:12]}" in done.stdout, done.stdout
    assert _origin_git(sandbox, origin, "rev-parse", "init/ship").stdout.strip() == main
    assert not _on_origin_main(sandbox, origin, "ship.txt")
    after = _record(sandbox, "ship")
    assert after["state"] == "pr_open" and after["phases"]["p1"]["merge_sha"] == before["merge_sha"] and not after.get("held")


def _second_host(sandbox, tmp_path, origin: Path) -> tuple[dict, Path]:
    """Another machine: its own home, with the same profile, store and workspace, and its own clone of the origin."""
    home = tmp_path / "host-b"
    shutil.copytree(sandbox.home / ".config", home / ".config")
    shutil.copy(sandbox.home / ".gitconfig", home / ".gitconfig")
    clone = home / "src" / "app"
    _git(sandbox, tmp_path, "clone", "--quiet", str(origin), str(clone))
    env = {**sandbox.env, "HOME": str(home), "XDG_CONFIG_HOME": f"{home}/.config", "XDG_DATA_HOME": f"{home}/.local/share",
           "XDG_STATE_HOME": f"{home}/.local/state", "XDG_CACHE_HOME": f"{home}/.cache", "GIT_CONFIG_GLOBAL": f"{home}/.gitconfig"}
    return env, clone


def test_two_landers_on_two_clones_repair_the_branch_once_the_second_waits_on_the_lease(sandbox, tmp_path):
    repo, origin, tip = _published_and_lost(sandbox, tmp_path, lose_the_phase=False, publish_the_phase=True)
    env_b, clone_b = _second_host(sandbox, tmp_path, origin)
    pushing, release = tmp_path / "a-pushing", tmp_path / "a-release"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\ntouch '{pushing}'\nwhile [ ! -e '{release}' ]; do sleep 0.2; done\n", encoding="utf-8")
    hook.chmod(0o755)

    lander_a = subprocess.Popen([str(sandbox.install.cox), "lander", "once"], cwd=sandbox.home, env=sandbox.env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + PASS_TIMEOUT_S
        while not pushing.exists():
            assert time.monotonic() < deadline and lander_a.poll() is None, "lander A never reached its push"
            time.sleep(0.2)
        done_b = subprocess.run([str(sandbox.install.cox), "lander", "once"], cwd=env_b["HOME"], env=env_b,
                                capture_output=True, text=True, timeout=PASS_TIMEOUT_S)
    finally:
        release.touch()
        output_a = lander_a.communicate(timeout=PASS_TIMEOUT_S)[0]

    assert done_b.returncode == 0, done_b.stdout + done_b.stderr
    assert "reconcile: ship waits: lander:" in done_b.stdout and "is moving init/ship" in done_b.stdout, done_b.stdout
    b_subjects = subprocess.run(["git", "-C", str(clone_b), "log", "--all", "--format=%s"], env=env_b, capture_output=True,
                                text=True, timeout=30).stdout
    assert "merge phase p1 again" not in b_subjects, "lander B made a merge commit while A held the lease"
    assert lander_a.returncode == 0 and "is merged again; init/ship is pushed at" in output_a, output_a
    subjects = _origin_git(sandbox, origin, "log", "--format=%s", "init/ship").stdout.splitlines()
    assert sum("merge phase p1 again" in s for s in subjects) == 1, subjects
    head = _origin_git(sandbox, origin, "rev-parse", "init/ship").stdout.strip()
    entry = _record(sandbox, "ship")["phases"]["p1"]
    assert entry["merge_sha"] == head and entry["tip"] == tip, (entry, head)
    assert not _record(sandbox, "ship").get("held")
