"""Pure core for `agent-tools setup install`: the ordered plan, the
profile text it writes, and the hook entry it merges. No file reads, no
env access, no subprocess calls — every function here takes plain
arguments and returns plain values; the edge (built in a later phase)
executes the steps this module returns.

`route.parse_profile` never unquotes a scalar it reads. Whatever
`profile_text` writes must be readable back bare, verbatim. A value that
itself looks like a flow list/map (`[a]`, `{a: b}`) or that carries an
inline space-then-`#` would round-trip to something other than what was
written, so `profile_text` refuses those with `ValueError`.
"""

from __future__ import annotations

import posixpath
import re
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from agent_tools.home import Locations

# The one checkout a host runs: `<installs>/coxswain`, a clone of the coxswain repository.
CHECKOUT = "coxswain"
# The uv tool and the distribution share the name; it puts `cox` and `cartridge` on PATH.
TOOL = "coxswain"
# The marketplace NAME is declared in src/cartridges/.claude-plugin/marketplace.json; only its location moved.
MARKETPLACE = "coxswain-cartridges"
DEFAULT_PLUGIN = f"local-skills@{MARKETPLACE}"
# The rule the beat checks (`checkout_env.SYNC_COMMAND`): exactly the lock, every extra.
SYNC_ARGV = ("uv", "sync", "--frozen", "--all-extras", "-q")
_HOOK_COMMAND = "agent-tools route context 2>/dev/null || true"


class Layout(NamedTuple):
    """Every path a host derives from its one checkout."""

    checkout: str
    harness_dir: str
    cartridges: str
    skills_root: str
    provider_profile: str
    crew_install: str


def checkout_layout(checkout: str) -> Layout:
    """The paths inside one checkout; `cartridges` is the folder holding `.claude-plugin/`."""
    cartridges = f"{checkout}/src/cartridges"
    return Layout(
        checkout,
        f"{checkout}/src/graphs",
        cartridges,
        f"{cartridges}/skills-plugins",
        f"{cartridges}/providers/claude-code.yaml",
        f"{checkout}/crew/install.sh",
    )


def layout(installs: str) -> Layout:
    """The layout of `<installs>/coxswain`."""
    return checkout_layout(posixpath.join(installs, CHECKOUT))


def sync_step(checkout: str) -> dict:
    """`uv sync` in the checkout: the one venv, `<checkout>/.venv`."""
    return {"op": "run", "argv": list(SYNC_ARGV), "cwd": checkout, "why": "sync the checkout's one venv"}


def tool_step(checkout: str, extras: tuple[str, ...] = ()) -> dict:
    """The editable `cox` tool from the checkout; warn-only, so a failure leaves the tool already on PATH working."""
    return {"op": "run", "argv": ["uv", "tool", "install", "--force", "-q", "--editable", _tool_target(checkout, extras)],
            "why": "put cox on PATH", "warn_only": True}


def crew_step(checkout: str) -> dict:
    """crew/install.sh links each seat into ~/.claude/agents and never replaces a file that is not a link."""
    script = checkout_layout(checkout).crew_install
    return {"op": "run", "argv": [script], "cwd": posixpath.dirname(script), "why": "link the crew seats",
            "warn_only": True}


def profile_path(config_dir: str) -> str:
    """Where the profile lives under `config_dir`. The one place this path
    is formatted; the edge asks here rather than restating it."""
    return f"{config_dir}/agent-tools/profile.yaml"


def _unsafe_scalar(value: str) -> bool:
    stripped = value.strip()
    bracketed = (stripped.startswith("[") and stripped.endswith("]")) or (
        stripped.startswith("{") and stripped.endswith("}")
    )
    return bracketed or bool(re.search(r"\s#", value))


