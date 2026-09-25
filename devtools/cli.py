"""The maintainer's release edge, moved out of the published `cox dev` group:
`uv run --frozen python -m devtools release|release-check|backfill-github-releases|commands`,
run from the umbrella checkout (`--frozen`, or a moved component version rewrites
uv.lock and the release refuses a dirty umbrella). Same arguments and output as `cox dev ...`."""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path

from devtools import (
    release,
    release_check,
    release_check_cli,
    release_check_index,
    release_check_manifest,
    release_check_notes,
    release_check_pages,
    release_check_readmes,
)

_NO_CHECKS = "no checks reported"
# the umbrella's own README is not the command table's; render tools' by default
TOOLS_README = Path(__file__).resolve().parent.parent.parent / "coxswain-tools" / "README.md"


def _wait_decision(returncode: int, output: str, elapsed_s: float, timeout_s: float) -> str:
    """`green`, `failed`, `retry` or `timeout`: a branch whose checks have not registered yet is retried until `timeout_s`."""
    if returncode == 0:
        return "green"
    if _NO_CHECKS in output.lower():
        return "retry" if elapsed_s < timeout_s else "timeout"
    return "failed"


def _await_checks(poll, timeout_s: float = 180.0, sleep=time.sleep, now=time.monotonic) -> tuple[bool, str]:
    """`poll() -> (returncode, output)` until green or failed. No check yet means not yet: retry every 15s for `timeout_s`."""
    started, waiting = now(), False
    while True:
        rc, output = poll()
        decision = _wait_decision(rc, output, now() - started, timeout_s)
        if decision == "retry":
            if not waiting:
                print(f"no checks reported yet, waiting up to {timeout_s:.0f}s for the first one to appear")
            waiting = True
            sleep(15)
        elif decision == "timeout":
            return False, f"no checks reported within {timeout_s:.0f}s"
        else:
            return decision == "green", "green" if decision == "green" else output.strip()


def _read_text_or_none(p: Path):
    try:
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def _load_manifest(path: Path) -> dict | None:
    """The parsed manifest, or None on a missing or unparseable file — never
    a traceback; the caller turns None into a named, exit-2 refusal."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None


def _real_run(argv: list, cwd: str | None) -> tuple:
    """The subprocess wrapper `install_exec.execute` calls at the edge;
    stdout and stderr are folded together since the caller only prints."""
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def _remote_tags(repo: str) -> list[str] | None:
    """The tags on `repo`'s GitHub remote, or None when git could not read the
    remote. None is not `[]`: the planner refuses on None, so an unreachable or
    misnamed remote can never look like a clean one."""
    result = subprocess.run(["git", "ls-remote", "--tags", f"https://github.com/{repo}.git"],
                             capture_output=True, text=True)
    return None if result.returncode != 0 else release.parse_ls_remote(result.stdout)


_RELEASE_DETAIL = {
    "refuse": lambda step: step["detail"],
    "note": lambda step: step["detail"],
    "tag": lambda step: f"{step['repo']} -> {step['tag']}",
    "bump_manifest": lambda step: f"{step['from']} -> {step['to']}",
    "notes": lambda step: step["path"],
    "tag_self": lambda step: step["tag"],
    "wait_workflows": lambda step: step["tag"],
    "pinned": lambda step: step["tag"],
    "tap_formula_pr": lambda step: f"{step['repo']} {step['path']} from {step['index_url']}",
    "rejoin": lambda step: f"{step['from']} -> {step['tag']} ({step['commits']} commits)",
    "github_release": lambda step: (f"{step['repo']} -> "
        f"{' '.join(release.github_release_create_argv(step['tag'], step['title'], step['notes_path']))}"),
}


def _release_detail(step: dict) -> str:
    """One line of detail per step kind; an unknown kind shows itself rather than raising."""
    fmt = _RELEASE_DETAIL.get(step["kind"])
    return fmt(step) if fmt is not None else str(step)


def _default_branch(directory: str, run) -> str:
    """`directory`'s default branch, read from `origin/HEAD` — `"main"` when
    that symbolic ref can't be read (a plain checkout with no such ref set,
    or a directory the fake runner in tests never populated one for)."""
    ref_rc, ref_out = run(["git", "-C", directory, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], None)
    return ref_out.strip().rsplit("/", 1)[-1] if ref_rc == 0 and ref_out.strip() else "main"


def _checkout_ready(directory: str, run) -> tuple[bool, str]:
    """Clean and on its default branch, or `(False, reason)` — checked
    before a single tag is made, since a release must never tag some
    components and stop partway through a checkout that turns out dirty."""
    status_rc, status_out = run(["git", "-C", directory, "status", "--porcelain"], None)
    if status_rc != 0:
        return False, status_out.strip() or f"could not read status for {directory}"
    if status_out.strip():
        return False, f"{directory} is dirty"
    _, branch_out = run(["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"], None)
    current = branch_out.strip()
    default = _default_branch(directory, run)
    if current != default:
        return False, f"{directory} is on {current}, not {default}"
    return True, ""


def _dirty_files(directory: str, run) -> list[str]:
    """Paths `git status --porcelain` reports as changed in `directory`."""
    _, out = run(["git", "-C", directory, "status", "--porcelain"], None)
    return [line.strip().split(None, 1)[-1] for line in out.splitlines() if line.strip()]


def _fetch_index(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def _release_step_dir(component: str, root: str, overrides: dict, umbrella: str) -> str:
    """The checkout a `bump_pyproject`/`push`/`pr_create`/`wait_checks`/`merge`
    step runs against: `umbrella` for the manifest's own bump (its component
    is always `"manifest"`, never a real manifest entry), otherwise that
    component's own checkout."""
    return umbrella if component == "manifest" else release.component_dir(root, component, overrides)


