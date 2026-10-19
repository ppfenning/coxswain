"""The pure planner over coxswain's release: what `python -m devtools release
<version> --dry-run` prints, and the steps `release <version>` runs through an
injected runner. No filesystem, subprocess or clock here — `cli.py` gathers the
existing tags, resolves checkout directories, and runs the git commands at the edge.

One repository, one distribution, one CalVer version (`YYYY.M.N`, tag
`vYYYY.M.N`): the coxswain checkout is the release. A cut bumps the root
pyproject.toml and the manifest's version on a release branch and lands it,
tags the repository (the tag push publishes `coxswain` to PyPI), writes the
GitHub release from docs/releases/<version>.md, and opens the tap formula PR.
A `[components]` entry (only dash, its own repository) is tagged at the
release version only when it has commits since its manifest tag; an unchanged
one keeps its tag and the manifest keeps naming it."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import yaml

from agent_tools.release_version import CALVER_RE, pep440, release_key
from devtools.release_check_index import index_section
from devtools.release_notes_blanks import find_blanks

UMBRELLA = "coxswain"
UMBRELLA_REPO = "ppfenning/coxswain"


def _refuse(component: str | None, detail: str) -> list[dict]:
    return [{"kind": "refuse", "component": component, "detail": detail}]


_VERSIONS_LABEL_RE = re.compile(r"^(\S+) (pyproject\.toml|Cargo\.toml) is (\S+), manifest wants (\S+)")
# The component bump that rewrites each file a `versions` drift can name.
_BUMP_KIND_FOR_FILE = {"pyproject.toml": "bump_pyproject", "Cargo.toml": "bump_cargo"}
_PERFORMS_BUMP_SUFFIX_RE = re.compile(r"\s*\(devtools release [^)]*\)$")


def _drift_line(d, will_bump: bool = False) -> str:
    a = f"{d.a_file}:{d.a_line}" if d.a_line is not None else d.a_file
    b = f"{d.b_file}:{d.b_line}" if d.b_line is not None else d.b_file
    correction = d.correction
    if d.check == "versions":
        correction = _PERFORMS_BUMP_SUFFIX_RE.sub("", correction)
        correction += " — the release bumps it" if will_bump else " — bump it by hand"
    return f"{d.check}: {a} <-> {b} — {correction}"


def _versions_drift_is_planned(d, plan_steps: Sequence[dict]) -> bool:
    """True when `plan_steps` already carries the bump that resolves `d`: a
    `bump_pyproject` step (`bump_cargo` when the drift names a Cargo.toml) for
    the same component landing at the version the drift wants, or — when `d`
    names the umbrella's own pyproject.toml — a `bump_version` step landing
    there. `d.correction`'s own wording
    (`"<label> <file> is <found>, manifest wants <wants>"`, written by
    `release_check.check_versions`) is the only place that names both the
    component and the target version, so it is parsed rather than re-derived."""
    m = _VERSIONS_LABEL_RE.match(d.correction)
    if not m:
        return False
    label, file, _found, wants = m.groups()
    if label == "umbrella":
        return any(s["kind"] == "bump_version" and s.get("to") == wants for s in plan_steps)
    return any(s["kind"] == _BUMP_KIND_FOR_FILE[file] and s.get("component") == label and s.get("to") == wants
               for s in plan_steps)


def blank_names(notes_text: str) -> list[str]:
    """Names of the blank markers a notes draft still holds; empty for a finished page."""
    return list(find_blanks(notes_text))


def gate(drifts: Sequence, allow_reason: str | None, plan_steps: Sequence[dict] = ()) -> list[dict]:
    """A `notes_blank` drift always refuses: `allow_reason` cannot waive an unwritten paragraph.
    Every other drift goes through `_gate_drifts`."""
    blanks = [d for d in drifts if d.check == "notes_blank"]
    rest = [d for d in drifts if d.check != "notes_blank"]
    blank_steps = [{"kind": "refuse", "component": d.check, "detail": _drift_line(d)} for d in blanks]
    return blank_steps + _gate_drifts(rest, allow_reason, plan_steps)


def _gate_drifts(drifts: Sequence, allow_reason: str | None, plan_steps: Sequence[dict] = ()) -> list[dict]:
    """Refuse steps naming each drift, or one note step when `allow_reason`
    says why they stand; empty when `drifts` is empty. A `versions` drift
    still refuses unless `plan_steps` (the same invocation's `release_plan`
    output) already carries the bump that resolves it, in which case it
    becomes a note instead — the plan is about to do exactly what the drift
    asks for. A `versions` drift the plan does not touch, or a pyproject
    ahead of the manifest (no bump step is ever planned for that), still
    refuses."""
    if not drifts:
        return []
    versions = [d for d in drifts if d.check == "versions"]
    other = [d for d in drifts if d.check != "versions"]
    bumped = [d for d in versions if _versions_drift_is_planned(d, plan_steps)]
    blocking = [d for d in versions if d not in bumped]
    blocking_steps = [{"kind": "refuse", "component": d.check, "detail": _drift_line(d)} for d in blocking]
    bumped_steps = [{"kind": "note", "component": d.check, "detail": _drift_line(d, will_bump=True)} for d in bumped]
    if not other:
        return blocking_steps + bumped_steps
    if allow_reason is None:
        return blocking_steps + bumped_steps + [{"kind": "refuse", "component": d.check, "detail": _drift_line(d)} for d in other]
    plural = "" if len(other) == 1 else "s"
    return blocking_steps + bumped_steps + [{"kind": "note", "component": "release-check",
             "detail": f"{allow_reason} ({len(other)} drift{plural} allowed)"}]


def is_maintainer_remote(url: str) -> bool:
    """True when `url` names the ppfenning/coxswain repository itself — the one
    checkout a release is allowed to run against (not coxswain-dash, not the tap)."""
    return re.search(rf"[/:]{re.escape(UMBRELLA_REPO)}(?:\.git)?/?$", url.strip()) is not None


def parse_ls_remote(text: str) -> list[str]:
    """Tag names from `git ls-remote --tags` output, peeled `^{}` refs skipped.
    Pure: the edge fetches the text, this decides what it means."""
    refs = (line.split("\t", 1)[1] for line in text.splitlines() if "\t" in line)
    return [ref[len("refs/tags/"):] for ref in refs if ref.startswith("refs/tags/") and not ref.endswith("^{}")]


def _with_wait_workflows(steps: list[dict]) -> list[dict]:
    """`steps` with a `wait_workflows` step inserted immediately after every
    `tag` or `tag_self` step, naming that same component and tag — the
    release refusing to call itself done until the workflows the tag
    started finish. No other kind gets one."""
    out = []
    for step in steps:
        out.append(step)
        # `rejoin` tags and pushes exactly like `tag`, so it waits the same way.
        if step["kind"] in ("tag", "tag_self", "rejoin"):
            out.append({"kind": "wait_workflows", "component": step["component"], "tag": step["tag"]})
    return out


def declares_tag_trigger(workflow_text: str) -> bool:
    """True when a GitHub Actions workflow's `on: push: tags:` block names
    at least one pattern. PyYAML's default loader reads a bare `on` key as
    the boolean `True` under YAML 1.1, so both spellings are read; a list
    form of `on:` (e.g. `on: [push, pull_request]`) never names a tag."""
    try:
        doc = yaml.safe_load(workflow_text) or {}
    except yaml.YAMLError:
        return False
    if not isinstance(doc, dict):
        return False
    on = doc.get("on", doc.get(True))
    push = on.get("push") if isinstance(on, dict) else None
    return bool(push.get("tags")) if isinstance(push, dict) else False


def _bump_and_land(bump_step: dict, branch: str, body: str) -> list[dict]:
    """`bump_step` landed the way `cox runs land` lands: `push` the branch it
    was committed to, `pr_create`, `wait_checks`, `merge` — the kinds
    `_execute_land_step` (`agent_tools/cli.py`) already knows how to run."""
    component = bump_step["component"]
    title = bump_step["commit_subject"]
    return [bump_step,
            {"kind": "push", "component": component, "branch": branch},
            {"kind": "pr_create", "component": component, "title": title, "body": body},
            {"kind": "wait_checks", "component": component},
            {"kind": "merge", "component": component}]


def _component_github_release_step(name: str, repo: str, tag: str, version: str, umbrella_slug: str | None,
                                    fallback_from: str | None = None) -> dict:
    """A `github_release` step for a component tagged this run: its own
    `## coxswain-<name>` section of the umbrella's release notes, linked back
    to the umbrella's release. `fallback_from` names the tag `cli.py` reports
    "unchanged since" when that section doesn't exist — only a `rejoin`
    component isn't always written one. No `link` at all when `umbrella_slug`
    couldn't be resolved."""
    step = {"kind": "github_release", "component": name, "repo": repo, "tag": tag,
            "title": f"coxswain-{name} {version}", "notes_path": f"docs/releases/{version}.md",
            "heading": f"## coxswain-{name}"}
    if fallback_from is not None:
        step["from"] = fallback_from
    if umbrella_slug is not None:
        step["link"] = f"https://github.com/{umbrella_slug}/releases/tag/{tag}"
    return step


