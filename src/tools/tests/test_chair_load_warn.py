import json

from agent_tools import cli
from agent_tools.chair_load_warn import warn_overloaded
from agent_tools.host_load import LOAD_KEY, load_report


def _row(name: str, load: float, cores: int) -> dict:
    return {"name": name, "versions_json": json.dumps({LOAD_KEY: load_report(load, cores, 1000, 250)})}


def test_an_overloaded_host_warns_once_naming_the_host():
    lines: list[str] = []
    warned = warn_overloaded([_row("big-a", 88, 20)], lines.append, frozenset())
    assert warned == {"big-a"}
    assert len(lines) == 1 and "big-a" in lines[0]


def test_a_host_still_overloaded_does_not_warn_again():
    lines: list[str] = []
    warned = warn_overloaded([_row("big-a", 88, 20)], lines.append, frozenset({"big-a"}))
    assert warned == {"big-a"} and lines == []


def test_a_recovered_then_re_overloaded_host_warns_again():
    lines: list[str] = []
    warned = warn_overloaded([_row("big-a", 2, 20)], lines.append, frozenset({"big-a"}))
    assert warned == frozenset() and lines == []
    warn_overloaded([_row("big-a", 88, 20)], lines.append, warned)
    assert len(lines) == 1


def test_a_host_under_twice_its_cores_does_not_warn():
    lines: list[str] = []
    assert warn_overloaded([_row("mid-b", 22, 14)], lines.append, frozenset()) == frozenset() and lines == []


def test_a_row_without_a_load_record_does_not_warn():
    lines: list[str] = []
    assert warn_overloaded([{"name": "old-c", "versions_json": "{}"}], lines.append, frozenset()) == frozenset() and lines == []


def test_the_chair_beat_echoes_the_warning_once_per_transition(monkeypatch, tmp_path):
    lines: list[str] = []
    rows = [_row("big-a", 88, 20)]
    monkeypatch.setattr(cli.run_store, "hosts", lambda runs_dir: rows)
    deps = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", True, lines.append, tmp_path / "p.yaml", "files")
    deps.beat()
    deps.beat()
    assert len(lines) == 1 and "big-a" in lines[0]
    rows[:] = [_row("big-a", 2, 20)]
    deps.beat()
    rows[:] = [_row("big-a", 88, 20)]
    deps.beat()
    assert len(lines) == 2


def test_a_failed_hosts_read_does_not_break_the_beat(monkeypatch, tmp_path):
    lines: list[str] = []

    def boom(runs_dir):
        raise OSError("store unreachable")

    deps = cli._chair_run_deps(tmp_path, {}, "chair", 1, "h", True, lines.append, tmp_path / "p.yaml", "files")
    monkeypatch.setattr(cli.run_store, "hosts", boom)  # after the build, which reads hosts once at startup
    deps.beat()
    assert lines == []
