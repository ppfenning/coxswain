from collections.abc import Callable, Hashable, Iterable, Mapping
from types import MappingProxyType

Memo = Mapping[str, tuple[Hashable, object]]

EMPTY: Memo = MappingProxyType({})


def reuse(memo: Memo, name: str, key: Hashable, compute: Callable[[], object]) -> tuple[object, Memo, bool]:
    """Return (value, memo, reused). A stored key equal to `key` skips `compute`; otherwise the result is stored in a new memo."""
    if name in memo and memo[name][0] == key:
        return memo[name][1], memo, True
    value = compute()
    return value, MappingProxyType({**memo, name: (key, value)}), False


def split_reuse(names: Iterable[str], results: Iterable[tuple[str, bool]]) -> tuple[list[str], list[str]]:
    """Split (name, reused) pairs into (recomputed, reused), each in the order of `names`.

    A name with no result is in neither list. The pair feeds cost_block, which checks the cover of SECTION_ORDER.
    """
    flags = dict(results)
    wanted = list(names)
    return (
        [n for n in wanted if n in flags and not flags[n]],
        [n for n in wanted if flags.get(n)],
    )
