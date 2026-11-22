"""The records other code shares, hashes or keys on stay frozen: one guard for all of them, read off the declaration."""

from __future__ import annotations

import pytest

from agent_tools.budget import MonthBudget
from agent_tools.chair_restart_log import RestartRecord
from agent_tools.chair_schedule_graph import Node
from agent_tools.ci_gate import CiGate
from agent_tools.forge_status import ForgeStatus
from agent_tools.forgejo_api import ForgejoSettings
from agent_tools.inbox import InboxItem
from agent_tools.lake_config import LakeConfig
from agent_tools.land_batch_settings import MergeQueue
from agent_tools.pr_checks import Verdict
from agent_tools.progress_read import Progress
from agent_tools.setup_store import SharedConnection
from agent_tools.state_paths import StatePath
from agent_tools.store_url import TracesRoot
from core.bounds import ChairBounds
from harness.stack_refresh import RefreshResult
from harness.store_graphs import GraphDefinition
from harness.ticket_claims import Claim
from runner.decision_log import CallDecision
from runner.shadow_tier import ShadowConfig

RECORDS = (
    MonthBudget,
    RestartRecord,
    Node,
    CiGate,
    ForgeStatus,
    ForgejoSettings,
    InboxItem,
    LakeConfig,
    MergeQueue,
    Verdict,
    Progress,
    SharedConnection,
    StatePath,
    TracesRoot,
    ChairBounds,
    RefreshResult,
    GraphDefinition,
    Claim,
    CallDecision,
    ShadowConfig,
)


@pytest.mark.parametrize("record", RECORDS, ids=lambda record: f"{record.__module__}.{record.__name__}")
def test_the_record_is_declared_frozen(record: type) -> None:
    assert record.__dataclass_params__.frozen  # type: ignore[attr-defined]
