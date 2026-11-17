"""A split module stays one namespace: reads find the parts' names, writes and their undo reach every part (H9)."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator

import pytest

from core import module_facade

PART_A = "facade_probe_part_a"
PART_B = "facade_probe_part_b"
FACADE = "facade_probe"


def _module(name: str, source: str) -> types.ModuleType:
    module = types.ModuleType(name)
    sys.modules[name] = module
    exec(source, module.__dict__)  # the probe modules are this test's own source
    return module


@pytest.fixture
def facade() -> Iterator[types.ModuleType]:
    """A facade over two parts: `b.twice` calls `helper`, which it imported from `a`, as a moved function would."""
    _module(PART_A, "def helper():\n    return 'real'\n")
    _module(PART_B, f"from {PART_A} import helper\n\ndef twice():\n    return helper() * 2\n")
    module = _module(FACADE, "")
    module_facade.install(FACADE, (PART_A, PART_B))
    yield module
    for name in (FACADE, PART_A, PART_B):
        sys.modules.pop(name, None)


def test_a_read_finds_the_name_in_the_first_part_that_defines_it(facade: types.ModuleType) -> None:
    assert module_facade.attribute(FACADE, "helper") is sys.modules[PART_A].helper
    assert module_facade.attribute(FACADE, "twice")() == "realreal"


def test_a_name_no_part_defines_is_an_attribute_error(facade: types.ModuleType) -> None:
    with pytest.raises(AttributeError, match="no attribute 'missing'"):
        module_facade.attribute(FACADE, "missing")


def test_a_dunder_is_never_looked_up_in_the_parts(facade: types.ModuleType) -> None:
    sys.modules[PART_A].__probe__ = 1
    with pytest.raises(AttributeError):
        module_facade.attribute(FACADE, "__probe__")


def test_a_write_reaches_every_part_and_restoring_the_original_puts_each_part_back(facade: types.ModuleType) -> None:
    original = module_facade.attribute(FACADE, "helper")
    facade.helper = lambda: "fake"
    assert sys.modules[PART_B].twice() == "fakefake"
    assert sys.modules[PART_A].helper() == "fake"
    facade.helper = original
    assert sys.modules[PART_B].twice() == "realreal"
    assert sys.modules[PART_A].helper is original


def test_a_delete_puts_each_part_back(facade: types.ModuleType) -> None:
    original = sys.modules[PART_A].helper
    facade.helper = lambda: "fake"
    del facade.helper
    assert sys.modules[PART_A].helper is original
    assert sys.modules[PART_B].twice() == "realreal"


def test_nested_writes_unwind_in_order(facade: types.ModuleType) -> None:
    original = module_facade.attribute(FACADE, "helper")
    first, second = (lambda: "one"), (lambda: "two")
    facade.helper = first
    facade.helper = second
    assert sys.modules[PART_B].twice() == "twotwo"
    facade.helper = first
    assert sys.modules[PART_B].twice() == "oneone"
    facade.helper = original
    assert sys.modules[PART_B].twice() == "realreal"


def test_a_name_only_the_facade_holds_is_written_to_the_facade_alone(facade: types.ModuleType) -> None:
    facade.own = 3
    assert facade.own == 3
    assert not hasattr(sys.modules[PART_A], "own")
    assert not hasattr(sys.modules[PART_B], "own")


def test_install_imports_no_part() -> None:
    module = _module(FACADE, "")
    try:
        module_facade.install(FACADE, ("facade_probe_never_imported",))
        assert "facade_probe_never_imported" not in sys.modules
        assert isinstance(module, module_facade.FacadeModule)
    finally:
        sys.modules.pop(FACADE, None)