def profile_text(
    team: str,
    cartridges_dir: str,
    skills_root: str,
    provider_profile: str,
    harness_dir: str,
    workspace: str,
    assume: str = "a",
    window_ceiling_usd: float | None = None,
    weekly_ceiling_usd: float | None = None,
    umbrella_dir: str = "",
) -> str:
    """profile.yaml text that round-trips through `route.parse_profile`.
    `umbrella_dir`, when given, names the checkout itself.
    Every value is emitted bare; one that parse_profile would read back
    as something else raises ValueError instead of being written.
    `window_ceiling_usd` and `weekly_ceiling_usd`, when given, are written
    into the `spend:` block `route.parse_profile` reads back as floats."""
    fields = {
        "team": team,
        "cartridges_dir": cartridges_dir,
        "skills_root": skills_root,
        "provider_profile": provider_profile,
        "harness_dir": harness_dir,
        "workspace_dir": workspace,
        "assume": assume,
        "umbrella_dir": umbrella_dir,
    }
    unsafe = [key for key, value in fields.items() if _unsafe_scalar(value)]
    if unsafe:
        raise ValueError(f"cannot round-trip through parse_profile: {unsafe}")
    lines = [
        "# Written by agent-tools setup install — rerun with --force-profile to rewrite",
        f"team: {team}",
        f"cartridges_dir: {cartridges_dir}",
        f"skills_roots: [{skills_root}]",
        f"provider_profile: {provider_profile}",
        f"harness_dir: {harness_dir}",
        f"workspace_dir: {workspace}",
        f"assume: {assume}",
        *([f"umbrella_dir: {umbrella_dir}"] if umbrella_dir else []),
    ]
    if window_ceiling_usd is not None or weekly_ceiling_usd is not None:
        lines.append("spend:")
        if window_ceiling_usd is not None:
            lines.append(f"  window_ceiling_usd: {window_ceiling_usd}")
        if weekly_ceiling_usd is not None:
            lines.append(f"  weekly_ceiling_usd: {weekly_ceiling_usd}")
    return "\n".join(lines) + "\n"


def _agent_steps(cast: dict) -> list:
    """A `write` step for each seat whose `enabled` is true or absent, a
    `remove` step for each seat whose `enabled` is false."""
    steps = []
    for seat, cfg in cast.items():
        if cfg.get("enabled", True):
            steps.append({"op": "write", "path": cfg["path"], "text": cfg["text"],
                          "why": f"install the {seat} seat"})
        else:
            steps.append({"op": "remove", "path": cfg["path"],
                          "why": f"{seat} seat is disabled"})
    return steps


def marketplace_steps(cartridges_dir: str, registered: str | None) -> list:
    """Registry-only, per docs/design/coxswain-home.md section 3. `cartridges_dir` is the
    folder holding `.claude-plugin/`, `<checkout>/src/cartridges`. `registered`
    is the source text `claude plugin marketplace list` shows for the
    cartridges marketplace, or None when none is listed."""
    add = {"op": "run", "argv": ["claude", "plugin", "marketplace", "add", cartridges_dir],
           "why": f"register {MARKETPLACE} as a plugin marketplace"}
    if registered is None:
        return [add]
    if registered == cartridges_dir:
        return [{"op": "skip", "what": "marketplace",
                 "why": "already registered at the resolved location"}]
    remove = {"op": "run", "argv": ["claude", "plugin", "marketplace", "remove", MARKETPLACE],
              "why": f"marketplace points at {registered}, not {cartridges_dir}"}
    return [remove, add]


def _tool_target(checkout: str, extras: tuple[str, ...]) -> str:
    """The `-e` install target: the checkout, with `[a,b]` appended when extras are given."""
    return f"{checkout}[{','.join(extras)}]" if extras else checkout


def _workspace_steps(workspace: str) -> list:
    folders = (workspace, *(f"{workspace}/{name}" for name in ("work", "runs", "intake", "cartridges")))
    return [{"op": "mkdir", "path": path, "why": "create the workspace folder"} for path in folders]


