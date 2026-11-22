"""H9 (docs/hardening.md): no source module exceeds 2,000 lines.

A ratchet while the split lands: the modules still over the limit are listed with their length now, and may only
shrink. A listed module that is split below the limit leaves the list in the same change; any other module over the
limit fails, so a new monolith is caught the day it crosses the line.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = (
    "src/tools/agent_tools",
    "src/graphs/harness",
    "src/graphs/graphs",
    "src/graphs/runner",
    "src/cartridges/core",
    "devtools",
)
LIMIT = 2000

# The modules over LIMIT, each at most this many lines. Empty when H9 is done.
OVER_LIMIT: dict[str, int] = {
    "src/graphs/graphs/delivery/lifecycle_propose.py": 2680,
    "devtools/cli.py": 2015,
}


def line_counts() -> dict[str, int]:
    """Every module under PACKAGES, by repo-relative path, with its line count."""
    return {
        path.relative_to(ROOT).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for package in PACKAGES
        for path in sorted((ROOT / package).rglob("*.py"))
    }


def over(counts: dict[str, int], allowed: dict[str, int]) -> dict[str, int]:
    """The modules longer than their allowance: LIMIT, or their entry in `allowed`."""
    return {path: n for path, n in counts.items() if n > allowed.get(path, LIMIT)}


def test_no_module_exceeds_the_limit_or_its_ratchet():
    assert over(line_counts(), OVER_LIMIT) == {}


def test_a_module_under_the_limit_leaves_the_ratchet():
    counts = line_counts()
    assert [path for path in OVER_LIMIT if counts.get(path, 0) <= LIMIT] == []


def test_over_reads_the_limit_and_the_allowance():
    counts = {"a.py": LIMIT, "b.py": LIMIT + 1, "c.py": 2500}
    assert over(counts, {}) == {"b.py": LIMIT + 1, "c.py": 2500}
    assert over(counts, {"c.py": 2500}) == {"b.py": LIMIT + 1}
    assert over(counts, {"c.py": 2499}) == {"b.py": LIMIT + 1, "c.py": 2500}
