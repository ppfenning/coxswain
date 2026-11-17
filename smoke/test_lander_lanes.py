"""H3: the lander does not block. Real `cox lander` against real git repositories (bare origins) and the store.

Each initiative is put in flight the way an operator does it (`cox route import`, then `cox lander adopt --apply`, which
writes the landing record and merges the built phase into the initiative branch), and then landed by the lander's own
entry points. The forge is the local one (`forge_local`, the default): there is no pull-request host, so a repository's
checks are its gate checks, which the lander runs itself. A gate check that has not finished is a repository whose
checks are pending.

- Two repositories, one whose checks stay pending: `cox lander run` lands the other one while they do, then lands the
  first once its checks pass (H3.1, H3.2).
- The record says a phase merged and the initiative branch does not hold it (H3.3): with the commit gone from the
  clone, the first `cox lander once` holds the initiative with the reason; with the phase branch still there, it merges
  the phase again and the initiative lands in the same pass.
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

from smoke import plan

PASS_TIMEOUT_S = 120.0


def _git(sandbox, where: Path, *args: str) -> str:
    return sandbox.git(where, *args)


def _profile_with_cartridge(sandbox, tmp_path: Path) -> None:
    """A team cartridge whose ticket gate runs each repository's own `gate.sh`, and gate `full`: the lander merges."""
    cartridges = tmp_path / "cartridges"
    (cartridges / "smoke").mkdir(parents=True)
    (cartridges / "smoke" / "cartridge.yaml").write_text(
        "team: smoke\nversion: 1\nlanding_areas:\n  ticket_checks:\n    - name: gate\n      cmd: sh ./gate.sh\n", encoding="utf-8")
    profile = sandbox.home / ".config" / "agent-tools" / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + f"cartridges_dir: {cartridges}\n", encoding="utf-8")
    sandbox.write("runs/policy.gate.json", json.dumps({"level": "full"}))


def _repository(sandbox, tmp_path: Path, name: str, gate: str) -> tuple[Path, Path]:
    """A clone at `~/src/<name>` of a bare origin, with `gate.sh` and one commit on main pushed."""
    origin, path = tmp_path / f"{name}.git", sandbox.home / "src" / name
    _git(sandbox, tmp_path, "init", "--quiet", "--bare", "--initial-branch=main", str(origin))
    _git(sandbox, tmp_path, "clone", "--quiet", str(origin), str(path))
    (path / "README.md").write_text(f"{name}\n", encoding="utf-8")
    (path / "gate.sh").write_text(gate, encoding="utf-8")
    _git(sandbox, path, "add", "README.md", "gate.sh")
    _git(sandbox, path, "commit", "--quiet", "-m", "init")
    _git(sandbox, path, "push", "--quiet", "origin", "main")
    _git(sandbox, path, "remote", "set-head", "origin", "main")
    return path, origin


