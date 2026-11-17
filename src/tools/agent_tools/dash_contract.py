"""The dash contract: the version every feed and detail snapshot carries as `schema`, and the published list of the
fixtures that fix its shape.

Towpath (coxswain-dash) does not keep its own copies of these fixtures. It reads them from this repository at one
pinned commit, recorded in its `coxswain-contract.toml`, and its CI fails when its vendored set differs from the set
at that pin. `tests/fixtures/dash_contract.json` is what it fetches first: the contract version and the fixture names.

`CONTRACT_VERSION` moves only on a breaking change (a key removed or renamed, a type changed). An added key keeps the
version, since a towpath that does not know the key ignores it. Towpath refuses a snapshot whose `schema` is not the
version it was built for, with a message saying which side to upgrade.

Pure: `manifest_findings` takes the parsed manifest and fixtures and returns what is wrong with them."""

from __future__ import annotations

from collections.abc import Mapping

CONTRACT_VERSION = 1
MANIFEST_NAME = "dash_contract.json"
FIXTURE_PREFIX = "dash_"


def manifest_findings(manifest: Mapping, fixtures: Mapping[str, object], version: int) -> tuple[str, ...]:
    """What is wrong with `manifest` (the parsed `dash_contract.json`) against `fixtures` (every `dash_*.json` in the
    fixtures folder but the manifest itself, by file name, parsed) at contract `version`; empty when nothing is.

    The manifest must carry `version` as `contract`, list its fixtures sorted and once each, list only fixtures that
    exist and list every one that does; a listed fixture that carries `schema` must carry `version`."""
    listed = manifest.get("fixtures")
    if not isinstance(listed, list) or not all(isinstance(name, str) for name in listed):
        return ("the manifest's `fixtures` is not a list of file names",)
    contract = manifest.get("contract")
    version_findings = (
        () if contract == version
        else (f"the manifest says contract {contract!r}, the producers send {version}",)
    )
    order_findings = () if listed == sorted(set(listed)) else ("the manifest's `fixtures` is not sorted and unique",)
    missing = tuple(f"listed but not in the fixtures folder: {name}" for name in sorted(set(listed) - set(fixtures)))
    unlisted = tuple(f"in the fixtures folder but not listed: {name}" for name in sorted(set(fixtures) - set(listed)))
    stale = tuple(
        f"{name} carries schema {body['schema']!r}, not contract {version}"
        for name in sorted(set(listed) & set(fixtures))
        if isinstance(body := fixtures[name], Mapping) and "schema" in body and body["schema"] != version
    )
    return version_findings + order_findings + missing + unlisted + stale
