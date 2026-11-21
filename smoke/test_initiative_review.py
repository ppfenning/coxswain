"""D2, the initiative review loop, end to end: once every phase of a new-model initiative has merged into its
initiative branch, the chair queues its review, a worker's lane reviews the whole branch and on approve runs the full
suite (the `ticket` gate with `.agent-checks`' `# level: initiative` section) at the tip, and the lander opens the one
pull request only on that approval and that suite. Real Postgres, the real `cox worker`, `cox chair` and `cox lander`;
the scripted runner stands in for the model, keyed by call (`review_phase@<phase>-review-<round>`, the initiative's
`review_phase@initiative-review-<round>`), so a call the test did not expect fails the run.

The initiative `ship` has two phases, p1 (task a, adding `notes/a.txt`) and p2 (task b, adding `notes/b.txt`). Each
passes its own checks, smoke and review; but the repository's suite (`suite.sh`) fails when both notes are there
without `notes/fix.txt`: the merge breaks a test that neither phase broke alone.

- No forge: the review approves, the suite fails at the tip, the worker finishes the review `failed`, and the lander
  holds the initiative and opens no pull request; the chair is told once. Then a fix phase is filed, built and merged:
  the tip moves, the hold lifts, the chair reopens the review (a new episode), it approves, the suite passes at the
  new tip, and the pull request opens there, its body naming that sha.
- On Forgejo: round 1 says revise; the chair writes the revision phase `revise-1`, a worker claims and builds it (it
  adds the fix), the lander merges it; the chair queues the review again, round 2 approves, the suite passes. Then
  `main` moves before the land: the lander takes it forward, opens no pull request at a tip the suite never ran at,
  and pushes the refreshed branch; the chair reopens the review, the suite passes at the new tip, and the lander opens
  the pull request on the forge at that tip, saying which round and which sha passed.
- On Forgejo, wait for fresh: the pull request opens at a passing tip (a person's gate keeps it open), then `main`
  moves and the gate becomes `full`. The lander takes `main` forward and pushes the head into the open pull request,
  which becomes a draft (`WIP: `) whose body names the sha that last passed and the head ahead of it; the forge
  refuses an operator's merge of it, and the lander's own merge waits. The chair reopens the review with the pull
  request open, the suite passes at the new head, and the lander marks it ready and merges it.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from smoke import plan
from smoke.conftest import TEAM, Launchable, make_launchable
from smoke.test_lander_lanes import _gate, _record
from smoke.test_phase_lane import APPROVE, PASSWORD_ENV, ROLE_ENV, THREADS, Worker, _owner

psycopg = pytest.importorskip("psycopg")

INITIATIVE, INIT = "ship", "init/ship"
SUITE = ('if [ -e notes/a.txt ] && [ -e notes/b.txt ] && [ ! -e notes/fix.txt ]; then '
         'echo "test_notes: a and b together need notes/fix.txt" >&2; exit 1; fi\n')
REVISE = {"verdict": "revise", "rationale": "a and b together break the suite without a fix",
          "findings": [{"charter_principle": "goal", "file": "notes/fix.txt", "detail": "add notes/fix.txt"}]}


def _build(task: str, path: str) -> dict:
    return {"patch": plan.new_file_patch(path, task), "summary": "built", "files_touched": [path], "commands_run": []}


def _plan_answers(**reviews: object) -> dict[str, object]:
    """Plan and build a and b, and the reviewers the test names, keyed by call."""
    return {"plan": {"steps": ["write the note"], "files_expected": [], "out_of_scope": []},
            "build@a": _build("a", "notes/a.txt"), "build@b": _build("b", "notes/b.txt"),
            **{f"review_phase@{call.replace('_', '-')}": answer for call, answer in reviews.items()}}


def _phase_row(sandbox, phase: str) -> dict:
    keys = ("state", "outcome", "reason", "run_id", "kind", "base_ref", "claim_epoch", "attempts")
    found = _owner(sandbox, f"SELECT {', '.join(keys)} FROM phase_queue WHERE initiative = %s AND phase = %s",
                   args=(INITIATIVE, phase))[0]
    return dict(zip(keys, found[0], strict=True)) if found else {}


def _rounds(sandbox) -> list[tuple]:
    return [tuple(r) for r in _owner(sandbox, "SELECT round, verdict, head_sha FROM review_rounds WHERE initiative = %s "
                                              "AND phase = '' ORDER BY round", args=(INITIATIVE,))[0]]


def _suites(sandbox) -> list[tuple]:
    return [tuple(r) for r in _owner(sandbox, "SELECT step, ok, subject_sha FROM step_results WHERE initiative = %s "
                                              "AND phase = '' AND kind = 'suite' ORDER BY finished_at",
                                     args=(INITIATIVE,))[0]]


def _tick(sandbox) -> str:
    tick = sandbox.cox("chair", "run", "--once", timeout=180)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    return tick.stdout


def _lander(sandbox) -> str:
    done = sandbox.cox("lander", "once", timeout=300)
    assert done.returncode in (0, 1), done.stdout + done.stderr
    return done.stdout + done.stderr


def _origin_tip(sandbox, clone: Path, branch: str) -> str:
    listed = sandbox.git(clone, "ls-remote", "origin", f"refs/heads/{branch}")
    return listed.split()[0] if listed.split() else ""


def _work(sandbox, clone: Path) -> None:
    """The repository's checks and suite on main, and the initiative's two phases filed in the store."""
    (clone / ".agent-checks").write_text("# level: phase\ntrue\n# level: initiative\nsh ./suite.sh\n", encoding="utf-8")
    (clone / "suite.sh").write_text(SUITE, encoding="utf-8")
    sandbox.git(clone, "add", ".agent-checks", "suite.sh")
    sandbox.git(clone, "commit", "--quiet", "-m", "the checks and the suite")
    sandbox.git(clone, "push", "--quiet", "origin", "main")
    sandbox.write(f"work/{INITIATIVE}/initiative.md",
                  plan.work_item_text({"id": INITIATIVE, "title": "Ship", "repo": "~/src/app"}, "Ship two notes."))
    for task, phase in (("a", "p1"), ("b", "p2")):
        sandbox.write(f"work/{INITIATIVE}/{phase}/{task}.md", plan.work_item_text(
            {"id": task, "title": f"Add note {task}", "state": "ready", "phase": phase, "repo": "app"}, f"Add notes/{task}.txt."))
    filled = sandbox.cox("route", "store-fill")
    assert filled.returncode == 0, filled.stdout + filled.stderr


def _machine(launchable: Launchable, clone: Path) -> Iterator[tuple]:
    """The sandbox as one phase-model machine whose lander makes the landing record (`cox lander adopt`) and merges each
    phase as it is approved; a scoped store role for this test's worker, dropped at the end. Yields (sandbox, worker
    name, the worker's environment)."""
    sandbox = launchable.sandbox
    path = launchable.cartridges / TEAM / "cartridge.yaml"
    cartridge = json.loads(path.read_text(encoding="utf-8"))
    cartridge["policy"] = {"dispatch": {"lane_model": "phase", "lane_prereqs": False}}
    cartridge["landing_areas"]["checks"] = [{"name": "lane-probe", "cmd": f"command -v {plan.LANE_PROBE}"}]
    path.write_text(json.dumps(cartridge, indent=2) + "\n", encoding="utf-8")
    _gate(sandbox, "ticket")  # a person merges the pull request: it stays open for the test to read
    _work(sandbox, clone)
    adopted = sandbox.cox("lander", "adopt", "--apply", INITIATIVE)
    assert adopted.returncode == 0 and f"{INITIATIVE}: record made" in adopted.stdout, adopted.stdout + adopted.stderr
    assert _record(sandbox, INITIATIVE)["lane_model"] == "phase"
    if not _origin_tip(sandbox, clone, INIT):
        sandbox.git(clone, "push", "--quiet", "origin", f"{INIT}:refs/heads/{INIT}")
    out = _tick(sandbox)
    assert "launch_epic" not in out and _phase_row(sandbox, "p1")["state"] == "ready", out

    name = f"smoke-{urlsplit(sandbox.store_url).path.rsplit('_', 1)[-1]}"
    login = "cox_w_" + name.replace("-", "_")
    _owner(sandbox, "INSERT INTO hosts (name, ssh, capacity, state, beat_at, versions_json, updated_at, updated_by, "
                    "threads) VALUES (%s, '', 1, 'up', '', '{}', '', 'smoke', %s)", args=(name, THREADS))
    try:
        made = sandbox.cox("setup", "store", "worker-role", name)
        assert made.returncode == 0, made.stdout + made.stderr
        lines = (sandbox.home / ".config" / "agent-tools" / "garage.env").read_text(encoding="utf-8").splitlines()
        values = dict(line.split("=", 1) for line in lines if "=" in line)
        yield sandbox, name, {**sandbox.env, ROLE_ENV: values[ROLE_ENV], PASSWORD_ENV: values[PASSWORD_ENV]}
    finally:
        _owner(sandbox, f"DO $$BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{login}') "
                        f"THEN EXECUTE 'DROP OWNED BY {login}'; EXECUTE 'DROP ROLE {login}'; END IF; END$$")


