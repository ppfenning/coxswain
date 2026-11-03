import subprocess

import pytest

from agent_tools import chair_read_land_conflicts, land_manual

LINUX_OTHER = "/".join(["", "home", "bob"])
MAC_OTHER = "/".join(["", "Users", "carol"])


class _Done:
    returncode = 0
    stdout = "abc\n"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _recording_run():
    seen: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, ""

    return seen, run


@pytest.mark.parametrize("recorded", ["~/r", LINUX_OTHER + "/r", MAC_OTHER + "/r"])
def test_rev_parse_argv_carries_expanded_path(home, monkeypatch, recorded):
    seen: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: seen.append(argv) or _Done())
    assert chair_read_land_conflicts._rev_parse(recorded, "main") == "abc"
    assert seen == [["git", "-C", f"{home}/r", "rev-parse", "--verify", "--quiet", "main"]]


@pytest.mark.parametrize("recorded", ["~/r", LINUX_OTHER + "/r"])
def test_merged_on_origin_argv_carries_expanded_path(home, recorded):
    seen, run = _recording_run()
    land_manual.merged_on_origin(recorded, ["pr/t"], ["main"], run)
    assert seen and all(argv[:3] == ["git", "-C", f"{home}/r"] for argv in seen)


@pytest.mark.parametrize("recorded", ["~/r", LINUX_OTHER + "/r"])
def test_lookup_merged_pr_changes_into_expanded_path(home, recorded):
    seen, run = _recording_run()
    land_manual.lookup_merged_pr(recorded, ["pr/t"], run)
    assert seen[0][4] == f"{home}/r"


@pytest.mark.parametrize("recorded", ["~/r", LINUX_OTHER + "/r"])
def test_is_manual_reads_overlay_under_patched_home(home, recorded):
    overlay = home / "r" / land_manual.OVERLAY
    overlay.parent.mkdir(parents=True)
    overlay.write_text("land: manual\n", encoding="utf-8")
    assert land_manual.is_manual(recorded) is True
