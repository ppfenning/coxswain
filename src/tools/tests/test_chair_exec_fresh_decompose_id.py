from agent_tools.chair_exec import Deps, perform

ACTION = {"kind": "launch_decompose", "intake_ids": ["intake/x.md"], "epoch": 1}


def _run(rows: list[dict], intake_id: str) -> tuple[list, list]:
    argvs: list = []
    recorded: list = []

    def run(argv):
        argvs.append(argv)
        return 0, "ok"

    deps = Deps(
        run=run,
        delete_branches=lambda repo, pattern: ([], ""),
        acquire_lease=lambda holder, host: "",
        record=recorded.append,
        run_id=lambda action: "run-1",
        repo_for=lambda action: "r",
        intake_id=lambda path: intake_id,
        initiative_rows=lambda: rows,
    )
    perform([ACTION], deps, lambda: 1, False)
    return argvs, recorded


def _initiative_id(argv: list) -> str:
    return argv[argv.index("--initiative-id") + 1]


def test_a_dropped_initiative_holding_the_intake_id_gives_the_next_suffixed_id() -> None:
    argvs, _ = _run([{"kind": "initiative", "task_id": "x", "state": "dropped"}], "x")
    assert _initiative_id(argvs[0]) == "x-2"


def test_no_matching_initiative_row_keeps_the_intake_id() -> None:
    argvs, _ = _run([], "x")
    assert _initiative_id(argvs[0]) == "x"


def test_a_live_initiative_row_keeps_the_intake_id() -> None:
    argvs, _ = _run([{"kind": "initiative", "task_id": "x", "state": "ready"}], "x")
    assert _initiative_id(argvs[0]) == "x"


def test_a_path_shaped_id_is_still_refused_and_launches_nothing() -> None:
    argvs, recorded = _run([{"kind": "initiative", "task_id": "../x", "state": "dropped"}], "../x")
    assert argvs == []
    assert (recorded[0]["status"], "path-shaped" in recorded[0]["reason"]) == ("refused", True)
