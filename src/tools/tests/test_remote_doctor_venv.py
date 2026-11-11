from agent_tools.lane_hosts import LaneHost
from agent_tools.lane_toolchain import CheckTools
from agent_tools.remote_doctor import doctor_on_host

_HOST = LaneHost("lane-a", "me@lane-a", "/srv")
_PYTHON = CheckTools(("python",), ())


def _run(missing_when):
    """A probe stub: `python` is reported missing when `missing_when(probe_command)` holds."""

    def run(argv):
        command = " ".join(argv)
        if "setup token status --probe" in command:
            return 0, '{"loggedIn": true}'
        if "lane-toolchain" in command:
            return 0, "python\n" if missing_when(command) else ""
        return 0, "ok rows"

    return run


def _doctor(run, **kwargs):
    return doctor_on_host(_HOST, run, argv_for=lambda argv: argv, **kwargs)


def _refusals(rows):
    return [row for row in rows if " not on " in row]


def test_a_tool_found_in_the_repo_venv_gives_no_refusal_row():
    run = _run(lambda command: "repo-one" not in command)
    code, rows = _doctor(run, repo_checks=[(_PYTHON, "repo-one")])
    assert (code, _refusals(rows)) == (0, [])


def test_a_tool_missing_from_the_lane_path_and_the_venv_names_both():
    code, rows = _doctor(_run(lambda command: True), repo_checks=[(_PYTHON, "repo-one")])
    assert (code, _refusals(rows)) == (1, ["lane-a: python not on the lane PATH or in the repo venv"])


def test_two_repos_with_the_same_missing_tool_give_one_row():
    _, rows = _doctor(_run(lambda command: True), repo_checks=[(_PYTHON, "repo-one"), (_PYTHON, "repo-two")])
    assert _refusals(rows) == ["lane-a: python not on the lane PATH or in the repo venv"]


def test_the_old_checks_form_still_gives_the_old_message():
    code, rows = _doctor(_run(lambda command: True), checks=CheckTools(("python",), ("if x",)))
    assert code == 1
    assert _refusals(rows) == ["lane-a: python not on the lane PATH"]
    assert rows.count("checks: could not read tools from: if x") == 1


def test_unparsed_lines_are_reported_once_across_checks_and_repos():
    unparsed = CheckTools((), ("if x",))
    _, rows = _doctor(
        _run(lambda command: False), checks=unparsed, repo_checks=[(unparsed, "repo-one"), (unparsed, "repo-two")],
    )
    assert rows.count("checks: could not read tools from: if x") == 1