def _claim_once(sandbox, env: dict[str, str], name: str) -> str:
    return Worker(sandbox, env, "once", "--name", name).finished(timeout=420)


def _phases_built_and_merged(sandbox, env: dict[str, str], name: str, clone: Path) -> str:
    """Each phase claimed and built by the worker and approved by its own review; the chair approves their tasks; the
    lander merges both into origin's initiative branch. That branch's tip."""
    for _ in ("p1", "p2"):
        _claim_once(sandbox, env, name)
    assert [(_phase_row(sandbox, p)["state"], _phase_row(sandbox, p)["outcome"]) for p in ("p1", "p2")] == [
        ("finished", "approved"), ("finished", "approved")]
    _tick(sandbox)  # the tasks approved at their roll-ups
    out = _lander(sandbox)
    for phase in ("p1", "p2"):
        assert f"phase {phase} of {INITIATIVE} merged into {INIT}" in out, out
    assert "the pull request waits: the initiative review has not decided a round yet" in out, out
    return _origin_tip(sandbox, clone, INIT)


def _queued_review(sandbox) -> dict:
    out = _tick(sandbox)
    row = _phase_row(sandbox, "initiative")
    assert (row["state"], row["kind"], row["base_ref"]) == ("ready", "initiative_review", INIT), (out, row)
    return row


