"""Pure core for the routing layer: profile parsing and naming, plus the one thin edge `cox route file`
writes a file through. Every function here but `write_filed_item`, `set_initiative_priority` and
`list_open_queue` takes plain arguments and returns plain values, with no file reads and no env access.
`set_initiative_priority` is a second edge that writes through `write_filed_item`, and `list_open_queue` an edge
that only reads the store. `write_filed_item` is the exception the name says it is: it
decides through two pure helpers (`_unrecorded_row_warning`, `_filed_write_error`) what to print, and does
no more I/O than the one store call and the one file write the ticket asks it to order."""

from __future__ import annotations

from typing import Any

from agent_tools.route_lint import Problem as Problem
from agent_tools.route_lint import lint_items as lint_items
from agent_tools.route_lint import merge_same_phase as merge_same_phase
from agent_tools.route_plan import _TIER_LADDER as _TIER_LADDER
from agent_tools.route_plan import context_document as context_document
from agent_tools.route_plan import context_from_rows as context_from_rows
from agent_tools.route_plan import context_rows as context_rows
from agent_tools.route_plan import current_priority as current_priority
from agent_tools.route_plan import initiative_repo as initiative_repo
from agent_tools.route_plan import intake_priority_rows as intake_priority_rows
from agent_tools.route_plan import is_chair_machine as is_chair_machine
from agent_tools.route_plan import launch_claim as launch_claim
from agent_tools.route_plan import launch_claim_gate as launch_claim_gate
from agent_tools.route_plan import launch_from_rows as launch_from_rows
from agent_tools.route_plan import launch_gate as launch_gate
from agent_tools.route_plan import launch_input as launch_input
from agent_tools.route_plan import list_open_queue as list_open_queue
from agent_tools.route_plan import next_priority as next_priority
from agent_tools.route_plan import overlay as overlay
from agent_tools.route_plan import priority_rows as priority_rows
from agent_tools.route_plan import priority_target as priority_target
from agent_tools.route_plan import priority_text as priority_text
from agent_tools.route_plan import ready_tasks as ready_tasks
from agent_tools.route_plan import render_context as render_context
from agent_tools.route_plan import run_under_claim as run_under_claim
from agent_tools.route_plan import set_initiative_priority as set_initiative_priority
from agent_tools.route_plan import set_intake_priority as set_intake_priority
from agent_tools.route_plan import should_export_board as should_export_board
from agent_tools.route_plan import write_filed_item as write_filed_item
from agent_tools.route_profile import _JSON_KEYS as _JSON_KEYS
from agent_tools.route_profile import _SPEND_KEYS as _SPEND_KEYS
from agent_tools.route_profile import _SPEND_TEXT_KEYS as _SPEND_TEXT_KEYS
from agent_tools.route_profile import ProfileError as ProfileError
from agent_tools.route_profile import RepoRefused as RepoRefused
from agent_tools.route_profile import _yaml_scalar as _yaml_scalar
from agent_tools.route_profile import harness_argv as harness_argv
from agent_tools.route_profile import initiative_files as initiative_files
from agent_tools.route_profile import intake_file as intake_file
from agent_tools.route_profile import is_git_checkout as is_git_checkout
from agent_tools.route_profile import next_run_id as next_run_id
from agent_tools.route_profile import parse_profile as parse_profile
from agent_tools.route_profile import pull_plan as pull_plan
from agent_tools.route_profile import repo_field as repo_field
from agent_tools.route_profile import resolve_repo as resolve_repo
from agent_tools.route_profile import review_argv as review_argv
from agent_tools.route_profile import slugify as slugify
from agent_tools.route_profile import surface_candidates as surface_candidates
from agent_tools.route_profile import unknown_keys as unknown_keys
from agent_tools.route_status import DEFAULT_LAND_AT as DEFAULT_LAND_AT
from agent_tools.route_status import FENCED_NOTE as FENCED_NOTE
from agent_tools.route_status import TERMINAL as TERMINAL
from agent_tools.route_status import child_env as child_env
from agent_tools.route_status import initiative_states as initiative_states
from agent_tools.route_status import initiative_summaries as initiative_summaries
from agent_tools.route_status import intake_entries as intake_entries
from agent_tools.route_status import intake_groups as intake_groups
from agent_tools.route_status import is_merged as is_merged
from agent_tools.route_status import is_met_need as is_met_need
from agent_tools.route_status import latest_groups_file as latest_groups_file
from agent_tools.route_status import launch_blockers as launch_blockers
from agent_tools.route_status import parse_frontmatter as parse_frontmatter
from agent_tools.route_status import parse_pid as parse_pid
from agent_tools.route_status import render_status as render_status
from agent_tools.route_status import run_entries as run_entries
from agent_tools.route_status import state_problems as state_problems
from agent_tools.route_status import state_word as state_word
from agent_tools.route_status import status_entries as status_entries
from agent_tools.route_status import status_rows as status_rows
from agent_tools.route_status import with_store_states as with_store_states
from agent_tools.route_status import work_item as work_item
from core import module_facade

__all__ = [
    "Problem",
    "ProfileError",
    "child_env",
    "context_document",
    "context_from_rows",
    "context_rows",
    "current_priority",
    "harness_argv",
    "initiative_files",
    "initiative_repo",
    "initiative_summaries",
    "intake_entries",
    "intake_file",
    "intake_priority_rows",
    "is_chair_machine",
    "latest_groups_file",
    "launch_blockers",
    "launch_claim",
    "launch_claim_gate",
    "launch_from_rows",
    "launch_gate",
    "launch_input",
    "lint_items",
    "list_open_queue",
    "merge_same_phase",
    "next_priority",
    "next_run_id",
    "overlay",
    "parse_frontmatter",
    "parse_pid",
    "parse_profile",
    "priority_rows",
    "priority_target",
    "priority_text",
    "pull_plan",
    "ready_tasks",
    "render_context",
    "render_status",
    "review_argv",
    "run_entries",
    "run_under_claim",
    "set_initiative_priority",
    "set_intake_priority",
    "should_export_board",
    "slugify",
    "state_problems",
    "status_entries",
    "status_rows",
    "surface_candidates",
    "unknown_keys",
    "work_item",
    "write_filed_item",
]


# H9 (docs/hardening.md): the routing core lives in the part modules above.
# This module answers for every name they define, and a write (a test's monkeypatch) reaches every part that
# holds the name (core.module_facade). Callers' names are re-exported here, typed; the rest resolve on read.
def __getattr__(name: str) -> Any:
    return module_facade.attribute(__name__, name)


module_facade.install(
    __name__,
    (
        "agent_tools.route_status",
        "agent_tools.route_profile",
        "agent_tools.route_lint",
        "agent_tools.route_plan",
    ),
    eager=True,
)
