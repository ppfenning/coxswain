"""Pure plan for moving a host to the monorepo layout: one checkout, one venv.

The source is the 0.47 layout: the component checkouts `<installs>/coxswain-{tools,graphs,cartridges}` (and
`coxswain-crew` where crew was installed) beside the umbrella checkout `<installs>/coxswain`, with the uv tools
`coxswain-tools` and `agent-cartridges`. The target is the layout `setup_install.layout` names: `<installs>/coxswain`
is the monorepo at a release tag, `<checkout>/.venv` the one venv, `cox` the editable `coxswain` uv tool. The
umbrella checkout already sits at the target path, so it becomes the monorepo by checking out the release tag.

Nothing here executes a step: `migrate_plan` takes the observed host as plain data and returns step dicts that the
cli edge applies. Only the `read_*` and `observe_*` functions read files, and they are edges.

Observed host, a dict with these keys:

- `checkout`: what sits at `<installs>/coxswain`: `kind` (absent, dir, symlink, file), `monorepo` (its pyproject
  names the `coxswain` distribution), `dirty`, and `url` (its origin, or the repository to clone when absent).
- `target_tag`: the release tag a checkout that is not yet the monorepo moves to; None when none is offered.
- `components`: for each name in RETIRED, the kind at `<installs>/<name>`.
- `worktrees`: for each component checkout present as a directory, its worktree paths other than the main one.
  Required for every such checkout; a missing one yields a refuse step.
- `venv_source`: the editable source of `coxswain` in `<checkout>/.venv`, None when there is none.
- `install_bins`: optional. Each uv tool receipt in TOOL_RECEIPTS plus `coxswain`, as `observe_install_bins`
  returns it. When the key is absent the uv tool dir was unknown and no tool step is planned.
- `settings`: profile field to its current value, for SETTING_KEYS and `repo_map`.
- `home_record`: the current text of the home record, or None when it is absent.
- `cartridge_links`: each workspace base and local link to its current lexical target.
- `marketplace`, `marketplace_plugins`, `claude_on_path`: the path registered for the marketplace (None when
  absent), the plugin ids installed from it, and whether `claude` is on PATH.
- `chair_unit`, `chair_unit_drop_ins`, `chair_active`: the chair unit file text, its `*.conf` drop-ins as
  (file name, text), and whether the chair service was active.
- `workspace`: a dict with `path`. The workspace is registered by location and is never moved.
- `today`: the date, `YYYY-MM-DD`, that names the folder the component checkouts retire into.

Rules, in plan order:

1. Refuse, and plan nothing else, when the checkout path holds something other than a directory, when a
   checkout that is not yet the monorepo is dirty or has no release tag to move to, when an absent checkout has
   no repository to clone, when the cartridge links were not observed, or when a component checkout to retire
   has no observed worktrees.
2. A checkout that is not yet the monorepo is cloned (absent) or fetched and checked out (present) at the
   target tag. A monorepo checkout is never moved here: that is `cox upgrade`'s job.
3. The venv is synced when `coxswain` in `<checkout>/.venv` is not editable from the checkout.
4. Profile keys naming a component checkout are rewritten into the checkout (`harness_dir` to `src/graphs`,
   `skills_roots` and `provider_profile` under `src/cartridges`); a provider profile outside the components
   is the team's own and is kept. `umbrella_dir` is set to the checkout. A `repo_map` value naming a component
   checkout follows it to its retired folder. The home record names the resolved home.
5. Each workspace base and local link is re-pointed at `<checkout>/src/cartridges/cartridges/<name>`.
6. The plugin marketplace is re-registered at `<checkout>/src/cartridges` and its plugins reinstalled.
7. The `coxswain-tools` and `agent-cartridges` uv tools are uninstalled, then `coxswain` is installed editable
   from the checkout with the union of their extras.
8. A chair unit whose effective PATH names a component checkout is regenerated, or its drop-in rewritten.
9. Each component checkout present at `<installs>/<name>` moves to `<installs>/.retired-<today>/<name>`, and its
   worktrees are repaired there. Nothing is deleted.

A 0.47 install made with `cox install --root <X>` is migrated where it sits: `install_locations` makes `<X>` the
installs folder, and no home record is written, since that install is not under the home.

A host already in the monorepo layout plans one skip step, so a second run plans nothing. Every step is a dict
with `op`, a one-sentence `why`, and the fields of its op.
"""