def install_plan(
    *,
    root: str,
    team: str,
    workspace: str,
    provider_profile: str,
    skills_root: str,
    uv_on_path: bool,
    claude_on_path: bool,
    profile_exists: bool,
    force_profile: bool,
    plugins: bool,
    hook: bool,
    config_dir: str,
    claude_settings_path: str,
    assume: str = "a",
    cast: dict | None = None,
    window_ceiling_usd: float | None = None,
    weekly_ceiling_usd: float | None = None,
    locations: Locations | None = None,
    registered_marketplace: str | None = None,
    workspace_exists: bool = False,
    tool_extras: tuple[str, ...] = (),
) -> list:
    """Ordered setup steps for the one checkout `<installs>/coxswain`. With
    `locations`, the installs folder and the profile path come from the
    resolved record (`installs`, `profile`) and `root` and `config_dir` are
    ignored; without it `root` is the installs folder.
    `claude_settings_path` is pre-resolved by the caller; this core never
    expands `~` or touches the filesystem.
    The checkout's `src/cartridges` is what the plugin marketplace registers;
    the profile's own `cartridges_dir` field is a workspace path, a different
    thing that happens to share a name in the field the routing layer reads.
    With `locations` and an empty `workspace`, the workspace is
    `<workspaces>/main` and its folders are made unless `workspace_exists`.
    With `locations`, the last write records `<home>` at `locations.record`.
    `tool_extras` are the extras the `coxswain` uv tool already carries, read
    from its receipt by the caller; they ride on its `uv tool install` target."""
    default_workspace = locations is not None and not workspace
    if locations is not None and default_workspace:
        workspace = posixpath.join(locations.workspaces, "main")
    installs = locations.installs if locations else root
    profile_file = locations.profile if locations else profile_path(config_dir)
    paths = layout(installs)
    steps: list = []

    if default_workspace and not workspace_exists:
        steps.extend(_workspace_steps(workspace))

    if not uv_on_path:
        steps.append({"op": "skip", "what": "venv",
                      "why": "uv is not on PATH; install it from https://astral.sh/uv"})
    else:
        steps.extend([sync_step(paths.checkout), tool_step(paths.checkout, tool_extras)])

    if not profile_exists or force_profile:
        text = profile_text(team=team, cartridges_dir=f"{workspace}/cartridges",
                             skills_root=skills_root, provider_profile=provider_profile,
                             harness_dir=paths.harness_dir, workspace=workspace, assume=assume,
                             window_ceiling_usd=window_ceiling_usd, weekly_ceiling_usd=weekly_ceiling_usd,
                             umbrella_dir=paths.checkout)
        steps.append({"op": "write", "path": profile_file, "text": text,
                      "why": "write the routing layer's profile"})
    else:
        steps.append({"op": "skip", "what": "profile",
                      "why": "exists; pass --force-profile to rewrite"})

    steps.extend(_agent_steps(cast or {}))
    steps.append(crew_step(paths.checkout))

    if plugins:
        if claude_on_path:
            steps.extend(marketplace_steps(paths.cartridges, registered_marketplace))
            steps.append({"op": "run",
                          "argv": ["claude", "plugin", "install", DEFAULT_PLUGIN],
                          "why": "install the local-skills plugin"})
        else:
            steps.append({"op": "skip", "what": "plugins",
                          "why": "claude is not on PATH; this step is provider-specific"})

    if locations is not None:
        steps.append({"op": "write", "path": locations.record, "text": f"{locations.home}\n",
                      "why": "record the coxswain home"})

    if hook:
        steps.append({"op": "hook", "path": claude_settings_path,
                      "why": "add a SessionStart hook that prints route context"})

    steps.append({"op": "print", "text": "verify with: agent-tools setup doctor"})
    return steps


def hook_settings(existing: dict) -> tuple:
    """(new_settings, changed): `existing` with a SessionStart hook entry
    running the route-context command appended under hooks.SessionStart,
    unless an entry with that exact command is already present. Never
    mutates `existing`."""
    entry = {"matcher": "", "hooks": [{"type": "command", "command": _HOOK_COMMAND}]}
    hooks = dict(existing.get("hooks", {}))
    session_start = list(hooks.get("SessionStart", []))
    for item in session_start:
        for sub in item.get("hooks", []):
            if sub.get("command") == _HOOK_COMMAND:
                return existing, False
    new_settings = dict(existing)
    hooks["SessionStart"] = session_start + [entry]
    new_settings["hooks"] = hooks
    return new_settings, True


def render_plan(steps: list) -> str:
    """One line per step, for `--dry-run`."""
    lines = []
    for step in steps:
        op = step["op"]
        if op == "run":
            cwd = f" ({step['cwd']})" if "cwd" in step else ""
            lines.append(f"run{cwd} {' '.join(step['argv'])}")
        elif op == "write":
            lines.append(f"write {step['path']}")
        elif op == "mkdir":
            lines.append(f"mkdir {step['path']}")
        elif op == "remove":
            lines.append(f"remove {step['path']}")
        elif op == "skip":
            lines.append(f"skip {step['what']} — {step['why']}")
        elif op == "hook":
            lines.append(f"hook {step['path']}")
        elif op == "print":
            lines.append(f"print {step['text']}")
        else:
            lines.append(f"{op} {step}")
    return "\n".join(lines)
