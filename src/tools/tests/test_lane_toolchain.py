import datetime
import json

from agent_tools.lane_hosts import LaneHost
from agent_tools.lane_toolchain import (
    CheckTools,
    check_lines,
    lane_argv,
    lookup_argv,
    parse_check_tools,
    refusals,
    refuse_message,
)
from agent_tools.launch_place import Placement, Refusal, format_refusal, place, place_checked
from agent_tools.remote_doctor import doctor_on_host
from agent_tools.remote_launch import LaunchError, launch_on_host

NOW = datetime.datetime(2026, 10, 30, 12, 0, tzinfo=datetime.UTC)
FRESH = "2026-10-30T11:59:00+00:00"
HOST = LaneHost("box", "me@box", "/srv/ws")


def _row(name):
    return {
        "name": name, "ssh": name, "capacity": 1, "weight": 1, "state": "active", "beat_at": FRESH,
        "versions_json": json.dumps({"workspace_dir": "/srv/ws"}),
    }


def _lacking(*missing):
    return lambda argv: (0, "\n".join(missing) + "\n")


def test_a_directory_change_joined_to_a_runner_yields_the_runner():
    assert parse_check_tools(["cd src/tools && pytest -q -n auto"]) == CheckTools(("pytest",), ())


def test_leading_assignments_are_skipped_and_a_semicolon_splits_segments():
    assert parse_check_tools(["A=1 B='x y' ruff check; cargo fmt --check"]).found == ("ruff", "cargo")


def test_an_unparsable_line_is_unparsed_and_never_found():
    assert parse_check_tools(["echo 'unclosed", "$(which x) go", "pytest"]) == CheckTools(("pytest",), ("echo 'unclosed", "$(which x) go"))


def test_check_lines_drop_comments_and_blanks():
    assert check_lines("# note\n\ncargo test\n") == ["cargo test"]


def test_a_missing_tool_is_one_refusal_line_naming_it():
    assert refusals("box", ("git", "pytest"), ["probe"], _lacking("pytest")) == [refuse_message("box", "pytest")]
    assert refuse_message("box", "pytest") == "box: pytest not on the lane PATH"


def test_a_failed_probe_is_never_read_as_every_tool_found():
    assert refusals("box", ("git",), ["probe"], lambda argv: (255, "")) == ["box: toolchain probe exited 255"]


def test_the_lookup_runs_in_the_lane_login_shell():
    assert lane_argv(["git"])[-1] == "sh -c 'for t in \"$@\"; do command -v \"$t\" >/dev/null 2>&1 || echo \"$t\"; done' lane-toolchain git"
    assert lane_argv(["git"])[:3] == ["/bin/sh", "-c", 'exec "${SHELL:-/bin/sh}" -l -i -c "$1"']
    assert lookup_argv(["git"])[-2:] == ["lane-toolchain", "git"]


def test_a_missing_tool_refuses_before_any_copy_step_is_issued():
    issued = []
    result = launch_on_host(
        HOST, "init", "run-1", "label", "2026-10-30T12:00:00+00:00", lambda argv: issued.append(argv) or 0,
        probe=_lacking("pytest"), tools=("git", "pytest"),
    )
    assert result == LaunchError("toolchain", "box: pytest not on the lane PATH")
    assert issued == []


def test_placement_skips_a_host_that_failed_the_probe_and_picks_the_next():
    rows = [_row("a"), _row("b")]
    first = place(rows, {}, NOW).host
    other = "b" if first == "a" else "a"
    refused = lambda name: None if name == other else f"{name}: git not on the lane PATH"  # noqa: E731
    placed = place_checked(lambda barred: place(rows, {}, NOW, barred=barred), refused)
    assert placed == Placement(other, 0, 1)


def test_placement_refuses_only_when_every_host_failed_and_says_why():
    rows = [_row("a"), _row("b")]
    placed = place_checked(lambda barred: place(rows, {}, NOW, barred=barred), lambda name: f"{name}: git not on the lane PATH")
    assert isinstance(placed, Refusal)
    assert format_refusal(placed) == ["a 0/1  (a: git not on the lane PATH)", "b 0/1  (b: git not on the lane PATH)"]


def test_a_pinned_host_refuses_without_trying_another():
    issued = []
    result = launch_on_host(
        HOST, "init", "run-1", "label", "2026-10-30T12:00:00+00:00", lambda argv: issued.append(argv) or 0,
        probe=_lacking("git"), tools=("git",),
    )
    assert isinstance(result, LaunchError)
    assert result.message == "box: git not on the lane PATH"
    assert len(issued) == 0


def test_the_doctor_output_includes_the_missing_tool_line():
    def run(argv):
        return (0, "pytest\n") if "lane-toolchain" in argv[-1] else (0, '{"loggedIn": true}')

    code, rows = doctor_on_host(HOST, run, checks=CheckTools(("git", "pytest"), ("$(x) y",)))
    assert code == 1
    assert "box: pytest not on the lane PATH" in rows
    assert "checks: could not read tools from: $(x) y" in rows