from __future__ import annotations

import json
import posixpath
import re
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

from agent_tools import chair_unit, repo_path, repo_path_migrate, setup_install
from agent_tools.home import Locations
from agent_tools.repo_path_migrate import Change
from agent_tools.setup_migrate_hosts import ObservedHost, is_migrated

Step = dict[str, Any]

CARTRIDGES = "coxswain-cartridges"
GRAPHS = "coxswain-graphs"
TOOLS = "coxswain-tools"
CREW = "coxswain-crew"
# The component checkouts of the pre-home layout, whose compatibility links `--finish` removes.
CHECKOUTS = (CARTRIDGES, GRAPHS, TOOLS)
# The component checkouts the monorepo retires from the installs folder, and where each one's content now lives.
RETIRED = (TOOLS, GRAPHS, CARTRIDGES, CREW)
SUBDIRS = {TOOLS: "src/tools", GRAPHS: "src/graphs", CARTRIDGES: "src/cartridges", CREW: "crew"}
RETIRED_PREFIX = ".retired-"
# The umbrella checkout, which becomes the monorepo checkout in place.
UMBRELLA = setup_install.CHECKOUT
# The uv tools of the component layout, by the checkout each one installed; the monorepo replaces both.
TOOL_RECEIPTS = {TOOLS: "coxswain-tools", CARTRIDGES: "agent-cartridges"}
DEFAULT_PLUGIN = setup_install.DEFAULT_PLUGIN
OPS = frozenset(
    {
        "checkout",
        "move",
        "symlink",
        "rewrite_setting",
        "rewrite_profile_key",
        "skip",
        "print",
        "refuse",
        "worktree_repair",
        "verify",
        "chair_unit",
        "chair_unit_dropin",
        "run",
    }
)
SETTING_KEYS = ("skills_roots", "provider_profile", "harness_dir", "umbrella_dir")
# Not in SETTING_KEYS: its value is a mapping, which the string and list helpers below do not handle.
REPO_MAP_KEY = "repo_map"
WORKTREES_CLEAN = "no worktree names a missing path"
FINISH_HINT = "run cox setup migrate --finish once every host has migrated."
FINISH_NO_ROOT = "finish: no old root known; pass --old-root"
MARKER_NAME = "migrate.json"


def _step(op: str, why: str, **fields: Any) -> Step:
    return {"op": op, **fields, "why": why}


def _refuse(name: str, reason: str) -> Step:
    return _step("refuse", f"{name} {reason}, so nothing is moved.", checkout=name)


def retired_dir(installs: str, today: str) -> str:
    """The folder the component checkouts retire into: `<installs>/.retired-<today>`."""
    return posixpath.join(installs, f"{RETIRED_PREFIX}{today}")


def _refusals(observed: Mapping[str, Any], checkout_path: str) -> list[Step]:
    checkout = observed.get("checkout") or {}
    kind, monorepo = checkout.get("kind", "absent"), bool(checkout.get("monorepo"))
    if kind not in ("dir", "absent"):
        return [_refuse(UMBRELLA, f"has a {kind} at {checkout_path}, not a checkout")]
    if kind == "dir" and not monorepo and checkout.get("dirty"):
        return [_refuse(UMBRELLA, f"is dirty at {checkout_path}; commit or stash before it becomes the monorepo")]
    if not monorepo and not observed.get("target_tag"):
        return [_refuse(UMBRELLA, "has no release tag to move to; pass --to")]
    if kind == "absent" and not checkout.get("url"):
        return [_refuse(UMBRELLA, f"is absent at {checkout_path} and no repository was named to clone")]
    if "cartridge_links" not in observed:
        why = "The workspace cartridge links were not observed, so their state is unknown and nothing is moved."
        return [_step("refuse", why, checkout="cartridge_links")]
    worktrees = observed.get("worktrees", {})
    return [
        _refuse(name, "needs its worktrees repaired but their paths were not observed")
        for name in _present(observed)
        if name not in worktrees
    ]


def _present(observed: Mapping[str, Any]) -> list[str]:
    """The component checkouts still sitting in the installs folder as directories."""
    components = observed.get("components", {})
    return [name for name in RETIRED if components.get(name) == "dir"]


