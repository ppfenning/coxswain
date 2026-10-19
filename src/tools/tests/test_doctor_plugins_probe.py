"""The harness plugins probe: which interpreter it runs, and what its output means, from literals."""

from agent_tools.cli import parse_plugins_probe, plugins_probe_argv

_OK = '{"coxswain.system_one": ["a"], "coxswain.runners": []}'


def test_the_probe_runs_under_the_harness_interpreter():
    argv = plugins_probe_argv("/h/installs/coxswain/.venv/bin/python")
    assert argv[0] == "/h/installs/coxswain/.venv/bin/python"
    assert argv[-2:] == ["coxswain.system_one", "coxswain.runners"]


def test_a_clean_answer_names_each_group():
    assert parse_plugins_probe(0, _OK) == {"coxswain.system_one": ["a"], "coxswain.runners": []}


def test_a_failed_interpreter_is_not_checked():
    assert parse_plugins_probe(1, _OK) is None


def test_output_that_is_not_json_is_not_checked():
    assert parse_plugins_probe(0, "Traceback (most recent call last):") is None


def test_an_answer_missing_a_group_is_not_checked():
    assert parse_plugins_probe(0, '{"coxswain.runners": []}') is None
