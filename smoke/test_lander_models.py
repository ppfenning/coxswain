"""D1: one lander pass handles both lane models. Real `cox route import`, `cox lander adopt --apply` and `cox lander once`
against real clones of real bare origins and the store, with the local forge.

Three initiatives in one store, each in a repository of its own, and in each two phases that edit the same line of the
same file, so the second phase's merge into the initiative branch conflicts:

- `fresh` is on the new model (`lane_model: phase` on its `initiative.md`, stamped on its landing record by adopt). Its
  phases are built on another clone, as the phase lane builds them on a worker, and pushed to origin: the lander's
  clone has no copy of them until it fetches. Its conflict goes to the merge agent, L5's `merge_tasks` seat, run
  through the local harness: the provider profile's runner is the scripted one, as in `smoke/test_lane_merge.py`. The
  seat keeps both phases' lines, the answer passes the guards and the level checks, and origin's initiative branch
  holds both phases.
- `dropped` is on the new model too, but the scripted seat keeps only p2's line: L5's `lost_lines` guard refuses it,
  and the initiative holds with needs_chair. A refusal is not retried on its own; `cox lander release` tries it again.
- `legacy` is on the old model (no `lane_model`). Its conflict is committed, markers and all, on the phase branch,
  and a rectify task is filed, exactly as before D1.
"""

from __future__ import annotations

import json
from pathlib import Path

from smoke import plan
from smoke.test_lander_lanes import _gate, _git, _origin_git, _record, _repository

LINE = "one\ntwo\nthree\n"
EDITS = {"p1": "one\nTWO from p1\nthree\n", "p2": "one\nTWO from p2\nthree\n"}
BOTH = "one\nTWO from p1\nTWO from p2\nthree\n"
SEAT = {"merge_tasks": "smoke-skills:merge-tasks"}


def _merged(text: str) -> dict[str, object]:
    return {"resolved": True, "files": [{"path": "shared.txt", "content": text}], "summary": "kept both phases' lines"}


def _profile_with_merge_seat(sandbox, tmp_path: Path) -> None:
    """A team cartridge with the ticket gate of `test_lander_lanes` and the `merge_tasks` seat bound to a smoke skill,
    and a provider profile whose runner is the scripted one, answering the seat for `fresh` and `dropped`."""
    cartridges, skills = tmp_path / "cartridges", tmp_path / "skills"
    (cartridges / "smoke").mkdir(parents=True)
    (cartridges / "smoke" / "cartridge.yaml").write_text(
        "team: smoke\nversion: 1\nlanding_areas:\n  ticket_checks:\n    - name: gate\n      cmd: sh ./gate.sh\n"
        f"skills:\n  merge_tasks: {SEAT['merge_tasks']}\n", encoding="utf-8")
    for relative, text in plan.skill_plugin_files(SEAT).items():
        (skills / relative).parent.mkdir(parents=True, exist_ok=True)
        (skills / relative).write_text(text, encoding="utf-8")
    answers = sandbox.home / "scripted.json"
    answers.write_text(json.dumps({"merge_tasks@fresh/p2": _merged(BOTH),
                                   "merge_tasks@dropped/p2": _merged(EDITS["p2"])}), encoding="utf-8")
    config = sandbox.home / ".config" / "agent-tools"
    (config / "provider.yaml").write_text(plan.scripted_provider_profile_text(sandbox.store_url, str(answers)),
                                          encoding="utf-8")
    profile = config / "profile.yaml"
    profile.write_text(profile.read_text(encoding="utf-8") + f"cartridges_dir: {cartridges}\nskills_roots: [{skills}]\n",
                       encoding="utf-8")
    _gate(sandbox, "ticket")


def _files(sandbox, initiative: str, name: str, tips: dict[str, str], *, new_model: bool) -> None:
    """The initiative and one task per phase, approved at its tip once `tips` names one, imported into the store."""
    own = {"title": initiative, "repo": f"~/src/{name}", "draft": "false", **({"lane_model": "phase"} if new_model else {})}
    sandbox.write(f"work/{initiative}/initiative.md", plan.work_item_text(own, "An initiative."))
    for phase in ("p1", "p2"):
        tip = tips.get(phase, "")
        fields = {"id": f"{initiative}-{phase}", "title": f"Edit the line in {phase}", "phase": phase,
                  "state": "approved" if tip else "ready", **({"merged_commit": tip} if tip else {})}
        sandbox.write(f"work/{initiative}/{phase}/{initiative}-{phase}.md", plan.work_item_text(fields, "Task body"))
    imported = sandbox.cox("route", "import")
    assert imported.returncode == 0, imported.stdout + imported.stderr