def _checkout_steps(checkout: Mapping[str, Any], tag: str | None, path: str) -> list[Step]:
    if checkout.get("monorepo"):
        return []
    if checkout.get("kind") == "absent":
        argvs = [["git", "clone", "--branch", tag, checkout["url"], path]]
        why = f"The monorepo checkout is cloned at {tag}."
    else:
        argvs = [["git", "-C", path, "fetch", "--tags"], ["git", "-C", path, "checkout", tag]]
        why = f"The umbrella checkout becomes the monorepo by checking out {tag}."
    return [_step("checkout", why, path=path, tag=tag, argvs=argvs)]


def _repair_steps(name: str, new: str, worktrees: list[str]) -> tuple[Step, Step]:
    return (
        _step(
            "worktree_repair",
            f"The worktree records of {name} still name its old path.",
            checkout=name,
            argv=["git", "-C", new, "worktree", "repair", *worktrees],
        ),
        _step(
            "verify",
            f"{name} must show no worktree at a missing path.",
            checkout=name,
            argv=["git", "-C", new, "worktree", "list", "--porcelain"],
            expect=WORKTREES_CLEAN,
        ),
    )


def _remap(value: Any, pairs: tuple[tuple[str, str], ...]) -> Any:
    """Swap an old checkout prefix for its new one in a string or a list of strings."""
    if isinstance(value, list):
        return [_remap(item, pairs) for item in value]
    if not isinstance(value, str):
        return value
    return next((new + value[len(old) :] for old, new in pairs if value == old or value.startswith(old + "/")), value)


def _expand_all(value: Any, user_home: str) -> Any:
    """Expand a leading tilde in a string or a list of strings."""
    if isinstance(value, list):
        return [_expand_all(item, user_home) for item in value]
    if isinstance(value, str) and (value == "~" or value.startswith("~/")):
        return user_home + value[1:]
    return value


def _old_spellings(old: str, user_home: str, home_spellings: Sequence[str]) -> frozenset[str]:
    """Every spelling of an old absolute checkout path: itself, its tilde form and each other home's form."""
    home = user_home.rstrip("/")
    if not old.startswith(home + "/"):
        return frozenset({old})
    rest = old[len(home) :]
    return frozenset({old, "~" + rest, *(spelling.rstrip("/") + rest for spelling in home_spellings)})


def _script_spellings(old: str, user_home: str) -> tuple[str, ...]:
    """The absolute path, then its tilde and $HOME forms when it lies under the home folder."""
    home = user_home.rstrip("/")
    if not old.startswith(home + "/"):
        return (old,)
    rest = old[len(home) :]
    return (old, "~" + rest, "$HOME" + rest)


def _names(text: str, spelling: str) -> bool:
    """True when the spelling ends at end of text, a slash, whitespace, a quote or a colon."""
    return re.search(re.escape(spelling) + r"""(?=\Z|[/\s'":])""", text) is not None


def stale_script_lines(files: Mapping[str, str], old_checkouts: Sequence[str], user_home: str) -> list[str]:
    """One line per file and old checkout the file still names, files in name order, checkouts as given."""
    return [
        f"migrate: {name} still names {old}; edit it by hand"
        for name in sorted(files)
        for old in old_checkouts
        if any(_names(files[name], spelling) for spelling in _script_spellings(old, user_home))
    ]


def _remap_repo_map(
    repo_map: Mapping[str, Any], pairs: tuple[tuple[str, str], ...], user_home: str, home_spellings: Sequence[str]
) -> dict[str, Any]:
    """A new map in the same key order; a value equal to an old checkout path becomes its new path."""
    new_for = {spelling: new for old, new in pairs for spelling in _old_spellings(old, user_home, home_spellings)}
    return {key: new_for.get(value, value) if isinstance(value, str) else value for key, value in repo_map.items()}