def _chair_told(sandbox) -> int:
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.returncode == 0, inbox.stdout + inbox.stderr
    return inbox.stdout.count("the initiative review holds the pull request")


def _fixed(sandbox, env: dict[str, str], name: str, clone: Path) -> str:
    """A person files a fix phase (task `fix`, adding `notes/fix.txt`); a worker builds it, the chair approves its task
    and the lander merges it into origin's initiative branch. That branch's new tip."""
    sandbox.write(f"work/{INITIATIVE}/fix/fix.md", plan.work_item_text(
        {"id": "fix", "title": "Fix the suite", "state": "ready", "phase": "fix", "repo": "app"}, "Add notes/fix.txt."))
    filled = sandbox.cox("route", "store-fill")
    assert filled.returncode == 0, filled.stdout + filled.stderr
    _tick(sandbox)
    out = _claim_once(sandbox, env, name)
    assert (_phase_row(sandbox, "fix")["state"], _phase_row(sandbox, "fix")["outcome"]) == ("finished", "approved"), out
    _tick(sandbox)  # its task approved at its roll-up
    out = _lander(sandbox)
    assert f"phase fix of {INITIATIVE} merged into {INIT}" in out and "opened the pull request" not in out, out
    return _origin_tip(sandbox, clone, INIT)


