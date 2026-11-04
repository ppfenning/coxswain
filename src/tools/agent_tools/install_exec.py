"""The edge that turns an `install.plan()` step into a real git/cox
invocation. `install.py` already decided what to do; this only turns that
decision into a subprocess call — through an injected `run` — and a record
of what happened. No decision-making lives here: `from_plan` only carries
forward data the planner already knew (a component's `repo`/`tag`, the
requested `team`/`workspace`) so `execute` never has to re-derive it."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from agent_tools import dash_binary
from agent_tools.install import CHECKOUT, checkout_spec

Runner = Callable[[list, str | None], tuple]


def from_plan(steps: Sequence[Mapping], *, manifest: Mapping, options: Mapping) -> list[dict]:
    """Enriches each planner step with the fields `execute` needs to build
    a real command: `repo`/`tag` for `clone`/`fetch_checkout` from the
    manifest, `team`/`workspace` (and `home`, when supplied) for
    `setup_install` from the options.
    Leaves `skip`, `refuse`, `doctor`, `desktop`, `binary`, `dev_build` and `dev_link` steps as they arrived."""
    components = {CHECKOUT: checkout_spec(manifest), **manifest.get("components", {})}
    return [_enrich(step, components, options) for step in steps]


def _enrich(step: Mapping, components: Mapping, options: Mapping) -> dict:
    """One planner step with the fields `execute` needs for its kind."""
    kind = step["kind"]
    if kind in ("clone", "fetch_checkout"):
        spec = components.get(step.get("component"), {})
        return {**step, "repo": spec.get("repo"), "tag": spec.get("tag"), "ref": spec.get("ref", "main"),
                "channel": options.get("channel", "release")}
    if kind == "setup_install":
        enriched = {**step, "team": options.get("team"), "workspace": options.get("workspace")}
        home = options.get("home")
        return enriched if home is None else {**enriched, "home": home}
    return dict(step)


def _edge(step: Mapping) -> bool:
    return step.get("channel") == "edge"


def clone_dir(root: str, component: str) -> str:
    """The one place a clone target is named: the checkout is `<root>/coxswain`, which `setup_install` installs;
    an optional component such as dash is `<root>/coxswain-<component>`."""
    return f"{root}/{CHECKOUT}" if component == CHECKOUT else f"{root}/coxswain-{component}"


def _clone_argv(step: Mapping, root: str) -> list:
    pin = step["ref"] if _edge(step) else step["tag"]
    return ["git", "clone", "--branch", pin, "--depth", "1",
            f"https://github.com/{step['repo']}.git", clone_dir(root, step["component"])]


def _fetch_argv(step: Mapping, root: str) -> list:
    directory = clone_dir(root, step["component"])
    if _edge(step):
        ref = step["ref"]
        return ["git", "-C", directory, "fetch", "origin",
                f"+refs/heads/{ref}:refs/remotes/origin/{ref}"]
    return ["git", "-C", directory, "fetch", "--tags"]


def _checkout_argv(step: Mapping, root: str) -> list:
    """Edge moves the local branch onto the fetched tip; release checks out the tag."""
    directory = clone_dir(root, step["component"])
    if _edge(step):
        return ["git", "-C", directory, "checkout", "-B", step["ref"], f"origin/{step['ref']}"]
    return ["git", "-C", directory, "checkout", step["tag"]]


def _setup_install_argv(step: Mapping, root: str) -> list:
    home = step.get("home")
    argv = ["cox", "setup", "install", *(["--root", root] if home is None else ["--home", home])]
    if step.get("team") is not None:
        argv += ["--team", step["team"]]
    if step.get("workspace") is not None:
        argv += ["--workspace", step["workspace"]]
    return argv


def _binary_argvs(step: Mapping) -> list[list]:
    """Download, unpack and copy argvs for a `binary` step, built from the step alone.
    The scratch directory sits beside the destination, so no clock, environment or tempfile is read.
    The release archive holds the binaries at its top level, as the dash readme's `tar -xzf` then `mv` shows."""
    scratch = f"{step['bin_dir']}/.towpath-{step['target']}.tmp"
    archive = f"{scratch}/{step['url'].rsplit('/', 1)[-1]}"
    return [
        ["curl", "--fail", "--location", "--silent", "--show-error", "--create-dirs", "--output", archive, step["url"]],
        ["tar", "-xzf", archive, "-C", scratch],
        ["cp", "-f", *(f"{scratch}/{name}" for name in step["binaries"]), step["bin_dir"]],
    ]


