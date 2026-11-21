"""The command tables for filing and moving work: `mcp`, `courier`, `plan`, `epic`, `pr` and `route`, and three rows
`cli` binds into other groups (`lander release`, `worker forget`, `worker reopen`).

Data only, kept out of `agent_tools.cli` for H9 (docs/hardening.md). Each row names its handler as an
`agent_tools.cli:...` string, so importing this module imports no command module; `cli` binds the rows
in `COMMAND_TABLE`.
"""

from __future__ import annotations

from agent_tools import commands


def _leader_takeover_hours() -> float:
    """`policy.leader.takeover_hours`, default 3; answers the default for the same reason `_leader_heartbeat_minutes` does."""
    return 3.0


MCP_GROUP = commands.Group(
    name="mcp", help="serve the chair's tools to MCP clients",
    description=(
        "Serve the board, the queue and the cartridge settings to an MCP client. `cox mcp` and `cox mcp serve` are the same.\n"
        "Run it on the machine that runs the chair and holds the cartridge.\n"
        "Reads answer from the board as it is now. Plain writes (file an intake, set a stage or a priority, hold, "
        "release, change a cartridge setting) run the cox command a person would type and return its outcome.\n"
        "A release cut, a scope change and a feature switch are sent to the chair as requests.\n"
        "With no --http it speaks MCP on stdin and stdout, which is what a local client launches. "
        "--http binds only a loopback, private, tailnet or link-local address and has no TLS."
    ),
    epilog="examples:\n  cox mcp\n  cox mcp serve --http 127.0.0.1:8765 --token-file tokens",
    args=(
        commands.Arg(("mode",), {"nargs": "?", "choices": ("serve",), "help": "optional; serving is all this command does"}),
        commands.Arg(("--http",), {"metavar": "HOST:PORT", "help": "serve over HTTP on this literal private address instead of stdin and stdout"}),
        commands.Arg(("--token-file", "--tokens-file"), {"metavar": "PATH", "dest": "tokens_file", "help": "with --http: lines of 'label token'; a token is at least 24 characters"}),
        commands.Arg(("--label",), {"help": "who the stdio client is, on every entry it sends; default $COXSWAIN_MCP_CONVERSATION, else stdio:<user>. Over HTTP the token's label is used"}),
        commands.Arg(("--profile",)),
    ),
    fn="agent_tools.cli:_mcp_serve",
)


COURIER_GROUP = commands.Group(name="courier", help="the courier bus: hand a reference to another label", description="", epilog="")
COURIER_COMMANDS = [
    commands.Command(
        "send", "courier", "append a bus entry naming a courier reference",
        (commands.Arg(("ref",)), commands.Arg(("--to",), {"required": True}), commands.Arg(("--note",), {"required": True}), commands.Arg(("--profile",)),
         commands.Arg(("--event",), {"help": "a typed event name"}), commands.Arg(("--topic",), {"help": "a topic to file the entry under"}),
         commands.Arg(("--field",), {"action": "append", "help": "key=value, repeatable"})),
        "agent_tools.cli:_courier_send", False, (),
    ),
    commands.Command(
        "inbox", "courier", "list this label's unacknowledged bus entries",
        (commands.Arg(("--label",)), commands.Arg(("--profile",)),
         commands.Arg(("--topic",)), commands.Arg(("--event",)), commands.Arg(("--consumer",)),
         commands.Arg(("--wake",), {"action": "store_true", "help": "only entries that need a model"})),
        "agent_tools.cli:_courier_inbox", False, (),
    ),
    commands.Command(
        "ack", "courier", "acknowledge one bus entry by id",
        (commands.Arg(("id",)), commands.Arg(("--profile",)), commands.Arg(("--consumer",))),
        "agent_tools.cli:_courier_ack", False, (),
    ),
]

PLAN_GROUP = commands.Group(
    name="plan", help="visual plans through the local bridge",
    description="Visual plans through the local bridge.",
    epilog="examples:\n  cox plan serve work/<id> --kind plan\n  cox plan serve work/<id> --check",
)
PLAN_COMMANDS = [
    commands.Command(
        "serve", "plan", "serve a visual plan through the local bridge",
        (commands.Arg(("dir",)), commands.Arg(("--kind",), {"default": "plan"}), commands.Arg(("--check",), {"action": "store_true"}), commands.Arg(("--no-open",), {"action": "store_true"})),
        "agent_tools.cli:_plan_serve", False, (),
    ),
]

