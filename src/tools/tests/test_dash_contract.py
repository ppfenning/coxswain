"""The dash contract manifest: the pure check on example inputs, then the same check on the published fixtures."""

import json
from pathlib import Path

from agent_tools import dash_contract, dash_feed
from agent_tools.dash_contract import CONTRACT_VERSION, MANIFEST_NAME, manifest_findings

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_GOOD = {"contract": 1, "fixtures": ["dash_a_v1.json", "dash_b_v1.json"]}
_FIXTURES = {"dash_a_v1.json": {"schema": 1, "at": ""}, "dash_b_v1.json": {"board": {}}}


def test_a_manifest_that_lists_every_fixture_at_the_version_has_no_findings():
    assert manifest_findings(_GOOD, _FIXTURES, 1) == ()


def test_a_manifest_at_another_version_is_a_finding():
    assert manifest_findings({**_GOOD, "contract": 2}, _FIXTURES, 1) == (
        "the manifest says contract 2, the producers send 1",
    )


def test_a_fixture_the_manifest_does_not_list_is_a_finding():
    fixtures = {**_FIXTURES, "dash_c_v1.json": {}}
    assert manifest_findings(_GOOD, fixtures, 1) == ("in the fixtures folder but not listed: dash_c_v1.json",)


def test_a_listed_fixture_that_is_gone_is_a_finding():
    fixtures = {"dash_a_v1.json": _FIXTURES["dash_a_v1.json"]}
    assert manifest_findings(_GOOD, fixtures, 1) == ("listed but not in the fixtures folder: dash_b_v1.json",)


def test_a_fixture_carrying_another_schema_is_a_finding():
    fixtures = {**_FIXTURES, "dash_a_v1.json": {"schema": 2}}
    assert manifest_findings(_GOOD, fixtures, 1) == ("dash_a_v1.json carries schema 2, not contract 1",)


def test_an_unsorted_or_repeated_list_is_a_finding():
    manifest = {"contract": 1, "fixtures": ["dash_b_v1.json", "dash_a_v1.json", "dash_a_v1.json"]}
    assert manifest_findings(manifest, _FIXTURES, 1) == ("the manifest's `fixtures` is not sorted and unique",)


def test_a_manifest_without_a_fixture_list_is_one_finding():
    assert manifest_findings({"contract": 1}, _FIXTURES, 1) == ("the manifest's `fixtures` is not a list of file names",)


def test_the_published_manifest_matches_the_fixtures_and_the_producers_version():
    """What towpath fetches at its pin: every `dash_*.json` here, listed, at the version the producers send."""
    manifest = json.loads((_FIXTURES_DIR / MANIFEST_NAME).read_text(encoding="utf-8"))
    fixtures = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in _FIXTURES_DIR.glob(f"{dash_contract.FIXTURE_PREFIX}*.json")
        if path.name != MANIFEST_NAME
    }
    assert manifest_findings(manifest, fixtures, CONTRACT_VERSION) == ()


def test_the_feed_snapshot_carries_the_contract_version():
    feed = dash_feed.snapshot("2026-09-29T00:00:00Z", {}, {}, [], [], [], 0, [], 0, [])
    assert feed["schema"] == CONTRACT_VERSION
