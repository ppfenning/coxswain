"""The edge of a chair tick: gather `chair_types.Facts` from injected sources.

The shapers are pure and `gather_facts` calls each source once. The only clock is the `now` argument.
No retry state is stored: `harness_failures` is counted from run history on every call.
"""

from agent_tools.chair_facts_derive import DEFAULT_HOUSEKEEPING_HOURS as DEFAULT_HOUSEKEEPING_HOURS
from agent_tools.chair_facts_derive import DEFAULT_STALE_DAYS as DEFAULT_STALE_DAYS
from agent_tools.chair_facts_derive import dispatch_facts as dispatch_facts
from agent_tools.chair_facts_derive import docket_by_land_at as docket_by_land_at
from agent_tools.chair_facts_derive import empty_decompose_facts as empty_decompose_facts
from agent_tools.chair_facts_derive import gather_facts as gather_facts
from agent_tools.chair_facts_derive import land_refusals_from_actions as land_refusals_from_actions
from agent_tools.chair_facts_read import HARNESS_CAUSE as HARNESS_CAUSE
from agent_tools.chair_facts_read import LAUNCH_KINDS as LAUNCH_KINDS
from agent_tools.chair_facts_read import RESCUE_FAILED_CAUSE as RESCUE_FAILED_CAUSE
from agent_tools.chair_facts_read import STRANDED_CAUSE as STRANDED_CAUSE
from agent_tools.chair_facts_read import FactsDeps as FactsDeps
from agent_tools.chair_facts_read import Key as Key
from agent_tools.chair_facts_read import action_doc as action_doc
from agent_tools.chair_facts_read import run_initiative as run_initiative
from agent_tools.chair_facts_read import run_ordinal as run_ordinal
from agent_tools.chair_facts_read import task_key as task_key
from core import module_facade


# H9 (docs/hardening.md): the facts live in the part modules above.
# This module answers for every name they define, and a write (a test's monkeypatch) reaches every part that
# holds the name (core.module_facade). Callers' names are re-exported here, typed; the rest resolve on read.
def __getattr__(name: str) -> object:
    return module_facade.attribute(__name__, name)


module_facade.install(
    __name__,
    (
        "agent_tools.chair_facts_read",
        "agent_tools.chair_facts_derive",
    ),
    eager=True,
)