def _release_bump(directory: str, branch: str, commit_subject: str, paths: list[str], write, run) -> tuple[bool, str]:
    """Checks out `branch` from the default branch in `directory`, and only
    once that succeeds calls `write()` to rewrite `paths` on disk — `write`
    never runs, and nothing on the default branch is ever touched, when the
    checkout itself fails (a stale local `branch` left by an earlier partial
    run, a hook, a permission error). Then stages `paths` and commits them
    with `commit_subject`. `(False, detail)` names the first git call that
    failed."""
    co_rc, co_out = run(release.checkout_branch_argv(directory, branch), None)
    if co_rc != 0:
        return False, co_out.strip() or "checkout failed"
    write()
    add_rc, add_out = run(release.add_argv(directory, *paths), None)
    if add_rc != 0:
        return False, add_out.strip() or "add failed"
    commit_rc, commit_out = run(release.commit_argv(directory, commit_subject, *paths), None)
    if commit_rc != 0:
        return False, commit_out.strip() or "commit failed"
    return True, "committed"


def _release_bump_pyproject(directory: str, path: Path, to: str, branch: str, commit_subject: str,
                             run) -> tuple[bool, str]:
    """`_release_bump` for a `bump_pyproject` step: rewrites `path`'s version
    line to `to`, only once `branch` is checked out."""
    def write():
        path.write_text(release.bumped_version_line(path.read_text(), to))
    return _release_bump(directory, branch, commit_subject, ["pyproject.toml"], write, run)


def _release_bump_manifest(umbrella: str, manifest_file: Path, to: str, branch: str, commit_subject: str,
                            rejoining: set, run, extra_paths: tuple[str, ...] = ()) -> tuple[bool, str]:
    """`_release_bump` for a `bump_manifest` step: rewrites `manifest_file`'s
    version and component tags, and — when the umbrella checkout carries its
    own `pyproject.toml` — that file's version line and, when `uv.lock` also
    exists and that pyproject names a package, its matching `[[package]]`
    stanza's version — all only once `branch` is checked out. Which of the
    umbrella's files exist is read before the checkout (a read touches
    nothing), so `paths` is known up front without ever writing early.
    `extra_paths` (files already rewritten on disk, such as the release index)
    ride in the same commit."""
    pyproject_path = Path(umbrella) / "pyproject.toml"
    lock_path = Path(umbrella) / "uv.lock"
    paths = ["manifest.toml", *extra_paths]
    package_name = None
    if pyproject_path.exists():
        package_name = tomllib.loads(pyproject_path.read_text()).get("project", {}).get("name")
        paths.append("pyproject.toml")
        if lock_path.exists() and package_name:
            paths.append("uv.lock")

    def write():
        manifest_file.write_text(release.bumped_manifest_text(manifest_file.read_text(), to, rejoining=rejoining))
        if "pyproject.toml" in paths:
            pyproject_path.write_text(release.bumped_version_line(pyproject_path.read_text(), to))
        if "uv.lock" in paths:
            lock_path.write_text(release.bumped_uv_lock_text(lock_path.read_text(), package_name, to))

    return _release_bump(umbrella, branch, commit_subject, paths, write, run)


def _component_declares_tag_trigger(directory: str) -> bool:
    """True when any `.github/workflows/*.y*ml` file under `directory`
    triggers on a pushed tag — read at the edge, decided by the pure
    `release.declares_tag_trigger`."""
    workflows_dir = Path(directory) / ".github" / "workflows"
    if not workflows_dir.is_dir():
        return False
    paths = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
    return any(release.declares_tag_trigger(p.read_text(encoding="utf-8")) for p in paths)