_TAP_REPO = "ppfenning/homebrew-coxswain"
_PYPI_PACKAGE = "coxswain"


TAP_CHECKOUT = "homebrew-coxswain"


def towpath_version(repo_components: list[tuple[str, Mapping]], tagged: set[str] | list[str], version: str) -> str:
    """The dash release whose towpath tarballs the formula installs: `version` when this release tags dash (or the
    manifest already pins it at `v<version>`, or names no dash), else dash's pinned manifest tag without its `v`."""
    spec = next((spec for name, spec in repo_components
                 if (name == "dash" or spec.get("provides") == "towpath") and name not in tagged), None)
    tag = str(spec.get("tag") or "") if spec is not None else ""
    return tag.removeprefix("v") if tag and tag != f"v{version}" else version


def _tap_formula_steps(version: str, tap_state: str, towpath: str | None = None) -> list[dict]:
    """The tap formula PR every release ends with, since every umbrella tag publishes `coxswain` to PyPI;
    a `refuse` instead when the tap checkout is not `clean`. `towpath` is the dash release the formula's
    towpath resources point at (`towpath_version`), `version` when None."""
    if tap_state != "clean":
        return _refuse("tap", f"tap checkout {_TAP_REPO} is {tap_state}; the formula bump needs a clean checkout")
    return [{"kind": "tap_formula_pr", "component": "tap", "repo": _TAP_REPO, "path": "Formula/cox.rb",
             "version": version, "branch": f"release/{version}", "title": f"cox {version}",
             "index_url": f"https://pypi.org/pypi/{_PYPI_PACKAGE}/{pep440(version)}/json",
             "towpath_version": towpath or version}]


def _tap_only_plan(repo_components: list[tuple[str, Mapping]], changed: set[str], new_tag: str,
                    existing_tags: Mapping[str, list[str] | None], version: str, tap_state: str) -> list[dict]:
    """Just the tap step once `new_tag` is on the umbrella and on every component this release tags, else one `refuse`.

    A component holds the release when it has commits or the manifest already pins it at `new_tag`.
    Presence is required: an umbrella the caller did not look up is unknown, which refuses."""
    holders = [name for name, spec in repo_components if name in changed or spec.get("tag") == new_tag]
    checked = holders + [UMBRELLA]
    unknown = sorted(name for name in checked if existing_tags.get(name) is None)
    if unknown:
        return _refuse(", ".join(unknown), f"tags unknown for {', '.join(unknown)} (remote unreadable); tap-only cannot confirm {new_tag}")
    missing = sorted(name for name in checked if new_tag not in existing_tags[name])
    if missing:
        return _refuse(", ".join(missing), f"tap-only needs tag {new_tag} on every repo; missing on {', '.join(missing)}")
    return _tap_formula_steps(version, tap_state, towpath_version(repo_components, holders, version))