def _in_flight(sandbox, repo: Path, name: str, initiative: str) -> str:
    """One initiative with one approved task on its phase branch, imported and adopted. Its phase tip."""
    phase = f"epic/{initiative}/p1"
    _git(sandbox, repo, "checkout", "--quiet", "-b", phase, "main")
    (repo / f"{initiative}.txt").write_text(f"{initiative}\n", encoding="utf-8")
    _git(sandbox, repo, "add", f"{initiative}.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", f"{initiative}: the feature")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    tip = _git(sandbox, repo, "rev-parse", phase)
    sandbox.write(f"work/{initiative}/initiative.md",
                  plan.work_item_text({"title": initiative, "repo": f"~/src/{name}", "draft": "false"}, "An initiative."))
    sandbox.write(f"work/{initiative}/p1/{initiative}-t1.md", plan.work_item_text(
        {"id": f"{initiative}-t1", "title": "The feature", "phase": "p1", "state": "approved", "merged_commit": tip}, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr
    adopted = sandbox.cox("lander", "adopt", "--apply", initiative)
    assert adopted.returncode == 0, adopted.stdout + adopted.stderr
    assert f"phase p1 of {initiative} merged into init/{initiative}" in adopted.stdout, adopted.stdout
    return tip


def _on_origin_main(sandbox, origin: Path, path: str) -> bool:
    done = subprocess.run(["git", "--git-dir", str(origin), "cat-file", "-e", f"main:{path}"], env=sandbox.env,
                          capture_output=True, text=True, timeout=30)
    return done.returncode == 0


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


def test_a_repository_whose_checks_stay_pending_never_holds_another_ones_land(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path)
    release = tmp_path / "release-slow"
    slow, slow_origin = _repository(sandbox, tmp_path, "slow", f"while [ ! -e '{release}' ]; do sleep 0.2; done\n")
    fast, fast_origin = _repository(sandbox, tmp_path, "fast", "exit 0\n")
    _in_flight(sandbox, slow, "slow", "alpha-slow")  # first by name and in the queue: a lander that works one repository after another takes it first
    _in_flight(sandbox, fast, "fast", "beta-fast")

    log = tmp_path / "lander.log"
    with log.open("w", encoding="utf-8") as sink:
        lander = subprocess.Popen([str(sandbox.install.cox), "lander", "run", "--interval", "1"], cwd=sandbox.home,
                                  env={**sandbox.env, "PYTHONUNBUFFERED": "1"}, stdout=sink, stderr=subprocess.STDOUT)
        try:
            _until(lambda: _on_origin_main(sandbox, fast_origin, "beta-fast.txt"), "beta-fast landed on fast's origin", lander, log)
            assert not _on_origin_main(sandbox, slow_origin, "alpha-slow.txt"), "alpha-slow landed with its checks still pending"
            assert [i["initiative"] for i in _status(sandbox)["initiatives"]] == ["alpha-slow"]

            release.touch()
            _until(lambda: _on_origin_main(sandbox, slow_origin, "alpha-slow.txt"), "alpha-slow landed once its checks passed", lander, log)
            _until(lambda: _status(sandbox)["initiatives"] == [], "every record landed", lander, log)
        finally:
            lander.kill()
            lander.wait(timeout=30)
    output = log.read_text(encoding="utf-8")
    assert "beta-fast recorded landed" in output and "alpha-slow recorded landed" in output, output
    assert output.index("beta-fast recorded landed") < output.index("alpha-slow recorded landed"), output


def _break_the_branch(sandbox, repo: Path, initiative: str, *, lose_the_phase: bool) -> None:
    """The efficiency case: the record still says p1 merged, but the initiative branch is back at main."""
    _git(sandbox, repo, "branch", "--force", f"init/{initiative}", "main")
    if lose_the_phase:
        _git(sandbox, repo, "branch", "--delete", "--force", f"epic/{initiative}/p1")
        _git(sandbox, repo, "reflog", "expire", "--expire=now", "--all")
        _git(sandbox, repo, "gc", "--quiet", "--prune=now")


def test_a_record_merged_on_a_branch_missing_the_commit_is_held_with_the_reason_on_the_first_pass(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path)
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")
    tip = _in_flight(sandbox, repo, "app", "ship")
    _break_the_branch(sandbox, repo, "ship", lose_the_phase=True)
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", tip], env=sandbox.env, capture_output=True, timeout=30).returncode != 0

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


def test_a_record_merged_on_a_branch_missing_the_commit_is_merged_again_when_the_tip_is_here(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path)
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")
    _in_flight(sandbox, repo, "app", "ship")
    _break_the_branch(sandbox, repo, "ship", lose_the_phase=False)

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    assert "is merged again; init/ship is now at" in done.stdout, done.stdout
    assert "ship recorded landed" in done.stdout, done.stdout
    assert _on_origin_main(sandbox, origin, "ship.txt")
    assert _status(sandbox)["initiatives"] == []
