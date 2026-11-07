import json

from agent_tools.remote_argv import rsync_pull_argv
from agent_tools.remote_fetch import decompose_pull_argvs
from agent_tools.remote_lane import is_decompose_record, parse_remote_record, record_graph, remote_record


def test_decompose_pull_argvs_stage_the_work_directory_and_bring_back_the_log():
    assert decompose_pull_argvs("me@box", "/hw/", "/cw", "my-init", "my-init-1") == [
        rsync_pull_argv("me@box:/hw/work/my-init/", "/cw/runs/.fetch-staging/my-init-1/work/my-init/"),
        rsync_pull_argv("me@box:/hw/runs/my-init-1.log", "/cw/runs/"),
    ]


def test_decompose_pull_argvs_fetch_no_branch():
    flat = [part for argv in decompose_pull_argvs("me@box", "/hw", "/cw", "i", "i-1") for part in argv]
    assert "git" not in flat and not any("refs/heads" in part for part in flat)


def test_a_record_without_a_graph_field_parses_as_epic():
    parsed = parse_remote_record(json.dumps({"host": "box", "launched_at": "t"}))
    assert record_graph(parsed) == "epic"
    assert not is_decompose_record(parsed)


def test_a_record_with_graph_decompose_is_recognised():
    parsed = parse_remote_record(json.dumps(remote_record("box", "t", graph="decompose")))
    assert parsed == {"host": "box", "launched_at": "t", "graph": "decompose"}
    assert is_decompose_record(parsed)


def test_a_non_string_graph_falls_back_to_epic():
    parsed = parse_remote_record('{"host": "box", "launched_at": "t", "graph": 7}')
    assert record_graph(parsed) == "epic"