def sdist_from_index(payload: Mapping) -> tuple[str, str] | None:
    """The `(url, sha256)` of the sdist in a PyPI `/pypi/<name>/<version>/json` payload, or None when it lists none."""
    return next(((u["url"], u["digests"]["sha256"]) for u in payload.get("urls", [])
                 if u.get("packagetype") == "sdist"), None)


_DASH_REPO = "ppfenning/coxswain-dash"
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def towpath_asset_urls(version: str) -> dict[str, str]:
    """The dash release's towpath tarball url for each platform; each has its digest in the `.sha256` asset beside it."""
    base = f"https://github.com/{_DASH_REPO}/releases/download/v{version}"
    return {target: f"{base}/towpath-v{version}-{target}.tar.gz" for target in (TOWPATH_MACOS_ARM, TOWPATH_LINUX_INTEL)}


def sha256_from_asset(text: str) -> str | None:
    """The first whitespace-separated token of a `.sha256` asset when it is 64 hex chars, else None."""
    token = next(iter(text.split()), "")
    return token if _SHA256_RE.fullmatch(token) else None


def towpath_assets(version: str, texts: Mapping[str, str | None]) -> tuple[TowpathAssets | None, str | None]:
    """`(assets, None)`, or `(None, reason)` naming the first `.sha256` asset in `texts` that is missing, empty or malformed.

    `texts` maps each `.sha256` asset url to its body, or None when it could not be fetched."""
    found = {}
    for target, url in towpath_asset_urls(version).items():
        sha = sha256_from_asset(texts.get(f"{url}.sha256") or "")
        if sha is None:
            return None, f"{url}.sha256 is missing, empty or not a 64-hex digest"
        found[target] = (url, sha)
    return found, None


def _joins(spec: Mapping, version: str) -> bool:
    """A component whose `since` is this version has no earlier tag to count commits from."""
    return str(spec.get("since", "")) == version


def _component_steps(name: str, spec: Mapping, version: str, umbrella_slug: str | None,
                     found: str | None, is_cargo: bool) -> list[dict]:
    """A changed component's own release: `tag` and `github_release`, after a version bump landed first when its
    checkout's `found` version (pyproject.toml, or Cargo.toml when `is_cargo`) is behind `version`."""
    new_tag = "v" + version
    tag_step = {"kind": "tag", "component": name, "repo": spec["repo"], "tag": new_tag}
    gr_step = _component_github_release_step(name, spec["repo"], new_tag, version, umbrella_slug,
                                               fallback_from=spec["tag"])
    found_key = release_key(found) if found is not None else None
    if found_key is None or found_key >= release_key(version):
        return [tag_step, gr_step]
    branch = f"release/{version}"
    manifest_file = "Cargo.toml" if is_cargo else "pyproject.toml"
    kind = "bump_cargo" if is_cargo else "bump_pyproject"
    subject = f"{'cargo' if is_cargo else 'pyproject'}: bump to {version} to match the tag"
    bump_step = {"kind": kind, "component": name, "repo": spec["repo"],
                 "branch": branch, "commit_subject": subject, "from": found, "to": version}
    body = f"Bumps {name}'s {manifest_file} version to {version} to match tag {new_tag}.\n\n{pr_footer(version)}"
    return _bump_and_land(bump_step, branch, body) + [tag_step, gr_step]