def _reopened(sandbox, tip: str) -> None:
    """The chair reopens the finished review once the branch moved from the tip its last round reviewed."""
    out = _tick(sandbox)
    row = _phase_row(sandbox, "initiative")
    assert (row["state"], row["outcome"]) == ("ready", ""), (out, row)
    assert _record(sandbox, INITIATIVE)["review_tip"]["tip"] == tip


@pytest.fixture
def review_machine(launchable, repo) -> Iterator[tuple]:
    for sandbox, name, env in _machine(launchable, repo.path):
        yield sandbox, name, env, launchable, repo.path


def test_phases_whose_merge_breaks_the_suite_open_no_pull_request_until_a_fix_passes_at_the_new_tip(review_machine):
    sandbox, name, env, launchable, clone = review_machine
    launchable.scripted({**_plan_answers(p1_review_1=APPROVE, p2_review_1=APPROVE, initiative_review_1=APPROVE,
                                         fix_review_1=APPROVE, initiative_review_3=APPROVE),
                         "build@fix": _build("fix", "notes/fix.txt")})
    tip = _phases_built_and_merged(sandbox, env, name, clone)
    _queued_review(sandbox)

    out = _claim_once(sandbox, env, name)
    row = _phase_row(sandbox, "initiative")
    assert f"finished failed: the suite failed at {tip[:12]}" in out and "test_notes" in row["reason"], (out, row)
    assert (row["state"], row["outcome"]) == ("finished", "failed"), row
    assert _rounds(sandbox) == [(1, "approve", tip)]
    assert _suites(sandbox) == [("phase/suite/1", 0, tip)]

    out = _lander(sandbox)
    held = _record(sandbox, INITIATIVE)["held"]
    assert (held["cause"], held["tip"], held["phase"]) == ("suite_failed", tip, ""), held
    assert held["reason"].startswith(f"the initiative review holds the pull request: the suite failed at {tip[:12]}"), held
    assert not (_record(sandbox, INITIATIVE).get("pr") or {}).get("url"), "no pull request opens"
    assert "opened the pull request" not in out, out
    assert _chair_told(sandbox) == 1
    again = _lander(sandbox)
    assert "opened the pull request" not in again and _chair_told(sandbox) == 1, again

    # A fix merges: the tip moves, the hold lifts, the chair reopens the review, and the suite passes at the new tip.
    fixed = _fixed(sandbox, env, name, clone)
    assert not _record(sandbox, INITIATIVE).get("held") and fixed != tip
    _reopened(sandbox, fixed)
    out = _claim_once(sandbox, env, name)
    assert f"finished approved: the initiative review approved it in round 3 and the suite passed at {fixed[:12]}" in out, out
    assert _rounds(sandbox) == [(1, "approve", tip), (2, "reopened", ""), (3, "approve", fixed)]
    out = _lander(sandbox)
    assert "opened the pull request for ship" in out, out
    pr = _record(sandbox, INITIATIVE)["pr"]
    assert pr["url"] and _origin_tip(sandbox, clone, INIT) == fixed, pr


@pytest.fixture
def forge_review_machine(forge_sandbox, forge_repo, tmp_path) -> Iterator[tuple]:
    launchable = make_launchable(forge_sandbox, forge_repo.path, tmp_path, {"forge": "auto"})
    for sandbox, name, env in _machine(launchable, forge_repo.path):
        yield sandbox, name, env, launchable, forge_repo