def _wait_workflows(directory: str, tag: str, component: str, run,
                     timeout_s: float = 900, sleep=time.sleep, now=time.monotonic) -> tuple[bool, str]:
    """A component with no tag-triggered workflow has nothing to wait on and
    returns immediately, before ever polling — otherwise it would stall for
    the full `timeout_s` on every release. Otherwise polls `gh run list
    --branch <tag> --json status,conclusion,name,url,event,headBranch`,
    keeping only the runs the tag itself started — event `push` on that
    exact `headBranch` — until every one has concluded or `timeout_s`
    passes; zero matching runs inside that loop is always still pending.
    Any run whose conclusion is not `success` after that fails, naming
    `component`, that run's `name` and its `url`; so does a timeout with
    zero matching runs, since a declared trigger means one was expected."""
    if not _component_declares_tag_trigger(directory):
        return True, "no tag-triggered workflow for this component"
    started = now()
    while True:
        # `gh` infers the repository from its working directory; from the
        # release root (not a git repository) it fails before asking GitHub —
        # the first real 0.7.0 cut stopped on exactly that after tagging one component.
        gh_rc, gh_out = run(["gh", "run", "list", "--branch", tag, "--json",
                              "status,conclusion,name,url,event,headBranch"], directory)
        if gh_rc != 0:
            return False, gh_out.strip() or "gh run list failed"
        all_runs = json.loads(gh_out) if gh_out.strip() else []
        runs = [r for r in all_runs if r.get("event") == "push" and r.get("headBranch") == tag]
        pending = any(r.get("status") != "completed" for r in runs)
        timed_out = now() - started >= timeout_s
        if (not runs or pending) and not timed_out:
            sleep(10)
            continue
        if not runs:
            return False, f"{component}: no workflow run started for {tag} within {timeout_s:.0f}s"
        failed = next((r for r in runs if r.get("conclusion") != "success"), None)
        if failed is not None:
            return False, f"{component}: {failed.get('name')} did not succeed ({failed.get('url')})"
        return True, f"{len(runs)} run(s) green for {tag}"


def _previous_release_tag(umbrella: str, name: str, version: str, run) -> str | None:
    """`name`'s tag in the manifest of the release before `version` — the
    latest `docs/releases/*.md` stem below `version`, read via `git show
    v<previous>:manifest.toml` through the injected `run` — or None when
    `version` is the first release this umbrella has notes for."""
    releases_dir = Path(umbrella) / "docs" / "releases"
    versions = release.versions_oldest_first(p.stem for p in releases_dir.glob("*.md") if p.stem != "index")
    earlier = [v for v in versions if v != version]
    if not earlier:
        return None
    show_rc, show_out = run(["git", "-C", umbrella, "show", f"v{earlier[-1]}:manifest.toml"], None)
    if show_rc != 0:
        return None
    return tomllib.loads(show_out).get("components", {}).get(name, {}).get("tag")


def _github_release_notes_text(umbrella: str, notes_path: str, heading: str, from_tag: str | None,
                                link: str | None) -> str:
    """The `github_release` step's own body for a component or crew section:
    `notes_path`'s text under `heading`, `unchanged since <from_tag>` when
    that section is absent and a previous release named a tag, or `first
    release` when none did — followed by the link line back to the umbrella
    release. The backfill command builds every body it creates or edits
    through this same function, so a rerun's comparison is apples to apples."""
    section = release.extract_release_notes(str(Path(umbrella) / notes_path), heading)
    fallback = f"unchanged since {from_tag}\n" if from_tag else "first release\n"
    body = section if section is not None else fallback
    link_line = f"\nSee the full release notes: {link}\n" if link else "\n"
    return body + link_line


