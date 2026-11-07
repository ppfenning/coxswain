from agent_tools import source_github
from agent_tools.sources import SourceConfig

_BASE = {"title": "t", "body": "b", "url": "https://github.com/acme/widgets/issues/1"}


def test_read_carries_labels_and_milestone():
    raw = {**_BASE, "labels": [{"name": "intake"}, {"name": "bug"}], "milestone": {"title": "v1"}}
    candidate = source_github.read(raw)
    assert candidate.labels == frozenset({"intake", "bug"})
    assert candidate.milestone == "v1"


def test_read_tolerates_a_null_milestone():
    candidate = source_github.read({**_BASE, "labels": [{"name": "intake"}], "milestone": None})
    assert candidate.labels == frozenset({"intake"})
    assert candidate.milestone == ""


def test_read_tolerates_missing_labels_and_milestone():
    candidate = source_github.read(_BASE)
    assert candidate.labels == frozenset()
    assert candidate.milestone == ""


def test_list_argv_requests_the_milestone_field():
    config = SourceConfig(repos=("acme/widgets",), filter="label:intake", token_env="GH_TOKEN")
    argv = source_github.list_argv(config, "acme/widgets")
    assert "milestone" in argv[argv.index("--json") + 1].split(",")
