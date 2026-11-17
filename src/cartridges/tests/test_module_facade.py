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


def test_an_eager_install_imports_the_parts_now() -> None:
    _module(PART_A, "def helper():\n    return 'real'\n")
    module = _module(FACADE, "")
    try:
        sys.modules.pop(PART_A)
        with pytest.raises(ModuleNotFoundError):  # proof the parts are imported at install: this one is gone
            module_facade.install(FACADE, (PART_A,), eager=True)
        assert isinstance(module, module_facade.FacadeModule)
    finally:
        sys.modules.pop(FACADE, None)


def test_a_builtin_shadowed_on_the_facade_reaches_every_part_and_its_undo_takes_it_out(facade: types.ModuleType) -> None:
    _module(PART_A, "def shout():\n    return len('abc')\n")
    module_facade.install(FACADE, (PART_A, PART_B))
    facade.len = lambda _value: 99
    assert sys.modules[PART_A].shout() == 99
    del facade.len
    assert "len" not in sys.modules[PART_A].__dict__
    assert sys.modules[PART_A].shout() == 3


def test_a_restored_name_forgets_what_the_parts_held(facade: types.ModuleType) -> None:
    """Regression: test A patched a part directly, then the facade, and undid both; test B's later patch and undo of
    the facade put test A's fake back into the part, from the record the first write had kept."""
    part_a = sys.modules[PART_A]
    real = part_a.helper
    original = module_facade.attribute(FACADE, "helper")
    fake_a = lambda: "a"  # noqa: E731 - test A's direct patch of the part
    part_a.helper = fake_a  # test A
    facade.helper = lambda: "facade"
    facade.helper = original  # test A's undo, in reverse order
    part_a.helper = real
    facade.helper = lambda: "b"  # test B
    facade.helper = original  # test B's undo
    assert part_a.helper is real
    assert sys.modules[PART_B].twice() == "realreal"