def _commit(sandbox, where: Path, branch: str, start: str, text: str) -> str:
    _git(sandbox, where, "checkout", "--quiet", "-B", branch, start)
    (where / "shared.txt").write_text(text, encoding="utf-8")
    _git(sandbox, where, "add", "shared.txt")
    _git(sandbox, where, "commit", "--quiet", "-m", f"{branch}: edit the line")
    tip = _git(sandbox, where, "rev-parse", "HEAD")
    _git(sandbox, where, "checkout", "--quiet", "main")
    return tip


def _repo_with_line(sandbox, tmp_path: Path, name: str) -> tuple[Path, Path]:
    repo, origin = _repository(sandbox, tmp_path, name, "exit 0\n")
    (repo / "shared.txt").write_text(LINE, encoding="utf-8")
    _git(sandbox, repo, "add", "shared.txt")
    _git(sandbox, repo, "commit", "--quiet", "-m", "the shared line")
    _git(sandbox, repo, "push", "--quiet", "origin", "main")
    return repo, origin


def _origin_has(sandbox, origin: Path, ref: str) -> str:
    done = _origin_git(sandbox, origin, "rev-parse", "--verify", "--quiet", ref)
    return done.stdout.strip() if done.returncode == 0 else ""


def _holds(sandbox, origin: Path, sha: str, tip: str) -> bool:
    return _origin_git(sandbox, origin, "merge-base", "--is-ancestor", sha, tip).returncode == 0


def _built_on_a_worker(sandbox, tmp_path: Path, initiative: str, origin: Path) -> dict[str, str]:
    """The new model's phases: built on a worker's clone and pushed to origin, never in the lander's clone."""
    worker = tmp_path / f"worker-{initiative}"
    _git(sandbox, tmp_path, "clone", "--quiet", str(origin), str(worker))
    tips = {p: _commit(sandbox, worker, f"epic/{initiative}/{p}", "origin/main", text) for p, text in EDITS.items()}
    for phase in EDITS:
        _git(sandbox, worker, "push", "--quiet", "origin", f"epic/{initiative}/{phase}")
    return tips