def _setting_steps(
    settings: Mapping[str, Any],
    home_record: str | None,
    locs: Locations,
    checkout: str,
    retired: tuple[tuple[str, str], ...],
    home_spellings: Sequence[str] = (),
) -> list[Step]:
    """Profile keys under a component checkout move into the checkout; `umbrella_dir` names the checkout; a
    `repo_map` value naming a retired checkout follows it; the home record names the home."""
    user_home = posixpath.dirname(locs.config)
    pairs = tuple((posixpath.join(locs.installs, name), posixpath.join(checkout, SUBDIRS[name])) for name in RETIRED)
    keys = [
        _step(
            "rewrite_profile_key",
            f"{key} still points into a component checkout.",
            key=key,
            old=settings[key],
            new=new,
        )
        for key in SETTING_KEYS
        if key != "umbrella_dir"
        and key in settings
        and (new := _remap(expanded := _expand_all(settings[key], user_home), pairs)) != expanded
    ]
    umbrella = settings.get("umbrella_dir")
    if _expand_all(umbrella, user_home) != checkout:
        keys.append(
            _step("rewrite_profile_key", "umbrella_dir must name the checkout.", key="umbrella_dir", old=umbrella, new=checkout)
        )
    repo_map = settings.get(REPO_MAP_KEY)
    mapped = _remap_repo_map(repo_map, retired, user_home, home_spellings) if isinstance(repo_map, Mapping) else None
    if mapped is not None and mapped != dict(repo_map):
        keys.append(
            _step(
                "rewrite_profile_key",
                f"{REPO_MAP_KEY} names component checkouts that retire.",
                key=REPO_MAP_KEY,
                old=dict(repo_map),
                new=mapped,
            )
        )
    # An install migrated where it sits outside the home (`cox install --root`) is not the home's, so no record.
    record = (
        []
        if home_record == locs.home or not _under(locs.installs, locs.home)
        else [
            _step(
                "rewrite_setting",
                "The home record must name the resolved home.",
                key="home_record",
                path=locs.record,
                old=home_record,
                new=locs.home,
            )
        ]
    )
    return [*keys, *record]


def cartridge_link_target(path: str, locs: Locations) -> str:
    """Expected lexical target of a base or local link: <checkout>/src/cartridges/cartridges/<entry name>,
    relative to the folder holding the link."""
    entry = posixpath.join(setup_install.layout(locs.installs).cartridges, "cartridges", posixpath.basename(path))
    return posixpath.relpath(entry, posixpath.dirname(path))


def _link_steps(links: Mapping[str, str], locs: Locations) -> list[Step]:
    wanted = {path: cartridge_link_target(path, locs) for path in links}
    return [
        _step(
            "symlink",
            "The relative link must reach its cartridges subfolder in the checkout.",
            path=path,
            target=wanted[path],
            old_target=links[path],
        )
        for path in sorted(links)
        if wanted[path] != links[path]
    ]


def _marketplace_steps(observed: Mapping[str, Any], target: str) -> tuple[list[Step], list[Step]]:
    """Run steps that re-register the marketplace and its plugins, and the one print when claude was not checked."""
    if not observed.get("claude_on_path", False):
        why = "The Claude marketplace was not checked because claude is not on PATH."
        return [], [_step("print", why, item="marketplace not checked")]
    registered = observed.get("marketplace")
    if registered == target:
        return [], []
    plugins = observed.get("marketplace_plugins") or [DEFAULT_PLUGIN]
    installs = [
        _step(
            "run",
            f"reinstall {plugin} from the re-registered marketplace",
            argv=["claude", "plugin", "install", plugin],
        )
        for plugin in plugins
    ]
    return [*setup_install.marketplace_steps(target, registered), *installs], []


def _tool_steps(bins: Mapping[str, Any] | None, checkout: str) -> list[Step]:
    """Uninstall each component uv tool still installed, then install `coxswain` editable from the checkout with
    the union of every receipt's extras; none when the coxswain tool already is that and no old tool is left."""
    if bins is None:
        return []
    olds = [tool for tool in TOOL_RECEIPTS.values() if (bins.get(tool) or {}).get("installed")]
    current = bins.get(setup_install.TOOL) or {}
    if current.get("editable") == checkout and not olds:
        return []
    extras = tuple(
        dict.fromkeys(x for tool in (setup_install.TOOL, *TOOL_RECEIPTS.values()) for x in (bins.get(tool) or {}).get("extras") or ())
    )
    uninstall = [
        _step("run", f"The {tool} uv tool is replaced by {setup_install.TOOL}.", argv=["uv", "tool", "uninstall", tool])
        for tool in olds
    ]
    install = {
        **setup_install.tool_step(checkout, extras),
        "warn_only": False,
        "why": f"The {setup_install.TOOL} uv tool must be editable from the checkout, so cox runs the monorepo.",
    }
    return [*uninstall, install]


_PATH_LINE = re.compile(r'\s*Environment=\s*"?PATH=')


