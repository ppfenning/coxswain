from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from agent_tools import lane_checkout_sync as lcs
from agent_tools.lane_hosts import LaneHost

CHAIR_WS = "/chair/ws"
ALPHA = LaneHost("alpha", "me@alpha", "/srv/ws")
BETA = LaneHost("beta", "me@beta", "/srv/ws")
ALPHA_PATH = "/srv/ws/repo"


def facts_out(clean: bool = True, branch: str = "main", ancestor: bool = True, at_tip: bool = False) -> str:
    return f"clean={int(clean)}\nbranch={branch}\ndefault=main\nancestor={int(ancestor)}\nat_tip={int(at_tip)}\n"


class FakeRunner:
    """Replies per ssh destination (the second-to-last argv word); every argv is recorded."""

    def __init__(self, reads: dict[str, tuple[int, str]], merge: tuple[int, str] = (0, "")) -> None:
        self.reads, self.merge, self.calls = reads, merge, []

    def __call__(self, argv: Sequence[str]) -> tuple[int, str]:
        self.calls.append(list(argv))
        return self.merge if "merge --ff-only" in argv[-1] else self.reads[argv[-2]]

    @property
    def merges(self) -> list[list[str]]:
        return [call for call in self.calls if "merge --ff-only" in call[-1]]


def test_verdict_from_facts_one_literal_call_per_verdict() -> None:
    assert lcs.verdict_from_facts(True, "main", "main", True, False) == "fast_forward"
    assert lcs.verdict_from_facts(True, "main", "main", True, True) == "already_current"
    assert lcs.verdict_from_facts(False, "main", "main", True, False) == "dirty"
    assert lcs.verdict_from_facts(True, "topic", "main", True, False) == "wrong_branch"
    assert lcs.verdict_from_facts(True, "main", "main", False, False) == "diverged"


def test_needs_chair_action_shape_and_clean_verdicts() -> None:
    assert lcs.needs_chair_action("alpha", ALPHA_PATH, "fast_forward") is None
    assert lcs.needs_chair_action("alpha", ALPHA_PATH, "already_current") is None
    action = lcs.needs_chair_action("alpha", ALPHA_PATH, "wrong_branch", "topic", "main")
    assert action == {
        "kind": "needs_chair",
        "host": "alpha",
        "cause": "lane_checkout_wrong_branch",
        "reason": "lane host alpha, checkout /srv/ws/repo: checkout not fast-forwarded: on topic, not main",
    }


def test_parse_facts_reads_five_facts_and_refuses_partial_output() -> None:
    assert lcs.parse_facts(facts_out(at_tip=True)) == (True, "main", "main", True, True)
    assert lcs.parse_facts(facts_out(branch="")) == (True, None, "main", True, False)
    assert lcs.parse_facts("clean=1\nbranch=main\n") is None
    assert lcs.parse_facts("fatal: not a git repository") is None


def test_clean_checkout_behind_origin_is_merged_without_an_action() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out())})
    assert lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run) is None
    assert len(run.merges) == 1
    assert run.merges[0][0] == "ssh"
    assert run.merges[0][-2] == "me@alpha"
    assert run.calls[0][-2] == "me@alpha"
    assert ALPHA_PATH in run.merges[0][-1]


def test_clean_checkout_already_current_is_not_merged() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out(at_tip=True))})
    assert lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run) is None
    assert run.merges == []


def test_dirty_checkout_is_not_merged_and_names_host_and_path() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out(clean=False))})
    action = lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run)
    assert run.merges == []
    assert action is not None
    assert action["kind"] == "needs_chair"
    assert action["host"] == "alpha"
    assert action["cause"] == "lane_checkout_dirty"
    assert "alpha" in action["reason"]
    assert ALPHA_PATH in action["reason"]
    assert "\n" not in action["reason"]


def test_diverged_checkout_is_not_merged() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out(ancestor=False))})
    action = lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run)
    assert run.merges == []
    assert action is not None
    assert action["cause"] == "lane_checkout_diverged"


