"""The thin edge of a chair tick: perform planned actions through injected callables.

Pure helpers decide what an action means; `perform` only fences, dispatches and collects results.
Argv spellings follow `cox runs land --help` and `cox route launch epic|decompose|rescue --help`.
"""

from __future__ import annotations

from typing import Any

from agent_tools.chair_exec_act import escalation as escalation
from agent_tools.chair_exec_act import lane_writer as lane_writer
from agent_tools.chair_exec_act import perform as perform
from agent_tools.chair_exec_act import phase_task_states as phase_task_states
from agent_tools.chair_exec_edge import edge_deps as edge_deps
from agent_tools.chair_exec_edge import smoke_targets as smoke_targets
from agent_tools.chair_exec_land import LAUNCH_KINDS as LAUNCH_KINDS
from agent_tools.chair_exec_land import Deps as Deps
from agent_tools.chair_exec_land import LandSink as LandSink
from agent_tools.chair_exec_land import Refusal as Refusal
from agent_tools.chair_exec_land import Result as Result
from agent_tools.chair_exec_land import already_landed as already_landed
from agent_tools.chair_exec_land import argv_for as argv_for
from agent_tools.chair_exec_land import branch_pattern as branch_pattern
from agent_tools.chair_exec_land import decompose_id as decompose_id
from agent_tools.chair_exec_land import delete_branches_with as delete_branches_with
from agent_tools.chair_exec_land import land_command as land_command
from agent_tools.chair_exec_land import land_commit as land_commit
from agent_tools.chair_exec_land import land_outcome as land_outcome
from agent_tools.chair_exec_land import land_refusal as land_refusal
from agent_tools.chair_exec_land import landed as landed
from agent_tools.chair_exec_land import runs_as_process as runs_as_process
from agent_tools.chair_exec_land import tail as tail
from core import module_facade

__all__ = [
    "LAUNCH_KINDS",
    "Deps",
    "LandSink",
    "Refusal",
    "Result",
    "already_landed",
    "argv_for",
    "branch_pattern",
    "decompose_id",
    "delete_branches_with",
    "edge_deps",
    "escalation",
    "land_command",
    "land_commit",
    "land_outcome",
    "land_refusal",
    "landed",
    "lane_writer",
    "perform",
    "phase_task_states",
    "runs_as_process",
    "smoke_targets",
    "tail",
]


# H9 (docs/hardening.md): the executor lives in the part modules above.
# This module answers for every name they define, and a write (a test's monkeypatch) reaches every part that
# holds the name (core.module_facade). Callers' names are re-exported here, typed; the rest resolve on read.
def __getattr__(name: str) -> Any:
    return module_facade.attribute(__name__, name)


module_facade.install(
    __name__,
    (
        "agent_tools.chair_exec_land",
        "agent_tools.chair_exec_act",
        "agent_tools.chair_exec_edge",
    ),
    eager=True,
)