def rewrite_dropin_path(text: str, pairs: Sequence[tuple[str, str]]) -> str:
    """Drop-in text with each old checkout folder replaced by its new one, on `Environment=PATH=` lines only."""
    olds = sorted({old for old, new in pairs if old != new}, key=len, reverse=True)
    if not olds:
        return text
    news = dict(pairs)
    folder = re.compile("(?:" + "|".join(re.escape(old) for old in olds) + r')(?=[/:"\s]|$)')

    def line(raw: str) -> str:
        return folder.sub(lambda m: news[m.group(0)], raw) if _PATH_LINE.match(raw) else raw

    return "".join(map(line, text.splitlines(keepends=True)))


def _chair_unit_steps(
    unit: str | None, drop_ins: Sequence[tuple[str, str]], active: bool, installs: str, checkout: str
) -> list[Step]:
    """One step when the effective PATH still names a component checkout: the unit file regenerated, or its
    drop-in rewritten so each component venv becomes the checkout's one venv."""
    seen = chair_unit.effective_path(unit, drop_ins)
    olds = {name: posixpath.join(installs, name) for name in RETIRED}
    if seen is None or not any(_names(seen.path, old) for old in olds.values()):
        return []
    if seen.source == chair_unit.UNIT_NAME:
        why = "The chair systemd unit still names a component checkout in its PATH, so it is regenerated."
        return [_step("chair_unit", why)]
    pairs = [
        *([f"{old}/.venv", f"{checkout}/.venv"] for old in olds.values()),
        *([old, posixpath.join(checkout, SUBDIRS[name])] for name, old in olds.items()),
    ]
    why = "A chair unit drop-in still names a component checkout in its PATH, so those folders are rewritten."
    return [_step("chair_unit_dropin", why, dropin=seen.source, pairs=pairs, restart=active)]


def _retire_steps(present: Sequence[str], installs: str, retired: str, worktrees: Mapping[str, list[str]]) -> list[Step]:
    """Each component checkout moves aside, never deleted, and its worktrees are repaired where it now sits."""
    return [
        step
        for name in present
        for step in (
            _step(
                "move",
                f"{name} is part of the monorepo now, so its checkout retires; nothing deletes it.",
                src=posixpath.join(installs, name),
                dst=posixpath.join(retired, name),
            ),
            *(_repair_steps(name, posixpath.join(retired, name), worktrees[name]) if worktrees.get(name) else ()),
        )
    ]


def _prints(workspace: Mapping[str, Any] | None, moved: Mapping[str, str], locs: Locations) -> list[Step]:
    notes = [_step("print", FINISH_HINT, item="retired checkouts", checkouts=dict(moved))]
    if not workspace:
        return notes
    path = workspace["path"]
    where = "under the workspaces folder" if path.startswith(locs.workspaces + "/") else "where it is registered"
    return [*notes, _step("print", f"The workspace stays {where} and is never moved.", item="workspace", path=path)]


def profile_install_folder(harness_dir: Any, user_home: str) -> str | None:
    """The folder the profile's `harness_dir` says holds the install: `<X>` for `<X>/coxswain-graphs` (the 0.47
    layout) and for `<X>/coxswain/src/graphs` (the monorepo); None when it names neither."""
    if not isinstance(harness_dir, str):
        return None
    path = _expand_all(harness_dir, user_home).rstrip("/")
    suffixes = (f"/{GRAPHS}", f"/{UMBRELLA}/{SUBDIRS[GRAPHS]}")
    return next((path[: -len(s)] for s in suffixes if path.endswith(s) and len(path) > len(s)), None)


def install_locations(locs: Locations, folder: str | None, umbrella_present: bool) -> Locations:
    """The locations the migration works in. A 0.47 install made with `cox install --root <X>` keeps its checkouts
    under `<X>`, not the home: when the profile names such a folder and `<X>/coxswain` is there, the install is
    migrated where it sits (`installs` becomes `<X>`). Anything else migrates the home's installs folder."""
    return replace(locs, installs=folder) if folder and folder != locs.installs and umbrella_present else locs