def test_wrong_branch_checkout_is_not_merged() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out(branch="topic"))})
    action = lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run)
    assert run.merges == []
    assert action is not None
    assert action["cause"] == "lane_checkout_wrong_branch"


def test_unreachable_host_is_an_action_not_an_exception() -> None:
    run = FakeRunner({"me@alpha": (255, "ssh: connect to host alpha port 22: Connection timed out")})
    action = lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run)
    assert run.merges == []
    assert action is not None
    assert action["cause"] == "lane_checkout_unreachable"
    assert "Connection timed out" in action["reason"]
    assert ALPHA_PATH in action["reason"]


def test_runner_that_raises_is_unreachable() -> None:
    def broken(argv: Sequence[str]) -> tuple[int, str]:
        raise TimeoutError("hung")

    action = lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, broken)
    assert action is not None
    assert action["cause"] == "lane_checkout_unreachable"


def test_unreadable_facts_are_unreachable_never_a_fast_forward() -> None:
    run = FakeRunner({"me@alpha": (0, "clean=1\n")})
    action = lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run)
    assert run.merges == []
    assert action is not None
    assert action["cause"] == "lane_checkout_unreachable"


def test_refused_fast_forward_is_reported_not_retried() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out())}, merge=(1, "fatal: Not possible to fast-forward, aborting.\n"))
    action = lcs.sync_host_checkout(ALPHA, "/chair/ws/repo", CHAIR_WS, run)
    assert len(run.merges) == 1
    assert action is not None
    assert action["cause"] == "lane_checkout_unreachable"
    assert "Not possible to fast-forward" in action["reason"]


def test_two_hosts_one_dirty_one_clean_yield_one_action() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out(clean=False)), "me@beta": (0, facts_out(at_tip=True))})
    actions = lcs.sync_lane_checkouts([ALPHA, BETA], ["/chair/ws/repo"], CHAIR_WS, run)
    assert [(a["host"], a["cause"]) for a in actions] == [("alpha", "lane_checkout_dirty")]
    assert run.merges == []


def test_every_host_and_repo_is_visited() -> None:
    run = FakeRunner({"me@alpha": (0, facts_out(at_tip=True)), "me@beta": (0, facts_out(at_tip=True))})
    repos = ["/chair/ws/one", "/chair/ws/two"]
    assert lcs.sync_lane_checkouts([ALPHA, BETA], repos, CHAIR_WS, run) == []
    assert len(run.calls) == 4


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture
def clone_behind_origin(tmp_path: Path) -> Path:
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    git(tmp_path, "clone", str(origin), str(seed))
    git(seed, "checkout", "-b", "main")
    (seed / "a").write_text("a")
    git(seed, "add", "a")
    git(seed, "commit", "-m", "a")
    git(seed, "push", "origin", "main")
    git(tmp_path, "clone", str(origin), str(tmp_path / "lane"))
    (seed / "b").write_text("b")
    git(seed, "add", "b")
    git(seed, "commit", "-m", "b")
    git(seed, "push", "origin", "main")
    return tmp_path / "lane"


def test_read_and_merge_scripts_run_against_a_real_clone(clone_behind_origin: Path) -> None:
    lane = clone_behind_origin
    read = subprocess.run(["sh", "-c", lcs._READ_SCRIPT, "lane-sync", str(lane)], capture_output=True, text=True)
    assert read.returncode == 0, read.stderr
    assert lcs.parse_facts(read.stdout) == (True, "main", "main", True, False)
    merge = subprocess.run(["sh", "-c", lcs._MERGE_SCRIPT, "lane-sync", str(lane), "main"], capture_output=True)
    assert merge.returncode == 0
    assert (lane / "b").exists()
    (lane / "a").write_text("edited")
    read_dirty = subprocess.run(["sh", "-c", lcs._READ_SCRIPT, "lane-sync", str(lane)], capture_output=True, text=True)
    assert lcs.parse_facts(read_dirty.stdout) == (False, "main", "main", True, True)
    missing = subprocess.run(["sh", "-c", lcs._READ_SCRIPT, "lane-sync", str(lane / "nope")], capture_output=True)
    assert missing.returncode != 0
