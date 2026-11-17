from agent_tools.chair_read_approved import run_file_index


def test_run_file_index_groups_numbered_runs_by_initiative_for_one_suffix():
    names = ["a-1.pid", "a-12.pid", "a-b-2.pid", "a.pid", "a-1.remote.json", "a-x.pid"]
    assert run_file_index(names, ".pid") == {"a": {"a-1": "a-1.pid", "a-12": "a-12.pid"}, "a-b": {"a-b-2": "a-b-2.pid"}}
    assert run_file_index(names, ".remote.json") == {"a": {"a-1": "a-1.remote.json"}}
    assert run_file_index([], ".pid") == {}