def migrate_plan(observed: dict[str, Any], locs: Locations, home_spellings: Sequence[str] = ()) -> list[Step]:
    """Ordered steps for the remaining migration; one skip when none remain, only refuses when any rule refuses.
    `home_spellings` are extra absolute spellings of the user home that a repo_map value may use."""
    paths = setup_install.layout(locs.installs)
    refusals = _refusals(observed, paths.checkout)
    if refusals:
        return refusals
    present = _present(observed)
    retired = retired_dir(locs.installs, str(observed.get("today", "")))
    moved = {posixpath.join(locs.installs, name): posixpath.join(retired, name) for name in present}
    market_runs, market_notes = _marketplace_steps(observed, paths.cartridges)
    body = [
        *_checkout_steps(observed.get("checkout") or {}, observed.get("target_tag"), paths.checkout),
        *([] if observed.get("venv_source") == paths.checkout else [setup_install.sync_step(paths.checkout)]),
        *_setting_steps(
            observed.get("settings", {}), observed.get("home_record"), locs, paths.checkout, tuple(moved.items()),
            home_spellings,
        ),
        *_link_steps(observed["cartridge_links"], locs),
        *market_runs,
        *_tool_steps(observed.get("install_bins"), paths.checkout),
        *_chair_unit_steps(
            observed.get("chair_unit"),
            tuple((name, text) for name, text in observed.get("chair_unit_drop_ins", ())),
            bool(observed.get("chair_active", False)),
            locs.installs,
            paths.checkout,
        ),
        *_retire_steps(present, locs.installs, retired, observed.get("worktrees", {})),
    ]
    if not body:
        return [_step("skip", "The checkout, its venv, the tools, links, settings and profile keys are already final.")]
    return [*body, *_prints(observed.get("workspace"), moved, locs), *market_notes]


def is_monorepo_pyproject(text: str | None) -> bool:
    """True when pyproject text names the `coxswain` distribution."""
    try:
        return tomllib.loads(text or "").get("project", {}).get("name") == setup_install.TOOL
    except tomllib.TOMLDecodeError:
        return False


def read_is_monorepo(checkout: str) -> bool:
    """Edge. Whether `<checkout>/pyproject.toml` names the coxswain distribution."""
    try:
        return is_monorepo_pyproject((Path(checkout) / "pyproject.toml").read_text())
    except OSError:
        return False


def read_venv_source(checkout: str, dist: str = setup_install.TOOL) -> str | None:
    """Edge. The editable source in the dist-info direct_url.json of `checkout`'s .venv, without file://."""
    pattern = f".venv/lib/python*/site-packages/{dist}-*.dist-info/direct_url.json"
    for path in sorted(Path(checkout).glob(pattern)):
        try:
            url = json.loads(path.read_text()).get("url")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(url, str):
            return url.removeprefix("file://")
    return None


def _receipt_entry(tool_dir: str, tool: str) -> dict[str, Any]:
    """Edge. Whether `tool` is installed, and the editable value and extras of its uv receipt."""
    absent: dict[str, Any] = {"installed": False, "editable": None, "extras": []}
    try:
        data = tomllib.loads((Path(tool_dir) / tool / "uv-receipt.toml").read_text())
        requirements = data["tool"]["requirements"]
    except (OSError, ValueError, KeyError, TypeError):
        return absent
    found = [r for r in requirements if isinstance(r, dict) and r.get("name") == tool]
    if not found:
        return {**absent, "installed": True}
    editable, extras = found[0].get("editable"), found[0].get("extras")
    return {
        "installed": True,
        "editable": editable if isinstance(editable, str) else None,
        "extras": [x for x in extras if isinstance(x, str)] if isinstance(extras, list) else [],
    }


def observe_install_bins(tool_dir: str) -> dict[str, Any]:
    """Edge. The `install_bins` observation: the receipt of each component uv tool and of `coxswain`.

    A missing or unreadable receipt is recorded as not installed, never raised. `tool_dir` is `uv tool dir`."""
    return {tool: _receipt_entry(tool_dir, tool) for tool in (*TOOL_RECEIPTS.values(), setup_install.TOOL)}


def _detail(step: Step) -> str:
    op = step["op"]
    if op == "checkout":
        return f"{step['path']} at {step['tag']}"
    if op == "move":
        return f"{step['src']} -> {step['dst']}"
    if op == "symlink":
        return f"{step['path']} -> {step['target']}"
    if op in ("rewrite_setting", "rewrite_profile_key"):
        return f"{step['key']} {step['old']} -> {step['new']}"
    if op in ("worktree_repair", "verify", "run"):
        return " ".join(step["argv"])
    return step.get("checkout") or step.get("item") or step.get("dropin") or ""


