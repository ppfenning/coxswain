from pathlib import Path

from agent_tools.chair_restart import (
    RESTART,
    RESTART_EXIT_CODE,
    STAY,
    WAIT,
    CheckoutState,
    Pending,
    changed_paths,
    checkout_root,
    coalesce,
    read_checkout_state,
    restart_decision,
    restart_line,
    restart_reason,
    touches_chair_code,
)

OLD = "a" * 40
NEW = "b" * 40
BRANCH_OLD = CheckoutState(OLD, True)
BRANCH_NEW = CheckoutState(NEW, True)
DETACHED_OLD = CheckoutState(OLD, False)
DETACHED_NEW = CheckoutState(NEW, False)


def test_exit_code_differs_from_the_others():
    assert RESTART_EXIT_CODE == 75
    assert RESTART_EXIT_CODE not in {0, 1, 2, 3}


def test_moved_head_on_a_branch_restarts():
    assert restart_decision(BRANCH_OLD, BRANCH_NEW, False).state == RESTART


def test_moved_head_with_a_land_in_flight_waits():
    assert restart_decision(BRANCH_OLD, BRANCH_NEW, True).state == WAIT


def test_unmoved_head_stays():
    assert restart_decision(BRANCH_OLD, BRANCH_OLD, False).state == STAY


def test_detached_at_start_stays_even_if_head_moved():
    assert restart_decision(DETACHED_OLD, BRANCH_NEW, False).state == STAY


def test_branch_at_start_but_detached_now_stays():
    assert restart_decision(BRANCH_OLD, DETACHED_NEW, False).state == STAY


def test_unreadable_current_state_stays():
    assert restart_decision(BRANCH_OLD, None, False).state == STAY


def test_line_names_both_short_commits():
    line = restart_line(OLD, NEW)
    assert "a" * 12 in line and "b" * 12 in line
    assert OLD not in line and NEW not in line


def test_read_checkout_state_parses_the_runner_output():
    def run(args: list[str]) -> str | None:
        return f"{OLD}\n" if "rev-parse" in args else "refs/heads/main\n"

    assert read_checkout_state(Path("/repo"), run) == CheckoutState(OLD, True)


def test_read_checkout_state_detached_head():
    def run(args: list[str]) -> str | None:
        return f"{OLD}\n" if "rev-parse" in args else None

    assert read_checkout_state(Path("/repo"), run) == CheckoutState(OLD, False)


def test_read_checkout_state_is_none_on_failure():
    assert read_checkout_state(Path("/repo"), lambda args: None) is None


def test_checkout_root_holds_the_repository():
    root = checkout_root()
    assert root.is_absolute()
    assert (root / ".git").exists()


def test_docs_only_paths_do_not_touch_chair_code():
    assert not touches_chair_code(
        ["docs/index.md", "crew/seats/sky.md", "deploy/superset/bootstrap.py", "devtools/cli.py"]
    )


def test_agent_tools_path_touches_chair_code():
    assert touches_chair_code(["docs/index.md", "src/tools/agent_tools/chair_run.py"])


def test_graphs_and_cartridges_core_touch_chair_code():
    assert touches_chair_code(["src/graphs/a.py"]) and touches_chair_code(["src/cartridges/core/policy.py"])


def test_tests_path_under_src_tools_does_not_touch_chair_code():
    assert not touches_chair_code(["src/tools/tests/test_chair_restart.py", "src/cartridges/core/tests/t.py"])


def test_markdown_under_chair_code_does_not_count():
    assert not touches_chair_code(["src/tools/agent_tools/README.md"])


def test_empty_path_list_does_not_touch_chair_code():
    assert not touches_chair_code([])


def test_changed_paths_returns_the_non_blank_lines():
    seen: list[list[str]] = []

    def run(args: list[str]) -> str | None:
        seen.append(args)
        return "a.py\n\nb/c.py\n"

    assert changed_paths(Path("/repo"), OLD, NEW, run) == ["a.py", "b/c.py"]
    assert seen == [["-C", "/repo", "diff", "--name-only", OLD, NEW]]


def test_changed_paths_is_none_when_the_diff_fails():
    assert changed_paths(Path("/repo"), OLD, NEW, lambda args: None) is None


def test_three_heads_inside_the_window_restart_only_after_it():
    first, p1 = coalesce(None, "h1", 0.0, 30.0, 600.0)
    second, p2 = coalesce(p1, "h2", 10.0, 30.0, 600.0)
    third, p3 = coalesce(p2, "h3", 20.0, 30.0, 600.0)
    early, p4 = coalesce(p3, "h3", 49.0, 30.0, 600.0)
    late, _ = coalesce(p4, "h3", 50.0, 30.0, 600.0)
    assert (first, second, third, early, late) == (False, False, False, False, True)
    assert p3 == Pending("h3", 0.0, 20.0)


def test_max_defer_forces_the_answer_while_heads_keep_changing():
    pending = Pending("h1", 0.0, 95.0)
    assert coalesce(pending, "h2", 100.0, 30.0, 100.0) == (True, Pending("h2", 0.0, 100.0))


def test_reason_names_short_heads_and_the_chair_code_count():
    line = restart_reason(OLD, NEW, ["src/graphs/a.py", "docs/x.md", "src/tools/agent_tools/b.py"])
    assert "a" * 12 in line and "b" * 12 in line and OLD not in line
    assert "2 chair code files" in line
    assert "| tick error: " not in line


def test_reason_says_when_the_diff_was_unreadable():
    line = restart_reason(OLD, NEW, None)
    assert "unreadable" in line and "| tick error: " not in line
