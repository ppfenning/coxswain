"""The command table rows for the phase worker that live outside `agent_tools.cli`: `worker forget`.

Data only, kept out of `agent_tools.cli` for H9 (docs/hardening.md). The row names its handler as a
`module:function` string, so importing this module imports no command module; `cli` binds the row into its worker
group.
"""

from __future__ import annotations

from agent_tools import commands

WORKER_FORGET = commands.Command(
    "forget", "worker", "mark a dead worker's row stopping, so the next start of its name takes it over",
    (
        commands.Arg(("name",), {"help": "the worker's name, as `cox worker status` lists it"}),
        commands.Arg(("--force",), {"action": "store_true", "help": (
            "forget it even though it records a machine and was beaten within 900 s, so it may still be running")}),
        commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
    ),
    "agent_tools.worker_cli:cmd_forget", False, (), defaults={"profile": None},
    description=(
        "A worker name is one machine's at a time: a start is refused while another machine's live row holds the "
        "name. A row an older build left `up` (it records no machine) is never taken over on its own, since that "
        "build's beat says nothing about whether it still runs. Once you know that worker is gone, forget it here: "
        "its row is marked `stopping`, and the next start of the name takes it over.\nA row that records a machine "
        "and was beaten within 900 s may be a running worker, so it is refused without --force. A row not `up` is "
        "left as it is."
    ),
)