def release_plan(manifest: Mapping, version: str, existing_tags: Mapping[str, list[str] | None],
                  component_versions: Mapping[str, str | None] | None = None,
                  pinned_commits: Mapping[str, int] | None = None,
                  repository_url: str | None = None,
                  tap_state: str = "clean",
                  cargo_versions: Mapping[str, str | None] | None = None,
                  *, tap_only: bool = False, index_changed: bool = False) -> list[dict]:
    """Steps in order:

    1. per `[components]` entry (only dash): `pinned` when `pinned_commits` shows no commits since its manifest
       `tag`, else its own release (`_component_steps`). A missing count is 0, so pinned: the safe side. A
       component whose `since` is `version` joins with this release and is tagged whatever its count;
    2. `notes`: docs/releases/<version>.md must exist;
    3. when `version` is above the manifest's: `bump_version` (root pyproject.toml, uv.lock and manifest.toml,
       with the tag of each component tagged above) on `release/<version>`, then `push`, `pr_create`,
       `wait_checks`, `merge`. Equal to the manifest's, it is the first cut of a version already declared:
       nothing to bump, unless `index_changed` (the caller found that rendering `docs/releases/index.md` for
       `version` differs from the file on disk): then `commit_index` (that file, on `release/<version>`) in
       the bump's place, landed by the same `push`, `pr_create`, `wait_checks`, `merge`. A plan with a
       bump ignores `index_changed`: the bump commit already carries the rewritten index;
    4. `tag_self` `v<version>` on the repository, `wait_workflows` (the tag push publishes `coxswain` to
       PyPI), `github_release` with the notes page;
    5. `tap_formula_pr`, every release, or a `refuse` naming the tap when `tap_state` (`clean`, `dirty`,
       `absent`) says the checkout is unfit.

    A single `refuse` names the reason when `version` is not CalVer `YYYY.M.N`, is below the manifest's
    version (a legacy 0.x version is below every CalVer one), or `v<version>` already exists on the umbrella
    (`existing_tags["coxswain"]`) or on a component this plan tags; or when a remote this plan tags could
    not be read (None). `repository_url` is the installed `coxswain` package's `Repository` URL, the
    umbrella's slug when the manifest names none. `component_versions`/`cargo_versions` are each
    component checkout's pyproject.toml / Cargo.toml version, gathered at the edge.

    `tap_only` returns exactly `[tap_formula_pr]` when `v<version>` already exists on the umbrella and on
    every component the release tagged, and a single `refuse` naming what is missing or unreadable otherwise."""
    if not CALVER_RE.match(version):
        return _refuse(version, f"{version!r} is not a valid version (expected CalVer YYYY.M.N, such as 2026.10.0)")
    component_versions = component_versions or {}
    cargo_versions = cargo_versions or {}
    new_tag = "v" + version
    repo_components = [(name, spec) for name, spec in manifest.get("components", {}).items() if spec.get("repo")]
    commit_counts = {name: (pinned_commits or {}).get(name) or 0 for name, _ in repo_components}
    tagged = {name for name, spec in repo_components if commit_counts[name] or _joins(spec, version)}
    current = manifest.get(UMBRELLA, {}).get("version")
    current_key = release_key(str(current)) if current is not None else None
    if current_key is not None and release_key(version) < current_key:
        return _refuse(version, f"{version} is not greater than the current version {current}")
    if tap_only:
        return _tap_only_plan(repo_components, tagged, new_tag, existing_tags, version, tap_state)

    # Three states per repository: a list of tags, an empty list (reachable, no tags), or None (the remote
    # could not be read). Unknown is not clean: a reused tag is the one thing a release must never risk.
    checked = [name for name, _ in repo_components if name in tagged] + [UMBRELLA]
    unknown = sorted(name for name in checked if existing_tags.get(name) is None)
    if unknown:
        return _refuse(", ".join(unknown), f"tags unknown for {', '.join(unknown)} (remote unreadable); refusing rather than risk reusing {new_tag}")
    colliding = sorted(name for name in checked if new_tag in (existing_tags.get(name) or []))
    if colliding:
        return _refuse(", ".join(colliding), f"tag {new_tag} already exists on {', '.join(colliding)}")

    umbrella_slug = umbrella_release_slug(manifest, repository_url)
    component_steps = [
        step
        for name, spec in repo_components
        for step in ([{"kind": "pinned", "component": name, "tag": spec["tag"]}] if name not in tagged else
                     _component_steps(name, spec, version, umbrella_slug,
                                      cargo_versions.get(name) if name in cargo_versions else component_versions.get(name),
                                      name in cargo_versions))
    ]
    # The release notes are a page of the docs site, so they live under `docs/` with every other page.
    notes_path = f"docs/releases/{version}.md"
    notes_step = {"kind": "notes", "component": "notes", "path": notes_path}
    branch = f"release/{version}"
    bump_step = {"kind": "bump_version", "component": UMBRELLA, "from": current, "to": version,
                 "branch": branch, "commit_subject": f"release: bump to {version} to match the tag"}
    body = f"Bumps pyproject.toml and manifest.toml to {version} to match tag {new_tag}.\n\n{pr_footer(version)}"
    at_current = current_key is not None and release_key(version) == current_key
    index_step = {"kind": "commit_index", "component": UMBRELLA, "branch": branch,
                  "commit_subject": f"release: land the {version} releases index",
                  "paths": ["docs/releases/index.md"]}
    bump = (_bump_and_land(index_step, branch, pr_footer(version)) if at_current and index_changed else
            [] if at_current else _bump_and_land(bump_step, branch, body))
    release_steps = [{"kind": "tag_self", "component": UMBRELLA, "tag": new_tag},
                     {"kind": "github_release", "component": UMBRELLA, "repo": umbrella_slug, "tag": new_tag,
                      "title": f"coxswain {version}", "notes_path": notes_path, "heading": None}]
    steps = component_steps + [notes_step] + bump + release_steps
    return _with_wait_workflows(steps) + _tap_formula_steps(version, tap_state,
                                                            towpath_version(repo_components, tagged, version))


def release_index_text(existing_index: str, version: str, manifest: Mapping,
                        tagged: Iterable[str] = ()) -> str:
    """`existing_index` with its `## VERSION` section for `version` replaced
    by `index_section`'s current rendering, or that rendering appended when
    no such section exists yet. Keyed on the heading, not the rendered text,
    so a rerun after `manifest` has changed updates that section in place
    instead of leaving a stale one beside a fresh one. Sections split on any
    `## ` at the start of a line, blank line before it or not, since a
    hand-written `index.md` cannot be relied on for that blank line and a
    missed one must never delete a neighbouring version's entry. A new
    section goes before the first older version's, so the page stays
    newest-first. Each
    component is listed at the tag the manifest will hold after the bump:
    `v<version>` when named in `tagged`, else its current manifest tag."""
    tagged_names = set(tagged)
    component_tags = {name: f"v{version}" if name in tagged_names else str(spec.get("tag"))
                       for name, spec in manifest.get("components", {}).items()}
    section = index_section(version, component_tags, manifest.get("components", {}))
    headings = (f"## {version}", f"## `{version}`")
    body = existing_index.strip("\n")
    sections = [s.strip("\n") for s in re.split(r"\n(?=## )", body)] if body else []
    at = next((i for i, s in enumerate(sections)
               if any(s == h or s.startswith(f"{h}\n") for h in headings)), None)
    if at is not None:
        new_sections = [*sections[:at], section, *sections[at + 1:]]
    else:
        older = next((i for i, s in enumerate(sections) if _older_section(s, version)), len(sections))
        new_sections = [*sections[:older], section, *sections[older:]]
    return "\n\n".join(new_sections) + "\n"


def _older_section(section: str, version: str) -> bool:
    """Is `section` headed by a version older than `version`?"""
    m = re.fullmatch(r"## `?([^`\s]+)`?", section.split("\n", 1)[0])
    theirs, ours = (release_key(m.group(1)) if m else None), release_key(version)
    return theirs is not None and ours is not None and theirs < ours


def versions_oldest_first(versions: Iterable[str]) -> list[str]:
    """`versions` (bare, no `v` prefix) ordered oldest-first by the same
    rules `release_plan` sorts by (the legacy 0.x line below every CalVer
    release); a name that doesn't parse as a version sorts after every one
    that does, stable among themselves."""
    parsed = [(v, release_key(v)) for v in versions]
    return [v for v, p in sorted(parsed, key=lambda vp: (1, ()) if vp[1] is None else (0, vp[1]))]


