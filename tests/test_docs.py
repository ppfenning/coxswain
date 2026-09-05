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


def _nav_targets(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _nav_targets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _nav_targets(item)
    else:
        yield node


def test_every_doc_page_appears_in_nav():
    mkdocs = _load_mkdocs()
    nav_targets = set(_nav_targets(mkdocs["nav"]))

    docs_dir = ROOT / "docs"
    pages = set()
    for path in docs_dir.rglob("*.md"):
        parts = path.relative_to(docs_dir).parts
        if len(parts) == 3 and parts[0] == "components" and parts[2] == "README.md":
            continue
        pages.add("/".join(parts))

    missing = pages - nav_targets
    assert not missing, f"pages missing from nav: {sorted(missing)}"


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
