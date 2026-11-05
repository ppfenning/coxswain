"""A build that returns nothing because its base lacks the ticket's files is `stale_base`, not `no_work`."""

# ruff: noqa: F811  (cart and repo are imported fixtures that each test takes by name)

from __future__ import annotations

from datetime import UTC, datetime

from harness.store_migrate import open_store
from harness.store_write import Store
from runner.protocol import RunnerError
from tests.test_epic_driver import (  # noqa: F401 -- cart and repo are fixtures
    Runner,
    cart,
    drive,
    initiative,
    repo,
)

TASK = "t1-probe"


class FailingBuild(Runner):
    """Every build call raises `message`, the way the runner reports a build that produced no patch."""

    def __init__(self, message: str) -> None:
        super().__init__({})
        self.message = message

    def run(self, *, role, **kw):
        if role == "build":
            raise RunnerError(self.message)
        return super().run(role=role, **kw)


def _one_task(surfaces: list[str], body: str = "read the vendor schema") -> dict:
    work = initiative(two_phases=False)
    probe = {**work["items"][0], "surfaces": surfaces, "body": body}
    return {**work, "items": [probe]}


def _quarantine_of(repo, cart, tmp_path, message: str, surfaces: list[str]):
    store = Store(open_store("sqlite:///:memory:", datetime.now(UTC).isoformat()))
    result, _ = drive(
        repo, cart, tmp_path, runner=FailingBuild(message), work=_one_task(surfaces), store=store, run_id="epic-stale"
    )
    entry = next(q for q in result["quarantined"] if q["id"] == TASK)
    attempt = store.conn.query_one(f"SELECT kind, cause FROM attempts WHERE task_id = '{TASK}'")
    return entry, attempt


def test_a_patch_empty_build_whose_surface_is_absent_is_stale_base(repo, cart, tmp_path) -> None:
    # One surface exists, so the surface preflight passes; the other is missing from the level worktree.
    surfaces = ["README.md", "src/absent_module.py"]
    entry, attempt = _quarantine_of(repo, cart, tmp_path, "node 'build' failed: patch_empty", surfaces)

    assert entry["kind"] == "stale_base"
    assert "src/absent_module.py" in entry["reason"]
    assert "patch_kept" not in entry
    assert tuple(attempt) == ("stale_base", "harness")


def test_a_patch_empty_build_whose_files_all_exist_stays_no_work(repo, cart, tmp_path) -> None:
    entry, attempt = _quarantine_of(repo, cart, tmp_path, "node 'build' failed: patch_empty", ["README.md"])

    assert entry["kind"] == "no_work"
    assert attempt[0] == "no_work"


def test_a_failure_that_is_not_patch_empty_stays_no_work_with_a_missing_file(repo, cart, tmp_path) -> None:
    surfaces = ["README.md", "src/absent_module.py"]
    entry, attempt = _quarantine_of(repo, cart, tmp_path, "provider-side safeguard error", surfaces)

    assert entry["kind"] == "no_work"
    assert attempt[0] == "no_work"


def test_a_surface_marked_new_never_counts_as_missing(repo, cart, tmp_path) -> None:
    surfaces = ["README.md", "src/new_thing.py (new)"]
    entry, attempt = _quarantine_of(repo, cart, tmp_path, "node 'build' failed: patch_empty", surfaces)

    assert entry["kind"] == "no_work"
    assert attempt[0] == "no_work"
