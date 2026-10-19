"""The coxswain pointer package: name, version, and its trusted-publish workflow."""
import importlib.util
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

from agent_tools.release_version import is_calver, pep440

ROOT = Path(__file__).resolve().parent.parent


def _load_toml(name: str) -> dict:
    return tomllib.loads((ROOT / name).read_text())


def test_project_is_named_coxswain_with_a_calver_version():
    pyproject = _load_toml("pyproject.toml")
    assert pyproject["project"]["name"] == "coxswain"
    assert is_calver(pyproject["project"]["version"])


def test_pyproject_and_manifest_versions_denote_the_same_release():
    pyproject = _load_toml("pyproject.toml")
    manifest = _load_toml("manifest.toml")
    assert pep440(manifest["coxswain"]["version"]) == pyproject["project"]["version"]


def _guard():
    spec = importlib.util.spec_from_file_location("check_release_tag", ROOT / ".github" / "check_release_tag.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, module._release_version(ROOT)


@pytest.mark.parametrize("tag, version, refused", [
    ("v2026.10.0", "2026.10.0", None),
    ("v2026.11.12", "2026.11.12", None),
    ("v2026.10.1", "2026.10.0", "does not name"),
    ("2026.10.0", "2026.10.0", "does not name"),
    ("v0.48.0", "0.48.0", "not a CalVer release"),
    ("v2026.01.0", "2026.01.0", "not a CalVer release"),
])
def test_the_publish_guard_takes_only_a_calver_tag_naming_the_pyproject_version(tag, version, refused):
    guard, rules = _guard()
    reason = guard.refusal(tag, version, rules)
    assert reason is None if refused is None else refused in reason


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
    assert runs.index(next(r for r in runs if "check_release_tag.py" in r)) < runs.index("uv build")


@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv to build the wheel")
def test_the_built_wheel_never_ships_devtools(tmp_path):
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(tmp_path)], cwd=ROOT, check=True, capture_output=True)
    (wheel,) = tmp_path.glob("*.whl")
    names = zipfile.ZipFile(wheel).namelist()
    assert any(n.startswith("coxswain/") for n in names)
    assert not [n for n in names if n.startswith("devtools/") or "/devtools/" in n]
