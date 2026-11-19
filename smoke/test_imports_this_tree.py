"""The suite's in-process imports come from this tree, not from the checkout the runner's venv installed.

A chair lane's venv can carry an editable install of an older checkout. `smoke/conftest.py` imports the forge's parser
from `agent_tools`, which imports `core` and `harness`; if only `src/tools` is put ahead on `sys.path`, those two still
resolve through the venv, and the suite fails to load on a name this tree has but that checkout lacks.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("name", ["agent_tools", "harness", "core"])
def test_the_source_packages_import_from_this_tree(name: str) -> None:
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve().is_relative_to(ROOT), f"{name} imported from {module.__file__}, not {ROOT}"