@pytest.mark.forge
def test_a_revise_round_s_revision_phase_is_built_merged_and_reviewed_again_and_the_pull_request_opens(forge_review_machine):
    sandbox, name, env, launchable, forge_repo = forge_review_machine
    clone = forge_repo.path
    fix = {"patch": plan.new_file_patch("notes/fix.txt", "fix"), "summary": "the fix", "files_touched": ["notes/fix.txt"],
           "commands_run": []}
    launchable.scripted({**_plan_answers(p1_review_1=APPROVE, p2_review_1=APPROVE, initiative_review_1=REVISE,
                                         revise_1_review_1=APPROVE, initiative_review_2=APPROVE,
                                         initiative_review_4=APPROVE),
                         "build@revise-1": fix})
    first = _phases_built_and_merged(sandbox, env, name, clone)
    _queued_review(sandbox)

    # Round 1 asks for one revision: the worker finishes the review `revised`, and the chair writes its phase.
    out = _claim_once(sandbox, env, name)
    assert "finished revised: the initiative review's round 1 asked for revise-1" in out, out
    assert _rounds(sandbox) == [(1, "revise", first)] and _suites(sandbox) == []
    _tick(sandbox)
    revision = _phase_row(sandbox, "revise-1")
    assert (revision["state"], revision["kind"], revision["base_ref"]) == ("ready", "build", INIT), revision
    (task,) = _owner(sandbox, "SELECT state, title FROM work_items WHERE initiative = %s AND task_id = 'revise-1'",
                     args=(INITIATIVE,))[0]
    assert task == ["ready", "Revise ship after initiative review round 1"], task

    # A worker claims the revision phase and builds it on the merged initiative branch; the lander merges it.
    out = _claim_once(sandbox, env, name)
    assert (_phase_row(sandbox, "revise-1")["state"], _phase_row(sandbox, "revise-1")["outcome"]) == ("finished", "approved"), out
    _tick(sandbox)  # its task approved at its roll-up
    out = _lander(sandbox)
    assert f"phase revise-1 of {INITIATIVE} merged into {INIT}" in out, out
    assert "opened the pull request" not in out and forge_repo.branch_tip(INIT) is not None
    second = _origin_tip(sandbox, clone, INIT)
    assert second != first and forge_repo.file_text(INIT, "notes/fix.txt") == "fix\n"

    # Assembled again: the same review queued again, round 2 approves, the suite passes at the new tip.
    out = _tick(sandbox)
    row = _phase_row(sandbox, "initiative")
    assert (row["state"], row["outcome"]) == ("ready", ""), (out, row)
    out = _claim_once(sandbox, env, name)
    assert f"finished approved: the initiative review approved it in round 2 and the suite passed at {second[:12]}" in out, out
    assert _rounds(sandbox) == [(1, "revise", first), (2, "approve", second)]
    assert _suites(sandbox) == [("phase/suite/2", 1, second)]

    # main moves before the land: no pull request at a tip the suite never ran at; the refreshed branch is pushed.
    sandbox.git(clone, "checkout", "--quiet", "main")
    (clone / "elsewhere.txt").write_text("another change on main\n", encoding="utf-8")
    sandbox.git(clone, "add", "elsewhere.txt")
    sandbox.git(clone, "commit", "--quiet", "-m", "another change lands on main")
    sandbox.git(clone, "push", "--quiet", "origin", "main")
    out = _lander(sandbox)
    assert "opened the pull request" not in out and "after the suite passed" in out, out
    third = _origin_tip(sandbox, clone, INIT)
    assert third != second and forge_repo.file_text(INIT, "elsewhere.txt") == "another change on main\n"
    assert _record(sandbox, INITIATIVE)["review_tip"] == {"tip": third, "moves": 1}

    _reopened(sandbox, third)
    out = _claim_once(sandbox, env, name)
    assert f"finished approved: the initiative review approved it in round 4 and the suite passed at {third[:12]}" in out, out
    assert _suites(sandbox) == [("phase/suite/2", 1, second), ("phase/suite/4", 1, third)]
    out = _lander(sandbox)
    assert "opened the pull request for ship" in out, out
    pull = forge_repo.pull(1)
    assert (pull["state"], pull["head"], pull["base"], pull["sha"]) == ("open", INIT, "main", third), pull
    body = str(forge_repo.api("GET", "/pulls/1")["body"])  # type: ignore[index]
    assert f"the initiative review approved it in round 4 and the suite passed at {third[:12]}." in body
    assert f"The full suite passed at `{third}`." in body