def _run_argvs(argvs: Sequence[list], run: Runner) -> tuple:
    """Runs each argv in order and stops at the first non-zero exit; returns the argvs run, the last exit, all output."""
    done, output, exit_code = [], "", 0
    for argv in argvs:
        exit_code, out = run(argv, None)
        done, output = [*done, argv], output + out
        if exit_code != 0:
            break
    return done, exit_code, output


def _run_step(step: Mapping, root: str, run: Runner) -> tuple:
    """The commands run (always a list of argv, one or more) and the
    (exit, output) actually observed for one runnable step."""
    kind = step["kind"]
    if kind == "binary":
        return _run_argvs(_binary_argvs(step), run)
    if kind == "dev_build":
        return _run_argvs([dash_binary.dev_build_argv(clone_dir(root, step["component"]))], run)
    if kind == "dev_link":
        return _run_argvs(dash_binary.dev_link_argvs(clone_dir(root, step["component"]), step["bin_dir"]), run)
    if kind == "clone":
        argv = _clone_argv(step, root)
        exit_code, output = run(argv, None)
        return [argv], exit_code, output
    if kind == "fetch_checkout":
        fetch_argv = _fetch_argv(step, root)
        exit_code, output = run(fetch_argv, None)
        if exit_code != 0:
            return [fetch_argv], exit_code, output
        checkout_argv = _checkout_argv(step, root)
        checkout_exit, checkout_output = run(checkout_argv, None)
        return [fetch_argv, checkout_argv], checkout_exit, output + checkout_output
    if kind == "setup_install":
        argv = _setup_install_argv(step, root)
        exit_code, output = run(argv, None)
        return [argv], exit_code, output
    if kind == "doctor":
        argv = ["cox", "setup", "doctor"]
        exit_code, output = run(argv, None)
        return [argv], exit_code, output
    return [], 1, f"unknown step kind: {kind}"


def is_fatal(result: Mapping) -> bool:
    """True when a step result is a non-zero exit that must stop the run.
    A doctor step and a dev_build step are exempt: a failed build leaves the release binary already on disk in place."""
    return result["exit"] not in (0, None) and result["step"]["kind"] not in ("doctor", "dev_build")


def _build_failed(results: Sequence[Mapping], component: str) -> bool:
    """True when a `dev_build` for `component` already ran and exited non-zero."""
    return any(
        r["step"]["kind"] == "dev_build" and r["step"]["component"] == component and r["exit"] not in (0, None)
        for r in results
    )


def execute(steps: Sequence[Mapping], *, root: str, run: Runner) -> list[dict]:
    """Runs `steps` in order through the injected `run`, stopping at the
    first fatal non-zero exit or at a `refuse`; a non-zero `doctor` does not
    stop the steps after it. A `dev_link` after a failed `dev_build` of the
    same component is not run and carries `exit: None`, so `ln -sf` never
    replaces the release binary with a link to a build that did not happen.
    `skip` runs nothing and reports clean; `desktop` prints its no-op line instead of calling `run`. Every
    step gets one `{"step", "commands", "exit", "output"}` record (`commands` is
    always a list of argv, empty for a step that ran no command); steps after
    a stop carry `exit: None` because they never ran."""
    results = []
    stopped = False
    for step in steps:
        kind = step["kind"]
        if stopped or (kind == "dev_link" and _build_failed(results, step["component"])):
            results.append({"step": step, "commands": [], "exit": None, "output": None})
            continue
        if kind == "skip":
            results.append({"step": step, "commands": [], "exit": 0, "output": ""})
            continue
        if kind == "refuse":
            results.append({"step": step, "commands": [], "exit": 2, "output": step.get("detail", "")})
            stopped = True
            continue
        if kind == "desktop":
            output = "desktop: planned"
            print(output)
            results.append({"step": step, "commands": [], "exit": 0, "output": output})
            continue
        commands, exit_code, output = _run_step(step, root, run)
        result = {"step": step, "commands": commands, "exit": exit_code, "output": output}
        results.append(result)
        stopped = is_fatal(result)
    return results
