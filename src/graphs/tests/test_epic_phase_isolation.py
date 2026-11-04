"""Two phases share one runner without sharing its repo_dir, repo_digest or check_commands."""

from __future__ import annotations

import threading
import time
from pathlib import Path

from harness import epic as epic_module
from harness.epic import _Ctx, _PhaseRunner, _run_phase
from harness.runners import _DelegatingFastPath
from tests.test_epic_driver import (  # noqa: F401
    TASK_IDS,
    Runner,
    cart,
    drive,
    initiative,
    new_file_patch,
    repo,
)
from tests.test_epic_only_subset import _ctx

PHASE_OF = {"t1-probe": "p1-foundations", "t2-bench": "p1-foundations", "t3-cutover": "p2-rollout"}
WATCHED = ("repo_dir", "repo_digest", "check_commands")


class Recording(Runner):
    """Records the repo_dir every node call sees, and every assignment to the watched names after construction."""

    repo_dir: Path | None = None
    repo_digest: str | None = None
    check_commands: list[str] = []  # noqa: RUF012

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.assigned: list[str] = []
        self.seen: list[tuple[str, Path | None]] = []
        self.closes = 0

    def __setattr__(self, name: str, value) -> None:
        if name in WATCHED and hasattr(self, "assigned"):
            self.assigned.append(name)
        object.__setattr__(self, name, value)

    def run(self, **kwargs):
        time.sleep(0.01)  # keeps the two phases inside the runner together
        task = next((t for t in TASK_IDS if t in kwargs["prompt"]), None)
        with self.lock:
            self.seen.append((task, self.repo_dir))
        return super().run(**kwargs)

    def close(self) -> None:
        self.closes += 1


def _two_independent_phases() -> dict:
    work = initiative()
    next(i for i in work["items"] if i["id"] == "t3-cutover")["needs"] = []
    return work


def _run_both_phases(ctx: _Ctx, work: dict) -> None:
    errors: list[BaseException] = []

    def phase(name: str) -> None:
        try:
            _run_phase(ctx, initiative=work, phase=name, parents=(), items=work["items"])
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=phase, args=(n,)) for n in ("p1-foundations", "p2-rollout")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


def _assert_each_call_saw_its_own_worktree(ctx: _Ctx, leaf: Recording) -> None:
    tasked = [(t, d) for t, d in leaf.seen if t]
    assert {t for t, _ in tasked} == set(TASK_IDS)
    assert all(d == ctx.phase_worktree(PHASE_OF[t]) for t, d in tasked)
    assert leaf.assigned == []
    assert leaf.closes == 0


def test_two_phases_in_two_threads_each_see_only_their_own_worktree(repo, cart, tmp_path) -> None:  # noqa: F811
    leaf = Recording({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    ctx, work = _ctx(repo, cart, tmp_path, leaf), _two_independent_phases()
    epic_module._mirror_read(ctx, work["items"])
    _run_both_phases(ctx, work)
    _assert_each_call_saw_its_own_worktree(ctx, leaf)


def test_the_delegating_fast_path_hands_each_inner_run_its_own_worktree(repo, cart, tmp_path) -> None:  # noqa: F811
    leaf = Recording({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    wrapper = _DelegatingFastPath(leaf, object(), {}, {})
    ctx, work = _ctx(repo, cart, tmp_path, wrapper), _two_independent_phases()
    epic_module._mirror_read(ctx, work["items"])
    _run_both_phases(ctx, work)
    _assert_each_call_saw_its_own_worktree(ctx, leaf)


def test_run_epic_closes_the_runner_once_after_every_phase(repo, cart, tmp_path) -> None:  # noqa: F811
    leaf = Recording({t: new_file_patch(f"{t}.txt") for t in TASK_IDS})
    result, _ = drive(repo, cart, tmp_path, runner=leaf)
    assert len(result["phases"]) == 2
    assert leaf.closes == 1
    assert leaf.assigned == []


def test_a_phase_view_overrides_the_three_names_and_leaves_the_shared_runner_alone() -> None:
    leaf = Recording({})
    view = _PhaseRunner(leaf, Path("/wt/a"), "digest", ["pytest"])
    assert (view.repo_dir, view.repo_digest, view.check_commands) == (Path("/wt/a"), "digest", ["pytest"])
    assert leaf.repo_dir is None
    assert leaf.assigned == []
    assert not hasattr(_PhaseRunner(object(), Path("/wt/a"), None, []), "repo_dir")


def test_items_lock_stays_out_of_comparison_and_repr() -> None:
    field = _Ctx.__dataclass_fields__["items_lock"]
    assert (field.compare, field.repr) == (False, False)
