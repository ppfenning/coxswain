"""The epic run records land_at on every phase and opens a review PR only at that granularity."""

from __future__ import annotations

import pytest

from core import ledger
from harness import epic as epic_module
from harness.epic import EXIT_AWAITING_LAND, build_outcome, opens_pr
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    GOAL_UNMET,
    Runner,
    cart,
    drive,
    git,
    initiative,
    new_file_patch,
    repo,
)

TASKS = {"t1-probe", "t2-bench", "t3-cutover"}
LEVELS = {"p1-foundations", "p2-rollout"}
LEVELS_IN_ORDER = ("p1-foundations", "p2-rollout")


class Forge(list):
    """The heads of the PRs opened so far. `down` makes PR creation fail; `unreachable` makes the lookup fail."""

    down = False
    unreachable = False

    def __init__(self) -> None:
        super().__init__()
        self.bases: dict[str, str] = {}


@pytest.fixture
def prs(monkeypatch) -> Forge:
    """A fake forge: nothing is pushed and `gh` never runs. It lists every PR the run opens, by head branch."""
    forge = Forge()

    def open_pr(ctx, *, title, body, head, base):
        if forge.down:
            return False, "forge down"
        forge.append(head)
        forge.bases[head] = base
        return True, f"pr:{head}"

    def existing(ctx, head):
        if forge.unreachable:
            return False, "auth error"
        return True, (f"pr:{head}" if head in forge else "")

    monkeypatch.setattr(epic_module, "_push_review_ref", lambda ctx, local, name: (True, ""))
    monkeypatch.setattr(epic_module, "_existing_pr", existing)
    monkeypatch.setattr(epic_module, "_open_review_pr", open_pr)
    return forge


def land(land_at: str) -> dict:
    return {**initiative(), "land_at": land_at}


def stored_approved(work: dict) -> dict:
    return {**work, "items": [{**item, "state": "approved", "merged": True} for item in work["items"]]}


@pytest.mark.parametrize(
    ("land_at", "grain", "expected"),
    [("initiative", "task", False), ("initiative", "level", False), ("task", "task", True), ("task", "level", False)],
)
def test_a_pr_is_opened_only_at_the_configured_granularity(land_at, grain, expected) -> None:
    assert opens_pr(land_at, grain) is expected


def test_at_ticket_no_task_or_level_opens_a_pr_and_the_record_carries_land_at(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=land("ticket"))
    assert prs == []
    assert result["land_at"] == "initiative", "the alias `ticket` is recorded under its canonical name"
    assert [p["land_at"] for p in result["phases"]] == ["initiative", "initiative"]
    assert all(t["state"] == "approved" and t["merged"] for t in result["tasks"])
    assert not any("review_pr" in t for t in result["tasks"])


def test_land_at_defaults_to_initiative_when_the_initiative_sets_none(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path)
    assert prs == []
    assert [p["land_at"] for p in result["phases"]] == ["initiative", "initiative"]


def test_a_ticket_run_needs_no_approval_and_mints_no_gate_evidence(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=land("ticket"), assume=None)
    assert [p["status"] for p in result["phases"]] == ["complete", "complete"]
    assert prs == []
    assert all(t["merged"] for t in result["tasks"])
    ledger_file = tmp_path / "ledger.jsonl"
    assert (ledger.read(ledger_file) if ledger_file.exists() else []) == [], "no clean row for a decision nobody made"


def test_the_retired_level_opens_no_pull_request_per_phase(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=land("level"))
    assert prs == [], "per-phase pull requests no longer exist"
    assert [p["land_at"] for p in result["phases"]] == ["initiative", "initiative"]
    assert not any("review_pr" in p for p in result["phases"])
    assert all(t["state"] == "approved" and t["merged"] for t in result["tasks"])