def component_dir(root: str, name: str, overrides: Mapping[str, str] | None = None) -> str:
    """The checkout of a repository the release touches besides the coxswain
    checkout itself — dash, or the tap (`TAP_CHECKOUT`) — under `root` (the
    directory beside the coxswain checkout), unless `overrides` names a
    different path for it: a developer machine's `--checkout name=path`."""
    if overrides and name in overrides:
        return overrides[name]
    plain = Path(root) / name
    try:
        if not plain.is_dir():
            # A developer checkout is cloned under its repository name
            # (`coxswain-graphs`), not its manifest key (`graphs`); without an
            # override, prefer the directory that exists over one that does not.
            prefixed = Path(root) / f"coxswain-{name}"
            if prefixed.is_dir():
                return str(prefixed)
    except OSError:
        # An unreadable root (CI runs the pure-shape tests against "/root")
        # is not a reason to change the answer this function always gave.
        pass
    return str(plain)


def component_version(pyproject_toml_text: str) -> str | None:
    """The version a checkout's own pyproject.toml declares: `[project]
    version`, or `[tool.poetry] version` when the former is absent; `None`
    when neither table carries one."""
    parsed = tomllib.loads(pyproject_toml_text)
    found = parsed.get("project", {}).get("version")
    return found if found is not None else parsed.get("tool", {}).get("poetry", {}).get("version")


def tag_argv(directory: str, version: str) -> list[str]:
    """`git -C <directory> tag -a v<version> -m "coxswain <version>"`."""
    return ["git", "-C", directory, "tag", "-a", "v" + version, "-m", f"coxswain {version}"]


def push_argv(directory: str, version: str) -> list[str]:
    """`git -C <directory> push origin v<version>`."""
    return ["git", "-C", directory, "push", "origin", "v" + version]


def bumped_version_line(text: str, to: str) -> str:
    """`text` (a pyproject.toml or manifest.toml) with its first top-level
    `version = "..."` line rewritten to `to`; unchanged if none is found."""
    return re.sub(r'(?m)^version = "[^"]*"', f'version = "{to}"', text, count=1)


def bumped_uv_lock_text(text: str, package: str, to: str) -> str:
    """`text` (uv.lock) with `package`'s own `[[package]]` stanza's `version`
    line rewritten to `to`; every other package's stanza, and the lockfile's
    own top-of-file `version = 1` schema line, are left untouched."""
    blocks = re.split(r"(?=\n\[\[package\]\])", text)
    return "".join(
        re.sub(r'(?m)^version = "[^"]*"', f'version = "{to}"', b, count=1)
        if re.search(rf'(?m)^name = "{re.escape(package)}"$', b) else b
        for b in blocks)


def bumped_cargo_lock_text(text: str, package: str, to: str) -> str:
    """`text` (Cargo.lock) with `package`'s own `[[package]]` stanza's
    `version` line rewritten to `to`; every other package's stanza is left
    untouched. Cargo.lock's `[[package]]` stanzas share uv.lock's shape
    (`name = "..."` followed by a `version = "..."` line), so the
    split-and-match approach is the same as `bumped_uv_lock_text`."""
    blocks = re.split(r"(?=\n\[\[package\]\])", text)
    return "".join(
        re.sub(r'(?m)^version = "[^"]*"', f'version = "{to}"', b, count=1)
        if re.search(rf'(?m)^name = "{re.escape(package)}"$', b) else b
        for b in blocks)


def checkout_branch_argv(directory: str, branch: str, start_point: str | None = None) -> list[str]:
    """`git -C <directory> checkout -b <branch> [start_point]` — cut from
    `start_point` when given (the tap step passes `origin/<default>` so the
    formula bump never branches from a stale local HEAD, the cause of the
    0.22.0 cut landing on the 0.20.0 formula), otherwise from whatever commit
    `directory` is on, which `_checkout_ready` has already required to be
    the clean default branch."""
    argv = ["git", "-C", directory, "checkout", "-b", branch]
    return argv + [start_point] if start_point else argv


def fetch_argv(directory: str) -> list[str]:
    """`git -C <directory> fetch origin` — brings `origin/HEAD` and every
    remote-tracking branch up to date before the tap step reads the remote's
    default branch, so `checkout_branch_argv`'s `start_point` is never a
    stale `origin/<default>`."""
    return ["git", "-C", directory, "fetch", "origin"]


def add_argv(directory: str, *paths: str) -> list[str]:
    """`git -C <directory> add -- <paths...>`."""
    return ["git", "-C", directory, "add", "--", *paths]


def commit_argv(directory: str, subject: str, *paths: str) -> list[str]:
    """`git -C <directory> commit -m "<subject>" -- <paths...>`."""
    return ["git", "-C", directory, "commit", "-m", subject, "--", *paths]


def push_branch_argv(directory: str, branch: str) -> list[str]:
    """`git -C <directory> push -u origin <branch>` — a branch push, unlike
    `push_argv`'s tag push."""
    return ["git", "-C", directory, "push", "-u", "origin", branch]


def pr_footer(version: str) -> str:
    """The line every bump PR body ends with, naming the release `version`
    this plan is cutting rather than any installed package's own version."""
    return f"🚣 Built with [coxswain](https://github.com/ppfenning/coxswain) {version}"


def pr_create_argv(title: str, body: str) -> list[str]:
    """`gh pr create --title "<title>" --body "<body>"`, run with `cwd` set
    to the checkout the branch was pushed from."""
    return ["gh", "pr", "create", "--title", title, "--body", body]


def pr_checks_argv() -> list[str]:
    """`gh pr checks --watch --fail-fast`, run against the checkout's current
    branch's own pull request."""
    return ["gh", "pr", "checks", "--watch", "--fail-fast"]


def pr_merge_argv() -> list[str]:
    """`gh pr merge --squash --delete-branch`."""
    return ["gh", "pr", "merge", "--squash", "--delete-branch"]


