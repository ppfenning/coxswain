import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "docs"))

import _pull  # noqa: E402


def _load_toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text())


def _load_mkdocs() -> dict:
    return yaml.safe_load((ROOT / "mkdocs.yml").read_text())


def test_mkdocs_status_matches_manifest_status():
    mkdocs = _load_mkdocs()
    manifest = _load_toml("manifest.toml")
    assert mkdocs["extra"]["status"] == manifest["coxswain"]["status"]


def test_plan_yields_one_pair_per_repo_component_and_skips_path_components():
    manifest = {
        "components": {
            "cartridges": {"repo": "ppfenning/coxswain-cartridges", "tag": "v0.1.0-beta.1"},
            "tools": {"repo": "ppfenning/coxswain-tools", "tag": "v0.1.0-beta.1"},
            "desktop": {"path": "desktop/", "flag": "desktop"},
        }
    }

    result = _pull.plan(manifest)

    assert result == [
        (
            "https://raw.githubusercontent.com/ppfenning/coxswain-cartridges/v0.1.0-beta.1/README.md",
            "docs/components/cartridges/README.md",
        ),
        (
            "https://raw.githubusercontent.com/ppfenning/coxswain-tools/v0.1.0-beta.1/README.md",
            "docs/components/tools/README.md",
        ),
    ]
