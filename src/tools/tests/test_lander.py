"""The lander end to end: a real repo, a real store, a fake forge. No phase ever gets a pull request."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent_tools import lander
from core import landing_record as lr
from harness import landing_git as lg
from harness import landing_ops as ops
from harness import landing_store as ls
from harness.store_dialect import connect
from harness.store_migrate import default_modules, migrate

ID = ("-c", "user.email=t@invalid", "-c", "user.name=t")


def run(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *ID, *args], capture_output=True, text=True, check=True).stdout.strip()


def commit(repo: Path, branch: str, name: str, text: str) -> str:
    if run(repo, "branch", "--show-current") == branch:
        (repo / name).write_text(text)
        run(repo, "add", "--all")
        run(repo, "commit", "-m", f"{branch}: {name}")
        return run(repo, "rev-parse", "HEAD")
    wt = repo.parent / f"wt-{branch.replace('/', '_')}"
    run(repo, "worktree", "add", str(wt), branch)
    try:
        (wt / name).write_text(text)
        run(wt, "add", "--all")
        run(wt, "commit", "-m", f"{branch}: {name}")
        return run(wt, "rev-parse", "HEAD")
    finally:
        run(repo, "worktree", "remove", "--force", str(wt))


class Forge:
    """A forge that records every call and really squash-merges, so `main` moves as it would."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.prs: dict[str, dict] = {}
        self.checks: tuple[bool, str] = (True, "all checks passed")

    def push(self, repo, branch):
        self.calls.append(("push", branch))
        return True, ""

    def find_open_prs(self, repo, branch):
        pr = self.prs.get(branch)
        return [pr["url"]] if pr and pr["state"] == "open" else []

    def open_pr(self, repo, title, body, *, head, base):
        url = f"https://forge.example/pull/{len(self.prs) + 1}"
        self.prs[head] = {"url": url, "state": "open", "title": title, "body": body, "base": base}
        self.calls.append(("pr_create", head, base, title))
        return True, url

    def pr_merged(self, repo, branch):
        return bool(self.prs.get(branch, {}).get("state") == "merged"), ""

    def wait_checks(self, repo, timeout, ref):
        self.calls.append(("wait_checks", ref))
        return self.checks

    def merge(self, repo, step):
        self.calls.append(("merge", step["branch"]))
        self.person_merges(repo, step["branch"])
        return True, ""

    def person_merges(self, repo, branch):
        run(repo, "merge", "--squash", branch)
        run(repo, "commit", "-m", f"squash {branch}")
        self.prs[branch]["state"] = "merged"

    def delete_branch(self, repo, branch):
        self.calls.append(("delete_remote", branch))
        return True, ""

    def kinds(self) -> list[str]:
        return [c[0] for c in self.calls]