def tap_may_merge(conclusion: str | None) -> bool:
    """True only for the literal conclusion `success`; cancelled, skipped, timed_out, failure and missing are not a pass."""
    return conclusion == "success"


def install_proof_dispatch_argv(tap_ref: str, version: str) -> list[str]:
    """`gh workflow run install-proof.yml -f tap_ref=<tap_ref> -f version=<version>`."""
    return ["gh", "workflow", "run", "install-proof.yml", "-f", f"tap_ref={tap_ref}", "-f", f"version={version}"]


def install_proof_list_argv() -> list[str]:
    """The 20 newest dispatched install-proof runs, as JSON."""
    return ["gh", "run", "list", "--workflow", "install-proof.yml", "--event", "workflow_dispatch",
            "--json", "databaseId", "--limit", "20"]


def install_proof_view_argv(run_id: int) -> list[str]:
    """`gh run view <run_id>` as JSON, the source of the run's conclusion."""
    return ["gh", "run", "view", str(run_id), "--json", "databaseId,status,conclusion,url"]


def install_proof_ids(text: str) -> list[int] | None:
    """Run ids in `gh run list` JSON; `[]` for no runs, None for output that is not a list of runs."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    ok = isinstance(payload, list) and all(isinstance(r, dict) and isinstance(r.get("databaseId"), int) for r in payload)
    return [r["databaseId"] for r in payload] if ok else None


def new_install_proof_runs(before: list[int], after: list[int]) -> list[int]:
    """Ids in `after` newer than every id in `before`; GitHub run ids only grow."""
    return [i for i in after if i > max(before, default=0)]


def install_proof_result(text: str) -> tuple[str | None, str | None]:
    """`(conclusion, url)` from `gh run view` JSON; conclusion None while unfinished or when the output is malformed."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None, None
    return ((payload.get("conclusion") or None, payload.get("url") or None) if isinstance(payload, dict)
            else (None, None))


def checkout_ref_argv(directory: str, ref: str) -> list[str]:
    """`git -C <directory> checkout <ref>` — switches back to an existing
    branch, unlike `checkout_branch_argv`'s `-b`; run after a merge lands the
    bump upstream, so the very next `tag`/`tag_self` step tags what actually
    merged instead of the pre-squash commit the bump branch is still on."""
    return ["git", "-C", directory, "checkout", ref]


def pull_argv(directory: str, branch: str) -> list[str]:
    """`git -C <directory> pull origin <branch>` — brings the squash-merge
    commit `checkout_ref_argv` just switched onto down from `origin`."""
    return ["git", "-C", directory, "pull", "origin", branch]


def github_release_view_argv(tag: str) -> list[str]:
    """`gh release view <tag>` — the idempotence check `github_release` runs first."""
    return ["gh", "release", "view", tag]


def github_release_body_argv(tag: str) -> list[str]:
    """`gh release view <tag> --json body -q .body` — just the body text, with
    no header lines above a `--` separator to strip before comparing it."""
    return ["gh", "release", "view", tag, "--json", "body", "-q", ".body"]


def github_release_create_argv(tag: str, title: str, notes_path: str) -> list[str]:
    """`gh release create <tag> --verify-tag --title "<title>" --notes-file <notes_path>`."""
    return ["gh", "release", "create", tag, "--verify-tag", "--title", title, "--notes-file", notes_path]


def github_release_edit_argv(tag: str, notes_path: str) -> list[str]:
    """`gh release edit <tag> --notes-file <notes_path>` — run instead of `create`
    once `github_release_view_argv` shows the release already exists."""
    return ["gh", "release", "edit", tag, "--notes-file", notes_path]


def extract_release_notes(notes_path: str, heading: str) -> str | None:
    """The text of `notes_path` from the line equal to `heading` (e.g.
    `"## coxswain-crew"`) through the line before the next `"## "` heading or
    end of file, or `None` when the file is missing or `heading` is absent.
    Reads the file itself, unlike the rest of this module: a later backfill
    task calls this the same way against a past release's notes file, with
    nothing else here to import alongside it."""
    try:
        lines = Path(notes_path).read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return None
    start = next((i for i, line in enumerate(lines) if line.rstrip("\n") == heading), None)
    if start is None:
        return None
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return "".join(lines[start:end])


def umbrella_release_slug(manifest: Mapping, repository_url: str | None = None) -> str:
    """The repository's `org/repo`: `manifest["coxswain"]["repo"]` when the
    manifest names one, else the slug of `repository_url` (the installed
    `coxswain` package's own `Repository` URL, gathered by the edge from
    package metadata the way `existing_tags` is), else `ppfenning/coxswain`."""
    repo = manifest.get(UMBRELLA, {}).get("repo")
    if repo:
        return repo
    if repository_url:
        return "/".join(repository_url.rstrip("/").removesuffix(".git").split("/")[-2:])
    return UMBRELLA_REPO


_MANIFEST_SECTION_RE = re.compile(r"^\[components\.([\w-]+)\]\s*$")
_MANIFEST_VERSION_RE = re.compile(r'(\s*version\s*=\s*")[^"]*(")')
_MANIFEST_TAG_RE = re.compile(r'(\s*tag\s*=\s*")v[^"]*(")')
# Any `[table]` or `[table.sub]` header, to know which table an inline-table
# line sits under — an inline component only ever appears directly under the
# bare `[components]` table, never under `[coxswain]` or some other table
# that happens to hold an unrelated inline table with its own `tag` key.
_MANIFEST_TABLE_RE = re.compile(r"^\[([\w.-]+)\]\s*$")
# An inline-table component opens on a `name = {` line and may close on a later one.
_MANIFEST_INLINE_START_RE = re.compile(r"^\s*([\w-]+)\s*=\s*\{(.*)$")
_MANIFEST_INLINE_TAG_RE = re.compile(r'(\btag\s*=\s*")v[^"]*(")')


