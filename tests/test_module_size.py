"""H9 (docs/hardening.md): no module in agent_tools or the harness exceeds 2,000 lines.

A ratchet while the split lands: the modules still over the limit are listed with their length now, and may only
shrink. A listed module that is split below the limit leaves the list in the same change; any other module over the
limit fails, so a new monolith is caught the day it crosses the line.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGES = ("src/tools/agent_tools", "src/graphs/harness")
LIMIT = 2000

# The modules over LIMIT, each at most this many lines. Empty when H9 is done.
OVER_LIMIT: dict[str, int] = {
    "src/graphs/harness/cli.py": 2187,
    "src/tools/agent_tools/chair_exec.py": 2633,
    "src/tools/agent_tools/chair_facts.py": 2224,
    "src/tools/agent_tools/dash_feed.py": 2384,
    "src/tools/agent_tools/route.py": 3016,
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
