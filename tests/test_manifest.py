"""The manifest's shape, checked with literals: this is what `cox` will read."""
import re
import tomllib
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "manifest.toml"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-beta\.\d+)?$")


def _load() -> dict:
    return tomllib.loads(MANIFEST.read_text())


def test_version_is_semver_with_optional_beta():
    m = _load()
    assert SEMVER.match(m["coxswain"]["version"])
    assert m["coxswain"]["status"] in {"beta", "stable"}


def test_every_component_is_pinned_in_lockstep_or_lives_here():
    m = _load()
    tag = "v" + m["coxswain"]["version"]
    for name, c in m["components"].items():
        assert ("repo" in c) != ("path" in c), name
        if "repo" in c:
            assert c["tag"] == tag, f"{name} is not pinned to {tag}"
            assert c["repo"].startswith("ppfenning/coxswain-"), name
        assert c.get("required") or c.get("flag"), f"{name} is neither required nor optional"


def test_exactly_one_component_provides_cox():
    m = _load()
    assert [n for n, c in m["components"].items() if c.get("provides") == "cox"] == ["tools"]


def test_providers_declare_status_and_supported_ones_a_profile():
    m = _load()
    for name, p in m["providers"].items():
        assert p["status"] in {"supported", "planned"}, name
        if p["status"] == "supported":
            assert p["profile"].endswith(".yaml"), name