class World:
    def __init__(self, tmp_path: Path, cartridge: dict | None = None, gate: str = "full") -> None:
        self.repo = tmp_path / "repo"
        self.repo.mkdir()
        run(self.repo, "init", "-b", "main")
        (self.repo / "base.txt").write_text("base\n")
        (self.repo / "shared.txt").write_text("one\ntwo\nthree\n")
        run(self.repo, "add", "--all")
        run(self.repo, "commit", "-m", "base")
        self.conn = connect(f"sqlite:///{tmp_path / 'store.db'}")
        migrate(self.conn, "2026-11-12T00:00:00Z", default_modules())
        self.forge = Forge()
        self.rows: dict[str, list[dict]] = {}
        self.cartridge = cartridge or {}
        self.gate = gate
        self.meta_of: dict[str, dict] = {}
        self.notes: list[tuple] = []
        self.rectified: list[tuple] = []
        self.splits: list[tuple] = []
        self.logged: list[str] = []
        self.clock = 0
        self.fail_mark_done = 0
        self.check_result: dict[str, tuple[bool, str]] = {}
        self.ports = lander.Ports(
            conn=self.conn, worker="lander-1", now=self.now, tasks=lambda i: [dict(r) for r in self.rows.get(i, [])],
            meta=self.meta, forge=self.forge, checks=self.checks, mark_merged=lambda i, t, tip: None,
            mark_done=self.mark_done, file_rectify=self.file_rectify, split_blocked=self.split,
            notify=lambda topic, payload: self.notes.append((topic, dict(payload))), log=self.logged.append,
        )

    def now(self) -> str:
        self.clock += 1
        return f"2026-11-12T{10 + self.clock // 3600:02d}:{self.clock % 3600 // 60:02d}:{self.clock % 60:02d}Z"

    def meta(self, initiative: str) -> dict:
        own = {"id": initiative, "title": f"Title of {initiative}", **self.meta_of.get(initiative, {})}
        return {"initiative": own, "cartridge": self.cartridge, "gate": self.gate, "epic_issue": None}

    def checks(self, repo, tree, gate):
        return self.check_result.get(gate, (True, "ok"))

    def mark_done(self, initiative, task, pr):
        if self.fail_mark_done:
            self.fail_mark_done -= 1
            raise RuntimeError("the store went away")
        for row in self.rows[initiative]:
            if row["id"] == task:
                row["state"], row["pr"] = "done", pr
        return

    def file_rectify(self, initiative, phase, title, body):
        task = f"{phase}-rectify-{len(self.rectified) + 1}"
        self.rectified.append((initiative, phase, title, body))
        self.rows[initiative].append({"id": task, "phase": phase, "state": "ready", "needs": [], "title": title})
        return task

    def split(self, initiative, phases):
        self.splits.append((initiative, list(phases)))
        self.rows[initiative] = [r for r in self.rows[initiative] if r["phase"] not in phases]
        return f"{initiative}-2"

    def start(self, initiative: str, tasks: list[dict], branch: str | None = None) -> dict:
        self.rows[initiative] = tasks
        record, why = ops.start_record(self.conn, self.repo, initiative=initiative, branch=branch or f"init/{initiative}",
                                       parent="main", parent_ref="main", now=self.now())
        assert why == "" and record is not None
        return record

    def build(self, initiative: str, phase: str, name: str | None = None, text: str | None = None) -> str:
        """What a build run leaves: the phase branch cut from the initiative tip, one commit, its tasks approved."""
        record = ls.get_record(self.conn, initiative)
        branch = ops.phase_branch(initiative, phase)
        if lg.rev(self.repo, f"refs/heads/{branch}") is None:
            run(self.repo, "branch", branch, record["branch"])
        tip = commit(self.repo, branch, name or f"{initiative}-{phase}.txt", text or f"{phase}\n")
        for row in self.rows[initiative]:
            if row["phase"] == phase and row["state"] in ("ready", "todo"):
                row["state"] = "approved"
        return tip

    def record(self, initiative: str) -> dict:
        return ls.get_record(self.conn, initiative)


def task(ident: str, phase: str, state: str = "ready", needs: tuple[str, ...] = ()) -> dict:
    return {"id": ident, "phase": phase, "state": state, "needs": list(needs), "title": f"Task {ident}"}


