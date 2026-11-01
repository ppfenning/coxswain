from dataclasses import replace
from pathlib import Path

import pytest

from agent_tools import chair, cli
from agent_tools.chair_exec import Deps, argv_for, edge_deps, perform

LANDED = "merge: ok\nmark_done: ok\n"
LAND = {"kind": "land", "task_id": "t1", "repo": "r", "run": "run-1", "epoch": 1}
LAND_PHASE = {"kind": "land_phase", "initiative": "i", "phase": "p1", "repo": "r", "run": "run-1", "epoch": 1}
LIVE_LOCK = {"session": "chair-host", "pid": 1, "host": "host-a"}


def _guarded_run(calls: list):
    """Applies the real landing guard to the label a `cox` argv carries, as `_leader_guard_or_refuse` does."""

    def run(argv):
        calls.append(argv)
        explicit = argv[argv.index("--label") + 1] if "--label" in argv else None
        holder = chair.resolve_holder_label(LIVE_LOCK, "live", {}, "host-a", explicit) or "unlabeled"
        line = chair.guard(LIVE_LOCK, holder, "live")
        return (2, line) if line else (0, LANDED)

    return run


def _deps(run, label: str) -> Deps:
    return Deps(
        run=run,
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "run-1",
        repo_for=lambda action: "r",
        rev_parse=lambda repo, ref: None,
        label=label,
    )


def test_a_land_argv_carries_the_chairs_label() -> None:
    assert argv_for(LAND, label="chair-host")[-2:] == ["--label", "chair-host"]  # type: ignore[index]
    assert argv_for(LAND_PHASE, label="chair-host")[-2:] == ["--label", "chair-host"]  # type: ignore[index]


def test_an_unlabeled_chair_adds_no_label_flag() -> None:
    assert "--label" not in argv_for(LAND)  # type: ignore[operator]


@pytest.mark.parametrize(
    "action",
    [
        {"kind": "launch_epic", "initiative": "i"},
        {"kind": "relaunch", "initiative": "i"},
        {"kind": "retry", "initiative": "i"},
        {"kind": "launch_decompose", "intake_ids": ["i1"]},
        {"kind": "rescue", "initiative": "i", "task_id": "t1"},
    ],
)
def test_a_launch_argv_carries_the_chairs_label(action) -> None:
    argv = argv_for(action, "init-1", label="chair-host")
    assert argv is not None
    assert argv[argv.index("--label") + 1] == "chair-host"


def test_a_chair_land_is_not_refused_by_its_own_live_lock() -> None:
    calls: list = []
    results = perform([LAND], _deps(_guarded_run(calls), "chair-host"), lambda: 1, False)
    assert results[0]["status"] != "refused"
    assert calls[0][-2:] == ["--label", "chair-host"]


def test_a_phase_land_is_not_refused_by_its_own_live_lock() -> None:
    calls: list = []
    results = perform([LAND_PHASE], _deps(_guarded_run(calls), "chair-host"), lambda: 1, False)
    assert results[0]["status"] != "refused"


def test_a_land_without_the_label_is_refused_by_the_same_lock() -> None:
    calls: list = []
    results = perform([LAND], _deps(_guarded_run(calls), ""), lambda: 1, False)
    assert results[0]["status"] == "refused"
    assert "held by chair-host (live)" in results[0]["reason"]


def _chair_run_exec_deps(tmp_path: Path, label: str, calls: list) -> Deps:
    built = cli._chair_run_deps(tmp_path, {}, label, 1, "host-a", True, print, tmp_path / "p.yaml", "files")
    return replace(built.exec_deps, run=_guarded_run(calls))


def test_the_chair_run_builder_labels_its_deps_and_a_land_through_them_passes_the_guard(tmp_path: Path) -> None:
    calls: list = []
    deps = _chair_run_exec_deps(tmp_path, "chair-host", calls)
    assert deps.label == "chair-host"
    assert perform([LAND], deps, lambda: 1, False)[0]["status"] != "refused"
    assert calls[0][-2:] == ["--label", "chair-host"]


def test_edge_deps_alone_sets_no_label(tmp_path: Path) -> None:
    deps = edge_deps(
        tmp_path,
        tmp_path,
        "cox-runs-move",
        1,
        run_id=lambda action: "",
        repo_for=lambda action: "",
        record=lambda action: None,
        host="host-a",
        harness_python="python",
    )
    assert deps.label == ""


@pytest.mark.parametrize("kind", ["relaunch", "retry"])
def test_a_runs_move_relaunch_carries_no_label_so_a_caller_held_lock_still_matches_by_pid(
    tmp_path: Path, kind: str
) -> None:
    deps = cli._runs_move_exec_deps(tmp_path, tmp_path / "runs", "python")
    assert deps.label == ""
    assert "--label" not in argv_for({"kind": kind, "initiative": "i"}, label=deps.label)  # type: ignore[operator]
