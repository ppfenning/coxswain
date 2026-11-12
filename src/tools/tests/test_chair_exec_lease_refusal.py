from pathlib import Path

import pytest

from agent_tools.chair_exec import Deps, _launch

REFUSAL = "routing: launch failed: run: run-9 holds runs:epic-a; refusing to start run-10"


def _deps(code: int, output: str, work_dir: Path = Path(".")) -> Deps:
    return Deps(
        run=lambda argv: (code, output),
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=lambda action: None,
        run_id=lambda action: "run-1",
        repo_for=lambda action: "r",
        work_dir=work_dir,
    )


@pytest.mark.parametrize("kind", ["launch_epic", "relaunch"])
def test_a_lease_refused_launch_is_lease_held(kind: str) -> None:
    result = _launch({"kind": kind, "initiative": "i"}, _deps(1, REFUSAL))
    assert (result["status"], result["reason"]) == ("lease_held", REFUSAL)


def test_a_nonzero_exit_with_other_output_is_failed() -> None:
    assert _launch({"kind": "launch_epic", "initiative": "i"}, _deps(1, "boom"))["status"] == "failed"


def test_exit_zero_is_done() -> None:
    assert _launch({"kind": "launch_epic", "initiative": "i"}, _deps(0, "launched run-10"))["status"] == "done"