def _inline_owners(lines: list[str]) -> list[str | None]:
    """The component each line belongs to: the `name = {` entry directly under
    `[components]` whose braces are still open on that line, else None.
    Braces are counted per line; a brace inside a quoted string is not handled."""
    owners = []
    table = None
    owner = None
    depth = 0
    for line in lines:
        text = line.rstrip("\n")
        header = _MANIFEST_TABLE_RE.match(text)
        start = _MANIFEST_INLINE_START_RE.match(text)
        if depth > 0:
            depth = max(0, depth + text.count("{") - text.count("}"))
        else:
            table = header.group(1) if header else table
            opens = table == "components" and start is not None
            owner = start.group(1) if opens else None
            depth = max(0, text.count("{") - text.count("}")) if opens else 0
        owners.append(owner)
    return owners


def rejoined(steps: list[dict]) -> set[str]:
    """Component names `release_plan` tagged this release (dash, when it changed), for `bumped_manifest_text`'s `rejoining`."""
    return {s["component"] for s in steps if s["kind"] in ("tag", "rejoin")}


def bumped_manifest_text(text: str, version: str, rejoining: Iterable[str] = ()) -> str:
    """`text` with `[coxswain] version` rewritten to `version`, and the
    `tag = "v..."` value of each component named in `rejoining` (the
    components this release tags) — whether the component is a
    `[components.<name>]` section or a `name = { ... }` inline table directly
    under `[components]`, on one line or several. Every other component's
    `tag` stays exactly as it reads; a `version` key in any other table,
    comments, blank lines and layout are untouched, and every other key on an
    inline table's lines is kept byte-identical."""
    new_tag = "v" + version
    tagging = set(rejoining)
    section = None
    table = None
    out = []
    lines = text.splitlines(keepends=True)
    for line, owner in zip(lines, _inline_owners(lines)):
        stripped = line.rstrip("\n")
        m = _MANIFEST_SECTION_RE.match(stripped)
        t = _MANIFEST_TABLE_RE.match(stripped)
        if m:
            section = m.group(1)
        elif t:
            section = None
        table = t.group(1) if t else table
        if table == UMBRELLA and _MANIFEST_VERSION_RE.match(line):
            out.append(_MANIFEST_VERSION_RE.sub(lambda m: f"{m.group(1)}{version}{m.group(2)}", line))
        elif owner:
            retag = owner in tagging
            out.append(_MANIFEST_INLINE_TAG_RE.sub(lambda m: f"{m.group(1)}{new_tag}{m.group(2)}", line) if retag else line)
        elif _MANIFEST_TAG_RE.match(line) and section in tagging:
            out.append(_MANIFEST_TAG_RE.sub(lambda m: f"{m.group(1)}{new_tag}{m.group(2)}", line))
        else:
            out.append(line)
    return "".join(out)


_FORMULA_URL_RE = re.compile(r'^(\s*url\s+")[^"]*(")')
_FORMULA_SHA_RE = re.compile(r'^(\s*sha256\s+")[^"]*(")')
_FORMULA_RESOURCE_RE = re.compile(r"^\s*resource\s")
_FORMULA_HEAD_RE = re.compile(r'^(\s*head\s+")[^"]*(".*)$')
HEAD_URL = f"https://github.com/{UMBRELLA_REPO}.git"


TOWPATH_MACOS_ARM = "aarch64-apple-darwin"
TOWPATH_LINUX_INTEL = "x86_64-unknown-linux-gnu"
TowpathAssets = Mapping[str, tuple[str, str]]

_TOWPATH_PLATFORMS = {("on_macos", "on_arm"): TOWPATH_MACOS_ARM, ("on_linux", "on_intel"): TOWPATH_LINUX_INTEL}
_TOWPATH_RESOURCE_RE = re.compile(r'^(\s*)resource\s+"towpath"\s+do\b')
_TOWPATH_VERSION_RE = re.compile(r"towpath.*--version")
_FORMULA_SCOPE_RE = re.compile(r"^(\s*)(on_\w+)\s+do\s*$")
_FORMULA_END_RE = re.compile(r"^(\s*)end\s*$")
_FORMULA_INSTALL_RE = re.compile(r"^  def install\b")
_FORMULA_TEST_RE = re.compile(r"^\s*test do\s*$")
_FORMULA_VENV_RE = re.compile(r"^(\s*virtualenv_install_with_resources)(\s*)$")
_FORMULA_TOP_RESOURCE_RE = re.compile(r"^  resource\s")
_FORMULA_TOP_END_RE = re.compile(r"^  end\s*$")
# The towpath resource exists only on macOS arm and Linux intel, so the install, pip and test lines guard on it.
_TOWPATH_STAGE = ('    if resources.map(&:name).include?("towpath")\n      resource("towpath").stage do\n'
                  '        bin.install "towpath", "coxtop"\n      end\n    end\n')
_TOWPATH_WITHOUT = ' without: resources.map(&:name) & ["towpath"]'
_TOWPATH_TEST = '    system bin/"towpath", "--version" if (bin/"towpath").exist?\n'


def _towpath_block(towpath: TowpathAssets) -> str:
    """The two platform-scoped towpath resource stanzas, 2-space indented, ending in a newline."""
    def stanza(scope: str, arch: str, key: str) -> str:
        url, sha = towpath[key]
        return (f'  {scope} do\n    {arch} do\n      resource "towpath" do\n        url "{url}"\n'
                f'        sha256 "{sha}"\n      end\n    end\n  end\n')
    return stanza("on_macos", "on_arm", TOWPATH_MACOS_ARM) + "\n" + stanza("on_linux", "on_intel", TOWPATH_LINUX_INTEL)


