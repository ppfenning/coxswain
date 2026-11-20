"""The cli facade keeps its dunders to itself: a write or a reload never reaches the parts' own (H9 follow-up)."""

from __future__ import annotations

import importlib
from collections.abc import Iterator

import pytest

from agent_tools import cli
from core import module_facade


@pytest.fixture
def doc() -> Iterator[None]:
    original = cli.__doc__
    yield
    cli.__doc__ = original


def test_setting_the_facade_doc_leaves_every_part_doc_alone(doc: None) -> None:
    held = module_facade.parts(cli)
    before = [part.__doc__ for part in held]
    cli.__doc__ = "x"
    assert cli.__doc__ == "x"
    assert [part.__doc__ for part in held] == before


def test_each_part_keeps_its_own_spec_name_through_a_reload() -> None:
    held = module_facade.parts(cli)
    names = [part.__spec__.name for part in held]
    assert "agent_tools.cli" not in names
    assert importlib.reload(cli) is cli
    assert [part.__spec__.name for part in held] == names
    assert cli.__spec__.name == "agent_tools.cli"
