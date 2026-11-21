"""L3: the phase-level smoke check (`pytest -q -rs smoke -m "not forge"`) needs no forge.

- **Git-only behaviours.** Each behaviour the lander and the phase roll-up rely on that is only about git (push after
  merge, re-merge, roll-up, resume from task commits) names, in `plan.GIT_ONLY_BEHAVIOURS`, smoke tests that carry no
  `forge` mark. A behaviour whose test L1 has not landed yet sits in `plan.GIT_ONLY_PENDING`, which fails here once
  the test exists, so the list cannot go stale.
- **No Forgejo.** Only the session's `forge` fixture starts a Forgejo container, and no test of this run without the
  `forge` mark requests it, directly or through another fixture. A run that deselects the forge tests therefore
  leaves no container of the Forgejo image behind, because it never starts one; this is checked from the collected
  items, without a second run.
"""

from pathlib import Path

import pytest

from smoke import plan

ROOT = Path(__file__).resolve().parent.parent


def _unmarked(path: str) -> frozenset[str] | None:
    """The tests of `path` without the forge mark, or None when the file does not exist."""
    file = ROOT / path
    return plan.unmarked_tests(file.read_text(encoding="utf-8"), plan.FORGE_MARK) if file.is_file() else None


def _missing(nodes: tuple[str, ...]) -> list[str]:
    """The node ids that do not name a test without the forge mark."""
    return [node for node in nodes if plan.split_node_id(node)[1] not in (_unmarked(plan.split_node_id(node)[0]) or ())]


@pytest.mark.parametrize("behaviour", sorted(plan.GIT_ONLY_BEHAVIOURS))
def test_each_git_only_behaviour_has_a_test_without_the_forge_mark(behaviour):
    nodes = plan.GIT_ONLY_BEHAVIOURS[behaviour]
    assert nodes, f"{behaviour}: no test named"
    assert _missing(nodes) == [], f"{behaviour}: these name no test, or one marked {plan.FORGE_MARK}"


@pytest.mark.parametrize("behaviour", sorted(plan.GIT_ONLY_PENDING))
def test_a_pending_behaviour_fails_once_its_test_lands(behaviour):
    landed = [node for node in plan.GIT_ONLY_PENDING[behaviour] if node not in _missing((node,))]
    assert landed == [], f"{behaviour}: its test has landed; move it from GIT_ONLY_PENDING to GIT_ONLY_BEHAVIOURS"


def test_only_the_forge_fixture_starts_forgejo_and_no_unmarked_test_of_this_run_requests_it(request):
    conftest = (ROOT / "smoke" / "conftest.py").read_text(encoding="utf-8")
    assert plan.callers_of(conftest, "forgejo_run_argv") == {"forge"}

    items = [item for item in request.session.items if isinstance(item, pytest.Function)]
    assert request.node in items
    marked = [item.nodeid for item in items if item.get_closest_marker(plan.FORGE_MARK)]
    assert plan.forge_users({item.nodeid: item.fixturenames for item in items}, marked) == []
