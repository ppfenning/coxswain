"""Runs an upgrade Plan: git fetch and checkout per step, then the steps that follow the checkout's move
(its venv sync, the cox tool, the crew links). The only side effects are the injected
`run(argv, cwd) -> (exit, output)` and print."""
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from agent_tools import release_version
from agent_tools.install import CHECKOUT
from agent_tools.install_exec import clone_dir
from agent_tools.upgrade_plan import Plan, Step

Run = Callable[[list, str | None], tuple]


def offered_tags(ls_remote_output: str) -> tuple[str, ...]:
    """Tag names in `git ls-remote --tags` output, in the order given, with peeled `^{}` duplicates dropped."""
    names = (
        line.split("\t", 1)[1].removeprefix("refs/tags/").removesuffix("^{}")
        for line in ls_remote_output.splitlines()
        if "\trefs/tags/" in line
    )
    return tuple(dict.fromkeys(names))


def newest_release(tags: Sequence[str]) -> str | None:
    """The greatest `v`-prefixed release tag, whatever order `tags` come in: CalVer `v2026.10.0` above every
    legacy `v0.x` tag. A prerelease such as `v1.2.3-rc1` or `v0.20.0-beta.1` is never a default target."""
    return release_version.newest(tag for tag in tags if tag.startswith("v"))


def _step_dir(root: Path, step: Step) -> Path:
    """The umbrella step moves the one checkout, `<root>/coxswain`; a component is `<root>/coxswain-<name>`."""
    return Path(clone_dir(str(root), CHECKOUT if step.kind == "umbrella" else step.name))


def _move(step: Step, root: Path, run: Run, clone_url: str | None) -> tuple:
    """A component with a clone URL has no checkout yet: clone it instead of fetching."""
    directory = str(_step_dir(root, step))
    first = ["git", "clone", clone_url, directory] if clone_url else ["git", "-C", directory, "fetch", "--tags"]
    for argv in (first, ["git", "-C", directory, "checkout", step.to_tag]):
        code, output = run(argv, None)
        if code != 0:
            return code, output
    return 0, ""


def _describe(step: Step) -> str:
    return f"{step.name}: {step.from_tag or 'not installed'} to {step.to_tag}"


def _after(steps: Sequence[Mapping], run: Run) -> tuple:
    """Each step in order; a warn-only step that fails prints and goes on, any other stops with its exit."""
    for step in steps:
        code, output = run(list(step["argv"]), step.get("cwd"))
        if code == 0:
            continue
        if not step.get("warn_only"):
            return code, output
        print(f"warn: {' '.join(step['argv'])} exited {code}: {output.strip()}")
    return 0, ""


def run_plan(
    plan: Plan,
    *,
    root: Path | str,
    run: Run,
    dry_run: bool,
    clone_urls: Mapping[str, str] | None = None,
    after_move: Sequence[Mapping] = (),
) -> int:
    """Exit 2 on a failed command or when any component is left behind, else 0. A dry run makes no `run` call.
    `clone_urls` names the components with no checkout yet. `after_move` are run steps (`argv`, `cwd`,
    `warn_only`) that follow the umbrella step: they run only once the checkout has moved."""
    root = Path(root)
    urls = clone_urls or {}
    for step in plan.steps:
        print(f"would move {_describe(step)}" if dry_run else f"moving {_describe(step)}")
        follows = after_move if step.kind == "umbrella" else ()
        if dry_run:
            for after in follows:
                print(f"would run {' '.join(after['argv'])}")
            continue
        code, output = _move(step, root, run, urls.get(step.name) if step.kind == "component" else None)
        if code == 0 and follows:
            code, output = _after(follows, run)
        if code != 0:
            print(f"FAILED: {step.name}: {output.strip()}")
            return 2
    for item in plan.left_behind:
        print(f"left behind: {item.name}: {item.reason}: {item.detail}")
    return 2 if plan.left_behind else 0