EPIC_GROUP = commands.Group(
    name="epic", help="watch a detached run",
    description="Watch a detached run.",
    epilog="examples:\n  cox epic watch RUN.pid --log RUN.log\n  cox epic watch RUN.pid --json",
)
EPIC_COMMANDS = [
    commands.Command(
        "watch", "epic", "poll a detached run's pidfile until it exits",
        (
            commands.Arg(("pidfile",)), commands.Arg(("--log",)),
            commands.Arg(("--max-seconds",), {"type": float, "default": 570}), commands.Arg(("--interval",), {"type": float, "default": 20}),
            commands.Arg(("--json",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_epic_watch", False, (),
    ),
]

PR_GROUP = commands.Group(
    name="pr", help="land a pull request once its required checks pass",
    description="Land a pull request once its required checks pass.",
    epilog="examples:\n  cox pr land 123 --repo ~/repos/x",
)
PR_COMMANDS = [
    commands.Command(
        "land", "pr", "wait for the required checks, squash-merge a pull request and mark its tasks done",
        (
            commands.Arg(("number",), {"type": int, "help": "pull request number"}), commands.Arg(("--repo",), {"required": True}),
            commands.Arg(("--timeout",), {"type": float, "default": 600, "help": "seconds to wait for the required checks (default 600)"}),
        ),
        "agent_tools.cli:_pr_land", False, (),
    ),
]

ROUTE_GROUP = commands.Group(
    name="route", help="file work for the harness, and see what is queued or running",
    description="File work for the harness, and see what is queued or running.",
    epilog="examples:\n  cox route launch epic --initiative work/<id> --repo PATH\n  cox route status --profile PATH",
)
_CHAIR_PID_ARG = commands.Arg(("--pid",), {"type": int, "help": "the durable pid that owns the loop (default: the parent process)"})
_LAUNCH_SHARED_ARGS = (
    commands.Arg(("--tier-ceiling",), {"choices": ("cheap", "standard", "deep")}),
    commands.Arg(("--effort-ceiling",), {"choices": ("low", "high")}),
    commands.Arg(("--force",), {"action": "store_true", "help": "launch despite a usage stop, a foreign live leader or a full local host"}),
    commands.Arg(("--no-claim",), {"action": "store_true", "help": "launch without taking an unheld or stale loop"}),
    commands.Arg(("--label",)),
)
ROUTE_COMMANDS = [
    commands.Command(
        "context", "route", "the routing profile's resolved context",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_route_context", False, (),
    ),
    commands.Command(
        "status", "route", "what is queued or running for this profile",
        (
            commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"}),
            commands.Arg(("--all",), {"action": "store_true", "help": "every run, not only live and recent ones"}),
        ),
        "agent_tools.cli:_route_status", False, (),
    ),
    commands.Command(
        "file", "route", "file a new ticket for the harness",
        (
            commands.Arg(("--profile",)), commands.Arg(("--repo",)), commands.Arg(("--title",)), commands.Arg(("--body",)),
            commands.Arg(("--phase",), {"default": "build"}), commands.Arg(("--intake",), {"action": "store_true"}),
            commands.Arg(("--from-intake",), {"help": "link and file an existing intake file's initiative, then retire it"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "file an intake even when its title duplicates an open issue"}),
            commands.Arg(("--link",), {"default": None, "help": "with --intake: the issue url this intake is filed from, so a later pull skips it"}),
            commands.Arg(("--after",), {"action": "append", "default": None, "help": "with --intake: ids this intake waits on; repeat the flag or comma-separate"}),
            commands.Arg(("--draft",), {"action": "store_true", "help": "with --intake: file it as a draft, which the chair skips until it is ready (the default)"}),
            commands.Arg(("--ready",), {"action": "store_true", "help": "with --intake: file it ready for the chair to pick up"}),
            commands.Arg(("--epic",), {"default": None, "help": "with --intake: the epic id this intake belongs to; it must already exist"}),
            commands.Arg(("--priority",), {"default": None, "help": "with --intake: a whole number; higher runs first"}),
            commands.Arg(("--target-release",), {"default": None, "help": "with --intake: the release it ships in, a dotted version such as 2026.11.6"}),
            commands.Arg(("--kind",), {"default": None, "help": "with --intake: what it is, one of idea, feature, bug or hotfix; a hotfix or bug is queued first"}),
        ),
        "agent_tools.cli:_route_file", False, (),
    ),
    commands.Command(
        "priority", "route", "move an initiative or a queued intake up or down the launch order",
        (
            commands.Arg(("initiative",), {"nargs": "?", "help": "an initiative or a queued intake; required with a flag, omit it to list the open queue"}),
            commands.Arg(("--profile",)),
            commands.Arg(("--set",), {"type": int, "default": None, "metavar": "N", "help": "write N on every item"}),
            commands.Arg(("--up",), {"action": "store_true", "help": "pass the nearest higher open initiative"}),
            commands.Arg(("--down",), {"action": "store_true", "help": "pass the nearest lower open initiative"}),
            commands.Arg(("--top",), {"action": "store_true", "help": "one above every other open initiative"}),
            commands.Arg(("--bottom",), {"action": "store_true", "help": "one below every other open initiative"}),
        ),
        "agent_tools.cli:_route_priority", False, (),
        exclusive=("--set", "--up", "--down", "--top", "--bottom"),
    ),
    commands.Command(
        "schedule", "route", "project the whole queue as one schedule",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--explain",), {"metavar": "INITIATIVE", "default": None, "help": "why this initiative sits where it does"}),
            commands.Arg(("--should-jump",), {"metavar": "INITIATIVE", "default": None, "help": "ask whether this initiative should move up the queue"}),
        ),
        "agent_tools.cli:_route_schedule", False, (),
        exclusive=("--explain", "--should-jump"),
    ),
    commands.Command(
        "pull", "route", "file intake tickets from a source, once per link",
        (
            commands.Arg(("--profile",)),
            commands.Arg(("--source",), {"default": None, "help": "one source; default every source in the profile"}),
            commands.Arg(("--dry-run",), {"action": "store_true"}),
        ),
        "agent_tools.cli:_route_pull", False, (),
    ),
    commands.Command(
        "lint", "route", "static ticket lint over a filed initiative, work-shape.md §3",
        (commands.Arg(("initiative_dir",)), commands.Arg(("--repo",), {"default": None})),
        "agent_tools.cli:_route_lint", False, (),
    ),
    commands.Command(
        "groups", "route", "print the newest plans/intake-groups/<date>.md file, work-shape.md §5",
        (commands.Arg(("--profile",)),),
        "agent_tools.cli:_route_groups", False, (),
    ),
    commands.Command(
        "cartridges", "route", "publish this machine's cartridges to the store, or write a published snapshot here",
        (
            commands.Arg(("action",), {"choices": ("publish", "fetch")}),
            commands.Arg(("--sha",), {"help": "the snapshot to fetch"}),
            commands.Arg(("--replace",), {"action": "store_true", "help": "fetch over cartridges that no fetch put there"}),
            commands.Arg(("--into",), {"help": "the directory to publish from or fetch into; default the profile's cartridges_dir"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_cartridges", False, (),
        description=(
            "Cartridges are written and versioned where their owner keeps them. The store only carries a copy of "
            "the exact files a launch used, under the digest of those files, so a lane host can read them.\n"
            "publish prints the sha of the profile's cartridges_dir and stores its files once.\n"
            "fetch makes cartridges_dir hold exactly that snapshot. It refuses to write over files a person put "
            "there or edited, unless --replace is given."
        ),
    ),
    commands.Command(
        "export", "route", "write this machine's work/ and intake/ files from the store; nothing is committed",
        (commands.Arg(("--profile",)),),
        "agent_tools.cli:_route_export", False, (),
        description=(
            "The store holds the board. This writes it out as files under the workspace so they can be read, and "
            "removes files no row names.\nIt first loads any ticket a decompose wrote as a file and the store has "
            "not seen. Under work_state files it does nothing."
        ),
    ),
    commands.Command(
        "drift", "route", "items whose store state and file state differ",
        (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
        "agent_tools.cli:_route_drift", False, (),
    ),
    commands.Command(
        "import", "route", "load the current work item files into the store's work_items table",
        (commands.Arg(("--profile",)), commands.Arg(("--workspace",))),
        "agent_tools.cli:_route_import", False, (),
    ),
    commands.Command(
        "paths", "route", "rewrite recorded repo paths to the tilde form, dry run first",
        (
            commands.Arg(("--profile",)), commands.Arg(("--workspace",)),
            commands.Arg(("--apply",), {"action": "store_true", "help": "write the changes; the default only lists them"}),
            commands.Arg(("--move",), {"nargs": 2, "metavar": ("OLD", "NEW"), "help": "swap repo prefix OLD for NEW in open records only; done and dropped records are left alone"}),
        ),
        "agent_tools.cli:_route_paths", False, (),
    ),
    commands.Command(
        "store-fill", "route", "fill the store's work_items from the workspace files",
        (commands.Arg(("--profile",)), commands.Arg(("--workspace",))),
        "agent_tools.store_fill:main", False, (),
    ),
    commands.Command(
        "approve", "route", "approve a draft initiative's todo tickets: todo becomes ready",
        (
            commands.Arg(("initiative",)), commands.Arg(("--task",), {"help": "approve this one task only"}),
            commands.Arg(("--by",), {"help": "who approves; default the OS user"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_approve", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "decline", "route", "decline a draft initiative: its todo tickets become dropped",
        (
            commands.Arg(("initiative",)), commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who declines; default the OS user"}), commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_decline", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "edit", "route", "change a queued intake, an initiative or a task ticket",
        (
            commands.Arg(("id",), {"help": "a queued intake, an initiative or a task ticket"}), commands.Arg(("--title",)),
            commands.Arg(("--body",)), commands.Arg(("--body-file",)),
            commands.Arg(("--note",), {"default": None, "help": "task ticket only: append this text to the body once, with or without --state; not with --body or --body-file"}),
            commands.Arg(("--repo",)),
            commands.Arg(("--initiative",), {"default": None, "help": "task ticket only: the initiative holding the task, so an id repeated across initiatives picks this one"}),
            commands.Arg(("--surfaces",), {"help": "task ticket only: comma-separated paths that replace the list"}),
            commands.Arg(("--state",), {"help": "task ticket only: ready, approved, done or dropped; never backward"}),
            commands.Arg(("--pr",), {"help": "with --state done: the merged PR url to record on the task, also on a task that is already done; without it the merged PR is looked up with gh"}),
            commands.Arg(("--rebuild",), {"action": "store_true", "help": "with --state ready: send an approved task back to build on current main; needs a recorded land conflict or --force"}),
            commands.Arg(("--force",), {"action": "store_true", "help": "with --rebuild: allow it with no recorded land conflict"}),
            commands.Arg(("--link",), {"default": None, "help": "queued intake only: set the issue url; an empty value clears the link"}),
            commands.Arg(("--after",), {"action": "append", "default": None, "help": "queued intake only: ids it waits on, repeated or comma-separated; an empty value clears the list"}),
            commands.Arg(("--epic",), {"default": None, "help": "initiative only: join the epic with this id, or none to leave it"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the diff and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_edit", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "epic", "route", "create an epic definition file: `route epic new <id>`",
        (
            commands.Arg(("action",), {"help": "only `new`"}), commands.Arg(("id",), {"help": "the epic id, a lowercase slug"}),
            commands.Arg(("--title",)), commands.Arg(("--target-branch",)),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_epic", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "remove", "route", "remove queued intakes or initiatives that have no live run",
        (
            commands.Arg(("id",), {"nargs": "*", "help": "queued intakes or initiatives; repeat for several"}),
            commands.Arg(("--match",), {"metavar": "GLOB", "help": "also remove every queued intake whose file stem or id matches"}),
            commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who removes; default the OS user"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_remove", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "hold", "route", "hold a queued intake or an initiative so the chair skips it until released; for an intake, an alias for draft with a reason",
        (
            commands.Arg(("id",), {"help": "a queued intake or an initiative"}),
            commands.Arg(("--reason",), {"required": True}),
            commands.Arg(("--by",), {"help": "who holds; default the OS user"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_hold", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "release", "route", "release a held intake or initiative back to the queue; an intake moves to ready",
        (
            commands.Arg(("id",), {"help": "a held intake or initiative"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_release", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "ready", "route", "move a queued intake to ready so the chair can pick it up",
        (
            commands.Arg(("id",), {"help": "a queued intake"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_stage_ready", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "draft", "route", "move a queued intake to draft so the chair skips it until it is ready",
        (
            commands.Arg(("id",), {"help": "a queued intake"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_stage_draft", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "requeue", "route", "requeue the intake of an initiative that has no live tasks",
        (
            commands.Arg(("id",), {"help": "an intake or an initiative"}),
            commands.Arg(("--dry-run",), {"action": "store_true", "help": "print the plan and write nothing"}),
            commands.Arg(("--profile",)),
        ),
        "agent_tools.cli:_route_requeue", False, (), defaults={"runs_dir": None},
    ),
    commands.Command(
        "leader", "route", "whether this session is the chair (the SessionStart line)", (), None, False, (),
        subcommands=(
            commands.Command(
                "status", "route", "print one line: held by this session, taken over, or not the chair",
                (commands.Arg(("--profile",)),),
                "agent_tools.cli:_route_leader_status", False, (),
            ),
        ),
        sub_dest="leader_cmd", sub_required=False,
    ),
    commands.Command(
        "chair", "route", "the chair lock for the landing loop (runs/chair.json)", (), None, False, (),
        subcommands=(
            commands.Command(
                "take", "route", "take the chair lock; --steal overrides a live holder and names it",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--steal",), {"action": "store_true", "help": "override a live holder and name it"}),
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "how long the takeover lasts before the loop may steal it (default: policy.leader.takeover_hours)"}),
                    commands.Arg(("--model",), {"choices": ["sonnet", "opus", "haiku"], "default": None, "help": "the model alias this chair runs on, recorded in the chair record (default: not recorded)"}),
                ),
                "agent_tools.cli:_route_chair_take", False, (),
            ),
            commands.Command(
                "extend", "route", "move the end of the chair takeover this session holds",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--hours",), {"type": float, "default": _leader_takeover_hours(), "help": "hours from now (default: policy.leader.takeover_hours)"}),
                ),
                "agent_tools.cli:_route_chair_extend", False, (),
            ),
            commands.Command(
                "beat", "route", "refresh the chair lock's heartbeat",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG,
                    commands.Arg(("--run",)),
                ),
                "agent_tools.cli:_route_chair_beat", False, (),
            ),
            commands.Command(
                "release", "route", "release the chair lock this session holds",
                (commands.Arg(("--profile",)), commands.Arg(("--label",)), _CHAIR_PID_ARG),
                "agent_tools.cli:_route_chair_release", False, (),
            ),
            commands.Command(
                "status", "route", "the chair lock's holder and computed state",
                (commands.Arg(("--profile",)), commands.Arg(("--json",), {"action": "store_true"})),
                "agent_tools.cli:_route_chair_status", False, (),
            ),
            commands.Command(
                "clear", "route", "remove the chair lock file, refusing a live holder unless --force",
                (
                    commands.Arg(("--profile",)),
                    commands.Arg(("--force",), {"action": "store_true", "help": "clear the lock even if its recorded pid is live"}),
                ),
                "agent_tools.cli:_route_chair_clear", False, (),
            ),
            commands.Command(
                "chat", "route", "append to or read the leader chat thread (runs/leader.chat.jsonl)",
                (
                    commands.Arg(("text",), {"nargs": "?"}), commands.Arg(("--profile",)),
                    commands.Arg(("--read",), {"action": "store_true"}),
                    commands.Arg(("--since",)), commands.Arg(("--json",), {"action": "store_true"}),
                    commands.Arg(("--as-leader",), {"action": "store_true", "help": "send as the lock's holder; refuses unless this process is the live holder"}),
                ),
                "agent_tools.cli:_route_chair_chat", False, (),
            ),
            commands.Command(
                "converse", "route", "talk to the running chair through the courier, without the lease (`chat` is the leader thread)", (), None, False, (),
                subcommands=(
                    commands.Command(
                        "send", "route", "send the chair a directive; it applies on the chair's next tick",
                        (
                            commands.Arg(("kind",), {"help": "one of: priority, hold, release, scope, feature, cut_release"}),
                            commands.Arg(("args",), {"nargs": "*", "help": "key=value pairs, e.g. target_kind=task target=t1 rank=1"}),
                            commands.Arg(("--profile",)),
                            commands.Arg(("--sender",), {"default": "session"}),
                        ),
                        "agent_tools.cli:_route_chair_converse_send", False, (),
                    ),
                    commands.Command(
                        "ask", "route", "ask the chair a question and wait a bounded time for its answer",
                        (
                            commands.Arg(("kind",), {"help": "one of: left_on_release, why_not_launched, what_failed"}),
                            commands.Arg(("subject",), {"nargs": "?", "default": ""}),
                            commands.Arg(("--profile",)),
                            commands.Arg(("--sender",), {"default": "session"}),
                            commands.Arg(("--wait",), {"type": float, "default": 30.0, "help": "seconds to wait for the answer (default 30)"}),
                        ),
                        "agent_tools.cli:_route_chair_converse_ask", False, (),
                    ),
                    commands.Command(
                        "state", "route", "print the directives in force, the epics the chair last saw, and recent events",
                        (commands.Arg(("--profile",)),),
                        "agent_tools.cli:_route_chair_converse_state", False, (),
                    ),
                ),
                sub_dest="converse_cmd", sub_required=False,
            ),
        ),
        sub_dest="chair_cmd", sub_required=False,
    ),
    commands.Command(
        "sync", "route", "mirror the work store onto the profile's tracker",
        (
            commands.Arg(("--profile",)), commands.Arg(("--item",)), commands.Arg(("--project",)),
            commands.Arg(("--workspace",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
            commands.Arg(("--prune-out-of-scope",), {"action": "store_true", "help": "close open task issues outside tracker_scope as not planned and remove their cards; with --dry-run, list them only. A coxswain label added by hand counts as made by sync"}),
        ),
        "agent_tools.cli:_route_sync", False, (),
    ),
    commands.Command(
        "launch", "route", "run one of the harness's graphs directly; `epic` is the run of one initiative", (), None, False, (),
        subcommands=(
            commands.Command(
                "epic", "route", "launch the run of one filed initiative (the epic graph; epic is its historical name)",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--fix-attempts",), {"type": int, "default": None}),
                    commands.Arg(("--base",), {"help": "branch phase branches are cut from, as origin/<branch>; default: the repo's base: setting, then origin/HEAD, then main"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    commands.Arg(("--on",), {"help": "start the lane on this host from the hosts table; `local` is this machine. Without it the table places the lane"}),
                    commands.Arg(("--local",), {"action": "store_true", "help": "start the lane on this machine, not where the hosts table places it"}),
                    commands.Arg(("--include-blocked",), {"action": "store_true", "help": "launch despite a ready task behind a blocked item (--force does not)"}),
                    commands.Arg(("--phase",), {"action": "append", "default": None, "help": "run only the named phases of the initiative; repeat to start one run per phase, each on its own lane"}),
                    commands.Arg(("--lane-worker",), {"help": "run the phase as the phase lane of this worker's phase_queue claim (with --claim-epoch; `cox worker` passes both)"}),
                    commands.Arg(("--claim-epoch",), {"type": int, "default": None, "help": "the claim's epoch, which every write of the phase lane is fenced on"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "epic"},
            ),
            commands.Command(
                "decompose", "route", "launch the decompose graph against an idea",
                (
                    commands.Arg(("--profile",)),
                    commands.Arg(("--idea",), {"required": True, "metavar": "PATH", "help": "the intake/idea markdown file"}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--task-ids",), {"choices": ["ordinal"], "help": "name the graph's own tasks <initiative-id>-t1, -t2, ..."}),
                    commands.Arg(("--local",), {"action": "store_true", "help": "start the lane on this machine, not where the hosts table places it"}),
                    commands.Arg(("--detach",), {"action": "store_true", "help": "start the decompose in the background and return once it has started"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    # A queue worker's launch carries its kind lease on these (`chair_kinds.proof_argv`).
                    commands.Arg(("--queue-lease",), {"help": "the kind lease a queue worker launches under (chair:decompose); with --queue-holder and --queue-epoch, the launch goes on beside the loop's live lock while the store says that lease is held"}),
                    commands.Arg(("--queue-holder",), {"help": "the holder of --queue-lease"}),
                    commands.Arg(("--queue-epoch",), {"type": int, "default": None, "help": "the epoch of --queue-lease"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "decompose"},
            ),
            commands.Command(
                "rescue", "route", "launch the rescue graph against a task",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--initiative",), {"required": True}),
                    commands.Arg(("--task",), {"required": True}),
                    commands.Arg(("--repo",)),
                    commands.Arg(("--base",), {"help": "branch the rescue checks against, as origin/<branch>; default: the repo's base: setting, then origin/HEAD, then main"}),
                    commands.Arg(("--dry-run",), {"action": "store_true"}),
                    commands.Arg(("--run-id",), {"help": "use this run id instead of the next free one; refused when taken"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "rescue"},
            ),
            commands.Command(
                "cos", "route", "launch the cos graph",
                (
                    commands.Arg(("--profile",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                    *_LAUNCH_SHARED_ARGS,
                ),
                "agent_tools.cli:_route_launch", False, (), defaults={"graph": "cos"},
            ),
            commands.Command(
                "sweep", "route", "launch the sweep graph against an idea",
                (
                    commands.Arg(("--idea",), {"required": True}),
                    commands.Arg(("--initiative-id",), {"required": True}),
                    commands.Arg(("--label",)), commands.Arg(("--dry-run",), {"action": "store_true"}),
                ),
                "agent_tools.cli:_route_launch_sweep", False, (),
            ),
        ),
        sub_dest="graph", sub_required=True,
    ),
]


# `cox lander release`: the one lander row kept out of `agent_tools.cli` (H9). `cli` appends it to LANDER_COMMANDS.
LANDER_RELEASE = commands.Command(
    "release", "lander", "lift a new-model merge hold so the next pass tries the merge again",
    (
        commands.Arg(("initiative",), {"help": "the held initiative's id"}),
        commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
    ),
    "agent_tools.cli:_lander_release", False, (), defaults={"profile": None},
    description=(
        "A new-model phase merge that could not be made holds its initiative. The hold lifts on its own when a new "
        "tip is pushed to the phase branch, or when its automatic retry is due (a resolver that raised, a harness "
        "fault, no merge agent yet).\nA clean refusal and a failed level check are never retried on their own: "
        "release one here, and the next lander pass merges the phase again, the merge agent asked again. Only a "
        "merge hold is released; a record that disagrees with its branch is reconciliation's."
    ),
)

WORKER_FORGET = commands.Command(
    "forget", "worker", "mark a dead worker's row stopping, so the next start of its name takes it over",
    (
        commands.Arg(("name",), {"help": "the worker's name, as `cox worker status` lists it"}),
        commands.Arg(("--force",), {"action": "store_true", "help": (
            "forget it even though it records a machine and was beaten within 900 s, so it may still be running")}),
        commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
    ),
    "agent_tools.cli:_worker_forget", False, (), defaults={"profile": None},
    description=(
        "A worker name is one machine's at a time: a start is refused while another machine's live row holds the "
        "name. A row an older build left `up` (it records no machine) is never taken over on its own, since that "
        "build's beat says nothing about whether it still runs. Once you know that worker is gone, forget it here: "
        "its row is marked `stopping`, and the next start of the name takes it over.\nA row that records a machine "
        "and was beaten within 900 s may be a running worker, so it is refused without --force. A row not `up` is "
        "left as it is."
    ),
)

WORKER_REOPEN = commands.Command(
    "reopen", "worker", "put a new-model phase that finished failed or quarantined back to ready, for another try",
    (
        commands.Arg(("target",), {"metavar": "INITIATIVE/PHASE", "help": "the phase, as `cox worker status` lists it"}),
        commands.Arg(("--reason",), {"required": True, "help": "why it is tried again; recorded on the phase's row"}),
        commands.Arg(("--by",), {"help": "who reopens it; default the OS user"}),
        commands.Arg(("--dry-run",), {"action": "store_true", "help": "say what would change and write nothing"}),
        commands.Arg(("--profile",), {"help": "the routing profile (default: ~/.config/agent-tools/profile.yaml or $AGENT_TOOLS_PROFILE)"}),
    ),
    "agent_tools.cli:_worker_reopen", False, (), defaults={"profile": None},
    description=(
        "A phase that finished failed (its claims ran out, or its smoke failed) stays finished: the chair never "
        "retries it on its own, and releasing a hold does not either. Reopen it here: its row goes back to ready with "
        "0 attempts, the reason and who reopened it are recorded on the row, and its review starts a new episode. "
        "Nothing on the phase branch is touched, so the next claim builds only the tasks still missing.\n"
        "A quarantined phase may be reopened the same way. A claimed phase, an approved one and a phase that is not "
        "on the new model are refused. A phase already ready is left as it is."
    ),
)
