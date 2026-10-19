"""The manifest's shape, checked with literals: this is what `cox` and `devtools release` read."""
import tomllib
from pathlib import Path

from agent_tools.release_version import is_calver, release_key

MANIFEST = Path(__file__).resolve().parent.parent / "manifest.toml"


def _load() -> dict:
    return tomllib.loads(MANIFEST.read_text())


def test_version_is_calver_and_status_is_named():
    m = _load()
    assert is_calver(m["coxswain"]["version"])
    assert m["coxswain"]["status"] in {"beta", "stable"}


def test_only_dash_is_a_component_and_it_lives_in_its_own_repo():
    m = _load()
    assert list(m["components"]) == ["dash"]
    dash = m["components"]["dash"]
    assert dash["repo"] == "ppfenning/coxswain-dash"
    assert dash["flag"] == "dash"
    assert dash["provides"] == "towpath"
    assert release_key(dash["tag"]) is not None
    assert dash["docs"] and all(isinstance(d, str) for d in dash["docs"])


def test_providers_declare_status_and_supported_ones_a_profile():
    m = _load()
    for name, p in m["providers"].items():
        assert p["status"] in {"supported", "experimental", "planned"}, name
        if p["status"] in {"supported", "experimental"}:
            assert p["profile"].endswith(".yaml"), name