THREE_WAVES = lambda: [  # noqa: E731
    task("a", "p1"), task("b", "p2"), task("c", "p3", needs=("a", "b")), task("d", "p4", needs=("c",)),
]


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def test_four_phases_in_three_waves_give_exactly_one_pull_request_from_the_initiative_branch(world: World) -> None:
    world.start("i", THREE_WAVES())
    tips = {}
    for wave in (["p1", "p2"], ["p3"], ["p4"]):
        assert lr.placeable_phases(world.record("i"), world.rows["i"]) == wave, "a wave opens only when its parents merged"
        for phase in wave:
            tips[phase] = world.build("i", phase)
        lander.run_once(world.ports)
    creates = [c for c in world.forge.calls if c[0] == "pr_create"]
    assert creates == [("pr_create", "init/i", "main", "Title of i")]
    assert all("p1" not in str(c) and "epic/" not in str(c) for c in world.forge.calls if c[0] in ("pr_create", "push", "merge"))
    assert [c for c in world.forge.calls if c[0] == "push"] == [("push", "init/i")], "pushed once, when the pull request opens"
    assert {r["id"]: r["state"] for r in world.rows["i"]} == {"a": "done", "b": "done", "c": "done", "d": "done"}
    assert all(r["pr"] == "https://forge.example/pull/1" for r in world.rows["i"])
    record = world.record("i")
    assert record["state"] == "landed" and record["landed_pr"] == "https://forge.example/pull/1"
    assert [w["state"] for w in lr.wave_states(record, world.rows["i"])] == ["green", "green", "green"]
    assert run(world.repo, "ls-tree", "--name-only", "main").split() == [
        "base.txt", "i-p1.txt", "i-p2.txt", "i-p3.txt", "i-p4.txt", "shared.txt",
    ]
    assert run(world.repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/") == "main", "its branches are cleaned"
    body = world.forge.prs["init/i"]["body"]
    assert "### p1" in body and "Task d (`d`)" in body
    assert [i["state"] for i in ls.list_items(world.conn)] == ["done"] * 5


def test_a_wave_two_phase_is_cut_from_a_tip_that_holds_wave_one(world: World) -> None:
    world.start("i", [task("a", "p1"), task("c", "p3", needs=("a",))])
    p1_tip = world.build("i", "p1")
    lander.run_once(world.ports)
    world.build("i", "p3")
    assert lg.is_ancestor(world.repo, p1_tip, "epic/i/p3")


def test_a_land_that_dies_after_the_merge_finishes_cleanly_when_it_runs_again(world: World) -> None:
    world.start("i", [task("a", "p1"), task("b", "p1")])
    world.build("i", "p1")
    world.fail_mark_done = 1
    with pytest.raises(RuntimeError, match="the store went away"):
        lander.run_once(world.ports)
    assert world.forge.prs["init/i"]["state"] == "merged"
    assert world.record("i")["state"] == "pr_open", "the merge happened; the bookkeeping did not"
    world.clock += 4000  # the dead worker's claim lapses
    lander.run_once(world.ports)
    assert {r["state"] for r in world.rows["i"]} == {"done"}
    assert world.record("i")["state"] == "landed"
    assert world.forge.kinds().count("pr_create") == 1 and world.forge.kinds().count("merge") == 1
    lander.run_once(world.ports)
    assert world.forge.kinds().count("pr_create") == 1, "a third pass finds nothing to do"


def test_behind_a_person_gate_the_pull_request_opens_and_waits_and_nothing_merges(tmp_path: Path) -> None:
    world = World(tmp_path, gate="ticket")
    world.start("i", [task("a", "p1")])
    world.build("i", "p1")
    outcomes = lander.run_once(world.ports)
    assert outcomes[-1].status == "waiting" and lander.PERSON_GATE_NOTE in outcomes[-1].detail
    assert "merge" not in world.forge.kinds() and "wait_checks" not in world.forge.kinds()
    assert world.record("i")["state"] == "pr_open" and world.rows["i"][0]["state"] == "approved"
    lander.run_once(world.ports)
    assert world.forge.kinds().count("pr_create") == 1 and world.forge.kinds().count("push") == 1
    world.forge.person_merges(world.repo, "init/i")
    lander.run_once(world.ports)
    assert world.rows["i"][0]["state"] == "done" and world.record("i")["state"] == "landed"
    assert "merge" not in world.forge.kinds(), "the lander never merged: a person did"


def test_checks_still_running_leave_the_land_queued_and_red_checks_send_it_back(world: World) -> None:
    world.start("i", [task("a", "p1")])
    world.build("i", "p1")
    world.forge.checks = (False, "pending: test")
    assert lander.run_once(world.ports)[-1].status == "waiting"
    assert ls.get_item(world.conn, "initiative_land:i")["state"] == "queued"
    world.forge.checks = (False, "failing checks: test")
    out = lander.run_once(world.ports)[-1]
    assert out.status == "failed" and "checks failed" in out.detail
    assert world.rectified[-1][1] == lr.INTEGRATE_PHASE
    assert world.record("i")["state"] == "conflicted"
    assert "merge" not in world.forge.kinds()


def test_two_phases_editing_one_line_a_rectify_task_is_filed_built_and_the_phase_merges(world: World) -> None:
    world.start("i", [task("a", "p1"), task("b", "p2")])
    world.build("i", "p1", "shared.txt", "one\nTWO from p1\nthree\n")
    p2_work = world.build("i", "p2", "shared.txt", "one\nTWO from p2\nthree\n")
    outcomes = lander.run_once(world.ports)
    assert [o.status for o in outcomes] == ["done", "failed"]
    [(initiative, phase, title, body)] = world.rectified
    assert (initiative, phase) == ("i", "p2") and "resolve its merge with init/i" in title
    assert "`shared.txt`" in body and "do not rebuild" in body
    assert "<<<<<<<" in run(world.repo, "show", "epic/i/p2:shared.txt"), "the conflict is in the tree the task starts from"
    assert lg.is_ancestor(world.repo, p2_work, "epic/i/p2"), "the phase's approved work is still on its branch"
    assert lr.phase_states(world.record("i"), world.rows["i"])["p2"] == "conflicted"
    assert lr.placeable_phases(world.record("i"), world.rows["i"]) == ["p2"]
    world.build("i", "p2", "shared.txt", "one\nTWO from both\nthree\n")  # the stub builder's approved patch
    lander.run_once(world.ports)
    assert run(world.repo, "show", "main:shared.txt") == "one\nTWO from both\nthree"
    assert {r["state"] for r in world.rows["i"]} == {"done"}
    assert world.forge.kinds().count("pr_create") == 1


def test_red_level_checks_on_the_merged_tree_file_a_rectify_task_with_the_output(world: World) -> None:
    world.start("i", [task("a", "p1"), task("b", "p2")])
    world.build("i", "p1")
    world.build("i", "p2")
    world.check_result["level"] = (False, "tests/test_x.py::test_y FAILED")
    outcomes = lander.run_once(world.ports)
    assert [o.status for o in outcomes] == ["done", "failed"], "p1 holds the tip already; p2 joins p1's work and is checked"
    [(_, phase, title, body)] = world.rectified
    assert phase == "p2" and "checks fail once merged" in title and "test_y FAILED" in body
    assert lg.is_ancestor(world.repo, "init/i", "epic/i/p2"), "the rectify task builds on the merged tree"


def test_a_conflict_with_the_parent_goes_back_to_build_in_an_integrate_phase_and_then_lands(world: World) -> None:
    world.start("i", [task("a", "p1")])
    world.build("i", "p1", "shared.txt", "one\nTWO from the initiative\nthree\n")
    commit(world.repo, "main", "shared.txt", "one\nTWO from main\nthree\n")
    out = lander.run_once(world.ports)[-1]
    assert out.status == "failed" and "conflicted in shared.txt" in out.detail
    assert world.forge.calls == [], "nothing reached the forge"
    [(_, phase, title, _body)] = world.rectified
    assert phase == "integrate" and title == "Integrate with main: resolve the conflicts"
    assert "<<<<<<<" in run(world.repo, "show", "epic/i/integrate:shared.txt")
    assert lr.initiative_state(world.record("i"), world.rows["i"]) == "conflicted"
    world.build("i", "integrate", "shared.txt", "one\nTWO from both\nthree\n")
    lander.run_once(world.ports)
    assert run(world.repo, "show", "main:shared.txt") == "one\nTWO from both\nthree"
    assert world.record("i")["state"] == "landed" and world.forge.kinds().count("pr_create") == 1


def _aim_shape() -> list[dict]:
    """Three built phases in a chain, and a fourth whose one task is quarantined behind a human gate."""
    return [task("a", "p1"), task("b", "p2", needs=("a",)), task("c", "p3", needs=("b",)),
            task("gate", "p4", "quarantined", needs=("c",))]


def test_with_split_the_built_phases_land_and_the_blocked_phase_moves_to_a_follow_up(tmp_path: Path) -> None:
    world = World(tmp_path, cartridge={"landing_areas": {"on_blocked": "split"}})
    world.start("aim", _aim_shape())
    for phase in ("p1", "p2", "p3"):
        world.build("aim", phase)
        lander.run_once(world.ports)
    assert world.splits == [("aim", ["p4"])]
    assert {r["id"]: r["state"] for r in world.rows["aim"]} == {"a": "done", "b": "done", "c": "done"}
    assert world.record("aim")["state"] == "landed" and world.forge.kinds().count("pr_create") == 1


def test_with_hold_the_built_phases_merge_and_free_their_files_and_the_operator_is_told_once(world: World) -> None:
    world.start("aim", _aim_shape())
    for phase in ("p1", "p2", "p3"):
        world.build("aim", phase)
        lander.run_once(world.ports)
    lander.run_once(world.ports)
    states = lr.phase_states(world.record("aim"), world.rows["aim"])
    assert states == {"p1": "merged", "p2": "merged", "p3": "merged", "p4": "blocked"}
    assert lr.merged_tasks(world.record("aim"), world.rows["aim"]) == ["a", "b", "c"], "merged work holds no files"
    assert [n[0] for n in world.notes] == ["blocked"], "told once, not every pass"
    assert world.notes[0][1]["choices"][0].startswith("split")
    assert world.forge.calls == [] and world.splits == []


def test_split_is_refused_and_held_when_a_merged_phase_depends_on_the_blocked_one(tmp_path: Path) -> None:
    world = World(tmp_path, cartridge={"landing_areas": {"on_blocked": "split"}})
    world.start("i", [task("a", "p1"), task("b", "p2", needs=("a",))])
    world.build("i", "p1")
    lander.run_once(world.ports)
    world.build("i", "p2")
    lander.run_once(world.ports)
    world.rows["i"] = [task("a", "p1", "quarantined"), task("b", "p2", "approved", needs=("a",))]
    ls.put_record(world.conn, {**world.record("i"), "state": "building",
                               "phases": {"p2": world.record("i")["phases"]["p2"]}})
    lander.sweep(world.ports)
    assert world.splits == []
    assert "merged phase(s) p2 depend on blocked phase p1" in world.notes[-1][1]["reason"]


def test_a_second_initiative_waits_while_another_is_landing_on_the_same_parent(world: World) -> None:
    world.start("i", [task("a", "p1")])
    world.start("j", [task("x", "p1")])
    world.build("i", "p1")
    world.build("j", "p1")
    for name in ("i", "j"):
        lander.merge_phase(world.ports, name, "p1")
    assert ls.claim(world.conn, "initiative_land:i", "lander-other", now=world.now(), lease_until="2026-11-12T23:00:00Z")
    out = lander.land_initiative(world.ports, "j")
    assert (out.status, out.detail) == ("waiting", "i is landing on main first")
    assert ls.get_item(world.conn, "initiative_land:j")["state"] == "queued"


def test_the_sweep_queues_what_an_event_missed_and_does_no_work_itself(world: World) -> None:
    world.start("i", [task("a", "p1")])
    tip = world.build("i", "p1")
    events = lander.sweep(world.ports)
    assert events == ["sweep: queued phase p1 of i"]
    assert ls.get_item(world.conn, "phase_merge:i:p1")["state"] == "queued"
    assert not lg.is_ancestor(world.repo, tip, "init/i"), "the sweep merged nothing"
    assert lander.sweep(world.ports) == [], "queued once"


def test_the_caps_are_separate_a_full_land_cap_does_not_delay_a_phase_merge(world: World) -> None:
    world.start("i", [task("a", "p1"), task("b", "p2")])
    world.build("i", "p1")
    world.build("i", "p2")
    outcomes = lander.run_once(world.ports, phase_cap=1, land_cap=0)
    assert [(o.item, o.status) for o in outcomes] == [("phase_merge:i:p1", "done")]
    outcomes = lander.run_once(world.ports, phase_cap=2, land_cap=0)
    assert [(o.item, o.status) for o in outcomes] == [("phase_merge:i:p2", "done")]
    assert world.forge.calls == [] and ls.get_item(world.conn, "initiative_land:i")["state"] == "queued"


# -- the work setup's contract, part two: what reaches the forge ----------------------------------------------------

WORK = {"landing_areas": {"land_at": "ticket", "target_branch": "main",
                          "initiative_pr": {"branch": "{kind}/{issue}-{name}", "kinds": {"main": "fix", "develop": "feature"},
                                            "reference": "Closes #{issue}"}}}


@pytest.mark.parametrize("phases", [["p1"], ["p1", "p2", "p3"]])
def test_work_contract_one_pull_request_per_initiative_nothing_per_phase_and_no_merge_without_the_gate(tmp_path: Path, phases) -> None:
    world = World(tmp_path, cartridge=WORK, gate="ticket")
    for number, name in ((412, "export-csv"), (413, "import-csv")):  # two initiatives of one epic
        world.meta_of[name] = {"issue": str(number), "epic": "csv"}
        rows = [task(f"{name}-{p}", p, needs=((f"{name}-{phases[i - 1]}",) if i else ())) for i, p in enumerate(phases)]
        world.start(name, rows, branch=f"fix/{number}-{name}")
        for phase in phases:
            world.build(name, phase)
            lander.run_once(world.ports)
    creates = [c for c in world.forge.calls if c[0] == "pr_create"]
    assert [c[1] for c in creates] == ["fix/412-export-csv", "fix/413-import-csv"], "exactly one pull request per initiative"
    assert all(c[2] == "main" for c in creates)
    for call in world.forge.calls:
        assert call[0] in ("pr_create", "push"), f"unexpected forge call {call}"
        assert not any(p in str(call[1:]) for p in ("epic/", *phases)), f"a phase reached the forge: {call}"
    assert sorted(c[1] for c in world.forge.calls if c[0] == "push") == ["fix/412-export-csv", "fix/413-import-csv"]
    assert "Closes #412" in world.forge.prs["fix/412-export-csv"]["body"]
    assert "merge" not in world.forge.kinds(), "the merge waits for a person"
    assert all(r["state"] == "approved" for name in ("export-csv", "import-csv") for r in world.rows[name])
    assert {world.record(n)["state"] for n in ("export-csv", "import-csv")} == {"pr_open"}