def _rewritten_towpath(text: str, towpath: TowpathAssets) -> str:
    """`text` with each towpath resource's url and sha256 replaced; raises rather than leave a stale pair.

    KeyError: a platform is missing from `towpath`. ValueError: a towpath resource sits under scopes other than
    on_macos/on_arm or on_linux/on_intel, or a platform's new url and sha256 did not land in any block."""
    out = []
    scopes: list[tuple[str, str]] = []
    block: tuple[str, tuple[str, str]] | None = None
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        end, scope, res = _FORMULA_END_RE.match(bare), _FORMULA_SCOPE_RE.match(bare), _TOWPATH_RESOURCE_RE.match(bare)
        if block is not None:
            indent, (url, sha) = block
            if end and end.group(1) == indent:
                block = None
                out.append(line)
            elif _FORMULA_URL_RE.match(line):
                out.append(_FORMULA_URL_RE.sub(lambda m, value=url: f"{m.group(1)}{value}{m.group(2)}", line))
            elif _FORMULA_SHA_RE.match(line):
                out.append(_FORMULA_SHA_RE.sub(lambda m, value=sha: f"{m.group(1)}{value}{m.group(2)}", line))
            else:
                out.append(line)
            continue
        if res:
            names = tuple(name for _, name in scopes[-2:])
            key = _TOWPATH_PLATFORMS.get(names)
            if key is None:
                raise ValueError(f"towpath resource under unrecognised scopes {names}")
            block = (res.group(1), towpath[key])
        elif scope:
            scopes.append((scope.group(1), scope.group(2)))
        elif end and scopes and scopes[-1][0] == end.group(1):
            scopes.pop()
        out.append(line)
    result = "".join(out)
    stale = [key for key in _TOWPATH_PLATFORMS.values()
             if f'url "{towpath[key][0]}"' not in result or f'sha256 "{towpath[key][1]}"' not in result]
    if stale:
        raise ValueError(f"towpath url and sha256 not rewritten for {', '.join(stale)}")
    return result


def _top_resource_end(lines: Sequence[str]) -> int | None:
    """Index of the `  end` closing the last top-level resource, or None."""
    ends = [i for i, line in enumerate(lines) if _FORMULA_TOP_END_RE.match(line)]
    closers = [next((e for e in ends if e > i), None) for i, line in enumerate(lines) if _FORMULA_TOP_RESOURCE_RE.match(line)]
    return max((e for e in closers if e is not None), default=None)


def _with_towpath_block(text: str, towpath: TowpathAssets) -> str:
    """`text` with the towpath block, stage-and-install, pip exclusion and test line added; ValueError if any of them has no anchor."""
    lines = text.splitlines(keepends=True)
    block = _towpath_block(towpath)
    resource_end = _top_resource_end(lines)
    installs = [i for i, line in enumerate(lines) if _FORMULA_INSTALL_RE.match(line)]
    closing = [i for i, line in enumerate(lines) if line.rstrip() == "end"]
    if resource_end is not None:
        inserts = {resource_end + 1: "\n" + block}
    elif installs:
        inserts = {installs[0]: block + "\n"}
    elif closing:
        inserts = {closing[-1]: "\n" + block}
    else:
        raise ValueError("formula has no resource, def install or closing end to anchor the towpath block")
    tests = [i for i, line in enumerate(lines) if _FORMULA_TEST_RE.match(line)]
    body = [_FORMULA_VENV_RE.sub(lambda m: m.group(1) + _TOWPATH_WITHOUT + m.group(2), line) for line in lines]
    # Each requirement below raises when its anchor is missing: a formula that declares the resource but never
    # stages it, never keeps pip off its tarball, or never tests it is not a bumped formula.
    if 'resource("towpath")' in text:
        install_after = {}
    elif installs:
        install_after = {installs[0] + 1: _TOWPATH_STAGE}
    else:
        raise ValueError("formula has no `def install` to stage the towpath resource in")
    if _TOWPATH_WITHOUT not in text and body == lines:
        raise ValueError("formula has no bare `virtualenv_install_with_resources` line to exclude the towpath resource from")
    if _TOWPATH_VERSION_RE.search(text):
        test_after = {}
    elif tests:
        test_after = {tests[0] + 1: _TOWPATH_TEST}
    else:
        raise ValueError("formula has no `test do` to run `towpath --version` in")
    extras = {**install_after, **test_after}
    return "".join(inserts.get(i, "") + extras.get(i, "") + line for i, line in enumerate([*body, ""]))


def bumped_formula_text(text: str, version: str, sdist_url: str, sha256: str, *, towpath: TowpathAssets | None = None) -> str:
    """`text` with the `url` and `sha256` lines before the first `resource` stanza rewritten, and the `head` url
    pointed at the coxswain repository; `version` is carried by `sdist_url` (`coxswain-<version>.tar.gz`).

    `towpath` maps TOWPATH_MACOS_ARM and TOWPATH_LINUX_INTEL to (url, sha256). When given, an existing towpath block
    has its url and sha256 lines rewritten, and a formula without one gains the block, an install line and a test line.
    Either path raises KeyError or ValueError instead of returning a formula with a stale or missing towpath pair."""
    bumped = _bumped_sdist_text(text, sdist_url, sha256)
    if towpath is None:
        return bumped
    if any(_TOWPATH_RESOURCE_RE.match(line.rstrip("\r\n")) for line in bumped.splitlines()):
        return _rewritten_towpath(bumped, towpath)
    return _with_towpath_block(bumped, towpath)


def _bumped_sdist_text(text: str, sdist_url: str, sha256: str) -> str:
    out = []
    in_resource = False
    for line in text.splitlines(keepends=True):
        in_resource = in_resource or _FORMULA_RESOURCE_RE.match(line) is not None
        if not in_resource and _FORMULA_HEAD_RE.match(line):
            out.append(_FORMULA_HEAD_RE.sub(lambda m: f"{m.group(1)}{HEAD_URL}{m.group(2)}", line))
        elif not in_resource and _FORMULA_URL_RE.match(line):
            out.append(_FORMULA_URL_RE.sub(lambda m: f"{m.group(1)}{sdist_url}{m.group(2)}", line))
        elif not in_resource and _FORMULA_SHA_RE.match(line):
            out.append(_FORMULA_SHA_RE.sub(lambda m: f"{m.group(1)}{sha256}{m.group(2)}", line))
        else:
            out.append(line)
    return "".join(out)
