import copy

from devtools.run_status import Verdict, classify_runs, render_verdict


def test_an_empty_list_is_no_workflow():
    assert classify_runs([]) == Verdict("no_workflow")


def test_runs_without_a_startup_failure_are_unknown():
    runs = [{"conclusion": "failure", "name": "ci", "url": "u1"}, {"conclusion": "success", "name": "docs", "url": "u2"}]
    assert classify_runs(runs) == Verdict("unknown")


def test_a_run_with_no_conclusion_yet_is_unknown():
    assert classify_runs([{"conclusion": "", "name": "ci", "url": "u1"}, {"name": "docs", "url": "u2"}]) == Verdict("unknown")


def test_a_lone_startup_failure_carries_its_name_and_url():
    runs = [{"conclusion": "startup_failure", "name": "ci", "url": "https://example.test/runs/1"}]
    assert classify_runs(runs) == Verdict("startup_failure", "ci", "https://example.test/runs/1")


def test_a_startup_failure_listed_after_a_success_is_still_found():
    runs = [
        {"conclusion": "success", "name": "docs", "url": "u1"},
        {"conclusion": "startup_failure", "name": "ci", "url": "u2"},
    ]
    assert classify_runs(runs) == Verdict("startup_failure", "ci", "u2")


def test_the_first_of_two_startup_failures_is_returned():
    runs = [
        {"conclusion": "startup_failure", "name": "ci", "url": "u1"},
        {"conclusion": "startup_failure", "name": "docs", "url": "u2"},
    ]
    assert classify_runs(runs) == Verdict("startup_failure", "ci", "u1")


def test_classifying_does_not_mutate_the_input():
    runs = [{"conclusion": "success", "name": "docs", "url": "u1"}, {"conclusion": "startup_failure", "name": "ci", "url": "u2"}]
    before = copy.deepcopy(runs)
    classify_runs(runs)
    assert runs == before


def test_render_startup_failure_names_the_workflow_and_url():
    verdict = Verdict("startup_failure", "ci", "https://example.test/runs/1")
    assert render_verdict(verdict, "feat-x") == "workflow ci hit startup_failure: https://example.test/runs/1"


def test_render_no_workflow_names_the_branch():
    assert render_verdict(Verdict("no_workflow"), "feat-x") == "no workflow ran for branch feat-x"


def test_render_unknown_names_the_branch():
    assert render_verdict(Verdict("unknown"), "feat-x") == "no startup_failure among the runs for branch feat-x"
