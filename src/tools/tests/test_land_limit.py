"""The per-repo land limit: an int of at least 1 from the repo overlay, else 1."""
import pytest

from agent_tools import land_limit
from agent_tools.land_limit import land_limit_setting, repo_land_limit

BAD = [0, -1, "2", True, 2.0]


def test_default_is_one():
    assert land_limit_setting(None, "r") == 1
    assert land_limit_setting({}, "r") == 1


def test_top_level_value():
    assert land_limit_setting({"land_limit": 3}, "r") == 3


def test_repos_entry_beats_top_level():
    overlay = {"land_limit": 3, "repos": {"r": {"land_limit": 2}}}
    assert land_limit_setting(overlay, "r") == 2
    assert land_limit_setting(overlay, "other") == 3


@pytest.mark.parametrize("bad", BAD)
def test_invalid_values_fall_back_to_one(bad):
    assert land_limit_setting({"land_limit": bad}, "r") == 1
    assert land_limit_setting({"repos": {"r": {"land_limit": bad}}}, "r") == 1


@pytest.mark.parametrize("bad", BAD)
def test_invalid_repos_entry_falls_through_to_top_level(bad):
    assert land_limit_setting({"land_limit": 3, "repos": {"r": {"land_limit": bad}}}, "r") == 3


def test_malformed_repos_falls_through_to_top_level():
    assert land_limit_setting({"land_limit": 3, "repos": ["r"]}, "r") == 3
    assert land_limit_setting({"land_limit": 3, "repos": {"r": 5}}, "r") == 3


def write_overlay(tmp_path, text):
    path = tmp_path / "repo" / land_limit.OVERLAY
    path.parent.mkdir(parents=True)
    path.write_text(text, encoding="utf-8")
    return str(tmp_path / "repo")


def test_edge_reads_overlay_for_directory_name(tmp_path):
    repo = write_overlay(tmp_path, "land_limit: 2\nrepos:\n  repo:\n    land_limit: 4\n")
    assert repo_land_limit(repo) == 4


def test_edge_reads_top_level(tmp_path):
    assert repo_land_limit(write_overlay(tmp_path, "land_limit: 5\n")) == 5


def test_edge_missing_file_is_one(tmp_path):
    assert repo_land_limit(str(tmp_path / "repo")) == 1


def test_edge_invalid_yaml_is_one(tmp_path):
    assert repo_land_limit(write_overlay(tmp_path, "repos: [unclosed\n")) == 1


def test_edge_non_mapping_yaml_is_one(tmp_path):
    assert repo_land_limit(write_overlay(tmp_path, "- a\n")) == 1
