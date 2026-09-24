"""The coxswain pointer package: name, version, and its trusted-publish workflow."""
import re
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
# A release (0.2.0) or a pre-release of one (0.1.0b1). The beta suffix was
# required until 0.2.0: a zero major already says the interface may move, so
# saying it twice only cost pre-release resolution in pip and uv.
PYPROJECT_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:b\d+)?$")


def _load_toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text())


def _normalize(manifest_version: str) -> str:
    return manifest_version.replace("-beta.", "b")


def test_project_is_named_coxswain_with_a_release_version():
    pyproject = _load_toml("pyproject.toml")
    assert pyproject["project"]["name"] == "coxswain"
    assert PYPROJECT_VERSION.match(pyproject["project"]["version"])


def test_pyproject_and_manifest_versions_denote_the_same_release():
    pyproject = _load_toml("pyproject.toml")
    manifest = _load_toml("manifest.toml")
    assert _normalize(manifest["coxswain"]["version"]) == pyproject["project"]["version"]


def test_publish_workflow_is_a_trusted_publisher_on_tags():
    workflow = yaml.safe_load((ROOT / ".github/workflows/publish.yml").read_text())
    # PyYAML parses the bare `on:` key as boolean True (YAML 1.1), not the string "on".
    assert workflow[True]["push"]["tags"] == ["v*"]
    job = workflow["jobs"]["publish"]
    assert job["environment"] == "pypi"
    assert job["permissions"] == {"id-token": "write", "contents": "read"}
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    runs = [step["run"] for step in job["steps"] if "run" in step]
    assert "pypa/gh-action-pypi-publish@release/v1" in uses
    assert any("uv build" in run for run in runs)


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")
def test_the_built_wheel_never_ships_devtools(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=ROOT, check=True, capture_output=True)
    (wheel,) = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert any(n.startswith("coxswain/") for n in names)
    assert not [n for n in names if n.startswith("devtools/") or "/devtools/" in n]