def _release_execute(steps: list[dict], version: str, root: str, overrides: dict, umbrella: str, run,
                      manifest: dict, manifest_path: str, sleep=time.sleep, now=time.monotonic,
                      fetch_index=_fetch_index) -> int:
    """Runs `steps` for real, through `run`. Every checkout that will be
    tagged or branched — every component, the umbrella when `tag_self` is in
    the plan, and any component or the umbrella a `bump_pyproject` or
    `bump_manifest` step will branch — and the umbrella's release note are
    all checked before a single tag is made. Then each `tag` step's tag and
    push run in turn, and `tag_self` tags and pushes the umbrella; a
    `pinned` step runs no git command at all, since its component keeps the
    tag the manifest already names. A `bump_pyproject`/`bump_manifest` step
    checks out its `branch`, rewrites the version on disk and commits it —
    a no-op, printing "already at <version>", when the file already reads
    `to`, and every later `push`/`pr_create`/`wait_checks`/`merge` step for
    that same component no-ops the same way rather than push a branch
    nothing was committed to. `merge` squash-merges and, once that lands,
    switches the checkout back to its default branch and pulls it — the very
    next step for that same directory is the pre-existing `tag`/`tag_self`
    executor, which tags HEAD with no ref, so without this the tag would
    land on the stale pre-squash commit `bump_pyproject`/`bump_manifest`
    left checked out on `release/<version>` instead of what actually merged.
    A `github_release` step checks `gh release view` first and edits an
    existing release instead of creating one. One line per step; the first
    failure stops the rest."""
    refusal = next((s for s in steps if s["kind"] == "refuse"), None)
    if refusal is not None:
        print(f"refuse {refusal['component']}: {refusal['detail']}")
        return 2

    tag_checkouts = [(s["component"], release.component_dir(root, s["component"], overrides))
                      for s in steps if s["kind"] in ("tag", "rejoin")]
    umbrella_checkouts = [("coxswain", umbrella)] if any(s["kind"] == "tag_self" for s in steps) else []
    for name, directory in tag_checkouts + umbrella_checkouts:
        ready, reason = _checkout_ready(directory, run)
        if not ready:
            print(f"refuse {name}: {reason}")
            return 2

    for notes_step in (s for s in steps if s["kind"] == "notes"):
        if not (Path(umbrella) / notes_step["path"]).exists():
            print(f"refuse notes: {notes_step['path']} missing under {umbrella}")
            return 2

    # A component whose bump_pyproject/bump_manifest step no-ops (the file was
    # already at the target version) has nothing to push, PR, wait on or
    # merge; its later land steps no-op the same way rather than push a
    # branch nothing was ever committed to.
    already_bumped: dict[str, str] = {}
    # Paths the `notes` step really rewrote; the manifest bump commits them so
    # the umbrella is clean again before the merge pulls.
    rewritten: list[str] = []

    for step in steps:
        kind = step["kind"]
        if kind in ("tag", "rejoin"):
            directory = release.component_dir(root, step["component"], overrides)
            tag_rc, tag_out = run(release.tag_argv(directory, version), None)
            if tag_rc != 0:
                print(f"FAILED tag {step['component']}: {tag_out.strip()}")
                return 2
            push_rc, push_out = run(release.push_argv(directory, version), None)
            if push_rc != 0:
                print(f"FAILED push {step['component']}: {push_out.strip()}")
                return 2
            print(f"{kind} {step['component']}: {step['tag']}")
        elif kind == "notes":
            index_path = Path(umbrella) / "docs" / "releases" / "index.md"
            existing = index_path.read_text() if index_path.exists() else ""
            new_index = release.release_index_text(existing, version, manifest)
            if new_index != existing:
                index_path.write_text(new_index)
                rewritten.append("docs/releases/index.md")
            print(f"notes notes: {step['path']}")
        elif kind == "note":
            print(f"note {step['component']}: {step['detail']}")
        elif kind == "pinned":
            print(f"pinned {step['component']}: {step['tag']}")
        elif kind == "bump_pyproject":
            directory = release.component_dir(root, step["component"], overrides)
            path = Path(directory) / "pyproject.toml"
            if release.component_version(path.read_text()) == step["to"]:
                already_bumped[step["component"]] = step["to"]
                print(f"bump_pyproject {step['component']}: already at {step['to']}")
                continue
            ok, detail = _release_bump_pyproject(directory, path, step["to"], step["branch"],
                                                  step["commit_subject"], run)
            if not ok:
                print(f"FAILED bump_pyproject {step['component']}: {detail}")
                return 2
            print(f"bump_pyproject {step['component']}: {step['from']} -> {step['to']}")
        elif kind == "bump_manifest":
            manifest_file = Path(manifest_path)
            if tomllib.loads(manifest_file.read_text()).get("coxswain", {}).get("version") == step["to"]:
                already_bumped[step["component"]] = step["to"]
                print(f"bump_manifest {step['component']}: already at {step['to']}")
                continue
            ok, detail = _release_bump_manifest(umbrella, manifest_file, step["to"], step["branch"],
                                                 step["commit_subject"], release.rejoined(steps), run,
                                                 tuple(rewritten))
            if not ok:
                print(f"FAILED bump_manifest {step['component']}: {detail}")
                return 2
            print(f"bump_manifest {step['component']}: {step['from']} -> {step['to']}")
        elif kind == "push":
            if step["component"] in already_bumped:
                print(f"push {step['component']}: already at {already_bumped[step['component']]}")
                continue
            directory = _release_step_dir(step["component"], root, overrides, umbrella)
            push_rc, push_out = run(release.push_branch_argv(directory, step["branch"]), None)
            if push_rc != 0:
                print(f"FAILED push {step['component']}: {push_out.strip()}")
                return 2
            print(f"push {step['component']}: {step['branch']}")
        elif kind == "pr_create":
            if step["component"] in already_bumped:
                print(f"pr_create {step['component']}: already at {already_bumped[step['component']]}")
                continue
            directory = _release_step_dir(step["component"], root, overrides, umbrella)
            pr_rc, pr_out = run(release.pr_create_argv(step["title"], step["body"]), directory)
            if pr_rc != 0:
                print(f"FAILED pr_create {step['component']}: {pr_out.strip()}")
                return 2
            print(f"pr_create {step['component']}: {step['title']}")
        elif kind == "wait_checks":
            if step["component"] in already_bumped:
                print(f"wait_checks {step['component']}: already at {already_bumped[step['component']]}")
                continue
            directory = _release_step_dir(step["component"], root, overrides, umbrella)
            wc_ok, wc_out = _await_checks(lambda d=directory: run(release.pr_checks_argv(), d), sleep=sleep, now=now)
            if not wc_ok:
                print(f"FAILED wait_checks {step['component']}: {wc_out}")
                return 2
            print(f"wait_checks {step['component']}: {wc_out}")
        elif kind == "merge":
            if step["component"] in already_bumped:
                print(f"merge {step['component']}: already at {already_bumped[step['component']]}")
                continue
            directory = _release_step_dir(step["component"], root, overrides, umbrella)
            dirty = _dirty_files(directory, run)
            if dirty:
                print(f"FAILED merge {step['component']}: {directory} has uncommitted changes: {', '.join(dirty)}")
                return 2
            merge_rc, merge_out = run(release.pr_merge_argv(), directory)
            if merge_rc != 0:
                print(f"FAILED merge {step['component']}: {merge_out.strip()}")
                return 2
            # `gh pr merge --squash` lands the bump as a brand-new commit on
            # the default branch upstream; it does not touch this local
            # checkout, which `bump_pyproject`/`bump_manifest` left on
            # `release/<version>`. The very next step for this same directory
            # is the pre-existing `tag`/`tag_self` executor, which tags HEAD
            # with no ref — so without switching back and pulling here, it
            # would tag the stale pre-squash commit on the release branch,
            # not what actually landed, and leave the checkout parked off
            # the default branch for the next release to refuse.
            default = _default_branch(directory, run)
            co_rc, co_out = run(release.checkout_ref_argv(directory, default), None)
            if co_rc != 0:
                print(f"FAILED merge {step['component']}: {co_out.strip()}")
                return 2
            pull_rc, pull_out = run(release.pull_argv(directory, default), None)
            if pull_rc != 0:
                print(f"FAILED merge {step['component']}: {pull_out.strip()}")
                return 2
            print(f"merge {step['component']}: merged")
        elif kind == "tag_self":
            self_tag_rc, self_tag_out = run(release.tag_argv(umbrella, version), None)
            if self_tag_rc != 0:
                print(f"FAILED tag_self coxswain: {self_tag_out.strip()}")
                return 2
            self_push_rc, self_push_out = run(release.push_argv(umbrella, version), None)
            if self_push_rc != 0:
                print(f"FAILED push coxswain: {self_push_out.strip()}")
                return 2
            print(f"tag_self coxswain: {step['tag']}")
        elif kind == "wait_workflows":
            directory = umbrella if step["component"] == "coxswain" else release.component_dir(root, step["component"], overrides)
            ok, detail = _wait_workflows(directory, step["tag"], step["component"], run)
            if not ok:
                # docs/design/release-discipline.md §2 says exit code 1; every
                # other failure branch in this function returns 2, and that
                # file-wide convention wins here for consistency with its siblings.
                print(f"FAILED wait_workflows {step['component']}: {detail}")
                return 2
            print(f"wait_workflows {step['component']}: {detail}")
        elif kind == "github_release":
            directory = umbrella if step["component"] == "coxswain" else release.component_dir(root, step["component"], overrides)
            if step["heading"] is None:
                notes_path = str(Path(umbrella) / step["notes_path"])
            else:
                bumped = any(s["kind"] == "tag" and s["component"] == step["component"] for s in steps)
                from_tag = (_previous_release_tag(umbrella, step["component"], version, run)
                            if bumped else step.get("from"))
                text = _github_release_notes_text(umbrella, step["notes_path"], step["heading"], from_tag, step.get("link"))
                with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
                    tmp.write(text)
                notes_path = tmp.name
            view_rc, _ = run(release.github_release_view_argv(step["tag"]), directory)
            argv = (release.github_release_edit_argv(step["tag"], notes_path) if view_rc == 0 else
                    release.github_release_create_argv(step["tag"], step["title"], notes_path))
            gr_rc, gr_out = run(argv, directory)
            if gr_rc != 0:
                print(f"FAILED github_release {step['component']}: {gr_out.strip()}")
                return 2
            print(f"github_release {step['component']}: {step['tag']}")
        elif kind == "tap_formula_pr":
            try:
                sdist = release.sdist_from_index(fetch_index(step["index_url"]))
            except (OSError, ValueError) as exc:
                sdist, detail = None, str(exc)
            else:
                detail = "the index lists no sdist"
            if sdist is None:
                print(f"FAILED tap_formula_pr tap: {step['index_url']}: {detail}")
                return 2
            directory = release.component_dir(root, release.TAP_CHECKOUT, overrides)
            formula = Path(directory) / step["path"]
            ok, detail = _release_bump(
                directory, step["branch"], step["title"], [step["path"]],
                lambda f=formula, s=sdist: f.write_text(release.bumped_formula_text(f.read_text(), version, *s)), run)
            if not ok:
                print(f"FAILED tap_formula_pr tap: {detail}")
                return 2
            for argv, cwd in ((release.push_branch_argv(directory, step["branch"]), None),
                              (release.pr_create_argv(step["title"], f"Bumps the formula to {version} on PyPI."), directory)):
                rc, out = run(argv, cwd)
                if rc != 0:
                    print(f"FAILED tap_formula_pr tap: {out.strip()}")
                    return 2
            print(f"tap_formula_pr tap: {step['title']}")
        else:
            print(f"FAILED {kind} {step.get('component', '')}: no executor for this step kind")
            return 2
    return 0


