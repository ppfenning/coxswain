"""The epic-swarm driver: a whole initiative, phase by phase, landing nothing.

NOT a graph, and it must never acquire a `SPEC`. It walks a phase graph, blocks
on fan-outs, creates branches, runs checks and merges into a stack — a graph is
`run(args, runner) -> dict`, pure and replayable, and this is none of those. It
lives beside `phase.py` and `invoke.py`, which is where the contract already put
everything that owns a side effect.

What it does, per phase, in this order and no other:

    branch from the parent phase's head -> fan out `lifecycle` per ready task
    -> apply, check and commit each patch in a worktree the harness owns
    -> escalate anything whose diff touched governance
    -> VALIDATE (`phase-validate`, invoked like any other graph)
    -> one gate for the phase -> execute what was cleared -> record the phase

Validation sits between the fan-out and the merges on purpose. The phase verdict
judges the union of what the tasks produced, before any of it is joined to the
phase branch, so a phase that does not add up is caught while nothing has moved.
The alternative — merge first, re-read the branch afterwards — asks the verdict
to be about a state the driver has already committed to.

**Nothing here merges to a default branch.** There is no code path that emits or
executes `merge_main`; the swarm's terminal state is branches and proposals. See
the comment where the merges execute.

Two things are load-bearing about the record. Every phase records its own
manifest under `f"{run_id}:{phase}"` — one cartridge, one scope, so
`_require_single_scope` stays satisfiable — and an auto-cleared proposal gets no
gate diff and no ledger row, because autonomy is spent by acting and re-earned
only at a gate.

Checks are the cartridge's `landing_areas.checks` plus whatever the repository
itself declares in a root `.agent-checks` file, read once at the edge and
merged in by `Ctx.checks`.
"""

from __future__ import annotations

from typing import Any

from core import module_facade
from harness.epic_branches import resolve_default_ref as resolve_default_ref
from harness.epic_build import work_state_of as work_state_of
from harness.epic_context import Ctx as Ctx
from harness.epic_context import PoolSetting as PoolSetting
from harness.epic_context import phase_order as phase_order
from harness.epic_execute import rescue_task as rescue_task
from harness.epic_phase_branch import branch_action as branch_action
from harness.epic_records import PRINCIPAL as PRINCIPAL
from harness.epic_records import mirror_read as mirror_read
from harness.epic_run import run_epic as run_epic
from harness.waves import phase_parents as phase_parents

__all__ = ["PoolSetting", "branch_action", "phase_order", "phase_parents", "rescue_task", "run_epic"]


# H9 (docs/hardening.md): the driver lives in the part modules above, one stage each.
# This module answers for every name they define, and a write (a test's monkeypatch) reaches every part that
# holds the name (core.module_facade). Callers' names are re-exported here, typed; the rest resolve on read.
def __getattr__(name: str) -> Any:
    return module_facade.attribute(__name__, name)


module_facade.install(
    __name__,
    (
        "harness.epic_context",
        "harness.epic_phase_branch",
        "harness.epic_records",
        "harness.epic_branches",
        "harness.epic_build",
        "harness.epic_batch",
        "harness.epic_execute",
        "harness.epic_triage",
        "harness.epic_phase",
        "harness.epic_run",
    ),
    eager=True,
)
