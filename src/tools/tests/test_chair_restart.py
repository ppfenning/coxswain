from pathlib import Path

from agent_tools.chair_restart import (
    RESTART,
    RESTART_EXIT_CODE,
    STAY,
    WAIT,
    CheckoutState,
    checkout_root,
    read_checkout_state,
    restart_decision,
    restart_line,
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