def _maintainer_remote_url(directory: str) -> str | None:
    """`git -C <directory> remote get-url origin`, or None when git could
    not read it — an unreadable remote is not a maintainer's checkout."""
    result = subprocess.run(["git", "-C", directory, "remote", "get-url", "origin"],
                             capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def _tools_repository_url() -> str | None:
    """coxswain-tools' own `Repository` project URL, from installed package
    metadata — a live read that survives shipping as a wheel, unlike a
    `pyproject.toml` path that only resolves in a checkout. `None` when the
    package (or that URL) can't be found, e.g. an uninstalled checkout."""
    try:
        urls = importlib.metadata.metadata("coxswain-tools").get_all("Project-URL") or []
    except importlib.metadata.PackageNotFoundError:
        return None
    for entry in urls:
        name, _, url = entry.partition(",")
        if name.strip() == "Repository":
            return url.strip()
    return None


def _tap_state(directory: str) -> str:
    """`clean`, `dirty` or `absent`; a checkout whose status git cannot read is `absent`."""
    if not (Path(directory) / ".git").exists():
        return "absent"
    result = subprocess.run(["git", "-C", directory, "status", "--porcelain"], capture_output=True, text=True)
    return "absent" if result.returncode != 0 else "dirty" if result.stdout.strip() else "clean"


def _release(a: argparse.Namespace) -> int:
    manifest_path = Path(a.manifest) if a.manifest else Path("coxswain") / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print(f"refusing: no manifest at {manifest_path}")
        return 2
    checkout = str(manifest_path.resolve().parent)
    remote = _maintainer_remote_url(checkout)
    if remote is None or not release.is_maintainer_remote(remote):
        print(f"refuse: {checkout} is not a ppfenning/coxswain checkout (cox dev release runs on a maintainer's machine)")
        return 2
    root = a.root or "."
    overrides = dict(pair.split("=", 1) for pair in (a.checkout or []))
    component_dirs = {name: release.component_dir(root, name, overrides) for name in manifest.get("components", {})}
    plan = release_check.facts_plan(root, manifest)
    facts = {**plan, **release_check.gather_version_facts(manifest, str(manifest_path), component_dirs, plan["umbrella"])}
    drifts = release_check.run_checks(facts)
    existing_tags = {name: _remote_tags(spec["repo"]) for name, spec in manifest.get("components", {}).items()
                      if spec.get("repo")}
    pinned_commits = {}
    for name, spec in manifest.get("components", {}).items():
        if spec.get("repo") and not spec.get("lockstep", True):
            directory = release.component_dir(root, name, overrides)
            rc, out = _real_run(["git", "-C", directory, "rev-list", f"{spec['tag']}..HEAD", "--count"], None)
            pinned_commits[name] = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0
    component_versions = {}
    for name, directory in component_dirs.items():
        pyproject_path = Path(directory) / "pyproject.toml"
        if not pyproject_path.exists():
            continue
        found = release.component_version(pyproject_path.read_text())
        if found is not None:
            component_versions[name] = found
    plan_steps = release.release_plan(
        manifest, a.version, existing_tags, component_versions=component_versions, pinned_commits=pinned_commits,
        tools_repository_url=_tools_repository_url(),
        tap_state=_tap_state(release.component_dir(root, release.TAP_CHECKOUT, overrides)))
    steps = release.gate(drifts, a.allow_doc_drift, plan_steps) + plan_steps
    if a.dry_run:
        for step in steps:
            print(f"{step['kind']} {step['component']}: {_release_detail(step)}")
        return 2 if any(step["kind"] == "refuse" for step in steps) else 0
    umbrella = a.umbrella or str(Path(root) / "coxswain")
    return _release_execute(steps, a.version, root, overrides, umbrella, _real_run, manifest, str(manifest_path))


def _backfill_github_releases(a: argparse.Namespace) -> int:
    """Walks the umbrella's docs/releases/<version>.md files oldest-first,
    creating or editing the GitHub Release for every tagged repo at that
    version whose body doesn't already match the one this command would
    write, so a rerun touches nothing already current. Each version's own
    manifest.toml — `git show v<version>:manifest.toml`, falling back to the
    manifest at HEAD when that tag predates the file — decides every
    component's tag; a pinned component not tagged or rejoined this version
    is skipped, since its release was already backfilled at the version it
    was actually cut and a later cut must never rewrite it. `from_tag`
    carries forward the last tag this walk saw for a component, mirroring
    the live step's own pre-bump `spec["tag"]` read rather than the already-
    bumped value that version's own manifest now shows. The first failed
    `gh` write stops the walk, the same as the live `github_release` step."""
    root = a.root
    umbrella = str(Path(root) / "coxswain")
    head_manifest = _load_manifest(Path(umbrella) / "manifest.toml") or {}
    tools_repository_url = _tools_repository_url()
    releases_dir = Path(umbrella) / "docs" / "releases"
    versions = release.versions_oldest_first(p.stem for p in releases_dir.glob("*.md") if p.stem != "index")
    previous_tag: dict[str, str] = {}
    for version in versions:
        show_rc, show_out = _real_run(["git", "-C", umbrella, "show", f"v{version}:manifest.toml"], None)
        manifest = tomllib.loads(show_out) if show_rc == 0 else head_manifest
        slug = release.umbrella_release_slug(manifest, tools_repository_url)
        notes_path = f"docs/releases/{version}.md"
        link = f"https://github.com/{slug}/releases/tag/v{version}" if slug else None
        items = [("coxswain", slug, f"v{version}", umbrella, None, None, f"coxswain {version}")]
        for name, spec in manifest.get("components", {}).items():
            if not spec.get("repo"):
                continue
            tag = f"v{version}" if spec.get("lockstep", True) else spec["tag"]
            from_tag = previous_tag.get(name, spec.get("tag"))
            previous_tag[name] = spec.get("tag")
            if tag == f"v{version}":
                items.append((name, spec["repo"], tag, release.component_dir(root, name),
                              f"## coxswain-{name}", from_tag, f"coxswain-{name} {version}"))
        for _name, repo, tag, directory, heading, from_tag, title in items:
            if not Path(directory).is_dir():
                # An old manifest can name a component that has no checkout
                # here (the 0.2.0 `hud`); its release is not ours to write.
                print(f"skipped {repo} {tag}: no checkout at {directory}")
                continue
            text = (Path(umbrella, notes_path).read_text(encoding="utf-8") if heading is None else
                    _github_release_notes_text(umbrella, notes_path, heading, from_tag, link))
            view_rc, view_out = _real_run(release.github_release_body_argv(tag), directory)
            state = "created" if view_rc != 0 else ("already-current" if view_out.strip() == text.strip() else "edited")
            prefix = "would " if a.dry_run else ""
            shown = {"created": "create", "edited": "edit"}.get(state, state) if a.dry_run else state
            if state != "already-current" and not a.dry_run:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as tmp:
                    tmp.write(text)
                argv = (release.github_release_create_argv(tag, title, tmp.name) if state == "created" else
                        release.github_release_edit_argv(tag, tmp.name))
                write_rc, write_out = _real_run(argv, directory)
                if write_rc != 0:
                    print(f"FAILED {repo} {tag}: {write_out.strip()}")
                    return 2
            print(f"{prefix}{shown} {repo} {tag}")
    return 0


def _release_check(a: argparse.Namespace) -> int:
    manifest_path = Path(a.manifest) if a.manifest else Path("coxswain") / "manifest.toml"
    manifest = _load_manifest(manifest_path)
    if manifest is None:
        print(f"refusing: no manifest at {manifest_path}")
        return 2
    root = a.root or "."
    overrides = dict(pair.split("=", 1) for pair in (a.checkout or []))
    component_dirs = {name: release.component_dir(root, name, overrides) for name in manifest.get("components", {})}
    readmes = {name: str(Path(d) / "README.md") for name, d in component_dirs.items()}
    pyprojects = {name: str(Path(d) / "pyproject.toml") for name, d in component_dirs.items()}
    plan = release_check.facts_plan(root, manifest)
    facts = {
        **plan,
        **release_check_cli.gather_cli_facts(root, _real_run),
        **release_check_manifest.gather_manifest_facts(manifest, str(manifest_path), plan["component_docs"], plan["release_notes"]),
        **release_check_notes.gather_notes_facts(root, manifest, subprocess.run),
        **release_check_pages.gather_page_facts(root, manifest, subprocess.run),
        **release_check_readmes.gather_readmes_facts(
            readmes, manifest, release_check_readmes.resolve_docs_base(str(Path(plan["umbrella"]) / "mkdocs.yml"))
        ),
        **release_check.gather_version_facts(manifest, str(manifest_path), component_dirs, plan["umbrella"]),
        **release_check_index.gather_release_index_facts(plan["umbrella"]),
        "pyprojects": pyprojects,
    }
    drifts = release_check.run_checks(facts)
    rendered = release_check.render(drifts, len(release_check.CHECKS))
    print(json.dumps({"checks_run": len(release_check.CHECKS), "drifts": release_check.to_json(drifts)}) if a.json else rendered)
    return 0


def _dev_commands(a: argparse.Namespace) -> int:
    """`cox dev commands render`: rewrite the README's marked Commands block,
    and with --pages-dir the slash-command pages, from `COMMAND_TABLE`."""
    # tools' public command table is the input; imported here so the release
    # commands never need coxswain-tools installed.
    from agent_tools.cli import COMMAND_TABLE

    from devtools import commands_render

    if a.target in ("all", "readme"):
        readme = Path(a.readme)
        text = _read_text_or_none(readme)
        if text is None:
            print(f"commands render: cannot read {readme}"); return 2
        updated = commands_render.splice(text, commands_render.render_readme_block(COMMAND_TABLE))
        if updated is None:
            print(f"commands render: {readme} lacks the {commands_render.BEGIN} / {commands_render.END} markers"); return 2
        readme.write_text(updated, encoding="utf-8")
    if a.target == "pages" and not a.pages_dir:
        print("commands render: --target pages needs --pages-dir"); return 2
    if a.target in ("all", "pages") and a.pages_dir:
        out = Path(a.pages_dir)
        out.mkdir(parents=True, exist_ok=True)
        for name, page in commands_render.render_pages(COMMAND_TABLE).items():
            (out / name).write_text(page, encoding="utf-8")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uv run --frozen python -m devtools",
                                description="maintainer commands for the Coxswain repositories; not needed to use Coxswain")
    sub = p.add_subparsers(dest="cmd", required=True)

    rel = sub.add_parser("release", help="the lockstep tag/bump-manifest/notes plan across coxswain's manifest, or (without --dry-run) tags and pushes every component")
    rel.add_argument("version")
    rel.add_argument("--manifest")
    rel.add_argument("--dry-run", action="store_true")
    rel.add_argument("--root", default=".")
    rel.add_argument("--checkout", action="append", default=None, metavar="NAME=PATH")
    rel.add_argument("--umbrella")
    rel.add_argument("--allow-doc-drift", dest="allow_doc_drift", metavar="REASON", default=None,
                     help="proceed despite a standing release-check drift, naming why")
    rel.set_defaults(fn=_release)

    chk = sub.add_parser("release-check", help="gather facts and print drifts between the CLI, the manifest, the docs and the release notes")
    chk.add_argument("--manifest")
    chk.add_argument("--root", default=".")
    chk.add_argument("--json", action="store_true")
    chk.add_argument("--checkout", action="append", default=None, metavar="NAME=PATH")
    chk.set_defaults(fn=_release_check)

    back = sub.add_parser("backfill-github-releases",
                          help="walk the umbrella's past docs/releases/<version>.md files oldest-first, creating or editing the GitHub Release for every tagged repo missing or drifted from one")
    back.add_argument("--root", required=True, help="the checkouts root containing the umbrella and every component")
    back.add_argument("--dry-run", action="store_true")
    back.set_defaults(fn=_backfill_github_releases)

    cmds = sub.add_parser("commands", help="write the plugin's slash-command pages and the README's Commands section from the command table")
    cmds.add_argument("verb", choices=("render",), help="the only action: render")
    cmds.add_argument("--target", choices=("all", "pages", "readme"), default="all")
    cmds.add_argument("--pages-dir", help="where the slash-command pages are written; without it only the README is rendered")
    cmds.add_argument("--readme", default=str(TOOLS_README), help="default: ../coxswain-tools/README.md beside this checkout")
    cmds.set_defaults(fn=_dev_commands)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(sys.argv[1:] if argv is None else list(argv))
    return a.fn(a)