def test_one_pass_merges_new_model_conflicts_through_the_merge_seat_and_sends_an_old_model_one_to_rectify(sandbox, tmp_path):
    _profile_with_merge_seat(sandbox, tmp_path)
    fresh, fresh_origin = _repo_with_line(sandbox, tmp_path, "fresh")
    _, dropped_origin = _repo_with_line(sandbox, tmp_path, "dropped")
    legacy, _ = _repo_with_line(sandbox, tmp_path, "legacy")
    for initiative, new_model in (("fresh", True), ("dropped", True), ("legacy", False)):
        _files(sandbox, initiative, initiative, {}, new_model=new_model)
    for initiative in ("fresh", "dropped", "legacy"):  # the records, before anything is built: adopt merges nothing yet
        adopted = sandbox.cox("lander", "adopt", "--apply", initiative)
        assert adopted.returncode == 0 and f"{initiative}: record made" in adopted.stdout, adopted.stdout + adopted.stderr
    assert _record(sandbox, "fresh")["lane_model"] == "phase" and _record(sandbox, "dropped")["lane_model"] == "phase"
    assert "lane_model" not in _record(sandbox, "legacy")

    new_tips = {i: _built_on_a_worker(sandbox, tmp_path, i, o) for i, o in (("fresh", fresh_origin),
                                                                           ("dropped", dropped_origin))}
    # The old model's phases: on the lander's own clone, as an old-model build run leaves them.
    old_tips = {p: _commit(sandbox, legacy, f"epic/legacy/{p}", "main", text) for p, text in EDITS.items()}
    _files(sandbox, "fresh", "fresh", new_tips["fresh"], new_model=True)
    _files(sandbox, "dropped", "dropped", new_tips["dropped"], new_model=True)
    _files(sandbox, "legacy", "legacy", old_tips, new_model=False)

    done = sandbox.cox("lander", "once")

    out = done.stdout + done.stderr
    assert done.returncode == 1, out  # `once` exits 1 when an item failed: dropped's and legacy's p2 merges did
    for initiative in ("fresh", "dropped", "legacy"):
        assert f"phase p1 of {initiative} merged into init/{initiative}" in out, out
    # New model, the seat keeps both lines: p2 rolls up through it, and origin's initiative branch holds both phases.
    assert "phase p2 of fresh merged into init/fresh" in out, out
    assert "its conflict in init/fresh was resolved by the merge_tasks seat" in out, out
    merged = _origin_has(sandbox, fresh_origin, "refs/heads/init/fresh")
    assert merged and all(_holds(sandbox, fresh_origin, new_tips["fresh"][p], merged) for p in EDITS), "both phases"
    assert _origin_git(sandbox, fresh_origin, "show", f"{merged}:shared.txt").stdout == BOTH
    assert not _record(sandbox, "fresh").get("held")
    assert _origin_has(sandbox, fresh_origin, "refs/heads/epic/fresh/p2") == new_tips["fresh"]["p2"], "never written"
    assert not (sandbox.workspace / "work" / "fresh" / "p2" / "p2-rectify.md").exists(), "no rectify task on the new model"
    # New model, the seat drops p1's line: `lost_lines` refuses it, the initiative holds, and the chair is told.
    held = _record(sandbox, "dropped")["held"]
    assert held["reason"].startswith(
        f"the lander could not merge new-model phase p2 at {new_tips['dropped']['p2'][:12]}: conflict in shared.txt"), held
    assert "dropped lines a side added in shared.txt" in held["reason"] and "TWO from p1" in held["reason"], held
    assert (held["cause"], held["attempts"], held["phase"]) == ("refused", 1, "p2"), "a refusal waits for a person"
    assert not (sandbox.workspace / "work" / "dropped" / "p2" / "p2-rectify.md").exists()
    tip = _origin_has(sandbox, dropped_origin, "refs/heads/init/dropped")
    assert _holds(sandbox, dropped_origin, new_tips["dropped"]["p1"], tip)
    assert not _holds(sandbox, dropped_origin, new_tips["dropped"]["p2"], tip), "nothing merged that lost a line"
    # Old model, same pass: the conflict committed on the phase branch for a rectify task, which is filed.
    assert (sandbox.workspace / "work" / "legacy" / "p2" / "p2-rectify.md").is_file(), out
    assert "<<<<<<<" in _git(sandbox, legacy, "show", "epic/legacy/p2:shared.txt")
    assert "lane_model" not in _record(sandbox, "legacy") and not _record(sandbox, "legacy").get("held")
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.returncode == 0 and inbox.stdout.count("the lander could not merge new-model phase p2") == 1, inbox.stdout

    again = sandbox.cox("lander", "once")
    assert "phase_merge:dropped:p2: waiting: dropped is held" in again.stdout, again.stdout + again.stderr
    assert _record(sandbox, "dropped")["held"] == held, "a refused merge is not tried again on its own"
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.stdout.count("the lander could not merge new-model phase p2") == 1, "and the chair is told once"

    # An operator releases the hold (`cox lander release`): the next pass tries the merge again, and says how it went.
    refused = sandbox.cox("lander", "release", "legacy")
    assert refused.returncode == 1 and "legacy is not held" in refused.stdout, refused.stdout + refused.stderr
    released = sandbox.cox("lander", "release", "dropped")
    assert released.returncode == 0 and "the merge hold on phase p2 is released" in released.stdout, released.stdout + released.stderr
    retried = sandbox.cox("lander", "once")
    assert "an operator released it" in retried.stdout, retried.stdout + retried.stderr
    again_held = _record(sandbox, "dropped")["held"]
    assert (again_held["attempts"], again_held["tip"], again_held.get("released")) == (
        2, new_tips["dropped"]["p2"], None), again_held
    inbox = sandbox.cox("courier", "inbox", "--label", "chair")
    assert inbox.stdout.count("the lander could not merge new-model phase p2") == 2, "the release's answer is told"