def test_a_forge_that_cannot_be_asked_opens_no_duplicate(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    prs.unreachable = True
    result, _ = drive(repo, cart, tmp_path, work={**initiative(two_phases=False), "land_at": "task"})
    assert prs == []
    assert not any("review_pr" in t for t in result["tasks"])


def test_at_task_a_pr_is_opened_per_task(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    result, _ = drive(repo, cart, tmp_path, work=land("task"))
    assert sorted(prs) == sorted(f"review/demo-initiative/{t}" for t in TASKS)
    assert {t["id"] for t in result["tasks"] if "review_pr" in t} == TASKS
    assert [p["land_at"] for p in result["phases"]] == ["task", "task"]
    assert not any(t["state"] == "done" for t in result["tasks"]), "the run never writes done; only land does"


def test_a_bad_land_at_refuses_the_run_before_any_phase(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    with pytest.raises(ValueError, match="land_at must be one of"):
        drive(repo, cart, tmp_path, work=land("sprint"))
    assert prs == []






def test_a_restart_opens_the_task_prs_a_failed_first_run_never_made(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    first = {**initiative(two_phases=False), "land_at": "task"}
    prs.down = True
    drive(repo, cart, tmp_path, work=first)
    assert prs == []

    prs.down = False
    drive(repo, cart, tmp_path, work=stored_approved(first), run_id="epic-2", resume_from="epic-1")
    assert sorted(prs) == ["review/demo-initiative/t1-probe", "review/demo-initiative/t2-bench"]




def test_a_restart_with_one_level_merged_builds_only_the_next(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    work = land("ticket")
    stored = {
        **work,
        "items": [
            {**item, "state": "approved", "merged": True} if item["phase"] == "p1-foundations" else item
            for item in work["items"]
        ],
    }
    patches = {"t3-cutover": new_file_patch("t3-cutover.txt")}
    result, runner = drive(repo, cart, tmp_path, work=stored, patches=patches)
    builds = [str(c) for c in runner.calls if c["role"] == "build"]
    assert builds and not any(t in b for b in builds for t in ("t1-probe", "t2-bench"))
    assert [t["id"] for t in result["tasks"] if t["merged"]] == ["t3-cutover"]
    assert [p["status"] for p in result["phases"]] == ["complete", "complete"]
    assert prs == []


def test_a_governance_task_opens_no_review_pr_at_ticket(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    patches = {"t1-probe": new_file_patch("harness/x.py"), "t2-bench": new_file_patch("t2-bench.txt")}
    work = {**initiative(two_phases=False), "land_at": "ticket"}
    result, _ = drive(repo, cart, tmp_path, work=work, patches=patches, assume=None)
    record = next(t for t in result["tasks"] if t["id"] == "t1-probe")
    assert prs == []
    assert record["status"] == "approved" and "review_pr" not in record


def _advance_main(repo) -> None:  # noqa: F811
    (repo / "moved.md").write_text("moved\n", encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-qm", "advance main", cwd=repo)


def _restart_work(land_at: str, *, levels: tuple[str, ...], unmerged: str | None = None) -> dict:
    """The store a restart reads: every task of `levels` approved and merged, bar `unmerged`."""
    work = land(land_at)
    items = [
        {**item, "state": "approved", "merged": item["id"] != unmerged} if item["phase"] in levels else item
        for item in work["items"]
    ]
    return {**work, "items": items}


def _first_run_then_move_main(repo, cart, tmp_path, land_at: str) -> None:  # noqa: F811
    drive(repo, cart, tmp_path, work=land(land_at), run_id="epic-1")
    _advance_main(repo)


def _p2(result: dict) -> dict:
    return next(p for p in result["phases"] if p["phase"] == "p2-rollout")


def test_at_ticket_a_merged_p1_is_an_ordinary_awaiting_land_phase_that_holds_p2(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    _first_run_then_move_main(repo, cart, tmp_path, "ticket")
    work = _restart_work("ticket", levels=("p1-foundations",))
    result, runner = drive(repo, cart, tmp_path, work=work, run_id="epic-2")
    assert result["phases"][0]["status"] == "awaiting_land" and "land_deferred" not in result["phases"][0]
    assert "awaiting the land" in _p2(result)["reason"]
    assert build_outcome(result)["blocked_kind"] == "awaiting_land"
    assert not any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)


def test_at_ticket_a_partial_p1_with_every_task_merged_still_blocks_p2(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    patches = {t: new_file_patch(f"{t}.txt") for t in ("t1-probe", "t2-bench", "t3-cutover")}
    runner = Runner(patches, verdicts={"p1-foundations": GOAL_UNMET})
    result, runner = drive(repo, cart, tmp_path, runner=runner, work=land("ticket"))
    assert result["phases"][0]["status"] == "partial"
    assert all(t["merged"] for t in result["tasks"] if t["id"] != "t3-cutover")
    assert _p2(result)["status"] == "blocked" and "did not meet its goal" in _p2(result)["reason"]
    assert not any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)




def test_at_ticket_p2_stays_blocked_when_one_p1_task_is_approved_unmerged(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    _first_run_then_move_main(repo, cart, tmp_path, "ticket")
    work = _restart_work("ticket", levels=("p1-foundations",), unmerged="t2-bench")
    result, runner = drive(repo, cart, tmp_path, work=work, run_id="epic-2")
    assert _p2(result)["status"] == "blocked" and "parent phase 'p1-foundations'" in _p2(result)["reason"]
    assert not any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)


def test_at_task_a_merged_p1_does_not_unblock_p2(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    _first_run_then_move_main(repo, cart, tmp_path, "task")
    work = _restart_work("task", levels=("p1-foundations",))
    result, runner = drive(repo, cart, tmp_path, work=work, run_id="epic-2")
    assert _p2(result)["status"] == "blocked" and "parent phase 'p1-foundations'" in _p2(result)["reason"]
    assert not any(c["role"] == "build" and "t3-cutover" in c["prompt"] for c in runner.calls)


def test_at_ticket_the_last_phase_approved_and_merged_still_awaits_its_land(repo, cart, tmp_path, prs) -> None:  # noqa: F811
    only = {**initiative(two_phases=False), "land_at": "ticket"}
    drive(repo, cart, tmp_path, work=only, run_id="epic-1")
    _advance_main(repo)
    approved = {**only, "items": [{**i, "state": "approved", "merged": True} for i in only["items"]]}
    result, _ = drive(repo, cart, tmp_path, work=approved, run_id="epic-2")
    assert [p["status"] for p in result["phases"]] == ["awaiting_land"]
    assert build_outcome(result)["blocked_kind"] == "awaiting_land"
    assert result["exit_code"] == EXIT_AWAITING_LAND
