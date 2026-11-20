"""2026-11-20: the lander opened an initiative pull request with "+0 -0, 0 files".

The phase branch held only merge commits ("merge main into ...", "Merge branch init/... into epic/.../p1"), and its
task had been marked approved by the fetch bug with no commit of its own. Real `cox lander adopt --apply` and
`cox lander once` against a real clone of a real bare origin and the store, with the local forge:

- **Only merges.** The phase is refused with the reason, the chair is told once across passes (naming the task that
  has no commit on the remote), and nothing is merged, queued to land or opened.
- **A real change.** A phase with a commit of its own still merges and lands, and the chair is told nothing.
"""

from __future__ import annotations

from smoke import plan
from smoke.test_lander_lanes import (
    _git,
    _in_flight,
    _item,
    _on_origin_main,
    _origin_git,
    _profile_with_cartridge,
    _record,
    _repository,
)

REASON = "ship: phase p1: no changes against main; the approved tasks have no commits"


def _inbox(sandbox) -> str:
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.returncode == 0, inbox.stdout + inbox.stderr
    return inbox.stdout


def test_an_approved_phase_of_only_merges_is_refused_told_once_and_opens_no_pull_request(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path, gate="full")
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")
    _git(sandbox, repo, "branch", "epic/ship/p1", "main")  # the phase branch, cut before main moved on
    (repo / "elsewhere.txt").write_text("another initiative's change\n", encoding="utf-8")
    _git(sandbox, repo, "add", "elsewhere.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", "another initiative landed")
    _git(sandbox, repo, "push", "--quiet", "origin", "main")
    _git(sandbox, repo, "checkout", "--quiet", "epic/ship/p1")
    _git(sandbox, repo, "merge", "--quiet", "--no-ff", "-m", "merge main into epic/ship/p1", "main")
    _git(sandbox, repo, "checkout", "--quiet", "main")
    _git(sandbox, repo, "push", "--quiet", "origin", "epic/ship/p1")
    main_tip = _origin_git(sandbox, origin, "rev-parse", "refs/heads/main").stdout.strip()
    sandbox.write("work/ship/initiative.md",
                  plan.work_item_text({"title": "ship", "repo": "~/src/app", "draft": "false"}, "An initiative."))
    sandbox.write("work/ship/p1/ship-t1.md", plan.work_item_text(
        {"id": "ship-t1", "title": "The feature", "phase": "p1", "state": "approved"}, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr

    adopted = sandbox.cox("lander", "adopt", "--apply", "ship")
    first, second = sandbox.cox("lander", "once"), sandbox.cox("lander", "once")

    assert adopted.returncode == 0 and REASON in adopted.stdout, adopted.stdout + adopted.stderr
    for done in (first, second):
        assert done.returncode == 0, done.stdout + done.stderr
        assert f"phase_merge:ship:p1: waiting: {REASON}" in done.stdout, done.stdout
    inbox = _inbox(sandbox)
    assert inbox.count(REASON) == 1, inbox
    assert "approved with no commit on the remote: ship-t1" in inbox, inbox
    record = _record(sandbox, "ship")
    assert (record.get("phases") or {}).get("p1", {}).get("state") != "merged", record
    assert not (record.get("pr") or {}).get("url"), "no pull request was opened"
    assert _item(sandbox, "initiative_land:ship") is None, "nothing was queued to land"
    assert _origin_git(sandbox, origin, "rev-parse", "refs/heads/main").stdout.strip() == main_tip


def test_a_phase_with_a_real_change_still_lands(sandbox, tmp_path):
    _profile_with_cartridge(sandbox, tmp_path, gate="full")
    repo, origin = _repository(sandbox, tmp_path, "app", "exit 0\n")
    _in_flight(sandbox, repo, "app", "ship")

    done = sandbox.cox("lander", "once")

    assert done.returncode == 0, done.stdout + done.stderr
    assert _record(sandbox, "ship")["state"] == "landed", done.stdout
    assert _on_origin_main(sandbox, origin, "ship.txt")
    assert "no changes against" not in done.stdout and "no changes against" not in _inbox(sandbox)