def _stale_body(sandbox, forge_repo, passed: str, head: str) -> None:
    pull = forge_repo.api("GET", "/pulls/1")
    assert isinstance(pull, dict) and str(pull["title"]).startswith("WIP: "), pull
    assert f"The full suite last passed at `{passed}`; the head is at `{head}`, ahead of it." in str(pull["body"]), pull
    assert _record(sandbox, INITIATIVE)["pr"]["stale"] is True


@pytest.mark.forge
def test_an_open_pull_request_whose_head_moves_is_a_draft_that_cannot_land_until_the_suite_passes_there(forge_review_machine):
    sandbox, name, env, launchable, forge_repo = forge_review_machine
    clone = forge_repo.path
    both = {"patch": plan.new_file_patch("notes/b.txt", "b") + plan.new_file_patch("notes/fix.txt", "fix"),
            "summary": "built", "files_touched": ["notes/b.txt", "notes/fix.txt"], "commands_run": []}
    launchable.scripted({**_plan_answers(p1_review_1=APPROVE, p2_review_1=APPROVE, initiative_review_1=APPROVE,
                                         initiative_review_3=APPROVE), "build@b": both})
    first = _phases_built_and_merged(sandbox, env, name, clone)
    _queued_review(sandbox)
    out = _claim_once(sandbox, env, name)
    assert f"finished approved: the initiative review approved it in round 1 and the suite passed at {first[:12]}" in out, out
    out = _lander(sandbox)
    assert "opened the pull request for ship" in out, out
    pull = forge_repo.pull(1)
    assert (pull["state"], pull["sha"]) == ("open", first), pull
    assert _record(sandbox, INITIATIVE)["pr"]["passed"] == first

    # main moves, and the lander now merges the pull request itself: it takes main forward and pushes the head first.
    sandbox.git(clone, "checkout", "--quiet", "main")
    (clone / "elsewhere.txt").write_text("another change on main\n", encoding="utf-8")
    sandbox.git(clone, "add", "elsewhere.txt")
    sandbox.git(clone, "commit", "--quiet", "-m", "another change lands on main")
    sandbox.git(clone, "push", "--quiet", "origin", "main")
    _gate(sandbox, "full")
    out = _lander(sandbox)
    second = _origin_tip(sandbox, clone, INIT)
    assert second != first and "after the suite passed" in out, out
    assert forge_repo.pull(1)["sha"] == second and forge_repo.pull(1)["state"] == "open", forge_repo.pull(1)
    assert f"ship: the pull request is a draft: its head {second[:12]} has no passing suite" in out, out
    _stale_body(sandbox, forge_repo, first, second)

    # Stale, it cannot land: the forge refuses an operator's merge, and the lander's own merge waits for the suite.
    status, _ = forge_repo.forge.api("POST", f"/repos/{forge_repo.owner}/{forge_repo.name}/pulls/1/merge", {"Do": "squash"})
    assert status >= 400 and forge_repo.pull(1)["state"] == "open", status
    out = _lander(sandbox)
    assert f"the suite has not run at {second[:12]}" in out and forge_repo.pull(1)["state"] == "open", out
    _stale_body(sandbox, forge_repo, first, second)

    # The chair reopens the review with the pull request open; the suite runs at the new head; it is ready and lands.
    _reopened(sandbox, second)
    out = _claim_once(sandbox, env, name)
    assert f"finished approved: the initiative review approved it in round 3 and the suite passed at {second[:12]}" in out, out
    assert _suites(sandbox) == [("phase/suite/1", 1, first), ("phase/suite/3", 1, second)]
    out = _lander(sandbox)
    assert f"ship: the pull request is ready: the suite passed at its head {second[:12]}" in out, out
    pull = forge_repo.pull(1)
    assert (pull["state"], pull["merged"]) == ("closed", True), (out, pull)
    assert _record(sandbox, INITIATIVE)["state"] == "landed"
    assert forge_repo.file_text("main", "elsewhere.txt") == "another change on main\n"