def render_plan(steps: list[Step]) -> str:
    """One line per step, numbered from 1, for a dry run."""
    return "\n".join(
        " ".join(part for part in (f"{n}.", step["op"], _detail(step), "-", step["why"]) if part)
        for n, step in enumerate(steps, 1)
    )


class LeaseOutcome(NamedTuple):
    path: str
    granted: bool
    holder: str | None = None
    error: str | None = None


class Lane(NamedTuple):
    name: str
    host: str | None


class ChairLease(NamedTuple):
    released: bool
    stale: bool
    host: str | None


class HostRow(NamedTuple):
    name: str
    state: str


def _lease_line(lease: LeaseOutcome) -> str:
    who = lease.holder or f"an unknown holder (lease error: {lease.error})"
    return f"migrate: refusing, {who} holds {lease.path}"


def preflight_refusals(
    leases: tuple[LeaseOutcome, ...],
    lanes: tuple[Lane, ...],
    hostname: str,
    chair: ChairLease,
    hosts: tuple[HostRow, ...],
) -> list[str]:
    """Refusal lines in the order leases, lanes, chair, host row; a lane with host None counts as local."""
    return [
        *(_lease_line(lease) for lease in leases if not lease.granted),
        *(
            f"migrate: refusing, live lane {lane.name} holds a run on this machine"
            for lane in lanes
            if lane.host is None or lane.host == hostname
        ),
        *(
            [f"migrate: refusing, the chair holds its lease on {hostname}"]
            if not chair.released and not chair.stale and chair.host == hostname
            else []
        ),
        *(
            f"migrate: refusing, host {row.name} holds active state, run cox host drain {row.name} first"
            for row in hosts
            if row.name == hostname and row.state == "active"
        ),
    ]


class LinkFact(NamedTuple):
    """What the edge saw at an old checkout path: kind is absent, dir, symlink or file; target is the raw link text."""

    name: str
    path: str
    kind: str
    target: str | None = None


class FinishFacts(NamedTuple):
    """Everything `finish` decides on. `marker_text` is None when the marker file is absent."""

    old_root: str | None
    marker_text: str | None
    installs: str
    home: str
    plan: Sequence[Step]
    home_rows: Sequence[Mapping[str, Any]]
    hosts: Sequence[ObservedHost]
    rows: Sequence[Mapping[str, Any]]
    files: Mapping[str, str]
    links: Sequence[LinkFact]


class FinishEdge(NamedTuple):
    """The only side effects `finish` may cause, and only in apply mode. `write` takes (old, new) tilde prefixes."""

    write: Callable[[tuple[tuple[str, str], ...]], None]
    rescan: Callable[[], tuple[Sequence[Mapping[str, Any]], Mapping[str, str]]]
    remove: Callable[[str], None]
    delete_marker: Callable[[], None]


class FinishResult(NamedTuple):
    code: int
    lines: tuple[str, ...]
    rewritten: tuple[Change, ...] = ()
    removed: tuple[str, ...] = ()
    kept: tuple[str, ...] = ()


def marker_path(locs: Locations) -> str:
    return posixpath.join(locs.state, MARKER_NAME)


def resolve_old_root(explicit: str | None, marker_text: str | None) -> str | None:
    """The explicit root, else `old_root` from the marker JSON; None when neither names one."""
    if explicit:
        return explicit
    if marker_text is None:
        return None
    try:
        data = json.loads(marker_text)
    except ValueError:
        return None
    root = data.get("old_root") if isinstance(data, dict) else None
    return root if isinstance(root, str) and root else None


def finish_gates(
    plan: Sequence[Step], home_rows: Sequence[Mapping[str, Any]], hosts: Sequence[ObservedHost]
) -> list[str]:
    """One `finish: waiting on` line per failing gate; `hosts` are the active rows only."""
    single_skip = len(plan) == 1 and plan[0].get("op") == "skip"
    return [
        *([] if single_skip else ["finish: waiting on the local plan to reach a single skip step"]),
        *(f"finish: waiting on home check {row.get('check')}" for row in home_rows if not row.get("ok")),
        *(f"finish: waiting on host {h.name} to beat" for h in hosts if h.source != "beat"),
        *(f"finish: waiting on host {h.name} to migrate" for h in hosts if h.source == "beat" and not is_migrated(h)),
    ]


