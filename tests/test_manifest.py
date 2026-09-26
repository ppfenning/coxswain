"""The manifest's shape, checked with literals: this is what `cox` will read."""
import re
import tomllib
from pathlib import Path

MANIFEST = Path(__file__).resolve().parent.parent / "manifest.toml"
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-beta\.\d+)?$")


def _key(tag: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", tag.split("-")[0]))


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
            if c.get("lockstep", True):
                # Changed-only versioning (Pat, 2026-09-23): a component with no changes since its last tag keeps
                # it, so a lockstep component sits at this release or an earlier one, never a later one.
                assert SEMVER.match(c["tag"].lstrip("v")), f"{name} pins a non-semver tag {c['tag']}"
                assert _key(c["tag"]) <= _key(tag), f"{name} is pinned past {tag}"
            else:
                # A `lockstep = false` component keeps the tag it names (crew,
                # from 0.8.0); it must still be a real release tag.
                assert SEMVER.match(c["tag"].lstrip("v")), f"{name} pins a non-semver tag {c['tag']}"
            assert c["repo"].startswith("ppfenning/coxswain-"), name
        assert c.get("required") or c.get("flag"), f"{name} is neither required nor optional"
        if "docs" in c:
            assert c["docs"], f"{name} declares an empty docs list"
            assert all(isinstance(d, str) for d in c["docs"]), f"{name} docs must be paths"


def test_exactly_one_component_provides_cox():
    m = _load()
    assert [n for n, c in m["components"].items() if c.get("provides") == "cox"] == ["tools"]


def test_providers_declare_status_and_supported_ones_a_profile():
    m = _load()
    for name, p in m["providers"].items():
        assert p["status"] in {"supported", "experimental", "planned"}, name
        if p["status"] in {"supported", "experimental"}:
            assert p["profile"].endswith(".yaml"), name