def rewrite_moves(old_root: str, installs: str, home: str) -> tuple[tuple[str, str], ...]:
    """Per checkout, the tilde prefix under the old root and the tilde prefix under the installs folder."""
    return tuple(
        (repo_path.contract(posixpath.join(old_root, n), home), repo_path.contract(posixpath.join(installs, n), home))
        for n in CHECKOUTS
    )


def _under(value: str, prefix: str) -> bool:
    return value == prefix or value.startswith(prefix + "/")


def pending_changes(
    rows: Sequence[Mapping[str, Any]], files: Mapping[str, str], home: str, moves: tuple[tuple[str, str], ...]
) -> list[Change]:
    """Values still under an old prefix. A change that only contracts an unrelated path is not finish's to report."""
    return [
        c
        for old, new in moves
        for c in repo_path_migrate.move_changes(rows, files, home, old, new)
        if _under(repo_path.contract(c["old"], home), old)
    ]


def _verdict(link: LinkFact, installs: str) -> tuple[str, str]:
    want = posixpath.join(installs, link.name)
    if link.kind == "absent":
        return "none", ""
    if link.kind == "symlink" and link.target == want:
        return "remove", ""
    if link.kind == "symlink":
        return "keep", f"a link to {link.target}, not {want}"
    return "keep", "a real directory" if link.kind == "dir" else "a file, not a link"


def removal_decisions(links: Sequence[LinkFact], installs: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Paths to remove: symlinks whose target equals the checkout under `installs`. The rest are kept, with a reason."""
    verdicts = [(link, *_verdict(link, installs)) for link in links]
    return (
        tuple(link.path for link, what, _ in verdicts if what == "remove"),
        tuple(f"{link.path}: {why}" for link, what, why in verdicts if what == "keep"),
    )


def finish(facts: FinishFacts, apply: bool, edge: FinishEdge) -> FinishResult:
    """Plan by default. Gates first, then rewrite and rescan, then link removal, then the marker. Writes only when `apply`."""
    old_root = resolve_old_root(facts.old_root, facts.marker_text)
    if old_root is None:
        return FinishResult(2, (FINISH_NO_ROOT,))
    waiting = finish_gates(facts.plan, facts.home_rows, facts.hosts)
    if waiting:
        return FinishResult(1, tuple(waiting))
    moves = rewrite_moves(old_root, facts.installs, facts.home)
    changes = pending_changes(facts.rows, facts.files, facts.home, moves)
    removals, kept = removal_decisions(facts.links, facts.installs)
    shown = "" if apply else "would "
    kept_lines = tuple(f"finish: kept {k}" for k in kept)
    if not (changes or removals or kept or facts.marker_text is not None):
        return FinishResult(0, ("finish: nothing to do",))
    if apply and changes:
        edge.write(moves)
        left = pending_changes(*edge.rescan(), facts.home, moves)
        if left:
            return FinishResult(1, (f"finish: {len(left)} value(s) still under {old_root}",), tuple(changes))
    if apply:
        for path in removals:
            edge.remove(path)
        if facts.marker_text is not None:
            edge.delete_marker()
    lines = (
        *(f"finish: {shown}rewrite {c['key']}: {c['old']} -> {c['new']}" for c in changes),
        *(f"finish: {shown}remove {path}" for path in removals),
        *kept_lines,
        *([f"finish: {shown}delete the marker"] if facts.marker_text is not None else []),
    )
    return FinishResult(0, lines, tuple(changes), removals, kept)


def observe_links(old_root: str, home: str) -> tuple[LinkFact, ...]:
    """Edge. What sits at <old root>/<name> for each checkout."""
    root = repo_path.expand(old_root, home) if old_root.startswith("~") else old_root
    return tuple(_observe_link(n, posixpath.join(root, n)) for n in CHECKOUTS)


def _observe_link(name: str, path: str) -> LinkFact:
    p = Path(path)
    if p.is_symlink():
        return LinkFact(name, path, "symlink", str(p.readlink()))
    if p.is_dir():
        return LinkFact(name, path, "dir")
    return LinkFact(name, path, "file" if p.exists() else "absent")


def remove_link(path: str) -> None:
    """Edge. Unlinks one symlink; `finish` only passes paths it judged to be links to the installs checkout."""
    Path(path).unlink()


def delete_marker(path: str) -> None:
    """Edge. Removes the marker file; an absent marker is fine."""
    Path(path).unlink(missing_ok=True)
